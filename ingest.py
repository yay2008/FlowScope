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
import threading
import time

import pandas as pd
from tqsdk import TqApi, TqAuth

from indicator import CFG, build_bars, bars_to_records, finalize_bars

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_TICKS = 10000     # get_tick_serial 单次上限 10000 条 ≈ 83 分钟
MAX_KLINES = 2000     # 2000 根 30s K线 ≈ 2.5 个交易日
SNAPSHOT_BARS = 800   # 推送给前端的最近 bar 数


def _csv_key(symbol: str) -> str:
    return symbol.replace("@", "_").replace(".", "_")


class Feed:
    """单个合约的订阅、快照与 CSV 落盘"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.klines = None
        self.ticks = None
        self.ready = threading.Event()     # 首个快照就绪
        self.snapshot: dict | None = None
        self.ltf = 0                       # 买卖量拆分粒度(秒), 0 = 逐 tick 盘口判定
        self.dirty = False                 # ltf 被改动后置位, 驱动 ingest 线程重算
        self._last_tick_dt = None
        self._csv_last_time = 0
        self.csv_path = os.path.join(DATA_DIR, f"{_csv_key(symbol)}_30s.csv")

    def subscribe(self, api: TqApi):
        self.klines = api.get_kline_serial(self.symbol, 30, data_length=MAX_KLINES)
        self.ticks = api.get_tick_serial(self.symbol, data_length=MAX_TICKS)

    def _csv_history(self) -> pd.DataFrame | None:
        if not os.path.exists(self.csv_path):
            return None
        try:
            return pd.read_csv(self.csv_path)
        except Exception:
            return None

    def _append_csv(self, bars: pd.DataFrame):
        done = bars.iloc[:-1]                 # 最后一根 bar 未走完, 不落盘
        done = done[(done["time"] > self._csv_last_time) & done["buy"].notna()]
        if done.empty:
            return
        done[["time", "buy", "sell"]].to_csv(
            self.csv_path, mode="a", header=not os.path.exists(self.csv_path), index=False)
        self._csv_last_time = int(done["time"].iloc[-1])

    def recompute(self, broadcast):
        """在 ingest 线程内调用: 重算 bars -> 更新快照 -> 广播最新 bar -> 落盘"""
        bars = build_bars(self.klines, self.ticks, self.ltf)
        if bars.empty:
            return
        # CSV 是逐 tick 粒度口径, 只在 ltf=0 时用于填补/落盘, 避免混入粗粒度值
        if self.ltf == 0:
            csv = self._csv_history()
            if csv is not None and not csv.empty:
                old = dict(zip(csv["time"].astype("int64"), zip(csv["buy"], csv["sell"])))
                lack = bars["buy"].isna() & bars["time"].map(lambda t: t in old)
                if lack.any():
                    bars.loc[lack, "buy"] = bars.loc[lack, "time"].map(lambda t: old[t][0])
                    bars.loc[lack, "sell"] = bars.loc[lack, "time"].map(lambda t: old[t][1])
                    bars = finalize_bars(bars)
            self._append_csv(bars)

        recs = bars_to_records(bars.tail(SNAPSHOT_BARS))
        self.snapshot = {"symbol": self.symbol, "cfg": CFG, "ltf": self.ltf, "bars": recs}
        self.ready.set()
        self.dirty = False
        broadcast({"type": "bar", "symbol": self.symbol, "bar": recs[-1]})


class FeedManager:
    """管理全部订阅; wait_update 循环跑在独立守护线程"""

    def __init__(self):
        self.feeds: dict[str, Feed] = {}
        self.cmd_q: queue.Queue[str] = queue.Queue()
        self.clients: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        threading.Thread(target=self._run, daemon=True, name="tqsdk-ingest").start()

    def ensure(self, symbol: str) -> Feed:
        feed = self.feeds.get(symbol)
        if feed is None:
            feed = Feed(symbol)
            self.feeds[symbol] = feed
            self.cmd_q.put(symbol)
        return feed

    def broadcast(self, msg: dict):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict):
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.clients.discard(q)

    def _run(self):
        api = TqApi(auth=TqAuth(os.environ["TQ_USER"], os.environ["TQ_PASS"]))
        while True:
            # 处理新订阅命令(在 ingest 线程内调用 get_* 保证线程安全)
            try:
                while True:
                    self.feeds[self.cmd_q.get_nowait()].subscribe(api)
            except queue.Empty:
                pass
            api.wait_update(deadline=time.time() + 1)
            for feed in list(self.feeds.values()):
                if feed.ticks is None or len(feed.ticks) == 0:
                    continue
                last_dt = feed.ticks.iloc[-1]["datetime"]
                if feed.snapshot is None or feed.dirty or (pd.notna(last_dt) and last_dt != feed._last_tick_dt):
                    feed._last_tick_dt = last_dt
                    try:
                        feed.recompute(self.broadcast)
                    except Exception as exc:
                        print(f"[ingest] recompute {feed.symbol} 出错: {exc}", flush=True)
