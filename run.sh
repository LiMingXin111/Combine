#!/usr/bin/env bash
# 一键启动（macOS / Linux）：创建虚拟环境、安装依赖、启动服务，浏览器打开 http://127.0.0.1:8765
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-lock.txt
exec python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "${PORT:-8765}"
