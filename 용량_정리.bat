@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>&1 || ( echo git not found. & pause & exit /b 1 )
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\yongryang_jeongri.ps1"
pause
