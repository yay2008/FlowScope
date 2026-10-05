"""大周期由 30s 合成(rollup.py): 分桶、覆盖判定、CVD 与 30s 对得上、管理器只采集底层。"""
import asyncio
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import app as server
import crypto_feed
import ingest
import rollup as rollup_module
from binance_feed import BinanceAdapter
from crypto_aggregate import AggregateBook
from crypto_feed import IDLE_RECOMPUTE_SEC, CandleStore, CryptoManager, CryptoRollupFeed, aggregate_trades
from history_store import ChangeLog, HistoryStore
from indicator import (BAR_COLUMNS, NATIVE_TFS, ROLLUP_BASE_TF, TF_OPTIONS, TZ_SHIFT_S, is_rollup,
                       ltf_options)
from okx_feed import OkxAdapter
from rollup import (ROW_COLUMNS, ClosedCache, bucket_ends, bucket_stats, candle_buckets, candles_from_klines,
                    rollup_bars, store_rows, utc_bucket)
from test_crypto import make_trades

T0 = 1_789_200_000          # 展示秒, 4 小时的整数倍(按 UTC 也是)
SYMBOL = "KQ.m@SHFE.fu"
BTC = "BINANCE.BTCUSDT.P"
OKX = "OKX.BTC-USDT-SWAP"
START_MS = 1_790_985_600_000 + 11 * 3_600_000     # 2026-10-03 11:00 UTC, 5 分钟对齐


def rows(*items):
    """items: (time, buy, sell, coverage[, volume]); unknown 记 0, 对照列同主列。"""
    data = []
    for item in items:
        t, buy, sell, coverage = item[:4]
        volume = item[4] if len(item) > 4 else buy + sell
        data.append({"time": t, "buy": buy, "sell": sell, "unknown": 0.0, "buyLegacy": buy,
                     "sellLegacy": sell, "coverage": coverage, "hasBaseline": coverage == "complete",
                     "volume": volume})
    return pd.DataFrame(data, columns=ROW_COLUMNS)


def candles(*items):
    """items: (time, volume); 价格随便给, 只看分桶与成交量。"""
    return pd.DataFrame([{"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": v}
                         for t, v in items])


def base_bars(items):
    """items: (time, volume, buy, sell) -> 30s 底层 Feed 的 bars 表(tick 口径都核对通过)。"""
    frame = pd.DataFrame([{"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": v,
                           "buy": b, "sell": s, "unknown": v - b - s, "buyLegacy": b, "sellLegacy": s,
                           "coverage": "complete", "hasBaseline": True} for t, v, b, s in items])
    frame["delta"] = frame["buy"] - frame["sell"]
    frame["deltaLegacy"] = frame["delta"]
    frame["cvd"] = np.nan
    return frame[BAR_COLUMNS]


