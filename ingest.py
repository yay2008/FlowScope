# -*- coding: utf-8 -*-
"""tqSdk 采集: 订阅 30s K线 + tick, 增量计算 Volume Suite bars, 广播并落盘。

设计要点:
- wait_update 循环独占一个守护线程; FastAPI 线程经命令队列请求新合约订阅
- 闭市时初始数据回填不触发 wait_update 返回, 循环用 1s deadline 轮询兼容
- 每根走完且有 tick 覆盖的 bar 的 buy/sell 追加写入 data/{symbol}_30s.csv;
  tick 历史只有约 83 分钟, 更早的 buy/sell 靠 CSV 随运行时间累积
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import threading
import time

import pandas as pd
from dotenv import load_dotenv
from tqsdk import TqApi, TqAuth

from indicator import CFG, build_bars, bars_to_records, build_footprint, finalize_bars

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_TICKS = 10000     # get_tick_serial 单次上限 10000 条 ≈ 83 分钟
MAX_KLINES = 2000     # 2000 根 30s K线 ≈ 2.5 个交易日
SNAPSHOT_BARS = 800   # 推送给前端的最近 bar 数
MAX_FEEDS = 32
SYMBOL_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)?\Z")


def _csv_key(symbol: str) -> str:
    return symbol.replace("@", "_").replace(".", "_")


def validate_symbol(symbol: str) -> str:
    """校验并规范化 TqSdk 合约标识，避免非法订阅和路径穿越。"""
    value = symbol.strip()
    if not value or len(value) > 64 or SYMBOL_RE.fullmatch(value) is None:
        raise ValueError("symbol 格式无效")
    return value


class Feed:
    """单个合约的订阅、快照与 CSV 落盘"""

    def __init__(self, symbol: str):
        self.symbol = validate_symbol(symbol)
        self.klines = None
        self.ticks = None
        self.ready = threading.Event()     # 首个快照就绪
        self.snapshots: dict[int, dict] = {}
        self.footprint: dict | None = None
        self.error: str | None = None
        self._last_tick_dt = None
        self._state_lock = threading.RLock()
        self._csv_values: dict[int, tuple[float, float]] = {}
        self._csv_last_time = 0
        self.csv_path = os.path.join(DATA_DIR, f"{_csv_key(self.symbol)}_30s.csv")
        self._load_csv_cache()

    def subscribe(self, api: TqApi):
        self.klines = api.get_kline_serial(self.symbol, 30, data_length=MAX_KLINES)
        self.ticks = api.get_tick_serial(self.symbol, data_length=MAX_TICKS)
        with self._state_lock:
            self._last_tick_dt = None
            self.snapshots.clear()
            self.footprint = None
            self.ready.clear()
            self.error = None

    def _load_csv_cache(self):
        if not os.path.exists(self.csv_path):
            return
        try:
            csv = pd.read_csv(self.csv_path)
            if {"time", "buy", "sell"}.issubset(csv.columns):
                csv = csv[["time", "buy", "sell"]].dropna()
                csv["time"] = pd.to_numeric(csv["time"], errors="coerce")
                csv["buy"] = pd.to_numeric(csv["buy"], errors="coerce")
                csv["sell"] = pd.to_numeric(csv["sell"], errors="coerce")
                csv = csv.dropna().astype({"time": "int64"})
                duplicate_rows = len(csv) != csv["time"].nunique()
                csv = csv.drop_duplicates("time", keep="last").sort_values("time")
                for row in csv[["time", "buy", "sell"]].dropna().itertuples(index=False):
                    self._csv_values[int(row.time)] = (float(row.buy), float(row.sell))
                if self._csv_values:
                    self._csv_last_time = max(self._csv_values)
                if duplicate_rows:
                    temp_path = f"{self.csv_path}.tmp"
                    csv.to_csv(temp_path, index=False)
                    os.replace(temp_path, self.csv_path)
                    print(f"[ingest] 已压缩重复 CSV 记录: {self.csv_path}", flush=True)
        except Exception:
            print(f"[ingest] 读取 CSV 失败: {self.csv_path}", flush=True)

    def snapshot_for(self, ltf: int) -> dict | None:
        with self._state_lock:
            return self.snapshots.get(ltf)

    def footprint_snapshot(self) -> dict | None:
        with self._state_lock:
            return self.footprint

    def _append_csv(self, bars: pd.DataFrame):
        done = bars.iloc[:-1]                 # 最后一根 bar 未走完, 不落盘
        done = done[done["buy"].notna() & done["sell"].notna()]
        done = done[(done["time"] > self._csv_last_time) &
                    ~done["time"].isin(self._csv_values)]
        if done.empty:
            return
        done[["time", "buy", "sell"]].to_csv(
            self.csv_path, mode="a", header=not os.path.exists(self.csv_path), index=False)
        for row in done[["time", "buy", "sell"]].itertuples(index=False):
            timestamp = int(row.time)
            self._csv_values[timestamp] = (float(row.buy), float(row.sell))
        self._csv_last_time = max(self._csv_last_time, max(self._csv_values))

    def recompute(self, broadcast):
        """在 ingest 线程内调用: 重算 bars -> 更新快照 -> 广播最新 bar -> 落盘"""
        tick_bars = build_bars(self.klines, self.ticks, 0)
        if tick_bars.empty:
            return
        if self._csv_values:
            lack = tick_bars["buy"].isna() & tick_bars["time"].map(self._csv_values.__contains__)
            if lack.any():
                tick_bars.loc[lack, "buy"] = tick_bars.loc[lack, "time"].map(
                    lambda timestamp: self._csv_values[int(timestamp)][0])
                tick_bars.loc[lack, "sell"] = tick_bars.loc[lack, "time"].map(
                    lambda timestamp: self._csv_values[int(timestamp)][1])
                tick_bars = finalize_bars(tick_bars)
        self._append_csv(tick_bars)

        snapshots = {}
        messages = []
        for ltf in CFG["ltfOptions"]:
            bars = tick_bars if ltf == 0 else build_bars(self.klines, self.ticks, ltf)
            if bars.empty:
                continue
            recs = bars_to_records(bars.tail(SNAPSHOT_BARS))
            snapshots[ltf] = {"symbol": self.symbol, "cfg": CFG, "ltf": ltf, "bars": recs}
            messages.append({"type": "bar", "symbol": self.symbol, "ltf": ltf, "bar": recs[-1]})
        fp = build_footprint(self.klines, self.ticks)
        with self._state_lock:
            self.snapshots.update(snapshots)
            self.footprint = {"symbol": self.symbol, **fp}
            self.ready.set()
        for message in messages:
            broadcast(message)
        if fp["bars"]:
            broadcast({"type": "footprint", "symbol": self.symbol, "bar": fp["bars"][-1]})


class FeedManager:
    """管理全部订阅; wait_update 循环跑在独立守护线程"""

    def __init__(self):
        self.feeds: dict[str, Feed] = {}
        self.cmd_q: queue.Queue[str] = queue.Queue()
        self.clients: dict[asyncio.Queue, tuple[str, int]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = "stopped"
        self.last_error: str | None = None
        self.failed_feeds: dict[str, str] = {}

    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="tqsdk-ingest")
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._set_status("stopped")

    def add_client(self, queue_: asyncio.Queue, symbol: str, ltf: int):
        self.clients[queue_] = (symbol, ltf)

    def remove_client(self, queue_: asyncio.Queue):
        self.clients.pop(queue_, None)

    def ensure(self, symbol: str) -> Feed:
        value = validate_symbol(symbol)
        with self._lock:
            if value in self.failed_feeds:
                raise ValueError(self.failed_feeds[value])
            feed = self.feeds.get(value)
            if feed is None:
                if len(self.feeds) >= MAX_FEEDS:
                    raise ValueError("订阅合约数量已达上限")
                feed = Feed(value)
                self.feeds[value] = feed
                self.cmd_q.put(value)
            return feed

    def fail_feed(self, feed: Feed, message: str):
        with feed._state_lock:
            feed.error = message
        with self._lock:
            if self.feeds.get(feed.symbol) is feed:
                self.feeds.pop(feed.symbol, None)
            self.failed_feeds[feed.symbol] = message

    def status_snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "lastError": self.last_error,
                    "feeds": sorted(self.feeds), "failedFeeds": dict(self.failed_feeds)}

    def _set_status(self, status: str, error: str | None = None):
        with self._lock:
            self.status = status
            self.last_error = error

    def broadcast(self, msg: dict):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict):
        for q, subscription in list(self.clients.items()):
            if msg.get("type") == "footprint":
                # 足迹口径固定 tick 级, 与客户端 ltf 无关, 只按 symbol 匹配
                if msg.get("symbol") != subscription[0]:
                    continue
            elif (msg.get("symbol"), msg.get("ltf")) != subscription:
                continue
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.clients.pop(q, None)

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            api = None
            try:
                username = os.getenv("TQ_USER")
                password = os.getenv("TQ_PASS")
                if not username or not password:
                    raise RuntimeError("未设置 TQ_USER/TQ_PASS")
                self._set_status("connecting")
                api = TqApi(auth=TqAuth(username, password))
                self._set_status("connected")
                backoff = 1
                self._run_api(api)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._set_status("error", message)
                print(f"[ingest] TqSdk 连接失败: {message}", flush=True)
            finally:
                if api is not None:
                    try:
                        api.close()
                    except Exception:
                        pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _run_api(self, api: TqApi):
        subscribed: set[str] = set()
        with self._lock:
            feeds = list(self.feeds.values())
        for feed in feeds:
            try:
                feed.subscribe(api)
                subscribed.add(feed.symbol)
            except Exception as exc:
                message = f"合约 {feed.symbol} 订阅失败: {exc}"
                self.fail_feed(feed, message)
                print(f"[ingest] {message}", flush=True)

        while not self._stop.is_set():
            # 处理新订阅命令(在 ingest 线程内调用 get_* 保证线程安全)
            try:
                while True:
                    symbol = self.cmd_q.get_nowait()
                    if symbol in subscribed:
                        continue
                    with self._lock:
                        feed = self.feeds.get(symbol)
                    if feed is None:
                        continue
                    try:
                        feed.subscribe(api)
                        subscribed.add(symbol)
                    except Exception as exc:
                        message = f"合约 {symbol} 订阅失败: {exc}"
                        self.fail_feed(feed, message)
                        print(f"[ingest] {message}", flush=True)
            except queue.Empty:
                pass
            api.wait_update(deadline=time.time() + 1)
            with self._lock:
                feeds = list(self.feeds.values())
            for feed in feeds:
                if feed.ticks is None or len(feed.ticks) == 0:
                    continue
                last_dt = feed.ticks.iloc[-1]["datetime"]
                with feed._state_lock:
                    needs_recompute = (not feed.snapshots or
                                       (pd.notna(last_dt) and last_dt != feed._last_tick_dt))
                    if needs_recompute:
                        feed._last_tick_dt = last_dt
                if needs_recompute:
                    try:
                        feed.recompute(self.broadcast)
                    except Exception as exc:
                        print(f"[ingest] recompute {feed.symbol} 出错: {exc}", flush=True)
