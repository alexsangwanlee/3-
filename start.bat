@echo off
rem Windows: start.bat            (실거래: config.toml 에서 mode = "live" 후 start.bat --i-understand-the-risk)
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist config.toml copy config.example.toml config.toml >nul
if not exist .env (
  copy .env.example .env >nul
  echo .env 를 만들었습니다. 실거래를 하려면 API 키를 넣으세요. 모의매매는 그대로 실행됩니다.
)
python -m tradebot check || exit /b 1
python -m tradebot run %*
