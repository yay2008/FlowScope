# FlowScope

FlowScope 是一个基于 TqSdk 的期货 30 秒行情监控页面，提供主动买卖量、Volume Suite、Delta、CVD 与 LSMA×CRVOL 等指标。

## 启动

1. 创建 Python 3.12 虚拟环境并安装依赖：

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. 在项目根目录复制 `.env.example` 为 `.env`，填写 TqSdk 账号：

   ```powershell
   Copy-Item .env.example .env
   ```

   服务启动时会自动读取 `.env`；也可以继续使用系统环境变量覆盖同名配置。

3. 启动服务：

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

   也可以运行 `run.bat`。

浏览器访问 `http://127.0.0.1:8000/`。图表库已随项目放在 `static/vendor/`，可离线打开；若本地文件不可用，页面会回退到 CDN。行情线程断线时会自动重连，状态可通过 `/api/status` 查看。

## 数据与口径

- 默认合约是 `KQ.m@SHFE.fu`，可在页面中切换；服务最多保留 32 个合约订阅。
- `ltf=0` 使用 tick 盘口判定主动方向；其他选项按小周期 K 线阴阳归类。
- `data/{symbol}_30s.csv` 保存已完成 30 秒 bar 的买卖量。服务启动时会读取、去重并按时间排序，避免重启重复追加。
- tick 历史受 TqSdk 返回窗口限制，更早的买卖量依赖服务持续运行期间的 CSV 累积。

## 接口

- `GET /api/history?symbol=...&ltf=0`：返回最近 800 根 bar 快照。
- `GET /api/status`：返回行情采集线程状态、最近错误和已订阅合约。
- `WS /ws?symbol=...&ltf=0`：接收指定粒度的最新 bar 增量和心跳消息；不同客户端的粒度相互隔离。

合约参数只接受字母、数字、点、下划线、短横线和 `@`，用于防止非法订阅与路径穿越。
