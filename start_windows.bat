@echo off
cd /d "%~dp0"
if not exist .env (
  echo Copy .env.example to .env and set BOT_TOKEN and ADMIN_IDS first.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe main.py
pause
