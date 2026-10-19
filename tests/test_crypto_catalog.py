"""加密合约的选择器分组、自选与报价、常驻采集, 以及多所汇总(合成、历史、报价)。"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

import app as server
import ingest
from binance_feed import BinanceAdapter, make_instrument
from crypto_aggregate import AggregateBook
from crypto_feed import CryptoManager, aggregate_trades
from favorites import FavoriteStore, FavoritesService
from indicator import ltf_options
from okx_feed import OkxAdapter

BASE = 1_790_985_600_000 + 11 * 3_600_000
BTC, OKX_BTC = "BINANCE.BTCUSDT.P", "OKX.BTC-USDT-SWAP"


def make_manager():
    manager = CryptoManager([BinanceAdapter(), OkxAdapter()])
    manager.aggregates = AggregateBook(manager)
    return manager


class CatalogTests(unittest.TestCase):
    def test_groups_rank_by_turnover_and_include_aggregates(self):
        manager = make_manager()
        with manager._lock:
            manager.instruments["BINANCE.ETHUSDT.P"] = replace(manager.instruments["BINANCE.ETHUSDT.P"], rank=9e9)
            manager.instruments[BTC] = replace(manager.instruments[BTC], rank=1e9)
            manager.instruments["BINANCE.SOLUSDT.P"] = make_instrument("SOLUSDT", "SOL", 0.01, 1.0, rank=5e9)
        ordered = manager.catalog_groups()
        # 汇总排最前: 交易所的永续有几百个, 排在后面得翻到底才找得到
        self.assertEqual([group["exchangeId"] for group in ordered], ["AGG", "BINANCE", "OKX"])
        groups = {group["exchangeId"]: group for group in ordered}
        self.assertEqual([item["contSymbol"] for item in groups["BINANCE"]["products"]],
                         ["BINANCE.ETHUSDT.P", "BINANCE.SOLUSDT.P", BTC])
        row = groups["BINANCE"]["products"][0]
        self.assertEqual((row["name"], row["mainSymbol"], row["crypto"]), ("ETHUSDT", "", True))
        self.assertEqual(groups["BINANCE"]["exchangeName"], "币安永续")
        # 汇总只列各交易所都有的币: OKX 内置只有 BTC
        self.assertEqual([item["contSymbol"] for item in groups["AGG"]["products"]], ["AGG.BTC"])

    def test_watch_rows_come_from_the_24h_ticker(self):
        manager = make_manager()
        manager._apply_event(("ticker", BTC, {"last": 101.0, "open": 100.0, "changePct": 1.0,
                                               "volume": 10.0, "amount": 1000.0}))
        manager._apply_event(("ticker", OKX_BTC, {"last": 101.5, "open": 100.0, "changePct": 1.5,
                                                   "volume": 5.0, "amount": 500.0}))
        btc, missing, agg = manager.watch_rows([BTC, "BINANCE.ETHUSDT.P", "AGG.BTC"])
        self.assertEqual((btc["name"], btc["venue"], btc["lastPrice"], btc["change"], btc["priceDecs"]),
                         ("BTCUSDT 永续", "币安", 101.0, 1.0, 1))
        self.assertIsNone(missing["lastPrice"])                           # 还没收到行情
        self.assertEqual((agg["venue"], agg["lastPrice"], agg["volume"], agg["amount"]), ("多所汇总", 101.0, 15.0, 1500.0))
        self.assertIn((BTC, "ticker"), manager._interest)

    def test_pins_resolve_once_the_instrument_list_arrives(self):
        manager = make_manager()
        self.assertEqual(manager.set_pinned(["BINANCE.SOLUSDT.P", "AGG.BTC"]), [BTC, OKX_BTC])
        with manager._lock:
            manager.instruments["BINANCE.SOLUSDT.P"] = make_instrument("SOLUSDT", "SOL", 0.01, 1.0)
        manager._sync(time.monotonic())
        self.assertEqual(manager.pinned, [BTC, OKX_BTC, "BINANCE.SOLUSDT.P"])
        self.assertIn("solusdt@aggTrade", manager._topics["BINANCE.market"])
        self.assertIn("solusdt@ticker", manager._topics["BINANCE.market"])


class FakeCatalog:
    def __init__(self):
        self.calls = []

    async def payload(self, exchange=None, product=None, refresh=False):
        self.calls.append((exchange, product))
        return {"groups": [{"exchangeId": "SHFE", "exchangeName": "上期所", "products": []}],
                "source": "live", "error": None, "months": [], "monthsError": None}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.crypto = make_manager()
        self.catalog = FakeCatalog()
        self.futures = SimpleNamespace(set_pinned=lambda keys: [], query=self.no_tqsdk)
        favorites = FavoritesService(lambda: self.futures,
                                     FavoriteStore(lambda: os.path.join(self.temp.name, "favorites.json")))
        self.patches = [patch.object(server, "crypto", self.crypto), patch.object(server, "catalog", self.catalog),
                        patch.object(server, "manager", self.futures), patch.object(server, "favorites", favorites)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    @staticmethod
    async def no_tqsdk(fn, timeout=None):
        raise RuntimeError("行情线程未就绪(error)")

    def test_symbols_append_crypto_groups_and_skip_months_for_crypto(self):
        payload = asyncio.run(server.symbols())
        self.assertEqual([group["exchangeId"] for group in payload["groups"]], ["SHFE", "AGG", "BINANCE", "OKX"])
        payload = asyncio.run(server.symbols(exchange="BINANCE", product="BTCUSDT"))
        self.assertEqual(self.catalog.calls[-1], (None, None))             # 加密品种不问 TqSdk 的月份
        self.assertEqual(payload["months"], [])

    def test_crypto_favorites_are_checked_against_the_instrument_list_and_pinned(self):
        result = asyncio.run(server.favorites_add(BTC))
        self.assertEqual(result["symbols"], [BTC])
        asyncio.run(server.favorites_add("AGG.BTC"))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.favorites_add("BINANCE.NOPEUSDT.P"))
        self.assertIn("不支持", caught.exception.detail)
        self.assertEqual(self.crypto._pin_request, [BTC, "AGG.BTC"])

    def test_watch_keeps_crypto_quotes_when_tqsdk_is_down(self):
        self.crypto._apply_event(("ticker", BTC, {"last": 101.0, "open": 100.0, "changePct": 1.0}))
        payload = asyncio.run(server.watch(f"SHFE.rb2601,{BTC}"))
        self.assertEqual(payload["source"], "live")
        self.assertIn("未就绪", payload["error"])
        self.assertEqual([row["symbol"] for row in payload["quotes"]], [BTC])
        self.assertEqual(payload["quotes"][0]["lastPrice"], 101.0)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.manager = make_manager()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_live_window_sums_volumes_and_takes_the_worst_coverage(self):
        feed = self.manager.ensure("AGG.BTC", 30)
        self.assertEqual(feed.components, [BTC, OKX_BTC])
        self.manager._sync(time.monotonic())
        binance, okx = self.manager.feeds[(BTC, 30)], self.manager.feeds[(OKX_BTC, 30)]
        for trade_id, offset in enumerate([1000, 31000, 61000]):
            binance.trades.add(trade_id + 1, 100.0 + trade_id, 1.0, BASE + offset, False)
            okx.trades.add(trade_id + 50, 100.5, 0.25, BASE + offset + 500, True)
        bars = feed.combine(self.manager, 0)
        self.assertEqual(list(bars.open), [100.0, 101.0, 102.0])           # 开高低收取币安的
        self.assertEqual(list(bars.volume), [1.25, 1.25, 1.25])
        self.assertEqual((bars.iloc[1].buy, bars.iloc[1].sell), (1.0, 0.25))
        self.assertEqual(list(bars.coverage), ["partial", "complete", "complete"])
        self.assertEqual(feed.cfg()["volumeDigits"], 4)                   # 取两家里更细的数量位数

    def test_history_is_rebuilt_from_bars_complete_on_every_exchange(self):
        self.manager.ensure("AGG.BTC", 30)
        self.manager._sync(time.monotonic())
        trades = pd.DataFrame({"id": range(1, 13), "price": 100.0, "qty": 0.5, "t": [BASE + i * 5000 + 1000 for i in range(12)],
                               "sell": [i % 2 == 0 for i in range(12)]})
        bars = aggregate_trades(trades, 30, ltf_options(30), BASE, BASE + 60000)
        self.manager.feeds[(BTC, 30)].apply_backfill(bars)
        self.manager.feeds[(OKX_BTC, 30)].apply_backfill(bars.iloc[:1])  # OKX 只补了第一根
        agg = self.manager.aggregates.feeds[("AGG.BTC", 30)]
        agg.sync_history(self.manager)
        store = agg._store(0)
        self.assertEqual(sorted(store.values), [(BASE // 1000) + 8 * 3600])
        self.assertEqual(store.values[(BASE // 1000) + 8 * 3600], (3.0, 3.0))
        self.manager.feeds[(OKX_BTC, 30)].apply_backfill(bars)             # 另一根补上之后, 汇总也跟上
        agg.sync_history(self.manager)
        self.assertEqual(len(agg._store(0).values), 2)
        self.assertEqual(len(agg._store(5).values), 2)

    def test_history_sync_only_rechecks_what_changed_but_still_repairs_it(self):
        self.manager.ensure("AGG.BTC", 30)
        self.manager._sync(time.monotonic())
        trades = pd.DataFrame({"id": range(1, 25), "price": 100.0, "qty": 0.5,
                               "t": [BASE + i * 5000 + 1000 for i in range(24)], "sell": [i % 3 == 0 for i in range(24)]})
        bars = aggregate_trades(trades, 30, ltf_options(30), BASE, BASE + 120000)
        for symbol in (BTC, OKX_BTC):
            self.manager.feeds[(symbol, 30)].apply_backfill(bars)
        agg = self.manager.aggregates.feeds[("AGG.BTC", 30)]
        agg.sync_history(self.manager)
        store = agg._store(0)
        first = (BASE // 1000) + 8 * 3600
        right = store.values[first]
        # 汇总自己的文件被改了一根(比如实时窗口写进去的), 下一次合成照样改回来
        store.save_completed(pd.DataFrame({"time": [first], "buy": [9.0], "sell": [9.0], "delta": [0.0],
                                           "unknown": [0.0], "buyLegacy": [9.0], "sellLegacy": [9.0],
                                           "coverage": ["complete"], "hasBaseline": [True]}), final=True)
        self.assertEqual(agg._sync_since(0, [self.manager.feeds[(s, 30)]._store(0) for s in (BTC, OKX_BTC)], store),
                         first)
        agg.sync_history(self.manager)
        self.assertEqual(store.values[first], right)
        self.assertEqual(agg._sync_since(0, [self.manager.feeds[(s, 30)]._store(0) for s in (BTC, OKX_BTC)], store),
                         float("inf"), "都没改过: 下一次不用核对")

    def test_tail_recompute_matches_full_recompute(self):
        """汇总也只算尾部(见 AggregateFeed._parts_since): 每一轮的窗口、快照与推送和整窗口重算的一样。"""
        feed = self.manager.ensure("AGG.BTC", 10)
        self.manager._sync(time.monotonic())
        parts = [self.manager.feeds[(symbol, 10)] for symbol in (BTC, OKX_BTC)]
        for part in parts:
            part.trades.max_bars = 12                    # 窗口很快往前滑: 两家窗口的起点会错开
        full = type(feed)(feed.symbol, 10, feed.components, feed.price_digits, feed.qty_digits)
        other = os.path.join(self.temp.name, "full")
        os.makedirs(other)
        calls = []
        combine = feed.combine_since
        feed.combine_since = lambda manager, ltf, since=None: (calls.append(since), combine(manager, ltf, since))[1]
        ids, t = [1, 10_001], BASE + 300
        for step in range(40):
            if step in (15, 32):
                ids[0] += 5                              # 第一家断线留洞, 正赶上一段没成交: 它少几根, 两家窗口起点错开
            t += 40_000 if step % 29 == 15 else 900 + (step * 613) % 2300
            for index, part in enumerate(parts):
                if step % 5 == 2 and index == 1:
                    continue                             # 另一家这段没成交
                for k in range(1 + step % 3):
                    part.trades.add(ids[index], 100.0 + (step * 7 + k) % 9 * 0.1,
                                    0.01 * (1 + (step + k) % 5), t + 100 * k + index, (step + k) % 2 == 0)
                    ids[index] += 1
                part.request(ltf=0)
                part.recompute(lambda _: None)           # 各家先算(写各自的历史), 同管理线程的顺序
            if step % 20 == 19:
                feed.sync_history(self.manager)
                with patch.object(ingest, "DATA_DIR", other):
                    full.sync_history(self.manager)
            got, want = [], []
            feed.recompute(self.manager, got.append)
            full._tail_marks.clear()
            full._record_state.clear()
            full._part_marks.clear()
            with patch.object(ingest, "DATA_DIR", other):
                full.recompute(self.manager, want.append)
            self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(want, sort_keys=True), step)
            self.assertEqual(json.dumps(feed.snapshots, sort_keys=True), json.dumps(full.snapshots, sort_keys=True), step)
            pd.testing.assert_frame_equal(feed.latest[0][1], full.latest[0][1])
        self.assertGreater(sum(since is not None for since in calls), len(calls) // 2, "大部分轮次应当只算尾部")

    def test_unknown_aggregates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "多所汇总"):
            self.manager.ensure("AGG.NOPE", 30)
        self.assertFalse(self.manager.known("AGG.NOPE"))
        self.assertEqual(self.manager.label("AGG.BTC"), "BTC 永续 · 多所汇总")


if __name__ == "__main__":
    unittest.main()
