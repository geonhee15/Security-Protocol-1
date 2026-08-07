# Security-Protocol-1

카메라 손 제스처로 발동하는 macOS 락다운 시스템 — 풀스크린 JARVIS 스타일 HUD.

![preview](docs/preview.png)

## 무엇을 하나

카메라에 **설정한 트리거 제스처**를 잠시 유지하면 락다운이 걸린다. 락다운은 2단계로 동작한다:

1. **셰이드 단계** — 원래 화면 위에 살짝 불투명한 검은 오버레이가 덮이고, 키보드·마우스가 차단된다. 커서 이동과 화면 중앙의 **UNLOCK 버튼 클릭만** 허용된다.
2. **인증 단계** — UNLOCK을 누르면 풀스크린 JARVIS HUD가 뜨고, **설정한 해제 제스처 시퀀스**를 카메라에 순서대로 보여줘야만 풀린다.

락다운 중에도 전원 버튼(잠금화면 진입)은 정상 동작하며, macOS 비밀번호로 세션에 복귀해도 **락다운은 그대로 유지**된다.

> 🔐 트리거 제스처, 해제 시퀀스, 비상키는 `config.local.json`(gitignore됨)에만 존재한다. 이 저장소에는 예시 설정만 포함되어 있으므로, 코드를 봐도 실제 잠금을 풀 방법은 알 수 없다.

## HUD

- 카메라 영상이 화면 전체를 채우고(다크 네이비 틴트) 그 위에 모든 요소를 렌더
- **홀로그램 오브**: 유기적으로 일렁이며 회전하는 링 3겹 + 근접 노드끼리 실시간으로 연결되는 신경망 애니메이션(80노드) — 손바닥을 따라다님
- 손 21개 관절 스켈레톤 + 손끝 ID 라벨 + 타겟 브래킷(크기·중심좌표 실측)
- 좌측 패널: 헥스 스트림, PROC 로드 바, **손가락 신장도(실측)**, NEURAL SYNC
- 우측 패널: 제스처 클래스·신뢰도, **홀드 게이지 링(실측)**, **관절 각도(실측)**, 신뢰도 트레이스 그래프, AUTH 진행 세그먼트
- 코너 회전 아크, 도트 매트릭스, 좌표 눈금자, 애니메이션 파형, 스캔라인

## 설치

```bash
git clone https://github.com/geonhee15/Security-Protocol-1.git
cd Security-Protocol-1
./setup.sh
```

`setup.sh`는 가상환경 생성 → 의존성 설치 → MediaPipe 제스처 모델 다운로드 → `config.local.json` 생성까지 자동으로 한다.

### 권한 (최초 1회)

시스템 설정 → 개인정보 보호 및 보안에서, 이 프로그램을 실행하는 터미널 앱에:

1. **카메라** — 첫 실행 시 팝업 허용
2. **손쉬운 사용(Accessibility)** — 입력 차단에 필요. 없으면 락다운 시도가 자동 취소됨

## 설정 — `config.local.json`

```json
{
  "trigger_gesture": "Thumb_Down",
  "trigger_hold_sec": 1.5,
  "unlock_sequence": ["Thumb_Up", "ILoveYou", "Thumb_Up"],
  "step_hold_sec": 0.8,
  "emergency_keycode": 37,
  "emergency_modifiers": ["control", "option", "command"]
}
```

- **사용 가능한 제스처**: `Closed_Fist` `Open_Palm` `Victory` `Pointing_Up` `Thumb_Up` `Thumb_Down` `ILoveYou`
- `unlock_sequence`는 원하는 길이만큼 배열로 — 길수록 안전
- `emergency_keycode`는 macOS 가상 키코드 (예: A=0, S=1, L=37, ...), `emergency_modifiers`는 `control`/`option`/`command`/`shift` 조합
- **비상키 조합을 누르면 락다운을 풀고 즉시 macOS 기본 잠금화면으로 전환** — 맥 비밀번호 없이는 못 들어오므로 보안이 유지된다

## 실행

```bash
./start.sh --test   # 인식 테스트만 (락다운 없음)
./start.sh          # 상시 감시 가동
```

종료는 실행 중인 터미널에서 `Ctrl+C`, 또는 다른 터미널에서 `pkill -f security_protocol.py`.

**소리 의미**: Sosumi(삐) = 입력 차단 확정 · Tink(틱) = 해제 단계 성공 · Glass(띠링) = 해제 · Basso(둔탁) = 오버레이 표시 실패로 락다운 자동 취소

## 벽돌 방지 안전장치

이런 종류의 도구는 잘못 만들면 자기 기기를 벽돌로 만든다. 다음 장치들이 겹겹이 들어있다:

- 입력 차단용 이벤트 탭(CGEventTap)은 **락다운 중에만 존재** — 해제 시 mach port까지 완전 파괴하므로, 락다운이 아닌데 입력이 차단되는 상태가 구조적으로 불가능
- 오버레이가 **WindowServer에 실제로 표시된 것을 확인한 뒤에만** 입력 차단 (macOS의 창 표시가 비동기라 이 확인이 필수)
- **워치독(2초마다)**: 잠금 아닌데 탭이 남아있으면 강제 제거, 락다운 중 오버레이가 사라지면 안전 해제 후 macOS 잠금화면 전환
- 해제·비상탈출·macOS 잠금해제 직후 쿨다운 (재발동 방지), macOS 잠금화면에서는 트리거 무시
- macOS 잠금화면(전원 버튼 등) 중에는 이벤트 탭이 전부 통과 — 비밀번호 입력을 방해하지 않고, 세션 복귀 시 차단이 즉시 재개되며 락다운은 유지됨
- 락다운 중 카메라가 죽으면 자동으로 macOS 잠금화면 전환
- 제스처 인식 디바운싱: 프레임 단위 인식 깜빡임(0.5초 이내)은 유지로 간주
- 모든 이벤트가 `protocol.log`에 기록 (gitignore됨)

## 로그인 시 자동 시작 (선택)

`~/Library/LaunchAgents/com.<이름>.security-protocol-1.plist`를 만들어 `ProgramArguments`에 `<프로젝트경로>/venv/bin/python`과 `<프로젝트경로>/security_protocol.py`를 넣고 `RunAtLoad`/`KeepAlive`를 true로 설정한 뒤 `launchctl load` 하면 된다. launchd로 실행하면 python 바이너리 자체에 카메라/손쉬운 사용 권한을 부여해야 할 수 있다.

## 한계

- 전원 버튼 강제 재부팅은 소프트웨어로 막을 수 없다 — 재부팅 후에는 macOS 로그인 비밀번호가 최종 방어선이므로 **FileVault + 로그인 비밀번호**를 켜둘 것
- SSH가 열려 있으면 원격에서 프로세스를 종료할 수 있다 (역으로 비상 탈출구로도 활용 가능)
- 웹캠 지문 인식은 해상도 한계로 불가능

## 스택

Python 3.12 · [MediaPipe](https://developers.google.com/mediapipe) GestureRecognizer · OpenCV · PyObjC (AppKit/Quartz)
