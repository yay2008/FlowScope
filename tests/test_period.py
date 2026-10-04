"""主图周期(tf)回归: 10s/30s 并列, 各周期独立聚合、独立落盘、互不串消息。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import ingest
from indicator import (CFG, DEFAULT_TF_SEC, TF_OPTIONS, bar_ns_for, build_bars,
                       build_bars_from_ltf, ltf_options, split_ticks_to_bars)
from test_data import BASE


class Clock:
    """可推进的单调时钟, 让冷却期/回收期测试与真实等待无关。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def frame(rows):
    """rows: (offset_sec, last_price, ask_price1, bid_price1, cumulative_volume)"""
    return pd.DataFrame({
        "datetime": [BASE + int(r[0] * 10**9) for r in rows],
        "last_price": [r[1] for r in rows],
        "ask_price1": [r[2] for r in rows],
        "bid_price1": [r[3] for r in rows],
        "volume": [r[4] for r in rows],
    })


def klines(seconds, count):
    return pd.DataFrame({
        "datetime": [BASE + i * seconds * 10**9 for i in range(count)],
        "open": [4287.] * count, "high": [4290.] * count,
        "low": [4286.] * count, "close": [4289.] * count,
        "volume": [100.] * count,
    })


class PeriodOptionTests(unittest.TestCase):
    def test_ten_and_thirty_are_parallel_options(self):
        self.assertIn(10, TF_OPTIONS)
        self.assertIn(30, TF_OPTIONS)
        self.assertEqual(CFG["tfOptions"], TF_OPTIONS)
        self.assertEqual(DEFAULT_TF_SEC, 30)

    def test_bar_width_follows_the_period(self):
        self.assertEqual(bar_ns_for(10), 10 * 10**9)
        self.assertEqual(bar_ns_for(30), 30 * 10**9)

    def test_illegal_period_falls_back_to_default(self):
        for bad in [0, 7, 45, 120, None, "x", "10.5"]:
            self.assertEqual(bar_ns_for(bad), DEFAULT_TF_SEC * 10**9)

    def test_split_granularity_must_divide_the_period(self):
        self.assertEqual(ltf_options(30), [0, 1, 5, 10, 15, 30])
        # 10s 下 15/30 既不能整除也不比主周期细, 必须消失
        self.assertEqual(ltf_options(10), [0, 1, 5, 10])


