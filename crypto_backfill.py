# -*- coding: utf-8 -*-
"""加密行情回填: 用交易所的每日归档包与 REST 补上"洞"(见 crypto_feed.TradeBars)。

一个洞 = 两笔已有成交之间缺了的编号(本次运行第一笔之前的洞左端没有已知成交, 从回填窗口起点
按时间补)。分工:

- 整个归档日都在洞里、且归档包已经发布 -> 下载归档包(币安按 UTC 切日、带 SHA256 校验;
  OKX 按北京时间切日)。不占 API 限额, 一次就是一整天;
- 其余部分(最近一两天、归档包还没发布的那天、洞两端零头) -> REST 按整点小时一段段取;
- 由新到旧处理: 最近的先补完, 页面上能看到的窗口先变完整。

每补完一段就把其中整根的 bar(开高低收量、主动买卖量、各拆分粒度)交给管理线程写盘; 洞两端
所在 bar 缺的那几笔最后一起交回去插进内存窗口。任何一段取不到(REST 不通、归档包还没发布), 这个洞
就算没补完, 管理线程过一会儿再派一次 —— 已经补好的 bar 那时会被跳过。

每个交易所一个回填线程(互不排队: OKX 用 REST 慢慢翻的时候, 币安的归档包照样下载), 只做网络与
计算, 不碰历史文件; 结果都经 results 队列交给管理线程。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pandas as pd

from crypto_feed import EDGE_MS, Instrument, aggregate_trades
from indicator import TF_OPTIONS, ltf_options

USER_AGENT = "FlowScope/1.0"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# 回填多少天: 第一次启动从这么久以前补起, 之后每次启动只补其中还不完整的 bar。
BACKFILL_DAYS = int(os.getenv("CRYPTO_BACKFILL_DAYS", "7") or 7)
BLOCKED_RETRY_SEC = 3600       # REST 被拒(451/403)后多久再试
RATE_LIMIT_RETRIES = 5


class FetchError(Exception):
    """取数失败; 消息直接进状态接口。"""


class NotPublished(FetchError):
    """归档包还没发布(404)。"""


class RestBlocked(FetchError):
    """REST 在本地区不可用(451 / 403)。"""


class RateLimited(FetchError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class Http:
    """标准库 urllib 的 GET(认 HTTPS_PROXY 等环境变量); 只在回填线程里用。"""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def get(self, url: str, params: dict | None = None) -> tuple[bytes, dict]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            if exc.code == 404:
                raise NotPublished(f"HTTP 404 {url}") from None
            if exc.code in (403, 451):
                raise RestBlocked(f"HTTP {exc.code}: {detail}") from None
            if exc.code in (418, 429):
                retry = exc.headers.get("Retry-After")
                raise RateLimited(f"HTTP {exc.code} 限频", float(retry) if retry else 60.0) from None
            raise FetchError(f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from None

    def json(self, url: str, params: dict | None = None):
        body, headers = self.get(url, params)
        return json.loads(body), headers


def rate_limited(call):
    """限频(429/418)就按 Retry-After 等一等再试, 最多 RATE_LIMIT_RETRIES 次。"""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return call()
        except RateLimited as exc:
            if attempt == RATE_LIMIT_RETRIES - 1:
                raise
            time.sleep(min(exc.retry_after, 120))
    return None


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame({"id": pd.Series(dtype="int64"), "price": pd.Series(dtype=float),
                         "qty": pd.Series(dtype=float), "t": pd.Series(dtype="int64"),
                         "sell": pd.Series(dtype=bool)})


@dataclass
class HoleJob:
    """补一个洞。时间两端都含: [start_ms, end_ms], 再按编号去掉洞外已有的成交。"""

    instrument: Instrument
    start_ms: int                  # 洞左端那笔成交的时间; 左边没有已知成交时是回填窗口起点
    end_ms: int                    # 洞右端那笔成交(第一笔已有的)的时间
    after_id: int | None           # 洞左边最后一笔已有成交的编号; None = 按时间从 start_ms 补起
    before_id: int                 # 洞右边第一笔已有成交的编号
    complete: dict[int, set[int]]  # 各主周期已经完整落盘的 bar 起点(毫秒), 这些不用再补

    @property
    def symbol(self) -> str:
        return self.instrument.symbol


class Backfiller:
    """回填: 每个交易所一个线程, 各自一次处理一个任务(一个洞, 或刷新合约列表)。"""

    def __init__(self, http: Http | None = None, days: int = BACKFILL_DAYS):
        self.http = http or Http()
        self.days = days
        self.adapters: dict = {}
        self.jobs: dict[str, queue.Queue] = {}
        self.results: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.current: dict[str, str] = {}    # 交易所 -> 正在做的任务
        self.last_error: str | None = None
        self.segments = 0                    # 本次运行补完的段数(诊断用)
        self.blocked: dict[str, tuple[float, str]] = {}   # 交易所 -> (REST 被拒到何时, 原因)

    def bind(self, adapters: dict):
        self.adapters = adapters
        self.jobs = {venue: queue.Queue() for venue in adapters}

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._run, args=(venue,), daemon=True,
                                          name=f"crypto-backfill-{venue}") for venue in self.jobs]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=5)

    def submit(self, job):
        """任务: ("instruments", 交易所) 或 HoleJob; 交给对应交易所的线程。"""
        venue = job[1] if isinstance(job, tuple) else job.instrument.venue
        self.jobs[venue].put(job)

    def job(self, instrument, after_id, after_ms, before_id, before_ms, complete, days=None) -> HoleJob:
        """洞 -> 回填任务; 左端没有已知成交时从回填窗口起点补起(往前 days 天, 对齐到最长主周期)。"""
        if after_id is None:
            edge = max(TF_OPTIONS) * 1000
            start = (before_ms - (self.days if days is None else days) * DAY_MS) // edge * edge
        else:
            start = after_ms
        return HoleJob(instrument, start, before_ms, after_id, before_id, complete)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {"current": dict(self.current), "queued": sum(jobs.qsize() for jobs in self.jobs.values()),
                    "lastError": self.last_error,
                    "segments": self.segments, "days": self.days,
                    "restBlocked": {venue: reason for venue, (until, reason) in self.blocked.items()
                                    if until > now}}

    def _run(self, venue: str):
        while not self._stop.is_set():
            try:
                job = self.jobs[venue].get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if isinstance(job, tuple) and job[0] == "instruments":
                    self._load_instruments(job[1])
                else:
                    self._fill(job)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self.last_error = message
                print(f"[crypto] 回填任务失败: {message}", flush=True)
                if isinstance(job, HoleJob):
                    self.results.put(("done", job.symbol, job.before_id, None, message))
            finally:
                with self._lock:
                    self.current.pop(venue, None)

    def _load_instruments(self, venue: str):
        adapter = self.adapters[venue]
        with self._lock:
            self.current[venue] = "合约列表"
        try:
            instruments = rate_limited(lambda: adapter.load_instruments(self.http))
        except FetchError as exc:
            with self._lock:
                self.last_error = f"{venue} 合约列表: {exc}"
            print(f"[crypto] {venue} 合约列表取不到, 先用内置的: {exc}", flush=True)
            return
        self.results.put(("instruments", venue, instruments))

    # ------------------------------------------------------------------ 补一个洞
    def segments_of(self, adapter, start: int, end: int):
        """把 [start, end) 切成段, 由新到旧: 整个归档日 -> ("day", 该日起点); 其余按整点小时 -> ("hour", None)。"""
        shift = adapter.archive_tz_ms        # 归档日相对 UTC 的偏移(OKX 按北京时间切日: 8 小时)
        segments = []
        cursor = start
        while cursor < end:
            day_start = (cursor + shift) // DAY_MS * DAY_MS - shift
            day_end = min(day_start + DAY_MS, end)
            if cursor == day_start and day_end == day_start + DAY_MS:
                segments.append(("day", day_start, cursor, day_end))
            else:
                segments.extend(self._hours(cursor, day_end))
            cursor = day_end
        return segments[::-1]

    @staticmethod
    def _hours(start: int, end: int):
        hours = []
        cursor = start
        while cursor < end:
            stop = min((cursor // HOUR_MS + 1) * HOUR_MS, end)
            hours.append(("hour", None, cursor, stop))
            cursor = stop
        return hours

    def _needed(self, job: HoleJob, start: int, end: int, edges: list[tuple[int, int]]) -> bool:
        """这一段里还有没补完的整根 bar, 或者有洞两端的成交要取。"""
        if any(start < hi and lo < end for lo, hi in edges):
            return True
        for tf in TF_OPTIONS:
            tf_ms = tf * 1000
            done = job.complete.get(tf, set())
            first = -(-start // tf_ms) * tf_ms
            if any(bar not in done for bar in range(first, end // tf_ms * tf_ms, tf_ms)):
                return True
        return False

    def _fill(self, job: HoleJob):
        adapter = self.adapters[job.instrument.venue]
        with self._lock:
            self.current[job.instrument.venue] = f"{job.symbol} 洞 #{job.before_id}"
        # 两端要插回内存的成交所在的时间段(各一根最长主周期的 bar)
        right = job.end_ms // EDGE_MS * EDGE_MS
        edges = [(right, job.end_ms + 1)]
        if job.after_id is not None:
            left = job.start_ms // EDGE_MS * EDGE_MS
            edges.append((job.start_ms, left + EDGE_MS))
        edge_frames = []
        failed = None
        pending = self.segments_of(adapter, job.start_ms, job.end_ms + 1)
        while pending:
            if self._stop.is_set():
                return
            kind, day, start, end = pending.pop(0)
            if not self._needed(job, start, end, edges):
                continue
            try:
                trades = self._fetch(adapter, job, kind, day, start, end)
            except NotPublished:
                if kind == "day":
                    pending[0:0] = self._hours(start, end)[::-1]   # 归档包还没发布: 这一天改走 REST
                    continue
                failed = failed or f"{adapter.venue} 数据还没发布"
                continue
            except FetchError as exc:
                failed = failed or str(exc)
                continue
            mask = (trades["t"] >= start) & (trades["t"] < end) & (trades["id"] < job.before_id)
            if job.after_id is not None:
                mask &= trades["id"] > job.after_id
            trades = trades[mask]
            for tf in TF_OPTIONS:
                bars = aggregate_trades(trades, tf, ltf_options(tf), start, end)
                skip = job.complete.get(tf, set())
                bars = bars[~bars.index.isin(skip)] if skip else bars
                if not bars.empty:
                    self.results.put(("bars", job.symbol, tf, bars))
            for lo, hi in edges:
                edge_frames.append(trades[(trades["t"] >= lo) & (trades["t"] < hi)])
            with self._lock:
                self.segments += 1
        if failed is not None:
            with self._lock:
                self.last_error = f"{job.symbol}: {failed}"
            self.results.put(("done", job.symbol, job.before_id, None, failed))
            return
        edge = pd.concat(edge_frames, ignore_index=True) if edge_frames else empty_trades()
        edge = edge.drop_duplicates("id").sort_values("id")
        trades = list(edge[["id", "price", "qty", "t", "sell"]].itertuples(index=False, name=None))
        self.results.put(("done", job.symbol, job.before_id, trades, None))

    def _fetch(self, adapter, job: HoleJob, kind, day, start: int, end: int) -> pd.DataFrame:
        if kind == "day":
            return rate_limited(lambda: adapter.archive_trades(self.http, job.instrument, day))
        blocked = self.blocked.get(adapter.venue)
        if blocked is not None and blocked[0] > time.monotonic():
            raise RestBlocked(blocked[1])
        after_id = job.after_id if start <= job.start_ms else None
        before_id = job.before_id if end > job.end_ms else None
        try:
            return rate_limited(lambda: adapter.rest_trades(self.http, job.instrument, start, end,
                                                            after_id, before_id))
        except RestBlocked as exc:
            reason = f"{adapter.venue} REST 不可用({exc})"
            with self._lock:
                self.blocked[adapter.venue] = (time.monotonic() + BLOCKED_RETRY_SEC, reason)
            raise RestBlocked(reason) from None
