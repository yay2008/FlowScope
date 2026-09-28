@echo off
REM 用法:
REM   run.bat       常规启动(盘中用): 不自动重载, 改后端代码后手动 Ctrl+C 再启。
REM   run.bat dev   开发用: 改后端 *.py 自动重启 uvicorn。
REM     每次重载 = 整个进程重启: TqApi 要关掉重连, 内存里的 K 线/tick 窗口会重建,
REM     期间页面会收到 resync 并有几秒空窗, 盘中不要用。
REM     docs/(离线脚本)、tests/、.venv 不在监视范围; uvicorn 只认绝对路径的排除目录。
REM   static/ 下的 js/css/html 是每次请求从磁盘读的, 改完刷新页面即可, 两种模式都不必重启。
cd /d %~dp0
if /i "%~1"=="dev" (
  .venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload ^
    --reload-exclude "%~dp0docs" --reload-exclude "%~dp0tests" --reload-exclude "%~dp0.venv"
) else (
  .venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
)
