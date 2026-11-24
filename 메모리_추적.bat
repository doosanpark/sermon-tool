@echo off
setlocal
cd /d "%~dp0"
rem ---- Watch Windows commit memory for 10 minutes. Writes data\memtrace.txt ----
rem ---- Close the Sermon Tool first, then run this and wait 10 minutes.      ----
rem ---- ASCII only on purpose: cmd.exe reads .bat files in the OEM code page.----
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\memtrace.ps1"
echo.
echo Show the file  data\memtrace.txt  to Claude.
echo.
pause
