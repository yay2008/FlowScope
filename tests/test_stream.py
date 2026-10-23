import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import app as server
import ingest
from indicator import (CFG, BAR_NS, TZ_SHIFT_S, build_bars, build_footprint, split_ticks_to_bars,
                       _classify_ticks)
from tick_analytics import TickAnalytics
from test_data import BASE, boundary_ticks, ticks, klines
from test_period import Clock


def full_bars(data, ltf=0):
    """全量重算; 与 aggregate(ltf, BASE) 一样丢掉首根 K 线之前的桶(BASE 上的快照归前一根)。"""
    bars = split_ticks_to_bars(data, ltf)
    return bars[bars.index >= BASE]


class IncrementalTests(unittest.TestCase):
    def test_stream_matches_full_recomputation_for_every_granularity(self):
        data = ticks(offsets=range(100), prices=[101 + i % 3 for i in range(100)],
                     volumes=[100 + 2 * i for i in range(100)])
        data["id"] = range(len(data))
        engine = TickAnalytics()
        for count in [2, 30, 31, 35, 60, 80, 100]:
            engine.update(data.iloc[:count])
            for ltf in CFG["ltfOptions"]:
                pd.testing.assert_frame_equal(engine.aggregate(ltf, BASE), full_bars(data.iloc[:count], ltf))

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
            pd.testing.assert_frame_equal(engine.aggregate(0, BASE), full_bars(data.iloc[:count]))

    def test_boundary_aligned_window_roll_matches_full_recompute(self):
        """窗口左端常恰在 bar 边界上: 裁剪与脏 bar 重算都得按 (start, end] 取快照。"""
        data = boundary_ticks(240)
        k = pd.DataFrame({"datetime": [BASE + i * BAR_NS for i in range(4)],
                          "open": [101.] * 4, "high": [103.] * 4, "low": [101.] * 4,
                          "close": [102.] * 4, "volume": [0.] * 4})
        coverage = {int(t) // 10**9 + TZ_SHIFT_S: "partial" for t in k.datetime}
        engine = TickAnalytics()
        for stop in [1, 60, 61, 120, 121, 180, 200, 240]:
            engine.update(data.iloc[max(0, stop - 120):stop])
            pd.testing.assert_frame_equal(engine.aggregate(0, BASE), full_bars(data.iloc[:stop]))
            self.assertEqual(engine.footprint(k, 1., coverage)["bars"],
                             build_footprint(k, data.iloc[:stop], 1., coverage=coverage)["bars"])

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

    def test_kline_volumes_are_saved_only_when_closed_klines_change(self):
        self.feed.request(ltf=10)
        self.feed.lower_klines[10] = klines()
        saved = []
        with patch.object(self.feed, "save_kline_volumes", side_effect=lambda k, final=False: saved.append(len(k))):
            self.feed.recompute(lambda _: None)
            self.feed.klines.loc[self.feed.klines.index[-1], "volume"] += 1   # 末根还在走
            self.feed.recompute(lambda _: None)
            self.assertEqual(len(saved), 1)
            self.feed.klines.loc[self.feed.klines.index[0], "volume"] += 1    # 已收盘的被改
            self.feed.recompute(lambda _: None)
            self.assertEqual(len(saved), 2)

    def test_snapshot_cfg_carries_contract_price_digits_once_quote_is_ready(self):
        # 国债期货步长 0.005: 位数要下发, 否则前端按 0 位取整, 107.85 变成 108
        self.feed.quote = SimpleNamespace(price_tick=0.005, price_decs=3)
        self.feed.recompute(lambda _: None)
        self.assertEqual(self.feed.snapshots[0]["cfg"]["priceDigits"], 3)
        # 报价还没到: price_tick 是 NaN, price_decs 是默认的 0, 不能当真
        self.feed.quote = SimpleNamespace(price_tick=float("nan"), price_decs=0)
        self.assertNotIn("priceDigits", self.feed.cfg())
        self.feed.quote = None
        self.assertNotIn("priceDigits", self.feed.cfg())

    def test_trading_times_are_copied_and_late_metadata_is_broadcast_without_new_bars(self):
        messages = []
        self.feed.recompute(messages.append)
        self.assertNotIn("tradingTime", self.feed.snapshots[0]["cfg"])
        messages.clear()
        day = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]]
        self.feed.quote.trading_time = SimpleNamespace(day=day, night=[["21:00:00", "25:00:00"]])
        self.feed.recompute(messages.append)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "bars")
        self.assertEqual(messages[0]["bars"], [])
        self.assertEqual(messages[0]["cfg"]["tradingTime"]["day"], day)
        previous = self.feed.snapshots[0]
        self.feed.quote.trading_time.night[0][1] = "26:30:00"
        self.assertEqual(previous["cfg"]["tradingTime"]["night"][0][1], "25:00:00")
        messages.clear()
        self.feed.recompute(messages.append)
        self.assertEqual(messages[0]["cfg"]["tradingTime"]["night"][0][1], "26:30:00")
        messages.clear()
        self.feed.recompute(messages.append)
        self.assertEqual(messages, [], "时段不变时不用重复下发配置")

    def test_trading_time_dict_and_rollup_use_the_same_contract_schedule(self):
        periods = {"day": [["09:00:00", "15:00:00"]], "night": []}
        self.feed.quote.trading_time = periods
        rolled = ingest.FuturesRollupFeed(self.feed.symbol, 3600, lambda: self.feed)
        self.assertEqual(rolled.cfg()["tradingTime"], periods)
        self.assertEqual(rolled.cfg()["tf"], 3600)
        from unittest.mock import Mock
        api = Mock()
        api.is_changing.side_effect = lambda obj, *keys: obj is self.feed.quote and "trading_time" in keys[0]
        self.assertTrue(self.feed.data_changing(api))

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

    def test_ws_relays_history_notice_without_revision(self):
        manager = ingest.FeedManager()
        manager.feeds[ingest.feed_key(self.feed.symbol, self.feed.tf)] = self.feed
        self.feed.recompute(lambda _: None)
        sent = asyncio.Queue()
        socket = SimpleNamespace(accept=lambda: asyncio.sleep(0), close=lambda **_: asyncio.sleep(0),
                                 send_json=lambda message: asyncio.sleep(0, sent.put_nowait(message)))

        async def exercise():
            manager.loop = asyncio.get_running_loop()
            task = asyncio.create_task(server.ws(socket, self.feed.symbol, 0, False))
            try:
                first = await asyncio.wait_for(sent.get(), timeout=2)
                manager.broadcast({"type": "history", "symbol": self.feed.symbol, "tf": self.feed.tf,
                                   "ltf": 0, "historyRevision": 3, "historyBackfill": None})
                notice = await asyncio.wait_for(sent.get(), timeout=2)
                self.assertEqual((notice["type"], notice["historyRevision"]), ("history", 3))
                # 通知不推进 bar 的 revision: 同一 revision 之后的增量照常转发
                manager.broadcast({"type": "bars", "symbol": self.feed.symbol, "tf": self.feed.tf,
                                   "ltf": 0, "revision": first["revision"] + 1, "bars": []})
                self.assertEqual((await asyncio.wait_for(sent.get(), timeout=2))["type"], "bars")
            finally:
                task.cancel()
                await task

        with patch.object(server, "manager", manager):
            asyncio.run(exercise())



