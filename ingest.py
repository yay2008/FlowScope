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
- 1 分钟及以上的周期不单独采集: 只订阅该周期的原生 K 线, 买卖量由同合约 30s 合成
  (FuturesRollupFeed, 见 rollup.py)。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import queue
import re
import threading
import time
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from tqsdk import TqApi, TqAuth

from indicator import (CFG, DEFAULT_TF_SEC, ROLLUP_BASE_TF, TF_OPTIONS, TZ_SHIFT_S, bar_ns_for, is_rollup,
                       ltf_options, build_bars, build_bars_from_ltf, bars_to_records)
from history_store import HistoryStore
from rollup import RollupMixin, candles_from_klines
from tick_analytics import TickAnalytics
from large_trades import LargeTradeDetector, decorate_bars

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
# 主力映射与行情流是否活跃无关: 无人看图、一直有 tick 时也要检查换月。
MAIN_CONTRACT_CHECK_SEC = 60.0
# 连接自检查的合约: 只要它在合约服务里存在即可(不订阅行情)。
PROBE_SYMBOL = "KQ.m@SHFE.fu"
MAIN_PREFIX = "KQ.m@"
# 订阅失败不是永久状态: 冷却期结束后自动重订, 避免一次抖动或一次手误把合约锁死到进程重启。
FAIL_RETRY_SEC = 30
COMPUTE_RETRY_SEC = 1
# 没有页面在看的周期: 数据变了最多每这么久重算一次(落盘、快照不必每笔 tick 都跟); 在看的照常每笔都算。
IDLE_RECOMPUTE_SEC = 5.0
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


def is_main(symbol: str) -> bool:
    """主连(自动跟随主力月份的入口), 如 KQ.m@SHFE.fu。"""
    return symbol.startswith(MAIN_PREFIX)


def frame_records(frame) -> list[dict]:
    """SDK 返回的 DataFrame(合约查询、K 线、交易日历) -> 行字典列表; 兼容测试里的普通列表。"""
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def main_contracts(api, symbols) -> tuple[dict[str, str], dict[str, str]]:
    """已确认存在的主连 -> 当前实际月份, 以及解析不出的主连 -> 原因; 一个解析不出不耽误其它的。

    不读取可能陈旧的 Quote 静态字段。
    """
    wanted = list(dict.fromkeys(symbol for symbol in symbols if is_main(symbol)))
    if not wanted:
        return {}, {}
    with transient_queries(api):        # 每分钟核对一次
        rows = frame_records(api.query_symbol_info(wanted))
    by_symbol = {row.get("instrument_id"): row for row in rows}
    found, errors = {}, {}
    for symbol in wanted:
        contract = str(by_symbol.get(symbol, {}).get("underlying_symbol") or "").strip()
        if re.fullmatch(re.escape(symbol[len(MAIN_PREFIX):]) + r"\d{3,4}", contract) is None:
            errors[symbol] = f"主力合约 {symbol} 的当前月份尚未就绪"
        else:
            found[symbol] = contract
    return found, errors


def data_contract(api, symbol: str) -> str:
    """主力入口订阅实际月份的整段历史; 解析失败时不回退到拼接的主连。"""
    ensure_listed(api, symbol)
    if not is_main(symbol):
        return symbol
    found, errors = main_contracts(api, [symbol])
    if symbol not in found:
        raise RuntimeError(errors[symbol])
    return found[symbol]


class MainContracts:
    """主连 -> 当前实际月份: 图表订阅、定期核对、自选面板、模拟交易、合约目录共用这一份,
    换月时各处一起换。查询只在采集线程里做(调用方须已确认代码存在, 见 listed_symbols)。

    MAIN_CONTRACT_CHECK_SEC 之内核对过的直接用; 过期的一次查完。查不到的沿用上一次的结果,
    也记下这次尝试, 同样隔这么久再试 —— 采集循环每轮都调也不会变成每轮都查。
    """

    def __init__(self, ttl: float = MAIN_CONTRACT_CHECK_SEC):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._known: dict[str, tuple[str, float]] = {}   # 主连 -> (月份, 查到它的时刻)
        self._tried: dict[str, float] = {}               # 主连 -> 最近一次查询的时刻
        self._errors: dict[str, str] = {}                # 主连 -> 最近一次没查到的原因

    def resolve(self, api, symbols, refresh: bool = False) -> dict[str, str]:
        """采集线程里调用: 主连 -> 当前月份(从没查到过的不在结果里); 非主连忽略。"""
        now = time.monotonic()
        wanted = [symbol for symbol in dict.fromkeys(symbols) if is_main(symbol)]
        with self._lock:
            stale = [symbol for symbol in wanted
                     if refresh or now - self._tried.get(symbol, float("-inf")) >= self.ttl]
        if stale:
            try:
                found, errors = main_contracts(api, stale)
            except Exception as exc:
                found, errors = {}, {symbol: f"{type(exc).__name__}: {exc}" for symbol in stale}
            with self._lock:
                self._tried.update(dict.fromkeys(stale, now))
                self._known.update({symbol: (contract, now) for symbol, contract in found.items()})
                for symbol in found:
                    self._errors.pop(symbol, None)
                self._errors.update(errors)
        with self._lock:
            return {symbol: self._known[symbol][0] for symbol in wanted if symbol in self._known}

    def errors(self, symbols) -> dict[str, str]:
        with self._lock:
            return {symbol: self._errors[symbol] for symbol in symbols if symbol in self._errors}

    def known(self) -> dict[str, tuple[str, float]]:
        """任意线程可读: 主连 -> (月份, 查到它的时刻)。"""
        with self._lock:
            return dict(self._known)


def validate_tf(tf) -> int:
    """主周期(秒) 校验; 非整数或不在可选集内时回落到默认周期。"""
    try:
        value = int(tf)
    except (TypeError, ValueError):
        return DEFAULT_TF_SEC
    return value if value in TF_OPTIONS else DEFAULT_TF_SEC


