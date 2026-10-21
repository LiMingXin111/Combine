@echo off
title Combine Team Service
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the server environment first: python -m venv .venv
  echo Then install dependencies: .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\start_team_server.py
if errorlevel 1 pause