class FuturesTailTests(unittest.TestCase):
    """期货也只算尾部(见 Feed._kline_changes / _tail_since): SDK 原地改末根 K 线、追加 tick 和 K 线,
    每一轮的窗口、快照、推送、足迹与写出的文件都要和每轮整窗口重算的一模一样。"""

    SETTINGS = '{"*": {"warmup": 15, "windowSec": 60, "lagSec": 2, "minQty": 3, "minNotional": 0}}'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()                     # 需求按时间过期: 两个 Feed 要在同一轮过期
        self.patches = [patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": self.SETTINGS}),
                        patch.object(ingest, "SNAPSHOT_BARS", 15), patch.object(ingest.time, "monotonic", self.clock)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def tick(state, roll=False):
        state["id"] += 1
        state["t"] += 1_500_000_000
        state["price"] += (state["id"] * 7919) % 3 - 1
        before = 0.0 if roll else state["volume"]
        state["volume"] = ((0.0 if roll else state["volume"]) + (state["id"] * 104729) % 9 * (state["id"] % 4 > 0)
                           + (60 if state["id"] % 61 == 0 else 0))      # 偶尔一笔大成交(间隔超过基线的时间窗)
        bid = state["price"] - state["id"] % 2
        return {"id": state["id"], "datetime": float(state["t"]), "last_price": state["price"],
                "ask_price1": bid + 1, "bid_price1": bid, "volume": state["volume"]}, state["volume"] - before

    @staticmethod
    def apply(candles, tick, dv):
        start = float((int(tick["datetime"]) - 1) // 10**10 * 10**10)
        last = candles.index[-1]
        price = tick["last_price"]
        if candles.at[last, "datetime"] == start:
            candles.loc[last, ["high", "low", "close", "volume"]] = [
                max(candles.at[last, "high"], price), min(candles.at[last, "low"], price), price,
                candles.at[last, "volume"] + dv]
            return candles
        row = pd.DataFrame([{"datetime": start, "open": price, "high": price, "low": price, "close": price,
                             "volume": dv}])
        return pd.concat([candles, row], ignore_index=True).tail(40).reset_index(drop=True)

    def test_tail_recompute_matches_full_recompute_every_round(self):
        state = {"id": 0, "t": 1_800_000_000 * 10**9, "price": 3000.0, "volume": 500.0}
        i = pd.Series(range(-40, 0), dtype=float)
        candles = pd.DataFrame({"datetime": (1_800_000_000 + i * 10) * 10**9, "open": 3000.0 + i % 5,
                                "high": 3003.0, "low": 2997.0, "close": 3001.0, "volume": 20.0 + i % 3})
        window = []
        for _ in range(150):
            tick, dv = self.tick(state)
            window.append(tick)
            candles = self.apply(candles, tick, dv)
        quote = SimpleNamespace(price_tick=1.0, volume_multiple=None)
        feeds = {}
        for name in ("tail", "full"):
            folder = os.path.join(self.temp.name, name)
            os.makedirs(folder)
            with patch.object(ingest, "DATA_DIR", folder):
                feed = ingest.Feed(server.DEFAULT_SYMBOL, 10)
                feed.quote = quote
                feed._store(0)
            feeds[name] = (feed, folder)
        tails = []
        publish = feeds["tail"][0]._publish
        feeds["tail"][0]._publish = lambda ltf, bars, previous, revision, tail=None: (
            tails.append(tail is not None), publish(ltf, bars, previous, revision, tail))[1]
        for step in range(70):
            self.clock.advance(61 if step == 30 else 1.5)    # 隔一阵没人要足迹图: 它的需求过期, 又回到只算尾部
            if step == 35:                       # 断线重连: tick 窗口整个换掉, 与旧窗口不重叠
                state["id"] += 30
                window = []
            for _ in range(1 if step != 35 else 150):
                tick, dv = self.tick(state, roll=step == 50)
                window.append(tick)
                candles = self.apply(candles, tick, dv)
            window = window[-150:]
            if step == 10:
                quote.volume_multiple = 10       # 合约乘数后到
            if step % 17 == 8:
                candles.loc[candles.index[-3], "volume"] += 1     # SDK 修订了前面一根
            if step == 60:                       # 别处往历史文件写了窗口中间几根
                part = candles.iloc[-12:-9]
                rows = pd.DataFrame({"time": (part.datetime // 10**9 + TZ_SHIFT_S).astype("int64"),
                                     "buy": part.volume, "sell": 0.0, "delta": part.volume, "unknown": 0.0,
                                     "buyLegacy": part.volume, "sellLegacy": 0.0, "coverage": "complete",
                                     "hasBaseline": True})
                for feed, _ in feeds.values():
                    feed._store(0).save_completed(rows, final=True)
            got = {}
            for name, (feed, folder) in feeds.items():
                feed.ticks = pd.DataFrame(window)
                feed.klines = candles
                if 20 <= step < 25:
                    feed.request(footprint=True)
                if name == "full":
                    feed._tail_marks.clear()
                    feed._record_state.clear()
                messages = []
                with patch.object(ingest, "DATA_DIR", folder):
                    feed.recompute(messages.append)
                got[name] = (json.dumps([messages, feed.snapshots, feed.footprint], sort_keys=True), feed.latest[0][1])
            self.assertEqual(got["tail"][0], got["full"][0], step)
            pd.testing.assert_frame_equal(got["tail"][1], got["full"][1])
        tail_dir, full_dir = feeds["tail"][1], feeds["full"][1]
        for name in sorted(os.listdir(full_dir)):
            with open(os.path.join(full_dir, name), "rb") as a, open(os.path.join(tail_dir, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)
        self.assertGreater(sum(tails), len(tails) // 2, "大部分轮次应当只算尾部")
        self.assertTrue(feeds["tail"][0].large_trades.event_rows(), "大额事件也走过")


class StaticFilesTests(unittest.TestCase):
    def test_frontend_files_revalidate_every_load(self):
        # 不带 Cache-Control 时浏览器按 Last-Modified 启发式缓存, 更新后新旧脚本混用
        files = server.app.routes[-1].app
        plain = {"type": "http", "method": "GET", "path": "/app.js", "headers": []}
        first = asyncio.run(files.get_response("app.js", plain))
        self.assertEqual((first.status_code, first.headers["cache-control"]), (200, "no-cache"))
        again = dict(plain, headers=[(b"if-none-match", first.headers["etag"].encode())])
        second = asyncio.run(files.get_response("app.js", again))
        self.assertEqual((second.status_code, second.headers["cache-control"]), (304, "no-cache"))
        index = asyncio.run(files.get_response(".", dict(plain, path="/")))
        self.assertEqual(index.headers["cache-control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
