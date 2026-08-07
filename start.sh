#!/bin/zsh
# 앱 번들이 있으면 번들로 실행한다 (자동 시작과 같은 신원 → 권한 1회만 부여).
# 번들이 없으면 venv python으로 폴백.
cd "$(dirname "$0")"
DIR="$(pwd)"
APP="$DIR/SecurityProtocol1.app/Contents/MacOS/SecurityProtocol1"

if [ -x "$APP" ] && [ -f "$DIR/.app-pythonpath" ]; then
  export PYTHONPATH="$(cat "$DIR/.app-pythonpath")"
  exec "$APP" "$DIR/security_protocol.py" "$@"
fi
exec ./venv/bin/python security_protocol.py "$@"
