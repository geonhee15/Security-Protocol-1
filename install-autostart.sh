#!/bin/zsh
# 로그인 시 Security-Protocol-1 자동 시작 (LaunchAgent 설치)
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.$(whoami).security-protocol-1"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$DIR/SecurityProtocol1.app/Contents/MacOS/SecurityProtocol1"

# 앱 번들 생성/갱신 (카메라 권한을 받기 위해 필수)
./build-app.sh
SITE=$(cat "$DIR/.app-pythonpath")

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP</string>
        <string>$DIR/security_protocol.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>$SITE</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$DIR/autostart.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/autostart.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo ""
echo "설치 완료: $PLIST"
echo "  상태 확인:  launchctl print gui/$(id -u)/$LABEL | grep state"
echo "  로그 확인:  tail -f $DIR/autostart.log"
echo "  제거:       ./uninstall-autostart.sh"
echo ""
echo "[권한] 이제 SecurityProtocol1.app 이름으로 권한을 요청합니다."
echo "  1) 카메라 — 첫 실행 시 팝업이 뜨면 허용"
echo "  2) 손쉬운 사용 — 시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용에서"
echo "     '+' 를 눌러 아래 앱을 추가하세요:"
echo "     $DIR/SecurityProtocol1.app"
