#!/usr/bin/env sh
# 제어판(브라우저)으로 시작: ./start.sh        서버에서 바로 실행: ./start.sh run [--i-understand-the-risk]
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python 3.11 이상이 필요합니다: https://www.python.org/downloads/"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
echo "필요한 프로그램을 확인하는 중입니다 (처음 한 번만 1~2분)..."
pip install -q --disable-pip-version-check -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
if [ "$1" = "run" ]; then
  shift
  python -m tradebot check
  exec python -m tradebot run "$@"
fi
exec python -m tradebot ui
