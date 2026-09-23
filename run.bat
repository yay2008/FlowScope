@echo off
REM --reload: 开发用。改 *.py 后自动重启 uvicorn, 不用手动 Ctrl+C 再启。
REM   两点注意:
REM   1. 每次重载 = 整个进程重启: TqApi 要关掉重连, 内存里的 K 线/tick 窗口会重建,
REM      期间页面会收到 resync 并有几秒空窗。盘中不想被打断就去掉 --reload。
REM   2. 只监视后端 *.py。static/ 下的 js/css/html 是每次请求从磁盘读的,
REM      改完刷新页面即可, 不要加进监视(否则改前端会白白重启后端)。
cd /d %~dp0
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
