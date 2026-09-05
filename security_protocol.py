#!/usr/bin/env python3
"""Security-Protocol-1 — 카메라 제스처 기반 macOS 락다운.

카메라에 설정된 트리거 제스처를 유지하면 풀스크린 JARVIS 스타일 HUD와 함께
키보드·마우스가 차단되고, 설정된 해제 제스처 시퀀스로만 풀 수 있다.
비상시에는 설정된 비상 키 조합으로 macOS 기본 잠금화면으로 전환된다
(맥 비밀번호를 알아야 풀리므로 보안은 유지됨).

트리거·해제 시퀀스·비상키는 config.local.json에서 로드한다 (저장소 미포함 —
config.example.json을 복사해서 원하는 값으로 수정).

안전장치:
  - 입력 차단용 이벤트 탭은 **락다운 중에만 존재** — 해제 시 완전히 파괴하므로
    락다운 밖에서 입력이 차단되는 상태 자체가 불가능
  - 오버레이가 실제 화면에 뜬 것을 확인한 뒤에만 입력 차단 (실패 시 Basso음 + 취소)
  - 워치독(2초마다): 잠금 아님 + 탭 존재 → 강제 제거 / 잠금 중 오버레이 소실 → 안전 해제
  - 해제/비상탈출/macOS 잠금해제 직후 쿨다운, 잠금화면에서는 트리거 무시
  - 락다운 중 카메라가 죽으면 자동으로 macOS 잠금화면으로 전환
  - 모든 이벤트를 protocol.log에 기록

실행:    ./venv/bin/python security_protocol.py          # 상시 감시 시작
         ./venv/bin/python security_protocol.py --test   # 인식 테스트만 (락다운 없음)
"""

import collections
import ctypes
import datetime
import fcntl
import hmac
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import traceback

# OpenCV가 카메라 권한을 스레드에서 요청하면 실패한다 (launchd 실행 시 특히).
# 권한은 아래 ensure_camera_access()가 메인 스레드에서 직접 처리한다.
os.environ.setdefault("OPENCV_AVFOUNDATION_SKIP_AUTH", "1")

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

import AppKit
import AVFoundation
import objc
import Quartz
from PyObjCTools import AppHelper

# ──────────────────────────── 설정 ────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "gesture_recognizer.task")
LOG_PATH = os.path.join(BASE_DIR, "protocol.log")

SEQUENCE_TIMEOUT_SEC = 15.0            # 첫 단계 후 이 시간 내 완료 못하면 처음부터
COOLDOWN_SEC = 5.0                     # 해제/잠금해제 직후 재발동 금지 시간
STARTUP_GRACE_SEC = 2.0                # 시작 직후 트리거 무시 시간
MIN_CONFIDENCE = 0.50                  # 제스처 신뢰도 하한
GESTURE_GAP_GRACE_SEC = 0.5            # 인식이 이 시간 이내로 잠깐 끊기면 유지로 간주
OVERLAY_VERIFY_SEC = 0.7               # 오버레이 표시 확인 대기 시간
WATCHDOG_SEC = 2.0                     # 워치독 점검 주기

# ── 제스처/비상키 설정 로드 ──
# 실제 값은 config.local.json(gitignore됨)에만 존재하고, 저장소에는
# config.example.json(예시)만 포함된다.
_MODIFIERS = {"control": Quartz.kCGEventFlagMaskControl,
              "option": Quartz.kCGEventFlagMaskAlternate,
              "command": Quartz.kCGEventFlagMaskCommand,
              "shift": Quartz.kCGEventFlagMaskShift}


def _load_config():
    for name in ("config.local.json", "config.example.json"):
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f), name
    return {}, None


CONFIG, CONFIG_SRC = _load_config()
NOTIFY = CONFIG.get("notify", {})      # 폰 알림 설정 (provider: none/ntfy/telegram)
REMOTE = CONFIG.get("remote", {})      # 폰 원격 조종 설정
INTRUDER_DIR = os.path.join(BASE_DIR, "intruders")
INTRUSION_COOLDOWN_SEC = 8.0           # 침입 스냅샷 최소 간격 (스팸 방지)
MAX_UNLOCK_ATTEMPTS = int(CONFIG.get("max_unlock_attempts", 5))
LOCK_FILE = os.path.join(BASE_DIR, ".security_protocol.lock")
TRIGGER_GESTURE = CONFIG.get("trigger_gesture", "Thumb_Down")
TRIGGER_HOLD_SEC = float(CONFIG.get("trigger_hold_sec", 1.5))
# 더블 클랩 v5 — 키보드 타건음 오발동 방지 게이트
CLAP_REQUIRE_VISION = bool(CONFIG.get("clap_require_vision", True))   # 타이핑 자세면 거부
UNLOCK_SEQUENCE = list(CONFIG.get("unlock_sequence",
                                  ["Thumb_Up", "ILoveYou", "Thumb_Up"]))
STEP_HOLD_SEC = float(CONFIG.get("step_hold_sec", 0.8))
EMERGENCY_KEYCODE = int(CONFIG.get("emergency_keycode", 37))
EMERGENCY_FLAGS = 0
for _m in CONFIG.get("emergency_modifiers", ["control", "option", "command"]):
    EMERGENCY_FLAGS |= _MODIFIERS.get(_m, 0)

SOUND_LOCK = "/System/Library/Sounds/Sosumi.aiff"    # 삐 (입력 차단 확정)
SOUND_UNLOCK = "/System/Library/Sounds/Glass.aiff"   # 띠링 (해제)
SOUND_STEP = "/System/Library/Sounds/Tink.aiff"      # 해제 단계 성공
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"    # 락다운 취소/오류

GESTURE_EMOJI = {"Closed_Fist": "✊", "Open_Palm": "✋", "Victory": "✌️",
                 "Pointing_Up": "☝️", "Thumb_Up": "👍", "Thumb_Down": "👎",
                 "ILoveYou": "🤟", "Double_Clap": "👏", "None": "·"}   # 터미널/로그용 (락다운 UI에는 미사용)

# MediaPipe 21개 손 랜드마크 연결 (스켈레톤)
HAND_CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 4),
                    (0, 5), (5, 6), (6, 7), (7, 8),
                    (5, 9), (9, 10), (10, 11), (11, 12),
                    (9, 13), (13, 14), (14, 15), (15, 16),
                    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]

# HUD 색상 (BGR)
HUD_MAIN = (255, 220, 60)     # 시안
HUD_DIM = (140, 95, 40)       # 어두운 시안
HUD_FAINT = (90, 58, 24)      # 아주 어두운 시안
HUD_TXT = (230, 245, 250)     # 밝은 흰-시안
HUD_RED = (70, 70, 255)       # 경고 레드
HUD_GRID = (52, 30, 13)
HUD_GRID2 = (40, 23, 10)      # 미세 그리드
HUD_TINT = (45, 22, 8)        # 카메라 틴트(다크 네이비)
HUD_BG = (22, 11, 5)          # 캔버스 배경


def log(msg):
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def play(path):
    subprocess.Popen(["afplay", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(message, photo_path=None):
    """폰 푸시 알림 (curl 비동기 — 메인 흐름을 절대 막지 않음).

    ntfy: 폰에 ntfy 앱 설치 후 topic 구독만 하면 끝 (계정 불필요).
    telegram: BotFather로 만든 봇 토큰 + chat_id 필요.
    """
    provider = NOTIFY.get("provider", "none")
    try:
        if provider == "telegram":
            token = NOTIFY.get("telegram_bot_token", "")
            chat = str(NOTIFY.get("telegram_chat_id", ""))
            if not token or not chat:
                return
            if photo_path:
                cmd = ["curl", "-sf", "-m", "20",
                       "-F", f"chat_id={chat}", "-F", f"caption={message}",
                       "-F", f"photo=@{photo_path}",
                       f"https://api.telegram.org/bot{token}/sendPhoto"]
            else:
                cmd = ["curl", "-sf", "-m", "20",
                       "-F", f"chat_id={chat}", "-F", f"text={message}",
                       f"https://api.telegram.org/bot{token}/sendMessage"]
        elif provider == "ntfy":
            topic = NOTIFY.get("ntfy_topic", "")
            if not topic:
                return
            url = f"https://ntfy.sh/{topic}"
            if photo_path:
                cmd = ["curl", "-sf", "-m", "20", "-T", photo_path,
                       "-H", f"Title: {message}",
                       "-H", "Filename: intruder.jpg", url]
            else:
                cmd = ["curl", "-sf", "-m", "20", "-d", message, url]
        else:
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def accessibility_trusted():
    try:
        appserv = ctypes.CDLL("/System/Library/Frameworks/"
                              "ApplicationServices.framework/ApplicationServices")
        return bool(appserv.AXIsProcessTrusted())
    except Exception:
        return True   # 판정 불가 시 일단 진행


def ensure_camera_access(timeout=30.0):
    """메인 스레드에서 카메라 권한을 확보한다.

    OpenCV는 권한 요청을 워커 스레드에서 시도하다 실패하므로(특히 launchd로
    실행될 때) AVFoundation으로 직접 요청한다. 최초 1회 시스템 팝업이 뜬다.
    """
    media = AVFoundation.AVMediaTypeVideo
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(media)
    if status == 3:            # AVAuthorizationStatusAuthorized
        return True
    if status in (1, 2):       # restricted / denied
        log("[!] 카메라 권한이 거부되어 있습니다.")
        log("    시스템 설정 → 개인정보 보호 및 보안 → 카메라에서")
        log(f"    {sys.executable} 을 켜주세요.")
        return False

    # notDetermined → 팝업 요청 (콜백이 올 때까지 런루프를 돌린다)
    log("카메라 권한 요청 중 — 팝업이 뜨면 허용해 주세요.")
    result = {}

    def handler(granted):
        result["granted"] = bool(granted)

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        media, handler)
    deadline = time.monotonic() + timeout
    while "granted" not in result and time.monotonic() < deadline:
        AppKit.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            AppKit.NSDefaultRunLoopMode,
            AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.1))
    granted = result.get("granted", False)
    log("카메라 권한 " + ("허용됨" if granted else "거부됨/시간초과"))
    return granted


def session_screen_locked():
    """macOS 잠금화면(로그인창) 상태인지."""
    d = Quartz.CGSessionCopyCurrentDictionary()
    return bool(d and d.get("CGSSessionScreenIsLocked", 0))


def shield_windows_onscreen():
    """WindowServer 기준, 이 프로세스 소유의 고레벨 창이 실제 화면에 몇 개 있는지."""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    pid = os.getpid()
    return sum(1 for i in infos
               if i.get("kCGWindowOwnerPID") == pid
               and i.get("kCGWindowLayer", 0) >= 1000)


