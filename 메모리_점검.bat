@echo off
setlocal
cd /d "%~dp0"
rem ---- Check what is holding Windows commit memory. Writes data\memcheck.txt ----
rem ---- It only looks. It changes nothing.                                     ----
rem ---- ASCII only on purpose: cmd.exe reads .bat files in the OEM code page.  ----
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\memcheck.ps1"
echo.
echo Show the file  data\memcheck.txt  to Claude.
echo.
pause
