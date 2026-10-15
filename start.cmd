@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B -X utf8 -m app %*
) else (
  python -B -X utf8 -m app %*
)
if errorlevel 1 pause
