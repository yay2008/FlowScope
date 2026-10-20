r"""验证 Twelve Data 免费 Key 的 XAU/USD 分钟线和价格流，保存延迟观测报告。

运行: .\.venv\Scripts\python.exe docs\twelvedata_probe.py
Key: 项目 .env 中的 TWELVEDATA_API_KEY，或启动时隐藏输入。
详细口径、参数和排错见同目录 twelvedata_probe.md。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
REST_URL = "https://api.twelvedata.com/time_series"
WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
HEARTBEAT_SECONDS = 10
SYMBOL = "XAU/USD"


def utc_text(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp,
                                  timezone.utc).isoformat(timespec="milliseconds")


def redact(value, key: str):
    """错误消息可能带认证 URL；落盘、控制台输出都从这里经过。"""
    if isinstance(value, str):
        for secret in sorted({key, quote(key, safe=""), quote_plus(key)}, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<REDACTED>")
        return value
    if isinstance(value, dict):
        return {redact(str(k), key): redact(v, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    return value


def distribution(values: list[float]) -> dict:
    values = sorted(values)
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}

    def percentile(q):
        index = (len(values) - 1) * q
        lo, hi = math.floor(index), math.ceil(index)
        return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 6)

    return {"count": len(values), "min": round(values[0], 6), "p50": percentile(.5),
            "p95": percentile(.95), "max": round(values[-1], 6)}


def normalized_symbol(value) -> str:
    return str(value).upper().replace("/", "").replace("_", "")


class ApiFailure(ValueError):
    def __init__(self, code, message):
        self.code = int(code or 0)
        super().__init__(f"API {self.code}: {message}")


def parse_candles(payload: dict, http_status: int = 200) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("REST 响应必须是 JSON 对象")
    if http_status != 200 or payload.get("status") == "error" or "code" in payload:
        raise ApiFailure(payload.get("code", http_status), payload.get("message", "请求失败"))
    meta = payload.get("meta", {})
    if normalized_symbol(meta.get("symbol")) != normalized_symbol(SYMBOL):
        raise ValueError("返回品种不是 XAU/USD")
    if meta.get("interval") != "1min":
        raise ValueError("返回周期不是 1min")
    rows = payload.get("values")
    if not isinstance(rows, list):
        raise ValueError("缺少 K 线数组 values")
    bars = {}
    for row in rows:
        # 请求显式指定 timezone=UTC；datetime 是 K 线起点，而不是收盘时间。
        dt = datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))
        start = dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
        prices = {field: float(row[field]) for field in ("open", "high", "low", "close")}
        if start % 60 or not all(math.isfinite(p) and p > 0 for p in prices.values()):
            raise ValueError("K 线时间未按整分钟对齐，或 OHLC 值无效")
        if not prices["low"] <= min(prices["open"], prices["close"]) <= max(
                prices["open"], prices["close"]) <= prices["high"]:
            raise ValueError("OHLC 高低价关系不合法")
        bar = {"start_epoch": start, "close_epoch": start + 60, **prices}
        if start in bars and bars[start] != bar:
            raise ValueError("同一分钟出现互相冲突的 K 线")
        bars[start] = bar
    return sorted(bars.values(), key=lambda bar: bar["start_epoch"])


class CandleObservations:
    def __init__(self):
        self.baseline_received = None
        self.previous_request_started = None
        self.closed = {}
        self.observations = []
        self.revisions = 0

    def observe(self, bars: list[dict], started: float, received: float) -> list[dict]:
        new = []
        for bar in bars:
            close = bar["close_epoch"]
            if close > received:  # 当前仍在形成的 K 线不能作为已收盘样本。
                continue
            previous = self.closed.get(close)
            if previous is not None and previous != bar:
                self.revisions += 1
            if previous is None and self.baseline_received is not None and close > self.baseline_received:
                # 只统计首个成功快照之后才收盘的 K 线，历史回填不算发布延迟。
                observation = {
                    "bar_start_utc": utc_text(bar["start_epoch"]),
                    "bar_close_utc": utc_text(close),
                    "first_observed_after_close_utc": utc_text(received),
                    "observed_delay_s": received - close,
                    # 前次请求开始到此次收到响应之间，是轮询可观测区间，不是 SLA。
                    "poll_window_lower_s": max(0., self.previous_request_started - close),
                    "poll_window_upper_s": received - close,
                }
                new.append(observation)
                self.observations.append(observation)
            self.closed[close] = bar
        if self.baseline_received is None:
            self.baseline_received = received
        self.previous_request_started = started
        return new


def subscription_result(event: dict) -> bool | None:
    """混合订阅结果必须核对目标品种，不能仅用顶层 status=ok/warning。"""
    def matches(item):
        symbol = item.get("symbol") if isinstance(item, dict) else item
        return normalized_symbol(symbol) == normalized_symbol(SYMBOL)

    if any(matches(item) for item in (event.get("fails") or [])):
        return False
    if any(matches(item) for item in (event.get("success") or [])):
        return True
    if str(event.get("status", "")).lower() in {"error", "failed"}:
        return False
    return None


def price_sample(event: dict, received: float) -> dict | None:
    if event.get("event") != "price" or normalized_symbol(event.get("symbol")) != normalized_symbol(SYMBOL):
        return None
    timestamp, price = float(event["timestamp"]), float(event["price"])
    if not math.isfinite(timestamp) or not math.isfinite(price) or timestamp <= 0 or price <= 0:
        raise ValueError("无效的推送时间戳或价格")
    scale = 1e9 if timestamp >= 1e17 else 1e6 if timestamp >= 1e14 else 1e3 if timestamp >= 1e11 else 1
    unit = {1: "seconds", 1e3: "milliseconds", 1e6: "microseconds", 1e9: "nanoseconds"}[scale]
    timestamp /= scale
    return {"provider_timestamp": timestamp, "timestamp_unit": unit,
            "provider_utc": utc_text(timestamp), "received_utc": utc_text(received),
            "price": price, "observed_delay_s": received - timestamp}


@dataclass
class Config:
    duration: float = 300
    poll_interval: float = 15
    timeout: float = 15
    max_rest_requests: int = 40
    no_proxy: bool = False
    output: Path | None = None


class Recorder:
    def __init__(self, directory: Path, key: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.key = directory, key
        self.stream = (directory / "events.jsonl").open("x", encoding="utf-8")

    def say(self, message):
        print(redact(f"[{utc_text()}] {message}", self.key), flush=True)

    def event(self, kind, **fields):
        self.stream.write(json.dumps(redact({"kind": kind, **fields}, self.key),
                                     ensure_ascii=False, allow_nan=False) + "\n")
        self.stream.flush()

    def finish(self, report):
        self.stream.close()
        with (self.directory / "report.json").open("w", encoding="utf-8") as handle:
            json.dump(redact(report, self.key), handle, ensure_ascii=False, indent=2, allow_nan=False)


def fetch_candles(key: str, timeout: float, no_proxy: bool, url: str = REST_URL) -> dict:
    query = urlencode({"symbol": SYMBOL, "interval": "1min", "outputsize": 10,
                       "timezone": "UTC", "apikey": key})
    request = Request(f"{url}?{query}", headers={"Accept": "application/json",
                                               "User-Agent": "FlowScope-TwelveData-Probe/1.0"})
    opener = build_opener(ProxyHandler({})) if no_proxy else build_opener()
    started, monotonic = time.time(), time.perf_counter()
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        response = error  # HTTP 4xx 的 JSON 错误也要保留，不误判为网络故障。
    with response:
        content = response.read()
        received, elapsed = time.time(), time.perf_counter() - monotonic
        headers = {name: response.headers.get(name) for name in (
            "Date", "api-credits-used", "api-credits-left", "Retry-After")}
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            payload = {"status": "error", "code": response.code, "message": "响应不是 JSON"}
        return {"payload": payload, "status": response.code, "headers": headers,
                "started": started, "received": received, "rtt_ms": elapsed * 1000}


async def rest_probe(config, key, deadline, recorder, state, tracker, rest_url):
    while time.perf_counter() < deadline and state["requests"] < config.max_rest_requests:
        state["requests"] += 1
        request_start = time.perf_counter()
        try:
            result = await asyncio.to_thread(fetch_candles, key,
                min(config.timeout, max(.1, deadline - request_start)), config.no_proxy, rest_url)
            state["rtt_ms"].append(result["rtt_ms"])
            state["last_credit_headers"] = result["headers"]
            recorder.event("rest_response", **result)
            if result["headers"].get("Date"):
                try:
                    server_time = parsedate_to_datetime(result["headers"]["Date"]).timestamp()
                    state["http_date_offset_s"].append(server_time - (result["started"] + result["received"]) / 2)
                except (TypeError, ValueError, OverflowError):
                    pass
            bars = parse_candles(result["payload"], result["status"])
            state["successful_responses"] += 1
            state["valid_bars_received"] += len(bars)
            if bars:  # 空数组不能建立历史基线，也不能证明已有分钟线可读。
                new = tracker.observe(bars, result["started"], result["received"])
                closed = [bar for bar in bars if bar["close_epoch"] <= result["received"]]
                state["latest_closed_bar_age_s"] = result["received"] - closed[-1]["close_epoch"] if closed else None
                recorder.say(f"REST #{state['requests']}: {len(bars)} 根有效 1m K线，"
                             f"HTTP 耗时 {result['rtt_ms']:.0f} ms，新增收盘观测 {len(new)} 根")
                for observation in new:
                    recorder.event("candle_observation", **observation)
                    recorder.say(f"  K线 {observation['bar_start_utc']} 收盘后首次观测 "
                                 f"{observation['observed_delay_s']:.2f} s，轮询区间 "
                                 f"[{observation['poll_window_lower_s']:.2f}, "
                                 f"{observation['poll_window_upper_s']:.2f}] s")
            else:
                recorder.say("REST 返回空 K 线数组，尚不能确认可用")
        except Exception as error:
            message = str(error)
            state["errors"].append(message)
            recorder.event("rest_error", at=utc_text(), message=message)
            recorder.say(f"REST 错误: {message}")
            if isinstance(error, ApiFailure) and error.code in {400, 401, 403, 404, 429}:
                state["stop_reason"] = f"api_{error.code}"
                break
        # 不追赶延迟的轮询，避免瞬时补发把免费分钟额度耗尽。
        await asyncio.sleep(max(0., min(config.poll_interval - (time.perf_counter() - request_start),
                                       deadline - time.perf_counter())))
    if state["requests"] >= config.max_rest_requests and state["stop_reason"] is None:
        state["stop_reason"] = "request_budget_reached"


async def websocket_probe(config, key, deadline, recorder, state, ws_url):
    from websockets.asyncio.client import connect

    options = {"open_timeout": min(config.timeout, max(.1, deadline - time.perf_counter())),
               "close_timeout": 2, "ping_interval": 20, "ping_timeout": 20}
    if config.no_proxy and "proxy" in inspect.signature(connect).parameters:
        options["proxy"] = None
    try:
        connect_start = time.perf_counter()
        async with connect(f"{ws_url}?{urlencode({'apikey': key})}", **options) as socket:
            state["connected"] = True
            state["handshake_ms"] = (time.perf_counter() - connect_start) * 1000
            recorder.say(f"WebSocket 已连接，握手 {state['handshake_ms']:.0f} ms；正在订阅 {SYMBOL}")
            await socket.send(json.dumps({"action": "subscribe", "params": {"symbols": SYMBOL}}))
            next_heartbeat = time.perf_counter() + HEARTBEAT_SECONDS
            last_price_arrival = last_print = None
            last_provider_timestamp = None
            while time.perf_counter() < deadline:
                now = time.perf_counter()
                if now >= next_heartbeat:
                    await socket.send('{"action":"heartbeat"}')
                    next_heartbeat = now + HEARTBEAT_SECONDS
                wait = max(.001, min(next_heartbeat, deadline) - time.perf_counter())
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=wait)
                except asyncio.TimeoutError:
                    continue
                received, arrival = time.time(), time.perf_counter()
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("WebSocket 消息不是 JSON 对象")
                    if event.get("event") == "subscribe-status":
                        verdict = subscription_result(event)
                        state["subscription_status"] = event
                        if verdict is not None:
                            state["subscribed"] = verdict
                        recorder.event("ws_subscription", received_utc=utc_text(received), response=event)
                        recorder.say(f"WebSocket 订阅结果: {json.dumps(event, ensure_ascii=False)}")
                        if verdict is False:
                            state["stop_reason"] = "subscription_rejected"
                            return
                    if event.get("event") == "error" or event.get("status") == "error":
                        state["stop_reason"] = "server_error"
                        raise ApiFailure(event.get("code", 0), event.get("message", event))
                    sample = price_sample(event, received)
                    if sample is None:
                        continue
                    state["subscribed"] = True  # 目标品种的有效 price 也能证明订阅成功。
                    state["price_events"] += 1
                    state["delays_s"].append(sample["observed_delay_s"])
                    state["timestamp_units"].add(sample["timestamp_unit"])
                    if last_price_arrival is not None:
                        state["arrival_intervals_s"].append(arrival - last_price_arrival)
                    if last_provider_timestamp is not None and sample["provider_timestamp"] < last_provider_timestamp:
                        state["out_of_order_events"] += 1
                    last_price_arrival, last_provider_timestamp = arrival, sample["provider_timestamp"]
                    recorder.event("ws_price", **sample)
                    if last_print is None or arrival - last_print >= 10:
                        recorder.say(f"WS 价格 {sample['price']:.5f}，累计 {state['price_events']} 条，"
                                     f"本条可观察延迟 {sample['observed_delay_s']:.3f} s")
                        last_print = arrival
                except ApiFailure:
                    raise
                except (ValueError, KeyError, TypeError, OverflowError, OSError) as error:
                    state["invalid_events"] += 1
                    recorder.event("ws_invalid_event", at=utc_text(), message=str(error))
    except Exception as error:
        state["errors"].append(str(error))
        state["stop_reason"] = state["stop_reason"] or "connection_error_or_closed"
        recorder.event("ws_error", at=utc_text(), message=str(error))
        recorder.say(f"WebSocket 错误: {error}")


async def run_probe(config: Config, key: str, *, rest_url=REST_URL, ws_url=WS_URL) -> dict:
    start_wall, start_monotonic = time.time(), time.perf_counter()
    output = config.output or ROOT / "data" / "twelvedata_probe" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    recorder = Recorder(output, key)
    tracker = CandleObservations()
    rest = {"requests": 0, "successful_responses": 0, "valid_bars_received": 0, "rtt_ms": [],
            "http_date_offset_s": [], "latest_closed_bar_age_s": None, "last_credit_headers": {},
            "stop_reason": None, "errors": []}
    ws = {"connected": False, "subscribed": None, "subscription_status": None, "handshake_ms": None,
          "price_events": 0, "invalid_events": 0, "out_of_order_events": 0, "delays_s": [],
          "arrival_intervals_s": [], "timestamp_units": set(), "stop_reason": None, "errors": []}
    recorder.say(f"测试 {SYMBOL}: {config.duration:g} 秒，REST 间隔 {config.poll_interval:g} 秒，"
                 f"最多 {min(config.max_rest_requests, math.ceil(config.duration / config.poll_interval))} 次 REST 请求")
    recorder.say("所有时间使用 UTC。请保持系统时间同步；秒级推送时间戳不能测出精确的毫秒网络延迟。")
    deadline = start_monotonic + config.duration
    tasks = [asyncio.create_task(rest_probe(config, key, deadline, recorder, rest, tracker, rest_url)),
             asyncio.create_task(websocket_probe(config, key, deadline, recorder, ws, ws_url))]
    interrupted = False
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        interrupted = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        rest["availability"] = "available" if rest["valid_bars_received"] else "not_confirmed"
        rest["rtt_ms"] = distribution(rest["rtt_ms"])
        rest["http_date_offset_s"] = distribution(rest["http_date_offset_s"])
        rest["new_closed_bar_observations"] = tracker.observations
        rest["close_to_first_observation_s"] = distribution([v["observed_delay_s"] for v in tracker.observations])
        rest["closed_bar_revisions"] = tracker.revisions
        ws["availability"] = "available" if ws["price_events"] else "not_confirmed"
        ws["negative_delay_count"] = sum(value < 0 for value in ws["delays_s"])
        ws["observed_delay_s"] = distribution(ws.pop("delays_s"))
        ws["arrival_intervals_s"] = distribution(ws["arrival_intervals_s"])
        ws["timestamp_units"] = sorted(ws["timestamp_units"])
        elapsed = time.perf_counter() - start_monotonic
        report = {"symbol": SYMBOL, "interval": "1min", "started_utc": utc_text(start_wall),
                  "ended_utc": utc_text(), "elapsed_s": elapsed, "interrupted": interrupted,
                  "wall_clock_drift_s": (time.time() - start_wall) - elapsed,
                  "poll_interval_s": config.poll_interval, "rest": rest, "websocket": ws,
                  "notes": ["REST 只统计基线快照之后收盘的 K 线；首次收盘后可见不保证最终定稿，修订另计。",
                            "REST 观测延迟含轮询等待与网络耗时；latest_closed_bar_age_s 是新鲜度，不是发布延迟。",
                            "WS 延迟=本机接收时间-服务端事件时间，包含时钟误差、时间戳取整及数据商处理时间。",
                            "HTTP Date 偏差仅为粗略诊断（可能受缓存影响），未用于校准时钟。",
                            "订阅成功但未收到价格时，可能休市或报价静止，不能直接判为无权限。",
                            "报告验证当前 Key 的访问情况，不验证账号计费套餐是否为 Basic。"]}
        report = redact(report, key)
        recorder.finish(report)
        recorder.say(f"完成：REST={rest['availability']}，WebSocket={ws['availability']}；"
                     f"新收盘 K线样本 {len(tracker.observations)}，推送 {ws['price_events']} 条")
        recorder.say(f"REST 收盘后首次观测秒数: {rest['close_to_first_observation_s']}")
        recorder.say(f"WS 可观察延迟秒数: {ws['observed_delay_s']}")
        if not tracker.observations or not ws["price_events"]:
            recorder.say("部分更新延迟没有样本；请查看错误/订阅结果，并在开市时延长测试。")
        recorder.say(f"报告: {output.resolve() / 'report.json'}")
    return report


def main(argv=None):
    # Windows 管道默认可能使用本地代码页；日志与 JSON 文件统一 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="测试 XAU/USD 1m K线与 WebSocket；Key 从 .env/环境变量读取或隐藏输入。")
    parser.add_argument("--duration", type=float, default=300, help="测试秒数，默认 300")
    parser.add_argument("--poll-interval", type=float, default=15, help="REST 请求间隔秒数，至少 10，默认 15")
    parser.add_argument("--timeout", type=float, default=15, help="网络超时秒数，默认 15")
    parser.add_argument("--max-rest-requests", type=int, default=40, help="本次 REST 次数上限，默认 40")
    parser.add_argument("--no-proxy", action="store_true", help="两路均不使用系统/环境代理")
    parser.add_argument("--output", type=Path, help="结果目录，必须尚无 events.jsonl；默认 data/twelvedata_probe/时间戳")
    args = parser.parse_args(argv)
    if not all(math.isfinite(v) and v > 0 for v in (args.duration, args.poll_interval, args.timeout)):
        parser.error("duration、poll-interval 和 timeout 必须是正的有限数值")
    if args.poll_interval < 10 or not 1 <= args.max_rest_requests <= 800:
        parser.error("poll-interval 至少 10 秒；max-rest-requests 必须在 1～800 之间")
    try:
        from dotenv import load_dotenv
        from websockets.asyncio.client import connect  # noqa: F401
    except ImportError:
        parser.error("缺少依赖；请使用项目 .venv，或安装 python-dotenv 和 websockets>=13,<18")
    load_dotenv(ROOT / ".env", override=False)
    key = os.getenv("TWELVEDATA_API_KEY", "").strip()
    if not key:
        if not sys.stdin.isatty():
            parser.error("请在 .env 或环境变量中设置 TWELVEDATA_API_KEY")
        key = getpass.getpass("请输入 Twelve Data API Key（不回显、不写入文件）: ").strip()
    if not key or key in {"YOUR_API_KEY", "your_api_key"}:
        parser.error("请输入有效 Key；不要保留示例占位符")
    try:
        report = asyncio.run(run_probe(Config(**vars(args)), key))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(redact(f"测试无法完成: {error}", key), file=sys.stderr)
        return 1
    if report["interrupted"]:
        return 130
    return 0 if all(report[part]["availability"] == "available" for part in ("rest", "websocket")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
