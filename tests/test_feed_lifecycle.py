"""驱动实际采集循环验证重试和回收；SDK、时钟和历史目录均隔离。"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ingest
from test_data import ticks
from test_period import Clock, klines
from tqsdk import TqTimeoutError


class ScriptedApi:
    def __init__(self, manager, clock, actions=(), quote_failures=0, lower_failures=0):
        self.manager, self.clock = manager, clock
        self.actions = iter(actions)
        self.quote_failures, self.lower_failures = quote_failures, lower_failures
        self.quote_calls, self.lower_calls = [], []
        self.resources = {}
        self.closed = False

    def get_quote(self, symbol):
        self.quote_calls.append((symbol, self.clock()))
        value = self.resources.setdefault(("quote", symbol), SimpleNamespace(price_tick=1.))
        if self.quote_failures:
            self.quote_failures -= 1
            raise RuntimeError("temporary subscription failure")
        return value

    def get_kline_serial(self, symbol, seconds, data_length):
        if seconds == 5:
            self.lower_calls.append(self.clock())
            if self.lower_failures:
                self.lower_failures -= 1
                raise RuntimeError("temporary lower candle failure")
        return self.resources.setdefault(("kline", symbol, seconds), klines(seconds, 3))

    def get_tick_serial(self, symbol, data_length):
        return self.resources.setdefault(("tick", symbol), ticks())

    def is_changing(self, *args):
        return False

    def query_quotes(self, ins_class=None, expired=None, **kwargs):
        """连接自检会用到; 返回一个合约即表示"连接还能收发"。"""
        return ["KQ.m@SHFE.fu"] if ins_class == "CONT" else []

    def wait_update(self, **kwargs):
        try:
            action = next(self.actions)
        except StopIteration:
            self.manager._stop.set()
        else:
            action(self)

    def close(self):
        self.closed = True
        self.resources.clear()


class FeedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        paths = patch.object(ingest, "DATA_DIR", self.directory.name)
        paths.start()
        self.addCleanup(paths.stop)
        self.clock = Clock()
        clock_patch = patch.object(ingest.time, "monotonic", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.manager = ingest.FeedManager()

    def feed(self, symbol="SHFE.review"):
        feed = self.manager.ensure(symbol, 30)
        def recompute(broadcast):
            feed.snapshots = {0: {"bars": []}}
            feed._computed_version = feed._demand_version
        feed.recompute = Mock(side_effect=recompute)
        return feed

    def advance(self, seconds):
        return lambda api: self.clock.advance(seconds)

    def run_manager(self, factory):
        with patch.dict(os.environ, {"TQ_USER": "test", "TQ_PASS": "test"}), \
             patch.object(ingest, "TqApi", side_effect=factory), \
             patch.object(ingest, "TqAuth", return_value=None), \
             patch.object(self.manager._stop, "wait", return_value=False):
            self.manager._run()

    def test_failed_feed_retries_after_cooldown_without_page_or_async_timer(self):
        feed = self.feed()
        api = ScriptedApi(self.manager, self.clock,
                          [self.advance(29), self.advance(1), lambda api: None], quote_failures=1)
        self.manager._run_api(api)
        self.assertEqual([when for _, when in api.quote_calls], [1000, 1030])
        self.assertIsNone(feed.error)
        self.assertEqual(self.manager.failed_feeds, {})
        self.assertIs(self.manager._subscribed[(feed.symbol, 30)], feed)
        self.assertEqual(feed.recompute.call_count, 1)

    def test_repeated_failure_restarts_cooldown_without_duplicate_attempts(self):
        self.feed()
        api = ScriptedApi(self.manager, self.clock,
                          [self.advance(30), self.advance(29), self.advance(1)], quote_failures=2)
        self.manager._run_api(api)
        self.assertEqual([when for _, when in api.quote_calls], [1000, 1030, 1060])
        self.assertEqual(self.manager.failed_feeds, {})

    def test_ensure_does_not_claim_recovery_before_subscription_succeeds(self):
        feed = self.feed()
        self.manager.fail_feed(feed, "failed")
        self.assertIn("30 秒后", self.manager.feed_retry_error(feed))
        self.clock.advance(30)
        self.assertIs(self.manager.ensure(feed.symbol), feed)
        self.assertIsNone(self.manager.feed_retry_error(feed))
        self.assertIn((feed.symbol, 30), self.manager.failed_feeds)
        api = ScriptedApi(self.manager, self.clock)
        self.assertTrue(self.manager._run_api_attempt(api, feed))
        self.assertIsNone(feed.error)
        self.assertIsNone(feed.retry_at)
        self.assertEqual(self.manager.failed_feeds, {})

    def test_eviction_releases_sdk_then_resubscribes_new_instance_and_retained_feed(self):
        old = self.feed()
        held = self.feed("SHFE.held")
        cache = held.stores
        q = asyncio.Queue()
        self.manager.add_client(q, held.symbol, 0)
        apis, rebuilt = [], []
        def evict(api):
            self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
            self.manager._prune()
            self.assertNotIn((old.symbol, 30), self.manager._subscribed)
            self.assertFalse(api.closed)  # 回收只发信号，必须由 _run 关闭 SDK。
            rebuilt.append(self.feed(old.symbol))
        def verify_rebuilt(api):
            self.assertIs(self.manager._subscribed[(old.symbol, 30)], rebuilt[0])
            self.assertIs(rebuilt[0].ticks, api.resources[("tick", old.symbol)])
            self.assertIs(held.stores, cache)
            self.manager._stop.set()
        def factory(**kwargs):
            if apis:
                self.assertTrue(apis[0].closed)
                self.assertEqual(apis[0].resources, {})
                self.assertIsNone(old.ticks)
                self.assertIsNone(held.ticks)
            api = ScriptedApi(self.manager, self.clock, [verify_rebuilt] if apis else [evict])
            apis.append(api)
            return api
        self.run_manager(factory)
        self.assertEqual(len(apis), 2)
        self.assertEqual({symbol for symbol, _ in apis[1].quote_calls}, {old.symbol, held.symbol})
        self.assertTrue(all(api.closed and not api.resources for api in apis))
        self.assertEqual(self.manager._sdk_feeds, set())
        self.assertEqual(self.manager._subscribed, {})

    def test_partial_subscription_resources_are_closed_when_failed_feed_is_evicted(self):
        feed = self.feed()
        apis = []
        def idle(api):
            self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        def factory(**kwargs):
            api = ScriptedApi(self.manager, self.clock, [idle] if not apis else [], quote_failures=1)
            if apis:
                self.assertTrue(apis[0].closed)
                self.assertEqual(apis[0].resources, {})
            apis.append(api)
            return api
        self.run_manager(factory)
        self.assertNotIn((feed.symbol, 30), self.manager.feeds)
        self.assertEqual(len(apis), 2)
        self.assertFalse(apis[1].quote_calls)
        self.assertTrue(all(api.closed for api in apis))

    def test_temporary_computation_error_retries_even_without_new_ticks(self):
        feed = self.feed()
        complete = feed.recompute.side_effect
        def compute(broadcast):
            if feed.recompute.call_count == 1:
                raise OSError("temporary write failure")
            complete(broadcast)
        feed.recompute.side_effect = compute
        api = ScriptedApi(self.manager, self.clock, [lambda api: None, self.advance(.9), self.advance(.1)])
        self.manager._run_api(api)
        self.assertEqual(feed.recompute.call_count, 2)
        self.assertIsNone(feed.error)
        self.assertEqual(self.manager.failed_feeds, {})
        self.assertEqual(len(api.quote_calls), 1)

    def test_lower_candle_subscription_failure_can_recover(self):
        feed = self.feed()
        feed.request(ltf=5)
        api = ScriptedApi(self.manager, self.clock, [lambda api: None, self.advance(1)], lower_failures=1)
        self.manager._run_api(api)
        self.assertEqual(len(api.lower_calls), 2)
        self.assertIn(5, feed.lower_klines)
        self.assertIsNone(feed.error)
        self.assertEqual(feed.recompute.call_count, 1)

    def test_connection_can_recover_while_feed_has_computation_error(self):
        feed = self.feed()
        feed.recompute.side_effect = OSError("temporary write failure")
        apis = []
        def disconnect(api):
            self.assertIsNotNone(feed.error)
            raise ConnectionError("connection lost")
        def verify(api):
            self.assertIsNone(feed.error)
            self.assertIs(self.manager._subscribed[(feed.symbol, 30)], feed)
            self.manager._stop.set()
        def factory(**kwargs):
            api = ScriptedApi(self.manager, self.clock,
                              [verify] if apis else [lambda api: None, disconnect])
            apis.append(api)
            return api
        self.run_manager(factory)
        self.assertEqual(len(apis), 2)
        self.assertEqual(len(apis[1].quote_calls), 1)

    def test_late_failure_from_retired_instance_does_not_poison_replacement(self):
        old = self.feed()
        with self.manager._lock:
            self.manager._forget((old.symbol, 30))
        new = self.feed(old.symbol)
        self.manager.fail_feed(old, "late error")
        self.assertIsNone(new.error)
        self.assertEqual(self.manager.failed_feeds, {})

    def test_eviction_during_subscription_does_not_register_retired_instance(self):
        old = self.feed()
        api = ScriptedApi(self.manager, self.clock)
        get_quote = api.get_quote
        def retire(symbol):
            with self.manager._lock:
                self.manager._forget((symbol, 30))
            self.feed(symbol)
            return get_quote(symbol)
        api.get_quote = retire
        self.assertFalse(self.manager._run_api_attempt(api, old))
        self.assertEqual(self.manager._subscribed, {})
        self.assertTrue(self.manager._rebuild_api.is_set())
        self.assertIsNot(self.manager.feeds[(old.symbol, 30)], old)

    def test_client_and_recent_demand_prevent_eviction_and_disconnect_starts_idle_time(self):
        feed = self.feed()
        q = asyncio.Queue()
        self.manager.add_client(q, feed.symbol, 0)
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager._prune()
        self.assertIn((feed.symbol, 30), self.manager.feeds)
        self.manager.remove_client(q)
        self.manager._prune()
        self.assertIn((feed.symbol, 30), self.manager.feeds)
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        feed.request(demand=True)
        self.manager._prune()
        self.assertIn((feed.symbol, 30), self.manager.feeds)
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager._prune()
        self.assertNotIn((feed.symbol, 30), self.manager.feeds)

    def test_pinned_feed_is_collected_and_never_evicted_without_pages(self):
        """关掉所有页面后常驻合约照常订阅、计算; 移出集合后才回到闲置回收。"""
        key = ("SHFE.pinned", 30)
        self.manager.set_pinned([key])
        feed = self.manager.feeds[key]
        feed.recompute = Mock()
        api = ScriptedApi(self.manager, self.clock,
                          [self.advance(ingest.IDLE_EVICT_SEC + 1), lambda api: None])
        self.manager._run_api(api)
        self.assertIs(self.manager.feeds[key], feed)
        self.assertIs(self.manager._subscribed[key], feed)
        self.assertGreaterEqual(feed.recompute.call_count, 2)
        self.assertEqual(self.manager.status_snapshot()["collecting"], ["SHFE.pinned@30s"])
        self.manager.set_pinned([])
        self.manager._prune()
        self.assertNotIn(key, self.manager.feeds)

    def test_pinned_set_is_capped_and_waits_for_a_free_slot(self):
        keys = [(f"SHFE.pin{index}", 30) for index in range(ingest.MAX_PINNED_FEEDS + 2)]
        for index in range(ingest.MAX_FEEDS):
            self.manager.ensure(f"SHFE.page{index}")
        pinned = self.manager.set_pinned(keys)
        self.assertEqual(pinned, keys[:ingest.MAX_PINNED_FEEDS])
        self.assertEqual(self.manager.status_snapshot()["collectSkipped"],
                         [f"SHFE.pin{index}@30s" for index in (ingest.MAX_PINNED_FEEDS,
                                                               ingest.MAX_PINNED_FEEDS + 1)])
        self.assertFalse(any(key in self.manager.feeds for key in pinned))
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager._prune()
        self.manager._ensure_pinned()
        self.assertEqual(sorted(self.manager.feeds), sorted(pinned))
        self.manager.ensure("SHFE.page-after")

    def test_full_pool_accepts_new_subscription_after_idle_eviction(self):
        for index in range(ingest.MAX_FEEDS):
            self.manager.ensure(f"SHFE.test{index}")
        with self.assertRaises(ValueError):
            self.manager.ensure("SHFE.overflow")
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager.ensure("SHFE.overflow")
        self.assertEqual(len(self.manager.feeds), 1)


class JobQueueTests(unittest.TestCase):
    """一次性 SDK 查询(合约目录)走采集线程执行, 失败不能拖垮采集循环。"""

    def setUp(self):
        self.manager = ingest.FeedManager()

    def test_job_runs_and_returns_its_result(self):
        self.manager.status = "connected"
        future = self.manager.submit_job(lambda api: f"from {api}")
        self.manager._run_jobs("ingest")
        self.assertEqual(future.result(), "from ingest")

    def test_job_is_rejected_while_the_ingest_thread_is_down(self):
        self.manager.status = "error"
        with self.assertRaises(RuntimeError):
            self.manager.submit_job(lambda api: None)

    def test_failing_job_only_fails_itself(self):
        self.manager.status = "connected"
        def boom(api):
            raise OSError("合约服务不可用")
        broken = self.manager.submit_job(boom)
        healthy = self.manager.submit_job(lambda api: "ok")
        self.manager._run_jobs("ingest")
        self.assertIsInstance(broken.exception(), OSError)
        self.assertEqual(healthy.result(), "ok")

    def test_cancelled_job_is_not_executed(self):
        self.manager.status = "connected"
        calls = []
        future = self.manager.submit_job(lambda api: calls.append(api))
        future.cancel()
        self.manager._run_jobs("ingest")
        self.assertEqual(calls, [])

    def test_disconnect_settles_pending_jobs_instead_of_leaving_waiters(self):
        self.manager.status = "connected"
        future = self.manager.submit_job(lambda api: None)
        self.manager.fail_pending_jobs("行情连接已重建")
        self.assertIsInstance(future.exception(), RuntimeError)
        self.manager.stop()   # 覆盖线程未启动时的停止路径

    def test_async_query_returns_the_result_from_the_ingest_thread(self):
        self.manager.status = "connected"
        worker = threading.Thread(target=lambda: (time.sleep(0.05), self.manager._run_jobs("api")))
        worker.start()
        try:
            result = asyncio.run(self.manager.query(lambda api: f"from {api}", timeout=5))
        finally:
            worker.join()
        self.assertEqual(result, "from api")

    def test_async_query_times_out_and_the_job_never_runs(self):
        self.manager.status = "connected"
        ran = []
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(self.manager.query(lambda api: ran.append(api), timeout=0.05))
        self.assertIn("超时", str(caught.exception))
        self.manager._run_jobs("api")   # 采集线程迟到时只看到已取消的任务
        self.assertEqual(ran, [])

    def test_async_query_fails_fast_when_the_thread_is_down(self):
        self.manager.status = "error"
        with self.assertRaises(RuntimeError):
            asyncio.run(self.manager.query(lambda api: None, timeout=5))

    def test_busy_seconds_track_the_running_job(self):
        self.manager.status = "connected"
        self.assertIsNone(self.manager.status_snapshot()["busySec"])
        running = threading.Event()
        release = threading.Event()

        def slow(api):
            running.set()
            release.wait(2)
            return "done"

        worker = threading.Thread(target=lambda: self.manager._run_jobs("api"))
        future = self.manager.submit_job(slow)
        worker.start()
        try:
            self.assertTrue(running.wait(2))
            self.assertIsNotNone(self.manager.status_snapshot()["busySec"])
        finally:
            release.set()
            worker.join()
        self.assertEqual(future.result(), "done")
        self.assertIsNone(self.manager.status_snapshot()["busySec"])
        self.assertEqual(self.manager.status_snapshot()["jobQueue"], 0)

    def test_submit_job_fails_fast_while_a_call_is_stuck(self):
        """连接卡死时不能让每个 HTTP 请求都各等 12 秒。"""
        self.manager.status = "connected"
        self.manager._busy_since = time.monotonic() - ingest.JOB_STALL_SEC - 1
        with self.assertRaises(RuntimeError) as caught:
            self.manager.submit_job(lambda api: None)
        self.assertIn("行情连接无响应", str(caught.exception))
        self.manager._busy_since = None

    def test_a_query_that_hits_the_sdk_timeout_asks_for_a_rebuild(self):
        """正常合约查询是 0.1 秒级; 慢到 sdk 的 30 秒上限就说明这条连接已经不可用了。"""
        self.manager.status = "connected"
        with patch.object(ingest, "JOB_REBUILD_SEC", 0.05):
            future = self.manager.submit_job(lambda api: (time.sleep(0.08), "done")[1])
            self.manager._run_jobs("api")
        self.assertEqual(future.result(), "done")
        self.assertTrue(self.manager._rebuild_api.is_set())
        self.assertIsNone(self.manager.status_snapshot()["busySec"])

    def test_fast_jobs_do_not_ask_for_a_rebuild(self):
        self.manager.status = "connected"
        future = self.manager.submit_job(lambda api: "done")
        self.manager._run_jobs("api")
        self.assertEqual(future.result(), "done")
        self.assertFalse(self.manager._rebuild_api.is_set())


class ProbeTests(unittest.TestCase):
    """闭市没有行情流时靠主动自检发现"连接被静默掐断"。"""

    def setUp(self):
        self.manager = ingest.FeedManager()

    def test_probe_is_due_only_after_the_market_has_been_quiet(self):
        now = time.monotonic()
        self.manager._last_data_at = now
        self.manager.last_probe_at = 0.0
        self.assertFalse(self.manager._probe_due(now))
        self.assertTrue(self.manager._probe_due(now + ingest.PROBE_INTERVAL_SEC + 1))
        # 行情在流动时, 即使上次自检很久以前也不必再探
        self.manager._last_data_at = now
        self.manager.last_probe_at = now - 10 * ingest.PROBE_INTERVAL_SEC
        self.assertFalse(self.manager._probe_due(now + 1))

    def test_successful_probe_clears_the_error_and_marks_the_time(self):
        api = Mock()
        api.query_quotes.return_value = ["KQ.m@SHFE.fu"]
        self.assertTrue(self.manager._run_probe(api))
        self.assertIsNone(self.manager.probe_error)
        self.assertGreater(self.manager.last_probe_at, 0)
        self.assertIsNone(self.manager.status_snapshot()["busySec"])
        self.assertIsNotNone(self.manager.status_snapshot()["lastProbeSec"])

    def test_failed_probe_reports_the_reason_for_a_rebuild(self):
        api = Mock()
        api.query_quotes.side_effect = TqTimeoutError("获取合约信息超时")
        self.assertFalse(self.manager._run_probe(api))
        self.assertIn("TqTimeoutError", self.manager.probe_error)
        self.assertEqual(self.manager.status_snapshot()["probeError"], self.manager.probe_error)
        self.assertIsNone(self.manager.status_snapshot()["busySec"])

    def test_status_reports_quiet_time_for_diagnosis(self):
        self.manager._last_data_at = time.monotonic() - 123
        self.assertGreaterEqual(self.manager.status_snapshot()["quietSec"], 120)


if __name__ == "__main__":
    unittest.main()
