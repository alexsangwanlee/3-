@echo off
chcp 65001 >nul
set PYTHONUTF8=1
rem 더블클릭하면 제어판이 브라우저에 열립니다. 서버에서 바로 실행: start.bat run [--i-understand-the-risk]
rem 다른 명령: start.bat report | check | resume
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul || goto nopython
rem 예전 Python 으로 만들어졌거나 반쯤 만들어진 .venv 는 지우고 새로 만듭니다
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>nul || rmdir /s /q .venv
)
if not exist .venv\Scripts\python.exe (
  if exist .venv rmdir /s /q .venv
  %PY% -m venv .venv
  if errorlevel 1 goto nopython
)
call .venv\Scripts\activate.bat
echo 필요한 프로그램을 확인하는 중입니다 (처음 한 번만 1~2분)...
python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo 설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요.
  goto end
)
if not exist config.toml copy config.example.toml config.toml >nul
if /i "%~1"=="run" (
  python -m tradebot check || goto end
  python -m tradebot run %2
) else if "%~1"=="" (
  python -m tradebot ui
) else (
  python -m tradebot %*
)
goto end
:nopython
echo Python 3.11 이상이 필요합니다: https://www.python.org/downloads/
echo 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
:end
pause
