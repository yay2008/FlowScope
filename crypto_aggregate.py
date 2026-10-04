# -*- coding: utf-8 -*-
"""多所汇总: ``AGG.BTC`` = 币安 BTCUSDT 永续 + OKX BTC-USDT 永续, 主动买卖量相加(都已折成币)。

- 开高低收取第一个交易所(币安)的, 两家价格几乎一样, 叠加的是成交量与主动买卖量;
- 一根 bar 只有各交易所都完整时才算完整(任何一家缺这根就整根 missing, 不拿半边数据冒充汇总);
- 自己的历史文件(文件名规则同期货, 见 ingest.Feed._store)只存"各交易所都完整"的 bar, 由各家的
  历史文件定期合成 —— 所以回填补上某一家之后, 汇总这边也会跟着补齐, 不需要单独回填;
- 页面上看到的最近窗口由各家的实时窗口当场合成。

两家都在采集时才有汇总: 打开或收藏 ``AGG.BTC`` 会让它的各交易所合约一起常驻/临时采集。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

import ingest
from crypto_feed import IDLE_RECOMPUTE_SEC, VENUE_NAMES, rollup_bases, venue_of
from indicator import BAR_COLUMNS, DEFAULT_TF_SEC, NATIVE_TFS, finalize_bars, ltf_options
from ingest import feed_key

PREFIX = "AGG."
SYNC_SEC = 30.0              # 每隔多久用各交易所的历史文件补一次汇总的历史文件
COVERAGE_RANK = {"complete": 0, "legacy": 1, "partial": 1, "missing": 2}
RANK_COVERAGE = {0: "complete", 1: "partial", 2: "missing"}


def components_of(base: str) -> list[str]:
    """一个币在各交易所的 USDT 永续(按汇总的优先顺序: 开高低收取第一个)。"""
    return [f"BINANCE.{base}USDT.P", f"OKX.{base}-USDT-SWAP"]


class AggregateFeed(ingest.Feed):
    """多所汇总的一个主周期; 只在管理线程里重算。"""

    def __init__(self, symbol: str, tf: int, components: list[str], price_digits: int, qty_digits: int):
        super().__init__(symbol, tf)
        self.components = components
        self.price_digits = price_digits
        self.qty_digits = qty_digits
        self.loaded = True
        self.last_sync = 0.0
        self.last_compute = 0.0
        self.pending = False      # 成分合约有变动、还没重算

    def cfg(self) -> dict:
        return {**super().cfg(), "priceDigits": self.price_digits, "volumeDigits": self.qty_digits}

    def footprint_snapshot(self, demand=True):
        self.request(demand=demand)
        with self._state_lock:
            return {"symbol": self.symbol, "tf": self.tf, "revision": self.revision, "tickSize": None, "bars": []}

    def sync_history(self, manager):
        """各交易所历史文件里都完整的 bar, 相加后写进汇总自己的历史文件(只补没有或不一样的)。"""
        for ltf in ltf_options(self.tf):
            stores = []
            for symbol in self.components:
                feed = manager.feeds.get(feed_key(symbol, self.tf))
                if feed is None or not feed.loaded:
                    return
                store = feed._store(ltf)
                store.refresh()
                stores.append(store.values)
            times = set(stores[0]).intersection(*stores[1:])
            mine = self._store(ltf)
            mine.refresh()
            rows = []
            for t in sorted(times):
                buy = round(sum(values[t][0] for values in stores), 8)
                sell = round(sum(values[t][1] for values in stores), 8)
                if mine.values.get(t) != (buy, sell):
                    rows.append((t, buy, sell))
            if rows:
                frame = pd.DataFrame(rows, columns=["time", "buy", "sell"])
                frame["unknown"] = 0.0
                frame["buyLegacy"], frame["sellLegacy"] = frame["buy"], frame["sell"]
                frame["coverage"], frame["hasBaseline"] = "complete", True
                mine.save_completed(frame, final=True)
        self.last_sync = time.monotonic()

    def _component_frame(self, manager, symbol, ltf):
        feed = manager.feeds.get(feed_key(symbol, self.tf))
        if feed is None or not feed.loaded:
            return None
        bars = feed.trades.frame(ltf)
        if bars.empty:
            return None
        store = feed._store(ltf)
        store.refresh()
        return store.merge(bars).set_index("time")

    def combine(self, manager, ltf) -> pd.DataFrame:
        """各交易所的当前窗口 -> 汇总的 bars 表(BAR_COLUMNS); 第一个交易所没有数据时为空表。"""
        frames = [self._component_frame(manager, symbol, ltf) for symbol in self.components]
        primary = frames[0]
        if primary is None:
            return pd.DataFrame(columns=BAR_COLUMNS)
        times = primary.index
        parts = [frame.reindex(times) if frame is not None else pd.DataFrame(index=times) for frame in frames]
        df = primary[["open", "high", "low", "close"]].copy()
        for column in ("volume", "buy", "sell"):
            # 任何一家缺这根(NaN)就整根缺: min_count 要求每家都有值
            df[column] = pd.concat([part.get(column, pd.Series(np.nan, index=times)) for part in parts],
                                   axis=1).sum(axis=1, min_count=len(parts)).round(8)
        rank = pd.concat([part.get("coverage", pd.Series("missing", index=times)).fillna("missing")
                          .map(COVERAGE_RANK).fillna(2) for part in parts], axis=1).max(axis=1)
        df["coverage"] = rank.map(RANK_COVERAGE)
        df.loc[df["buy"].isna(), "coverage"] = "missing"
        df["hasBaseline"] = pd.concat([part.get("hasBaseline", pd.Series(False, index=times)).fillna(False)
                                       .astype(bool) for part in parts], axis=1).all(axis=1)
        df["unknown"] = np.where(df["buy"].notna(), 0.0, np.nan)
        df["buyLegacy"], df["sellLegacy"] = df["buy"], df["sell"]
        df = finalize_bars(df.reset_index())
        return df[BAR_COLUMNS]

    def recompute(self, manager, broadcast):
        with self._state_lock:
            now = time.monotonic()
            ltfs = [ltf for ltf, expires in self._requested.items() if expires >= now]
            demand_version = self._demand_version
            previous = dict(self.snapshots)
        snapshots, messages = {}, []
        revision = self.revision + 1
        for ltf in ltfs:
            bars = self.combine(manager, ltf)
            if bars.empty:
                continue
            snapshots[ltf], message = self._publish(ltf, bars, previous, revision)
            if message:
                messages.append(message)
        with self._state_lock:
            self.snapshots = snapshots
            self.revision = revision
            self._computed_version = demand_version
            self.error = None
            if snapshots:
                self.ready.set()
        for message in messages:
            broadcast(message)


class AggregateBook:
    """全部汇总合约; 由 CryptoManager 持有(manager.aggregates), 在管理线程里重算。"""

    def __init__(self, manager):
        self.manager = manager
        self.feeds: dict[tuple[str, int], AggregateFeed] = {}

    @staticmethod
    def base_of(symbol: str) -> str:
        return symbol.strip()[len(PREFIX):]

    def components(self, symbol: str) -> list[str]:
        """能合成汇总的各交易所合约; 少于两家就不算汇总, 返回空列表。"""
        if venue_of(symbol) != "AGG":
            return []
        parts = [part for part in components_of(self.base_of(symbol)) if self.manager.instrument(part) is not None]
        return parts if len(parts) >= 2 else []

    def label(self, symbol: str) -> str:
        return f"{self.base_of(symbol)} 永续 · {VENUE_NAMES['AGG']}"

    def ensure(self, symbol: str, tf: int = DEFAULT_TF_SEC) -> AggregateFeed:
        parts = self.components(symbol)
        if not parts:
            raise ValueError(f"不支持的多所汇总 {symbol}(至少要两个交易所都有这个币的 USDT 永续)")
        primary = self.manager.instrument(parts[0])
        qty_digits = max(self.manager.instrument(part).qty_digits for part in parts)
        with self.manager._lock:
            self.manager._demand[symbol] = time.monotonic()
            for period in NATIVE_TFS:
                key = feed_key(symbol, period)
                if key not in self.feeds:
                    self.feeds[key] = AggregateFeed(key[0], period, parts, primary.price_digits, qty_digits)
            feed = self.feeds[feed_key(symbol, tf)]
        feed.request(demand=True)
        # 各交易所的 Feed 要马上建好(页面在等快照), 不等下一轮同步
        for part in parts:
            self.manager.ensure(part, tf)
        return feed

    def catalog_group(self, manager) -> dict:
        """选择器里的「多所汇总」分组: 各交易所都有 USDT 永续的币, 按合计 24 小时成交额降序。"""
        bases = {}
        for instrument in manager.instrument_list():
            bases.setdefault(instrument.base, []).append(instrument)
        products = []
        for base, items in bases.items():
            symbol = f"{PREFIX}{base}"
            if not self.components(symbol):
                continue
            products.append({"exchangeId": "AGG", "productId": base, "name": base,
                             "contSymbol": symbol, "mainSymbol": "", "crypto": True,
                             "openInterest": sum(item.rank for item in items if item.symbol in components_of(base))})
        products.sort(key=lambda item: -item["openInterest"])
        return {"exchangeId": "AGG", "exchangeName": VENUE_NAMES["AGG"], "products": products}

    def watch_row(self, manager, symbol: str) -> dict:
        """自选面板一行: 价格与涨跌取第一个交易所的, 成交量与成交额各家相加。"""
        from crypto_feed import WATCH_TTL_SEC, ticker_row
        parts = self.components(symbol)
        rows = []
        for part in parts:
            manager.touch(part, ["ticker"], WATCH_TTL_SEC)
            rows.append(ticker_row(part, manager.instrument(part), manager.quote(part).get("ticker") or {}))
        if not rows:
            return {**ticker_row(symbol, None, {}), "name": symbol}
        row = dict(rows[0])
        for column in ("volume", "amount"):
            values = [item[column] for item in rows]
            row[column] = None if any(value is None for value in values) else sum(values)
        name, _, venue = self.label(symbol).partition(" · ")
        return {**row, "symbol": symbol, "name": name, "venue": venue}

    def compute(self, manager, changed: set[str]):
        """有成分合约变动(或有新需求)的汇总才重算; 定期用各家历史文件补汇总的历史文件。"""
        now = time.monotonic()
        # 与 ensure(HTTP 线程)同一把锁: 增删 feeds 时不能和页面新建汇总撞在一起
        with manager._lock:
            watched = {(symbol, tf) for symbol, tf, _ in manager.clients.values()}
            watched |= rollup_bases(watched)      # 汇总的大周期由汇总的 30s 合成
            wanted = set(manager.pinned_aggregates()) | {symbol for symbol in manager._demand
                                                         if venue_of(symbol) == "AGG"}
            wanted |= {symbol for symbol, _ in watched if venue_of(symbol) == "AGG"}
            for key in [key for key in self.feeds if key[0] not in wanted]:
                del self.feeds[key]
            for symbol in wanted:
                if not self.components(symbol):
                    continue
                for tf in NATIVE_TFS:
                    if feed_key(symbol, tf) not in self.feeds:
                        self.ensure_feed(symbol, tf)
            feeds = list(self.feeds.items())
        for key, feed in feeds:
            try:
                if now - feed.last_sync >= SYNC_SEC:
                    feed.sync_history(manager)
                    feed.pending = True
                feed.pending = feed.pending or bool(set(feed.components) & changed)
                with feed._state_lock:
                    stale = feed._computed_version != feed._demand_version or feed.error is not None
                # 有人看: 成分一变就算; 没人看: 最多每 IDLE_RECOMPUTE_SEC 算一次, 保持快照不过时
                due = feed.pending and (key in watched or now - feed.last_compute >= IDLE_RECOMPUTE_SEC)
                if not (stale or due):
                    continue
                feed.pending = False
                feed.last_compute = now
                feed.recompute(manager, manager.broadcast)
            except Exception as exc:
                with feed._state_lock:
                    feed.error = f"汇总处理失败: {exc}"
                print(f"[crypto] {key[0]}@{key[1]}s {feed.error}", flush=True)

    def ensure_feed(self, symbol: str, tf: int):
        """常驻的汇总(自选里收了)没人看也要建好, 以便定期合成历史。"""
        parts = self.components(symbol)
        primary = self.manager.instrument(parts[0])
        qty_digits = max(self.manager.instrument(part).qty_digits for part in parts)
        self.feeds[feed_key(symbol, tf)] = AggregateFeed(symbol, tf, parts, primary.price_digits, qty_digits)
