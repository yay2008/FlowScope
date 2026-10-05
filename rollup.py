# -*- coding: utf-8 -*-
"""大周期(1 分钟及以上)由底层 30s 合成, 不单独采集。

为什么不单独采集: 期货每多一个周期就多占一路常驻采集, 而且新周期没有历史(tick 窗口只有
约 83 分钟); 加密每个周期要各存一份逐笔窗口与 1 秒小 bar, 回填还要按最长周期插回成交。
30s 的买卖量是可加的, 拆分粒度(1/5/10/15/30 秒)的小 bar 也不会横跨 30s 边界, 所以
大周期每根 bar 的买卖量就是其中各根 30s 之和, 与 30s 同一口径。

- 开高低收量: 期货用 TqSdk 原生的该周期 K 线(很深, 2000 根); 加密由 30s 开高低收合成。
  每根大周期 bar 的起点就是分桶的左边界, 期货按原生 K 线的起点切, 不假设 TqSdk 怎么对齐;
  30s bar 的起点落在 [起点, min(起点 + 周期, 下一根起点)) 里就归这根。期货 30s bar 覆盖
  (start, end] 的快照, 并起来正好是 (起点, 终点], 与原生 K 线的成交量同一口径。
- 买卖量: 底层最近窗口(已合并历史、带覆盖标记)优先, 更早的取底层历史文件, 优先级与
  HistoryStore.merge 一致(complete > partial > legacy)。
- 覆盖: 桶里一根 30s 都没有 -> missing; 期货要求各根都 complete 且成交量之和等于原生 K 线
  这根的成交量; 加密 7×24 连续、没成交的 30s 也补零量 bar, 直接数槽位齐不齐。
- CVD: 不另存历史, 直接用底层历史文件的累计和(HistoryStore.with_cvd 按桶起点取), 所以
  大周期每根 bar 末尾的 CVD 就是 30s 在同一时刻的 CVD。

只合成最近 max_bars 根(页面只显示这么多)。其中"已收尾"的桶(整根都早于底层最近窗口, 30s 全在
历史文件里)的结果按历史文件的改动记录增量维护(ClosedCache): 平时每轮只现算窗口里那几根, 历史文件
改了(回填、估算升级为核对通过)才从改动处往后重算。合成结果不落盘, 随时能从 30s 现算。
"""
from __future__ import annotations

import time
import weakref

import numpy as np
import pandas as pd

from indicator import BAR_COLUMNS, ROLLUP_BASE_TF, TZ_SHIFT_S, VOLUME_ROUND, ltf_options

BASE_SEC = ROLLUP_BASE_TF
SUM_COLUMNS = ["buy", "sell", "unknown", "buyLegacy", "sellLegacy"]
ROW_COLUMNS = ["time", *SUM_COLUMNS, "coverage", "hasBaseline", "volume"]
CANDLE_COLUMNS = ["time", "open", "high", "low", "close", "volume"]
# 每个桶的统计: 买卖量之和、30s 根数、其中核对通过/旧历史/有买卖量的根数、核对通过那几根的已知成交量、
# 有没有前置快照、最后一根 30s 的起点
STAT_COLUMNS = [*SUM_COLUMNS, "count", "complete", "legacy", "data", "known", "baseline", "last"]
INT64_MIN = np.iinfo(np.int64).min
INT64_MAX = np.iinfo(np.int64).max
COVERAGES = np.array(["complete", "partial", "legacy"], dtype=object)   # HistoryStore 的来源码 -> 覆盖
_NO_EXTRA = {}             # 没有对照列的行(只读)


