@echo off
rem Korean-named shortcut. The real work is in build_prep.bat (ASCII name so other .bat files can call it).
call "%~dp0build_prep.bat" %*
exit /b %errorlevel%
