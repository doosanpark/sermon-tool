@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>&1 || ( echo git not found. Install Git for Windows or commit with your git tool. & pause & exit /b 1 )
echo Files to commit:
git status --short -- run.bat app.py ui_summary.py core/transcribe.py
echo.
git add -- run.bat app.py ui_summary.py core/transcribe.py || goto fail
git commit -F commit_msg.txt || goto fail
del commit_msg.txt
echo.
git log --oneline -3
echo.
echo Committed. Not pushed. This file can be deleted now.
pause
exit /b 0
:fail
echo.
echo COMMIT FAILED - see messages above. Nothing was pushed.
pause
exit /b 1
