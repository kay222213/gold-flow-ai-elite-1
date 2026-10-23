@echo off
setlocal
cd /d "%~dp0backend"
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist .env (
  copy .env.example .env
  echo Created backend\.env. Add provider API keys to activate live feeds; the app will start in guarded NO TRADE mode meanwhile.
)
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
