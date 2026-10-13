@echo off
setlocal
cd /d "%~dp0"

rem ---- Use the project venv (.venv). Create it on first run with a final-release Python. ----
rem ---- Messages are ASCII on purpose: cmd.exe reads .bat files in the OEM code page,   ----
rem ---- so UTF-8 Korean here would show up garbled.                                       ----
set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto check

echo .venv not found. Creating it (first time only, takes a few minutes)...
set MADE=
for %%V in (3.11 3.12 3.13) do (
  if not defined MADE (
    py -%%V -m venv .venv >nul 2>&1
    if exist "%PY%" set MADE=%%V
  )
)
if not defined MADE python -m venv .venv
if not exist "%PY%" (
  echo Python not found. Install it and run again:
  echo   winget install -e --id Python.Python.3.12
  pause
  exit /b 1
)
:install
"%PY%" -m pip install -q --upgrade pip
echo Installing packages from requirements.txt. Download is about 500MB, so this can take 5-10 minutes.
echo Progress is shown below. Do not close this window.
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package install failed. Check the internet connection.
  pause
  exit /b 1
)
goto run

:check
rem ---- .venv may exist without the packages (e.g. made only for PyInstaller). ----
rem ---- If any required package is missing, install requirements.txt now.      ----
"%PY%" -c "import PySide6, numpy, sherpa_onnx, yt_dlp" >nul 2>&1
if errorlevel 1 (
  echo Some packages are missing in .venv. Installing them - first time only, takes a few minutes...
  goto install
)

:run
"%PY%" app.py
if errorlevel 1 (
  echo.
  echo The program exited with an error. See the messages above.
  pause
)
