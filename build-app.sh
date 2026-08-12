#!/bin/zsh
# SecurityProtocol1.app 번들 생성.
#
# macOS는 앱 번들이 아닌 맨 바이너리에는 카메라 권한 팝업을 띄우지 않는다.
# (특히 launchd로 실행될 때 즉시 거부됨) 그래서 python 실행 파일을 최소 앱
# 번들로 감싸고 ad-hoc 서명해, TCC가 이 번들을 하나의 앱으로 인식하게 한다.
# 수동 실행(start.sh)과 자동 시작(launchd)이 같은 번들을 쓰므로 권한은 1회만.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
APP="$DIR/SecurityProtocol1.app"

if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "[!] venv가 없습니다. 먼저 ./setup.sh 를 실행하세요."
  exit 1
fi

BASE=$("$DIR/venv/bin/python" -c "import sys; print(sys.base_prefix)")
SITE=$("$DIR/venv/bin/python" -c "import site; print(site.getsitepackages()[0])")

# 프레임워크의 Python.app 실행 파일을 우선 사용 (GUI/TCC에 적합)
SRC="$BASE/Resources/Python.app/Contents/MacOS/Python"
[ -f "$SRC" ] || SRC=$(readlink -f "$DIR/venv/bin/python")

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$SRC" "$APP/Contents/MacOS/SecurityProtocol1"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>SecurityProtocol1</string>
    <key>CFBundleDisplayName</key><string>Security Protocol 1</string>
    <key>CFBundleExecutable</key><string>SecurityProtocol1</string>
    <key>CFBundleIdentifier</key><string>com.$(whoami).security-protocol-1</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSUIElement</key><true/>
    <key>NSCameraUsageDescription</key>
    <string>제스처 인식으로 락다운을 발동하고 해제하기 위해 카메라를 사용합니다.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>더블 클랩(박수 두 번) 트리거의 음향 임펄스를 감지하기 위해 마이크를 사용합니다.</string>
</dict>
</plist>
EOF

plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP" 2>&1 | grep -v "replacing existing signature" || true

# 실행 환경 기록 (start.sh / LaunchAgent가 사용)
echo "$SITE" > "$DIR/.app-pythonpath"

echo "앱 번들 생성: $APP"
echo "[!] 재서명됨 — macOS가 손쉬운 사용(Accessibility) 권한을 무효화합니다."
echo "    시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용에서"
echo "    SecurityProtocol1을 제거(-) 후 다시 추가(+)해 주세요."
echo "  실행 파일: $(basename "$SRC") → Contents/MacOS/SecurityProtocol1"
echo "  PYTHONPATH: $SITE"