def _same_prefix(new: pd.DataFrame, old: pd.DataFrame) -> int:
    """new 开头有几根与 old 里同一时间的那根逐列按位相同(都按 time 升序)。

    浮点按位比: -0.0 与 0.0、NaN 都算不同, 宁可多转几根, 也不能把转出来不一样的当成一样。
    两边列或类型不一样时为 0。
    """
    if new.empty or old.empty or list(new.columns) != list(old.columns) or not new.dtypes.equals(old.dtypes):
        return 0
    new_times, old_times = new["time"].to_numpy(), old["time"].to_numpy()
    offset = int(np.searchsorted(old_times, new_times[0]))
    count = min(len(new_times), len(old_times) - offset)
    if count <= 0:
        return 0
    same = new_times[:count] == old_times[offset:offset + count]
    for column in new.columns:
        a = new[column].to_numpy()[:count]
        b = old[column].to_numpy()[offset:offset + count]
        if a.dtype.kind == "f":
            same &= a.view(f"i{a.dtype.itemsize}") == b.view(f"i{b.dtype.itemsize}")
        elif a.dtype.kind == "O":
            same &= np.fromiter((x is y or (type(x) is type(y) and x == y) for x, y in zip(a, b)), bool, count)
        else:
            same &= a == b
    return count if same.all() else int(np.argmin(same))


