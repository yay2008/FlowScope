#!/usr/bin/env bash
# 用法:
#   ./run.sh       盘中模式: 不自动重载, 前台运行, Ctrl+C 停止
#   ./run.sh dev   开发模式: 改后端 *.py 自动重启 (会断开行情, 盘中不要用)
cd "$(dirname "$0")" || exit 1
if [ "$1" = "dev" ]; then
  exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload \
    --reload-exclude docs --reload-exclude tests --reload-exclude .venv
else
  exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
fi
