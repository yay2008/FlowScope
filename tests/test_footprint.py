"""足迹的真实分价量、周期边界、缺失覆盖和推送隔离。"""
import asyncio
import time
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException

import app as server
import footprint
import ingest
from crypto_feed import TradeBars
from indicator import TZ_SHIFT_S
from test_crypto import BASE, make_trades
from test_rollup import BTC, OKX, START_MS, CryptoRollupCase
from test_rollup import SYMBOL, T0, base_bars, klines_at


def source(*bars, tick=.1):
    return {"tickSize": tick, "bars": [
        {"time": t, "levels": levels, "coverage": quality} for t, levels, quality in bars]}


def quantities(data):
    return [sum(level[i] for bar in data["bars"] for level in bar["levels"]) for i in (1, 2, 3)]


class FootprintMathTests(unittest.TestCase):
    def test_price_bands_conserve_both_sides_and_unknown_without_changing_raw(self):
        raw = source((100, [[100 + i / 10, i / 10, i / 5, i / 100] for i in range(160)], "partial"))
        shown = footprint.for_display(raw)
        self.assertEqual((shown["tickSize"], shown["priceTick"]), (.5, .1))
        self.assertLessEqual(len(shown["bars"][0]["levels"]), footprint.BASE_ROWS + 1)
        for before, after in zip(quantities(raw), quantities(shown)):
            self.assertAlmostEqual(before, after)
        self.assertEqual(len(raw["bars"][0]["levels"]), 160)
        self.assertEqual(shown["bars"][0]["coverage"], "partial")

    def test_decimal_prices_on_band_boundaries_do_not_fall_into_previous_row(self):
        raw = source((100, [[100.1, 1, 2, 0], [100.2, 3, 4, 0]], "complete"))
        shown = footprint.for_display(raw)
        self.assertEqual(shown["bars"], raw["bars"])
        self.assertEqual((shown["tickSize"], shown["priceTick"]), (.1, .1))
        self.assertEqual(footprint.common_tick([.2, .5, None]), .1)

    def test_base_band_follows_typical_bar_span_and_ignores_a_single_spike(self):
        typical = [(100 + i * 30, [[100 + j / 10, 1, 1, 0] for j in range(65)], "complete") for i in range(9)]
        spike = (400, [[100, 1, 1, 0], [200, 1, 1, 0]], "complete")
        shown = footprint.for_display(source(*typical, spike))
        self.assertEqual((shown["tickSize"], shown["priceTick"]), (.2, .1), "中位跨度 6.4 / 32 行 = 2 tick")
        self.assertEqual(len(shown["bars"][0]["levels"]), 33)
        # 期货同样分档：最小价位 1、单根跨 200 tick 的品种不能一行一个价位
        wide = source(*[(100 + i * 30, [[1000, 1, 0, 2], [1200, 0, 3, 0]], "complete") for i in range(3)], tick=1)
        shown = footprint.for_display(wide)
        self.assertEqual((shown["tickSize"], shown["priceTick"]), (10, 1))
        self.assertEqual(shown["bars"][0]["levels"], [[1000, 1, 0, 2], [1200, 0, 3, 0]])
        self.assertEqual(footprint.for_display(source(tick=None))["priceTick"], None)

    def test_venues_merge_actual_prices_and_keep_missing_venue_partial(self):
        a = source((100, [[10, 1, 2, 0]], "complete"), (130, [[11, 2, 3, 0]], "complete"))
        b = source((100, [[10, 3, 4, 0], [10.5, 2, 1, 0]], "complete"), tick=.5)
        result = footprint.combine([a, b])
        self.assertEqual(result["bars"][0]["levels"], [[10, 4, 6, 0], [10.5, 2, 1, 0]])
        self.assertEqual([bar["coverage"] for bar in result["bars"]], ["complete", "partial"])
        self.assertEqual(footprint.combine([a, None])["bars"][0]["coverage"], "partial")

    def test_period_buckets_follow_native_starts_and_exclude_session_gaps(self):
        raw = source((101, [[10, 1, 2, 0]], "complete"), (131, [[10, 3, 4, 0]], "complete"),
                     (161, [[11, 5, 6, 0]], "complete"), (221, [[12, 9, 9, 0]], "complete"),
                     (261, [[13, 7, 8, 0]], "complete"))
        candles = [{"time": 101, "volume": 3}, {"time": 131, "volume": 18},
                   {"time": 261, "volume": 15}]
        result = footprint.rollup(raw, candles, 60)
        self.assertEqual([bar["time"] for bar in result["bars"]], [101, 131, 261])
        self.assertEqual(quantities(result), [16, 20, 0])
        self.assertTrue(all(bar["coverage"] == "complete" for bar in result["bars"]))

    def test_native_volume_and_source_coverage_both_must_be_complete(self):
        raw = source((100, [[10, 1, 2, 0]], "complete"), (160, [[10, 3, 4, 0]], "partial"))
        candles = [{"time": 100, "volume": 4}, {"time": 160, "volume": 7}]
        self.assertEqual([bar["coverage"] for bar in footprint.rollup(raw, candles, 60)["bars"]],
                         ["partial", "partial"])

    def test_removal_or_new_step_replaces_snapshot_and_unchanged_rows_are_not_resent(self):
        old = {"symbol": BTC, "tf": 30, "revision": 1,
               **source((100, [[10, 1, 2, 0]], "complete"), (130, [[11, 2, 3, 0]], "partial"))}
        self.assertIsNone(footprint.message({**old, "revision": 2}, old))
        self.assertEqual(footprint.message({**old, "bars": old["bars"][1:]}, old)["type"], "footprint_snapshot")
        self.assertEqual(footprint.message({**old, "tickSize": .2}, old)["type"], "footprint_snapshot")
        self.assertEqual(footprint.message({**old, "priceTick": .01}, {**old, "priceTick": .1})["type"],
                         "footprint_snapshot")

    def test_cached_history_matches_rebuilding_after_repair_new_prices_and_window_trimming(self):
        merge_cache, rollup_cache, display_cache = {}, {}, {}
        a = source((100, [[100, 1, 2, 0]], "partial"), (130, [[101, 2, 3, 0]], "complete"))
        b = source((100, [[100, 3, 4, 0]], "partial"))
        candles = [{"time": 100, "volume": 50}, {"time": 160, "volume": 2}]
        for step in range(5):
            if step == 1:
                a["bars"][0] = {**a["bars"][0], "coverage": "complete"}
                b["bars"][0] = {**b["bars"][0], "coverage": "complete"}
            elif step == 2:
                a["bars"][1] = {**a["bars"][1], "levels": [[101, 3, 4, 0], [200, 2, 1, 0]]}
            elif step == 3:
                a["bars"].append({"time": 160, "levels": [[102, 1, 1, 0]], "coverage": "complete"})
            elif step == 4:
                a["bars"].pop(0)
                b["bars"].clear()
            cached = footprint.combine([a, b], merge_cache)
            rebuilt = footprint.combine([a, b])
            self.assertEqual(cached, rebuilt)
            cached = footprint.rollup(cached, candles, 60, rollup_cache)
            rebuilt = footprint.rollup(rebuilt, candles, 60)
            self.assertEqual(cached, rebuilt)
            self.assertEqual(footprint.for_display(cached, display_cache),
                             footprint.for_display(rebuilt))


