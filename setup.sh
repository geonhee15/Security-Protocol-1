#!/bin/zsh
set -e
cd "$(dirname "$0")"

echo "[1/4] 가상환경 생성"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip

echo "[2/4] 의존성 설치"
./venv/bin/pip install --quiet -r requirements.txt

echo "[3/4] 제스처 인식 모델 다운로드 (~8MB)"
mkdir -p models
if [ ! -f models/gesture_recognizer.task ]; then
  curl -sL -o models/gesture_recognizer.task \
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
fi

echo "[4/4] 로컬 설정 생성"
if [ ! -f config.local.json ]; then
  cp config.example.json config.local.json
  echo "    config.local.json 생성됨 — 반드시 나만의 제스처/비상키로 수정하세요!"
fi

echo "완료. ./start.sh --test 로 인식 테스트 후 ./start.sh 로 가동하세요."
