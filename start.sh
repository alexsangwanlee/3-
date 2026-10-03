#!/usr/bin/env sh
# Mac/Linux: ./start.sh            (실거래: config.toml 에서 mode = "live" 후 ./start.sh --i-understand-the-risk)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f config.toml ] || cp config.example.toml config.toml
if [ ! -f .env ]; then
  cp .env.example .env && chmod 600 .env
  echo ".env 를 만들었습니다. 실거래를 하려면 API 키를 넣으세요 (모의매매는 그대로 실행됩니다)."
fi
python -m tradebot check
exec python -m tradebot run "$@"
