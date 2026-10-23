"""回归: 深于 10000 根小 K 线的主图、分页补历史、持久化与 CVD 累计衔接。"""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import ingest
from futures_backfill import KlineBackfiller, read_download, recent_klines
from history_paging import read_history_page
from indicator import TZ_SHIFT_S, build_bars_from_ltf

T0 = 1_790_841_600 + TZ_SHIFT_S  # 历史时间, 与机器时钟无关
SYMBOL = "DCE.p2701"


def klines(seconds, count, volume=3.0):
    return pd.DataFrame({"datetime": (T0 - TZ_SHIFT_S + np.arange(count) * seconds) * 10**9,
                         "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": volume})


class FuturesBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        paths.start()
        self.addCleanup(paths.stop)
        self.manager = ingest.FeedManager()
        self.addCleanup(self.manager.stop)
        self.main = klines(30, 9000)
        self.lower = klines(15, 18000, 2.0)
        self.lower.loc[self.lower.index % 2 == 1, ["close", "volume"]] = [99.0, 1.0]
        self.feed = self.manager.ensure(SYMBOL, 300)
        self.base = self.feed.base()
        self.base.klines = self.main.tail(2000).copy()
        self.base.lower_klines[15] = self.lower.tail(4001).copy()
        self.feed.klines = klines(300, 900, 30.0)
        self.downloads = []
        self.messages = []
        self.manager.broadcast = self.messages.append

        async def query(fn, **kwargs):
            return fn(None)

        self.manager.query = query
        self.manager._history_backfiller = KlineBackfiller(self.download)
        self.seed_recent()

    def download(self, contract, tf, ltf, since, before, stopped):
        self.downloads.append((contract, tf, ltf, since, before))

        def select(frame):
            times = frame.datetime // 10**9 + TZ_SHIFT_S
            return frame[(times >= since) & (times < before)].copy()

        return select(self.main), select(self.lower)

    def seed_recent(self):
        bars = build_bars_from_ltf(self.base.klines, self.base.lower_klines[15], 15)
        self.base._store(15).save_completed(bars, final=True)
        self.base.latest[15] = (self.base.revision, bars)
        self.base.save_kline_volumes(self.base.klines, final=True)

    def page(self, limit=800, before=None):
        return read_history_page(self.manager, SYMBOL, 300, 15, before or T0 + 900 * 300, limit)

    def test_initial_chart_backfills_more_than_10000_lower_candles_and_reuses_disk(self):
        self.assertEqual(sum(b["cvd"] is None for b in self.page()["bars"]), 600)
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        page = self.page()
        self.assertEqual(len(page["bars"]), 800)
        self.assertTrue(all(b["coverage"] == "complete" and b["cvd"] is not None for b in page["bars"]))
        self.assertEqual(page["bars"][0]["time"], T0 + 100 * 300)
        self.assertEqual(self.downloads[0], (SYMBOL, 30, 15, T0 + 100 * 300, T0 + 700 * 300))
        self.assertGreater((self.downloads[0][4] - self.downloads[0][3]) // 15, 10000)
        self.assertEqual(page["bars"][-1]["cvd"], 8000)
        self.assertTrue(any(m["type"] == "history" and m["tf"] == 300 for m in self.messages))
        # 重建文件对象, 模拟服务重启后的读回: 不依赖之前的内存字典和 tick 历史。
        self.base.stores.clear()
        self.base._volumes = None
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.page()["bars"][-1]["cvd"], 8000)
        self.assertFalse(self.base._store(0).values, "回补 TV 历史不得给 tick 口径造数据")

    def test_older_page_backfills_only_its_range_and_rebases_existing_cvd(self):
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        prior = self.page()["bars"][-1]["cvd"]
        before = T0 + 100 * 300
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15, before=before, limit=100))
        self.assertEqual(self.downloads[-1][3:], (T0, before))
        older = self.page(limit=100, before=before)
        self.assertTrue(all(b["cvd"] is not None for b in older["bars"]))
        self.assertEqual(older["bars"][-1]["cvd"], 1000)
        latest = self.page()["bars"]
        self.assertEqual(latest[0]["cvdOpen"], 1000)
        self.assertEqual(latest[-1]["cvd"], prior + 1000)
        self.assertEqual(self.base.volume_history().values[T0], (3.0, 0.0))

    def test_provider_gaps_remain_partial_and_do_not_become_complete(self):
        main, lower = self.main.iloc[:2].copy(), self.lower.iloc[:4].copy()
        lower = lower.drop(lower.index[1])
        self.manager._apply_history(self.base, SYMBOL, 15, main, lower)
        page = read_history_page(self.manager, SYMBOL, 30, 15, T0 + 60, 2)
        # 当前原生 SDK 窗口里没有这两根, 直接读落盘结果验证来源而非伪造缺的成交。
        store = self.base._store(15)
        self.assertNotIn(T0, store.values)
        self.assertIn(T0, store.estimates)
        self.assertIn(T0 + 30, store.values)
        self.assertEqual(page["bars"], [])

    def test_late_old_contract_download_cannot_write_new_contract_history(self):
        self.base._bind_contract("DCE.p2705")
        result = self.manager._apply_history(self.base, SYMBOL, 15, self.main.iloc[:2], self.lower.iloc[:4])
        self.assertFalse(result)
        self.assertFalse(list(Path(self.temp.name).glob("DCE_p2705*")))

    def test_bars_left_partial_after_download_are_not_downloaded_again(self):
        # 没成交的秒没有小 K 线: 下载后这些 bar 仍是 partial, 不能每次加载都重下
        self.lower = self.lower[self.lower.index % 40 != 7].copy()
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        self.assertTrue(any(b["coverage"] == "partial" for b in self.page()["bars"]))
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        self.assertEqual(len(self.downloads), 1)

    def test_range_whose_save_failed_is_downloaded_again(self):
        store = self.base._store(15)
        with patch.object(type(store), "save_completed", side_effect=OSError("磁盘写入失败")):
            with self.assertRaises(OSError):
                asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        self.assertNotIn(15, self.base.history_attempted, "没存上不能记为下载过")
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        self.assertEqual(len(self.downloads), 2)
        self.assertTrue(all(b["cvd"] is not None for b in self.page()["bars"]))

    def test_holiday_chunk_without_any_kline_is_not_an_error(self):
        empty = self.main.iloc[:0].copy()
        self.assertTrue(self.manager._apply_history(self.base, SYMBOL, 15, empty, self.lower.iloc[:0].copy(),
                                                    T0, T0 + 86400))
        self.assertEqual(self.base.history_attempted[15], [(T0, T0 + 86400)])
        self.manager._apply_history(self.base, SYMBOL, 15, empty, self.lower.iloc[:0].copy(),
                                    T0 + 86400, T0 + 2 * 86400)
        self.assertEqual(self.base.history_attempted[15], [(T0, T0 + 2 * 86400)])

    def test_background_backfill_failure_is_logged_and_does_not_raise(self):
        def broken(*args):
            raise RuntimeError("网络中断")

        self.manager._history_backfiller.stop()
        self.manager._history_backfiller = KlineBackfiller(broken)

        async def run():
            first = self.manager.schedule_backfill(SYMBOL, 300, 15)
            self.assertIs(self.manager.schedule_backfill(SYMBOL, 300, 15), first, "同一范围只跑一个")
            await first
            return self.manager._backfills

        with patch("builtins.print") as printed:
            self.assertEqual(asyncio.run(run()), {})
        self.assertIn("网络中断", printed.call_args.args[0])
        self.assertIsNone(self.manager.schedule_backfill(SYMBOL, 300, 0))

    def test_first_load_returns_snapshot_without_waiting_for_download(self):
        import app as server

        release = threading.Event()

        def slow(*args):
            release.wait(5)
            return self.download(*args)

        self.manager._history_backfiller.stop()
        self.manager._history_backfiller = KlineBackfiller(slow)
        self.feed.snapshot_for(15, demand=False)
        self.feed.snapshots[15] = {"tf": 300, "ltf": 15, "bars": []}

        async def run():
            with patch.object(server, "manager", self.manager):
                snapshot = await asyncio.wait_for(server.history(SYMBOL, 15, 300), timeout=3)
                self.assertEqual(snapshot["symbol"], SYMBOL)
                (task,) = self.manager._backfills.values()
                self.assertFalse(task.done(), "下载还在后台进行")
                release.set()
                await task

        asyncio.run(run())
        self.assertEqual(len(self.downloads), 1)
        self.assertTrue(any(m["type"] == "history" for m in self.messages))

    def test_tick_mode_does_not_download_small_kline_history(self):
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 0))
        self.assertEqual(self.downloads, [])

    def test_refresh_range_does_not_include_bars_outside_loaded_interval(self):
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        page = read_history_page(self.manager, SYMBOL, 300, 15, T0 + 900 * 300, 2000, since=T0 + 850 * 300)
        self.assertEqual(len(page["bars"]), 50)
        self.assertEqual(page["bars"][0]["time"], T0 + 850 * 300)

    def test_free_account_expands_window_marks_limit_and_skips_unavailable_older_pages(self):
        calls = []

        class Api:
            def get_kline_serial(inner, symbol, seconds, data_length):
                calls.append((symbol, seconds, data_length))
                return (self.main if seconds == 30 else self.lower).tail(10000).copy()

            def is_serial_ready(inner, frame):
                return True

        def free_download(contract, tf, ltf, since, before, stopped):
            self.downloads.append((since, before))
            return recent_klines(Api(), contract, tf, ltf, since, stopped)

        self.manager._history_backfiller.stop()
        self.manager._history_backfiller = KlineBackfiller(free_download)
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        page = self.page()
        self.assertTrue(page["historyBackfill"]["limited"])
        self.assertEqual(sum(b["cvd"] is not None for b in page["bars"]), 500)
        self.assertTrue(all(length == 10000 for _, _, length in calls))
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15))
        asyncio.run(self.manager.backfill_history(SYMBOL, 300, 15, before=T0 + 100 * 300, limit=100))
        self.assertEqual(len(self.downloads), 1, "免费窗口以外的缺口不反复下载同一批数据")
        # 新开的其它周期沿用底层的权限提示, 不能只在触发下载的那张图上显示。
        another = self.manager.ensure(SYMBOL, 60)
        another.klines = klines(60, 4500, 6.0).tail(2000)
        other_page = read_history_page(self.manager, SYMBOL, 60, 15, T0 + 9000 * 30, 2000)
        self.assertEqual(other_page["historyBackfill"], page["historyBackfill"])


