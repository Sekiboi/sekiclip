@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" run.py
  exit /b 0
)
where pythonw >nul 2>&1 && (
  start "" pythonw run.py
  exit /b 0
)
python run.py
