@echo off
chcp 65001 >nul
rem 더블클릭하면 제어판이 브라우저에 열립니다. 서버에서 바로 실행: start.bat run [--i-understand-the-risk]
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist .venv (
  %PY% -m venv .venv
  if errorlevel 1 goto nopython
)
call .venv\Scripts\activate.bat
echo 필요한 프로그램을 확인하는 중입니다 (처음 한 번만 1~2분)...
python -m pip install -q --disable-pip-version-check -r requirements.txt
if not exist config.toml copy config.example.toml config.toml >nul
if /i "%~1"=="run" (
  python -m tradebot check || goto end
  python -m tradebot run %2
) else (
  python -m tradebot ui
)
goto end
:nopython
echo Python 3.11 이상이 필요합니다: https://www.python.org/downloads/
echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
:end
pause