def native_lock_screen():
    """macOS 기본 잠금화면으로 전환 (비밀번호 필요)."""
    try:
        login = ctypes.CDLL(
            "/System/Library/PrivateFrameworks/login.framework/login")
        login.SACLockScreenImmediate()
        return
    except Exception:
        pass
    cgsession = ("/System/Library/CoreServices/Menu Extras/User.menu/"
                 "Contents/Resources/CGSession")
    if os.path.exists(cgsession):
        subprocess.run([cgsession, "-suspend"])
    else:
        subprocess.run(["pmset", "displaysleepnow"])


# ──────────────────────────── JARVIS HUD 렌더러 ────────────────────────────
class HUDRenderer:
    """풀스크린 JARVIS HUD — 카메라가 화면 전체를 채우고 그 위에 전부 그린다.

    구성: 홀로그램 오브(유기적 링 + 신경망 노드 애니메이션), 손 스켈레톤/브래킷,
    좌우 데이터 패널, 상하단 스트립, 눈금자, 코너 아크, 파형. 글로우는 최소.
    """

    CAM_W, CAM_H = 640, 480
    PANEL_W = 248

    #        라벨  mcp  pip  tip
    FINGERS = (("THB", 2, 3, 4), ("IDX", 5, 6, 8), ("MID", 9, 10, 12),
               ("RNG", 13, 14, 16), ("PNK", 17, 18, 20))

    def __init__(self):
        self.conf_hist = collections.deque(maxlen=200)
        self.intrusions = 0
        self.fail_count = 0
        rng = random.Random(7)
        # 신경망 노드: (기준각, 반경비, 회전속도, 위상)
        self.nodes = [(rng.uniform(0, 2 * math.pi),
                       rng.uniform(0.30, 1.0),
                       rng.uniform(-0.25, 0.25),
                       rng.uniform(0, 6.28)) for _ in range(80)]
        self._map = (0, 0, 0, 0)   # (rw, rh, ox, oy) 카메라→화면 매핑

    # ── 유틸 ──
    @staticmethod
    def _t(img, s, x, y, scale=0.3, col=HUD_DIM, thick=1):
        cv2.putText(img, s, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, col, thick, cv2.LINE_AA)

    @staticmethod
    def _angle(a, b, c):
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 * n2 == 0:
            return 0.0
        cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        return math.degrees(math.acos(cosv))

    def _hand_px(self, result):
        if not result.hand_landmarks:
            return None
        rw, rh, ox, oy = self._map
        return [(int((1.0 - lm.x) * rw) - ox, int(lm.y * rh) - oy)
                for lm in result.hand_landmarks[0]]

    # ── 메인 ──
    def render(self, cam, result, gesture, score, held, seq_index, now, fps,
               size):
        W, H = size
        self.conf_hist.append(score)

        # 카메라를 화면 가득 (커버 크롭) + 강한 딤/틴트
        scale = max(W / self.CAM_W, H / self.CAM_H)
        rw, rh = int(self.CAM_W * scale + 0.5), int(self.CAM_H * scale + 0.5)
        ox, oy = (rw - W) // 2, (rh - H) // 2
        self._map = (rw, rh, ox, oy)
        bg = cv2.resize(cam, (rw, rh), interpolation=cv2.INTER_LINEAR)
        canvas = bg[oy:oy + H, ox:ox + W].copy()
        canvas = cv2.addWeighted(canvas, 0.40,
                                 np.full_like(canvas, HUD_TINT), 0.60, 0)

        # 도트 매트릭스 + 대형 그리드
        canvas[14::26, 14::26] = HUD_GRID
        for gx in range(0, W, 120):
            cv2.line(canvas, (gx, 0), (gx, H), HUD_GRID2, 1)
        for gy in range(0, H, 120):
            cv2.line(canvas, (0, gy), (W, gy), HUD_GRID2, 1)

        pts = self._hand_px(result)
        if pts:
            center = (sum(pts[i][0] for i in (0, 5, 9, 13, 17)) // 5,
                      sum(pts[i][1] for i in (0, 5, 9, 13, 17)) // 5)
        else:
            center = (W // 2, int(H * 0.52))
        R = int(min(W, H) * 0.30)

        self._corner_arcs(canvas, W, H, now)
        self._neural_orb(canvas, center, R, now)

        # 절제된 글로우 (스켈레톤 + 스캔라인만)
        ov = canvas.copy()
        sy = int(((now * 0.18) % 1.0) * H)
        cv2.line(ov, (0, sy), (W, sy), HUD_MAIN, 1)
        if pts:
            for a, b in HAND_CONNECTIONS:
                cv2.line(ov, pts[a], pts[b], HUD_MAIN, 3)
        canvas = cv2.addWeighted(ov, 0.13, canvas, 0.87, 0)

        if pts:
            self._hand_layer(canvas, W, H, pts, gesture, score, now)
        else:
            r = 16 + int(6 * abs(((now * 1.2) % 2.0) - 1.0))
            cv2.circle(canvas, center, r, HUD_DIM, 1, cv2.LINE_AA)
            self._t(canvas, "SCANNING FOR INPUT",
                    center[0] - 74, center[1] + R + 26, 0.4, HUD_DIM)

        self._waveform(canvas, W, H, now)
        self._strips(canvas, W, H, now, fps)
        self._left_panel(canvas, H, pts, now)
        self._right_panel(canvas, W, H, pts, gesture, score, held,
                          seq_index, now)

        # 캔버스 코너 액센트
        L = 22
        for cx, cy, dx, dy in ((3, 3, 1, 1), (W - 4, 3, -1, 1),
                               (3, H - 4, 1, -1), (W - 4, H - 4, -1, -1)):
            cv2.line(canvas, (cx, cy), (cx + dx * L, cy), HUD_MAIN, 1)
            cv2.line(canvas, (cx, cy), (cx, cy + dy * L), HUD_MAIN, 1)
        return canvas

    # ── 홀로그램 오브 (유기적 링 + 신경망) ──
    def _neural_orb(self, canvas, c, R, now):
        # 유기적으로 일렁이는 링 3겹 (회전)
        for Rk, wk, rot, col in ((1.00, 1.1, 0.15, HUD_DIM),
                                 (0.80, -1.6, -0.22, HUD_FAINT),
                                 (0.55, 2.1, 0.31, HUD_FAINT)):
            ring = []
            base = now * rot
            for d in range(0, 366, 5):
                a = math.radians(d) + base
                r = R * Rk * (1 + 0.045 * math.sin(3 * a + now * wk)
                              + 0.028 * math.sin(7 * a - now * 1.6))
                ring.append((int(c[0] + r * math.cos(a)),
                             int(c[1] + r * math.sin(a))))
            cv2.polylines(canvas, [np.array(ring, np.int32)], True, col, 1,
                          cv2.LINE_AA)

        # 방사형 틱 링
        for d in range(0, 360, 6):
            a = math.radians(d)
            ln = 9 if d % 30 == 0 else 4
            r0, r1 = R * 1.06, R * 1.06 + ln
            cv2.line(canvas,
                     (int(c[0] + r0 * math.cos(a)),
                      int(c[1] + r0 * math.sin(a))),
                     (int(c[0] + r1 * math.cos(a)),
                      int(c[1] + r1 * math.sin(a))), HUD_FAINT, 1)
        for d in (0, 90, 180, 270):
            a = math.radians(d)
            self._t(canvas, f"{d:03d}",
                    c[0] + (R * 1.06 + 16) * math.cos(a) - 10,
                    c[1] + (R * 1.06 + 16) * math.sin(a) + 4, 0.26, HUD_FAINT)

        # 신경망 노드 + 근접 연결 (막 구조)
        npts = []
        for a0, rf, sp, ph in self.nodes:
            a = a0 + now * sp
            r = R * rf * 0.92 * (1 + 0.05 * math.sin(now * 1.1 + ph))
            npts.append((int(c[0] + r * math.cos(a)),
                         int(c[1] + r * math.sin(a)), ph))
        thr = R * 0.30
        for i in range(len(npts)):
            x1, y1, _ = npts[i]
            for j in range(i + 1, len(npts)):
                x2, y2, _ = npts[j]
                if abs(x1 - x2) < thr and abs(y1 - y2) < thr \
                        and math.hypot(x1 - x2, y1 - y2) < thr:
                    cv2.line(canvas, (x1, y1), (x2, y2), HUD_FAINT, 1)
        for x, y, ph in npts:
            if (now * 2 + ph) % 3.0 < 0.18:
                cv2.circle(canvas, (x, y), 2, HUD_TXT, -1, cv2.LINE_AA)
            else:
                cv2.circle(canvas, (x, y), 1, HUD_DIM, -1)

        # 궤도 위성 마커
        for k, sp in enumerate((0.5, -0.34, 0.72)):
            a = now * sp + k * 2.1
            x = int(c[0] + R * 1.06 * math.cos(a))
            y = int(c[1] + R * 1.06 * math.sin(a))
            cv2.rectangle(canvas, (x - 2, y - 2), (x + 2, y + 2), HUD_MAIN, 1)
            self._t(canvas, f"N{k}", x + 6, y + 4, 0.26, HUD_DIM)

    # ── 코너 아크 ──
    def _corner_arcs(self, canvas, W, H, now):
        a0 = (now * 30) % 360
        for cx, cy, q in ((0, 0, 0), (W, 0, 90), (W, H, 180), (0, H, 270)):
            cv2.ellipse(canvas, (cx, cy), (86, 86), 0, q, q + 90, HUD_GRID, 1,
                        cv2.LINE_AA)
            cv2.ellipse(canvas, (cx, cy), (70, 70), 0, q + a0 % 90,
                        q + a0 % 90 + 40, HUD_FAINT, 1, cv2.LINE_AA)

    # ── 손 레이어 ──
    def _hand_layer(self, canvas, W, H, pts, gesture, score, now):
        for a, b in HAND_CONNECTIONS:
            cv2.line(canvas, pts[a], pts[b], HUD_MAIN, 1, cv2.LINE_AA)
        for i, p in enumerate(pts):
            if i in (4, 8, 12, 16, 20):
                cv2.circle(canvas, p, 5, HUD_TXT, 1, cv2.LINE_AA)
                cv2.circle(canvas, p, 2, HUD_MAIN, -1, cv2.LINE_AA)
                self._t(canvas, f"{i:02d}", p[0] + 8, p[1] - 7, 0.26, HUD_DIM)
            else:
                cv2.circle(canvas, p, 2, HUD_MAIN, -1, cv2.LINE_AA)

        palm = (sum(pts[i][0] for i in (0, 5, 9, 13, 17)) // 5,
                sum(pts[i][1] for i in (0, 5, 9, 13, 17)) // 5)
        cv2.circle(canvas, palm, 12, HUD_DIM, 1, cv2.LINE_AA)
        a0 = (now * 90) % 360
        cv2.ellipse(canvas, palm, (20, 20), 0, a0, a0 + 100, HUD_MAIN, 1,
                    cv2.LINE_AA)
        cv2.ellipse(canvas, palm, (20, 20), 0, a0 + 180, a0 + 280, HUD_MAIN,
                    1, cv2.LINE_AA)

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, y0 = max(min(xs) - 26, 0), max(min(ys) - 26, 0)
        x1, y1 = min(max(xs) + 26, W - 1), min(max(ys) + 26, H - 1)
        L = 20
        for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                               (x0, y1, 1, -1), (x1, y1, -1, -1)):
            cv2.line(canvas, (cx, cy), (cx + dx * L, cy), HUD_TXT, 1,
                     cv2.LINE_AA)
            cv2.line(canvas, (cx, cy), (cx, cy + dy * L), HUD_TXT, 1,
                     cv2.LINE_AA)
        label = (f"{gesture.upper()}  {score:.2f}"
                 if gesture != "None" else "TRACKING")
        self._t(canvas, label, x0, max(y0 - 10, 14), 0.38, HUD_MAIN)
        self._t(canvas, f"{x1 - x0}x{y1 - y0}PX", x1 - 70, y1 + 16, 0.28,
                HUD_DIM)
        self._t(canvas, f"C {(x0 + x1) // 2},{(y0 + y1) // 2}", x0, y1 + 16,
                0.28, HUD_DIM)

        # 트래킹 라인 (패널 안쪽 → 브래킷)
        my, mx = (y0 + y1) // 2, (x0 + x1) // 2
        lp, rp = self.PANEL_W + 14, W - self.PANEL_W - 14
        if x0 > lp:
            cv2.line(canvas, (lp, my), (x0, my), HUD_FAINT, 1)
            cv2.rectangle(canvas, (x0 - 2, my - 2), (x0 + 2, my + 2),
                          HUD_DIM, 1)
        if x1 < rp:
            cv2.line(canvas, (x1, my), (rp, my), HUD_FAINT, 1)
            cv2.rectangle(canvas, (x1 - 2, my - 2), (x1 + 2, my + 2),
                          HUD_DIM, 1)
        if y0 > 52:
            cv2.line(canvas, (mx, 52), (mx, y0), HUD_FAINT, 1)
        if y1 < H - 44:
            cv2.line(canvas, (mx, y1), (mx, H - 44), HUD_FAINT, 1)

    # ── 파형 (하단 중앙) ──
    def _waveform(self, canvas, W, H, now):
        bw, bh = 360, 34
        bx, by = W // 2 - bw // 2, H - 44 - bh
        cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), HUD_FAINT, 1)
        wave = []
        for i in range(0, bw, 4):
            v = (math.sin(i * 0.07 + now * 5.2) * 0.5
                 + math.sin(i * 0.023 - now * 2.1) * 0.35
                 + math.sin(i * 0.14 + now * 8.7) * 0.15)
            wave.append((bx + i, by + bh // 2 + int(v * (bh // 2 - 3))))
        cv2.polylines(canvas, [np.array(wave, np.int32)], False, HUD_DIM, 1,
                      cv2.LINE_AA)
        self._t(canvas, "SIG//", bx + 4, by + 11, 0.26, HUD_FAINT)

    # ── 상/하단 스트립 ──
    def _strips(self, canvas, W, H, now, fps):
        cv2.line(canvas, (8, 44), (W - 8, 44), HUD_GRID, 1)
        self._t(canvas, "SECURITY PROTOCOL 1", 14, 28, 0.5, HUD_TXT)
        self._t(canvas, "// BIOMETRIC AUTHENTICATION GRID", 226, 28, 0.3,
                HUD_DIM)
        cv2.line(canvas, (14, 36), (134, 36), HUD_MAIN, 1)
        if int(now * 2) % 2 == 0:
            self._t(canvas, "LOCKDOWN ACTIVE", W // 2 - 58, 28, 0.36, HUD_RED)
        self._t(canvas, f"FPS {fps:4.1f}", W - 236, 28, 0.3, HUD_DIM)
        clock = datetime.datetime.now().strftime("%H:%M:%S")
        self._t(canvas, f"T {clock}", W - 148, 28, 0.3, HUD_DIM)
        self._t(canvas, f"UPT {now:06.0f}S", W - 330, 28, 0.3, HUD_FAINT)

        # 하단 눈금자
        ry = H - 36
        cv2.line(canvas, (8, ry), (W - 8, ry), HUD_GRID, 1)
        for x in range(10, W - 8, 10):
            tick = 8 if x % 100 == 0 else (5 if x % 50 == 0 else 3)
            cv2.line(canvas, (x, ry), (x, ry + tick), HUD_FAINT, 1)
            if x % 200 == 0:
                self._t(canvas, f"{x:04d}", x - 14, ry + 20, 0.26, HUD_FAINT)
        self._t(canvas, "SP-1 CORE // GH-0209", 14, H - 12, 0.28, HUD_FAINT)
        self._t(canvas, "UNAUTHORIZED ACCESS PROHIBITED", W - 240, H - 12,
                0.28, HUD_RED)

        # 좌우 세로 눈금자 (패널 안쪽 경계)
        for x in (self.PANEL_W + 6, W - self.PANEL_W - 6):
            for y in range(52, H - 44, 8):
                ln = 6 if y % 40 == 4 else 3
                cv2.line(canvas, (x - ln // 2, y), (x + ln // 2, y),
                         HUD_FAINT, 1)

    # ── 패널 배경 ──
    @staticmethod
    def _panel_bg(canvas, x0, y0, x1, y1):
        region = canvas[y0:y1, x0:x1]
        canvas[y0:y1, x0:x1] = (region * 0.30
                                + np.array((14, 7, 3)) * 0.70).astype(np.uint8)
        cv2.rectangle(canvas, (x0, y0), (x1, y1), HUD_GRID, 1)

    # ── 좌측 패널 ──
    def _left_panel(self, canvas, H, pts, now):
        self._panel_bg(canvas, 8, 52, self.PANEL_W, H - 44)
        x0 = 18
        self._t(canvas, "SYS // DIAGNOSTICS", x0, 74, 0.34, HUD_TXT)
        cv2.line(canvas, (x0, 82), (self.PANEL_W - 10, 82), HUD_DIM, 1)

        for i in range(12):
            b = [int(127 + 120 * math.sin(now * k + i * ph))
                 for k, ph in ((1.7, 2.13), (2.3, 1.71), (1.1, 2.77),
                               (2.9, 1.13))]
            self._t(canvas, f"0x{0x4A00 + i * 16:04X}", x0, 100 + i * 15,
                    0.26, HUD_FAINT)
            self._t(canvas, " ".join(f"{v:02X}" for v in b), x0 + 62,
                    100 + i * 15, 0.26, HUD_DIM)
        cv2.line(canvas, (x0, 288), (self.PANEL_W - 10, 288), HUD_GRID, 1)

        self._t(canvas, "PROC LOAD", x0, 304, 0.3, HUD_DIM)
        for i in range(6):
            v = 0.5 + 0.45 * math.sin(now * (0.9 + i * 0.37) + i * 1.9)
            y = 314 + i * 15
            cv2.rectangle(canvas, (x0, y), (x0 + 130, y + 6), HUD_FAINT, 1)
            cv2.rectangle(canvas, (x0, y), (x0 + int(130 * v), y + 6),
                          HUD_DIM, -1)
            self._t(canvas, f"{int(v * 100):3d}%", x0 + 140, y + 7, 0.26,
                    HUD_DIM)
        cv2.line(canvas, (x0, 416), (self.PANEL_W - 10, 416), HUD_GRID, 1)

        self._t(canvas, "FINGER EXTENSION", x0, 432, 0.3, HUD_DIM)
        for i, (name, mcp, pip, tip) in enumerate(self.FINGERS):
            y = 444 + i * 15
            cv2.rectangle(canvas, (x0 + 32, y), (x0 + 152, y + 6),
                          HUD_FAINT, 1)
            if pts:
                d_tip = math.dist(pts[0], pts[tip])
                d_mcp = math.dist(pts[0], pts[mcp]) + 1e-6
                ext = max(0.0, min(1.0, (d_tip / d_mcp - 1.0) / 1.1))
                cv2.rectangle(canvas, (x0 + 32, y),
                              (x0 + 32 + int(120 * ext), y + 6), HUD_MAIN, -1)
                self._t(canvas, name, x0, y + 7, 0.26, HUD_DIM)
                self._t(canvas, f"{ext:.2f}", x0 + 160, y + 7, 0.26, HUD_DIM)
            else:
                self._t(canvas, name, x0, y + 7, 0.26, HUD_FAINT)
                self._t(canvas, "--", x0 + 160, y + 7, 0.26, HUD_FAINT)
        cv2.line(canvas, (x0, 532), (self.PANEL_W - 10, 532), HUD_GRID, 1)

        self._t(canvas, "NEURAL SYNC", x0, 548, 0.3, HUD_DIM)
        for i in range(3):
            v = 90 + 9 * math.sin(now * (1.1 + i * 0.53) + i)
            self._t(canvas, f"CH-{i}", x0, 562 + i * 14, 0.26, HUD_FAINT)
            self._t(canvas, f"{v:5.1f}%", x0 + 44, 562 + i * 14, 0.26,
                    HUD_DIM)
            cv2.line(canvas, (x0 + 104, 559 + i * 14),
                     (x0 + 104 + int(v), 559 + i * 14), HUD_FAINT, 2)
        cv2.line(canvas, (x0, 616), (self.PANEL_W - 10, 616), HUD_GRID, 1)

        for i, s in enumerate(("MEM  0x7FA2 OK", "NET  ISOLATED",
                               "I/O  LOCKED", f"UPT  T+{now:06.1f}S")):
            self._t(canvas, s, x0, 632 + i * 14, 0.28,
                    HUD_RED if "LOCKED" in s else HUD_FAINT)

    # ── 우측 패널 ──
    def _right_panel(self, canvas, W, H, pts, gesture, score, held,
                     seq_index, now):
        px0, px1 = W - self.PANEL_W, W - 8
        self._panel_bg(canvas, px0, 52, px1, H - 44)
        x0 = px0 + 10
        self._t(canvas, "TARGET // ANALYSIS", x0, 74, 0.34, HUD_TXT)
        cv2.line(canvas, (x0, 82), (px1 - 10, 82), HUD_DIM, 1)

        cls = gesture.upper() if gesture != "None" else "--------"
        self._t(canvas, cls, x0, 108, 0.5, HUD_MAIN)
        self._t(canvas, f"CONFIDENCE {score:.2f}", x0, 124, 0.28, HUD_DIM)

        # 홀드 게이지 링
        c = ((px0 + px1) // 2, 176)
        frac = max(0.0, min(1.0, held / STEP_HOLD_SEC))
        cv2.circle(canvas, c, 30, HUD_FAINT, 1, cv2.LINE_AA)
        if frac > 0:
            cv2.ellipse(canvas, c, (30, 30), -90, 0, int(360 * frac),
                        HUD_MAIN, 2, cv2.LINE_AA)
        a0 = (now * 60) % 360
        cv2.ellipse(canvas, c, (38, 38), 0, a0, a0 + 70, HUD_FAINT, 1,
                    cv2.LINE_AA)
        cv2.ellipse(canvas, c, (38, 38), 0, a0 + 180, a0 + 250, HUD_FAINT, 1,
                    cv2.LINE_AA)
        for k in range(12):
            ang = math.radians(k * 30)
            cv2.line(canvas,
                     (int(c[0] + 33 * math.cos(ang)),
                      int(c[1] + 33 * math.sin(ang))),
                     (int(c[0] + 36 * math.cos(ang)),
                      int(c[1] + 36 * math.sin(ang))), HUD_DIM, 1)
        self._t(canvas, f"{int(frac * 100):3d}%", c[0] - 14, c[1] + 4, 0.34,
                HUD_TXT)
        self._t(canvas, "HOLD", c[0] - 15, 226, 0.28, HUD_DIM)
        cv2.line(canvas, (x0, 238), (px1 - 10, 238), HUD_GRID, 1)

        self._t(canvas, "JOINT ANGLES", x0, 254, 0.3, HUD_DIM)
        for i, (name, mcp, pip, tip) in enumerate(self.FINGERS):
            y = 268 + i * 15
            if pts:
                ang = self._angle(pts[mcp], pts[pip], pts[tip])
                self._t(canvas, name, x0, y, 0.26, HUD_DIM)
                self._t(canvas, f"{ang:5.1f} DEG", x0 + 38, y, 0.26, HUD_TXT)
                cv2.line(canvas, (x0 + 130, y - 3),
                         (x0 + 130 + int(70 * ang / 180), y - 3), HUD_DIM, 2)
            else:
                self._t(canvas, f"{name}   --.- DEG", x0, y, 0.26, HUD_FAINT)
        cv2.line(canvas, (x0, 352), (px1 - 10, 352), HUD_GRID, 1)

        self._t(canvas, "CONF TRACE", x0, 368, 0.3, HUD_DIM)
        bx, by, bw, bh = x0, 376, self.PANEL_W - 38, 46
        cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), HUD_FAINT, 1)
        cv2.line(canvas, (bx, by + bh // 2), (bx + bw, by + bh // 2),
                 HUD_GRID, 1)
        if len(self.conf_hist) >= 2:
            n = len(self.conf_hist)
            line = [(bx + int(i * (bw - 4) / (n - 1)) + 2,
                     by + bh - 3 - int(v * (bh - 6)))
                    for i, v in enumerate(self.conf_hist)]
            cv2.polylines(canvas, [np.array(line, np.int32)], False,
                          HUD_MAIN, 1, cv2.LINE_AA)
        cv2.line(canvas, (x0, 440), (px1 - 10, 440), HUD_GRID, 1)

        n = len(UNLOCK_SEQUENCE)
        self._t(canvas, f"AUTH SEQUENCE {seq_index}/{n}", x0, 456, 0.32,
                HUD_MAIN)
        for i in range(n):
            x = x0 + i * 62
            if i < seq_index:
                cv2.rectangle(canvas, (x, 466), (x + 54, 476), HUD_MAIN, -1)
            else:
                cv2.rectangle(canvas, (x, 466), (x + 54, 476), HUD_DIM, 1)
        cv2.line(canvas, (x0, 490), (px1 - 10, 490), HUD_GRID, 1)

        for i, s in enumerate(("ID    GH-0209", "MODE  GESTURE-AUTH",
                               "LINK  CAM-01 SECURE", "GRID  STABLE")):
            self._t(canvas, s, x0, 506 + i * 14, 0.28, HUD_FAINT)
        icol = HUD_RED if self.intrusions else HUD_FAINT
        self._t(canvas, f"INTRUSION ATTEMPTS  {self.intrusions:02d}", x0, 562,
                0.28, icol)

        # 남은 해제 시도 횟수 (소진 시 macOS 비밀번호 요구)
        left = max(0, MAX_UNLOCK_ATTEMPTS - self.fail_count)
        fcol = HUD_RED if self.fail_count else HUD_DIM
        self._t(canvas, f"ATTEMPTS LEFT  {left}/{MAX_UNLOCK_ATTEMPTS}", x0, 578,
                0.28, fcol)
        for i in range(MAX_UNLOCK_ATTEMPTS):
            x = x0 + 118 + i * 12
            if i < left:
                cv2.rectangle(canvas, (x, 571), (x + 8, 578), HUD_MAIN, -1)
            else:
                cv2.rectangle(canvas, (x, 571), (x + 8, 578), HUD_RED, 1)


# ──────────────────────────── 제스처 안정화 ────────────────────────────
class GestureStabilizer:
    """프레임 단위로 인식이 깜빡여도(잠깐 None) 제스처 유지 시간을 안정적으로 잰다.

    - 같은 제스처가 이어지면 유지 시간 누적
    - None이 gap_grace 이내로 잠깐 끼어드는 것은 무시
    - 다른 제스처가 나오거나 gap_grace 넘게 끊기면 리셋
    """

    def __init__(self, gap_grace=GESTURE_GAP_GRACE_SEC):
        self.gap_grace = gap_grace
        self.candidate = "None"
        self.since = 0.0
        self.seen = 0.0

    def update(self, gesture, now):
        if gesture == "None":
            if self.candidate != "None" and now - self.seen > self.gap_grace:
                self._reset(now)
        elif gesture == self.candidate:
            self.seen = now
        else:
            self.candidate = gesture
            self.since = now
            self.seen = now
        held = now - self.since if self.candidate != "None" else 0.0
        return self.candidate, held

    def consume(self, now):
        """현재 제스처를 소비(트리거/단계 성공 등) — 재판정 방지."""
        self._reset(now)

    def _reset(self, now):
        self.candidate = "None"
        self.since = now
        self.seen = now


class AudioClapDetector:
    """마이크로 박수의 음향 임펄스를 감지한다 — 더블 클랩 v4의 주 신호.

    비전(손 추적)은 박수 접촉 순간 모션블러·겹침으로 오히려 끊기지만,
    소리는 정확히 그 순간 난다. 박수의 음향 특징으로 구분한다:

    - 짧다: 온셋 후 ~110ms 안에 RMS가 온셋 블록의 35% 아래로 감쇠
      (말소리·음악·환풍기 같은 지속음 차단)
    - 광대역: 인접 샘플 차분 에너지 비율이 높음 (저역 진동·웅웅거림 차단)
    - 크다: 적응형 소음 바닥의 12배 이상 + 절대 최소 피크 0.10
      (키보드 타이핑·미세음 차단 — v5에서 상향)
    - 고립돼 있다 (v5): 두 온셋 앞 0.6초와 뒤 0.35초에 다른 온셋이 없어야
      한다. 타이핑은 임펄스가 연달아 오므로 여기서 걸러진다.

    두 온셋이 0.12~1.0초 간격이면 더블 클랩 후보 → 고립 확인 후 확정.
    최종 발동은 GestureWatcher가 "최근에 손이 보였고 실제로 모였는가"
    (비전 접촉 게이트)와 융합해 결정한다 — TV·문 소리·키보드는 손이
    모이지 않으므로 무시된다.
    """

    SR = 16000
    BLOCK = 256                  # 16 ms
    PEAK_OVER_FLOOR = 12.0       # 온셋 피크 >= 소음 바닥 RMS x 배율 (config clap_peak_over_floor)
    ABS_MIN_PEAK = 0.10          # 절대 최소 피크 (config clap_min_peak)
    ISOLATION_SEC = 0.5          # 둘째 박수 뒤 이 시간 동안 추가 온셋 없어야 확정
    PRE_QUIET_SEC = 0.6          # 첫째 박수 앞 이 시간 동안 다른 온셋 없어야 함
    HF_RATIO_MIN = 0.20          # 광대역성: 인접 차분 에너지 / 전체 에너지
    DECAY_BLOCKS = 9             # 온셋 후 관찰 블록 수 (~144ms)
    DECAY_RATIO = 0.35           # 꼬리 RMS < 코어 RMS x 비율이어야 임펄스
    REFRACTORY_SEC = 0.07        # 온셋 간 최소 간격 (반향/바운스 무시)
    GAP_MIN = 0.12               # 두 박수 최소 간격 (강한 반향 오인 방지)
    GAP_MAX = 1.0                # 두 박수 최대 간격

    def __init__(self, autostart=True):
        self.available = False
        self._stream = None
        self._lock = threading.Lock()
        self._floor = 3e-3
        self._pending = None         # (시각, 온셋 블록 RMS) — 감쇠 확인 대기
        self._tail = []
        self._last_onset = -10.0
        self._onsets = collections.deque(maxlen=8)
        self._double_at = None
        self._cand = None            # (첫째, 둘째) 더블 클랩 후보 — 고립 확인 대기
        self.ABS_MIN_PEAK = float(CONFIG.get("clap_min_peak", self.ABS_MIN_PEAK))
        self.PEAK_OVER_FLOOR = float(CONFIG.get("clap_peak_over_floor", self.PEAK_OVER_FLOOR))
        self.ISOLATION_SEC = float(CONFIG.get("clap_isolation_sec", self.ISOLATION_SEC))
        if autostart:
            self.start()

    def start(self):
        try:
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=self.SR, blocksize=self.BLOCK, channels=1,
                dtype="float32", callback=self._on_block)
            self._stream.start()
            self.available = True
            log("클랩 오디오 감지 시작 (마이크 융합 모드)")
        except Exception as e:
            log(f"[!] 클랩 오디오 감지 사용 불가 → 비전 단독 폴백: {e}")

    def stop(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self.available = False

    def _on_block(self, indata, frames, t_info, status):
        try:
            x = indata[:, 0]
            peak = float(np.max(np.abs(x)))
            rms = float(np.sqrt(np.mean(x * x))) + 1e-9
            diff = np.diff(x)
            hf = float(np.sum(diff * diff) / (np.sum(x * x) + 1e-12))
            with self._lock:
                self._process(time.monotonic(), peak, rms, hf)
        except Exception:
            pass

    def _process(self, now, peak, rms, hf):
        """블록 하나 처리. 잠금 상태에서 호출된다 (테스트에서 직접 호출 가능)."""
        if self._pending is not None:
            self._tail.append(rms)
            if len(self._tail) >= self.DECAY_BLOCKS:
                t0, rms0 = self._pending
                # 어택이 여러 블록에 퍼질 수 있으니 온셋+2블록 중 최대 RMS를
                # 임펄스 '코어'로 삼고, ~80ms 이후 꼬리가 그 아래로 감쇠해야
                # 박수로 인정한다 (지속음은 꼬리가 코어와 비슷하게 유지됨)
                core = max([rms0] + self._tail[:2])
                late = self._tail[4:]
                tail_rms = sum(late) / max(1, len(late))
                self._pending = None
                self._tail = []
                if tail_rms < core * self.DECAY_RATIO:
                    self._register_onset(t0)
            return
        if (peak > self.ABS_MIN_PEAK
                and peak > self._floor * self.PEAK_OVER_FLOOR
                and hf > self.HF_RATIO_MIN
                and now - self._last_onset > self.REFRACTORY_SEC):
            self._pending = (now, rms)
            self._tail = []
        else:
            # 조용한 블록만 바닥에 반영 — 박수가 스스로 바닥을 끌어올리지 않게
            self._floor = max(1e-4, self._floor * 0.98 + rms * 0.02)

    def _register_onset(self, t):
        self._last_onset = t
        if self._cand is not None:
            # 후보 확정 전에 세 번째 임펄스 → 타이핑/연타 → 후보 취소
            self._cand = None
        prevs = [p for p in self._onsets if 0 < t - p <= self.GAP_MAX + self.PRE_QUIET_SEC]
        if prevs:
            t1 = prevs[-1]
            gap = t - t1
            if self.GAP_MIN <= gap <= self.GAP_MAX:
                # 첫째 박수 직전(PRE_QUIET_SEC)에 다른 온셋이 있었으면 연타로 거부
                if any(0 < t1 - p < self.PRE_QUIET_SEC for p in prevs[:-1]):
                    self._onsets.append(t)
                    return
                self._cand = (t1, t)
        self._onsets.append(t)

    def poll_double(self):
        """더블 클랩이 확정됐으면 (첫째, 둘째) 박수 시각을 반환하고 소비한다.
        후보는 둘째 박수 뒤 ISOLATION_SEC 동안 추가 온셋이 없을 때만 확정."""
        with self._lock:
            if self._cand is None:
                return None
            t1, t2 = self._cand
            # 판정 대기 중인 온셋(_pending)이 있으면 그 결과를 보고 결정
            if self._pending is not None or time.monotonic() - t2 < self.ISOLATION_SEC:
                return None
            self._cand = None
            self._onsets.clear()
            return (t1, t2)

    def onset_near(self, t, tol):
        """시각 t 근처(tol초)에 오디오 온셋이 있었는가 — 비전 확인용."""
        with self._lock:
            if self._last_onset > 0 and abs(self._last_onset - t) <= tol:
                return True
            return any(abs(o - t) <= tol for o in self._onsets)


class ClapDetector:
    """양손 랜드마크로 '박수 두 번'(더블 클랩) 트리거를 감지한다.

    핵심: 진짜 박수는 손이 너무 빨라 접촉 순간 모션블러 + 양손 겹침으로
    **손 추적 자체가 끊긴다**. 그래서 접촉을 두 경로로 잡는다:

    1. 직접 접촉 — 추적이 살아있고 손바닥 간격이 임계 이하 (살살 친 박수)
    2. **추론 접촉** — 두 손이 빠르게 접근하다가(근접 + 접근 속도) 추적이
       갑자기 끊기면 그 순간을 박수로 추론 (세게 친 진짜 박수)

    오작동 방지:
    - 접근 속도 게이트: 천천히 손을 모으는 동작은 두 경로 모두 무시
    - 추론 접촉은 끊기기 직전 간격이 충분히 가까웠을 때만
    - 재무장 히스테리시스: 박수 1회 후 손이 벌어져야(0.8) 다음 카운트
    - 직접 접촉이 길어지면(깍지/잡기) 해당 카운트 취소
    - 두 박수 간격 0.08~1.2초 창, 발동 후엔 기존 락다운 쿨다운
    """

    CONTACT_R = 0.65        # 직접 접촉: 손바닥 간격 < 손크기 × 0.65
    NEAR_R = 1.5            # 추론 접촉: 끊기기 직전 간격이 이 이하였어야 함
    REARM_R = 0.8           # 다음 박수로 재무장되는 분리 간격
    MIN_APPROACH = 2.2      # 최소 접근 속도 (손크기/초)
    MAX_CONTACT_SEC = 0.5   # 직접 접촉이 이보다 길면 잡기 → 카운트 취소
    GAP_MIN = 0.08          # 두 박수 최소 간격 (바운스 무시)
    GAP_MAX = 1.2           # 두 박수 최대 간격
    APART_R = 1.3           # v5: 두 손 간격이 이 이상이면 '떨어짐'(타이핑 자세) 프레임
    TYPING_FRAC = 0.85      # 창 안 프레임의 이 비율 이상이 '떨어짐'이면 타이핑 자세로 판정

    def __init__(self):
        self.hist = collections.deque(maxlen=240)   # (시각, 자세) — none/one/apart/close
        self.armed = True
        self.had2 = False
        self.last_mid = None
        self.last_dist = None
        self.approach = 0.0
        self.prev_dist = None
        self.prev_t = None
        self.last_clap_at = -10.0
        self.hit_at = -10.0
        self.hit_direct = False
        self.claps = 0

    @staticmethod
    def _palm(hand):
        xs = ys = 0.0
        for i in (0, 5, 9, 13, 17):
            xs += hand[i].x
            ys += hand[i].y
        return xs / 5.0, ys / 5.0

    @staticmethod
    def _size(hand):
        return math.hypot(hand[0].x - hand[9].x,
                          hand[0].y - hand[9].y) or 1e-3

    def _hit(self, now, direct):
        """접촉 1회 등록. 더블 클랩 완성 시 True."""
        self.hit_at = now
        self.hit_direct = direct
        self.armed = False
        gap = now - self.last_clap_at
        self.last_clap_at = now
        if self.claps == 1 and self.GAP_MIN <= gap <= self.GAP_MAX:
            self.claps = 0
            return True
        self.claps = 1
        return False

    def typing_posture(self, t_start, t_end):
        """[t_start, t_end] 동안 카메라가 본 자세가 '두 손이 멀리 떨어진 채'였는가.
        오디오 더블 클랩의 비전 게이트: 진짜 박수는 손이 겹치거나(close)
        블러로 추적이 끊겨(one/none) 이 조건을 만족하지 못하고, 키보드 위의
        두 손은 내내 떨어져 있어 만족한다. 증거 프레임이 적으면 거부하지 않는다."""
        frames = [st for t, st in self.hist if t_start <= t <= t_end]
        if len(frames) < 4:
            return False
        apart = sum(1 for st in frames if st == "apart")
        return apart / len(frames) >= self.TYPING_FRAC

    def update(self, hands, now):
        """매 프레임 호출. 더블 클랩이 완성된 순간에만 True."""
        fired = False

        if hands is None or len(hands) < 2:
            self.hist.append((now, "one" if hands and len(hands) == 1 else "none"))
            # 빠르게 접근하던 두 손이 갑자기 사라짐 = 겹침/블러 → 박수로 추론.
            # 단, 한 손이 남아 있으면 그 손이 직전 두 손의 중간점 근처(겹침
            # 블롭)여야 한다 — 한 손이 그냥 프레임을 벗어난 경우(타이핑,
            # 물건 집기)를 박수로 오인하지 않기 위한 v4 게이트.
            merged = True
            if hands and len(hands) == 1 and self.last_mid is not None:
                px, py = self._palm(hands[0])
                ref1 = self._size(hands[0])
                merged = math.hypot(px - self.last_mid[0],
                                    py - self.last_mid[1]) / ref1 < 1.6
            if (merged and self.had2 and self.armed
                    and self.last_dist is not None
                    and self.last_dist < self.NEAR_R
                    and self.approach > self.MIN_APPROACH):
                fired = self._hit(now, direct=False)
            self.had2 = False
            self.prev_dist = None   # 접근 속도는 동결(리셋 안 함) — 재등장 대비
            if self.claps and now - self.last_clap_at > self.GAP_MAX:
                self.claps = 0
            return fired

        (ax, ay) = self._palm(hands[0])
        (bx, by) = self._palm(hands[1])
        ref = (self._size(hands[0]) + self._size(hands[1])) / 2.0
        dist = math.hypot(ax - bx, ay - by) / ref

        if self.prev_dist is not None and self.prev_t is not None:
            dt = max(1e-3, now - self.prev_t)
            inst = (self.prev_dist - dist) / dt   # +면 접근 중
            # 빠른 어택 / 짧은 감쇠: 박수의 2~3프레임짜리 접근에도 반응
            self.approach = max(inst, self.approach * 0.6)
        self.prev_dist = dist
        self.prev_t = now
        self.last_dist = dist
        self.last_mid = ((ax + bx) / 2.0, (ay + by) / 2.0)
        self.had2 = True
        self.hist.append((now, "apart" if dist >= self.APART_R else "close"))

        if self.armed:
            if dist < self.CONTACT_R and self.approach > self.MIN_APPROACH:
                fired = self._hit(now, direct=True)
        else:
            if dist > self.REARM_R:
                self.armed = True
            elif (self.hit_direct and self.claps
                    and dist < self.CONTACT_R
                    and now - self.hit_at > self.MAX_CONTACT_SEC):
                self.claps = 0   # 박수인 줄 알았는데 잡기 → 취소

        if self.claps == 1 and now - self.last_clap_at > self.GAP_MAX:
            self.claps = 0
        return fired

# ──────────────────────────── 제스처 감시 스레드 ────────────────────────────
class GestureWatcher(threading.Thread):
    """카메라 프레임을 읽어 제스처를 분류하고 상태머신을 돌린다."""

    def __init__(self, app, test_mode=False):
        super().__init__(daemon=True)
        self.app = app
        self.test_mode = test_mode
        self.stop_flag = threading.Event()
        self.hud = HUDRenderer()
        self.last_capture_at = -1e9
        self.intrusion_count = 0
        self.fail_count = 0          # 연속 해제 실패 횟수 (MAX_UNLOCK_ATTEMPTS에서 락아웃)
        self.latest_frame = None     # 원격 스냅샷 명령용 최신 프레임

    def run(self):
        try:
            self._loop()
        except Exception:
            log("[!] 카메라 스레드 오류:\n" + traceback.format_exc())
            AppHelper.callAfter(self.app.cameraFailed)

    def _loop(self):
        options = vision.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,   # 더블 클랩 트리거는 양손 랜드마크가 필요
            min_hand_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        recognizer = vision.GestureRecognizer.create_from_options(options)

        cap = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if not cap.isOpened():
            raise RuntimeError("카메라를 열 수 없습니다. 카메라 권한을 확인하세요.")
        log("카메라 감시 시작")

        stab = GestureStabilizer()
        clap = ClapDetector()
        audio = None
        if TRIGGER_GESTURE == "Double_Clap" and CONFIG.get("clap_audio", True):
            audio = AudioClapDetector()
        hands_seen_at = -1e9      # 오디오 발동 게이트: 최근에 손이 보였는가
        HANDS_RECENT_SEC = 3.0
        prev_locked = False
        prev_stage = "shade"
        seq_index = 0             # 해제 시퀀스 진행 위치
        seq_started_at = 0.0
        cooldown_until = time.monotonic() + STARTUP_GRACE_SEC
        screen_locked_cache = False
        screen_checked_at = 0.0
        last_logged = "None"
        frame_i = 0
        fps = 0.0
        t0 = time.monotonic()
        last_frame_at = t0

        while not self.stop_flag.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            self.latest_frame = frame    # 원격 스냅샷 명령용
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts_ms = int((time.monotonic() - t0) * 1000)
            result = recognizer.recognize_for_video(image, ts_ms)

            gesture = "None"
            score = 0.0
            if result.gestures:
                # 손이 두 개면 신뢰도가 더 높은 손의 제스처를 쓴다
                top = max((g[0] for g in result.gestures if g),
                          key=lambda c: c.score, default=None)
                if top is not None and top.score >= MIN_CONFIDENCE:
                    gesture = top.category_name
                    score = top.score

            now = time.monotonic()
            dt = now - last_frame_at
            last_frame_at = now
            if dt > 0:
                fps = fps * 0.9 + (1.0 / dt) * 0.1
            locked = self.app.locked

            if result.hand_landmarks:
                hands_seen_at = now
            # 매 프레임 소비 — 쿨다운/락 중에 쌓인 이벤트가 뒤늦게 터지지 않게
            audio_double = audio.poll_double() if (audio and audio.available) else None

            # macOS 잠금화면 상태 (0.5초 스로틀) — 탭 콜백과 공유
            if now - screen_checked_at > 0.5:
                screen_checked_at = now
                was = screen_locked_cache
                screen_locked_cache = session_screen_locked()
                self.app.macos_screen_locked = screen_locked_cache
                if was and not screen_locked_cache:
                    cooldown_until = now + COOLDOWN_SEC
                    # 맥 비밀번호로 본인 확인됨 → 실패 카운트 리셋
                    if self.fail_count:
                        log(f"macOS 비밀번호 확인됨 → 해제 실패 카운트 리셋 "
                            f"({self.fail_count} → 0)")
                        self.fail_count = 0
                        self.hud.fail_count = 0
                    log("macOS 잠금 해제 감지"
                        + (" → 락다운 유지" if locked else " → 쿨다운 시작"))

            # 락 상태가 바뀌면 상태머신 리셋 (+ 해제 직후 쿨다운)
            if locked != prev_locked:
                prev_locked = locked
                prev_stage = "shade"
                stab.consume(now)
                seq_index = 0
                if not locked:
                    cooldown_until = now + COOLDOWN_SEC
                    self.fail_count = 0
                    self.hud.fail_count = 0

            candidate, held = stab.update(gesture, now)

            if self.test_mode:
                before = clap.claps
                if clap.update(result.hand_landmarks, now):
                    hint = ("+오디오 확인" if audio and audio.available
                            and audio.onset_near(now, 0.6) else "무음 — 실전에선 무시")
                    print(f"    👏👏 비전 더블 클랩 ({hint})", flush=True)
                elif clap.claps != before:
                    print(f"    👏 클랩 {clap.claps}/2", flush=True)
                if audio_double is not None:
                    a1, a2 = audio_double
                    seen = now - hands_seen_at < HANDS_RECENT_SEC
                    typing = clap.typing_posture(a1 - 0.4, a2 + 0.25)
                    if not seen:
                        verdict = "(최근 손 미검출 → 무시)"
                    elif CLAP_REQUIRE_VISION and typing:
                        verdict = "(박수 구간 내내 두 손 떨어짐 → 무시: 키보드/타건음)"
                    else:
                        verdict = "+ 손 확인 → 발동 조건 충족!"
                    print(f"    🔊 오디오 더블 클랩 {verdict}", flush=True)
                if gesture != last_logged:
                    print(f"    인식: {GESTURE_EMOJI.get(gesture, '')} {gesture}"
                          f"  (안정화: {candidate} {held:.1f}s)", flush=True)
                    last_logged = gesture
                continue

            # 진단용: 안정화된 제스처 변화를 로그에 남김
            if candidate != last_logged:
                if candidate != "None":
                    log(f"제스처 인식: {GESTURE_EMOJI.get(candidate, '')} {candidate}")
                last_logged = candidate

            if locked:
                if screen_locked_cache:
                    continue   # 맥 잠금화면 중에는 동결 (락다운은 유지됨)

                # 침입 블랙박스: 탭 콜백이 감지한 입력 시도 → 스냅샷
                reason = self.app.intrusion_pending
                if reason:
                    self.app.intrusion_pending = None
                    self._capture_intruder(frame, reason, now)

                # 셰이드 단계(UNLOCK 버튼 대기)에서는 HUD/시퀀스 비활성
                stage = self.app.stage
                if stage != prev_stage:
                    prev_stage = stage
                    stab.consume(now)
                    seq_index = 0
                if stage != "auth":
                    continue

                # 오버레이에 HUD 프레임 전송 (3프레임마다, 미러링)
                frame_i += 1
                if frame_i % 3 == 0:
                    size = getattr(self.app, "hud_size", None) or (1440, 900)
                    hud = self.hud.render(cv2.flip(frame, 1), result,
                                          candidate, score, held,
                                          seq_index, now, fps, size)
                    ok2, jpg = cv2.imencode(
                        ".jpg", hud, [cv2.IMWRITE_JPEG_QUALITY, 82])
                    if ok2:
                        AppHelper.callAfter(
                            self.app.updatePreview_, jpg.tobytes())

                # 해제 시퀀스 감시
                if seq_index > 0 and now - seq_started_at > SEQUENCE_TIMEOUT_SEC:
                    seq_index = 0
                    log("해제 시퀀스 시간 초과 → 처음부터")
                expected = UNLOCK_SEQUENCE[seq_index]
                if candidate == expected and held >= STEP_HOLD_SEC:
                    if seq_index == 0:
                        seq_started_at = now
                    seq_index += 1
                    stab.consume(now)   # 이 단계 소비
                    if seq_index >= len(UNLOCK_SEQUENCE):
                        seq_index = 0
                        AppHelper.callAfter(self.app.unlock)
                    else:
                        play(SOUND_STEP)
                        log(f"해제 단계 {seq_index}/{len(UNLOCK_SEQUENCE)} 성공")
                elif (candidate not in ("None", expected)
                        and held >= STEP_HOLD_SEC):
                    # 잘못된 해제 제스처 → 실패 카운트 + 침입 기록
                    stab.consume(now)
                    seq_index = 0        # 시퀀스는 처음부터 다시
                    self.fail_count += 1
                    self.hud.fail_count = self.fail_count
                    log(f"[FAIL] 해제 실패 {self.fail_count}/{MAX_UNLOCK_ATTEMPTS}")
                    self._capture_intruder(frame, "WRONG_GESTURE", now)
                    if self.fail_count >= MAX_UNLOCK_ATTEMPTS:
                        log(f"[LOCKOUT] 해제 {MAX_UNLOCK_ATTEMPTS}회 실패 "
                            "→ macOS 잠금화면으로 전환 (락다운 유지)")
                        play(SOUND_ERROR)
                        notify(f"SP-1 LOCKOUT // 해제 {MAX_UNLOCK_ATTEMPTS}회 실패 "
                               "→ macOS 비밀번호 요구")
                        AppHelper.callAfter(self.app.lockoutToLoginWindow)
            else:
                if screen_locked_cache:
                    continue
                if now < cooldown_until:
                    continue
                if TRIGGER_GESTURE == "Double_Clap":
                    before = clap.claps
                    vis_fired = clap.update(result.hand_landmarks, now)
                    fired = None   # 발동 경로 설명
                    if audio is not None and audio.available:
                        # 주 경로: 오디오 더블 클랩 + 최근 손 확인
                        if audio_double is not None:
                            a1, a2 = audio_double
                            if now - hands_seen_at >= HANDS_RECENT_SEC:
                                log("오디오 더블 클랩 무시 (최근 손 미검출 — 외부 소음)")
                            elif (CLAP_REQUIRE_VISION
                                    and clap.typing_posture(a1 - 0.4, a2 + 0.25)):
                                log("오디오 더블 클랩 무시 (박수 구간 내내 두 손이 "
                                    "떨어져 있음 — 키보드/타건음 추정)")
                            else:
                                fired = "오디오+손 확인"
                        # 보조 경로: 비전 더블 클랩은 소리가 함께 났을 때만
                        if fired is None and vis_fired:
                            if audio.onset_near(now, 0.6):
                                fired = "비전+오디오 확인"
                            else:
                                log("비전 더블 클랩 무시 (무음 — 오작동 방지)")
                    elif vis_fired:
                        fired = "비전 단독(오디오 폴백)"
                    if fired:
                        log(f"더블 클랩 감지({fired}) → 락다운 요청")
                        AppHelper.callAfter(self.app.lockdown)
                    elif clap.claps > before:
                        log("클랩 1/2 감지")
                elif candidate == TRIGGER_GESTURE and held >= TRIGGER_HOLD_SEC:
                    stab.consume(now)
                    log("트리거 제스처 감지 → 락다운 요청")
                    AppHelper.callAfter(self.app.lockdown)

        cap.release()
        if audio is not None:
            audio.stop()

    def _capture_intruder(self, frame, reason, now):
        """침입 시도 순간의 카메라 스냅샷을 저장하고 폰으로 전송."""
        if now - self.last_capture_at < INTRUSION_COOLDOWN_SEC:
            return
        self.last_capture_at = now
        self.intrusion_count += 1
        self.hud.intrusions = self.intrusion_count
        try:
            os.makedirs(INTRUDER_DIR, exist_ok=True)
            ts = datetime.datetime.now()
            path = os.path.join(
                INTRUDER_DIR, f"{ts:%Y%m%d_%H%M%S}_{reason.lower()}.jpg")
            img = cv2.flip(frame, 1)
            h, w = img.shape[:2]
            cv2.rectangle(img, (0, h - 26), (w, h), (0, 0, 0), -1)
            cv2.putText(img,
                        f"SP-1 INTRUSION // {reason} // {ts:%Y-%m-%d %H:%M:%S}",
                        (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (60, 60, 255), 1, cv2.LINE_AA)
            cv2.imwrite(path, img)
            log(f"[ALERT] 침입 시도 기록: {reason} → {os.path.basename(path)}")
            notify(f"SP-1 INTRUSION ATTEMPT // {reason}", photo_path=path)
        except Exception:
            log("[!] 침입 기록 실패:\n" + traceback.format_exc())


# ──────────────────────────── 원격 명령 수신 (폰 → ntfy → 맥) ────────────────────────────
class RemoteListener(threading.Thread):
    """ntfy 명령 topic을 구독해 폰에서 보낸 명령을 실행한다.

    메시지 형식:  "<token> <command>"   (token은 config의 remote.token)
    명령: lock / unlock / snap / status

    보안: 명령 topic은 알림 topic과 반드시 다른 비밀 이름이어야 하고,
    token이 틀리면 무시 + 경고 알림을 보낸다 (topic 유출 감지).
    """

    COMMANDS = ("lock", "unlock", "snap", "status")

    def __init__(self, app, watcher):
        super().__init__(daemon=True)
        self.app = app
        self.watcher = watcher
        self.stop_flag = threading.Event()
        self.topic = REMOTE.get("ntfy_command_topic", "")
        self.token = str(REMOTE.get("token", ""))
        self.started_at = time.time()

    def run(self):
        url = f"https://ntfy.sh/{self.topic}/json"
        log(f"원격 명령 수신 시작 (topic: {self.topic[:6]}…)")
        backoff = 2
        while not self.stop_flag.is_set():
            try:
                proc = subprocess.Popen(
                    ["curl", "-sN", "--no-buffer", "--max-time", "3600", url],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                backoff = 2
                for raw in proc.stdout:
                    if self.stop_flag.is_set():
                        break
                    self._handle_line(raw)
                proc.terminate()
            except Exception:
                log("[!] 원격 수신 오류:\n" + traceback.format_exc())
            if not self.stop_flag.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_line(self, raw):
        try:
            msg = json.loads(raw.decode("utf-8", "replace").strip() or "{}")
        except ValueError:
            return
        if msg.get("event") != "message":
            return                                  # open/keepalive 무시
        if msg.get("time", 0) < self.started_at - 5:
            return                                  # 캐시된 과거 메시지 무시
        text = (msg.get("message") or "").strip()
        parts = text.split()
        if not parts:
            return
        # 토큰 검증
        if self.token:
            if len(parts) < 2 or not hmac.compare_digest(parts[0], self.token):
                log("[REMOTE] 토큰 불일치 명령 무시 — 명령 topic이 유출됐을 수 있음")
                notify("SP-1 REMOTE // 잘못된 토큰의 명령 수신 "
                       "(명령 topic 유출 의심 — topic/토큰 교체 권장)")
                return
            cmd = parts[1].lower()
        else:
            cmd = parts[0].lower()
        if cmd not in self.COMMANDS:
            return
        log(f"[REMOTE] 명령 수신: {cmd}")
        self._dispatch(cmd)

    def _dispatch(self, cmd):
        if cmd == "lock":
            AppHelper.callAfter(self.app.remoteLock)
        elif cmd == "unlock":
            AppHelper.callAfter(self.app.remoteUnlock)
        elif cmd == "snap":
            self._snap()
        elif cmd == "status":
            state = "LOCKED" if self.app.locked else "ARMED"
            if self.app.locked:
                state += f" ({self.app.stage})"
            notify(f"SP-1 STATUS // {state} // "
                   f"실패 {self.watcher.fail_count}/{MAX_UNLOCK_ATTEMPTS} // "
                   f"침입 {self.watcher.intrusion_count}건")

    def _snap(self):
        frame = self.watcher.latest_frame
        if frame is None:
            notify("SP-1 SNAP // 카메라 프레임 없음")
            return
        try:
            os.makedirs(INTRUDER_DIR, exist_ok=True)
            ts = datetime.datetime.now()
            path = os.path.join(INTRUDER_DIR, f"{ts:%Y%m%d_%H%M%S}_snap.jpg")
            img = cv2.flip(frame, 1)
            h, w = img.shape[:2]
            cv2.rectangle(img, (0, h - 26), (w, h), (0, 0, 0), -1)
            cv2.putText(img, f"SP-1 REMOTE SNAP // {ts:%Y-%m-%d %H:%M:%S}",
                        (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (255, 220, 60), 1, cv2.LINE_AA)
            cv2.imwrite(path, img)
            log(f"[REMOTE] 원격 스냅샷 → {os.path.basename(path)}")
            notify(f"SP-1 REMOTE SNAP // {ts:%H:%M:%S}", photo_path=path)
        except Exception:
            log("[!] 원격 스냅샷 실패:\n" + traceback.format_exc())


# ──────────────────────────── 메인 앱 (오버레이 + 입력 차단) ────────────────────────────
class SecurityApp(AppKit.NSObject):

    def init(self):
        self = objc.super(SecurityApp, self).init()
        if self is None:
            return None
        self.locked = False
        self.stage = "shade"            # "shade"(UNLOCK 버튼 대기) | "auth"(HUD 인증)
        self.macos_screen_locked = False
        self.intrusion_pending = None   # 탭이 감지한 차단 입력 → 카메라 스레드가 스냅샷
        self.windows = []
        self.status_labels = []
        self.preview_views = []
        self.button_views = []
        self.button_rect = None         # UNLOCK 버튼 (CG 전역 좌표, x0,y0,x1,y1)
        self.tap = None
        self.tap_source = None
        self.overlay_miss = 0
        self.hud_size = None
        return self

    # ── 이벤트 탭: 락다운 중에만 생성, 해제 시 완전 파괴 ──
    def _createTap(self):
        """락다운 순간에만 호출. 성공 시 True."""
        if self.tap is not None:
            return True

        def callback(proxy, type_, event, refcon):
            try:
                if type_ in (Quartz.kCGEventTapDisabledByTimeout,
                             Quartz.kCGEventTapDisabledByUserInput):
                    # 락다운 유지 중일 때만 되살린다 (우리가 껐을 때는 부활 금지)
                    if self.locked and self.tap is not None:
                        Quartz.CGEventTapEnable(self.tap, True)
                    return None
                # 맥 잠금화면 중에는 전부 통과 (비밀번호 입력 방해 금지,
                # 락다운 오버레이/탭은 세션 복귀 후 그대로 유지).
                # 클릭/키다운은 스테일 플래그 방지를 위해 실시간 재확인.
                if self.macos_screen_locked:
                    if type_ in (Quartz.kCGEventLeftMouseDown,
                                 Quartz.kCGEventRightMouseDown,
                                 Quartz.kCGEventKeyDown):
                        if session_screen_locked():
                            return event
                        self.macos_screen_locked = False  # 복귀 확정 → 차단 재개
                    else:
                        return event
                # 커서 이동은 허용 (UNLOCK 버튼을 조준할 수 있도록)
                if type_ == Quartz.kCGEventMouseMoved:
                    return event
                # 셰이드 단계: UNLOCK 버튼 영역 안의 클릭만 인식 (이벤트는 삼킴)
                if (type_ == Quartz.kCGEventLeftMouseDown
                        and self.stage == "shade" and self.button_rect):
                    loc = Quartz.CGEventGetLocation(event)
                    x0, y0, x1, y1 = self.button_rect
                    if x0 <= loc.x <= x1 and y0 <= loc.y <= y1:
                        AppHelper.callAfter(self.enterAuthStage)
                        return None
                if type_ == Quartz.kCGEventKeyDown:
                    keycode = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode)
                    flags = Quartz.CGEventGetFlags(event)
                    if (keycode == EMERGENCY_KEYCODE
                            and (flags & EMERGENCY_FLAGS) == EMERGENCY_FLAGS):
                        AppHelper.callAfter(self.emergencyEscape)
                        return None
                # 그 외 차단되는 실제 입력(키/클릭)은 침입 시도로 플래그
                if type_ in (Quartz.kCGEventKeyDown,
                             Quartz.kCGEventLeftMouseDown,
                             Quartz.kCGEventRightMouseDown):
                    if self.intrusion_pending is None:
                        self.intrusion_pending = (
                            "KEYBOARD" if type_ == Quartz.kCGEventKeyDown
                            else "MOUSE")
            except Exception:
                pass
            return None  # 그 외 모든 입력 삼킴

        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            Quartz.kCGEventMaskForAllEvents,
            callback, None)
        if self.tap is None:
            return False
        self.tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(),
                                  self.tap_source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        return True

    def _destroyTap(self):
        """탭을 비활성화하고 런루프에서 제거, 포트 무효화. 몇 번 불려도 안전."""
        if self.tap is not None:
            try:
                Quartz.CGEventTapEnable(self.tap, False)
            except Exception:
                pass
        if self.tap_source is not None:
            try:
                Quartz.CFRunLoopRemoveSource(Quartz.CFRunLoopGetMain(),
                                             self.tap_source,
                                             Quartz.kCFRunLoopCommonModes)
            except Exception:
                pass
        if self.tap is not None:
            try:
                Quartz.CFMachPortInvalidate(self.tap)
            except Exception:
                pass
        self.tap = None
        self.tap_source = None

    # ── 락다운 (1단계: 오버레이 표시 → 2단계: 표시 확인 후 입력 차단) ──
    def lockdown(self):
        if self.locked:
            return
        log("[LOCK] 오버레이 표시 시도")
        self.stage = "shade"
        shown = self._showOverlays()
        if shown == 0:
            log("[!] 오버레이 생성 실패 → 락다운 취소 (벽돌 방지)")
            play(SOUND_ERROR)
            self._closeOverlays()
            return
        self.locked = True
        self.overlay_miss = 0
        AppHelper.callLater(OVERLAY_VERIFY_SEC, self.verifyOverlay)

    def verifyOverlay(self):
        """오버레이가 실제 화면에 커밋됐는지 확인한 뒤에만 입력을 차단한다."""
        if not self.locked:
            return
        onscreen = shield_windows_onscreen()
        if onscreen == 0:
            log("[!] 오버레이가 화면에 나타나지 않음 → 락다운 취소 (벽돌 방지)")
            self._cancelLockdown()
            return
        if not self._createTap():
            log("[!] 입력 차단 실패 — 손쉬운 사용(Accessibility) 권한 확인 필요 → 락다운 취소")
            self._cancelLockdown()
            return
        log(f"[LOCK] 오버레이 {onscreen}개 화면 표시 확인 → 입력 차단 활성화")
        play(SOUND_LOCK)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        notify(f"SP-1 LOCKDOWN ENGAGED // {ts}")

    def _cancelLockdown(self):
        play(SOUND_ERROR)
        self.locked = False
        self._destroyTap()
        self._closeOverlays()

    def enterAuthStage(self):
        """셰이드 단계에서 UNLOCK 버튼 클릭 → 풀스크린 HUD 인증 화면으로 전환."""
        if not self.locked or self.stage == "auth":
            return
        self.stage = "auth"
        log("[LOCK] UNLOCK 선택 → 생체 인증 화면 진입")
        play(SOUND_STEP)
        navy = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            0.010, 0.035, 0.090, 1.0)
        for win in self.windows:
            win.setOpaque_(True)
            win.setBackgroundColor_(navy)
        for v in self.button_views:
            v.setHidden_(True)
        for iv in self.preview_views:
            iv.setHidden_(False)

    def unlock(self):
        """어떤 상태에서 불려도 탭과 오버레이를 완전히 정리한다 (멱등)."""
        was = self.locked
        self.locked = False
        self.stage = "shade"
        self._destroyTap()
        self._closeOverlays()
        if was:
            log("[OPEN] 락다운 해제")
            play(SOUND_UNLOCK)

    def lockoutToLoginWindow(self):
        """해제 시도 초과 → 락다운은 유지한 채 macOS 잠금화면으로 전환.

        맥 비밀번호를 입력해야 세션에 돌아올 수 있고, 돌아와도 락다운은
        그대로 유지된다 (실패 카운트만 리셋).
        """
        if not self.locked:
            return
        self.stage = "shade"
        self.macos_screen_locked = True   # 탭이 비밀번호 입력을 막지 않도록
        for v in self.button_views:
            v.setHidden_(False)
        for iv in self.preview_views:
            iv.setHidden_(True)
        shade = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            0.0, 0.0, 0.0, 0.62)
        for win in self.windows:
            win.setOpaque_(False)
            win.setBackgroundColor_(shade)
        native_lock_screen()

    def emergencyEscape(self):
        log("[ESC] 비상 키 조합 → 전체 정리 후 macOS 잠금화면으로 전환")
        self.unlock()
        native_lock_screen()

    # ── 원격 명령 (폰 → ntfy → 맥) ──
    def remoteLock(self):
        if self.locked:
            log("[REMOTE] lock 요청 — 이미 락다운 상태")
            notify("SP-1 REMOTE // 이미 락다운 상태입니다")
            return
        log("[REMOTE] 원격 락다운 요청")
        self.lockdown()

    def remoteUnlock(self):
        if not REMOTE.get("allow_unlock", True):
            log("[REMOTE] unlock 요청 거부됨 (allow_unlock=false)")
            notify("SP-1 REMOTE // 원격 해제가 설정에서 비활성화됨")
            return
        if not self.locked:
            notify("SP-1 REMOTE // 락다운 상태가 아닙니다")
            return
        log("[REMOTE] 원격 해제 요청 → 락다운 해제")
        self.unlock()
        notify("SP-1 REMOTE // 락다운이 원격으로 해제되었습니다")

    def cameraFailed(self):
        if self.locked:
            log("[!] 락다운 중 카메라 실패 → 안전을 위해 macOS 잠금화면으로 전환")
            self.unlock()
            native_lock_screen()
        else:
            log("[!] 카메라 스레드가 종료되었습니다. 프로그램을 재시작하세요.")

    # ── 워치독: 2초마다 상태 불변식 점검 ──
    def watchdogTick_(self, timer):
        # 맥 잠금화면 중에는 판정 유예 — 오버레이가 WindowServer 목록에서
        # 빠져 보여도 락다운을 풀지 않는다 (전원 버튼 → 비번 복귀 시 유지)
        if session_screen_locked():
            return
        if not self.locked:
            if self.tap is not None:
                log("[!] 워치독: 잠금 아님 상태에서 이벤트 탭 발견 → 강제 제거")
                self._destroyTap()
            self.overlay_miss = 0
            return
        # 락다운 중: 탭이 꺼져 있으면 다시 켜고, 오버레이가 사라졌으면 안전 해제
        if self.tap is not None and not Quartz.CGEventTapIsEnabled(self.tap):
            log("[!] 워치독: 락다운 중 탭 비활성 감지 → 재활성화")
            Quartz.CGEventTapEnable(self.tap, True)
        if shield_windows_onscreen() == 0:
            self.overlay_miss += 1
            if self.overlay_miss >= 2:
                log("[!] 워치독: 락다운 중 오버레이 소실 → 안전 해제 + macOS 잠금화면")
                self.unlock()
                native_lock_screen()
        else:
            self.overlay_miss = 0

    def updateStatus_(self, text):
        for label in self.status_labels:
            label.setStringValue_(text)

    def updatePreview_(self, jpg_bytes):
        if not self.preview_views:
            return
        data = AppKit.NSData.dataWithBytes_length_(jpg_bytes, len(jpg_bytes))
        img = AppKit.NSImage.alloc().initWithData_(data)
        if img is None:
            return
        for view in self.preview_views:
            view.setImage_(img)

    # ── 오버레이 창 ──
    def _showOverlays(self):
        level = Quartz.CGShieldingWindowLevel()
        main = AppKit.NSScreen.mainScreen()
        if main is not None:
            fr = main.frame()
            self.hud_size = (int(fr.size.width), int(fr.size.height))
        # UNLOCK 버튼 히트 영역 (CG 전역 좌표: 주 화면 중앙, 220x56)
        if self.hud_size:
            W, H = self.hud_size
            self.button_rect = (W / 2 - 110, H / 2 - 28,
                                W / 2 + 110, H / 2 + 28)
        shown = 0
        for screen in AppKit.NSScreen.screens():
            try:
                self._makeOverlayForScreen_level_(screen, level)
                shown += 1
            except Exception:
                log("[!] 오버레이 생성 오류:\n" + traceback.format_exc())
        return shown

    def _makeOverlayForScreen_level_(self, screen, level):
        frame = screen.frame()
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setLevel_(level)
        # 1단계(셰이드): 원래 화면 위에 살짝 불투명한 검은 오버레이
        win.setBackgroundColor_(
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                0.0, 0.0, 0.0, 0.62))
        win.setOpaque_(False)
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(True)
        win.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)

        content = win.contentView()
        w, h = frame.size.width, frame.size.height

        # 2단계(auth)용 풀스크린 HUD 프레임 — 셰이드 단계에서는 숨김
        preview = AppKit.NSImageView.alloc().initWithFrame_(((0, 0), (w, h)))
        preview.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        preview.setHidden_(True)
        content.addSubview_(preview)
        self.preview_views.append(preview)

        # 주 화면에만 UNLOCK 버튼 표시 (클릭 감지는 이벤트 탭이 좌표로 처리)
        if frame.origin.x == 0 and frame.origin.y == 0:
            cyan = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                0.35, 0.85, 1.0, 1.0)
            bw, bh = 220, 56
            box = AppKit.NSView.alloc().initWithFrame_(
                (((w - bw) / 2, (h - bh) / 2), (bw, bh)))
            box.setWantsLayer_(True)
            layer = box.layer()
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(cyan.CGColor())
            layer.setCornerRadius_(6.0)
            layer.setBackgroundColor_(
                AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                    0.01, 0.05, 0.12, 0.85).CGColor())
            lbl = self._label("UNLOCK", 20, cyan, bold=True)
            lbl.setFrame_(((0, 14), (bw, 28)))
            box.addSubview_(lbl)
            content.addSubview_(box)
            self.button_views.append(box)

            cap = self._label("SECURITY PROTOCOL 1  //  SYSTEM LOCKED", 13,
                              AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                                  0.55, 0.78, 0.90, 0.9))
            cap.setFrame_(((0, (h - bh) / 2 + bh + 18), (w, 20)))
            content.addSubview_(cap)
            self.button_views.append(cap)

        win.orderFrontRegardless()
        self.windows.append(win)

    def _closeOverlays(self):
        for win in self.windows:
            win.orderOut_(None)
        self.windows = []
        self.status_labels = []
        self.preview_views = []
        self.button_views = []
        self.button_rect = None

    @staticmethod
    def _label(text, size, color, bold=False):
        label = AppKit.NSTextField.alloc().init()
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setAlignment_(AppKit.NSTextAlignmentCenter)
        weight = (AppKit.NSFontWeightBold if bold
                  else AppKit.NSFontWeightMedium)
        label.setFont_(
            AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight))
        label.setTextColor_(color)
        return label


