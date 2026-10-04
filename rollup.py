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

合成结果不落盘: 随时能从 30s 现算, 底层回填补齐之后大周期也跟着齐。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from indicator import BAR_COLUMNS, ROLLUP_BASE_TF, TZ_SHIFT_S, ltf_options

BASE_SEC = ROLLUP_BASE_TF
SUM_COLUMNS = ["buy", "sell", "unknown", "buyLegacy", "sellLegacy"]
ROW_COLUMNS = ["time", *SUM_COLUMNS, "coverage", "hasBaseline", "volume"]
CANDLE_COLUMNS = ["time", "open", "high", "low", "close", "volume"]
VOLUME_ROUND = 8


def candles_from_klines(klines) -> pd.DataFrame:
    """TqSdk K 线(datetime 纳秒) -> 开高低收量表(time 为展示秒, 同 HistoryStore)。"""
    if klines is None:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    k = klines[["datetime", "open", "high", "low", "close", "volume"]].dropna(subset=["datetime", "close"])
    k = k[k["datetime"] > 0]
    frame = k[["open", "high", "low", "close", "volume"]].astype(float).copy()
    frame.insert(0, "time", (k["datetime"].astype("int64") // 10**9 + TZ_SHIFT_S).astype("int64"))
    return frame.drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)


def resample_candles(frame: pd.DataFrame, tf: int) -> pd.DataFrame:
    """30s 开高低收量(time 为展示秒) -> tf 周期, 按交易所时间(UTC)整除对齐, 同交易所 K 线惯例。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    data = frame[CANDLE_COLUMNS].sort_values("time")
    utc = data["time"].to_numpy(dtype=np.int64) - TZ_SHIFT_S
    bucket = utc // tf * tf + TZ_SHIFT_S
    groups = data.groupby(bucket)
    out = pd.DataFrame({"open": groups["open"].first(), "high": groups["high"].max(),
                        "low": groups["low"].min(), "close": groups["close"].last(),
                        "volume": groups["volume"].sum().round(VOLUME_ROUND)})
    out.index.name = "time"
    return out.reset_index()


def join_candles(older: pd.DataFrame, newer: pd.DataFrame) -> pd.DataFrame:
    """两段合成好的大周期开高低收量接起来; older 的 30s 全都早于 newer 的(同一根可能两边各有一截)。"""
    if older is None or older.empty:
        return newer
    if newer is None or newer.empty:
        return older
    both = pd.concat([older, newer], ignore_index=True)
    groups = both.groupby("time", sort=True)
    out = pd.DataFrame({"open": groups["open"].first(), "high": groups["high"].max(),
                        "low": groups["low"].min(), "close": groups["close"].last(),
                        "volume": groups["volume"].sum().round(VOLUME_ROUND)})
    return out.reset_index()


def store_rows(store, before=None, volume_store=None) -> pd.DataFrame:
    """底层历史文件 -> 每根 30s 一行(只取 time < before 的); 买卖量与覆盖同 HistoryStore.merge。

    volume 列是这根 30s 已知的成交量, 只有期货的覆盖判定用得到: 来自 volume_store(tick 口径
    的历史文件)里核对通过的行, buy + sell + unknown 就是 K 线成交量; 其余为 NaN。
    """
    legacy = store.legacy
    times = sorted(set(store.values) | set(store.estimates) | set(legacy))
    if before is not None:
        times = times[:int(np.searchsorted(times, before, side="left"))]
    if not times:
        return pd.DataFrame(columns=ROW_COLUMNS)
    skeleton = pd.DataFrame({"time": np.array(times, dtype=np.int64), "buy": np.nan, "sell": np.nan,
                             "coverage": "missing", "hasBaseline": False})
    rows = store.merge(skeleton)
    if volume_store is not None:
        reference = volume_store.values
        extra = volume_store.extra
        rows["volume"] = [(reference[t][0] + reference[t][1] + (extra.get(t, {}).get("unknown") or 0.0))
                          if t in reference else np.nan for t in times]
    else:
        rows["volume"] = np.nan
    return rows[ROW_COLUMNS]


def recent_rows(recent: pd.DataFrame | None) -> pd.DataFrame:
    """底层最近窗口(Feed.latest[ltf]) -> 同 store_rows 的列。"""
    if recent is None or recent.empty:
        return pd.DataFrame(columns=ROW_COLUMNS)
    rows = recent.reindex(columns=ROW_COLUMNS).copy()
    rows["hasBaseline"] = rows["hasBaseline"].fillna(False).astype(bool)
    return rows


def rollup_bars(candles: pd.DataFrame, rows: pd.DataFrame, tf: int, rule: str = "volume") -> pd.DataFrame:
    """大周期开高低收量 + 底层 30s 买卖量 -> 大周期 bars 表(BAR_COLUMNS, 不含 CVD)。

    rule="volume"(期货): 各根 30s 都 complete 且已知成交量之和等于这根的成交量才算完整;
    rule="slots"(加密): 桶里的 30s 槽位(最后一桶数到底层最新一根为止)齐全且都 complete 才算完整。
    """
    if candles is None or candles.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = candles[CANDLE_COLUMNS].reset_index(drop=True).copy()
    starts = out["time"].to_numpy(dtype=np.int64)
    following = np.r_[starts[1:], np.iinfo(np.int64).max]
    ends = np.minimum(starts + tf, following)
    rows = rows if rows is not None else pd.DataFrame(columns=ROW_COLUMNS)
    t = rows["time"].to_numpy(dtype=np.int64)
    owner = np.searchsorted(starts, t, side="right") - 1
    inside = (owner >= 0) & (t < ends[np.clip(owner, 0, None)])
    rows = rows[inside]
    owner = owner[inside]
    index = pd.RangeIndex(len(out))
    for column in SUM_COLUMNS:
        values = pd.to_numeric(rows[column], errors="coerce").astype(float)
        out[column] = values.groupby(owner).sum(min_count=1).reindex(index).round(VOLUME_ROUND)
    coverage = rows["coverage"].astype(object)
    has_data = pd.to_numeric(rows["buy"], errors="coerce").notna().to_numpy()
    count = pd.Series(1, index=rows.index).groupby(owner).sum().reindex(index, fill_value=0)
    complete = coverage.eq("complete").to_numpy()
    n_complete = pd.Series(complete, index=rows.index).groupby(owner).sum().reindex(index, fill_value=0)
    n_legacy = (pd.Series(coverage.eq("legacy").to_numpy(), index=rows.index)
                .groupby(owner).sum().reindex(index, fill_value=0))
    n_data = pd.Series(has_data, index=rows.index).groupby(owner).sum().reindex(index, fill_value=0)
    if rule == "slots":
        latest = int(t[inside].max()) + BASE_SEC if inside.any() else int(starts[-1])
        expected = (np.minimum(ends, latest) - starts) // BASE_SEC
        whole = (n_complete.to_numpy() == expected) & (expected > 0)
    else:
        volume = pd.to_numeric(rows["volume"], errors="coerce").astype(float)
        known = volume.where(complete).groupby(owner).sum(min_count=1).reindex(index)
        whole = ((n_complete == count) & (count > 0)
                 & np.isclose(known, out["volume"], rtol=0, atol=1e-6)).to_numpy()
    out["coverage"] = np.where(out["buy"].isna(), "missing",
                               np.where(whole, "complete",
                                        np.where(n_legacy.to_numpy() == n_data.to_numpy(), "legacy", "partial")))
    baseline = pd.Series(rows["hasBaseline"].fillna(False).astype(bool).to_numpy(), index=rows.index)
    out["hasBaseline"] = baseline.groupby(owner).any().reindex(index, fill_value=False).astype(bool)
    out["delta"] = out["buy"] - out["sell"]
    out["deltaLegacy"] = out["buyLegacy"] - out["sellLegacy"]
    out["cvd"] = np.nan
    return out[BAR_COLUMNS]


class RollupMixin:
    """大周期 Feed 的公共部分, 与 ingest.Feed 一起继承(期货、加密、多所汇总各自提供开高低收)。

    底层 30s Feed 用 base_lookup() 每次现取: 底层被回收重建后, 旧实例就不能再用了。
    需求(拆分粒度、真实请求)原样转给底层, 底层才会去算、去落盘这个粒度, 也不会被当成闲置回收。
    """

    rule = "volume"
    is_rollup = True

    def _init_rollup(self, base_lookup):
        self.base_lookup = base_lookup
        self.base_revision = None      # 上次重算时底层的 revision: 变了才需要重算
        self._history = {}             # ltf -> (缓存键, 历史部分的 30s 行)

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
        """大周期不做足迹图: 给一份空足迹, 页面切到足迹图时不会一直等快照。"""
        self.request(demand=demand)
        with self._state_lock:
            return {"symbol": self.symbol, "tf": self.tf, "revision": self.revision,
                    "tickSize": None, "bars": []}

    def ensure_ltf_subscriptions(self, api):
        """小周期 K 线归底层 Feed 订阅。"""

    def candles(self, base) -> pd.DataFrame:
        raise NotImplementedError

    def volume_store(self, base, ltf):
        """期货覆盖判定用的成交量来源(tick 口径历史文件); 加密数槽位, 不需要。"""
        return base._store(0) if self.rule == "volume" else None

    def base_rows(self, base, ltf) -> pd.DataFrame | None:
        """底层 ltf 粒度的全部 30s 行: 历史部分按底层历史文件的版本缓存, 最近窗口每次现取。"""
        recent = base.latest.get(ltf)
        if recent is None:
            return None
        store = base._store(ltf)
        store.refresh()
        volume_store = self.volume_store(base, ltf)
        if volume_store is not None and volume_store is not store:
            volume_store.refresh()
        before = int(recent["time"].iloc[0]) if not recent.empty else None
        key = (id(store), store.revision, volume_store.revision if volume_store is not None else None, before)
        cached = self._history.get(ltf)
        if cached is None or cached[0] != key:
            cached = (key, store_rows(store, before, volume_store))
            self._history[ltf] = cached
        history, latest = cached[1], recent_rows(recent)
        if history.empty:
            return latest
        if latest.empty:
            return history
        return pd.concat([history, latest], ignore_index=True)

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
                rows = self.base_rows(base, ltf)
                if rows is None:
                    continue
                bars = rollup_bars(candles, rows, self.tf, self.rule)
                if bars.empty:
                    continue
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
