"""大额检测: 先筛原始事件, 再聚合；独立于图表周期与买卖量拆分口径。"""
import copy
import json
import math
import os
import unittest
from unittest.mock import patch

import pandas as pd

from indicator import GAP_NS, TZ_SHIFT_S
from large_trades import LargeTradeDetector, decorate_bars, default_settings, settings_for, NS
from tick_analytics import TickAnalytics

BASE = 1_800_000_000 * NS  # 整 30 秒


def detector(source="snapshot", **options):
    settings = dict(window=100, warmup=3, quantile=0.99, minRatio=1.5, minQty=20, minNotional=0)
    settings.update(options)
    return LargeTradeDetector("TEST.X", source, settings)


def raw(i, q=1, side=1, reset=False, usable=True, price=100, joined=None):
    ns = BASE + i * NS
    return ns, (ns, price, q, side, reset, usable, not reset if joined is None else joined)


def bars(tf, start=0, count=3, volume=1000):
    return [{"time": BASE // NS + TZ_SHIFT_S + start + i * tf, "volume": volume} for i in range(count)]


def futures_ticks():
    """200 个正量事件预热后, 两个不同 SDK 序号在同一时刻分别成交 40/80 手。"""
    offsets = [*range(201), 201, 201, 202]
    return pd.DataFrame({"id": range(len(offsets)), "datetime": [BASE + i * NS for i in offsets],
                         "last_price": 100., "ask_price1": 100., "bid_price1": 99.,
                         "volume": [*range(100, 301), 340, 420, 421]})


class FuturesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ticks = futures_ticks()
        self.analytics = TickAnalytics()
        self.detector = detector(window=2000, warmup=200)

    def sync(self, ticks, initial=False):
        self.analytics.update(ticks)
        self.detector.sync_futures(self.analytics.frame, self.analytics.changed_ns, initial)

    def summary(self):
        values = bars(30, start=180, count=1)
        decorate_bars(values, [self.detector], 30)
        return values[0]["large"]

    def test_same_timestamp_ticks_keep_both_events_and_repeated_sync_is_stable(self):
        self.sync(self.ticks, initial=True)
        summary = self.summary()
        self.assertEqual((summary["count"], summary["buy"], summary["delta"], summary["cvd"]),
                         (2, 120, 120, 120))
        expected_events = copy.deepcopy(self.detector.events)
        expected_window = list(self.detector._window)
        for initial in (False, False, True, False):
            self.detector.sync_futures(self.analytics.frame, self.analytics.changed_ns, initial)
            self.assertEqual(self.detector.events, expected_events)
            self.assertEqual(list(self.detector._window), expected_window)
            self.assertEqual(self.summary(), summary)

    def test_same_timestamp_ticks_arriving_in_separate_updates_are_not_revisions(self):
        self.sync(self.ticks.iloc[:202], initial=True)
        self.assertEqual(self.summary()["buy"], 40)
        self.sync(self.ticks.iloc[:203])
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (2, 120))
        self.sync(self.ticks)
        self.assertEqual((self.summary()["count"], self.summary()["cvd"]), (2, 120))

    def test_revising_either_same_timestamp_tick_matches_full_replay(self):
        for volumes, expected_qty in [([301, 381, 382], 80), ([340, 341, 342], 40)]:
            with self.subTest(volumes=volumes):
                self.analytics = TickAnalytics()
                self.detector = detector(window=2000, warmup=200)
                self.sync(self.ticks, initial=True)
                revised = self.ticks.copy()
                revised.loc[201:, "volume"] = volumes
                self.sync(revised)
                fresh = detector(window=2000, warmup=200)
                full = TickAnalytics()
                full.update(revised)
                fresh.sync_futures(full.frame, full.changed_ns, initial=True)
                self.assertEqual(self.detector.events, fresh.events)
                self.assertEqual(list(self.detector._window), list(fresh._window))
                self.assertEqual((self.summary()["count"], self.summary()["buy"], self.summary()["cvd"]),
                                 (1, expected_qty, expected_qty))

    def test_new_window_at_same_timestamp_resets_baseline_without_overlap(self):
        self.sync(self.ticks.iloc[:202], initial=True)
        tail = self.ticks.iloc[202:].copy()
        tail.loc[203, "volume"] = 920
        peer = TickAnalytics()
        peer.update(tail)
        self.detector.sync_futures(peer.frame, peer.changed_ns, initial=True)
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (1, 40))
        self.assertEqual(list(self.detector._window), [500])

    def test_timestamp_fallback_is_not_checked_as_a_consecutive_sdk_id(self):
        ticks = self.ticks.drop(columns="id")
        ticks.loc[202:, "datetime"] += NS
        self.sync(ticks, initial=True)
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (2, 120))