def acquire_single_instance_lock():
    """중복 실행 방지 (자동 시작 + 수동 실행이 겹치는 상황 대비).

    파일 락은 프로세스가 죽으면 커널이 자동 해제하므로 잔여 락이 남지 않는다.
    """
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh   # 프로세스가 살아있는 동안 열어둔다


def main():
    test_mode = "--test" in sys.argv

    if not os.path.exists(MODEL_PATH):
        print(f"[!] 모델 파일이 없습니다: {MODEL_PATH}")
        sys.exit(1)

    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        # 이미 다른 인스턴스가 감시 중 → 정상 종료(0)로 빠진다.
        # launchd가 무한 재시작 루프를 돌지 않도록 실패가 아닌 성공으로 처리.
        print("[!] 이미 실행 중입니다 — 이 인스턴스는 종료합니다.")
        print("    기존 프로세스를 끄려면: pkill -f security_protocol.py")
        sys.exit(0)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    controller = SecurityApp.alloc().init()

    if test_mode:
        print("[*] 테스트 모드 — 락다운 없이 제스처 인식만 출력합니다. Ctrl+C로 종료.")
    else:
        if not accessibility_trusted():
            log("[!] 손쉬운 사용(Accessibility) 권한이 없습니다.")
            log("    시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용에서")
            log("    이 프로그램을 실행하는 터미널 앱을 켜주세요.")
            log("    (권한 없이는 락다운 시도가 자동 취소됩니다)")
        log("Security-Protocol-1 가동")
        trig = GESTURE_EMOJI.get(TRIGGER_GESTURE, TRIGGER_GESTURE)
        seq = " → ".join(GESTURE_EMOJI.get(g, g) for g in UNLOCK_SEQUENCE)
        if TRIGGER_GESTURE == "Double_Clap":
            log(f"  트리거: {trig}{trig} 박수 두 번 (오디오+비전 융합) / 해제: {seq}")
        else:
            log(f"  트리거: {trig} {TRIGGER_HOLD_SEC}초 유지 / 해제: {seq}")
        log("  비상키: 설정된 조합 → macOS 잠금화면")
        if CONFIG_SRC != "config.local.json":
            log("[!] config.local.json 없음 — 예시 설정으로 동작 중. "
                "config.example.json을 복사해 나만의 제스처로 바꾸세요.")
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            WATCHDOG_SEC, controller, "watchdogTick:", None, True)

    # App Nap 방지
    AppKit.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        0x00FFFFFF, "Security Protocol camera monitoring")

    # 카메라 권한은 카메라 스레드를 띄우기 전에 메인 스레드에서 확보
    if not ensure_camera_access():
        log("[!] 카메라 권한 없이는 동작할 수 없습니다. 권한 부여 후 재시작하세요.")
        sys.exit(1)

    watcher = GestureWatcher(controller, test_mode=test_mode)
    watcher.start()

    if not test_mode and REMOTE.get("enabled") and \
            REMOTE.get("ntfy_command_topic"):
        if not REMOTE.get("token"):
            log("[!] remote.token이 비어 있습니다 — 명령 topic을 아는 사람이면 "
                "누구나 맥을 조종할 수 있습니다. 토큰 설정을 권장합니다.")
        RemoteListener(controller, watcher).start()
        log("  원격 명령: lock / unlock / snap / status")

    def swallow_error():
        log("[!] 이벤트 루프에서 예기치 못한 오류 발생 — 계속 진행")
        return True

    AppHelper.runEventLoop(unexpectedErrorAlert=swallow_error)


if __name__ == "__main__":
    main()
