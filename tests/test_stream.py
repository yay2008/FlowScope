import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import app as server
import ingest
from indicator import CFG, BAR_NS, build_bars, split_ticks_to_bars, _classify_ticks
from tick_analytics import TickAnalytics
from test_data import BASE, ticks, klines


class IncrementalTests(unittest.TestCase):
    def test_stream_matches_full_recomputation_for_every_granularity(self):
        data = ticks(offsets=range(100), prices=[101 + i % 3 for i in range(100)],
                     volumes=[100 + 2 * i for i in range(100)])
        data["id"] = range(len(data))
        engine = TickAnalytics()
        for count in [2, 30, 31, 35, 60, 80, 100]:
            engine.update(data.iloc[:count])
            for ltf in CFG["ltfOptions"]:
                expected = split_ticks_to_bars(data.iloc[:count], ltf)
                pd.testing.assert_frame_equal(engine.aggregate(ltf, BASE), expected)

    def test_latest_tick_correction_replaces_volume_instead_of_adding(self):
        data = ticks()
        engine = TickAnalytics()
        engine.update(data)
        engine.aggregate(0, BASE)
        data.loc[4, "volume"] += 7
        engine.update(data)
        pd.testing.assert_frame_equal(engine.aggregate(0, BASE), split_ticks_to_bars(data))

    def test_recompute_retry_keeps_dirty_boundary_without_another_tick(self):
        data = ticks()
        engine = TickAnalytics()
        engine.update(data.iloc[:4])
        engine.aggregate(0, BASE)
        engine.update(data)
        # 模拟更新分类后后续步骤失败；重试收到相同快照，也必须能补算聚合。
        engine.update(data)
        pd.testing.assert_frame_equal(engine.aggregate(0, BASE), split_ticks_to_bars(data))

    def test_old_tick_correction_is_detected(self):
        data = ticks()
        engine = TickAnalytics()
        engine.update(data)
        engine.aggregate(0, BASE)
        data.loc[2, "last_price"] = 100
        engine.update(data)
        pd.testing.assert_frame_equal(engine.aggregate(0, BASE), split_ticks_to_bars(data))

    def test_window_roll_preserves_volume_and_inherited_direction(self):
        data = ticks()
        engine = TickAnalytics()
        engine.update(data.iloc[:4])
        engine.aggregate(0, BASE)
        engine.update(data.iloc[2:])
        pd.testing.assert_frame_equal(engine.aggregate(0, BASE), split_ticks_to_bars(data))
        revision = engine.version
        engine.update(data.iloc[2:])
        self.assertEqual(engine.version, revision)

    def test_incremental_matches_full_recompute_across_lr_state_changes(self):
        """增量重算必须把新算法的判向状态(前价/前盘口/沿用方向)逐列传回。

        这段数据刻意串起四类状态变化: 报价上移、无成交报价更新、价差内部同价、
        累计量清零换日, 再加一次跨越 GAP_NS 的断档。
        """
        data = pd.DataFrame({
            "datetime": [BASE + int(s * 10**9) for s in [0, 1, 2, 3, 4, 5, 200, 201]],
            "last_price": [4287, 4287, 4287, 4285, 4285, 4285, 4285, 4285],
            "ask_price1": [4287, 4288, 4290, 4290, 4290, 4290, 4290, 4290],
            "bid_price1": [4286, 4287, 4289, 4289, 4289, 4289, 4289, 4289],
            "volume":     [100, 120, 120, 160, 200, 30, 60, 90],
            "id": range(8),
        })
        engine = TickAnalytics()
        for count in range(1, len(data) + 1):
            engine.update(data.iloc[:count])
            pd.testing.assert_frame_equal(engine.aggregate(0, BASE),
                                          split_ticks_to_bars(data.iloc[:count], 0))

    def test_tick_gap_starts_new_coverage_boundary(self):
        data = ticks()
        data["id"] = range(len(data))
        engine = TickAnalytics()
        engine.update(data.iloc[:2])
        engine.aggregate(0, BASE)
        engine.update(data.iloc[3:])
        self.assertEqual(engine.first_tick_ns, BASE + 31 * 10**9)
        result = build_bars(klines(), data, aggregates=engine.aggregate(0, BASE),
                            classified=engine.frame, first_tick_ns=engine.first_tick_ns)
        self.assertEqual(result.iloc[1].coverage, "partial")


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.feed = ingest.Feed(server.DEFAULT_SYMBOL)
        self.feed.quote = SimpleNamespace(price_tick=1.)
        self.feed.ticks = ticks()
        self.feed.klines = klines()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_only_requested_modes_are_computed(self):
        self.feed.recompute(lambda _: None)
        self.assertEqual(set(self.feed.snapshots), {0})
        self.assertIsNone(self.feed.footprint)
        self.feed.request(ltf=5, footprint=True)
        self.feed.lower_klines[5] = klines()
        self.feed.recompute(lambda _: None)
        self.assertEqual(set(self.feed.snapshots), {0, 5})
        self.assertEqual(self.feed.footprint["tickSize"], 1)

    def test_kline_mode_has_independent_cache_and_source(self):
        self.feed.request(ltf=10)
        self.feed.lower_klines[10] = klines()
        self.feed.recompute(lambda _: None)
        self.assertEqual(self.feed.snapshots[10]["source"], "kline")
        self.assertEqual(self.feed.snapshots[0]["source"], "tick")
        self.assertIn("kline_ltf10", str(self.feed._store(10).path))
        self.assertNotEqual(self.feed._store(0).path, self.feed._store(10).path)
        self.assertEqual(self.feed.snapshots[10]["bars"][0]["delta"], 20)
        self.assertEqual(self.feed.snapshots[0]["bars"][0]["delta"], 10)

    def test_lower_candles_subscribe_on_demand_and_reuse_main_period(self):
        from unittest.mock import Mock
        api = Mock()
        api.get_kline_serial.return_value = klines()
        self.feed.lower_klines[30] = self.feed.klines
        self.feed.ensure_ltf_subscriptions(api)
        api.get_kline_serial.assert_not_called()
        self.feed.request(ltf=10)
        self.feed.request(ltf=30)
        self.feed.ensure_ltf_subscriptions(api)
        self.feed.ensure_ltf_subscriptions(api)
        api.get_kline_serial.assert_called_once_with(self.feed.symbol, 10, data_length=6001)
        self.assertIs(self.feed.lower_klines[30], self.feed.klines)

    def test_historical_correction_is_broadcast_and_cvd_revised(self):
        self.feed.recompute(lambda _: None)
        self.feed.ticks.loc[2, "last_price"] = 100
        messages = []
        self.feed.recompute(messages.append)
        changes = next(m for m in messages if m["type"] == "bars")["bars"]
        # 该修订只翻转旧算法口径(101 从"贴卖一"变成"砸买一"), 新算法两版都判卖,
        # 所以 delta 不动、只有对照列变化, 广播的 bar 数由 2 降到 1。
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["delta"], 0)
        self.assertEqual(changes[0]["sellLegacy"], 10)
        self.assertEqual(changes[-1]["cvd"], 10)

    def test_empty_footprint_can_later_receive_first_trade(self):
        self.feed.request(footprint=True)
        self.feed.ticks["volume"] = 100
        self.feed.recompute(lambda _: None)
        self.assertEqual(self.feed.footprint["bars"], [])
        self.feed.ticks.loc[4, "volume"] = 110
        messages = []
        self.feed.recompute(messages.append)
        self.assertTrue(any(m["type"] == "footprints" and m["bars"] for m in messages))

    def test_slow_client_gets_explicit_resync(self):
        manager = ingest.FeedManager()
        q = asyncio.Queue(maxsize=1)
        manager.add_client(q, self.feed.symbol, 0)
        msg = {"type": "bars", "symbol": self.feed.symbol, "tf": self.feed.tf, "ltf": 0, "bars": []}
        manager._fanout(msg)
        manager._fanout(msg)
        self.assertEqual(q.get_nowait()["type"], "resync")

    def test_ws_reconnect_supplies_missing_bars_and_ignores_old_queue_data(self):
        manager = ingest.FeedManager()
        manager.feeds[ingest.feed_key(self.feed.symbol, self.feed.tf)] = self.feed
        self.feed.recompute(lambda _: None)
        class Socket:
            def __init__(self):
                self.messages = asyncio.Queue()
            async def accept(self):
                pass
            async def send_json(self, message):
                self.messages.put_nowait(message)
            async def close(self, **kwargs):
                self.messages.put_nowait({"closed": kwargs})
            async def receive(self):
                return await asyncio.wait_for(self.messages.get(), timeout=2)

        async def exercise():
            manager.loop = asyncio.get_running_loop()
            socket = Socket()
            task = asyncio.create_task(server.ws(socket, self.feed.symbol, 0, False))
            try:
                first = await socket.receive()
                self.assertEqual(first["type"], "snapshot")
                self.assertEqual(len(first["bars"]), 3)
                manager.broadcast({"type": "bars", "symbol": self.feed.symbol, "tf": self.feed.tf,
                                   "ltf": 0, "revision": first["revision"], "bars": [{"time": 0}]})
                self.feed.ticks.loc[4, "volume"] = 145
                self.feed.klines.loc[2, "volume"] = 15
                self.feed.recompute(manager.broadcast)
                update = await socket.receive()
                self.assertEqual(update["bars"][-1]["buy"], 15)
            finally:
                task.cancel()
                await task
            self.feed.ticks.loc[4, "volume"] = 150
            self.feed.klines.loc[2, "volume"] = 20
            self.feed.recompute(manager.broadcast)
            socket = Socket()
            task = asyncio.create_task(server.ws(socket, self.feed.symbol, 0, False))
            try:
                restored = await socket.receive()
                self.assertEqual(restored["type"], "snapshot")
                self.assertEqual(restored["bars"][-1]["buy"], 20)
            finally:
                task.cancel()
                await task
            self.assertEqual(manager.clients, {})

        with patch.object(server, "manager", manager):
            asyncio.run(exercise())



if __name__ == "__main__":
    unittest.main()
