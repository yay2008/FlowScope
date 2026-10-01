# -*- coding: utf-8 -*-
"""tqSdk 采集: 订阅 30s K线 + tick, 增量计算 Volume Suite bars, 广播并落盘。

设计要点:
- wait_update 循环独占一个守护线程; FastAPI 线程经命令队列请求新合约订阅
- 闭市时初始数据回填不触发 wait_update 返回, 循环用 1s deadline 轮询兼容
- 每根走完且覆盖完整的 bar 追加写入按粒度隔离的 v3 CSV(单文件, 行内 source 列区分
  完整量/估算量); 旧 CSV 仅作 legacy 回填。
- 常驻采集集合(set_pinned, 由 app 按默认合约 + 自选设置)不依赖页面、不参与闲置回收;
  页面临时打开的其它合约按真实需求回收。
- tick 窗口最多 10000 条，更早的 buy/sell 靠 CSV 随运行时间累积。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import queue
import re
import threading
import time
from collections.abc import Mapping, MutableMapping

import pandas as pd
from dotenv import load_dotenv
from tqsdk import TqApi, TqAuth

from indicator import (CFG, DEFAULT_TF_SEC, TF_OPTIONS, bar_ns_for, ltf_options,
                       build_bars, build_bars_from_ltf, bars_to_records)
from history_store import HistoryStore
from tick_analytics import TickAnalytics

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_TICKS = 10000     # 条数窗口；按 500ms 一条估算约 83 分钟
MAX_KLINES = 2000     # K线根数窗口(与周期无关): 30s ≈ 2.5 个交易日, 10s ≈ 5.5 小时
SNAPSHOT_BARS = 800   # 推送给前端的最近 bar 数(与周期无关)
MAX_FEEDS = 32
# 常驻采集(默认合约 + 自选)最多占用的 Feed 数, 其余名额留给页面临时打开的合约。
MAX_PINNED_FEEDS = MAX_FEEDS // 2
# 一次性 SDK 查询(合约目录、自选报价)的最长等待; 冷启动时可能要排队等采集线程连上。
JOB_TIMEOUT_SEC = 12.0
# 单个查询卡住超过这个时长就判定行情连接已不可用(只读缓存对象仍然能读到旧值, 所以
# 不能靠"有没有数据"判断死活), 此时新请求立刻失败并尽快重建连接。
JOB_STALL_SEC = 20.0
# 查询耗时达到 tqsdk 内部 30 秒超时上限, 说明这一次往返根本没回来 —— 直接重建连接。
JOB_REBUILD_SEC = 30.0
# 没有行情流时(闭市、无订阅)每隔这么久做一次极短的连接自检。
# 长连接被静默掐断时 tqsdk 不会报错, 只会让每个取新数据的调用各卡 30 秒 —— 必须主动探活。
PROBE_INTERVAL_SEC = 60.0
# 连接自检查的合约: 只要它在合约服务里存在即可(不订阅行情)。
PROBE_SYMBOL = "KQ.m@SHFE.fu"
# 订阅失败不是永久状态: 冷却期结束后自动重订, 避免一次抖动或一次手误把合约锁死到进程重启。
FAIL_RETRY_SEC = 30
COMPUTE_RETRY_SEC = 1
# 健康和失败 Feed 都按真实需求回收；后台自动重试不延长闲置期。
IDLE_EVICT_SEC = 600
# 回收订阅过的 Feed 要关掉整条连接重建(TqSdk 不能单独退订)。有一个到期时, 这么久之内
# 也会到期的已订阅 Feed 一并回收: 连着取消几个自选、关几个页面只断一次, 不是一个一次。
EVICT_BATCH_SEC = 60
SYMBOL_RE = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)?\Z")


def _csv_key(symbol: str) -> str:
    return symbol.replace("@", "_").replace(".", "_")


def validate_symbol(symbol: str) -> str:
    """校验并规范化 TqSdk 合约标识，避免非法订阅和路径穿越。"""
    value = symbol.strip()
    if not value or len(value) > 64 or SYMBOL_RE.fullmatch(value) is None:
        raise ValueError("symbol 格式无效")
    return value


class UnknownSymbolError(ValueError):
    """合约服务里查不到这个代码(多半是代码写错)。"""


def listed_symbols(api, symbols) -> set[str]:
    """合约服务里查得到的那部分代码(采集线程里执行, 不订阅行情)。

    这一步必须挡在 get_quote / query_symbol_info 前面: 这两个接口收到不存在的代码时,
    合约服务回一个报错, tqsdk 抛出 "代码不存在" —— 但**整条连接的数据管道随之停摆**:
    此后同一连接上任何需要往返的调用(已知合约的新订阅、报价、合约查询)都各自卡到超时。
    2026-09-28 实测: 查一次 SHFE.fu9999 后, 给 KQ.m@SHFE.fu 订阅 60s K 线等了 85 秒才超时。

    所以这里只用 query_quotes 的过滤条件查: 无效的交易所/品种只返回空列表, 不会让合约
    服务报错。期货、主连、指数按"交易所 + 品种"查(几十个代码); 期权、股票这类代码形状
    不是"品种 + 月份"的, 退到整个交易所查(一次几万个代码, 约 0.5 秒)。同一组条件在同一
    连接上命中 tqsdk 的查询缓存, 重复检查不再往返。主连 KQ.m@... 与指数 KQ.i@... 在合约
    服务里的交易所是 KQ。结果包含已下市合约: 它们在合约服务里有记录, 订阅不会出错。
    """
    found: set[str] = set()
    for symbol in dict.fromkeys(symbols):
        exchange_id, product_id, plain = _listing_query(symbol)
        if not exchange_id:
            continue
        if plain:
            listed = api.query_quotes(exchange_id=exchange_id, product_id=product_id)
        else:
            listed = api.query_quotes(exchange_id=exchange_id)
        if symbol in listed:
            found.add(symbol)
    return found


_PLAIN_CONTRACT_RE = re.compile(r"([A-Za-z]+)\d*\Z")


def _listing_query(symbol: str) -> tuple[str, str, bool]:
    """合约代码 -> 合约服务的查询条件 (交易所, 品种, 是否"品种 + 月份"形状)。

    SHFE.fu2611 -> (SHFE, fu, True); CZCE.TA701 -> (CZCE, TA, True);
    KQ.m@SHFE.fu -> (KQ, fu, True); SSE.600000 -> (SSE, "", False)。
    """
    head, at, body = symbol.partition("@")
    exchange_id, _, rest = (body if at else head).partition(".")
    match = _PLAIN_CONTRACT_RE.match(rest)
    return ("KQ" if at else exchange_id), (match.group(1) if match else ""), match is not None


def ensure_listed(api, symbol: str):
    """合约服务里查不到就抛 UnknownSymbolError; 必须在 get_quote 之前调用(见 listed_symbols)。"""
    if symbol not in listed_symbols(api, [symbol]):
        raise UnknownSymbolError(f"合约 {symbol} 不存在, 请检查代码")


def validate_tf(tf) -> int:
    """主周期(秒) 校验; 非整数或不在可选集内时回落到默认周期。"""
    try:
        value = int(tf)
    except (TypeError, ValueError):
        return DEFAULT_TF_SEC
    return value if value in TF_OPTIONS else DEFAULT_TF_SEC


class Feed:
    """单合约 + 单主周期快照；只有 ingest 线程读写 SDK 数据和历史文件。

    周期是 Feed 级维度: 不同周期各自订阅 K 线、各自聚合与落盘, 但共享同一路 tick。
    """

    def __init__(self, symbol: str, tf: int = DEFAULT_TF_SEC):
        self.symbol = validate_symbol(symbol)
        self.tf = validate_tf(tf)
        self.bar_ns = bar_ns_for(self.tf)
        self.klines = self.ticks = self.quote = None
        self.ready = threading.Event()
        self.snapshots = {}
        self.footprint = None
        self.error = None
        # 订阅失败的冷却截止时刻(None = 未失败); 冷却期过后由 FeedManager 自动重订。
        self.retry_at = None
        self.compute_retry_at = 0.0
        self._state_lock = threading.RLock()
        self._requested = {0: float("inf")}
        self._fp_until = 0.0
        # "真实请求"(页面/客户端)的最近时刻与首访时刻; 供 FeedManager 判定闲置。
        # None 表示还没人真正要过这份数据。ingest 线程的保活调用不会写这两个字段,
        # 否则"有人在算"与"有人在看"混在一起, Feed 就永远回收不掉。
        self._created = time.monotonic()
        self._last_demand = None
        self._demand_version = 0
        self._computed_version = -1
        self.revision = 0
        self.stores = {}
        self.analytics = TickAnalytics(self.bar_ns)
        self.lower_klines = {}

    def subscribe(self, api: TqApi):
        ensure_listed(api, self.symbol)   # 不存在的代码会让 get_quote 弄坏整条连接
        self.quote = api.get_quote(self.symbol)
        self.klines = api.get_kline_serial(self.symbol, self.tf, data_length=MAX_KLINES)
        self.ticks = api.get_tick_serial(self.symbol, data_length=MAX_TICKS)
        self.analytics = TickAnalytics(self.bar_ns)
        self.lower_klines = {self.tf: self.klines}
        with self._state_lock:
            self.snapshots.clear()
            self.footprint = None
            self.ready.clear()
            self.error = None
            self.retry_at = None
            self.compute_retry_at = 0.0
            self._computed_version = -1

    def release_serials(self):
        """由采集线程在 TqApi 关闭后释放实时引用，保留 JSON 快照和历史缓存。"""
        self.klines = self.ticks = self.quote = None
        self.lower_klines = {}
        self.analytics = TickAnalytics(self.bar_ns)

    def ensure_ltf_subscriptions(self, api):
        """只在 ingest 线程订阅当前被请求的小周期 K 线。"""
        allowed = ltf_options(self.tf)
        with self._state_lock:
            wanted = [ltf for ltf, expires in self._requested.items()
                      if ltf > 0 and ltf in allowed and expires >= time.monotonic()]
        for ltf in wanted:
            if ltf not in self.lower_klines:
                self.lower_klines[ltf] = api.get_kline_serial(
                    self.symbol, ltf, data_length=min(10000, MAX_KLINES * self.tf // ltf + 1))

    def request(self, ltf=None, footprint=False, demand=False):
        """登记数据需求; demand=True 表示来自页面/客户端的真实请求(参与闲置回收判定)。

        ingest 线程每轮都会调用本方法保持计算, 所以它传 demand=False: 否则"有人在算"
        会被误当成"有人在看", Feed 永远回收不掉。
        """
        now = time.monotonic()
        allowed = ltf_options(self.tf)
        with self._state_lock:
            if demand:
                self._last_demand = now
            # 0 是默认快照, 记无穷表示"一直保持"; 其它粒度超过 60 秒没有新请求即视为弃用。
            if ltf is not None:
                keep = {key: value for key, value in self._requested.items() if value > now}
                keep[0] = float("inf")
                if ltf not in allowed:
                    ltf = 0
                if keep.get(ltf, 0) < now:
                    self._demand_version += 1
                    self.snapshots.pop(ltf, None)
                keep[ltf] = float("inf") if ltf == 0 else now + 60
                self._requested = keep
            if footprint:
                if self._fp_until < now:
                    self._demand_version += 1
                    self.footprint = None
                self._fp_until = now + 60

    def has_demand(self, now=None):
        """除"刚刚被请求过"之外的活跃需求: 非默认粒度窗口或足迹窗口仍开着。

        刻意不看 ltf=0: 它是常驻保活项, 若据此判定有需求, Feed 就永远回收不掉。
        """
        now = time.monotonic() if now is None else now
        with self._state_lock:
            return (any(expires > now for ltf, expires in self._requested.items() if ltf != 0)
                    or self._fp_until > now)

    def idle_for(self, now=None):
        """距最近一次真实请求过了多久(秒); 从未被请求时以创建时刻起算。"""
        now = time.monotonic() if now is None else now
        with self._state_lock:
            return now - (self._created if self._last_demand is None else self._last_demand)

    def snapshot_for(self, ltf, demand=True):
        """读取快照。demand=False 供 ingest 线程保持计算用, 不计入闲置回收判定。"""
        self.request(ltf=ltf, demand=demand)
        with self._state_lock:
            return self.snapshots.get(ltf)

    def footprint_snapshot(self, demand=True):
        self.request(footprint=True, demand=demand)
        with self._state_lock:
            return self.footprint

    def _store(self, ltf):
        if ltf not in self.stores:
            key = _csv_key(self.symbol)
            source = "" if ltf == 0 else "kline_"
            # 文件名带主周期: tf=30 时与历史文件名完全一致, 既有历史无缝沿用;
            # tf=10 是独立文件, 不与 30s 混流(bar 边界不同, 混读会毁掉 CVD)。
            stem = f"{key}_{self.tf}s_{source}ltf{ltf}"
            # v3 起 buy/sell 改用 Lee-Ready 新算法, 必须换文件: v2 里的 buy/sell 是
            # 旧算法结果, 混读会被当成新算法, 并让 CVD 在接缝处跳变。
            # v2 与更早的 {key}_30s.csv 只作 legacy 回填来源, 永远不会被改写;
            # 它们只对应 30s 周期, 其它周期没有可比的历史。
            legacy = []
            if self.tf == DEFAULT_TF_SEC:
                legacy = [os.path.join(DATA_DIR, f"{key}_30s_{source}ltf{ltf}_v2.csv")]
                if ltf == 0:
                    legacy.insert(0, os.path.join(DATA_DIR, f"{key}_30s.csv"))
            self.stores[ltf] = HistoryStore(os.path.join(DATA_DIR, f"{stem}_v3.csv"), legacy)
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
                                  aggregates=aggregates, first_tick_ns=self.analytics.first_tick_ns,
                                  bar_ns=self.bar_ns)
            else:
                lower = self.lower_klines.get(ltf)
                if lower is None or not (lower.datetime > 0).any():
                    continue
                bars = build_bars_from_ltf(self.klines, lower, ltf, bar_ns=self.bar_ns)
            if bars.empty:
                continue
            if ltf == 0:
                tick_bars = bars
            store = self._store(ltf)
            # 增量读: 文件被其它进程(离线脚本/另一次运行)追加过时补齐内存视图,
            # 无变化时只是一次 stat, 不会重解析整个文件。
            store.refresh()
            bars = store.merge(bars)
            store.save_completed(bars)
            bars = store.with_cvd(bars)
            recs = bars_to_records(bars.tail(SNAPSHOT_BARS))
            snapshots[ltf] = {"symbol": self.symbol, "cfg": period_cfg(self.tf), "ltf": ltf,
                              "tf": self.tf,
                              "source": "tick" if ltf == 0 else "kline",
                              "revision": revision, "cvdBase": store.base,
                              "bars": recs}
            old = {b["time"]: b for b in previous.get(ltf, {}).get("bars", [])}
            changed = [bar for bar in recs if old.get(bar["time"]) != bar]
            if changed:
                messages.append({"type": "bars", "symbol": self.symbol, "ltf": ltf,
                                 "tf": self.tf, "revision": revision, "bars": changed})
        fp = None
        if want_fp and tick_bars is not None:
            coverage = dict(zip(tick_bars.time, tick_bars.coverage))
            fp = self.analytics.footprint(self.klines, getattr(self.quote, "price_tick", None), coverage)
            fp = {"symbol": self.symbol, "tf": self.tf, "revision": revision, **fp}
            # 足迹更新必须带步长；步长可能在首次加载后才就绪。
            removed = (set(bar["time"] for bar in previous_fp["bars"]) -
                       set(bar["time"] for bar in fp["bars"])) if previous_fp else set()
            if previous_fp is None or fp["tickSize"] != previous_fp["tickSize"] or removed:
                messages.append({"type": "footprint_snapshot", **fp})
            else:
                old = {b["time"]: b for b in previous_fp["bars"]}
                changed = [bar for bar in fp["bars"] if old.get(bar["time"]) != bar]
                if changed:
                    messages.append({"type": "footprints", "symbol": self.symbol, "tf": self.tf,
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


def _query_cache(api):
    """tqsdk 保存合约查询结果的表(api._data["symbols"]); 取不到时返回 None。

    这是 tqsdk 的内部结构, 所以只用来清理自检留下的条目, 结构变了就放弃清理, 不影响自检。
    """
    data = getattr(api, "_data", None)
    cache = data.get("symbols") if isinstance(data, Mapping) else None
    return cache if isinstance(cache, MutableMapping) else None


def feed_key(symbol: str, tf: int) -> tuple[str, int]:
    """FeedManager 的订阅标识: 同一合约的不同主周期是各自独立的 Feed。"""
    return (symbol, validate_tf(tf))


def feed_label(key: tuple[str, int]) -> str:
    return f"{key[0]}@{key[1]}s"


def period_cfg(tf: int) -> dict:
    """下发前端的配置: 把拆分粒度收敛到该周期合法的子集, 避免前端给出无意义选项。"""
    return {**CFG, "tf": tf, "tfOptions": TF_OPTIONS, "ltfOptions": ltf_options(tf)}


class FeedManager:
    """管理全部订阅; wait_update 循环跑在独立守护线程"""

    def __init__(self):
        self.feeds: dict[tuple[str, int], Feed] = {}
        self.cmd_q: queue.Queue[tuple[str, int]] = queue.Queue()
        # 一次性 SDK 查询(合约目录等): (调用, 结果 Future)。SDK 只能在采集线程碰,
        # 所以请求方排进队列后阻塞等待, 由采集循环执行并回填结果。
        self.jobs: queue.Queue[tuple[object, concurrent.futures.Future]] = queue.Queue()
        # 值: (合约, 主周期, 拆分粒度, 是否要足迹)
        self.clients: dict[asyncio.Queue, tuple[str, int, int, bool]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 唯一的订阅状态，绑定实例；相同 key 的新 Feed 不能继承旧实例的订阅。
        self._subscribed: dict[tuple[str, int], Feed] = {}
        # 包括订阅一半失败的实例；SDK 仍可能持有它们的序列，回收时必须关闭旧连接。
        self._sdk_feeds: set[Feed] = set()
        self._rebuild_api = threading.Event()
        self.status = "stopped"
        self.last_error: str | None = None
        # 订阅失败登记: key -> {reason, retry_at}；仅控制主订阅冷却，不用于跳过计算错误。
        self.failed_feeds: dict[tuple[str, int], dict] = {}
        # 常驻采集集合(见 set_pinned): 不依赖页面, 不参与闲置回收。
        self.pinned: list[tuple[str, int]] = []
        self.pin_skipped: list[tuple[str, int]] = []
        # 正在执行的 SDK 调用(查询或自检)的开始时刻; 跨线程读, 用于判定连接卡死。
        self._busy_since: float | None = None
        # 最近一次收到行情数据的时刻与最近一次连接自检的时刻。
        self._last_data_at = time.monotonic()
        self.last_probe_at = 0.0
        self.probe_error: str | None = None
        # 每轮 wait_update 之后在采集线程里调用的回调 fn(api)(模拟交易撮合挂单等)。
        # 回调只读已订阅的报价; 耗时同样计入 _busy_since, 卡住时连接自检照样能发现。
        self.loop_hooks: list = []

    def feed_retry_error(self, feed: Feed) -> str | None:
        """失败 Feed 未过冷却期时给出可重试的错误文案; 否则返回 None(可以重订)。"""
        key = feed_key(feed.symbol, feed.tf)
        with self._lock:
            entry = self.failed_feeds.get(key)
            reason = entry["reason"] if entry else None
            retry_at = entry["retry_at"] if entry else None
        if not reason:
            return None
        wait = (retry_at or 0.0) - time.monotonic()
        if wait <= 0:
            return None
        return f"{reason}({wait:.0f} 秒后自动重试)"

    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            with self._lock:
                self._subscribed.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="tqsdk-ingest")
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self.fail_pending_jobs("行情线程已停止")
        self._set_status("stopped")

    def submit_job(self, fn) -> concurrent.futures.Future:
        """把一次 SDK 查询排给采集线程, 返回 Future 由调用方等待。

        线程没连上行情源时立刻失败: 这类查询只在真有行情连接时才有意义,
        让 HTTP 请求干等超时不如直接回退到离线目录。连接卡死(某个调用久不返回)
        时也立刻失败, 并已被自检标记为需要重建。
        """
        with self._lock:
            if self.status in {"stopped", "error"}:
                raise RuntimeError(f"行情线程未就绪({self.status})")
            busy = None if self._busy_since is None else time.monotonic() - self._busy_since
            if busy is not None and busy >= JOB_STALL_SEC:
                raise RuntimeError(f"行情连接无响应({busy:.0f} 秒)，正在重建")
            future: concurrent.futures.Future = concurrent.futures.Future()
            self.jobs.put((fn, future))
        return future

    def fail_pending_jobs(self, message: str):
        """连接断开或线程停止时结算排队中的查询, 避免调用方等到超时。"""
        while True:
            try:
                _, future = self.jobs.get_nowait()
            except queue.Empty:
                return
            future.set_exception(RuntimeError(message))

    async def query(self, fn, timeout: float = JOB_TIMEOUT_SEC):
        """在采集线程执行一次 SDK 查询并等待结果(HTTP 处理器调用)。

        超时或线程未就绪都抛 RuntimeError, 由调用方决定回退方案;
        超时后取消 Future, 采集线程到点会跳过它, 不会执行半截。
        """
        future = self.submit_job(fn)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            future.cancel()
            raise RuntimeError(f"合约查询超时({timeout:.0f} 秒)") from None

    def _run_jobs(self, api):
        """执行排队中的查询; 单个查询失败只结算它自己, 不影响采集循环。"""
        while True:
            try:
                fn, future = self.jobs.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue  # 调用方已经超时/取消
            started = time.monotonic()
            self._busy_since = started
            try:
                future.set_result(fn(api))
            except Exception as exc:
                future.set_exception(exc)
            finally:
                self._busy_since = None
                elapsed = time.monotonic() - started
                if elapsed >= JOB_REBUILD_SEC:
                    # 一次往返慢到这个程度只会是连接已经不可用(正常的合约查询是 0.1 秒级),
                    # 让主循环立刻重建, 不必等下一个自检周期。
                    print(f"[ingest] 查询耗时 {elapsed:.0f} 秒，判定连接不可用，重建行情连接",
                          flush=True)
                    self._rebuild_api.set()

    def _run_loop_hooks(self, api):
        """执行循环回调; 单个回调出错只打印, 不影响采集。"""
        for hook in list(self.loop_hooks):
            if self._stop.is_set() or self._rebuild_api.is_set():
                return
            self._busy_since = time.monotonic()
            try:
                hook(api)
            except Exception as exc:
                print(f"[ingest] 循环回调失败: {type(exc).__name__}: {exc}", flush=True)
            finally:
                self._busy_since = None

    def _probe_due(self, now: float) -> bool:
        """行情流安静太久(闭市/无订阅)就该主动问一次连接还在不在。"""
        return (now - max(self._last_data_at, self.last_probe_at)) >= PROBE_INTERVAL_SEC

    def _run_probe(self, api) -> bool:
        """极短的合约查询, 只用来确认连接还能收发; 失败即要求重建连接。

        卡死的连接不会报错: 每个取新数据的调用都会各自等满 tqsdk 的 30 秒内部超时,
        所以这里必须真的发一次往返请求, 而不是看有没有数据。

        不能用 query_quotes: 同一连接上发过的相同查询, tqsdk 直接返回缓存结果,
        从第二次起自检不再走网络、永远"成功"(合约目录也发同一个查询)。
        query_symbol_info 每次都发新请求; 它的结果会留在连接的查询缓存里(约 5KB 一条),
        所以查完就删掉这一条, 否则每分钟一次的自检会让内存随连接寿命增长。
        """
        self._busy_since = time.monotonic()
        try:
            before = set(_query_cache(api) or ())
            rows = api.query_symbol_info([PROBE_SYMBOL])
            cache = _query_cache(api)
            for key in set(cache or ()) - before:
                cache.pop(key, None)
            if len(rows) != 1:
                raise RuntimeError(f"自检合约 {PROBE_SYMBOL} 没有返回合约信息")
        except Exception as exc:
            self.probe_error = f"{type(exc).__name__}: {exc}"
            print(f"[ingest] 连接自检失败({self.probe_error})，重建行情连接", flush=True)
            return False
        finally:
            self._busy_since = None
            self.last_probe_at = time.monotonic()
        self.probe_error = None
        return True

    def add_client(self, queue_: asyncio.Queue, symbol: str, ltf: int, footprint=False,
                   tf: int = DEFAULT_TF_SEC):
        key = feed_key(symbol, tf)
        with self._lock:
            self.clients[queue_] = (key[0], key[1], ltf, footprint)
            feed = self.feeds.get(key)
        if feed is not None:
            feed.request(ltf=ltf, footprint=footprint, demand=True)

    def remove_client(self, queue_: asyncio.Queue):
        with self._lock:
            subscription = self.clients.pop(queue_, None)
            if subscription is not None:
                feed = self.feeds.get(subscription[:2])
                if feed is not None:
                    feed.request(demand=True)  # 闲置期从最后一个客户端离开后起算。

    def ensure(self, symbol: str, tf: int = DEFAULT_TF_SEC) -> Feed:
        """取得 Feed 并登记真实需求；冷却到期后由采集循环重试，成功才清除失败状态。"""
        key = feed_key(validate_symbol(symbol), tf)
        now = time.monotonic()
        with self._lock:
            feed = self.feeds.get(key)
            if feed is not None:
                feed.request(demand=True)
            self._prune(now)
            if feed is None:
                if len(self.feeds) >= MAX_FEEDS:
                    raise ValueError("订阅合约数量已达上限")
                feed = Feed(key[0], key[1])
                feed.request(demand=True)
                self.feeds[key] = feed
                self.cmd_q.put(key)
            return feed

    def set_pinned(self, keys) -> list[tuple[str, int]]:
        """设置常驻采集集合: 关掉所有页面也照常订阅、计算并落盘。

        keys 为 (合约, 主周期), 按优先级排列; 超出 MAX_PINNED_FEEDS 的部分不常驻,
        在状态接口里列出。移出集合的 Feed 回到按真实需求闲置回收的规则, 闲置期从移出时起算。
        """
        ordered = list(dict.fromkeys(feed_key(validate_symbol(symbol), tf) for symbol, tf in keys))
        with self._lock:
            pinned = ordered[:MAX_PINNED_FEEDS]
            # 常驻期间没人看图时闲置时长从创建起算, 早已超过回收期; 不重新计时, 下一轮就会
            # 被回收, 而回收已订阅的 Feed 要重建整条连接, 所有图表一起断几秒。
            for key in set(self.pinned) - set(pinned):
                feed = self.feeds.get(key)
                if feed is not None:
                    feed.request(demand=True)
            self.pinned = pinned
            self.pin_skipped = ordered[MAX_PINNED_FEEDS:]
            self._ensure_pinned()
            return list(self.pinned)

    def _ensure_pinned(self):
        """补建缺失的常驻 Feed; 池子满时留到页面 Feed 回收后的下一轮。"""
        with self._lock:
            for key in self.pinned:
                if key not in self.feeds and len(self.feeds) < MAX_FEEDS:
                    self.feeds[key] = Feed(*key)

    def fail_feed(self, feed: Feed, message: str, now=None):
        """登记订阅冷却；采集循环自行检查截止时间，不跨线程操作 asyncio 定时器。"""
        key = feed_key(feed.symbol, feed.tf)
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.feeds.get(key) is not feed:
                return  # 已回收实例的迟到异常不得污染同 key 的新实例。
            with feed._state_lock:
                feed.error = message
                feed.retry_at = now + FAIL_RETRY_SEC
            self.failed_feeds[key] = {"reason": message, "retry_at": now + FAIL_RETRY_SEC}
            self._subscribed.pop(key, None)

    def status_snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            busy = None if self._busy_since is None else round(now - self._busy_since, 1)
            return {"status": self.status, "lastError": self.last_error,
                    "feeds": sorted(feed_label(key) for key in self.feeds),
                    "failedFeeds": {feed_label(k): v["reason"]
                                    for k, v in self.failed_feeds.items()},
                    # 常驻采集集合; collectSkipped 是超出名额、只在页面打开时才采集的部分
                    "collecting": [feed_label(key) for key in self.pinned],
                    "collectSkipped": [feed_label(key) for key in self.pin_skipped],
                    # 诊断"连接卡死"用: 排队查询数、当前调用已耗时、安静多久、上次自检结果
                    "jobQueue": self.jobs.qsize(),
                    "busySec": busy,
                    "quietSec": round(now - self._last_data_at, 1),
                    "lastProbeSec": round(now - self.last_probe_at, 1) if self.last_probe_at else None,
                    "probeError": self.probe_error}

    def _prune(self, now=None):
        """回收无人再要的 Feed, 否则 MAX_FEEDS 只增不减, 反复试错合约就会占满池子。

        自动重试不算真实需求；失败 Feed 也按最后一次真实请求起算，避免重试不断续命。
        常驻采集的 Feed 不回收。要回收已订阅的 Feed 时, 把 EVICT_BATCH_SEC 内也会到期的
        已订阅 Feed 一起回收, 合并成一次连接重建。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            busy = {key[:2] for key in self.clients.values()} | set(self.pinned)
            idle = {key: feed.idle_for(now) for key, feed in self.feeds.items()
                    if key not in busy and not feed.has_demand(now)}
            due = [key for key, seconds in idle.items() if seconds >= IDLE_EVICT_SEC]
            if any(self.feeds[key] in self._sdk_feeds for key in due):
                due += [key for key, seconds in idle.items()
                        if IDLE_EVICT_SEC - EVICT_BATCH_SEC <= seconds < IDLE_EVICT_SEC
                        and self.feeds[key] in self._sdk_feeds]
            for key in due:
                self._forget(key)

    def _forget(self, key: tuple[str, int]):
        """必须在持有 self._lock 时调用。"""
        self.failed_feeds.pop(key, None)
        feed = self.feeds.pop(key, None)
        self._subscribed.pop(key, None)
        if feed in self._sdk_feeds:
            # 只发信号，SDK 的关闭/重建均在采集线程执行。
            self._rebuild_api.set()

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
                if (msg.get("symbol"), msg.get("tf")) != subscription[:2] or not subscription[3]:
                    continue
            elif (msg.get("symbol"), msg.get("tf"), msg.get("ltf")) != subscription[:3]:
                continue
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 不能静默移除后继续心跳。通知 WS 关闭，让客户端重连并重取快照。
                self.remove_client(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "symbol": subscription[0], "tf": subscription[1]})

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            api = None
            rebuild = False
            try:
                username = os.getenv("TQ_USER")
                password = os.getenv("TQ_PASS")
                if not username or not password:
                    raise RuntimeError("未设置 TQ_USER/TQ_PASS")
                self._set_status("connecting")
                api = TqApi(auth=TqAuth(username, password))
                self._set_status("connected")
                backoff = 1
                rebuild = self._run_api(api)
                if rebuild:
                    self._set_status("connecting")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._set_status("error", message)
                print(f"[ingest] TqSdk 连接失败: {message}", flush=True)
            finally:
                if api is not None:
                    try:
                        api.close()
                    except Exception as exc:
                        print(f"[ingest] 关闭 TqSdk 失败: {exc}", flush=True)
                self._release_connection()
                api = None
            if rebuild:
                # 主动回收资源不使用故障退避；旧 API 关闭后立即重建仍需保留的订阅。
                continue
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)

    def _release_connection(self):
        """只在采集线程关闭 API 后调用，断开 Feed 对旧 SDK 序列的引用。"""
        with self._lock:
            feeds = list(self._sdk_feeds)
            self._sdk_feeds.clear()
            self._subscribed.clear()
        for feed in feeds:
            feed.release_serials()
        # 待执行查询绑定在刚关闭的连接上, 一律作废, 否则调用方会等到超时。
        self.fail_pending_jobs("行情连接已重建")

    def _run_api_attempt(self, api, feed: Feed) -> bool:
        """单次订阅：按实例核对身份，成功后才解除失败；忽略已回收实例的迟到结果。"""
        key = feed_key(feed.symbol, feed.tf)
        with self._lock:
            if (self.feeds.get(key) is not feed or self._rebuild_api.is_set()
                    or self._subscribed.get(key) is feed):
                return False
            entry = self.failed_feeds.get(key)
            if entry is not None and time.monotonic() < entry["retry_at"]:
                return False
            # 必须在调用 SDK 前标记，部分订阅失败也可能留下 SDK 缓存。
            self._sdk_feeds.add(feed)
        try:
            feed.subscribe(api)
        except UnknownSymbolError as exc:
            # 代码不存在不是网络抖动, 但也可能是合约服务刚好没数据: 仍按冷却期重试,
            # 这一步只查合约服务的过滤查询, 不会拖住连接。
            message = str(exc)
            self.fail_feed(feed, message)
            print(f"[ingest] {message}", flush=True)
            return False
        except Exception as exc:
            message = f"合约 {feed_label(key)} 订阅失败: {exc}"
            self.fail_feed(feed, message)
            print(f"[ingest] {message}", flush=True)
            return False
        with self._lock:
            if self.feeds.get(key) is not feed:
                self._rebuild_api.set()
                return False
            self._subscribed[key] = feed
            self.failed_feeds.pop(key, None)
        return True

    def _sweep_subscriptions(self, api):
        """唯一的重试入口：每轮检查未订阅实例与冷却时间，无须页面请求或异步定时器。"""
        with self._lock:
            pending = [feed for key, feed in self.feeds.items()
                       if self._subscribed.get(key) is not feed]
        for feed in pending:
            if self._stop.is_set() or self._rebuild_api.is_set():
                break
            self._run_api_attempt(api, feed)

    def _run_api(self, api: TqApi):
        """返回 True 请求关闭并重建连接，False 表示停止。SDK 操作均留在本线程。"""
        with self._lock:
            self._subscribed.clear()
            self._rebuild_api.clear()  # 上一个连接已由 _run 关闭并释放。
        while not self._stop.is_set():
            self._prune()
            self._ensure_pinned()
            if self._rebuild_api.is_set():
                return True
            # 命令只负责登记新需求，订阅状态统一由 sweep 维护。
            try:
                while True:
                    self.cmd_q.get_nowait()
            except queue.Empty:
                pass
            self._sweep_subscriptions(api)
            if self._rebuild_api.is_set():
                return True
            # 合约目录等一次性查询在采集线程执行; 它们内部可能自己 wait_update,
            # 正好顺带刷新主订阅, 所以放在主 wait_update 之前。
            self._run_jobs(api)
            if self._rebuild_api.is_set():
                return True
            if self._stop.is_set():
                break
            # 行情流安静太久就主动探一次活: 被静默掐断的连接不会报错, 只会让之后每个取新
            # 数据的调用各卡 30 秒。自检失败即返回, 由 _run 关掉旧连接重建。
            if self._probe_due(time.monotonic()) and not self._run_probe(api):
                return True
            if self._rebuild_api.is_set():
                return True
            api.wait_update(deadline=time.time() + 1)
            self._prune()
            if self._rebuild_api.is_set():
                return True
            with self._lock:
                feeds = list(self.feeds.values())
                clients = list(self.clients.values())
            by_key = {feed_key(f.symbol, f.tf): f for f in feeds}
            for symbol, tf, ltf, footprint in clients:
                feed = by_key.get((symbol, tf))
                if feed is not None:
                    feed.request(ltf=ltf, footprint=footprint, demand=False)
            for feed in feeds:
                if self._stop.is_set() or self._rebuild_api.is_set():
                    break
                with self._lock:
                    subscribed = self._subscribed.get(feed_key(feed.symbol, feed.tf)) is feed
                if not subscribed or feed.ticks is None or len(feed.ticks) == 0:
                    continue
                if time.monotonic() < feed.compute_retry_at:
                    continue
                try:
                    feed.ensure_ltf_subscriptions(api)
                    with feed._state_lock:
                        changed = (api.is_changing(feed.ticks) or api.is_changing(feed.klines) or
                                   any(api.is_changing(lower) for lower in feed.lower_klines.values()) or
                                   api.is_changing(feed.quote, "price_tick"))
                        needs_recompute = (not feed.snapshots or feed.error is not None or
                                           feed._computed_version != feed._demand_version or changed)
                    if changed:
                        # 有数据在流动 = 连接肯定是活的, 自检可以往后推
                        self._last_data_at = time.monotonic()
                    if needs_recompute:
                        feed.recompute(self.broadcast)
                        with feed._state_lock:
                            feed.error = None
                            feed.compute_retry_at = 0.0
                except Exception as exc:
                    # 计算/小周期订阅失败不撤销已成功的主订阅；短暂退避后再次处理。
                    with feed._state_lock:
                        feed.error = f"行情处理失败: {exc}"
                        feed.compute_retry_at = time.monotonic() + COMPUTE_RETRY_SEC
                    print(f"[ingest] {feed_label(feed_key(feed.symbol, feed.tf))} {feed.error}", flush=True)
            self._run_loop_hooks(api)
        return self._rebuild_api.is_set() and not self._stop.is_set()
