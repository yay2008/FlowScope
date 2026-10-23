"""历史翻页读取真实历史文件, 验证分页边界、聚合、CVD 与线程调度。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

import app as server
import ingest
from binance_feed import BinanceAdapter
from crypto_aggregate import AggregateBook
from crypto_feed import CryptoManager
from history_paging import read_history_page
from indicator import TZ_SHIFT_S
from okx_feed import OkxAdapter

T0 = 1_790_985_600 + TZ_SHIFT_S
BTC, OKX = "BINANCE.BTCUSDT.P", "OKX.BTC-USDT-SWAP"


class HistoryPagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.manager = CryptoManager([BinanceAdapter(), OkxAdapter()])
        self.manager.aggregates = AggregateBook(self.manager)
        self.agg = self.manager.ensure("AGG.BTC", 30)
        self.manager._sync(time.monotonic())

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def seed(self, symbol, count=3300, missing=(), wave=False, partial=()):
        """wave: 价格有起伏(平价时涨跌 RMA 都是 0, FlowWave 算不出值); partial: 这些根的覆盖不完整。"""
        feed = self.manager.ensure(symbol, 30)
        self.manager._ensure_loaded(feed)
        times = np.array([T0 + i * 30 for i in range(count) if i not in missing], dtype=np.int64)
        buy = 3.0 if symbol == BTC else 2.0
        candles = pd.DataFrame({"time": times, "open": 100.0, "high": 103.0, "low": 99.0,
                                "close": 102.0, "volume": buy + 1})
        if wave:
            price = 100 + np.sin(np.arange(len(candles)) / 13)
            candles["open"], candles["close"] = price, price
            candles["high"], candles["low"] = price + 1, price - 1
        feed.candles.save(candles, final=True)
        for ltf in (0, 5):
            rows = candles.assign(buy=buy if ltf == 0 else 1.0, sell=1.0 if ltf == 0 else buy,
                                  unknown=0.0, buyLegacy=buy, sellLegacy=1.0,
                                  coverage="complete", hasBaseline=True)
            rows.loc[rows.index.isin(partial), "coverage"] = "partial"
            feed._store(ltf).save_completed(rows, final=True)
        feed.trades.load(feed.candles.load())
        feed.request(ltf=5)
        feed.recompute(lambda _: None)
        return feed

    def page(self, symbol, before, limit=800, tf=30, ltf=0):
        return read_history_page(self.manager, symbol, tf, ltf, before, limit)

    def test_all_pages_extend_beyond_memory_window_and_preserve_cvd(self):
        self.seed(BTC)
        self.seed(OKX)
        self.agg.sync_history(self.manager)
        self.agg.recompute(self.manager, lambda _: None)
        bars = self.agg.snapshot_for(0)["bars"]
        self.assertEqual(len(bars), 800)
        sizes = {p.name: p.stat().st_size for p in Path(self.temp.name).iterdir()}
        lengths = []
        while True:
            page = self.page("AGG.BTC", bars[0]["time"])
            lengths.append(len(page["bars"]))
            self.assertTrue(all(b["time"] < bars[0]["time"] for b in page["bars"]))
            bars = page["bars"] + bars
            if not page["hasMore"]:
                break
        self.assertEqual(lengths, [800, 800, 800, 100])
        self.assertEqual([b["time"] for b in bars], list(range(T0, T0 + 3300 * 30, 30)))
        for i, bar in enumerate(bars):
            self.assertEqual((bar["buy"], bar["sell"], bar["volume"], bar["coverage"]), (5, 2, 7, "complete"))
            self.assertAlmostEqual(bar["cvd"], (i + 1) * 3)
            self.assertAlmostEqual(bar["cvdOpen"], i * 3)
        self.assertEqual(sizes, {p.name: p.stat().st_size for p in Path(self.temp.name).iterdir()}, "翻页不写历史文件")
        self.assertEqual(len(self.agg.snapshot_for(0)["bars"]), 800, "实时窗口保持有界")

    def test_native_pages_handle_missing_times_and_revised_rows(self):
        feed = self.seed(BTC, 1200, missing=(100, 101, 999))
        before = T0 + 1000 * 30
        page = self.page(BTC, before, 100)
        self.assertTrue(page["hasMore"])
        self.assertEqual(page["bars"][-1]["time"], before - 60)
        self.assertEqual(page["nextBefore"], page["bars"][0]["time"])
        revised = pd.DataFrame({"time": [T0 + 10 * 30], "open": 500.0, "high": 501.0,
                                "low": 499.0, "close": 500.5, "volume": 4.0})
        feed.candles.save(revised, final=True)
        # 把修订所在行移出内存, 确认读取的是文件里的后写版本。
        feed.trades.stored.clear()
        feed.latest.clear()
        page = self.page(BTC, T0 + 11 * 30, 1)
        self.assertEqual(page["bars"][0]["close"], 500.5)
        self.assertFalse(self.page(BTC, T0)["hasMore"])
        self.assertEqual(self.page(BTC, T0)["bars"], [])

    def test_aggregate_missing_exchange_does_not_fake_volume_or_direction(self):
        self.seed(BTC, 20)
        self.seed(OKX, 20, missing=(5,))
        self.agg.sync_history(self.manager)
        bars = self.page("AGG.BTC", T0 + 20 * 30)["bars"]
        self.assertEqual(len(bars), 20)
        self.assertEqual(bars[5]["close"], 102)
        self.assertEqual(bars[5]["coverage"], "missing")
        self.assertIsNone(bars[5]["volume"])
        self.assertIsNone(bars[5]["buy"])
        self.assertIsNone(bars[5]["cvd"])
        self.assertEqual(bars[6]["cvd"], 18)

    def test_selected_split_and_large_trade_coverage_are_preserved(self):
        self.seed(BTC, 20)
        self.seed(OKX, 20)
        self.agg.sync_history(self.manager)
        self.agg.recompute(self.manager, lambda _: None)
        page = self.page("AGG.BTC", T0 + 20 * 30, ltf=5)
        self.assertEqual(page["ltf"], 5)
        self.assertEqual((page["bars"][0]["buy"], page["bars"][0]["sell"]), (2, 5))
        self.assertEqual(page["bars"][-1]["cvd"], -60)
        self.assertEqual(page["bars"][0]["large"]["coverage"], "missing")

    def test_history_and_split_preserve_authoritative_live_flowwave_results(self):
        for symbol in (BTC, OKX):
            source = self.seed(symbol)
            candles = source.trades.frame(0)[["time", "open", "high", "low", "close", "volume"]].copy()
            price = 100 + np.sin(np.arange(len(candles)) / 13)
            candles["open"], candles["close"] = price, price
            candles["high"], candles["low"] = price + 1, price - 1
            source.candles.save(candles, final=True)
            source.trades.load(source.candles.load())
            source.recompute(lambda _: None)
        self.agg.sync_history(self.manager)
        self.agg.recompute(self.manager, lambda _: None)
        live = self.agg.snapshot_for(0)["bars"]
        self.assertIsNotNone(live[-1]["flowWave"]["wt2"])
        before = live[-1]["time"] + 30
        for ltf in (0, 5):
            page = self.page("AGG.BTC", before, limit=100, ltf=ltf)
            self.assertEqual([b["flowWave"] for b in page["bars"]], [b["flowWave"] for b in live[-100:]])
        older = self.page("AGG.BTC", live[0]["time"], limit=100)
        self.assertTrue(all("flowWave" in b for b in older["bars"]))

    def test_crypto_backfill_before_the_snapshot_bumps_history_revision(self):
        feed = self.seed(BTC)
        feed.snapshot_for(0)
        feed.recompute(lambda _: None)
        start, messages = feed.history_revision, []
        feed.recompute(messages.append)
        self.assertEqual(feed.history_revision, start, "没有改动不提升")
        first = feed.snapshot_for(0)["bars"][0]["time"]
        rows = pd.DataFrame({"time": [first - 300], "buy": [9.0], "sell": [1.0], "unknown": 0.0, "buyLegacy": 9.0,
                             "sellLegacy": 1.0, "coverage": "complete", "hasBaseline": True})
        feed._store(0).save_completed(rows, final=True)      # 回填改到了快照第一根之前
        feed.recompute(messages.append)
        self.assertEqual(feed.history_revision, start + 1)
        update = [m for m in messages if m["type"] == "bars" and m["ltf"] == 0][-1]
        self.assertEqual(update["historyRevision"], start + 1)
        self.assertEqual(feed.snapshot_for(0)["historyRevision"], start + 1)

    def test_backfill_before_the_snapshot_rewarms_live_flowwave(self):
        # 快照第一根之前那 30 根先缺覆盖(加密的价和量都不可用): 第一次算时 FlowWave 从快照第一根才开始预热
        feed = self.seed(BTC, wave=True, partial=range(2470, 2500))
        feed.snapshot_for(0)
        feed.recompute(lambda _: None)
        bars = feed.snapshot_for(0)["bars"]
        self.assertEqual(bars[0]["time"], T0 + 2500 * 30)
        self.assertIsNone(bars[0]["flowWave"]["wt2"])
        start = feed.history_revision
        rows = pd.DataFrame({"time": T0 + np.arange(2470, 2500) * 30, "buy": 3.0, "sell": 1.0, "unknown": 0.0,
                             "buyLegacy": 3.0, "sellLegacy": 1.0, "coverage": "complete", "hasBaseline": True})
        feed._store(0).save_completed(rows, final=True)      # 回填补齐
        feed.recompute(lambda _: None)
        self.assertEqual(feed.history_revision, start + 1)
        revised = feed.snapshot_for(0)["bars"]
        self.assertIsNotNone(revised[0]["flowWave"]["wt2"], "历史修订后要重新预热")
        feed._flowwave.clear()                               # 对照: 从头预热算出来的
        feed.recompute(lambda _: None)
        self.assertEqual([b["flowWave"] for b in revised], [b["flowWave"] for b in feed.snapshot_for(0)["bars"]])

    def test_rollup_flowwave_is_warmed_from_history_on_first_compute(self):
        source = self.seed(BTC)
        candles = source.trades.frame(0)[["time", "open", "high", "low", "close", "volume"]].copy()
        price = 100 + np.sin(np.arange(len(candles)) / 13)
        candles["open"], candles["close"] = price, price
        candles["high"], candles["low"] = price + 1, price - 1
        source.candles.save(candles, final=True)
        source.trades.load(source.candles.load())
        source.recompute(lambda _: None)
        rollup = self.manager.ensure(BTC, 60)
        rollup.recompute(lambda _: None)
        bars = rollup.snapshot_for(0)["bars"]
        # 大周期只合成快照那 800 根: 第一次算从历史读之前的各根预热, 最左边一根也有值, 与翻页结果接得上
        self.assertEqual(len(bars), 800)
        self.assertIsNotNone(bars[0]["flowWave"]["wt2"])
        page = self.page(BTC, bars[0]["time"], limit=50, tf=60)
        self.assertTrue(all(b["flowWave"]["wt2"] is not None for b in page["bars"]))

    def test_rollup_pages_use_old_30_second_files_and_the_same_cvd(self):
        self.seed(BTC)
        self.seed(OKX)
        self.agg.sync_history(self.manager)
        self.agg.recompute(self.manager, lambda _: None)
        rollup = self.manager.ensure("AGG.BTC", 60)
        rollup.recompute(lambda _: None)
        initial = rollup.snapshot_for(0)["bars"]
        live = (rollup._closed[0].frame, {s: c.frame for s, c in rollup._candle_cache.items()})
        page = self.page("AGG.BTC", initial[0]["time"], tf=60)
        # 翻页另用缓存: 实时窗口已收尾各桶的缓存原样留着, 下一轮实时重算不用重建
        self.assertIs(rollup._closed[0].frame, live[0])
        self.assertEqual({s: c.frame for s, c in rollup._candle_cache.items()}.keys(), live[1].keys())
        self.assertTrue(all(rollup._candle_cache[s].frame is frame for s, frame in live[1].items()))
        self.assertEqual(len(page["bars"]), 800)
        self.assertTrue(page["hasMore"])
        last = page["bars"][-1]
        self.assertEqual((last["volume"], last["buy"], last["sell"], last["coverage"]), (14, 10, 4, "complete"))
        self.assertEqual(last["cvd"], initial[0]["cvdOpen"])
        self.assertEqual(len(self.page("AGG.BTC", page["nextBefore"], tf=60)["bars"]), 50)

    def test_last_historical_bucket_is_not_mistaken_for_an_open_bar(self):
        self.seed(BTC, 40, missing=(9,))
        page = self.page(BTC, T0 + 300, tf=300)
        self.assertEqual(page["bars"][0]["coverage"], "partial")

    def test_futures_exposes_the_available_sdk_window_beyond_800(self):
        feed = ingest.Feed("SHFE.test", 30)
        feed.klines = pd.DataFrame({"datetime": (np.arange(2000) * 30 + T0 - TZ_SHIFT_S) * 10**9,
                                   "open": 1.0, "high": 3.0, "low": 0.0, "close": 2.0, "volume": 10.0})
        source = SimpleNamespace(ensure=lambda symbol, tf: feed)
        page = read_history_page(source, feed.symbol, 30, 0, T0 + 1200 * 30, 800)
        self.assertEqual(len(page["bars"]), 800)
        self.assertTrue(page["hasMore"])
        self.assertEqual(page["bars"][0]["time"], T0 + 400 * 30)
        self.assertEqual(page["bars"][0]["coverage"], "missing")
        self.assertIsNone(page["bars"][0]["buy"])

    def test_http_pagination_runs_on_the_crypto_thread_queue(self):
        self.seed(BTC, 20)
        self.manager.status = "running"

        async def run():
            with patch.object(server, "crypto", self.manager):
                task = asyncio.create_task(server.history(BTC, 0, 30, before=T0 + 20 * 30, limit=7))
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.manager._apply_event(self.manager.events.get_nowait())
                response = await task
                return json.loads(response.body)

        page = asyncio.run(run())
        self.assertEqual(len(page["bars"]), 7)
        self.assertTrue(page["hasMore"])

    def test_cancelled_or_failed_history_query_does_not_stop_market_events(self):
        async def run():
            self.manager.status = "running"
            task = asyncio.create_task(self.manager.query(lambda _: 1))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.manager._apply_event(self.manager.events.get_nowait())
            task = asyncio.create_task(self.manager.query(lambda _: 1 / 0))
            await asyncio.sleep(0)
            self.manager._apply_event(self.manager.events.get_nowait())
            with self.assertRaises(ZeroDivisionError):
                await task
            self.manager._apply_event(("ticker", BTC, {"last": 100}))
            self.assertEqual(self.manager.quotes[BTC]["ticker"]["last"], 100)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