class TradeFootprintTests(unittest.TestCase):
    def test_exact_thirty_second_boundary_and_duplicate_trade(self):
        trades = TradeBars(30)
        trades.add(1, 100.1, .2, BASE + 29999, False)
        trades.add(2, 100.2, .3, BASE + 30000, True)
        self.assertFalse(trades.add(2, 100.2, .3, BASE + 30000, True))
        data = trades.footprint(.1)
        self.assertEqual([bar["time"] for bar in data["bars"]],
                         [BASE // 1000 + TZ_SHIFT_S, BASE // 1000 + TZ_SHIFT_S + 30])
        self.assertEqual(quantities(data), [.2, .3, 0])
        self.assertEqual([bar["coverage"] for bar in data["bars"]], ["partial", "complete"])

    def test_gap_repair_adds_only_missing_volume_and_updates_cached_prices(self):
        trades = TradeBars(30)
        trades.add(1, 100, 1, BASE + 1000, False)
        trades.add(4, 101, 1, BASE + 31000, True)
        trades.holes.pop(0)
        trades.footprint(.1)
        self.assertTrue(trades.insert([(2, 100, 2, BASE + 2000, True),
                                       (3, 101, 3, BASE + 30000, False)], 4))
        data = trades.footprint(.1)
        self.assertEqual(quantities(data), [4, 3, 0])
        self.assertTrue(all(bar["coverage"] == "complete" for bar in data["bars"]))
        self.assertFalse(trades.insert([], 4))

    def test_stored_total_volume_cannot_make_partial_price_levels_complete(self):
        trades = TradeBars(30)
        trades.add(10, 100, 1, BASE + 1000, False)
        trades.put_stored([BASE], [[100, 100, 100, 100, 20]])
        data = trades.footprint(.1)
        self.assertEqual(quantities(data), [1, 0, 0])
        self.assertEqual(data["bars"][0]["coverage"], "partial")

    def test_closed_bars_keep_price_levels_compactly_with_the_same_footprint(self):
        trades = TradeBars(30)
        for i, (price, sell) in enumerate([(100.5, False), (100.1, True), (100.5, True), (100.3, False)], 1):
            trades.add(i, price, i * 0.1, BASE + i * 1000, sell)
        open_bar = trades.footprint(.1)["bars"][0]["levels"]
        trades.add(5, 101, 1, BASE + 31000, False)
        self.assertIsInstance(trades.price_levels[BASE], np.ndarray)
        self.assertIsInstance(trades.price_levels[BASE + 30000], dict)
        trades.drop_footprint_rows()
        self.assertEqual(trades.footprint(.1)["bars"][0]["levels"], open_bar)

    def test_price_cache_is_bounded_and_ten_second_candles_do_not_duplicate_it(self):
        with patch("crypto_feed.FOOTPRINT_BARS", 3), patch("crypto_feed.TRIM_SLACK", 0):
            trades = TradeBars(30)
            for i in range(10):
                trades.add(i, 100, 1, BASE + i * 30000, False)
            self.assertLessEqual(len(trades.price_levels), 3)
            self.assertEqual(len(trades.footprint(.1)["bars"]), 3)
        small = TradeBars(10)
        small.add(1, 100, 1, BASE, False)
        self.assertEqual(small.price_levels, {})


class FuturesFootprintTests(unittest.TestCase):
    def test_every_native_period_uses_thirty_second_prices_and_reconciles_volume(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(ingest, "DATA_DIR", folder):
            base = ingest.Feed(SYMBOL, 30)
            base.request(footprint=True)
            for tf in (60, 300, 900, 3600, 14400):
                with self.subTest(tf=tf):
                    count = tf // 30
                    items = [(T0 + i * 30, 1, 1, 0) for i in range(count + 2)]
                    base._publish(0, base_bars(items), {}, 1)
                    base._footprint_raw = source(*[(t, [[100, 1, 0, 0]], "complete")
                                                   for t, *_ in items], tick=1)
                    feed = ingest.FuturesRollupFeed(SYMBOL, tf, lambda: base)
                    feed.klines = klines_at((T0, count), (T0 + tf, 2))
                    feed.request(footprint=True)
                    feed.recompute(lambda _: None)
                    fp = feed.footprint_snapshot()
                    self.assertEqual([bar["time"] for bar in fp["bars"]], [T0, T0 + tf])
                    self.assertEqual(quantities(fp), [count + 2, 0, 0])
                    self.assertEqual([bar["coverage"] for bar in fp["bars"]], ["complete", "complete"])


class CryptoFootprintTests(CryptoRollupCase):
    def test_all_periods_reaggregate_original_trades_for_single_and_combined_venues(self):
        self.manager.ensure("AGG.BTC", 30)
        self.manager._sync(time.monotonic())
        trades = make_trades(START_MS, 90, step_ms=4000)
        self.feed_trades(BTC, trades)
        self.feed_trades(OKX, trades)
        expected = [trades.loc[~trades.sell, "qty"].sum(), trades.loc[trades.sell, "qty"].sum(), 0]
        for symbol, factor in ((BTC, 1), ("AGG.BTC", 2)):
            for tf in (30, 60, 300, 900, 3600, 14400):
                with self.subTest(symbol=symbol, tf=tf):
                    feed = self.manager.ensure(symbol, tf)
                    feed.request(footprint=True)
                    self.compute()
                    self.assertIsNone(feed.error)
                    data = feed.footprint_snapshot()
                    self.assertIsNotNone(data)
                    self.assertEqual((data["symbol"], data["tf"]), (symbol, tf))
                    for wanted, actual in zip(expected, quantities(data)):
                        self.assertAlmostEqual(wanted * factor, actual)
                    self.assertEqual(data["bars"][0]["coverage"], "partial")
                    self.assertTrue(all((bar["time"] - TZ_SHIFT_S) % tf == 0 for bar in data["bars"]))

    def test_live_subscriptions_keep_footprints_alive_and_isolate_periods_and_views(self):
        self.manager.ensure(BTC, 60)
        self.manager._sync(time.monotonic())
        fp_queue, normal_queue, other_period = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
        self.manager.add_client(fp_queue, BTC, 5, True, 30)
        self.manager.add_client(normal_queue, BTC, 0, False, 30)
        self.manager.add_client(other_period, BTC, 0, True, 60)
        self.feed_trades(BTC, make_trades(START_MS, 10))
        self.compute()
        feed = self.manager.feeds[(BTC, 30)]
        self.assertGreater(feed._fp_until, time.monotonic())
        self.feed_trades(BTC, make_trades(START_MS + 100000, 1, first_id=11))
        feed.recompute(self.manager._fanout)
        messages = []
        while not fp_queue.empty():
            messages.append(fp_queue.get_nowait())
        self.assertTrue(any(message["type"] in {"footprints", "footprint_snapshot"} for message in messages))
        while not normal_queue.empty():
            self.assertEqual(normal_queue.get_nowait()["type"], "bars")
        self.assertTrue(other_period.empty())

    def test_rest_and_websocket_reject_footprint_below_thirty_seconds(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.footprint(BTC, 10))
        self.assertEqual(caught.exception.status_code, 400)

        class Socket:
            async def accept(self):
                pass

            async def close(self, **kwargs):
                self.closed = kwargs

        socket = Socket()
        asyncio.run(server.ws(socket, BTC, 0, True, 10))
        self.assertEqual(socket.closed["code"], 1008)
        self.assertIn("30", socket.closed["reason"])


if __name__ == "__main__":
    unittest.main()
