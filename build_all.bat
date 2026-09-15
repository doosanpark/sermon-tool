@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  SermonTool - build exe and installer
echo ============================================
echo.

rem --- pick a FINAL python (3.10.0rc1 has a dis bug that breaks PyInstaller) ---
set PY=
rem --- prefer the project venv if run.bat already created it (all packages are in there) ---
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys;raise SystemExit(0 if sys.version_info.releaselevel=='final' else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=.venv\Scripts\python.exe"
)
for %%V in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys;raise SystemExit(0 if sys.version_info.releaselevel=='final' else 1)" >nul 2>&1
    if not errorlevel 1 set PY=py -%%V
  )
)
if not defined PY (
  python -c "import sys;raise SystemExit(0 if sys.version_info.releaselevel=='final' else 1)" >nul 2>&1
  if not errorlevel 1 set PY=python
)
if not defined PY (
  echo No final-release Python found.
  echo   winget install -e --id Python.Python.3.12
  echo   py -3.12 -m pip install PySide6 sherpa-onnx numpy faster-whisper pyinstaller
  goto err
)
echo Using: %PY%
%PY% -c "import sys;print('  Python', sys.version.split()[0])"
echo.

set MODE=lite
if /i "%~1"=="full" set MODE=full
if /i "%~1"=="--full" set MODE=full
echo Build mode: %MODE%
echo   (run  build_all.bat full  to include AI analysis)
echo.

echo [1/3] Checking PyInstaller
%PY% -m pip install -q pyinstaller
if errorlevel 1 goto err

echo [2/3] Building exe
if "%MODE%"=="full" (
  %PY% build_exe.py --full --clean
) else (
  %PY% build_exe.py --clean
)
if errorlevel 1 goto err

echo [3/3] Building installer
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo   Inno Setup not found. Download: https://jrsoftware.org/isdl.php
  echo   The exe is ready at: dist\SermonTool\SermonTool.exe
  goto done
)
"%ISCC%" installer.iss
if errorlevel 1 goto err
echo.
echo Installer created: Output\SermonToolSetup.exe

:done
echo.
echo Done.
pause
exit /b 0

:err
echo.
echo BUILD FAILED - see messages above.
pause
exit /b 1
