@echo off
rem Taskbar Stock Widget launcher (no console window)
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if %errorlevel%==0 goto use_pythonw

where python.exe >nul 2>nul
if %errorlevel%==0 goto use_python

echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
pause
exit /b 1

:use_pythonw
start "" pythonw.exe "%~dp0main.py"
exit /b 0

:use_python
start "" python.exe "%~dp0main.py"
exit /b 0