class Feed:
    """单合约 + 单主周期快照；只有 ingest 线程读写 SDK 数据和历史文件。

    周期是 Feed 级维度: 不同周期各自订阅 K 线、各自聚合与落盘, 但共享同一路 tick。
    """

    def __init__(self, symbol: str, tf: int = DEFAULT_TF_SEC):
        self.symbol = validate_symbol(symbol)
        # symbol 保留自选/URL 的稳定标识; contract 决定 SDK 订阅与历史文件。
        self.contract = self.symbol
        # 主力换了月份、新月份的快照还没发出去: 发整份替换(见 _snapshot_message), 断线重连不算
        self.rolled = False
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
        # 各粒度最近一次合并历史、算好 CVD 的完整窗口(连同那一轮的 revision); 大周期由它合成,
        # 只认最近一轮算出来的(见 latest_window)。
        self.latest = {}
        self.analytics = TickAnalytics(self.bar_ns)
        self.lower_klines = {}
        self.large_trades = self._new_large_detector()
        self._large_synced = None   # (TickAnalytics, version): 快照没变就不再喂检测器
        self._large_cache = None    # (输入签名, 各根汇总): 各拆分粒度共用, 见 _decorate_large
        # 增量重算(加密与大周期用, 见 _start_round): 每轮开始时编号加一, 只有紧挨着的上一轮留下的中间结果
        # 才能接着用 —— 中途出错的那一轮已经取走了改动记录, 它之前的结果就不能再接。
        self._round = 0
        self._large_since = float("-inf")   # 这一轮大额汇总要从哪根(展示秒)起重算; -inf 全部重算
        self._large_seen = None             # 上一轮汇总用的检测器(换了就全部重算)
        self._record_state = {}             # ltf -> (轮次, 快照, 生成它的那段 bars), 见 _records
        self._tail_marks = {}               # ltf -> (轮次, 历史文件, 它算完时的版本), 见 _tail_since
        self._klines_seen = None            # 上一轮的 K 线 (展示秒, 开高低收量), 见 _kline_changes
        # 采集循环的节流(见 FeedManager._run_api): 数据变了还没算 / 上次算完的时刻
        self.pending = False
        self.last_compute = 0.0

    def _new_large_detector(self):
        """原生周期自带大额检测器; 合成周期与多所汇总没有自己的原始成交, 覆盖为 None。
        按实际月份检测; 主连代码下写的门槛也作用于它(见 large_trades.settings_for)。"""
        aliases = (self.symbol,) if self.symbol != self.contract else ()
        return LargeTradeDetector(self.contract, aliases=aliases)

    def _bind_contract(self, contract: str):
        """换月时清掉旧月份的计算与快照缓存, 保留客户端需求、revision 和磁盘档案。"""
        with self._state_lock:
            if self.contract == contract:
                return
            self.rolled = self.contract != self.symbol     # 原来就在采某个月份: 这是换月
            self.contract = contract
            self.release_serials()
            self.snapshots.clear()
            self.footprint = None
            self.ready.clear()
            self.stores.clear()
            self.latest.clear()
            self.large_trades = self._new_large_detector()
            self._large_synced = self._large_cache = self._large_seen = None
            self._large_since = float("-inf")
            self._record_state.clear()
            self._tail_marks.clear()
            self._klines_seen = None
            self._computed_version = -1
            if getattr(self, "is_rollup", False):
                self._closed.clear()
                self.base_revision = None

    def subscribe(self, api: TqApi, contract: str | None = None):
        """contract: FeedManager 已确认存在并解析好的实际合约(见 FeedManager._contract_for); 不给就现查。"""
        self._bind_contract(contract or data_contract(api, self.symbol))
        self.quote = api.get_quote(self.contract)
        self.klines = api.get_kline_serial(self.contract, self.tf, data_length=MAX_KLINES)
        self.ticks = api.get_tick_serial(self.contract, data_length=MAX_TICKS)
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

    def has_data(self) -> bool:
        """采集循环据此决定这一轮要不要处理它: 有 tick 才算得出买卖量。"""
        return self.ticks is not None and len(self.ticks) > 0

    def data_changing(self, api) -> bool:
        """本轮 wait_update 有没有带来它关心的新数据。"""
        return (api.is_changing(self.ticks) or api.is_changing(self.klines) or
                any(api.is_changing(lower) for lower in self.lower_klines.values()) or
                api.is_changing(self.quote, ["price_tick", "price_decs", "trading_time"]))

    def ensure_ltf_subscriptions(self, api):
        """只在 ingest 线程订阅当前被请求的小周期 K 线。"""
        allowed = ltf_options(self.tf)
        with self._state_lock:
            wanted = [ltf for ltf, expires in self._requested.items()
                      if ltf > 0 and ltf in allowed and expires >= time.monotonic()]
        for ltf in wanted:
            if ltf not in self.lower_klines:
                self.lower_klines[ltf] = api.get_kline_serial(
                    self.contract, ltf, data_length=min(10000, MAX_KLINES * self.tf // ltf + 1))

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

    def _empty_footprint(self, demand=True):
        """不做足迹图的 Feed(加密、大周期)给一份空足迹, 页面切到足迹图时不会一直等快照。"""
        self.request(demand=demand)
        with self._state_lock:
            return {"symbol": self.symbol, "tf": self.tf, "revision": self.revision,
                    "tickSize": None, "bars": []}

    def latest_window(self, ltf):
        """最近一轮重算出的 ltf 粒度完整窗口; 那一轮没算这个粒度(已停用、还没算到)就是 None。

        某一轮算到一半出错时 revision 不前进, 已经算好的粒度带的是下一个 revision: 也认, 照样是最新的;
        出错的粒度还留着上一轮(当前 revision)的。
        """
        entry = self.latest.get(ltf)
        return entry[1] if entry is not None and entry[0] >= self.revision else None

    def _store(self, ltf):
        if ltf not in self.stores:
            key = _csv_key(self.contract)
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
        # 金额门槛要用合约乘数; 报价到之前先不喂, 到了再从整个 tick 窗口补算。
        detector = self.large_trades
        multiple = getattr(self.quote, "volume_multiple", None)
        if isinstance(multiple, (int, float)) and math.isfinite(multiple) and multiple > 0:
            detector.multiplier = float(multiple)
        # 同合约的 10s/30s 共用一个检测器, 各自的 tick 窗口没变时不必重复比对。
        synced = (self.analytics, self.analytics.version)
        if synced != self._large_synced and detector.multiplier is not None:
            fresh = not detector.raw
            detector.sync_futures(classified, None if fresh else self.analytics.changed_ns,
                                  fresh or self.analytics.version == 1)
            self._large_synced = synced
        valid_klines = self.klines.dropna(subset=["datetime", "close"])
        if valid_klines.empty:
            return
        first_bar_ns = int(valid_klines.datetime.iloc[0])
        # 检测器本轮已喂完 tick, 这时才取它的改动; 大额汇总只重做改到的那几根(见 _records)
        self._start_round()
        # K 线与 tick 汇总从上一轮到现在最早改到哪根(展示秒): 平时只有最后一两根, 只算尾部(见 _tail_since)
        times = (valid_klines["datetime"] // 10**9 + TZ_SHIFT_S).astype("int64").to_numpy()
        dirty = self.analytics.dirty_from(0)
        changed = min(self._kline_changes(valid_klines, times),
                      dirty if not math.isfinite(dirty) else int(dirty) // 10**9 + TZ_SHIFT_S)
        snapshots = {}
        messages = []
        revision = self.revision + 1
        tick_bars = None
        for ltf in ltfs:
            tail = None
            if ltf == 0:
                aggregates = self.analytics.aggregate(0, first_bar_ns)
                tick_bars_of = lambda klines, aggregates=aggregates: build_bars(
                    klines, self.ticks, 0, classified=classified, aggregates=aggregates,
                    first_tick_ns=self.analytics.first_tick_ns, bar_ns=self.bar_ns)
                # 足迹图要整窗口(合并历史之前)的覆盖, 开着足迹图时整窗口算
                since = None if want_fp else self._tail_since(0, changed)
                bars = tick_bars_of(valid_klines[times >= since]) if since is not None else None
                if bars is None or bars.empty:       # 尾部是空的不该发生(上一轮的最后一根总在里面)
                    bars = tick_bars_of(self.klines)
                else:
                    tail = (since, times, lambda: tick_bars_of(self.klines))
            else:
                # K 线口径: 小周期窗口一滚动, 覆盖起点就在主周期窗口中间挪动, 只算尾部省不了多少, 整窗口算
                lower = self.lower_klines.get(ltf)
                if lower is None or not (lower.datetime > 0).any():
                    continue
                bars = build_bars_from_ltf(self.klines, lower, ltf, bar_ns=self.bar_ns)
            if bars.empty:
                continue
            if ltf == 0:
                tick_bars = bars
            snapshots[ltf], message = self._publish(ltf, bars, previous, revision, tail)
            if message:
                messages.append(message)
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
                self.rolled = False
        for message in messages:
            broadcast(message)

    def cfg(self) -> dict:
        """快照里下发前端的配置: 周期参数、价格位数和合约交易时段。

        报价还没到时(price_tick 还是 NaN) price_decs 是默认的 0, 不可信: 这时不下发 priceDigits,
        前端按「位数未知」处理, 不拿 0 位去取整(国债期货 107.85 会被取成 108)。
        """
        cfg = period_cfg(self.tf)
        tick = getattr(self.quote, "price_tick", None)
        decs = getattr(self.quote, "price_decs", None)
        if (isinstance(tick, (int, float)) and math.isfinite(tick) and tick > 0
                and isinstance(decs, (int, float)) and decs >= 0):
            cfg["priceDigits"] = int(decs)
        trading_time = getattr(self.quote, "trading_time", None)
        if trading_time is not None:
            # TradingTime 是实时对象, 必须复制成普通列表, 避免旧快照也随 SDK 就地更新。
            cfg["tradingTime"] = {}
            for key in ("day", "night"):
                periods = (trading_time.get(key) if isinstance(trading_time, Mapping)
                           else getattr(trading_time, key, None))
                cfg["tradingTime"][key] = [list(period) for period in (periods or [])]
        cfg["largeTrades"] = [detector.config() for detector in self.large_sources()]
        return cfg

    def large_sources(self):
        return [self.large_trades]

    def _decorate_large(self, recs):
        """大额统计只取决于检测器状态和各根时间、成交量, 与拆分粒度无关:
        同一轮各粒度输入相同时复用上一份汇总(只读共享, 有变化时整份重建, 不会就地改旧快照)。"""
        sources = self.large_sources()
        key = (tuple(sources), tuple(d.version for d in sources),
               tuple((r["time"], r.get("volume")) for r in recs))
        if self._large_cache is not None and self._large_cache[0] == key:
            for rec, (summary, relative) in zip(recs, self._large_cache[1]):
                rec["large"] = summary
                if relative is not None:
                    rec["largeRelative"] = relative
            return
        decorate_bars(recs, sources, self.tf)
        self._large_cache = (key, [(rec.get("large"), rec.get("largeRelative")) for rec in recs])

    def _start_round(self):
        """增量重算的一轮开始(读任何数据之前调用): 轮次加一, 取走大额检测器从上一轮到现在的改动。

        检测器给的是成交时刻(ns), 换成展示秒再往前让一根: 一根 bar 的大额汇总只看 [起点, 起点+周期)
        里的事件、覆盖区间和这之前的累计, 在改动时刻之前就结束的 bar 不受影响。不调用它(期货的原生周期)
        就一直是 -inf, 每轮全部重新汇总。
        """
        self._round += 1
        sources = tuple(self.large_sources())
        earliest = min((source.changes.take(self) for source in sources), default=float("inf"))
        if sources != self._large_seen:
            earliest = float("-inf")
        self._large_seen = sources
        if math.isfinite(earliest):
            earliest = int(earliest // 10**9) + TZ_SHIFT_S - self.tf
        self._large_since = earliest

    def _kline_changes(self, klines, times):
        """K 线从上一轮到现在最早改到哪根(展示秒; inf 没改, -inf 整体都变了)。

        TqSdk 原地改 K 线序列, 没有改动记录, 只能与上一轮存下的逐根按位比: 平时只有最后一根在变,
        新的一根追加在末尾、最早的一根滑出窗口。
        """
        values = np.ascontiguousarray(klines[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float))
        previous, self._klines_seen = self._klines_seen, (times.copy(), values)
        if previous is None or not len(times) or not len(previous[0]):
            return float("-inf")
        old_times, old_values = previous
        offset = int(np.searchsorted(old_times, times[0]))
        count = min(len(times), len(old_times) - offset)
        if count <= 0:
            return float("-inf")
        same = times[:count] == old_times[offset:offset + count]
        same &= (values[:count].view(np.int64) == old_values[offset:offset + count].view(np.int64)).all(axis=1)
        first = count if same.all() else int(np.argmin(same))
        return float("inf") if first == len(times) else int(times[first])

    def _tail_since(self, ltf, changed):
        """这一粒度能不能只算尾部: 能就返回尾部起点(展示秒), 否则 None(整窗口重算)。

        changed 是数据来源从上一轮到现在最早改到哪根(展示秒; inf 没改, -inf 整体都变了)。尾部还要包括
        上一轮算完之后历史文件又改到的地方(回填、汇总合成写进来的), 和上一轮的最后一根(那时没走完、
        没落盘, 现在可能走完了)。更早的各根合并历史、落盘、CVD 的结果都与上一轮相同, 直接沿用。
        """
        mark = self._tail_marks.get(ltf)
        entry = self.latest.get(ltf)
        if mark is None or entry is None or mark[0] != self._round - 1:
            return None
        store = self._store(ltf)
        if mark[1] is not store:
            return None
        store.refresh()
        moved = store.changed_since(mark[2])
        since = min(changed, float("inf") if moved is None else moved, int(entry[1]["time"].iloc[-1]))
        return int(since) if math.isfinite(since) else None

    def _publish(self, ltf, bars, previous, revision, tail=None):
        """合并历史、落盘、算 CVD, 返回 (该粒度的快照, 有变化时的增量消息或 None)。

        tail=(起点, 本轮窗口各根的时间, 重取整窗口的函数) 时 bars 只有起点及以后的几根(见 _tail_since):
        更早的沿用上一轮的结果, 去掉滑出窗口的。拼出来与本轮窗口对不上(不该发生)就整窗口重算。
        """
        store = self._store(ltf)
        # 增量读: 文件被其它进程(离线脚本/另一次运行)追加过时补齐内存视图,
        # 无变化时只是一次 stat, 不会重解析整个文件。
        store.refresh()
        bars = self._merge_history(store, bars)
        if tail is not None:
            since, window, rebuild = tail
            kept = self.latest[ltf][1]
            times = kept["time"].to_numpy()
            kept = kept[(times < since) & np.isin(times, window)]
            if not kept.empty:
                bars = pd.concat([kept, bars], ignore_index=True)
            if not np.array_equal(bars["time"].to_numpy(), window):
                bars = self._merge_history(store, rebuild())
        self.latest[ltf] = (revision, bars)
        self._tail_marks[ltf] = (self._round, store, store.revision)
        return self._snapshot_message(ltf, bars, previous, revision, store.base)

    @staticmethod
    def _merge_history(store, bars):
        bars = store.merge(bars)
        store.save_completed(bars)
        return store.with_cvd(bars)

    def _records(self, ltf, tail, prior):
        """快照要发的最近几根 bars -> (沿用上一份快照的各根, 新转的各根)。

        开头那些与上一轮(紧挨着的那一轮)的 bars 逐列按位相同、又在大额汇总改动处之前的, 直接沿用上一份
        快照里的 dict(它们转出来、汇总出来都一样); 从第一根不同的起往后都重新转、重新汇总大额。
        大额汇总只看这一根的时间段和更早的事件, 对一段末尾单独汇总与整段汇总再取末尾相同。
        """
        start = 0
        state = self._record_state.get(ltf)
        if (prior is not None and state is not None and state[0] == self._round - 1 and state[1] is prior
                and self._large_since > float("-inf")):
            start = _same_prefix(tail, state[2])
            times = tail["time"].to_numpy()
            if math.isfinite(self._large_since):
                start = min(start, int(np.searchsorted(times, self._large_since, side="left")))
            # 新的一根离前一根不到一个周期时, 前一根汇总的终点(见 decorate_bars)跟着变
            while 0 < start < len(times) and times[start] - times[start - 1] < self.tf:
                start -= 1
        kept = []
        if start:
            old = {bar["time"]: bar for bar in prior["bars"]}
            kept = [old[t] for t in tail["time"].iloc[:start].tolist()]
        fresh = bars_to_records(tail.iloc[start:])
        self._decorate_large(fresh)
        return kept, fresh

    def _snapshot_message(self, ltf, bars, previous, revision, cvd_base):
        """算好的 bars -> (该粒度的快照, 与上一份快照相比有变化时的增量消息或 None)。"""
        tail = bars.tail(SNAPSHOT_BARS)
        prior = previous.get(ltf)
        kept, fresh = self._records(ltf, tail, prior)
        recs = kept + fresh
        snapshot ={"symbol": self.symbol, "contract": self.contract, "cfg": self.cfg(), "ltf": ltf,
                    "tf": self.tf,
                    "source": "tick" if ltf == 0 else "kline",
                    "revision": revision, "cvdBase": cvd_base,
                    "bars": recs}
        self._record_state[ltf] = (self._round, snapshot, tail)
        old = {b["time"]: b for b in (prior or {}).get("bars", [])}
        # 沿用的就是上一份里的同一个 dict, 不会有变化
        changed = [bar for bar in fresh if old.get(bar["time"]) != bar]
        cfg_changed = (prior or {}).get("cfg") != snapshot["cfg"]
        message = None
        if prior is None and self.rolled:
            # 换月后整张图替换, 不能按时间合并后留下新月份没有的旧 K 线。
            # 断线重连后的第一份照常按时间合并: 整份替换会让页面清掉足迹、整张重画。
            message = {"type": "snapshot", **snapshot}
        elif changed or cfg_changed:
            message = {"type": "bars", "symbol": self.symbol, "ltf": ltf,
                       "tf": self.tf, "revision": revision, "bars": changed}
            if cfg_changed:
                message["cfg"] = snapshot["cfg"]
        return snapshot, message


class FuturesRollupFeed(RollupMixin, Feed):
    """期货大周期: 只订阅该周期的原生 K 线(开高低收量), 买卖量由同合约 30s 合成(见 rollup.py)。

    不订阅 tick, 不做判向; base_lookup 返回同合约的 30s Feed(由 FeedManager 一并建好)。
    """

    def __init__(self, symbol: str, tf: int, base_lookup):
        super().__init__(symbol, tf)
        self._init_rollup(base_lookup, SNAPSHOT_BARS)

    def subscribe(self, api: TqApi, contract: str | None = None):
        self._bind_contract(contract or data_contract(api, self.symbol))
        self.klines = api.get_kline_serial(self.contract, self.tf, data_length=MAX_KLINES)
        with self._state_lock:
            self.snapshots.clear()
            self.ready.clear()
            self.error = None
            self.retry_at = None
            self.compute_retry_at = 0.0
            self._computed_version = -1

    def has_data(self) -> bool:
        base = self.base()
        return (self.klines is not None and len(self.klines) > 0
                and base is not None and base.contract == self.contract)

    def data_changing(self, api) -> bool:
        """原生 K 线在变, 或底层 30s 又算了一轮(revision 前进)。"""
        base = self.base()
        return api.is_changing(self.klines) or (base is not None and base.revision != self.base_revision)

    def candles(self, base) -> pd.DataFrame:
        return candles_from_klines(self.klines).tail(self.max_bars).reset_index(drop=True)


def _query_cache(api):
    """tqsdk 保存合约查询结果的表(api._data["symbols"]); 取不到时返回 None。

    这是 tqsdk 的内部结构, 所以只用来清理自检留下的条目, 结构变了就放弃清理, 不影响自检。
    """
    data = getattr(api, "_data", None)
    cache = data.get("symbols") if isinstance(data, Mapping) else None
    return cache if isinstance(cache, MutableMapping) else None


@contextmanager
def transient_queries(api):
    """这段里新发的合约查询用完就从连接缓存里删掉(见 _query_cache)。

    query_symbol_info 每次都发新请求, 结果按请求留在长连接的缓存里(约 5KB 一条): 周期性的
    查询(连接自检、主力核对、合约名、合约目录)不删, 内存就随连接寿命一直涨。query_quotes 的
    过滤条件只有固定几组, 留在缓存里正好省掉往返(见 listed_symbols), 不要包进来。
    """
    before = set(_query_cache(api) or ())
    try:
        yield
    finally:
        cache = _query_cache(api)
        for key in set(cache or ()) - before:
            cache.pop(key, None)


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
        # 主力映射(各处共用, 见 MainContracts); main_contract_error 是在采主连最近没核对上的原因
        self.mains = MainContracts()
        self.main_contract_error: str | None = None
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

    def serves(self, symbol: str, tf: int, feed: Feed) -> bool:
        """页面等快照期间这个 key 是否仍由 feed 供数: 固定月份可能改借同月份主力的 Feed(见 _retire),
        借用的主力也可能换月或订阅失败而去掉别名(见 _drop_aliases); 不是了就要重新 ensure。"""
        with self._lock:
            return self.feeds.get(feed_key(symbol, tf)) is feed

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
            # 与 HTTP 超时取消原子地交接状态: 先检查 cancelled() 再 set_exception
            # 仍有竞态; 取得执行权后取消便不会再成功。
            if future.set_running_or_notify_cancel():
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

    def _check_main_contracts(self, api):
        """持续采集也定期核对主力(映射表到期才真的查, 见 MainContracts); 换月后重建连接,
        释放旧月份订阅并重取整段历史。自选、模拟交易先查到的换月也在这里生效。"""
        with self._lock:
            feeds = [feed for feed in self._subscribed.values() if is_main(feed.symbol)]
        if not feeds:
            return
        symbols = list(dict.fromkeys(feed.symbol for feed in feeds))
        started = self._busy_since = time.monotonic()
        try:
            mains = self.mains.resolve(api, symbols)
        finally:
            self._busy_since = None
        if time.monotonic() - started >= JOB_REBUILD_SEC:
            self._rebuild_api.set()     # 一次往返慢到这个程度只会是连接已经不可用
        # 某个主连没核对上只影响它自己(沿用上次的月份), 其它照常检查换月
        errors = self.mains.errors(symbols)
        self.main_contract_error = "; ".join(errors.values()) or None
        changed = [feed for feed in feeds if feed.symbol in mains and feed.contract != mains[feed.symbol]]
        if not changed:
            return
        changes = sorted({f"{feed.symbol}: {feed.contract} -> {mains[feed.symbol]}" for feed in changed})
        with self._lock:
            for feed in changed:
                # 先换成新月份(清掉旧月份的缓存), 借用它的旧月份页面重连时才不会又借回来
                feed._bind_contract(mains[feed.symbol])
                self._drop_aliases(feed)
                with feed._state_lock:
                    feed.error = f"主力换月，正在切换到 {mains[feed.symbol]}"
        self._rebuild_api.set()
        print(f"[ingest] 主力换月，重建订阅: {', '.join(changes)}", flush=True)

    def _contract_for(self, api, symbol: str) -> str:
        """采集线程里调用: 订阅用的实际合约。先确认存在(见 listed_symbols); 主连用共用映射里的当前月份,
        同一主连的几个周期一起重订时只查一次。"""
        ensure_listed(api, symbol)
        if not is_main(symbol):
            return symbol
        contract = self.mains.resolve(api, [symbol]).get(symbol)
        if contract is None:
            raise RuntimeError(self.mains.errors([symbol]).get(symbol) or f"主力合约 {symbol} 的当前月份尚未就绪")
        return contract

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
            with transient_queries(api):
                rows = api.query_symbol_info([PROBE_SYMBOL])
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
            if feed is None:
                feed = self._main_peer(key)
                if feed is not None:
                    self.feeds[key] = feed
            if feed is not None:
                feed.request(demand=True)
            self._prune(now)
            if feed is None:
                # 大周期的买卖量由同合约 30s 合成: 底层没在采集就一并建上, 名额要按两路算,
                # 否则底层建好了大周期却建不了, 留下一路没人要的订阅
                base_key = (key[0], ROLLUP_BASE_TF)
                needed = 2 if (is_rollup(key[1]) and base_key not in self.feeds
                               and self._main_peer(base_key) is None) else 1
                if len(self._feed_list()) + needed > MAX_FEEDS:
                    raise ValueError("订阅合约数量已达上限")
                if is_rollup(key[1]):
                    self.ensure(*base_key)
                feed = self._new_feed(key)
                feed.request(demand=True)
                self.feeds[key] = feed
                self.cmd_q.put(key)
            return feed

    def _new_feed(self, key: tuple[str, int]) -> Feed:
        """同一月份的各原生周期共用大额检测器, 在订阅好、知道实际月份之后接上(见 _run_api_attempt)。"""
        if not is_rollup(key[1]):
            return Feed(*key)
        base_key = (key[0], ROLLUP_BASE_TF)
        return FuturesRollupFeed(key[0], key[1], lambda: self.feeds.get(base_key))

    # 同一月份同一周期默认只采集、落盘一份: 固定月份的 key 指向同月份主力 Feed(别名), 推送时换回页面代码。
    # 大额门槛不同则分别计算, 历史文件按已有记录去重。主力换月或订阅失败时去掉别名, 固定月份另建自己的。

    def _feed_list(self) -> list[Feed]:
        """必须持有 self._lock。self.feeds 里各不相同的 Feed(别名只算一个)。"""
        return list({id(feed): feed for feed in self.feeds.values()}.values())

    def _keys_of(self, feed: Feed) -> list[tuple[str, int]]:
        """必须持有 self._lock。指向 feed 的全部 key: 它自己的, 加上借用它的固定月份。"""
        return [key for key, value in self.feeds.items() if value is feed]

    def _main_peer(self, key: tuple[str, int]) -> Feed | None:
        """必须持有 self._lock。同周期、同月份且大额门槛相同的主力 Feed, 没有为 None。"""
        symbol, tf = key
        if is_main(symbol):
            return None
        peer = next((feed for feed in self.feeds.values()
                     if is_main(feed.symbol) and feed.tf == tf and feed.contract == symbol
                     and feed_key(feed.symbol, feed.tf) not in self.failed_feeds), None)
        if peer is None:
            return None
        sources = peer.large_sources()
        wanted = LargeTradeDetector(symbol).settings
        return peer if sources and all(source.settings == wanted for source in sources) else None

    def _drop_aliases(self, feed: Feed, keep=()):
        """必须持有 self._lock。去掉借用 feed 的固定月份(keep 除外), 让这些页面重连; 常驻的、大周期还在用的
        30s 底层下一轮另建(见 _ensure_pinned)。"""
        own = feed_key(feed.symbol, feed.tf)
        dropped = {key for key in self._keys_of(feed) if key != own and key not in keep}
        for key in dropped:
            del self.feeds[key]
        self._resync(dropped)

    def _retire(self, feed: Feed):
        """必须持有 self._lock。固定月份的 Feed 让位给同月份的主力 Feed, 不再采集。
        行情序列在同一连接上与主力是同一份(TqSdk 按合约复用), 不必重建连接。"""
        key = feed_key(feed.symbol, feed.tf)
        self._subscribed.pop(key, None)
        self.failed_feeds.pop(key, None)

    def _adopt(self, feed: Feed):
        """必须持有 self._lock。主力 Feed 刚订阅好(当前月份已知): 去掉不再是当前月份的别名;
        同月份已有一份固定月份的 Feed 时把它的 key 指过来, 页面重连后由主力供数。"""
        if not is_main(feed.symbol):
            return
        key = (feed.contract, feed.tf)
        self._drop_aliases(feed, keep={key})
        other = self.feeds.get(key)
        if other is not None and other is not feed and self._main_peer(key) is feed:
            self._retire(other)
            self.feeds[key] = feed
            self._resync({key})

    def _resync(self, keys):
        """必须持有 self._lock。这些 key 上的页面改由另一个 Feed 供数: 让它们重连、重取快照。"""
        if keys and self.loop is not None:
            self.loop.call_soon_threadsafe(self._resync_clients, set(keys))

    def _resync_clients(self, keys):
        with self._lock:
            clients = [(q, subscription) for q, subscription in self.clients.items()
                       if subscription[:2] in keys]
        for q, subscription in clients:
            self._send_resync(q, subscription)

    def set_pinned(self, keys) -> list[tuple[str, int]]:
        """设置常驻采集集合: 关掉所有页面也照常订阅、计算并落盘。

        keys 为 (合约, 主周期), 按优先级排列; 超出 MAX_PINNED_FEEDS 的部分不常驻,
        在状态接口里列出。移出集合的 Feed 回到按真实需求闲置回收的规则, 闲置期从移出时起算。
        """
        # 大周期由 30s 合成, 不单独常驻
        ordered = list(dict.fromkeys(feed_key(validate_symbol(symbol), tf) for symbol, tf in keys
                                     if not is_rollup(tf)))
        with self._lock:
            # 固定月份正是某个主力当前的月份时两者共用一个 Feed(见 _main_peer), 只占一个名额
            shared = {(feed.contract, feed.tf): feed_key(feed.symbol, feed.tf) for feed in self._feed_list()
                      if is_main(feed.symbol) and feed.contract != feed.symbol
                      and self._main_peer((feed.contract, feed.tf)) is feed}
            pinned, skipped, slots = [], [], set()
            for key in ordered:
                slot = shared.get(key, key)
                if slot in slots or len(slots) < MAX_PINNED_FEEDS:
                    slots.add(slot)
                    pinned.append(key)
                else:
                    skipped.append(key)
            # 常驻期间没人看图时闲置时长从创建起算, 早已超过回收期; 不重新计时, 下一轮就会
            # 被回收, 而回收已订阅的 Feed 要重建整条连接, 所有图表一起断几秒。
            for key in set(self.pinned) - set(pinned):
                feed = self.feeds.get(key)
                if feed is not None:
                    feed.request(demand=True)
            self.pinned = pinned
            self.pin_skipped = skipped
            self._ensure_pinned()
            return list(self.pinned)

    def _ensure_pinned(self):
        """补建缺失的常驻 Feed, 以及大周期还在用、却没了的 30s 底层(借用的主力换月或订阅失败时别名被去掉,
        大周期的页面不会因此重连, 没人补就再也合成不出来); 池子满时留到页面 Feed 回收后的下一轮。"""
        with self._lock:
            bases = [(feed.symbol, ROLLUP_BASE_TF) for feed in self._feed_list() if getattr(feed, "is_rollup", False)]
            for key in dict.fromkeys([*self.pinned, *bases]):
                if key in self.feeds:
                    continue
                peer = self._main_peer(key)
                if peer is not None:
                    self.feeds[key] = peer
                elif len(self._feed_list()) < MAX_FEEDS:
                    self.feeds[key] = self._new_feed(key)

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
            # 借用它的固定月份不跟着主力一起等冷却: 让它们另建自己的 Feed
            self._drop_aliases(feed)

    def status_snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            busy = None if self._busy_since is None else round(now - self._busy_since, 1)
            return {"status": self.status, "lastError": self.last_error,
                    "feeds": sorted(feed_label(feed_key(feed.symbol, feed.tf)) for feed in self._feed_list()),
                    # 借用主力 Feed 的固定月份: 别名 -> 实际供数的 Feed
                    "aliases": {feed_label(key): feed_label(feed_key(feed.symbol, feed.tf))
                                for key, feed in self.feeds.items() if key != feed_key(feed.symbol, feed.tf)},
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
                    "probeError": self.probe_error,
                    "mainContracts": {feed.symbol: feed.contract for feed in self._subscribed.values()
                                      if is_main(feed.symbol)},
                    "mainContractError": self.main_contract_error}

    def _prune(self, now=None):
        """回收无人再要的 Feed, 否则 MAX_FEEDS 只增不减, 反复试错合约就会占满池子。

        自动重试不算真实需求；失败 Feed 也按最后一次真实请求起算，避免重试不断续命。
        常驻采集的 Feed 不回收。要回收已订阅的 Feed 时, 把 EVICT_BATCH_SEC 内也会到期的
        已订阅 Feed 一起回收, 合并成一次连接重建。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            busy = {key[:2] for key in self.clients.values()} | set(self.pinned)
            # 按 Feed 判定: 主力和借用它的固定月份有一个还在用, 就整个留着
            owned = {}
            for key, feed in self.feeds.items():
                owned.setdefault(feed, []).append(key)
            idle = {feed: feed.idle_for(now) for feed, keys in owned.items()
                    if busy.isdisjoint(keys) and not feed.has_demand(now)}
            expired = {feed for feed, seconds in idle.items() if seconds >= IDLE_EVICT_SEC}
            due = self._without_kept_bases(expired)
            # 先排除留下的底层再判断: 否则一路被留下的底层每轮都会让其它订阅提前回收
            if any(feed in self._sdk_feeds for feed in due):
                soon = {feed for feed, seconds in idle.items()
                        if seconds >= IDLE_EVICT_SEC - EVICT_BATCH_SEC and feed in self._sdk_feeds}
                due = self._without_kept_bases(expired | soon)
            for feed in due:
                for key in owned[feed]:
                    self._forget(key)

    def _without_kept_bases(self, feeds):
        """必须持有 self._lock。大周期还留着, 它的 30s 底层就不能回收(只开着 WS 时底层收不到真实请求);
        与大周期同一轮回收的底层照常一起回收, 合并成一次连接重建。"""
        kept = {feed.base() for feed in self._feed_list() if getattr(feed, "is_rollup", False) and feed not in feeds}
        return {feed for feed in feeds if feed not in kept}

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
        origin = (msg.get("symbol"), msg.get("tf"))
        with self._lock:
            source = self.feeds.get(origin)
            clients = [(q, subscription, self.feeds.get(subscription[:2]))
                       for q, subscription in self.clients.items()]
        for q, subscription, target in clients:
            # 按 Feed 认订阅: 借用主力 Feed 的固定月份也收得到, 代码换回页面自己的(见 _main_peer)
            if (target is not source) if source is not None else origin != subscription[:2]:
                continue
            if msg.get("type") in {"footprints", "footprint_snapshot"}:
                if not subscription[3]:
                    continue
            elif msg.get("ltf") != subscription[2]:
                continue
            try:
                q.put_nowait(msg if subscription[0] == origin[0] else {**msg, "symbol": subscription[0]})
            except asyncio.QueueFull:
                self._send_resync(q, subscription)

    def _send_resync(self, q: asyncio.Queue, subscription):
        """不能静默移除后继续心跳。通知 WS 关闭，让客户端重连并重取快照。"""
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
            peer = self._main_peer(key)
            if peer is not None:
                # 主力已经在采这个月份: 借用它, 不再单独订阅
                self._retire(feed)
                self.feeds[key] = peer
                self._resync({key})
                return False
            entry = self.failed_feeds.get(key)
            if entry is not None and time.monotonic() < entry["retry_at"]:
                return False
            # 必须在调用 SDK 前标记，部分订阅失败也可能留下 SDK 缓存。
            self._sdk_feeds.add(feed)
        try:
            feed.subscribe(api, self._contract_for(api, feed.symbol))
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
            if not getattr(feed, "is_rollup", False):
                # 同一实际月份可以从主力入口或固定月份进入, 多周期共用大额检测器。主连代码另配了门槛时
                # 两边门槛不同, 各用各的: 否则谁先订阅就用谁的门槛, 主连的配置会被固定月份盖掉。
                peer = next((other for other in self.feeds.values()
                             if other is not feed and not getattr(other, "is_rollup", False)
                             and other.contract == feed.contract
                             and other.large_trades.settings == feed.large_trades.settings), None)
                if peer is not None:
                    feed.large_trades = peer.large_trades
            self._subscribed[key] = feed
            self.failed_feeds.pop(key, None)
            self._adopt(feed)
        return True

    def _sweep_subscriptions(self, api):
        """唯一的重试入口：每轮检查未订阅实例与冷却时间，无须页面请求或异步定时器。"""
        with self._lock:
            pending = [feed for feed in self._feed_list()
                       if self._subscribed.get(feed_key(feed.symbol, feed.tf)) is not feed]
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
            self._check_main_contracts(api)
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
                # 大周期排在最后: 它由同合约 30s 合成, 要用到 30s 本轮刚算出的窗口
                feeds = sorted(self._feed_list(), key=lambda f: getattr(f, "is_rollup", False))
                # 按 key 找 Feed: 借用主力 Feed 的固定月份页面也要登记它的拆分粒度与足迹
                clients = [(self.feeds.get((symbol, tf)), ltf, footprint)
                           for symbol, tf, ltf, footprint in self.clients.values()]
            for feed, ltf, footprint in clients:
                if feed is not None:
                    feed.request(ltf=ltf, footprint=footprint, demand=False)
            watched = {feed for feed, _, _ in clients if feed is not None}
            # 看着大周期, 也就是看着合成它的 30s: 30s 要跟着每笔算, 大周期才每笔都更新
            watched |= {feed.base() for feed in feeds if getattr(feed, "is_rollup", False) and feed in watched}
            for feed in feeds:
                if self._stop.is_set() or self._rebuild_api.is_set():
                    break
                with self._lock:
                    subscribed = self._subscribed.get(feed_key(feed.symbol, feed.tf)) is feed
                if not subscribed or not feed.has_data():
                    continue
                if time.monotonic() < feed.compute_retry_at:
                    continue
                try:
                    feed.ensure_ltf_subscriptions(api)
                    with feed._state_lock:
                        changed = feed.data_changing(api)
                        stale = (not feed.snapshots or feed.error is not None or
                                 feed._computed_version != feed._demand_version)
                    now = time.monotonic()
                    if changed:
                        # 有数据在流动 = 连接肯定是活的, 自检可以往后推
                        self._last_data_at = now
                        feed.pending = True
                    # 没人看的攒一攒再算(is_changing 只管这一轮, 所以记着 pending); 新需求、出过错的马上算
                    due = feed.pending and (feed in watched
                                            or now - feed.last_compute >= IDLE_RECOMPUTE_SEC)
                    if stale or due:
                        feed.recompute(self.broadcast)
                        feed.pending = False
                        feed.last_compute = time.monotonic()
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