class DetectionTests(unittest.TestCase):
    def test_previous_baseline_only_and_absolute_floor(self):
        d = detector()
        d.update([raw(0, 1), raw(1, 2), raw(2, 3), raw(3, 30)])
        e = d.event_rows()[0]
        self.assertEqual(e["ratio"], 15)  # 均值是 (1+2+3)/3, 不能包含本次的 30
        self.assertAlmostEqual(e["z"], 28 / math.sqrt(2 / 3))
        self.assertAlmostEqual(e["threshold"], 2.98)
        unchanged = copy.deepcopy(e)
        d.update([raw(4, 1000)])
        self.assertEqual(d.event_rows()[0], unchanged)
        small = detector()
        small.update([raw(0), raw(1), raw(2), raw(3, 10)])
        self.assertEqual(small.events, {})  # 虽然 10 倍, 仍小于 20 手

    def test_warmup_constant_volume_quantile_and_rolling_window(self):
        d = detector(window=3)
        d.update([raw(0, 100), raw(1, 100), raw(2, 100), raw(3, 100)])
        self.assertEqual(d.events, {})  # 不能因 std=0 或 >=P99 就把普通成交当大额
        d.update([raw(4), raw(5), raw(6), raw(7, 30)])
        self.assertEqual(d.event_rows()[0]["ratio"], 30)
        self.assertIsNone(d.event_rows()[0]["z"])
        json.dumps(d.event_rows(), allow_nan=False)

    def test_duplicates_and_revisions_match_full_replay(self):
        rows = [raw(0), raw(1), raw(2), raw(3, 40), raw(4, 80), raw(5)]
        d = detector()
        d.update(rows)
        expected = copy.deepcopy(d.events)
        d.update(rows)
        self.assertEqual(d.events, expected)
        rows[3] = raw(3, 2)
        d.update([rows[3]])
        fresh = detector()
        fresh.update(rows)
        self.assertEqual(d.events, fresh.events)
        self.assertEqual(list(d._window), list(fresh._window))
        self.assertEqual(d.readiness, fresh.readiness)

    def test_crypto_notional_floor_id_gap_and_duplicate(self):
        d = detector("aggregate", minQty=0, minNotional=10000)
        for i in range(1, 4):
            d.trade(i, 100, 1, BASE // 1_000_000 + i, False)
        d.trade(4, 100, 50, BASE // 1_000_000 + 4, False)
        self.assertEqual(d.events, {})  # 只有 5000 USDT
        d.trade(5, 100, 120, BASE // 1_000_000 + 5, True)
        self.assertEqual(d.event_rows()[0]["side"], -1)
        d.trade(5, 100, 120, BASE // 1_000_000 + 5, True)
        self.assertEqual(len(d.events), 1)
        d.trade(9, 100, 1000, BASE // 1_000_000 + 9, False)
        self.assertEqual(len(d.events), 1)  # 漏号后重建基线, 首笔不发信号
        self.assertFalse(d.readiness[9][1])

    def test_futures_reset_volume_gap_id_gap_and_nonoverlapping_window(self):
        def frame(offsets, volumes, ids=None):
            result = pd.DataFrame({"datetime": [BASE + i * NS for i in offsets], "volume": volumes,
                                   "last_price": 100., "side_lr": 1})
            result["_key"] = offsets if ids is None else ids
            return result
        initial = frame(range(5), [100, 101, 102, 103, 143])
        d = detector()
        d.sync_futures(initial, initial=True)
        self.assertEqual(len(d.events), 1)
        for tail in [frame([4, 5, 6, 7], [143, 0, 300, 600]),  # 跨交易日清零
                     frame([4, 10, 11, 12], [143, 5000, 5300, 5600]),  # 断线补量
                     frame([4, 5, 6, 7], [143, 5000, 5300, 5600], [4, 8, 9, 10]),
                     frame([5, 6, 7], [5000, 5300, 5600])]:  # 无重叠的新窗口
            case = detector()
            case.sync_futures(initial)
            case.sync_futures(tail, changed_ns=int(tail.datetime.iloc[0]))
            self.assertEqual(len(case.events), 1)
            self.assertLess(len(case._window), case.settings["warmup"])

    def test_futures_quiet_gap_with_consecutive_ids_keeps_baseline_and_judges_tick(self):
        def frame(offsets, volumes, ids=None):
            result = pd.DataFrame({"datetime": [BASE + i * NS for i in offsets], "volume": volumes,
                                   "last_price": 100., "side_lr": 1})
            result["_key"] = range(len(offsets)) if ids is None else ids
            return result
        # 200 条预热后静默 6 秒、2 分钟各一次, 序号连续: 都不是断档, 静默后的 300 手照常判为大额。
        offsets = [*range(201), 207, 327, 328]
        volumes = [*range(100, 301), 302, 304, 604]
        d = detector(window=2000, warmup=200)
        d.sync_futures(frame(offsets, volumes), initial=True)
        self.assertEqual([e["qty"] for e in d.event_rows()], [300])
        self.assertEqual(len(d._window), 203)
        self.assertTrue(all(ready for _, ready, _ in list(d.readiness.values())[201:]))
        # 时间戳回退键不能检查漏号: 超过 GAP_NS 才当断档, 短静默照常。
        fallback = frame(offsets, volumes).assign(_key=lambda f: f.datetime)
        fallback.attrs["key_column"] = "datetime"
        d = detector(window=2000, warmup=200)
        d.sync_futures(fallback, initial=True)
        self.assertTrue(d.readiness[BASE + 207 * NS][1])
        self.assertGreater(120 * NS, GAP_NS)
        self.assertFalse(d.readiness[BASE + 327 * NS][1])
        self.assertEqual(d.events, {})

    def test_session_close_bar_is_complete_when_no_tick_was_missed(self):
        # 最后一笔在收盘前 0.5 秒; 下一笔是换交易日(累计量回落)的重置记录。
        def frame(ids):
            offsets = [*range(-5, 30), 29.5, 400]
            volumes = [*range(100, 135), 136, 3]
            return pd.DataFrame({"datetime": [BASE + int(i * NS) for i in offsets], "volume": volumes,
                                 "last_price": 100., "side_lr": 1, "_key": ids})
        for ids, expected in [(range(37), "complete"), ([*range(36), 99], "partial")]:
            with self.subTest(ids=ids):
                d = detector()
                d.sync_futures(frame(ids), initial=True)
                values = bars(30, count=1)
                decorate_bars(values, [d], 30)
                self.assertEqual(values[0]["large"]["coverage"], expected)

    def test_crypto_invalid_trade_keeps_sequence_without_resetting_baseline(self):
        d = detector("trade", minQty=0, minNotional=0)
        for i in range(1, 5):
            d.trade(i, 100, 1, BASE // 1_000_000 + i, False)
        d.trade(5, 100, 0, BASE // 1_000_000 + 5, False)            # 零量成交: 没东西可判, 区间不断
        d.trade(6, float("nan"), 3, BASE // 1_000_000 + 6, False)   # 坏价格: 本条不判, 但不是漏号
        d.trade(7, 100, 50, BASE // 1_000_000 + 7, False)
        self.assertEqual(len(d._window), 5)
        self.assertTrue(d.readiness[5][1])
        self.assertFalse(d.readiness[6][1])
        self.assertTrue(d.readiness[7][1])
        self.assertEqual([e["qty"] for e in d.event_rows()], [50])
        json.dumps(d.raw[6], allow_nan=False)

    def test_bad_environment_setting_falls_back_to_defaults_instead_of_breaking_feeds(self):
        for text in ["{", "[]", '{"*": 5}', '{"*": {"minQty": "多"}}', '{"*": {"window": 10, "warmup": 20}}']:
            with self.subTest(text=text), patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": text}):
                with self.assertRaises(ValueError):
                    settings_for("TEST.X", "snapshot")
                with patch("builtins.print"):
                    self.assertEqual(LargeTradeDetector("TEST.X").settings, default_settings("snapshot"))
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": "  "}):
            self.assertEqual(settings_for("TEST.X", "trade"), default_settings("trade"))

    def test_environment_overrides_are_per_symbol_and_validated(self):
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": '{"*":{"warmup":50},"TEST.X":{"minQty":80}}'}):
            self.assertEqual(settings_for("TEST.X", "snapshot")["minQty"], 80)
            self.assertEqual(settings_for("OTHER.Y", "snapshot")["minQty"], 20)
            self.assertEqual(settings_for("TEST.X", "snapshot")["warmup"], 50)
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": '{"*":{"window":10,"warmup":20}}'}):
            with self.assertRaises(ValueError):
                settings_for("TEST.X", "snapshot")


class AggregationTests(unittest.TestCase):
    def test_period_boundary_signed_sums_share_and_cvd_window_anchor(self):
        for source, expected_index in [("snapshot", 0), ("trade", 1)]:
            d = detector(source)
            rows = [raw(i) for i in range(-5, 32)]
            rows[15] = raw(10, 40, 1)
            rows[25] = raw(20, 80, -1)
            rows[30] = raw(25, 120, 0)
            d.update(rows)
            small, big = bars(10), bars(30, count=1)
            decorate_bars(small, [d], 10)
            decorate_bars(big, [d], 30)
            self.assertEqual(small[expected_index]["large"]["buy"], 40)
            full = big[0]["large"]
            self.assertEqual((full["buy"], full["sell"], full["unknown"], full["count"]), (40, 80, 120, 3))
            self.assertEqual(full["delta"], -40)
            self.assertEqual(full["cvd"], -40)
            self.assertEqual(full["coverage"], "complete")
            self.assertEqual(full["share"], .24)
            self.assertEqual(sum(b["large"]["count"] for b in small), full["count"])
            self.assertEqual(sum(b["large"]["delta"] for b in small), full["delta"])
            self.assertEqual(small[-1]["large"]["cvd"], full["cvd"])
            shifted = bars(10, start=20, count=1)
            decorate_bars(shifted, [d], 10)
            self.assertEqual(shifted[0]["large"]["cvd"], -40)  # 滚动图窗不重置 CVD

    def test_unknown_and_uncollected_are_not_zero_selling(self):
        d = detector()
        d.update([raw(1), raw(2), raw(3), raw(4, 40, 0), raw(5)])
        values = bars(10, start=-20, count=4)
        decorate_bars(values, [d], 10)
        self.assertEqual(values[0]["large"]["coverage"], "missing")
        self.assertIsNone(values[0]["large"]["cvd"])
        s = values[2]["large"]
        self.assertEqual((s["unknown"], s["delta"], s["count"]), (40, 0, 1))
        self.assertEqual(s["coverage"], "partial")
        self.assertIsNone(s["share"])

    def test_multiple_sources_keep_baselines_separate_and_missing_venue_is_partial(self):
        a, b = detector("aggregate"), detector("trade")
        a.update([raw(i) for i in range(-5, 32)])
        b.update([raw(i, 100) for i in range(-5, 32)])
        a.update([raw(10, 40)])
        b.update([raw(10, 40)])  # 同样 40, 对另一数据源不是大额
        full = bars(30, count=1)
        decorate_bars(full, [a, b], 30)
        self.assertEqual(full[0]["large"]["buy"], 40)
        self.assertEqual(full[0]["large"]["coverage"], "complete")
        decorate_bars(full, [a, detector("trade")], 30)
        self.assertEqual(full[0]["large"]["coverage"], "partial")
        self.assertIsNone(full[0]["large"]["share"])


class FeedIntegrationTests(unittest.TestCase):
    def test_futures_native_periods_and_rollup_share_detector_and_publish_revisions(self):
        import ingest
        manager = ingest.FeedManager()
        small = manager._new_feed(("SHFE.fu2611", 10))
        manager.feeds[(small.symbol, 10)] = small
        big = manager._new_feed((small.symbol, 30))
        manager.feeds[(big.symbol, 30)] = big
        rollup = manager._new_feed((big.symbol, 60))
        self.assertIs(small.large_trades, big.large_trades)
        self.assertIs(rollup.large_sources()[0], big.large_trades)
        d = small.large_trades
        d.settings = detector().settings
        d.update([raw(i) for i in range(-5, 32)])
        d.update([raw(10, 40)])
        frame = pd.DataFrame(bars(30, count=1))
        first, _ = big._snapshot_message(0, frame, {}, 1, 0)
        other, _ = big._snapshot_message(5, frame, {}, 1, 0)
        self.assertIs(first["bars"][0]["large"], other["bars"][0]["large"])  # 各粒度同一份, 不重算
        self.assertEqual(first["cfg"]["largeTrades"][0]["source"], "snapshot")
        d.update([raw(10, 1)])
        revised, message = big._snapshot_message(0, frame, {0: first}, 2, 0)
        self.assertEqual(first["bars"][0]["large"]["count"], 1)  # 旧快照不能被就地改掉
        self.assertEqual(revised["bars"][0]["large"]["count"], 0)
        self.assertEqual(message["bars"][0]["large"]["count"], 0)

    def test_crypto_manager_deduplicates_shared_detector_and_backfill_does_not_fake_signals(self):
        from binance_feed import BinanceAdapter
        from crypto_feed import CryptoManager
        adapter = BinanceAdapter()
        instrument = adapter.builtin_instruments()[0]
        manager = CryptoManager([adapter])
        for tf in (10, 30):
            feed = manager._new_feed(instrument, tf)
            feed.loaded = True
            manager.feeds[(instrument.symbol, tf)] = feed
        a, b = manager.feeds.values()
        self.assertIs(a.large_trades, b.large_trades)
        self.assertIsNone(b.trades.large_trades)  # 只由一个周期喂成交
        self.assertEqual(a.large_trades.source, "aggregate")
        a.large_trades.settings = detector("aggregate", minQty=0, minNotional=10000).settings
        for i, qty in enumerate([1, 1, 1, 120], 1):
            manager._apply_event(("trade", instrument.symbol, i, 100., qty, BASE // 1_000_000 + i * 1000, False))
        self.assertIs(a.large_trades, b.large_trades)
        self.assertEqual(len(a.large_trades.raw), 4)
        self.assertEqual(len(a.large_trades.events), 1)
        manager._apply_event(("trade", instrument.symbol, 4, 100., 120, BASE // 1_000_000 + 4000, False))
        self.assertEqual(len(a.large_trades.events), 1)
        manager._apply_event(("trade", instrument.symbol, 9, 100., 1, BASE // 1_000_000 + 9000, False))
        a.trades.insert([(5, 100., 1000, BASE // 1_000_000 + 5000, True)], before_id=9)
        self.assertEqual(len(a.large_trades.events), 1)
        self.assertFalse(a.large_trades.readiness[9][1])


if __name__ == "__main__":
    unittest.main()
