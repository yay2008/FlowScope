# Twelve Data 黄金行情探针

`twelvedata_probe.py` 独立检查当前 Key 对 `XAU/USD` 的 1 分钟 OHLC K 线和 WebSocket 价格流的访问能力，并记录更新延迟。它不启动 FlowScope 服务，不使用 TqSdk 账号。

## 运行

在项目根目录现有 `.env` 中增加一行（不要覆盖已有配置）：

```dotenv
TWELVEDATA_API_KEY=填入你的免费APIKey
```

也可以不改 `.env`，直接启动，按提示隐藏输入 Key。环境变量优先于 `.env`。不提供命令行 Key 参数，报告和控制台中的 Key 会被脱敏。

```powershell
.\.venv\Scripts\python.exe docs\twelvedata_probe.py
```

脚本使用项目已有的 `python-dotenv`、`websockets>=13,<18`，其余为 Python 标准库。若尚未安装依赖，先按项目 README 创建 `.venv` 并安装 `requirements.txt`。

默认并行测试 **300 秒**，REST 每 **15 秒**拉最近 10 根 1 分钟 K 线，约用 20 个积分；WebSocket 只建一个连接、订阅黄金，每 10 秒发送应用心跳。不会注册账号、切换套餐或重连刷订阅。请在黄金开市期间测试。

```powershell
# 测 10 分钟，最多发起 40 次 REST 请求
.\.venv\Scripts\python.exe docs\twelvedata_probe.py --duration 600

# 若现有代理导致 WebSocket 握手失败，可单独对比直连
.\.venv\Scripts\python.exe docs\twelvedata_probe.py --no-proxy

# 看全部参数
.\.venv\Scripts\python.exe docs\twelvedata_probe.py --help
```

可用 `--poll-interval` 调整轮询间隔（至少 10 秒），`--max-rest-requests` 设置本次请求上限（默认 40）。达到上限仅停止 REST，WebSocket 继续到测试时间结束；HTTP/API 400、401、403、404、429 会停止该 REST 测试，避免反复消耗额度。其他程序共用这个 Key 的额度，因此脚本的次数限制不代表账号一定还有余额。

## 结果与测量口径

每次运行创建 `data/twelvedata_probe/<UTC时间戳>/`：

- `events.jsonl`：每次 REST 响应、信用额度响应头、订阅结果、每条价格及错误。
- `report.json`：两路可用性、样本数、延迟 min/p50/p95/max、收盘后修订次数、时钟诊断。

所有报告时间使用 UTC。按 Ctrl+C 会尽量保存已有结果。退出码 0 表示两路都收到有效数据；1 表示至少一路尚未确认或运行失败；2 表示参数/依赖/Key 配置错误；130 表示手动中断。`available` 只表示本次确实收到有效数据，不承诺连接后续一直稳定；查看 `errors` 和 `stop_reason` 判断是否提前断开。

| 指标 | 含义 |
| --- | --- |
| `rest.rtt_ms` | 一次 HTTP 请求到完整响应的耗时，含建立连接、服务器处理和传输，不是行情更新延迟 |
| `rest.close_to_first_observation_s` | 本次测试期间收盘的 K 线，收盘后第一次在响应中看到它的时间差；启动时的历史 K 线排除 |
| `rest.new_closed_bar_observations` | 逐根观测，以及前次成功请求开始至本次响应到达的轮询区间；失败请求不缩短区间 |
| `rest.latest_closed_bar_age_s` | 最近一次响应里最新已收盘 K 线的年龄；休市时可能很大，不能拿它当发布延迟 |
| `websocket.observed_delay_s` | 本机收到消息的 UTC 时间减去服务端事件时间；含上游处理、传输、本机时钟差和时间戳取整 |
| `websocket.arrival_intervals_s` | 相邻有效黄金价格消息的本机到达间隔，使用单调时钟 |
| `websocket.negative_delay_count` | 负时间差的样本数，原值保留，提示检查时钟及时间戳精度 |

K 线时间戳按**起始时间**解释，收盘时间为起点加 60 秒。REST 的首次收盘后可见仍不保证最终定稿；接口没有 final 标志，之后发生 OHLC 变化会另计 `closed_bar_revisions`。该测量含轮询等待与网络耗时，不能当成精确的服务端发布时间。

请先启用 Windows 自动时间同步。WebSocket 通常使用秒级 Unix 时间戳，即使脚本显示小数秒，也不能据此宣称测到了精确的毫秒级网络延迟。脚本兼容秒、毫秒、微秒和纳秒时间戳，不把负时间差强制归零。HTTP `Date` 仅提供粗略偏差诊断（受秒级精度、网络与缓存影响），不自动修正样本。

## 如何判断

- REST 返回品种 `XAU/USD`、周期 `1min` 且 OHLC 合法，才会标记 K 线可读。只读取到旧历史但没观察到新收盘 K 线时，更新延迟仍无结论。
- WebSocket 连接成功不等于订阅成功；脚本检查目标品种的 `subscribe-status`，并且必须实际收到黄金 `price` 消息才标记推送可用。订阅成功但零消息可能是休市，不自动归因于权限。
- REST 的 HTTP 200 也可能包含 API 错误，脚本会检查 JSON 的错误码。401/403 或订阅拒绝应看报告中的实际理由；429 是额度问题。
- 握手返回 HTTP 200、代理错误、TLS 错误属于连接问题，不能据此判定免费 Key 没有 WebSocket 权限。`--no-proxy` 只关闭代理，保留 TLS 证书校验。
- 脚本不查询计费套餐；请自己在 Twelve Data 控制台确认用的是 Basic 免费账户。

官方参考：[1 分钟历史接口](https://support.twelvedata.com/en/articles/5656039-how-to-get-historical-prices)、[实时价格流](https://support.twelvedata.com/en/articles/5620516-how-to-stream-the-data)、[延迟说明](https://support.twelvedata.com/en/articles/5203307-data-delays)、[额度规则](https://support.twelvedata.com/en/articles/5615854-credits)。

本地验证（不访问 Twelve Data、不消耗额度）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_twelvedata_probe.py -v
```
