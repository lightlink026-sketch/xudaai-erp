@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found.
  echo Create it with: python -m venv .venv
  echo Then install requirements.txt and requirements-clip.txt.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