def klines_at(*items):
    """items: (展示秒, volume) -> TqSdk K 线(datetime 为 UTC 纳秒)。"""
    return pd.DataFrame({"datetime": [(t - TZ_SHIFT_S) * 10**9 for t, _ in items],
                         "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                         "volume": [float(v) for _, v in items]})


class PeriodConstantTests(unittest.TestCase):
    def test_minutes_and_hours_are_options_but_only_ten_and_thirty_are_collected(self):
        self.assertEqual(TF_OPTIONS, [10, 30, 60, 300, 900, 3600, 14400])
        self.assertEqual(NATIVE_TFS, [10, 30])
        self.assertTrue(all(is_rollup(tf) for tf in [60, 300, 900, 3600, 14400]))
        self.assertFalse(any(is_rollup(tf) for tf in [10, 30, 45]))
        self.assertTrue(all(tf % ROLLUP_BASE_TF == 0 for tf in TF_OPTIONS if is_rollup(tf)))

    def test_frontend_offers_the_same_periods(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "chart-view.js").read_text(encoding="utf-8")
        match = re.search(r"const TF_CHOICES = \[([^\]]*)\]", source)
        self.assertIsNotNone(match, "chart-view.js 应声明 TF_CHOICES")
        self.assertEqual([int(item) for item in match.group(1).split(",")], TF_OPTIONS)

    def test_rollup_periods_accept_every_split_granularity(self):
        for tf in [60, 300, 900, 3600, 14400]:
            self.assertEqual(ltf_options(tf), [0, 1, 5, 10, 15, 30])
        self.assertEqual(ingest.validate_tf(14400), 14400)


class RollupBarsTests(unittest.TestCase):
    def test_volume_rule_needs_every_part_complete_and_the_volumes_to_add_up(self):
        out = rollup_bars(candles((T0, 40), (T0 + 60, 40), (T0 + 120, 40)),
                          rows((T0, 10, 5, "complete", 20), (T0 + 30, 15, 5, "complete", 20),
                               (T0 + 60, 10, 5, "complete", 20), (T0 + 90, 10, 5, "partial", 20)),
                          60)
        self.assertEqual(out["coverage"].tolist(), ["complete", "partial", "missing"])
        self.assertEqual(out["buy"].tolist()[:2], [25.0, 20.0])
        self.assertEqual(out["delta"].tolist()[:2], [15.0, 10.0])
        self.assertTrue(np.isnan(out["buy"].iloc[2]))

    def test_volume_rule_flags_a_bucket_whose_parts_do_not_cover_the_whole_bar(self):
        # 只采到前一根 30s: 各根都 complete, 但量不够这根 1 分钟 K 线
        out = rollup_bars(candles((T0, 40)), rows((T0, 10, 10, "complete", 20)), 60)
        self.assertEqual(out["coverage"].tolist(), ["partial"])

    def test_buckets_follow_the_native_starts_and_stop_at_the_period_end(self):
        # 原生 K 线在午休处断开: 11:30 收盘的那根之后下一根是 13:30; 落在中间的 30s(不该有)不归任何桶
        out = rollup_bars(candles((T0, 20), (T0 + 7200, 20)),
                          rows((T0, 10, 10, "complete"), (T0 + 3600, 1, 1, "complete"),
                               (T0 + 7200, 10, 10, "complete")),
                          3600)
        self.assertEqual(out["buy"].tolist(), [10.0, 10.0])
        self.assertEqual(out["coverage"].tolist(), ["complete", "complete"])

    def test_buckets_do_not_assume_epoch_alignment(self):
        out = rollup_bars(candles((T0 + 900, 40)),
                          rows((T0 + 870, 1, 1, "complete"), (T0 + 900, 10, 10, "complete"),
                               (T0 + 930, 10, 10, "complete")),
                          3600)
        self.assertEqual(out["buy"].tolist(), [20.0])

    def test_all_legacy_parts_stay_legacy(self):
        out = rollup_bars(candles((T0, 40)), rows((T0, 10, 10, "legacy"), (T0 + 30, 10, 10, "legacy")), 60)
        self.assertEqual(out["coverage"].tolist(), ["legacy"])

    def test_slot_rule_counts_thirty_second_slots_up_to_the_latest_bar(self):
        out = rollup_bars(candles((T0, 1), (T0 + 60, 1), (T0 + 120, 1)),
                          rows((T0, 1, 0, "complete"), (T0 + 30, 0, 0, "complete"),
                               (T0 + 60, 1, 0, "complete"),
                               (T0 + 120, 1, 0, "complete")),
                          60, rule="slots")
        # 中间那根缺了 T0+90; 最后一根是正在走的, 只数到 T0+120 这一个槽位
        self.assertEqual(out["coverage"].tolist(), ["complete", "partial", "complete"])

    def test_crypto_buckets_align_to_exchange_time(self):
        utc_midnight = T0 - T0 % 86400 + TZ_SHIFT_S     # 展示时间 08:00 = UTC 00:00
        self.assertEqual(utc_bucket(utc_midnight + 14370, 14400), utc_midnight)
        self.assertEqual(utc_bucket(utc_midnight - 30, 14400), utc_midnight - 14400)
        frame = pd.DataFrame({"time": [utc_midnight - 30, utc_midnight, utc_midnight + 14370],
                              "open": [1.0, 2.0, 3.0], "high": [1.0, 5.0, 3.0], "low": [1.0, 2.0, 0.5],
                              "close": [1.0, 2.0, 3.0], "volume": [1.0, 2.0, 3.0]})
        starts = np.array([utc_midnight - 14400, utc_midnight, utc_midnight + 14400])
        out = candle_buckets(frame, starts, starts + 14400)
        self.assertEqual(out["count"].tolist(), [1, 2, 0])            # 没有 30s 的桶也有一行, 根数为 0
        self.assertEqual(out.iloc[1][["open", "high", "low", "close", "volume"]].tolist(),
                         [2.0, 5.0, 0.5, 3.0, 5.0])
        self.assertTrue(np.isnan(out.iloc[2]["close"]))

    def test_candles_from_klines_use_display_seconds(self):
        out = candles_from_klines(klines_at((T0, 5), (T0 + 60, 6)))
        self.assertEqual(out["time"].tolist(), [T0, T0 + 60])
        self.assertEqual(out["volume"].tolist(), [5.0, 6.0])


class StoreRowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HistoryStore(Path(self.temp.name) / "x_30s_ltf0_v3.csv")

    def tearDown(self):
        self.temp.cleanup()

    def save(self, items, coverage="complete"):
        frame = rows(*[(t, b, s, coverage) for t, b, s in items])
        self.store.save_completed(frame, final=True)

    def test_complete_beats_partial_and_volume_comes_from_complete_rows(self):
        self.save([(T0, 1, 1), (T0 + 30, 2, 2)], coverage="partial")
        self.save([(T0 + 30, 3, 3)])
        out = store_rows(self.store, volume_store=self.store)
        self.assertEqual(out["coverage"].tolist(), ["partial", "complete"])
        self.assertEqual(out["buy"].tolist(), [1.0, 3.0])
        self.assertTrue(np.isnan(out["volume"].iloc[0]))
        self.assertEqual(out["volume"].iloc[1], 6.0)

    def test_only_rows_before_the_live_window(self):
        self.save([(T0, 1, 1), (T0 + 30, 2, 2), (T0 + 60, 3, 3)])
        self.assertEqual(store_rows(self.store, before=T0 + 60)["time"].tolist(), [T0, T0 + 30])

    def test_revision_moves_when_content_changes(self):
        start = self.store.revision
        self.save([(T0, 1, 1)])
        self.assertGreater(self.store.revision, start)
        after = self.store.revision
        self.save([(T0, 1, 1)])                 # 没变就不落盘, 版本号也不动
        self.assertEqual(self.store.revision, after)


class FuturesRollupFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.base = ingest.Feed(SYMBOL, 30)
        self.feed = ingest.FuturesRollupFeed(SYMBOL, 300, lambda: self.base)

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def publish_base(self, items, revision=1):
        bars = base_bars(items)
        self.base.request(ltf=0)
        snapshot, _ = self.base._publish(0, bars, {}, revision)
        self.base.revision = revision
        return snapshot

    def test_cvd_at_each_bucket_end_matches_the_thirty_second_cvd(self):
        items = [(T0 + 30 * i, 10.0, float(3 + i % 4), float(4 - i % 3)) for i in range(25)]
        # 先落盘一批更早的历史(窗口之外), 再发布最近窗口: 合成要把两段接起来
        earlier = self.publish_base(items[:15])
        snapshot = self.publish_base(items[12:], revision=2)
        self.feed.klines = klines_at((T0, 100), (T0 + 300, 100), (T0 + 600, 50))
        self.feed.recompute(Mock())
        bars = self.feed.snapshots[0]["bars"]
        cvd = {bar["time"]: bar["cvd"] for bar in earlier["bars"] + snapshot["bars"]}
        self.assertEqual([bar["time"] for bar in bars], [T0, T0 + 300, T0 + 600])
        # 每根 5 分钟 bar 的 CVD = 其中最后一根 30s 的 CVD(最后一根正在走, 取最新那根 30s)
        for bar, last in zip(bars, [T0 + 270, T0 + 570, items[-1][0]]):
            self.assertAlmostEqual(bar["cvd"], cvd[last])
        self.assertEqual([bar["coverage"] for bar in bars], ["complete", "complete", "complete"])
        self.assertEqual(self.feed.snapshots[0]["tf"], 300)
        self.assertEqual(self.feed.snapshots[0]["cvdBase"], snapshot["cvdBase"])

    def test_waits_for_the_base_window(self):
        self.feed.klines = klines_at((T0, 100))
        self.feed.request(ltf=0)
        self.feed.recompute(Mock())
        self.assertEqual(self.feed.snapshots, {})

    def test_requests_are_forwarded_to_the_base(self):
        self.feed.request(ltf=5, demand=True)
        self.assertIn(5, self.base._requested)
        self.assertIsNotNone(self.base._last_demand)

    def test_config_and_footprint(self):
        cfg = self.feed.cfg()
        self.assertEqual(cfg["tf"], 300)
        self.assertEqual(cfg["ltfOptions"], [0, 1, 5, 10, 15, 30])
        self.assertEqual(self.feed.footprint_snapshot()["bars"], [])

    def test_changes_when_klines_change_or_the_base_recomputes(self):
        api = Mock()
        api.is_changing.return_value = False
        self.feed.base_revision = self.base.revision
        self.assertFalse(self.feed.data_changing(api))
        self.base.revision += 1
        self.assertTrue(self.feed.data_changing(api))

    def test_subscribes_only_the_native_klines(self):
        api = Mock()
        api.query_quotes.return_value = [SYMBOL]
        api.get_kline_serial.return_value = klines_at((T0, 1))
        self.feed.subscribe(api)
        api.get_kline_serial.assert_called_once_with(SYMBOL, 300, data_length=ingest.MAX_KLINES)
        api.get_tick_serial.assert_not_called()
        self.assertTrue(self.feed.has_data())


class ManagerRollupTests(unittest.TestCase):
    def test_rollup_period_creates_its_thirty_second_base(self):
        manager = ingest.FeedManager()
        feed = manager.ensure(SYMBOL, 3600)
        self.assertIsInstance(feed, ingest.FuturesRollupFeed)
        self.assertIs(feed.base(), manager.feeds[(SYMBOL, 30)])
        self.assertIs(manager.ensure(SYMBOL, 3600), feed)
        self.assertEqual(sorted(manager.feeds), [(SYMBOL, 30), (SYMBOL, 3600)])

    def test_rollup_periods_are_never_pinned(self):
        manager = ingest.FeedManager()
        pinned = manager.set_pinned([(SYMBOL, 10), (SYMBOL, 30), (SYMBOL, 300)])
        self.assertEqual(pinned, [(SYMBOL, 10), (SYMBOL, 30)])

    def test_base_stays_while_its_rollup_is_alive_and_goes_with_it(self):
        manager = ingest.FeedManager()
        clock = [1000.0]
        with patch.object(ingest.time, "monotonic", lambda: clock[0]):
            manager.ensure(SYMBOL, 300)
            clock[0] += 100
            manager.feeds[(SYMBOL, 300)].request(demand=False)
            manager.feeds[(SYMBOL, 300)]._last_demand = clock[0]   # 大周期刚被看过, 底层没有
            clock[0] += ingest.IDLE_EVICT_SEC - 50
            manager._prune()
            self.assertIn((SYMBOL, 30), manager.feeds)
            clock[0] += 100
            manager._prune()
            self.assertEqual(manager.feeds, {})


class CryptoRollupTests(unittest.TestCase):
    """加密大周期: 开高低收量与买卖量都由 30s 合成, 不收逐笔成交; 多所汇总的量相加。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.manager = CryptoManager([BinanceAdapter(), OkxAdapter()])
        self.manager.aggregates = AggregateBook(self.manager)
        self.print = patch("builtins.print")
        self.print.start()

    def tearDown(self):
        self.print.stop()
        self.paths.stop()
        self.temp.cleanup()

    def feed_trades(self, symbol, trades):
        for row in trades.itertuples(index=False):
            self.manager._apply_event(("trade", symbol, int(row.id), float(row.price), float(row.qty),
                                       int(row.t), bool(row.sell)))

    def compute(self):
        now = time.monotonic()
        self.manager._sync(now)
        self.manager._compute(now)
        # 底层算完、大周期再算一轮(不依赖 _compute 内部的先后)
        self.manager._compute(time.monotonic() + IDLE_RECOMPUTE_SEC)

    def test_rollup_has_no_trade_window_and_matches_bulk_aggregation(self):
        feed = self.manager.ensure(BTC, 300)
        self.assertIsInstance(feed, CryptoRollupFeed)
        self.assertFalse(hasattr(feed, "trades"))
        self.assertEqual(sorted(self.manager.feeds), [(BTC, 10), (BTC, 30)])
        trades = make_trades(START_MS, 200, step_ms=4000)
        self.feed_trades(BTC, trades)
        self.compute()
        bars = feed.snapshot_for(0)["bars"]
        expected = aggregate_trades(trades, 300, [0], START_MS, int(trades.t.iloc[-1]) + 1)
        self.assertEqual([bar["time"] for bar in bars],
                         [int(t) // 1000 + TZ_SHIFT_S for t in expected.index] + [bars[-1]["time"]])
        for bar, (_, row) in zip(bars, expected.iterrows()):
            self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"]),
                             (row.open, row.high, row.low, row.close))
            self.assertAlmostEqual(bar["volume"], row.volume)
            self.assertAlmostEqual(bar["buy"], row.buy)
        # 本次运行的第一笔之前是个洞: 第一根 partial, 之后都完整(最后一根正在走, 数到最新的 30s)
        self.assertEqual([bar["coverage"] for bar in bars], ["partial"] + ["complete"] * (len(bars) - 1))
        base = self.manager.feeds[(BTC, 30)].snapshot_for(0)["bars"]
        self.assertAlmostEqual(bars[-1]["cvd"], base[-1]["cvd"])
        self.assertEqual(feed.cfg()["volumeDigits"], 3)          # 显示位数沿用合约的

    def test_older_candles_come_from_the_thirty_second_file(self):
        feed = self.manager.ensure(BTC, 300)
        self.feed_trades(BTC, make_trades(START_MS + 3_600_000, 10, step_ms=4000, first_id=5000))
        self.compute()
        older = make_trades(START_MS, 900, step_ms=4000)
        self.manager.feeds[(BTC, 30)].apply_backfill(aggregate_trades(older, 30, ltf_options(30), START_MS,
                                                                       START_MS + 3_600_000))
        self.manager._dirty.add(BTC)
        self.compute()
        bars = feed.snapshot_for(0)["bars"]
        self.assertEqual(bars[0]["time"], START_MS // 1000 + TZ_SHIFT_S)
        self.assertEqual({bar["coverage"] for bar in bars[:12]}, {"complete"})

    def test_aggregate_rollup_adds_both_venues(self):
        feed = self.manager.ensure("AGG.BTC", 300)
        self.assertIs(feed.base(), self.manager.aggregates.feeds[("AGG.BTC", 30)])
        trades = make_trades(START_MS, 150, step_ms=4000)
        self.feed_trades(BTC, trades)
        self.feed_trades(OKX, trades)
        self.compute()
        bars = feed.snapshot_for(0)["bars"]
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["coverage"], "complete")
        single = self.manager.ensure(BTC, 300)
        self.compute()
        alone = {bar["time"]: bar for bar in single.snapshot_for(0)["bars"]}
        for bar in bars:
            self.assertAlmostEqual(bar["volume"], 2 * alone[bar["time"]]["volume"])
            if bar["buy"] is not None:
                self.assertAlmostEqual(bar["buy"], 2 * alone[bar["time"]]["buy"])

    def test_clients_keep_the_split_alive_and_idle_rollups_go_away(self):
        feed = self.manager.ensure(BTC, 3600)
        q = asyncio.Queue()
        self.manager.add_client(q, BTC, 5, False, 3600)
        self.assertIn(5, self.manager.feeds[(BTC, 30)]._requested)
        self.manager._sync(time.monotonic() + crypto_feed.IDLE_EVICT_SEC + 1)
        self.assertIn((BTC, 3600), self.manager.rollups)          # 有人在看就不回收
        self.manager.remove_client(q)
        self.manager._sync(time.monotonic() + crypto_feed.IDLE_EVICT_SEC + 1)
        self.assertNotIn((BTC, 3600), self.manager.rollups)
        self.assertIsNot(self.manager.ensure(BTC, 3600), feed)    # 回收后再要就是新建的

    def test_history_endpoint_routes_rollup_periods(self):
        self.manager.ensure(BTC, 900)
        self.feed_trades(BTC, make_trades(START_MS, 60, step_ms=4000))
        self.compute()
        with patch.object(server, "crypto", self.manager):
            snapshot = asyncio.run(server.history(BTC, 0, 900))
            self.assertEqual((snapshot["tf"], snapshot["cfg"]["tf"]), (900, 900))
            self.assertEqual(asyncio.run(server.footprint(BTC, 900))["bars"], [])
            self.assertIn(f"{BTC}@900s", self.manager.status_snapshot()["rollups"])


class ChangeLogTests(unittest.TestCase):
    def test_earliest_change_since_a_revision(self):
        log = ChangeLog(size=3)
        self.assertIsNone(log.since(0))
        log.mark(500)
        log.mark(300)
        self.assertEqual(log.since(0), 300)
        self.assertEqual(log.since(1), 300)
        self.assertIsNone(log.since(2))

    def test_unknown_history_means_recompute_everything(self):
        log = ChangeLog(size=2)
        for t in (100, 200, 300):
            log.mark(t)
        self.assertEqual(log.since(1), 200)
        self.assertEqual(log.since(0), float("-inf"))     # 第 1 次改动已经不在记录里
        log.mark(float("-inf"))                           # 整表重读
        self.assertEqual(log.since(3), float("-inf"))


class ClosedCacheTests(unittest.TestCase):
    """已收尾的桶走缓存: 结果与每次全量现算一致, 平时不再扫描窗口之前的历史。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.base = ingest.Feed(SYMBOL, 30)
        self.feed = ingest.FuturesRollupFeed(SYMBOL, 300, lambda: self.base)
        self.feed.request(ltf=0)
        self.items = [(T0 + 30 * i, 10.0, float(2 + i % 5), float(3 + i % 3)) for i in range(400)]

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def publish(self, upto, window=40):
        """底层发布截至第 upto 根的最近 window 根(更早的已在历史文件里), 同时给出 5 分钟原生 K 线。"""
        self.base.request(ltf=0)
        revision = self.base.revision + 1
        self.base._publish(0, base_bars(self.items[max(0, upto - window):upto]), {}, revision)
        self.base.revision = revision
        last = self.items[upto - 1][0]
        starts = range(T0, last + 1, 300)
        self.feed.klines = klines_at(*[(t, 100.0 if t + 300 <= last + 30 else 10.0 * ((last - t) // 30 + 1))
                                       for t in starts])

    def full(self):
        """不走缓存的全量结果(rollup_bars 一次算完), 用来对照。"""
        store = self.base._store(0)
        recent = self.base.latest_window(0)
        allrows = pd.concat([store_rows(store, before=int(recent["time"].iloc[0]), volume_store=store),
                             recent.reindex(columns=ROW_COLUMNS)], ignore_index=True)
        return store.with_cvd(rollup_bars(self.feed.candles(self.base), allrows, 300))

    def compare(self):
        self.feed.recompute(Mock())
        got = pd.DataFrame(self.feed.snapshots[0]["bars"])
        expected = self.full().reset_index(drop=True)
        self.assertEqual(got["time"].tolist(), expected["time"].tolist())
        self.assertEqual(got["coverage"].tolist(), expected["coverage"].tolist())
        for column in ["buy", "sell", "delta", "cvd"]:
            np.testing.assert_allclose(got[column].astype(float), expected[column].astype(float), err_msg=column)

    def test_incremental_result_matches_full_recompute_as_the_window_moves(self):
        for upto in range(60, 400, 37):
            self.publish(upto)
            self.compare()

    def test_a_change_inside_a_closed_bucket_is_picked_up(self):
        for upto in range(40, 301, 40):          # 逐步发布, 历史文件里攒下全部已走完的 30s
            self.publish(upto)
        self.compare()
        store = self.base._store(0)
        # 回填/修订改了一根早就收尾的 30s(T0+60 那根 5 分钟的第一根)
        changed = rows((T0 + 60 * 5, 9.0, 1.0, "complete", 10.0))
        store.save_completed(changed, final=True)
        self.compare()
        bar = next(b for b in self.feed.snapshots[0]["bars"] if b["time"] == T0 + 300)
        self.assertEqual(bar["buy"], 9.0 + sum(item[2] for item in self.items[11:20]))

    def test_steady_state_reads_only_the_open_part_of_the_history(self):
        self.publish(300)
        self.feed.recompute(Mock())
        calls = []
        real = rollup_module.store_rows
        with patch.object(rollup_module, "store_rows", lambda *a, **k: calls.append(a[1:3]) or real(*a, **k)):
            self.publish(301)
            self.feed.recompute(Mock())
        window_start = self.items[301 - 40][0]
        self.assertTrue(calls)
        self.assertTrue(all(since >= window_start - 300 for since, _ in calls), calls)

    def test_only_the_latest_bars_are_rolled_up(self):
        self.feed.klines = klines_at(*[(T0 + 300 * i, 1.0) for i in range(ingest.SNAPSHOT_BARS + 100)])
        candles = self.feed.candles(self.base)
        self.assertEqual(len(candles), ingest.SNAPSHOT_BARS)
        self.assertEqual(int(candles["time"].iloc[-1]), T0 + 300 * (ingest.SNAPSHOT_BARS + 99))


class LatestWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_rollup_ignores_a_window_the_base_did_not_compute_this_round(self):
        base = ingest.Feed(SYMBOL, 30)
        feed = ingest.FuturesRollupFeed(SYMBOL, 300, lambda: base)
        feed.request(ltf=5)
        base._publish(5, base_bars([(T0, 10.0, 4.0, 5.0)]), {}, 1)
        base.revision = 1
        self.assertIsNotNone(base.latest_window(5))
        base.revision = 2                       # 底层又算了一轮, 这一轮没有 ltf=5(已停用)
        self.assertIsNone(base.latest_window(5))
        feed.klines = klines_at((T0, 10))
        feed.recompute(Mock())
        self.assertNotIn(5, feed.snapshots)


class ManagerLifecycleTests(unittest.TestCase):
    def test_kept_base_does_not_make_other_feeds_go_early(self):
        manager = ingest.FeedManager()
        clock = [1000.0]
        with patch.object(ingest.time, "monotonic", lambda: clock[0]):
            manager.ensure(SYMBOL, 300)
            manager.add_client(asyncio.Queue(), SYMBOL, 0, False, 300)   # 大周期一直有人看
            other = manager.ensure("SHFE.rb2601", 30)
            manager._sdk_feeds |= set(manager.feeds.values())
            clock[0] += 50
            other.request(demand=True)
            clock[0] += ingest.IDLE_EVICT_SEC - 30        # 底层闲置早超 600 秒(被留着), rb 才 570 秒
            manager._prune()
            self.assertIn(("SHFE.rb2601", 30), manager.feeds)
            self.assertIn((SYMBOL, 30), manager.feeds)
            self.assertFalse(manager._rebuild_api.is_set())

    def test_rollup_needs_room_for_its_base_too(self):
        manager = ingest.FeedManager()
        for i in range(ingest.MAX_FEEDS - 1):
            manager.feeds[(f"SHFE.x{i}", 30)] = ingest.Feed(f"SHFE.x{i}", 30)
        with self.assertRaisesRegex(ValueError, "上限"):
            manager.ensure(SYMBOL, 300)
        self.assertNotIn((SYMBOL, 30), manager.feeds)     # 不留下一路没人要的底层
        manager.feeds.pop(("SHFE.x0", 30))
        manager.ensure(SYMBOL, 30)
        self.assertIsInstance(manager.ensure(SYMBOL, 300), ingest.FuturesRollupFeed)   # 底层已在, 只占一路


class CandleStoreHistoryTests(unittest.TestCase):
    def test_rows_and_changes_follow_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x_30s_ohlc.csv"
            path.write_text("time,open,high,low,close,volume\n100,1,2,0.5,1.5,3\n130,1,2,0.5,1.5,4\n",
                            encoding="utf-8")
            store = CandleStore(path)
            store.refresh()
            revision = store.revision
            self.assertEqual(store.rows(0, 1000)["time"].tolist(), [100, 130])
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("160,1,2,0.5,1.5,5\n70,1,2,0.5,1.5,6\n190,1,2")   # 最后半行还在写
            store.refresh()
            self.assertEqual(store.changed_since(revision), 70)
            self.assertEqual(store.rows(0, 1000)["time"].tolist(), [70, 100, 130, 160])
            self.assertEqual(store.rows(100, 160)["volume"].tolist(), [3.0, 4.0])
            self.assertEqual(store.last_time(), 160)


class CryptoRollupLifecycleTests(CryptoRollupTests):
    def test_leaving_after_a_long_session_keeps_the_rollup_for_the_idle_period(self):
        feed = self.manager.ensure(BTC, 300)
        q = asyncio.Queue()
        self.manager.add_client(q, BTC, 0, False, 300)
        feed._last_demand = time.monotonic() - 900          # 看了 15 分钟
        self.manager.remove_client(q)
        self.manager._sync(time.monotonic())
        self.assertIn((BTC, 300), self.manager.rollups)

    def test_first_snapshot_does_not_wait_for_the_idle_interval(self):
        feed = self.manager.ensure(BTC, 300)
        now = time.monotonic()
        self.manager._sync(now)
        self.manager._compute(now)                          # 底层还没数据: 大周期算了个空
        self.assertEqual(feed.snapshots, {})
        self.feed_trades(BTC, make_trades(START_MS, 30, step_ms=4000))
        self.manager._compute(now + 1)                      # 不到 IDLE_RECOMPUTE_SEC, 也没有客户端
        self.assertIn(0, feed.snapshots)

    def test_aggregate_rollup_watches_both_venues(self):
        self.assertEqual(self.manager._rollup_sources({("AGG.BTC", 300), (BTC, 10)}),
                         {("AGG.BTC", 30), (BTC, 30), (OKX, 30)})

    def test_aggregate_volume_is_missing_where_one_venue_lacks_bars(self):
        feed = self.manager.ensure("AGG.BTC", 300)
        trades = make_trades(START_MS, 150, step_ms=4000)
        self.feed_trades(BTC, trades)
        self.feed_trades(OKX, trades[trades.t >= START_MS + 300_000])    # OKX 只有第二根 5 分钟
        self.compute()
        bars = {bar["time"]: bar for bar in feed.snapshot_for(0)["bars"]}
        first, second = START_MS // 1000 + TZ_SHIFT_S, START_MS // 1000 + TZ_SHIFT_S + 300
        self.assertIsNone(bars[first]["volume"])
        self.assertIsNotNone(bars[second]["volume"])


if __name__ == "__main__":
    unittest.main()
