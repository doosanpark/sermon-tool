@echo off
setlocal
cd /d "%~dp0"
rem ---- Gathers voiceprint, word list and AI models from this PC into dist\ (the pack folder). ----
rem ---- ASCII only: cmd.exe reads .bat files in the OEM code page.                              ----

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"

%PY% tools\prepare_build.py --pack
if errorlevel 1 echo Failed - see messages above.
echo.
pause
