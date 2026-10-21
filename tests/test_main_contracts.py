"""主力入口采集实际月份: 订阅、历史隔离、后台换月与完整快照。"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import app as server
import ingest
from test_feed_lifecycle import ScriptedApi
from test_period import Clock

MAIN = "KQ.m@SHFE.fu"
OLD = "SHFE.fu2611"
NEW = "SHFE.fu2701"


class MonthApi(ScriptedApi):
    def __init__(self, manager, clock, contract=OLD, actions=()):
        super().__init__(manager, clock, actions)
        self.contract = contract
        self.contracts = {}          # 其它主连 -> 标的(没写的都按 self.contract)
        self.info_calls = []
        self.serial_calls = []
        self.info_error = None

    def query_symbol_info(self, symbols):
        self.info_calls.append(list(symbols))
        if self.info_error:
            raise self.info_error
        return pd.DataFrame([{"instrument_id": symbol,
                              "underlying_symbol": self.contracts.get(symbol, self.contract)}
                             for symbol in symbols])

    def get_kline_serial(self, symbol, seconds, data_length):
        self.serial_calls.append(("kline", symbol, seconds))
        frame = super().get_kline_serial(symbol, seconds, data_length)
        frame[["open", "high", "low", "close"]] = 5700. if symbol == OLD else 4800.
        return frame

    def get_tick_serial(self, symbol, data_length):
        self.serial_calls.append(("tick", symbol))
        return super().get_tick_serial(symbol, data_length)


class MainContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        paths = patch.object(ingest, "DATA_DIR", self.directory.name)
        paths.start()
        self.addCleanup(paths.stop)
        self.clock = Clock()
        clock = patch.object(ingest.time, "monotonic", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.manager = ingest.FeedManager()
        self.api = MonthApi(self.manager, self.clock)

    def test_main_uses_actual_month_for_quote_ticks_all_klines_and_history(self):
        self.api.contract = NEW
        feed = ingest.Feed(MAIN)
        feed.subscribe(self.api)
        feed.request(ltf=5)
        feed.ensure_ltf_subscriptions(self.api)
        self.assertEqual(feed.symbol, MAIN)
        self.assertEqual(feed.contract, NEW)
        self.assertEqual([symbol for symbol, _ in self.api.quote_calls], [NEW])
        self.assertEqual(self.api.serial_calls,
                         [("kline", NEW, 30), ("tick", NEW), ("kline", NEW, 5)])
        self.assertEqual(feed._store(0).path.name, "SHFE_fu2701_30s_ltf0_v3.csv")
        self.assertEqual(feed._store(5).path.name, "SHFE_fu2701_30s_kline_ltf5_v3.csv")

    def test_unknown_main_mapping_never_falls_back_to_continuous_data(self):
        for value in ("", "SHFE.rb2701", "KQ.m@SHFE.fu", "nan"):
            with self.subTest(value=value):
                self.api.contract = value
                with self.assertRaisesRegex(RuntimeError, "当前月份尚未就绪"):
                    ingest.Feed(MAIN).subscribe(self.api)
        self.assertEqual(self.api.quote_calls, [])
        self.assertEqual(self.api.serial_calls, [])

    def test_rollover_drops_old_flow_caches_and_sends_full_replacement(self):
        feed = ingest.Feed(MAIN)
        feed.request(ltf=5, footprint=True)
        feed.subscribe(self.api)
        feed.ensure_ltf_subscriptions(self.api)
        feed.recompute(lambda message: None)
        old_revision = feed.revision
        old_store = feed._store(0)
        old_bytes = old_store.path.read_bytes()
        old_detector = feed.large_trades
        self.api.contract = NEW
        feed.subscribe(self.api)
        self.assertEqual(feed.snapshots, {})
        self.assertEqual(feed.latest, {})
        self.assertEqual(feed.stores, {})
        self.assertEqual(feed._record_state, {})
        self.assertIsNone(feed.footprint)
        self.assertFalse(feed.ready.is_set())
        self.assertEqual(feed.revision, old_revision)
        self.assertIsNot(feed.large_trades, old_detector)
        self.assertIn(5, feed._requested)
        self.assertEqual(feed._store(0).values, {})
        self.assertEqual(feed._store(0).estimates, {})
        self.assertEqual(old_store.path.read_bytes(), old_bytes)
        messages = []
        feed.ensure_ltf_subscriptions(self.api)
        feed.recompute(messages.append)
        for ltf in (0, 5):
            snapshot = next(m for m in messages if m["type"] == "snapshot" and m["ltf"] == ltf)
            self.assertEqual(snapshot["symbol"], MAIN)
            self.assertEqual(snapshot["contract"], NEW)
            self.assertGreater(snapshot["revision"], old_revision)
            self.assertTrue(all(bar["close"] == 4800. for bar in snapshot["bars"]))

    def test_reconnect_on_the_same_month_merges_instead_of_replacing(self):
        """断线重连、闲置回收重建连接都会清掉快照, 但月份没变: 只发按时间合并的增量, 不清页面的足迹。"""
        for symbol in (MAIN, OLD):
            with self.subTest(symbol=symbol):
                feed = ingest.Feed(symbol)
                feed.subscribe(self.api)
                feed.recompute(lambda message: None)
                feed.release_serials()
                feed.subscribe(self.api)
                messages = []
                feed.recompute(messages.append)
                self.assertEqual([message["type"] for message in messages], ["bars"])
                self.assertFalse(feed.rolled)

    def test_large_trade_overrides_written_for_the_main_follow_its_current_month(self):
        settings = '{"SHFE.fu":{"minQty":10},"KQ.m@SHFE.fu":{"minQty":20,"warmup":30},"SHFE.fu2611":{"minQty":40}}'
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": settings}):
            main = ingest.Feed(MAIN)
            main.subscribe(self.api)
            self.assertEqual(main.large_trades.symbol, OLD)
            self.assertEqual((main.large_trades.settings["minQty"], main.large_trades.settings["warmup"]), (40, 30),
                             "完整月份 > 主连代码 > 品种")
            self.api.contract = NEW
            main.subscribe(self.api)
            self.assertEqual((main.large_trades.symbol, main.large_trades.settings["minQty"]), (NEW, 20))
            fixed = ingest.Feed(NEW)
            fixed.subscribe(self.api)
            self.assertEqual(fixed.large_trades.settings["minQty"], 10, "固定月份不套主连的门槛")

    def test_fixed_month_subscription_does_not_query_or_follow_main(self):
        self.api.contract = NEW
        feed = ingest.Feed(OLD)
        feed.subscribe(self.api)
        self.assertEqual(feed.contract, OLD)
        self.assertEqual(self.api.info_calls, [])
        self.assertEqual(self.api.serial_calls, [("kline", OLD, 30), ("tick", OLD)])

    def test_periodic_check_rebuilds_pinned_feeds_without_any_browser(self):
        self.manager.set_pinned([(MAIN, 10), (MAIN, 30)])
        rollup = self.manager.ensure(MAIN, 300)
        apis, messages = [], []
        self.manager.broadcast = messages.append

        def switch(api):
            api.contract = NEW
            self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)

        def factory(**kwargs):
            api = MonthApi(self.manager, self.clock,
                           OLD if not apis else NEW,
                           [switch] if not apis else [lambda api: None])
            apis.append(api)
            return api

        with patch.dict("os.environ", {"TQ_USER": "test", "TQ_PASS": "test"}), \
             patch.object(ingest, "TqApi", side_effect=factory), \
             patch.object(ingest, "TqAuth", return_value=None), \
             patch.object(self.manager._stop, "wait", return_value=False):
            self.manager._run()
        self.assertEqual(len(apis), 2)
        self.assertTrue(all(api.closed for api in apis))
        self.assertEqual(self.manager.clients, {})
        self.assertEqual({feed.contract for feed in self.manager.feeds.values()}, {NEW})
        self.assertIn(("kline", NEW, 300), apis[1].serial_calls)
        native = [self.manager.feeds[(MAIN, tf)] for tf in (10, 30)]
        self.assertIs(native[0].large_trades, native[1].large_trades)
        self.assertEqual(rollup.snapshots[0]["contract"], NEW)
        self.assertTrue(all(bar["close"] == 4800. for bar in rollup.snapshots[0]["bars"]))
        self.assertTrue(any(m["type"] == "snapshot" and m.get("contract") == NEW for m in messages))
        self.assertTrue(list(Path(self.directory.name).glob("SHFE_fu2611_*.csv")))
        self.assertTrue(list(Path(self.directory.name).glob("SHFE_fu2701_*.csv")))
        self.assertFalse(list(Path(self.directory.name).glob("KQ_m_SHFE_fu_*.csv")))

    def test_failed_mapping_refresh_keeps_previous_data_and_retries_on_interval(self):
        feed = self.manager.ensure(MAIN)
        self.manager._run_api_attempt(self.api, feed)
        calls = len(self.api.info_calls)
        self.manager._check_main_contracts(self.api)
        self.assertEqual(len(self.api.info_calls), calls, "订阅时刚查过, 一分钟内不再查")
        self.api.info_error = RuntimeError("temporary metadata failure")
        self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
        self.manager._check_main_contracts(self.api)
        self.assertIn("temporary metadata failure", self.manager.main_contract_error)
        self.assertFalse(self.manager._rebuild_api.is_set())
        self.assertEqual(feed.contract, OLD)
        calls = len(self.api.info_calls)
        self.manager._check_main_contracts(self.api)
        self.assertEqual(len(self.api.info_calls), calls)
        self.api.info_error = None
        self.api.contract = NEW
        self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
        self.manager._check_main_contracts(self.api)
        self.assertTrue(self.manager._rebuild_api.is_set())
        self.assertIsNone(self.manager.main_contract_error)

    def test_one_unresolvable_main_does_not_block_other_rollovers(self):
        other = "KQ.m@DCE.i"
        self.api.contracts[other] = "DCE.i2701"
        fuel, iron = self.manager.ensure(MAIN), self.manager.ensure(other)
        for feed in (fuel, iron):
            self.assertTrue(self.manager._run_api_attempt(self.api, feed))
        self.api.contracts[other] = "nan"          # 品种下市或合约服务暂时不完整
        self.api.contract = NEW
        self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
        self.manager._check_main_contracts(self.api)
        self.assertEqual(sorted(self.api.info_calls[-1]), sorted([MAIN, other]), "一次查完")
        self.assertEqual(fuel.contract, NEW)
        self.assertEqual(iron.contract, "DCE.i2701", "没核对上的沿用上次的月份")
        self.assertTrue(self.manager._rebuild_api.is_set())
        self.assertIn(other, self.manager.main_contract_error)
        self.assertNotIn(MAIN, self.manager.main_contract_error)

    def test_several_periods_of_one_main_share_one_mapping_query(self):
        feeds = [self.manager.ensure(MAIN, tf) for tf in (10, 30, 300)]
        self.manager._sweep_subscriptions(self.api)
        self.assertEqual(self.api.info_calls, [[MAIN]])
        self.assertEqual({feed.contract for feed in feeds}, {OLD})


class SharedMonthTests(unittest.TestCase):
    """固定月份正好是主力当前的月份时借用主力 Feed: 只采集、落盘一份, 推送按页面自己的代码。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        paths = patch.object(ingest, "DATA_DIR", self.directory.name)
        paths.start()
        self.addCleanup(paths.stop)
        self.clock = Clock()
        clock = patch.object(ingest.time, "monotonic", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.manager = ingest.FeedManager()
        # 重连通知在事件循环里执行; 测试里直接调用
        self.manager.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))
        self.api = MonthApi(self.manager, self.clock)

    def subscribed_main(self, tf=30):
        feed = self.manager.ensure(MAIN, tf)
        self.assertTrue(self.manager._run_api_attempt(self.api, feed))
        return feed

    def client(self, symbol, tf=30):
        queue = asyncio.Queue()
        self.manager.add_client(queue, symbol, 0, tf=tf)
        return queue

    def test_fixed_month_borrows_the_main_feed_and_receives_its_own_symbol(self):
        main = self.subscribed_main()
        self.assertIs(self.manager.ensure(OLD), main)
        status = self.manager.status_snapshot()
        self.assertEqual(status["feeds"], [f"{MAIN}@30s"])
        self.assertEqual(status["aliases"], {f"{OLD}@30s": f"{MAIN}@30s"})
        fixed, continuous = self.client(OLD), self.client(MAIN)
        self.manager._fanout({"type": "bars", "symbol": MAIN, "tf": 30, "ltf": 0, "revision": 1, "bars": []})
        self.assertEqual(fixed.get_nowait()["symbol"], OLD)
        self.assertEqual(continuous.get_nowait()["symbol"], MAIN)
        self.manager._sweep_subscriptions(self.api)
        self.assertEqual([call for call in self.api.serial_calls if call[0] == "tick"], [("tick", OLD)])

    def test_fixed_month_opened_first_is_folded_into_the_main_without_rebuilding(self):
        fixed = self.manager.ensure(OLD)
        self.assertTrue(self.manager._run_api_attempt(self.api, fixed))
        page = self.client(OLD)
        main = self.subscribed_main()
        self.assertIs(self.manager.feeds[(OLD, 30)], main)
        self.assertFalse(self.manager.serves(OLD, 30, fixed), "拿着它等快照的请求要换成主力 Feed")
        self.assertNotIn((OLD, 30), self.manager._subscribed)
        self.assertFalse(self.manager._rebuild_api.is_set())
        self.assertEqual(page.get_nowait()["type"], "resync")
        self.assertEqual(self.manager.status_snapshot()["feeds"], [f"{MAIN}@30s"])

    def test_rollover_gives_the_old_fixed_month_its_own_feed(self):
        self.manager.set_pinned([(MAIN, 30), (OLD, 30)])
        main = self.manager.feeds[(MAIN, 30)]
        self.manager._sweep_subscriptions(self.api)
        self.assertIs(self.manager.feeds[(OLD, 30)], main)
        page = self.client(OLD)
        self.api.contract = NEW
        self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
        self.manager._check_main_contracts(self.api)
        self.assertEqual(main.contract, NEW)
        self.assertNotIn((OLD, 30), self.manager.feeds)
        self.assertEqual(page.get_nowait()["type"], "resync")
        self.assertIsNone(self.manager._main_peer((OLD, 30)), "重连时不能又借回已换月的主力")
        self.manager._ensure_pinned()
        fixed = self.manager.feeds[(OLD, 30)]
        self.assertIsNot(fixed, main)
        self.assertEqual((fixed.symbol, fixed.contract), (OLD, OLD))

    def test_rollover_rebuilds_the_borrowed_base_of_a_fixed_month_higher_timeframe(self):
        main = self.subscribed_main()
        rollup = self.manager.ensure(OLD, 300)
        self.assertIs(rollup.base(), main)
        page = self.client(OLD, 300)
        self.api.contract = NEW
        self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
        self.manager._check_main_contracts(self.api)
        self.assertIsNone(rollup.base(), "不能再拿已换月的主力合成旧月份")
        self.manager._ensure_pinned()
        base = rollup.base()
        self.assertIsNot(base, None, "只开着大周期页面时, 底层也要另建")
        self.assertIsNot(base, main)
        self.assertEqual((base.symbol, base.tf), (OLD, 30))
        self.assertTrue(page.empty(), "大周期自己的 Feed 没换, 页面不必重连")

    def test_failed_main_rebuilds_the_borrowed_base_of_a_fixed_month_higher_timeframe(self):
        main = self.subscribed_main()
        rollup = self.manager.ensure(OLD, 300)
        self.manager.fail_feed(main, "主力映射暂时不可用")
        self.manager._ensure_pinned()
        self.assertIsNot(rollup.base(), None)
        self.assertIsNot(rollup.base(), main)

    def test_shared_large_trade_detector_keeps_each_entrys_thresholds_whoever_subscribes_first(self):
        settings = '{"SHFE.fu":{"minQty":10},"KQ.m@SHFE.fu":{"minQty":20}}'
        for main_first in (True, False):
            with self.subTest(main_first=main_first), patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": settings}):
                self.manager = ingest.FeedManager()
                self.api = MonthApi(self.manager, self.clock)
                if main_first:
                    main = self.subscribed_main()
                fixed = self.manager.ensure(OLD, 10)
                self.assertTrue(self.manager._run_api_attempt(self.api, fixed))
                if not main_first:
                    main = self.subscribed_main()
                self.assertEqual((main.large_trades.settings["minQty"], fixed.large_trades.settings["minQty"]),
                                 (20, 10), "主连按主连代码的门槛, 固定月份不套主连的")
                self.assertIsNot(main.large_trades, fixed.large_trades)
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": '{"SHFE.fu":{"minQty":10}}'}):
            self.manager = ingest.FeedManager()
            self.api = MonthApi(self.manager, self.clock)
            fixed = self.manager.ensure(OLD, 10)
            self.assertTrue(self.manager._run_api_attempt(self.api, fixed))
            self.assertIs(self.subscribed_main().large_trades, fixed.large_trades, "门槛一样仍共用一个检测器")

    def test_failed_main_releases_borrowed_fixed_month(self):
        main = self.subscribed_main()
        self.manager.ensure(OLD)
        page = self.client(OLD)
        self.manager.fail_feed(main, "主力映射暂时不可用")
        self.assertNotIn((OLD, 30), self.manager.feeds)
        self.assertEqual(page.get_nowait()["type"], "resync")
        self.assertIsNot(self.manager.ensure(OLD), main)

    def test_different_large_trade_settings_do_not_alias_native_or_rollup_feeds(self):
        settings = '{"SHFE.fu":{"minQty":10},"KQ.m@SHFE.fu":{"minQty":20}}'
        for tf in (30, 300):
            for main_first in (True, False):
                with self.subTest(tf=tf, main_first=main_first), patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": settings}):
                    self.manager = ingest.FeedManager()
                    self.api = MonthApi(self.manager, self.clock)
                    if main_first:
                        main = self.subscribed_main(tf)
                    fixed = self.manager.ensure(OLD, tf)
                    self.manager._sweep_subscriptions(self.api)
                    if not main_first:
                        main = self.subscribed_main(tf)
                    self.manager._sweep_subscriptions(self.api)
                    self.assertIs(self.manager.feeds[(OLD, tf)], fixed)
                    self.assertIsNot(main, fixed)
                    self.assertEqual(main.large_sources()[0].settings["minQty"], 20)
                    self.assertEqual(fixed.large_sources()[0].settings["minQty"], 10)
                    self.assertEqual(self.manager.status_snapshot()["aliases"], {})

    def test_different_large_trade_settings_count_separate_slots_and_do_not_duplicate_history(self):
        settings = '{"SHFE.fu":{"minQty":10},"KQ.m@SHFE.fu":{"minQty":20}}'
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": settings}):
            main = self.subscribed_main()
            fixed = self.manager.ensure(OLD)
            self.assertIsNot(main, fixed)
            self.manager._sweep_subscriptions(self.api)
            main.recompute(lambda message: None)
            path = main._store(0).path
            before = path.read_bytes()
            fixed.recompute(lambda message: None)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(main.snapshots[0]["bars"][-1]["cvd"], fixed.snapshots[0]["bars"][-1]["cvd"])
            with patch.object(ingest, "MAX_PINNED_FEEDS", 1):
                self.assertEqual(self.manager.set_pinned([(MAIN, 30), (OLD, 30)]), [(MAIN, 30)])
                self.assertEqual(self.manager.pin_skipped, [(OLD, 30)])

    def test_shared_feed_takes_one_pinned_slot(self):
        for tf in (10, 30):
            self.subscribed_main(tf)
        with patch.object(ingest, "MAX_PINNED_FEEDS", 3):
            pinned = self.manager.set_pinned([(MAIN, 10), (MAIN, 30), (OLD, 10), (OLD, 30), ("SHFE.rb2701", 30)])
        self.assertEqual(len(pinned), 5)
        self.assertEqual(self.manager.pin_skipped, [])
        self.assertIs(self.manager.feeds[(OLD, 10)], self.manager.feeds[(MAIN, 10)])

    def test_main_stays_while_only_the_borrowing_fixed_page_is_open(self):
        main = self.subscribed_main()
        self.manager.ensure(OLD)
        page = self.client(OLD)
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager._prune()
        self.assertIs(self.manager.feeds.get((MAIN, 30)), main)
        self.manager.remove_client(page)
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager._prune()
        self.assertEqual(self.manager.feeds, {})
        self.assertTrue(self.manager._rebuild_api.is_set())

    def test_ws_snapshot_of_a_borrowing_fixed_month_carries_the_fixed_symbol(self):
        main = self.subscribed_main()
        main.recompute(lambda message: None)

        class Socket:
            def __init__(self):
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def send_json(self, message):
                self.messages.put_nowait(message)

            async def close(self, **kwargs):
                self.messages.put_nowait({"closed": kwargs})

        async def exercise():
            self.manager.loop = asyncio.get_running_loop()
            socket = Socket()
            task = asyncio.create_task(server.ws(socket, OLD, 0, False))
            try:
                first = await asyncio.wait_for(socket.messages.get(), timeout=2)
                self.assertEqual((first["type"], first["symbol"], first["contract"]), ("snapshot", OLD, OLD))
                history = await server.history(OLD, 0, 30)
                self.assertEqual((history["symbol"], history["contract"]), (OLD, OLD))
            finally:
                task.cancel()
                await task

        with patch.object(server, "manager", self.manager):
            asyncio.run(exercise())
        self.assertIs(self.manager.feeds[(OLD, 30)], main)

    def test_history_waiting_on_a_borrowed_main_does_not_return_the_next_month(self):
        main = self.subscribed_main()
        self.assertIs(self.manager.ensure(OLD), main)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                # 页面正在等快照时主力换月, 新月份的快照很快算好
                self.api.contract = NEW
                self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
                self.manager._check_main_contracts(self.api)
                main.subscribe(self.api, NEW)
                main.recompute(lambda message: None)
            elif len(waits) == 2:
                fixed = self.manager.feeds[(OLD, 30)]
                self.assertIsNot(fixed, main)
                fixed.subscribe(self.api)
                fixed.recompute(lambda message: None)

        with patch.object(server, "manager", self.manager), patch.object(server.asyncio, "sleep", sleep):
            history = asyncio.run(server.history(OLD, 0, 30))
        self.assertEqual((history["symbol"], history["contract"]), (OLD, OLD))
        self.assertEqual(history["bars"][-1]["close"], 5700.)

    def test_footprint_of_a_borrowing_fixed_month_carries_the_fixed_symbol(self):
        main = self.subscribed_main()
        main.request(footprint=True)
        main.recompute(lambda message: None)
        with patch.object(server, "manager", self.manager):
            result = asyncio.run(server.footprint(OLD, 30))
        self.assertEqual(result["symbol"], OLD)
        self.assertEqual(result["bars"], main.footprint["bars"])
        self.assertEqual(main.footprint["symbol"], MAIN, "不能改共享快照里的原始代码")

    def test_footprint_waiting_on_a_borrowed_main_does_not_return_the_next_month(self):
        main = self.subscribed_main()
        self.assertIs(self.manager.ensure(OLD), main)
        waits = []

        def publish(feed, price):
            feed.request(footprint=True)
            feed.recompute(lambda message: None)
            feed.footprint["bars"] = [{"time": 1700000000, "levels": [{"price": price}]}]

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.api.contract = NEW
                self.clock.advance(ingest.MAIN_CONTRACT_CHECK_SEC)
                self.manager._check_main_contracts(self.api)
                main.subscribe(self.api, NEW)
                publish(main, 4800.)
            elif len(waits) == 2:
                fixed = self.manager.feeds[(OLD, 30)]
                self.assertIsNot(fixed, main)
                fixed.subscribe(self.api)
                publish(fixed, 5700.)

        with patch.object(server, "manager", self.manager), patch.object(server.asyncio, "sleep", sleep):
            result = asyncio.run(server.footprint(OLD, 30))
        self.assertEqual(result["symbol"], OLD)
        self.assertEqual(result["bars"][0]["levels"][0]["price"], 5700.)


if __name__ == "__main__":
    unittest.main()
