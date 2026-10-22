"""按时间向前翻页。只在所属行情线程调用, 与实时采集共用历史索引和计算口径。

加密从已落盘的完整 OHLC 历史读取; 期货读取 SDK 已订阅的 K 线窗口。
分页不扩大实时计算窗口、不写回历史、不把分页结果当成实时快照。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from crypto_aggregate import AggregateFeed
from crypto_feed import CryptoFeed, CryptoRollupFeed
from indicator import BAR_COLUMNS, bars_to_records
from large_trades import decorate_bars
from rollup import CANDLE_COLUMNS, INT64_MIN, candles_from_klines, finish_bars, utc_bucket


def _select(frame, before, limit=None, since=INT64_MIN):
    frame = frame[(frame.time >= since) & (frame.time < before)]
    frame = frame.drop_duplicates("time", keep="last").sort_values("time")
    return (frame.tail(limit) if limit is not None else frame).reset_index(drop=True)


def _native_candles(feed, before, limit=None, since=INT64_MIN):
    if not isinstance(feed, CryptoFeed):
        return _select(candles_from_klines(feed.klines), before, limit, since)
    frames = []
    if feed.candles is not None:
        feed.candles.refresh()
        start = since
        if limit is not None:
            times = feed.candles.starts_before(before, limit)
            start = max(start, times[0] if times else before)
        frames.append(feed.candles.rows(start, before))
    # 包含内存窗口里尚未落盘的 bar, 同一时间优先采用它。
    frames.append(feed.trades.frame(0)[CANDLE_COLUMNS])
    return _select(pd.concat(frames, ignore_index=True), before, limit, since)


def _native_bars(feed, candles, ltf):
    bars = candles.reindex(columns=BAR_COLUMNS).copy()
    bars["coverage"], bars["hasBaseline"] = "missing", False
    recent = feed.latest_window(ltf)
    if recent is not None and not recent.empty:
        recent = recent[recent.time.isin(bars.time)].reindex(columns=BAR_COLUMNS)
        bars = pd.concat([bars, recent], ignore_index=True).drop_duplicates("time", keep="last")
        bars = bars.sort_values("time").reset_index(drop=True)
    store = feed._store(ltf)
    store.refresh()
    return store.merge(bars)


def _aggregate_bars(feed, source, before, limit, ltf):
    parts = [source.feeds.get((symbol, feed.tf)) for symbol in feed.components]
    if parts[0] is None:
        raise RuntimeError("历史数据尚未就绪，请重试")
    candles = _native_candles(parts[0], before, limit)
    if candles.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    frames = [_native_bars(parts[0], candles, ltf).set_index("time")]
    for part in parts[1:]:
        frames.append(None if part is None else _native_bars(part, _native_candles(
            part, before, since=int(candles.time.iloc[0])), ltf).set_index("time"))
    return feed.combine_frames(frames)


def _rollup_candles(feed, before, limit):
    if not isinstance(feed, CryptoRollupFeed):
        return _select(candles_from_klines(feed.klines), before, limit)
    parts = feed.candle_feeds()
    if not parts or parts[0] is None:
        raise RuntimeError("历史数据尚未就绪，请重试")
    primary = parts[0]
    starts = set()
    if primary.candles is not None:
        primary.candles.refresh()
        starts.update(primary.candles.starts_before(before, limit, feed.tf))
    recent = primary.latest_window(0)
    if recent is not None:
        starts.update(utc_bucket(int(t), feed.tf) for t in recent.time if utc_bucket(int(t), feed.tf) < before)
    return feed.candles_at(np.array(sorted(starts)[-limit:], dtype=np.int64), parts)


def read_history_page(source, symbol, tf, ltf, before, limit, since=None):
    """返回严格早于 before 的至多 limit 根, 多查一根判断是否还有历史。"""
    feed = source.ensure(symbol, tf)
    feed.request(ltf=ltf)
    if getattr(feed, "is_rollup", False):
        base = feed.base()
        if base is None:
            raise RuntimeError("历史数据尚未就绪，请重试")
        candles = _rollup_candles(feed, before, limit + 1)
        stats = feed.base_stats(base, ltf, candles) if not candles.empty else None
        if not candles.empty and stats is None:
            raise RuntimeError("历史数据尚未就绪，请重试")
        bars = finish_bars(candles, stats, tf, feed.rule, closed=True) if stats is not None else pd.DataFrame(columns=BAR_COLUMNS)
        store = base._store(ltf)
    else:
        if isinstance(feed, AggregateFeed):
            bars = _aggregate_bars(feed, source, before, limit + 1, ltf)
        else:
            bars = _native_bars(feed, _native_candles(feed, before, limit + 1), ltf)
        store = feed._store(ltf)
        store.refresh()
        bars = store.merge(bars)
    if since is not None:
        bars = bars[bars.time >= since]
    more = len(bars) > limit
    store.refresh()
    records = bars_to_records(store.with_cvd(bars.tail(limit)))
    decorate_bars(records, feed.large_sources(), tf)
    history_source = feed.base() if getattr(feed, "is_rollup", False) else feed
    return {"symbol": symbol, "contract": feed.contract, "tf": tf, "ltf": ltf,
            "bars": records, "hasMore": more, "nextBefore": records[0]["time"] if records else None,
            "cvdBase": store.base, "historyRevision": getattr(history_source, "history_revision", 0),
            "historyBackfill": getattr(history_source, "history_backfill", {}).get(ltf)}
