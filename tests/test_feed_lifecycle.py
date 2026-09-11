"""驱动实际采集循环验证重试和回收；SDK、时钟和历史目录均隔离。"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ingest
from test_data import ticks
from test_period import Clock, klines


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

    def test_full_pool_accepts_new_subscription_after_idle_eviction(self):
        for index in range(ingest.MAX_FEEDS):
            self.manager.ensure(f"SHFE.test{index}")
        with self.assertRaises(ValueError):
            self.manager.ensure("SHFE.overflow")
        self.clock.advance(ingest.IDLE_EVICT_SEC + 1)
        self.manager.ensure("SHFE.overflow")
        self.assertEqual(len(self.manager.feeds), 1)


if __name__ == "__main__":
    unittest.main()
