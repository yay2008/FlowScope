"""期货小周期历史下载。下载连接独占后台线程, 落盘和重算仍由行情线程执行。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from indicator import TZ_SHIFT_S

FIELDS = ["open", "high", "low", "close", "volume"]
CHUNK_SECONDS = 7 * 86400
DOWNLOAD_TIMEOUT = 120


def read_download(path, contract):
    """DataDownloader CSV -> 与 SDK 实时 K 线相同的列; 纳秒列保持整数精度。"""
    frame = pd.read_csv(path, dtype={"datetime_nano": "Int64"})
    names = {"datetime_nano": "datetime", **{f"{contract}.{key}": key for key in FIELDS}}
    frame = frame.rename(columns={"datetime": "datetime_text", **names})
    frame = frame.reindex(columns=["datetime", *FIELDS]).apply(pd.to_numeric, errors="coerce").dropna()
    frame["datetime"] = frame["datetime"].astype("int64")
    frame = frame[(frame.datetime > 0) & (frame.volume >= 0)]
    return frame.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)


def download_klines(contract, tf, ltf, since, before, stopped):
    """下载一个有界区间的原生主周期和小周期 K 线, 不接触 Feed 或历史文件。

    since/before 是前端使用的北京时间展示秒, 下载区间为 [since, before)。
    两种周期来自独立原生 K 线, 之后可以核对成交量, 而非把缺失的小 K 线补成零。
    """
    from tqsdk import TqApi, TqAuth
    from tqsdk.tools import DataDownloader

    user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
    if not user or not password:
        raise RuntimeError("未设置 TQ_USER/TQ_PASS")
    if stopped.is_set():
        raise RuntimeError("历史回补已停止")
    start = datetime.fromtimestamp(since - TZ_SHIFT_S, timezone.utc)
    end = datetime.fromtimestamp(before - TZ_SHIFT_S, timezone.utc)
    api = None
    try:
        api = TqApi(auth=TqAuth(user, password), debug=False)
        with tempfile.TemporaryDirectory(prefix="flowscope-kline-") as folder:
            paths = {seconds: Path(folder) / f"{seconds}.csv" for seconds in {tf, ltf}}
            try:
                tasks = [DataDownloader(api, symbol_list=contract, dur_sec=seconds,
                                        start_dt=start, end_dt=end, csv_file_name=str(path))
                         for seconds, path in paths.items()]
            except Exception as exc:
                if "不支持下载历史数据" not in str(exc):
                    raise
                return recent_klines(api, contract, tf, ltf, since, stopped)
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT
            while not all(task.is_finished() for task in tasks):
                if stopped.is_set():
                    raise RuntimeError("历史回补已停止")
                if time.monotonic() >= deadline:
                    raise RuntimeError("历史 K 线下载超时，请重试")
                api.wait_update(deadline=time.time() + 1)
            frames = {seconds: read_download(path, contract) for seconds, path in paths.items()}
            for seconds, frame in frames.items():
                stamps = frame.datetime // 10**9 + TZ_SHIFT_S
                frames[seconds] = frame[(stamps >= since) & (stamps + seconds <= before)].copy()
            if frames[tf].empty or frames[ltf].empty:
                raise RuntimeError("行情源未返回所需的小周期历史 K 线")
            return frames[tf], frames[ltf]
    finally:
        if api is not None:
            api.close()


def recent_klines(api, contract, tf, ltf, since, stopped):
    """无区间下载权限时仅使用公开实时序列接口, 扩到最大窗口并保留真实的历史边界。"""
    frames = {seconds: api.get_kline_serial(contract, seconds, data_length=10000) for seconds in {tf, ltf}}
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    while not all(api.is_serial_ready(frame) for frame in frames.values()):
        if stopped.is_set() or time.monotonic() >= deadline:
            raise RuntimeError("最近 K 线历史加载超时或已停止")
        api.wait_update(deadline=time.time() + 1)
    lower = frames[ltf].copy().dropna(subset=["datetime", "close"])
    lower = lower[lower.datetime > 0].sort_values("datetime")
    if len(lower) < 2:
        raise RuntimeError("行情源没有返回可用的小周期历史 K 线")
    # 最新一根仍可能形成。以其起点作为闭合边界, 第一根所在主周期允许被标为 partial。
    end = int(lower.datetime.iloc[-1]) // 10**9 + TZ_SHIFT_S
    available = int(lower.datetime.iloc[0]) // 10**9 + TZ_SHIFT_S
    start = max(since, available // tf * tf)
    main = frames[tf].copy().dropna(subset=["datetime", "close"])
    times = main.datetime // 10**9 + TZ_SHIFT_S
    main = main[(times >= start) & (times + tf <= end)].copy()
    lower = lower[(lower.datetime // 10**9 + TZ_SHIFT_S) < end].copy()
    main.attrs["historyLimit"] = {"limited": True, "availableSince": available,
                                  "reason": f"当前天勤账户仅能获取最近 10000 根 {ltf} 秒 K 线，更早历史需要区间下载权限"}
    return main, lower


class KlineBackfiller:
    """串行下载, 同一范围的并发请求共用 Future; HTTP 取消不取消其它页面的下载。"""

    def __init__(self, download=download_klines):
        self.download = download
        self.stopped = threading.Event()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kline-backfill")
        self.pending = {}
        self.lock = threading.Lock()

    async def fetch(self, contract, tf, ltf, since, before):
        key = (contract, tf, ltf, since, before)
        with self.lock:
            future = self.pending.get(key)
            if future is None:
                future = self.executor.submit(self.download, *key, self.stopped)
                self.pending[key] = future
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except Exception as exc:
            raise RuntimeError(f"{ltf} 秒历史下载失败: {exc}") from exc
        finally:
            with self.lock:
                if future.done() and self.pending.get(key) is future:
                    del self.pending[key]

    def stop(self):
        self.stopped.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
