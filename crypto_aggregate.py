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

import math
import time

import numpy as np
import pandas as pd

import ingest
from crypto_feed import IDLE_RECOMPUTE_SEC, VENUE_NAMES, large_trade_source, rollup_bases, venue_of
from indicator import (BAR_COLUMNS, DEFAULT_TF_SEC, NATIVE_TFS, TZ_SHIFT_S, VOLUME_ROUND, finalize_bars,
                       ltf_options)
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
        self._large_sources = []
        # 成分合约还没采集时的占位: 只提供参数说明, 没有记录, 汇总里该所按未覆盖算。
        self._large_missing = {part: ingest.LargeTradeDetector(part, large_trade_source(part))
                               for part in components}
        # ltf -> (各家历史文件, 各自上次同步完时的版本, 自己的历史文件, 它的版本), 见 sync_history
        self._synced = {}
        # ltf -> (轮次, 各家 Feed, 各家这一粒度的历史文件, 算完时各自的版本, 各家窗口的起点), 见 _parts_since
        self._part_marks = {}

    def _new_large_detector(self):
        """大额事件来自各成分合约自己的检测器(见 recompute), 汇总不自建。"""
        return None

    def large_sources(self):
        return self._large_sources

    def cfg(self) -> dict:
        return {**super().cfg(), "priceDigits": self.price_digits, "volumeDigits": self.qty_digits}

    def footprint_snapshot(self, demand=True):
        return self._empty_footprint(demand)

    def sync_history(self, manager):
        """各交易所历史文件里都完整的 bar, 相加后写进汇总自己的历史文件(只补没有或不一样的)。

        只核对上次同步之后各家或自己的历史文件改到的那段(第一次全部核对): 更早的各根两边都没动过,
        上次核对完就是一致的。历史有十几万根, 每 30 秒全部核对一遍要好几秒。
        """
        for ltf in ltf_options(self.tf):
            stores = []
            for symbol in self.components:
                feed = manager.feeds.get(feed_key(symbol, self.tf))
                if feed is None or not feed.loaded:
                    return
                store = feed._store(ltf)
                store.refresh()
                stores.append(store)
            mine = self._store(ltf)
            mine.refresh()
            since = self._sync_since(ltf, stores, mine)
            if since == float("inf"):
                continue
            times = stores[0].confirmed_times(since)
            for store in stores[1:]:
                times = np.intersect1d(times, store.confirmed_times(since), assume_unique=True)
            tables = [store.values for store in stores]
            rows = []
            for t in times.tolist():
                buy = round(sum(values[t][0] for values in tables), VOLUME_ROUND)
                sell = round(sum(values[t][1] for values in tables), VOLUME_ROUND)
                if mine.values.get(t) != (buy, sell):
                    rows.append((t, buy, sell))
            if rows:
                frame = pd.DataFrame(rows, columns=["time", "buy", "sell"])
                frame["unknown"] = 0.0
                frame["buyLegacy"], frame["sellLegacy"] = frame["buy"], frame["sell"]
                frame["coverage"], frame["hasBaseline"] = "complete", True
                mine.save_completed(frame, final=True)
            self._synced[ltf] = (tuple(stores), tuple(store.revision for store in stores), mine, mine.revision)
        self.last_sync = time.monotonic()

    def _sync_since(self, ltf, stores, mine):
        """上次同步这一粒度之后, 各家与自己的历史文件最早改到哪(inf 都没改; 没同步过或换了文件 -inf)。"""
        state = self._synced.get(ltf)
        if (state is None or state[2] is not mine or len(state[0]) != len(stores)
                or any(old is not new for old, new in zip(state[0], stores))):
            return float("-inf")
        since = float("inf")
        for store, revision in [*zip(stores, state[1]), (mine, state[3])]:
            moved = store.changed_since(revision)
            if moved is not None:
                since = min(since, moved)
        return since

    def _component_frame(self, manager, symbol, ltf, since=None):
        """一家的窗口(合并过它自己的历史) -> (按 time 索引的 bars 或 None, 它窗口各根的时间或 None)。"""
        feed = manager.feeds.get(feed_key(symbol, self.tf))
        if feed is None or not feed.loaded:
            return None, None
        bars, window = feed.trades.frame_since(ltf, since)
        if bars.empty:
            return None, window
        store = feed._store(ltf)
        store.refresh()
        return store.merge(bars).set_index("time"), window

    def combine(self, manager, ltf) -> pd.DataFrame:
        """各交易所的当前窗口 -> 汇总的 bars 表(BAR_COLUMNS); 第一个交易所没有数据时为空表。"""
        return self.combine_since(manager, ltf)[0]

    def combine_since(self, manager, ltf, since=None):
        """同 combine, 另返回窗口各根的时间(取第一个交易所的)与各家窗口的起点(没有窗口为 None)。
        since 给了只出这以后的几根: 每根只取决于各家同一时间的那根还在不在它的窗口里、是什么。"""
        parts = [self._component_frame(manager, symbol, ltf, since) for symbol in self.components]
        frames = [frame for frame, _ in parts]
        window = parts[0][1]
        starts = [int(part[0]) if part is not None and len(part) else None for _, part in parts]
        return self.combine_frames(frames), window, starts

    @staticmethod
    def combine_frames(frames):
        """按第一家的时间与价格合并; 历史分页与实时窗口共用缺失判定。"""
        primary = frames[0]
        if primary is None:
            return pd.DataFrame(columns=BAR_COLUMNS)
        times = primary.index
        parts = [frame.reindex(times) if frame is not None else pd.DataFrame(index=times) for frame in frames]
        df = primary[["open", "high", "low", "close"]].copy()
        for column in ("volume", "buy", "sell"):
            # 任何一家缺这根(NaN)就整根缺: min_count 要求每家都有值
            df[column] = pd.concat([part.get(column, pd.Series(np.nan, index=times)) for part in parts],
                                   axis=1).sum(axis=1, min_count=len(parts)).round(VOLUME_ROUND)
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

    def _starts_moved(self, ltf, starts, window):
        """第一家以外各家的窗口起点与上一轮不同时, 汇总窗口里从哪根起受影响(None 不受影响)。

        汇总的一根要看别家同一时间那根在不在它自己的窗口里: 别家窗口往前滑(或往回伸)时, 起点两次位置之间
        那几根进出了它的窗口, 而这不算它的成交有改动。第一家的窗口就是汇总的窗口, 滑出去的由拼接去掉。
        """
        mark = self._part_marks.get(ltf)
        floor = None
        for before, now in zip(mark[4][1:], starts[1:]):
            if before == now:
                continue
            if before is None or now is None:
                return float("-inf")
            if max(before, now) > window[0]:
                edge = max(min(before, now), int(window[0]))
                floor = edge if floor is None else min(floor, edge)
        return floor

    def _parts_since(self, ltf, parts, changed):
        """这一粒度能不能只算尾部(同 Feed._tail_since): 再算上各家这一粒度的历史文件在上一轮之后改到的地方
        (合并各家窗口时用了它们)。各家 Feed 换了、还没读回历史, 都整窗口重算。"""
        mark = self._part_marks.get(ltf)
        if (mark is None or mark[0] != self._round - 1 or len(mark[1]) != len(parts)
                or any(feed is None or not feed.loaded or old is not feed for old, feed in zip(mark[1], parts))):
            return None
        since = changed
        for old, feed, revision in zip(mark[2], parts, mark[3]):
            store = feed._store(ltf)
            if store is not old:
                return None
            store.refresh()
            moved = store.changed_since(revision)
            if moved is not None:
                since = min(since, moved)
        return self._tail_since(ltf, since)

    def recompute(self, manager, broadcast):
        self._large_sources = [detector for symbol in self.components
                               for detector in (manager.feeds[feed_key(symbol, self.tf)].large_sources()
                                   if feed_key(symbol, self.tf) in manager.feeds else [self._large_missing[symbol]])]
        with self._state_lock:
            now = time.monotonic()
            ltfs = [ltf for ltf, expires in self._requested.items() if expires >= now]
            demand_version = self._demand_version
            previous = dict(self.snapshots)
        self._start_round()
        parts = [manager.feeds.get(feed_key(symbol, self.tf)) for symbol in self.components]
        # 各家逐笔成交窗口从上一轮到现在最早改到哪根(展示秒), 见 _parts_since
        changed = min((feed.trades.changes.take(self) if feed is not None else float("-inf") for feed in parts),
                      default=float("-inf"))
        if math.isfinite(changed):
            changed = changed // 1000 + TZ_SHIFT_S
        snapshots, messages = {}, []
        revision = self.revision + 1
        for ltf in ltfs:
            since = self._parts_since(ltf, parts, changed)
            bars, window, starts = self.combine_since(manager, ltf, since)
            if since is not None and not bars.empty:
                moved = self._starts_moved(ltf, starts, window)
                if moved is not None and moved < since:
                    since = int(moved) if moved > window[0] else None
                    bars, window, starts = self.combine_since(manager, ltf, since)
            if since is not None and bars.empty:
                since = None                     # 不该发生: 上一轮的最后一根总在尾部里
                bars, window, starts = self.combine_since(manager, ltf)
            if bars.empty:
                continue
            tail = None if since is None else (since, window, lambda ltf=ltf: self.combine(manager, ltf))
            snapshots[ltf], message = self._publish(ltf, bars, previous, revision, tail)
            if all(feed is not None for feed in parts):
                stores = tuple(feed._store(ltf) for feed in parts)
                self._part_marks[ltf] = (self._round, tuple(parts), stores, tuple(store.revision for store in stores),
                                         starts)
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
            # 汇总的大周期还留着, 它的底层(汇总的 30s)就不能回收(见 CryptoManager.trade_symbols)
            wanted |= {symbol for symbol, _ in manager.rollups if venue_of(symbol) == "AGG"}
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
