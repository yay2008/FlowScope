"""复用快照分类与已完成 bar 聚合，仅重算新增/修订 tick 所在的 bar。"""
import numpy as np
import pandas as pd

from indicator import (BAR_NS, TZ_SHIFT_S, _classify_ticks, split_ticks_to_bars, build_footprint,
                       tick_bar_start)


class TickAnalytics:
    def __init__(self, bar_ns: int = BAR_NS):
        # bar_ns: 主周期宽度(纳秒)。缓存与"脏边界"都按它分桶, 所以一个实例只服务一个周期。
        self.bar_ns = bar_ns
        self.frame = None
        self.first_tick_ns = None
        self.changed_ns = None
        self.version = 0
        self._hashes = pd.Series(dtype="uint64")
        self._aggregates = {}
        self._fp = None
        self._fp_version = -1

    def update(self, ticks):
        columns = ["datetime", "last_price", "ask_price1", "bid_price1", "volume"]
        key_column = "id" if "id" in ticks else "datetime"
        raw = ticks[list(dict.fromkeys([key_column, *columns]))].copy()
        raw = raw.dropna(subset=[key_column, "datetime", "last_price", "volume"])
        raw = raw[(raw.datetime > 0) & (raw[key_column] >= 0) & (raw.volume >= 0) &
                  np.isfinite(raw.last_price) & np.isfinite(raw.volume)]
        raw = raw.drop_duplicates(key_column, keep="last").sort_values(key_column).reset_index(drop=True)
        if raw.empty:
            if self.frame is None:
                self.frame = _classify_ticks(raw)
            return
        keys = raw[key_column].to_numpy(dtype=np.int64)
        hashes = pd.util.hash_pandas_object(raw, index=False)
        hashes.index = keys
        old = self._hashes.reindex(keys)
        changed = old.isna().to_numpy() | (old.to_numpy() != hashes.to_numpy())
        # reindex 存在缺项时会把 uint64 转 float，比较哈希须避免精度丢失。
        known = np.isin(keys, self._hashes.index)
        if known.any():
            changed[known] = self._hashes.loc[keys[known]].to_numpy() != hashes.loc[keys[known]].to_numpy()
        if not changed.any():
            return
        start = int(np.flatnonzero(changed)[0])
        initial = self.frame is None or self.frame.empty
        # 无重叠且 ID 不连续表示丢过 tick；从新窗口重新建立覆盖边界。
        gap = (not initial and not known.any() and
               (key_column != "id" or keys[0] != int(self.frame["_key"].iloc[-1]) + 1))
        if gap:
            self._aggregates.clear()
            self._fp = None
            initial = True
            start = 0
        head = None if initial else self.frame[self.frame["_key"] < keys[start]]
        seed = None if head is None or head.empty else head.iloc[-1]
        # 判向状态逐列传回(含新算法的 prev_time/prev_price/prev_ask/prev_bid/carry),
        # 否则增量重算与全量重算会在窗口接缝处给出不同的方向。
        classified = _classify_ticks(
            raw.iloc[start:],
            previous_volume=None if seed is None else seed.volume,
            previous_side=0 if seed is None else seed.side,
            previous_time=None if seed is None else seed.datetime,
            previous_price=None if seed is None else seed.lr_price,
            previous_ask=None if seed is None else seed.lr_ask,
            previous_bid=None if seed is None else seed.lr_bid,
            previous_carry=0 if seed is None else seed.lr_carry,
        )
        classified["_key"] = keys[start:]
        self.frame = classified.reset_index(drop=True) if head is None else pd.concat([head, classified], ignore_index=True)
        if initial:
            self.first_tick_ns = int(raw.datetime.iloc[0])
        self.changed_ns = int(raw.datetime.iloc[start])
        self.version += 1
        self._hashes = hashes
        # 保留窗口边缘整根 bar 和一个前置 tick，以免裁剪后重算边缘 bar 丢量。
        cutoff = tick_bar_start(raw.datetime.iloc[0], self.bar_ns)
        keep = np.flatnonzero(self._buckets() >= cutoff)
        if len(keep):
            self.frame = self.frame.iloc[max(0, int(keep[0]) - 1):].reset_index(drop=True)

    def _buckets(self):
        return tick_bar_start(self.frame.datetime, self.bar_ns).to_numpy()

    def _dirty_tail(self):
        """首个变化快照所在 bar 的起点, 以及从这根 bar 起的全部快照。"""
        boundary = tick_bar_start(self.changed_ns, self.bar_ns)
        return boundary, self.frame[self._buckets() >= boundary]

    def aggregate(self, ltf, first_bar_ns):
        cached, version = self._aggregates.get(ltf, (None, -1))
        if cached is None or version < self.version - 1:
            cached = split_ticks_to_bars(self.frame, ltf, classified=self.frame, bar_ns=self.bar_ns)
        elif version != self.version:
            boundary, tail = self._dirty_tail()
            fresh = split_ticks_to_bars(tail, ltf, classified=tail, bar_ns=self.bar_ns)
            cached = pd.concat([cached[cached.index < boundary], fresh])
        cached = cached[cached.index >= first_bar_ns]
        self._aggregates[ltf] = (cached, self.version)
        return cached

    def footprint(self, klines, tick_size, coverage):
        if self._fp is None or self._fp_version < self.version - 1:
            self._fp = build_footprint(klines, self.frame, tick_size, classified=self.frame,
                                       bar_ns=self.bar_ns)["bars"]
        elif self._fp_version != self.version:
            boundary, tail = self._dirty_tail()
            boundary_s = boundary // 10**9 + TZ_SHIFT_S
            fresh = build_footprint(klines, tail, tick_size, classified=tail,
                                    bar_ns=self.bar_ns)["bars"]
            self._fp = [bar for bar in self._fp if bar["time"] < boundary_s] + fresh
        self._fp_version = self.version
        allowed = set(coverage)
        self._fp = [{**bar, "coverage": coverage[bar["time"]]} for bar in self._fp if bar["time"] in allowed][-800:]
        step = float(tick_size) if tick_size is not None else None
        if step is not None and (not np.isfinite(step) or step <= 0):
            step = None
        return {"tickSize": step, "bars": self._fp}

    def retain(self, ltfs, footprint):
        self._aggregates = {ltf: value for ltf, value in self._aggregates.items() if ltf in ltfs}
        if not footprint:
            self._fp = None
