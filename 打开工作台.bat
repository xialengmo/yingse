@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
py -3 -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 goto py_launcher
python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>&1
if not errorlevel 1 goto python_launcher
echo Python 3.9+ is required. Install from https://www.python.org/downloads/windows/
pause
exit /b 2
:py_launcher
py -3 "%~dp0launcher.py"
goto end
:python_launcher
python "%~dp0launcher.py"
:end
if errorlevel 1 pause
