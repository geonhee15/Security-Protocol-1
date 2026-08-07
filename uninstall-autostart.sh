#!/bin/zsh
# 로그인 시 자동 시작 해제 (LaunchAgent 제거)
cd "$(dirname "$0")"
LABEL="com.$(whoami).security-protocol-1"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "언로드됨" || echo "실행 중이 아님"
if [ -f "$PLIST" ]; then
  rm "$PLIST"
  echo "제거 완료: $PLIST"
else
  echo "설치되어 있지 않습니다."
fi
