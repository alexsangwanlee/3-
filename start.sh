#!/usr/bin/env sh
# 제어판(브라우저)으로 시작: ./start.sh    서버에서 바로 실행: ./start.sh run [--i-understand-the-risk]
# 다른 명령: ./start.sh report | check | resume
set -e
cd "$(dirname "$0")"
NEED='import sys; sys.exit(sys.version_info < (3, 11))'
command -v python3 >/dev/null && python3 -c "$NEED" || {
  echo "Python 3.11 이상이 필요합니다 (지금: $(python3 -V 2>&1 || echo 없음)). https://www.python.org/downloads/ 에서 설치하세요."
  exit 1
}
# 예전 Python 으로 만들어졌거나 반쯤 만들어진 .venv 는 지우고 새로 만듭니다
.venv/bin/python -c "$NEED" 2>/dev/null || { rm -rf .venv; python3 -m venv .venv || {
  echo "가상환경을 만들지 못했습니다. Ubuntu/Debian 이면: sudo apt install python3-venv"; rm -rf .venv; exit 1; }; }
. .venv/bin/activate
echo "필요한 프로그램을 확인하는 중입니다 (처음 한 번만 1~2분)..."
pip install -q --disable-pip-version-check -r requirements.txt || {
  echo "설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요."; exit 1; }
[ -f config.toml ] || cp config.example.toml config.toml
case "$1" in
  run) shift; python -m tradebot check; exec python -m tradebot run "$@" ;;
  "") exec python -m tradebot ui ;;
  *) exec python -m tradebot "$@" ;;  # ./start.sh report, ./start.sh resume, ...
esac
