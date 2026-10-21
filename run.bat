@echo off
rem 一键启动（Windows）：创建虚拟环境、安装依赖、启动服务，浏览器打开 http://127.0.0.1:8765
cd /d "%~dp0"
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements-lock.txt
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765
