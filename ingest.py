# -*- coding: utf-8 -*-
"""tqSdk 采集: 订阅 30s K线 + tick, 增量计算 Volume Suite bars, 广播并落盘。

设计要点:
- wait_update 循环独占一个守护线程; FastAPI 线程经命令队列请求新合约订阅
- 闭市时初始数据回填不触发 wait_update 返回, 循环用 1s deadline 轮询兼容
- 每根走完且覆盖完整的 bar 写入按粒度隔离的 v2 CSV；旧 CSV 仅作 legacy 回填。
- tick 窗口最多 10000 条，更早的 buy/sell 靠 CSV 随运行时间累积。
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

from indicator import CFG, build_bars, build_bars_from_ltf, bars_to_records
from history_store import HistoryStore
from tick_analytics import TickAnalytics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_TICKS = 10000     # 条数窗口；按 500ms 一条估算约 83 分钟
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
    """单合约快照；只有 ingest 线程读写 SDK 数据和历史文件。"""

    def __init__(self, symbol: str):
        self.symbol = validate_symbol(symbol)
        self.klines = self.ticks = self.quote = None
        self.ready = threading.Event()
        self.snapshots = {}
        self.footprint = None
        self.error = None
        self._state_lock = threading.RLock()
        self._requested = {0: float("inf")}
        self._fp_until = 0.0
        self._demand_version = 0
        self._computed_version = -1
        self.revision = 0
        self.stores = {}
        self.analytics = TickAnalytics()
        self.lower_klines = {}

    def subscribe(self, api: TqApi):
        self.quote = api.get_quote(self.symbol)
        self.klines = api.get_kline_serial(self.symbol, 30, data_length=MAX_KLINES)
        self.ticks = api.get_tick_serial(self.symbol, data_length=MAX_TICKS)
        self.analytics = TickAnalytics()
        self.lower_klines = {30: self.klines}
        with self._state_lock:
            self.snapshots.clear()
            self.footprint = None
            self.ready.clear()
            self.error = None
            self._computed_version = -1

    def ensure_ltf_subscriptions(self, api):
        """只在 ingest 线程订阅当前被请求的小周期 K 线。"""
        with self._state_lock:
            wanted = [ltf for ltf, expires in self._requested.items()
                      if ltf > 0 and expires >= time.monotonic()]
        for ltf in wanted:
            if ltf not in self.lower_klines:
                self.lower_klines[ltf] = api.get_kline_serial(
                    self.symbol, ltf, data_length=min(10000, MAX_KLINES * 30 // ltf + 1))

    def request(self, ltf=None, footprint=False):
        now = time.monotonic()
        with self._state_lock:
            if ltf is not None:
                if ltf not in CFG["ltfOptions"]:
                    ltf = 0
                if self._requested.get(ltf, 0) < now:
                    self._demand_version += 1
                    self.snapshots.pop(ltf, None)
                self._requested[ltf] = float("inf") if ltf == 0 else now + 60
            if footprint:
                if self._fp_until < now:
                    self._demand_version += 1
                    self.footprint = None
                self._fp_until = now + 60

    def snapshot_for(self, ltf):
        self.request(ltf=ltf)
        with self._state_lock:
            return self.snapshots.get(ltf)

    def footprint_snapshot(self):
        self.request(footprint=True)
        with self._state_lock:
            return self.footprint

    def _store(self, ltf):
        if ltf not in self.stores:
            key = _csv_key(self.symbol)
            source = "" if ltf == 0 else "kline_"
            self.stores[ltf] = HistoryStore(
                os.path.join(DATA_DIR, f"{key}_30s_{source}ltf{ltf}_v2.csv"),
                os.path.join(DATA_DIR, f"{key}_30s.csv") if ltf == 0 else None)
        return self.stores[ltf]

    def recompute(self, broadcast):
        with self._state_lock:
            now = time.monotonic()
            ltfs = [ltf for ltf, expires in self._requested.items() if expires >= now]
            want_fp = self._fp_until >= now
            demand_version = self._demand_version
            previous = dict(self.snapshots)
            previous_fp = self.footprint
        self.analytics.update(self.ticks)
        self.analytics.retain([0], want_fp)
        classified = self.analytics.frame
        valid_klines = self.klines.dropna(subset=["datetime", "close"])
        if valid_klines.empty:
            return
        first_bar_ns = int(valid_klines.datetime.iloc[0])
        snapshots = {}
        messages = []
        revision = self.revision + 1
        tick_bars = None
        for ltf in ltfs:
            if ltf == 0:
                aggregates = self.analytics.aggregate(0, first_bar_ns)
                bars = build_bars(self.klines, self.ticks, 0, classified=classified,
                                  aggregates=aggregates, first_tick_ns=self.analytics.first_tick_ns)
            else:
                lower = self.lower_klines.get(ltf)
                if lower is None or not (lower.datetime > 0).any():
                    continue
                bars = build_bars_from_ltf(self.klines, lower, ltf)
            if bars.empty:
                continue
            if ltf == 0:
                tick_bars = bars
            store = self._store(ltf)
            bars = store.merge(bars)
            store.save_completed(bars)
            bars = store.with_cvd(bars)
            recs = bars_to_records(bars.tail(SNAPSHOT_BARS))
            snapshots[ltf] = {"symbol": self.symbol, "cfg": CFG, "ltf": ltf,
                              "source": "tick" if ltf == 0 else "kline",
                              "revision": revision, "cvdBase": store.base,
                              "bars": recs}
            old = {b["time"]: b for b in previous.get(ltf, {}).get("bars", [])}
            changed = [bar for bar in recs if old.get(bar["time"]) != bar]
            if changed:
                messages.append({"type": "bars", "symbol": self.symbol, "ltf": ltf,
                                 "revision": revision, "bars": changed})
        fp = None
        if want_fp and tick_bars is not None:
            coverage = dict(zip(tick_bars.time, tick_bars.coverage))
            fp = self.analytics.footprint(self.klines, getattr(self.quote, "price_tick", None), coverage)
            fp = {"symbol": self.symbol, "revision": revision, **fp}
            # 足迹更新必须带步长；步长可能在首次加载后才就绪。
            removed = (set(bar["time"] for bar in previous_fp["bars"]) -
                       set(bar["time"] for bar in fp["bars"])) if previous_fp else set()
            if previous_fp is None or fp["tickSize"] != previous_fp["tickSize"] or removed:
                messages.append({"type": "footprint_snapshot", **fp})
            else:
                old = {b["time"]: b for b in previous_fp["bars"]}
                changed = [bar for bar in fp["bars"] if old.get(bar["time"]) != bar]
                if changed:
                    messages.append({"type": "footprints", "symbol": self.symbol,
                                     "revision": revision, "tickSize": fp["tickSize"], "bars": changed})
        with self._state_lock:
            self.snapshots = snapshots
            self.footprint = fp
            self.revision = revision
            self._computed_version = demand_version
            self.error = None
            if snapshots:
                self.ready.set()
        for message in messages:
            broadcast(message)


class FeedManager:
    """管理全部订阅; wait_update 循环跑在独立守护线程"""

    def __init__(self):
        self.feeds: dict[str, Feed] = {}
        self.cmd_q: queue.Queue[str] = queue.Queue()
        self.clients: dict[asyncio.Queue, tuple[str, int, bool]] = {}
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

    def add_client(self, queue_: asyncio.Queue, symbol: str, ltf: int, footprint=False):
        with self._lock:
            self.clients[queue_] = (symbol, ltf, footprint)
            feed = self.feeds.get(symbol)
        if feed is not None:
            feed.request(ltf=ltf, footprint=footprint)

    def remove_client(self, queue_: asyncio.Queue):
        with self._lock:
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
        with self._lock:
            clients = list(self.clients.items())
        for q, subscription in clients:
            if msg.get("type") in {"footprints", "footprint_snapshot"}:
                if msg.get("symbol") != subscription[0] or not subscription[2]:
                    continue
            elif (msg.get("symbol"), msg.get("ltf")) != subscription[:2]:
                continue
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 不能静默移除后继续心跳。通知 WS 关闭，让客户端重连并重取快照。
                self.remove_client(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "symbol": subscription[0]})

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
                clients = list(self.clients.values())
            for symbol, ltf, footprint in clients:
                feed = next((f for f in feeds if f.symbol == symbol), None)
                if feed is not None:
                    feed.request(ltf=ltf, footprint=footprint)
            for feed in feeds:
                if feed.ticks is None or len(feed.ticks) == 0:
                    continue
                try:
                    feed.ensure_ltf_subscriptions(api)
                except Exception as exc:
                    with feed._state_lock:
                        feed.error = f"小周期 K线订阅失败: {exc}"
                    continue
                with feed._state_lock:
                    needs_recompute = (not feed.snapshots or
                                       feed._computed_version != feed._demand_version or
                                       feed.error is not None or
                                       api.is_changing(feed.ticks) or
                                       api.is_changing(feed.klines) or
                                       any(api.is_changing(lower) for lower in feed.lower_klines.values()) or
                                       api.is_changing(feed.quote, "price_tick"))
                if needs_recompute:
                    try:
                        feed.recompute(self.broadcast)
                    except Exception as exc:
                        with feed._state_lock:
                            feed.error = f"指标计算失败: {exc}"
                        print(f"[ingest] recompute {feed.symbol} 出错: {exc}", flush=True)
