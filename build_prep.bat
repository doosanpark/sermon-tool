@echo off
setlocal
cd /d "%~dp0"
rem ---- Prepares a fresh PC for build_all.bat: venv, packages, PyInstaller, then bin tools and assets. ----
rem ---- ASCII only on purpose: cmd.exe reads .bat files in the OEM code page.                          ----
rem ---- Run again any time. It only fetches what is missing.                                          ----

echo ============================================
echo  SermonTool - prepare this PC for building
echo ============================================
echo.

set "PY=.venv\Scripts\python.exe"
if exist "%PY%" goto have

echo Creating .venv - needs a final-release Python 3.11 to 3.13.
set MADE=
for %%V in (3.12 3.13 3.11) do (
  if not defined MADE (
    py -%%V -m venv .venv >nul 2>&1
    if exist "%PY%" set MADE=%%V
  )
)
if not exist "%PY%" (
  echo.
  echo Python not found. Install it, then run this file again:
  echo   winget install -e --id Python.Python.3.12
  goto fail
)

:have
echo [1/3] Python packages
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto pipfail

echo.
echo [2/3] Tools and assets
"%PY%" tools\prepare_build.py %*
if errorlevel 1 goto fail

echo.
echo [3/3] Ready. Next: build_all.bat
if not defined SERMON_NOPAUSE pause
exit /b 0

:pipfail
echo.
echo Package install failed. Check the internet connection and run again.

:fail
echo.
echo NOT READY - see messages above.
if not defined SERMON_NOPAUSE pause
exit /b 1