class DownloaderTests(unittest.TestCase):
    def test_csv_parser_preserves_nanosecond_precision_and_contract_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "download.csv"
            stamp = 1790841600000000001
            path.write_text("datetime,datetime_nano,DCE.p2701.open,DCE.p2701.high,DCE.p2701.low,DCE.p2701.close,DCE.p2701.volume\n"
                            f"2026-10-01,{stamp},1,3,0,2,12\n", encoding="utf-8")
            frame = read_download(path, SYMBOL)
            self.assertEqual(frame.datetime.iloc[0], stamp)
            self.assertEqual(frame.volume.iloc[0], 12)

    def test_concurrent_requests_share_download_and_cancel_does_not_cancel_other_client(self):
        started, finish = threading.Event(), threading.Event()
        calls = []

        def download(*args):
            calls.append(args)
            started.set()
            finish.wait(3)
            return "main", "lower"

        worker = KlineBackfiller(download)
        self.addCleanup(worker.stop)

        async def run():
            first = asyncio.create_task(worker.fetch(SYMBOL, 30, 15, 1, 100))
            while not started.is_set():
                await asyncio.sleep(0)
            second = asyncio.create_task(worker.fetch(SYMBOL, 30, 15, 1, 100))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            finish.set()
            return await second

        self.assertEqual(asyncio.run(run()), ("main", "lower"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
