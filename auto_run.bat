@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
echo ============================================
echo  Checking channel for new service videos
echo ============================================
"%PY%" auto.py once
echo.
pause
