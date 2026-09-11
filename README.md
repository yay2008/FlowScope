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
- 工具栏「CVD口径」提供两种选择，也同步影响 Delta 与买卖量。默认「TqSdk tick口径」（`ltf=0`），使用 tick 快照盘口**估算**主动方向，此时禁用拆分粒度。快照区间成交量不能还原区间内逐笔成交的方向和价格。
- 「TV K线口径」直接订阅 TqSdk 小周期 K 线，按 `close > open` 把该根成交量计为买量，`close < open` 计为卖量，十字线不计入买卖量，再汇总到 30 秒。拆分粒度支持 1s、5s、10s、15s、30s，默认 10s，切回时记住本次页面使用的粒度。两种口径的数据源均为 TqSdk；TV 表示 Pine 的计算方法，数据源与累计起点仍可能导致结果与 TradingView 不同。
- CVD/Delta 的 RELATIVE 颜色阈值对齐 Pine：正负 Delta 分别除以各自的 20 根均值，严格大于 `[1.5, 2.5, 3.5] × 1.5` 才进入相应等级。
- 工具栏「视图」可切换到足迹图（固定 ltf=0 口径）。按原始成交价聚合，价格步长来自合约 `price_tick`；元数据未知时不推断步长，也不显示对角不平衡。稀疏成交档位只与相隔一个实际 tick 的价位比较。
- 每根 bar 带 `coverage`：`complete` 表示 tick 有前置快照或小周期 K 线根数齐全，且区间量与主周期成交量一致；`partial` 为部分覆盖或数据尚未对齐；`missing` 为无数据；`legacy` 为旧格式历史回填。“完整”指覆盖范围，方向和分价成交量仍是估算。
- tick 历史写入 `data/{symbol}_30s_ltf0_v2.csv`，K 线口径写入 `data/{symbol}_30s_kline_ltf{ltf}_v2.csv`，两种口径和各粒度独立；K 线口径不读取旧版本由 tick 拼接小周期的缓存。只保存已完成、覆盖完整的 bar。窗口滚动后的部分数据不会覆盖已确认的完整记录；同一时间的修订追加到文件，重启读取最后一条。
- 已完成但未通过成交量核对的快照估算量单独保存到 `*_v2_estimated.csv`，仍标记为 `partial`。窗口边缘截断的数据不会覆盖已保存的估算记录。
- 旧 `data/{symbol}_30s.csv` 保持原样，仅用于 tick 粒度显示回填，并标记 `legacy`；因缺少覆盖信息，不直接视作已确认记录。
- **FlowMeter 的 CVD 从该合约、该粒度的可用买卖量估算累计**，包括部分覆盖和旧历史回填；真正缺失的 bar 留空。估算历史和已确认历史使用同一固定基准，后者优先且不重复累计，不随窗口滑动清零。每根 CVD 蜡烛使用自身 `cvdOpen` 和 `cvd` 绘制，前一根缺失也能显示。`cvdConfirmed` 独立提供仅包含核对通过记录的累计量。数据缺口前后不是连续成交全量，覆盖标记始终保留。
- TqSdk tick 窗口上限 10,000 条；按每 500ms 一条估算约 83 分钟，实际跨度受行情频率和休市影响。更早的买卖量依赖持续采集的历史文件；足迹不落盘，按需计算并在内存保留最多 800 根。
- tick 粒度持续采集落盘；其他粒度和足迹按请求启用计算，最后一次请求后保留约 60 秒。小周期 K 线在 SDK 线程按需订阅，每种最多 10,000 根。前端最多保留 800 根，tick 分类与聚合复用已完成 bar，只重算新增或修订部分；K 线口径聚合已订阅的小周期序列。

同一数据目录请只运行一个服务进程，使用默认单 worker 启动方式。

## 接口

- `GET /api/history?symbol=...&ltf=0`：返回最近 800 根 bar 快照，包含 `source`（`tick` 或 `kline`）、`revision`、`cvdBase` 和各 bar 的 `coverage`。`ltf=0` 为 tick 口径，正数为 K 线口径的小周期秒数。
- `GET /api/footprint?symbol=...`：按需返回足迹快照，包含 `revision`、`tickSize`（未知时为 null）和各 bar 的 `coverage`。
- `GET /api/status`：返回行情采集线程状态、最近错误和已订阅合约。
- `WS /ws?symbol=...&ltf=0&footprint=false`：先注册订阅，再发送 `snapshot` 完整快照，随后发送带版本号的 `bars` 批量增量，包含最新 bar 和历史修订。`footprint=true` 还会接收 `footprints` 或 `footprint_snapshot`；两种数据分别跟踪版本号。
- 每次 WebSocket 重连都重新同步完整快照。慢客户端队列溢出时以 1013 关闭连接，前端自动重连补齐；15 秒无业务消息时发送包含行情源状态的 `ping`。

合约参数只接受字母、数字、点、下划线、短横线和 `@`，用于防止非法订阅与路径穿越。

## 验证

离线回归测试不需要 TqSdk 账号，历史文件写入临时目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/data-sync.test.js
node --check static/app.js
```

可用模拟行情验收实际页面（不连接真实行情，不改写真实历史）：

```powershell
.\.venv\Scripts\python.exe tests/preview_app.py --duration 300
```

访问 `http://127.0.0.1:8765/`。模拟器也会制造 K 线与 tick 分批到达的短暂部分覆盖，用于检查图表恢复；300 秒后自动停止。
