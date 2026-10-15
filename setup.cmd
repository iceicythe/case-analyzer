@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "configs\deepseek.local.json" copy /Y "configs\deepseek.example.json" "configs\deepseek.local.json" >nul
if not exist ".venv\Scripts\python.exe" (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
  if errorlevel 1 goto failed
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -B -X utf8 -m app --check
if errorlevel 1 goto failed
echo Setup completed. Run start.cmd to open the workbench.
exit /b 0
:failed
echo Setup failed. Check Python 3.11+, network access and the error above.
exit /b 1