class AggregationTests(unittest.TestCase):
    def test_ticks_are_bucketed_by_the_selected_period(self):
        data = frame([(0, 4287, 4287, 4286, 100), (5, 4288, 4288, 4287, 150),
                      (9, 4289, 4289, 4288, 200),
                      (10, 4287, 4287, 4286, 260), (15, 4286, 4287, 4286, 300),
                      (19, 4287, 4287, 4286, 340),
                      (20, 4288, 4288, 4287, 400)])
        ten = split_ticks_to_bars(data, 0, bar_ns=bar_ns_for(10))
        thirty = split_ticks_to_bars(data, 0, bar_ns=bar_ns_for(30))
        # 0/10/20 秒三条恰在边界上, 各自记入前一根 bar
        self.assertEqual([int(i) for i in ten.index],
                         [BASE - 10**10, BASE, BASE + 10**10])
        self.assertEqual(ten.observed.tolist(), [0., 160., 140.])
        self.assertEqual([int(i) for i in thirty.index], [BASE - 3 * 10**10, BASE])
        # 守恒在两个周期下都成立
        for bars in (ten, thirty):
            self.assertTrue((bars.buy + bars.sell + bars.unknown == bars.observed).all())
        self.assertEqual(ten.observed.sum(), thirty.observed.sum())

    def test_build_bars_joins_the_period_klines_with_period_buckets(self):
        data = frame([(0, 4287, 4287, 4286, 100), (5, 4288, 4288, 4287, 200),
                      (15, 4289, 4289, 4288, 300)])
        bars = build_bars(klines(10, 2), data, 0, bar_ns=bar_ns_for(10))
        self.assertEqual(len(bars), 2)
        # observed 是内部列, 对外只能通过三个桶之和观察
        self.assertEqual((bars.buy + bars.sell + bars.unknown).tolist(), [100., 100.])
        self.assertEqual(bars.time.tolist(),
                         [int(BASE // 10**9) + 28800, int(BASE // 10**9) + 28800 + 10])

    def test_kline_split_rejects_granularity_too_coarse_for_the_period(self):
        lower = klines(15, 2)
        with self.assertRaises(ValueError):
            build_bars_from_ltf(klines(10, 1), lower, 15, bar_ns=bar_ns_for(10))
        # 同一个 15s 粒度在 30s 主周期下合法
        self.assertFalse(build_bars_from_ltf(klines(30, 1), lower, 15,
                                             bar_ns=bar_ns_for(30)).empty)


class FeedPeriodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_feed_carries_its_period(self):
        feed = ingest.Feed("KQ.m@SHFE.fu", 10)
        self.assertEqual(feed.tf, 10)
        self.assertEqual(feed.bar_ns, 10 * 10**9)
        self.assertEqual(feed.analytics.bar_ns, 10 * 10**9)
        self.assertEqual(ingest.Feed("KQ.m@SHFE.fu").tf, DEFAULT_TF_SEC)

    def test_stores_are_isolated_per_period(self):
        ten = ingest.Feed("KQ.m@SHFE.fu", 10)._store(0).path
        thirty = ingest.Feed("KQ.m@SHFE.fu", 30)._store(0).path
        self.assertNotEqual(ten, thirty)
        self.assertIn("10s", ten.name)
        # 向后兼容: 30s 的文件名必须与历史文件完全一致, 否则会丢掉既有历史
        self.assertEqual(thirty.name, "KQ_m_SHFE_fu_30s_ltf0_v3.csv")

    def test_only_thirty_second_period_backfills_legacy_files(self):
        self.assertEqual(ingest.Feed("KQ.m@SHFE.fu", 10)._store(0).legacy, {})
        # 30s 会去读 v2/更早的 legacy 文件(此处目录为空, 故仍为空字典, 但走了回填分支)
        self.assertEqual(ingest.Feed("KQ.m@SHFE.fu", 30)._store(0).legacy, {})

    def test_illegal_split_falls_back_to_tick_mode_on_a_ten_second_feed(self):
        feed = ingest.Feed("KQ.m@SHFE.fu", 10)
        feed.request(ltf=15)          # 10s 周期下 15s 非法
        self.assertEqual(set(feed._requested), {0})
        feed.request(ltf=5)           # 合法
        self.assertIn(5, feed._requested)

    def test_lower_period_subscription_scales_with_the_period(self):
        api = Mock()
        api.get_kline_serial.return_value = klines(5, 3)
        feed = ingest.Feed("KQ.m@SHFE.fu", 10)
        feed.lower_klines[10] = feed.klines = klines(10, 3)
        feed.request(ltf=5)
        feed.ensure_ltf_subscriptions(api)
        api.get_kline_serial.assert_called_once_with(feed.symbol, 5, data_length=4001)


class ManagerPeriodTests(unittest.TestCase):
    def test_same_symbol_different_period_are_separate_feeds(self):
        manager = ingest.FeedManager()
        thirty = manager.ensure("KQ.m@SHFE.fu", 30)
        ten = manager.ensure("KQ.m@SHFE.fu", 10)
        self.assertIsNot(thirty, ten)
        self.assertIs(manager.ensure("KQ.m@SHFE.fu", 30), thirty)
        self.assertEqual(len(manager.feeds), 2)
        self.assertEqual(manager.status_snapshot()["feeds"],
                         ["KQ.m@SHFE.fu@10s", "KQ.m@SHFE.fu@30s"])

    def test_bars_do_not_leak_across_periods(self):
        manager = ingest.FeedManager()
        q = asyncio.Queue()
        manager.add_client(q, "KQ.m@SHFE.fu", 0, False, 10)
        manager._fanout({"type": "bars", "symbol": "KQ.m@SHFE.fu", "tf": 30, "ltf": 0, "bars": []})
        self.assertTrue(q.empty())
        manager._fanout({"type": "bars", "symbol": "KQ.m@SHFE.fu", "tf": 10, "ltf": 0, "bars": []})
        self.assertFalse(q.empty())

    def test_footprint_does_not_leak_across_periods(self):
        manager = ingest.FeedManager()
        q = asyncio.Queue()
        manager.add_client(q, "KQ.m@SHFE.fu", 0, True, 30)
        manager._fanout({"type": "footprints", "symbol": "KQ.m@SHFE.fu", "tf": 10,
                         "tickSize": 1, "bars": []})
        self.assertTrue(q.empty())
        manager._fanout({"type": "footprints", "symbol": "KQ.m@SHFE.fu", "tf": 30,
                         "tickSize": 1, "bars": []})
        self.assertFalse(q.empty())

    def test_period_config_narrows_the_split_options(self):
        self.assertEqual(ingest.period_cfg(10)["ltfOptions"], [0, 1, 5, 10])
        self.assertEqual(ingest.period_cfg(30)["ltfOptions"], [0, 1, 5, 10, 15, 30])
        self.assertEqual(ingest.period_cfg(10)["tf"], 10)



if __name__ == "__main__":
    unittest.main()