def candles_from_klines(klines) -> pd.DataFrame:
    """TqSdk K 线(datetime 纳秒) -> 开高低收量表(time 为展示秒, 同 HistoryStore)。"""
    if klines is None:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    k = klines[["datetime", "open", "high", "low", "close", "volume"]].dropna(subset=["datetime", "close"])
    k = k[k["datetime"] > 0]
    frame = k[["open", "high", "low", "close", "volume"]].astype(float).copy()
    frame.insert(0, "time", (k["datetime"].astype("int64") // 10**9 + TZ_SHIFT_S).astype("int64"))
    return frame.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def bucket_ends(starts: np.ndarray, tf: int) -> np.ndarray:
    """各桶的终点: min(起点 + 周期, 下一根起点)。"""
    starts = np.asarray(starts, dtype=np.int64)
    return np.minimum(starts + tf, np.r_[starts[1:], INT64_MAX])


def _owners(times: np.ndarray, starts: np.ndarray, ends: np.ndarray):
    """每根 30s 落进哪个桶(下标); 不在任何桶里的为 -1。"""
    owner = np.searchsorted(starts, times, side="right") - 1
    inside = (owner >= 0) & (times < ends[np.clip(owner, 0, None)])
    return np.where(inside, owner, -1)


def _ohlcv(groups) -> pd.DataFrame:
    """按桶分组的 30s 开高低收量 -> 每桶的开高低收量、30s 根数与最后一根 30s 的起点。"""
    return pd.DataFrame({"open": groups["open"].first(), "high": groups["high"].max(),
                         "low": groups["low"].min(), "close": groups["close"].last(),
                         "volume": groups["volume"].sum().round(VOLUME_ROUND), "count": groups["volume"].size(),
                         "last": groups["time"].max()})


def candle_buckets(frame: pd.DataFrame, starts: np.ndarray, ends: np.ndarray) -> pd.DataFrame:
    """30s 开高低收量(time 为展示秒) -> 给定各桶的开高低收量、30s 根数与最后一根 30s 的起点(last);
    按桶起点索引, 每个桶一行(桶里没有 30s 的开高低收与 last 为 NaN、根数为 0)。"""
    index = pd.Index(np.asarray(starts, dtype=np.int64), name="time")
    if frame is None or frame.empty or not len(index):
        out = pd.DataFrame(np.nan, index=index, columns=["open", "high", "low", "close", "volume"])
        out["count"] = 0
        out["last"] = np.nan
        return out
    data = frame[CANDLE_COLUMNS].sort_values("time")
    owner = _owners(data["time"].to_numpy(dtype=np.int64), index.to_numpy(), np.asarray(ends, dtype=np.int64))
    data = data[owner >= 0]
    out = _ohlcv(data.groupby(index.to_numpy()[owner[owner >= 0]])).reindex(index)
    out["count"] = out["count"].fillna(0).astype(int)
    return out


def utc_bucket(t: int, tf: int) -> int:
    """展示秒 t 所在 tf 周期桶的起点(展示秒), 按交易所时间(UTC)整除对齐, 同交易所 K 线惯例。"""
    return (t - TZ_SHIFT_S) // tf * tf + TZ_SHIFT_S


def store_rows(store, since=INT64_MIN, before=INT64_MAX, volume_store=None) -> pd.DataFrame:
    """底层历史文件里 [since, before) 的 30s -> 每根一行; 买卖量、对照列与覆盖同 HistoryStore.merge。

    volume 列是这根 30s 已知的成交量, 只有期货的覆盖判定用得到: 来自 volume_store(tick 口径
    的历史文件)里核对通过的行, buy + sell + unknown 就是 K 线成交量; 其余为 NaN。
    买卖量与来源直接切 HistoryStore 的索引数组; 对照列与成交量存在按时间的字典里, 只能逐根取。
    """
    times, buy, sell, codes = store.display_between(since, before)
    if not len(times):
        return pd.DataFrame(columns=ROW_COLUMNS)
    keys = times.tolist()
    # 对照列: 主文件的优先, 其次旧估算文件的(同 HistoryStore._fill_extra); 旧历史本身就是旧算法口径
    extra, estimated_extra = store.extra, store.estimated_extra
    columns = [extra.get(t) or estimated_extra.get(t) or _NO_EXTRA for t in keys]
    unknown, buy_legacy, sell_legacy = (np.array([row.get(name, np.nan) for row in columns], dtype=float)
                                        for name in ("unknown", "buyLegacy", "sellLegacy"))
    legacy = codes == 2
    unknown[legacy], buy_legacy[legacy], sell_legacy[legacy] = 0.0, buy[legacy], sell[legacy]
    volume = np.full(len(keys), np.nan)
    if volume_store is not None:
        reference, reference_extra = volume_store.values, volume_store.extra
        known = [reference.get(t) for t in keys]
        volume = np.array([np.nan if k is None else
                           k[0] + k[1] + ((reference_extra.get(t) or _NO_EXTRA).get("unknown") or 0.0)
                           for t, k in zip(keys, known)], dtype=float)
    return pd.DataFrame({"time": times, "buy": buy, "sell": sell, "unknown": unknown, "buyLegacy": buy_legacy,
                         "sellLegacy": sell_legacy, "coverage": COVERAGES[codes], "hasBaseline": False,
                         "volume": volume}, columns=ROW_COLUMNS)


def recent_rows(recent: pd.DataFrame | None) -> pd.DataFrame:
    """底层最近窗口(Feed.latest_window) -> 同 store_rows 的列。"""
    if recent is None or recent.empty:
        return pd.DataFrame(columns=ROW_COLUMNS)
    rows = recent.reindex(columns=ROW_COLUMNS).copy()
    rows["hasBaseline"] = rows["hasBaseline"].fillna(False).astype(bool)
    return rows


def bucket_stats(rows: pd.DataFrame, starts: np.ndarray, ends: np.ndarray) -> pd.DataFrame:
    """30s 行 -> 给定各桶的统计(STAT_COLUMNS); 按桶起点索引, 每个桶一行(没有 30s 的根数为 0)。"""
    index = pd.Index(np.asarray(starts, dtype=np.int64), name="time")
    out = pd.DataFrame(index=index, columns=STAT_COLUMNS, dtype=float)
    if rows is None or rows.empty or not len(index):
        out[["count", "complete", "legacy", "data"]] = 0
        out["baseline"] = False
        return out
    t = rows["time"].to_numpy(dtype=np.int64)
    owner = _owners(t, index.to_numpy(), np.asarray(ends, dtype=np.int64))
    keep = owner >= 0
    key = index.to_numpy()[owner[keep]]
    rows = rows[keep]
    for column in SUM_COLUMNS:
        out[column] = pd.to_numeric(rows[column], errors="coerce").astype(float).groupby(key).sum(min_count=1)
    complete = rows["coverage"].eq("complete").to_numpy()
    flags = pd.DataFrame({"count": 1, "complete": complete, "legacy": rows["coverage"].eq("legacy").to_numpy(),
                          "data": pd.to_numeric(rows["buy"], errors="coerce").notna().to_numpy()}, index=key)
    out[["count", "complete", "legacy", "data"]] = flags.groupby(level=0).sum().reindex(index).fillna(0)
    volume = pd.Series(pd.to_numeric(rows["volume"], errors="coerce").astype(float).to_numpy(), index=key)
    out["known"] = volume.where(complete).groupby(level=0).sum(min_count=1)
    baseline = pd.Series(rows["hasBaseline"].fillna(False).astype(bool).to_numpy(), index=key)
    out["baseline"] = baseline.groupby(level=0).any().reindex(index, fill_value=False).astype(bool)
    out["last"] = pd.Series(t[keep], index=key).groupby(level=0).max()
    return out


def finish_bars(candles: pd.DataFrame, stats: pd.DataFrame, tf: int, rule: str = "volume") -> pd.DataFrame:
    """大周期开高低收量 + 各桶统计 -> 大周期 bars 表(BAR_COLUMNS, 不含 CVD)。

    rule="volume"(期货): 各根 30s 都 complete 且已知成交量之和等于这根的成交量才算完整;
    rule="slots"(加密): 桶里的 30s 槽位(最后一桶数到底层最新一根为止)齐全且都 complete 才算完整。
    """
    if candles is None or candles.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = candles[CANDLE_COLUMNS].reset_index(drop=True).copy()
    starts = out["time"].to_numpy(dtype=np.int64)
    ends = bucket_ends(starts, tf)
    s = stats.reindex(starts)
    for column in SUM_COLUMNS:
        out[column] = s[column].astype(float).round(VOLUME_ROUND).to_numpy()
    count = s["count"].fillna(0).to_numpy()
    complete = s["complete"].fillna(0).to_numpy()
    if rule == "slots":
        last = s["last"].astype(float)
        latest = int(last.max()) + BASE_SEC if last.notna().any() else int(starts[-1])
        expected = (np.minimum(ends, latest) - starts) // BASE_SEC
        whole = (complete == expected) & (expected > 0)
    else:
        whole = ((complete == count) & (count > 0)
                 & np.isclose(s["known"].astype(float).to_numpy(), out["volume"].to_numpy(dtype=float),
                              rtol=0, atol=1e-6))
    legacy_only = s["legacy"].fillna(0).to_numpy() == s["data"].fillna(0).to_numpy()
    out["coverage"] = np.where(out["buy"].isna(), "missing",
                               np.where(whole, "complete", np.where(legacy_only, "legacy", "partial")))
    out["hasBaseline"] = s["baseline"].fillna(False).astype(bool).to_numpy()
    out["delta"] = out["buy"] - out["sell"]
    out["deltaLegacy"] = out["buyLegacy"] - out["sellLegacy"]
    out["cvd"] = np.nan
    return out[BAR_COLUMNS]


class ClosedCache:
    """已收尾的桶(30s 全在历史文件里)的合成结果, 按来源历史文件的改动记录增量维护。

    sources 是有 revision / changed_since 的历史文件(HistoryStore、CandleStore)。来源换了实例就全部重算;
    来源改了, 从改动涉及的最早时间所在的桶往后作废; 新收尾的桶、新进窗口的桶现算后并进来。
    """

    def __init__(self):
        # 来源的弱引用: 认实例不能用 id(), 底层回收重建后新实例可能正好落在旧地址上, 版本号却从头数起
        self.sources = []
        self.revisions = []
        self.frame = None      # 按桶起点索引; _known 列标出算过的桶

    def _same_sources(self, sources) -> bool:
        return (len(self.sources) == len(sources)
                and all(ref() is source for ref, source in zip(self.sources, sources)))

    def get(self, sources, starts, ends, compute) -> pd.DataFrame:
        """starts/ends: 现在要的已收尾各桶; compute(lo, hi, starts, ends) 算出这些桶(每桶一行)。"""
        starts = np.asarray(starts, dtype=np.int64)
        ends = np.asarray(ends, dtype=np.int64)
        if not len(starts):
            return compute(0, 0, starts, ends)     # 一根已收尾的都没有: 给一张同列的空表
        if not self._same_sources(sources) or self.frame is None:
            known = np.zeros(len(starts), dtype=bool)
            frame = None
        else:
            changes = [source.changed_since(revision) for source, revision in zip(sources, self.revisions)]
            changes = [change for change in changes if change is not None]
            frame = self.frame.reindex(starts)
            known = frame["_known"].eq(True).to_numpy()
            if changes:
                known = known & (ends <= min(changes))
        if not known.all():
            missing = np.flatnonzero(~known)
            fresh = compute(int(starts[missing[0]]), int(ends[missing[-1]]), starts[missing], ends[missing])
            fresh["_known"] = True
            parts = [fresh] if frame is None or not known.any() else [frame[known], fresh]
            frame = pd.concat(parts).sort_index() if len(parts) > 1 else fresh
        self.sources = [weakref.ref(source) for source in sources]
        self.revisions = [source.revision for source in sources]
        self.frame = frame
        return frame.drop(columns="_known")


def split_buckets(starts, ends, edge, cache: ClosedCache, sources, closed, recent) -> pd.DataFrame:
    """各桶分两段算, 按桶起点索引拼起来(每桶一行)。

    整根早于 edge(底层最近窗口的第一根)的已收尾, 30s 全在历史文件里: closed(lo, hi, starts, ends) 从文件算,
    经 cache 按 sources 的改动记录增量维护。其余由 recent(lo, starts, ends) 用文件里 [lo, edge) 加最近窗口现算。
    """
    done = ends <= edge
    parts = [cache.get(sources, starts[done], ends[done], closed)]
    if not done.all():
        parts.append(recent(int(starts[~done][0]), starts[~done], ends[~done]))
    return pd.concat(parts) if len(parts) > 1 else parts[0]


class RollupMixin:
    """大周期 Feed 的公共部分, 与 ingest.Feed 一起继承(期货、加密、多所汇总各自提供开高低收)。

    底层 30s Feed 用 base_lookup() 每次现取: 底层被回收重建后, 旧实例就不能再用了。
    需求(拆分粒度、真实请求)原样转给底层, 底层才会去算、去落盘这个粒度, 也不会被当成闲置回收。
    """

    rule = "volume"
    is_rollup = True

    def _init_rollup(self, base_lookup, max_bars: int):
        self.base_lookup = base_lookup
        self.max_bars = max_bars       # 只合成最近这么多根(页面只显示这么多)
        self.base_revision = None      # 上次重算时底层的 revision: 变了才需要重算
        self._closed = {}              # ltf -> ClosedCache(已收尾各桶的统计)

    def base(self):
        return self.base_lookup()

    def request(self, ltf=None, footprint=False, demand=False):
        super().request(ltf=ltf, demand=demand)
        base = self.base()
        if base is not None:
            base.request(ltf=ltf, demand=demand)

    def cfg(self) -> dict:
        base = self.base()
        cfg = base.cfg() if base is not None else super().cfg()
        return {**cfg, "tf": self.tf, "ltfOptions": ltf_options(self.tf)}

    def footprint_snapshot(self, demand=True):
        """大周期不做足迹图。"""
        return self._empty_footprint(demand)

    def ensure_ltf_subscriptions(self, api):
        """小周期 K 线归底层 Feed 订阅。"""

    def candles(self, base) -> pd.DataFrame:
        """本周期最近 max_bars 根的开高低收量(time 为展示秒, 每行起点即桶的左边界)。"""
        raise NotImplementedError

    def volume_store(self, base, ltf):
        """期货覆盖判定用的成交量来源(tick 口径历史文件); 加密数槽位, 不需要。"""
        return base._store(0) if self.rule == "volume" else None

    def base_stats(self, base, ltf, candles) -> pd.DataFrame | None:
        """底层 ltf 粒度 -> 各桶统计。已收尾的桶走缓存; 其余由历史文件里窗口之前那截加最近窗口现算。"""
        recent = base.latest_window(ltf)
        if recent is None:          # 底层这一轮没算这个粒度(刚要、或已停用): 等它算出来
            return None
        store = base._store(ltf)
        store.refresh()
        volume_store = self.volume_store(base, ltf)
        if volume_store is not None and volume_store is not store:
            volume_store.refresh()
        starts = candles["time"].to_numpy(dtype=np.int64)
        edge = int(recent["time"].iloc[0]) if not recent.empty else INT64_MAX

        def fresh(lo, s, e):
            rows = [frame for frame in (store_rows(store, lo, edge, volume_store), recent_rows(recent))
                    if not frame.empty]
            return bucket_stats(pd.concat(rows, ignore_index=True) if rows else None, s, e)

        sources = [store] if volume_store is None or volume_store is store else [store, volume_store]
        return split_buckets(starts, bucket_ends(starts, self.tf), edge, self._closed.setdefault(ltf, ClosedCache()),
                             sources, lambda lo, hi, s, e: bucket_stats(store_rows(store, lo, hi, volume_store), s, e),
                             fresh)

    def recompute(self, broadcast):
        """各粒度: 合成 -> 用底层历史文件的累计和算 CVD -> 出快照与增量消息。"""
        with self._state_lock:
            now = time.monotonic()
            ltfs = [ltf for ltf, expires in self._requested.items() if expires >= now]
            demand_version = self._demand_version
            previous = dict(self.snapshots)
        base = self.base()
        snapshots, messages = {}, []
        revision = self.revision + 1
        if base is not None:
            candles = self.candles(base)
            for ltf in ltfs:
                stats = self.base_stats(base, ltf, candles) if not candles.empty else None
                if stats is None:
                    continue
                bars = finish_bars(candles, stats, self.tf, self.rule)
                store = base._store(ltf)
                bars = store.with_cvd(bars)
                snapshots[ltf], message = self._snapshot_message(ltf, bars, previous, revision, store.base)
                if message:
                    messages.append(message)
            self.base_revision = base.revision
        with self._state_lock:
            self.snapshots = snapshots
            self.revision = revision
            self._computed_version = demand_version
            self.error = None
            if snapshots:
                self.ready.set()
        for message in messages:
            broadcast(message)
