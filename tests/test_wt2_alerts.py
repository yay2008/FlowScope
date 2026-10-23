"""离线验证数值一致性、收盘边界、跨进程去重、通知恢复与后台订阅生命周期。"""
import json
import math
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ingest
from binance_feed import BinanceAdapter
from crypto_feed import CryptoManager
from wt2 import calculate_wt2, calculate_flowwave
from wt2_alerts import AlertService, AlertStore, SHIFT, TF, closed_futures, zone_of, crypto_live

ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "KQ.m@SHFE.rb"
START = 1_790_000_100


def bars(n, last=50):
    out = []
    for i in range(n):
        price = 100 + 8 * math.sin(i / 13) + math.cos(i / 3)
        out.append(dict(time=START + i * TF, open=price, high=price + 1, low=price - 1,
                        close=price, volume=100 + i % 17, signal=last if i == n - 1 else 50))
    return out


def payload(rows, contract="SHFE.rb2701", **kwargs):
    rows = [{**row, "flowWave": row.get("flowWave", shared(row["signal"]))} for row in rows]
    return dict(symbol=SYMBOL, contract=contract, name="螺纹钢", bars=rows, **kwargs)


def shared(value, reason=None):
    return {"version": 1, "wt2": value, "wave": None, "reason": reason}


def now_for(rows, offset=5):
    return rows[-1]["time"] + TF - SHIFT + offset


class NumericTests(unittest.TestCase):
    def test_current_javascript_matches_python_including_zero_volume_and_flat_prices(self):
        samples = [bars(800), bars(50)]
        for row in samples[1]:
            row.update(open=100, high=100, low=100, close=100, volume=0)
        script = "const I=require('./static/indicators.js');const d=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(d.map(b=>I.deriveLw(b,b.map(()=>0)).wt2)))"
        result = subprocess.run(["node", "-e", script], cwd=ROOT, input=json.dumps(samples),
                                text=True, capture_output=True, check=True)
        for sample, expected in zip(samples, json.loads(result.stdout)):
            actual = calculate_wt2(sample)
            self.assertEqual(len(actual), len(expected))
            for x, y in zip(actual, expected):
                if x is None or y is None:
                    self.assertEqual(x, y)
                else:
                    self.assertAlmostEqual(x, y, places=10)

    def test_missing_volume_masks_exact_dependency_window_and_backfill_restores_it(self):
        rows = bars(180)
        expected = calculate_wt2(rows)
        for bad in (None, float("nan"), -1):
            broken = [dict(b) for b in rows]
            broken[100]["volume"] = bad
            actual = calculate_wt2(broken)
            self.assertEqual(actual[100:108], [None] * 8)
            for x, y in zip(actual[108:], expected[108:]):
                self.assertAlmostEqual(x, y, places=10)
            broken[100]["volume"] = rows[100]["volume"]
            self.assertEqual(calculate_wt2(broken), expected)
        rows[100]["volume"] = 0
        self.assertIsNotNone(calculate_wt2(rows)[100])

    def test_offline_js_matches_shared_core_through_volume_price_and_time_gaps(self):
        samples = []
        for kind in ("volume", "partial", "price", "time"):
            rows = bars(180)
            if kind == "volume": rows[100]["volume"] = None
            if kind == "partial": rows[100]["volumeValid"] = False
            if kind == "price": rows[100]["priceValid"] = False
            if kind == "time": del rows[100]
            samples.append(rows)
        script = "const I=require('./static/indicators.js');const d=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(d.map(b=>I.deriveLw(b,b.map(()=>0),{tf:300,continuous:true}))))"
        result = subprocess.run(["node", "-e", script], cwd=ROOT, input=json.dumps(samples),
                                text=True, capture_output=True, check=True)
        for rows, expected in zip(samples, json.loads(result.stdout)):
            actual = calculate_flowwave(rows, tf=TF, continuous=True)
            for field in ("wt2", "wave"):
                for item, value in zip(actual, expected[field]):
                    if item[field] is None or value is None: self.assertEqual(item[field], value)
                    else: self.assertAlmostEqual(item[field], value, places=9)

    def test_time_gap_reseeds_crypto_but_keeps_futures_session_history(self):
        rows = bars(120)
        del rows[100]
        restarted = calculate_wt2(rows, tf=TF, continuous=True)
        self.assertEqual(restarted[100:107], [None] * 7)
        self.assertEqual(restarted[100:], calculate_wt2(rows[100:]))
        self.assertIsNotNone(calculate_wt2(rows, tf=TF)[100])

    def test_thresholds_are_strict_and_unrounded(self):
        self.assertEqual([zone_of(v) for v in [20, 80, 19.9999, 80.0001]],
                         ["neutral", "neutral", "oversold", "overbought"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "alerts.sqlite3")
        self.store = AlertStore(lambda: self.path)
        self.store.reconcile([SYMBOL])

    def process(self, rows, **kw):
        return self.store.process(payload(rows, **kw), now_for(rows))

    def events(self):
        return self.store.snapshot()["events"]

    def test_first_enable_in_extreme_is_only_a_baseline(self):
        self.process(bars(100, 90))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.store.snapshot()["states"][SYMBOL]["zone"], "overbought")

    def test_entry_hold_exit_reentry_and_restart(self):
        rows = bars(100, 78)
        self.process(rows)
        for value in (82, 87, 79, 83):
            row = bars(len(rows) + 1, value)[-1]
            rows.append(row)
            self.process(rows)
            self.process(rows)  # 重复快照
            self.store = AlertStore(lambda: self.path)  # 真正重新打开数据库
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(sorted(e["wt2"] for e in self.events()), [82, 83])

    def test_pending_survives_restart_sent_does_not_resend(self):
        self.process(bars(100))
        rows = bars(101, 19)
        self.process(rows)
        self.store = AlertStore(lambda: self.path)
        self.store.recover()
        batch = self.store.claim(now_for(rows))
        self.assertEqual(len(batch), 1)
        self.store.delivered(batch)
        self.store = AlertStore(lambda: self.path)
        self.store.recover()
        self.assertEqual(self.store.claim(now_for(rows)), [])

    def test_crash_during_os_delivery_is_marked_unknown_not_replayed(self):
        self.process(bars(100))
        rows = bars(101, 90)
        self.process(rows)
        self.assertEqual(len(self.store.claim(now_for(rows))), 1)
        self.store = AlertStore(lambda: self.path)
        self.store.recover()
        self.assertEqual(self.store.claim(now_for(rows)), [])
        self.assertEqual(self.events()[0]["delivery"], "unknown")

    def test_reconnect_records_old_signals_but_sends_only_fresh_entry(self):
        self.process(bars(100))
        rows = bars(105)
        rows[100]["signal"] = 90
        rows[104]["signal"] = 10
        self.process(rows)
        self.assertEqual(sorted(e["delivery"] for e in self.events()), ["expired", "pending"])
        batch = self.store.claim(now_for(rows, 100))
        self.assertEqual(batch, [])
        self.assertTrue(all(e["delivery"] == "expired" for e in self.events()))

    def test_disable_or_remove_cancels_pending_and_reenable_rebaselines(self):
        for mode in ("global", "symbol", "remove"):
            with self.subTest(mode=mode):
                self.store.reconcile([SYMBOL])
                self.store.configure(True)
                self.store.configure(True, SYMBOL)
                self.process(bars(100))
                self.process(bars(101, 90))
                if mode == "remove":
                    self.store.reconcile([])
                    self.store.reconcile([SYMBOL])
                else:
                    target = SYMBOL if mode == "symbol" else None
                    self.store.configure(False, target)
                    self.store.configure(True, target)
                self.process(bars(101, 90))
                self.assertEqual(self.store.claim(now_for(bars(101))), [])

    def test_rollover_resets_baseline(self):
        self.process(bars(100))
        self.process(bars(101, 90), contract="SHFE.rb2705")
        self.assertEqual(self.events(), [])

    def test_shared_gap_survives_restart_and_first_recovered_value_only_rebaselines(self):
        self.assertEqual(self.process(bars(8))["status"], "monitoring")
        rows = bars(9)
        rows[-1]["flowWave"] = shared(None, "volume")
        self.assertEqual(self.process(rows)["status"], "warming")
        self.store = AlertStore(lambda: self.path)
        rows.append(bars(10, 90)[-1])
        self.assertEqual(self.process(rows)["status"], "monitoring")
        self.assertEqual(self.events(), [])
        rows.extend([bars(11, 79)[-1], bars(12, 90)[-1]])
        self.process(rows)
        self.assertEqual(len(self.events()), 1)

    def test_backfill_of_same_bar_updates_baseline_without_replaying_notification(self):
        self.process(bars(8))
        rows = bars(9, 90)
        rows[-1]["flowWave"] = shared(None, "volume")
        self.process(rows)
        rows[-1]["flowWave"] = shared(90)
        self.assertEqual(self.process(rows)["status"], "monitoring")
        self.assertEqual(self.events(), [])
        self.process([*rows, bars(10, 91)[-1]])
        self.assertEqual(self.events(), [])

    def test_old_database_adds_gap_cursor_without_losing_state_or_settings(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE states")
            db.execute("CREATE TABLE states (symbol TEXT PRIMARY KEY, contract TEXT NOT NULL, bar INTEGER NOT NULL, zone TEXT NOT NULL, wt2 REAL NOT NULL, name TEXT NOT NULL)")
            db.execute("INSERT INTO states VALUES (?,?,?,?,?,?)", (SYMBOL, "SHFE.rb2701", bars(8)[-1]["time"], "neutral", 50, SYMBOL))
        db.close()
        self.process(bars(9, 90))
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.store.snapshot()["enabled"])

    def test_history_revision_cannot_realert_the_same_bar(self):
        self.process(bars(100))
        self.process(bars(101, 90))
        self.process(bars(101, 10))
        self.assertEqual(len(self.events()), 1)

    def test_two_consumers_claim_only_once(self):
        self.process(bars(100))
        rows = bars(101, 90)
        self.process(rows)
        other = AlertStore(lambda: self.path)
        claims = []
        threads = [threading.Thread(target=lambda store=s: claims.extend(store.claim(now_for(rows))))
                   for s in (self.store, other)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(claims), 1)


class ClosingTests(unittest.TestCase):
    def test_forming_bar_is_never_confirmed_by_the_clock_alone(self):
        rows = bars(102)
        closed = closed_futures(rows, now_for(rows, 600))
        self.assertEqual(closed, rows[:-1])

    def test_three_second_settle_and_session_end_including_cross_midnight(self):
        for stamp, end in [("2026-10-22T15:00:00", "15:00:00"), ("2026-10-23T02:30:00", "26:30:00")]:
            display_end = int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp())
            row = {**bars(1)[0], "time": display_end - TF}
            quote = datetime.fromtimestamp(display_end - 1, timezone.utc).isoformat()
            sessions = {"day": [["09:00:00", end]]}
            self.assertEqual(closed_futures([row], display_end - SHIFT + 2, quote, sessions), [])
            self.assertEqual(closed_futures([row], display_end - SHIFT + 3, quote, sessions), [row])
            self.assertEqual(closed_futures([row], display_end - SHIFT + 3, quote, sessions, healthy=False), [])
            stale = datetime.fromtimestamp(display_end - 86400, timezone.utc).isoformat()
            self.assertEqual(closed_futures([row], display_end - SHIFT + 3, stale, sessions), [])


class LifecycleTests(unittest.TestCase):
    def test_futures_background_keeps_rollup_and_base_without_any_clients(self):
        manager = ingest.FeedManager()
        self.assertEqual(manager.set_background([(SYMBOL, TF)]), {})
        future = time.monotonic() + ingest.IDLE_EVICT_SEC + 10
        manager._prune(future)
        self.assertEqual(set(manager.feeds), {(SYMBOL, TF), (SYMBOL, 30)})
        manager.set_background([])
        manager._prune(future + ingest.IDLE_EVICT_SEC)
        self.assertEqual(manager.feeds, {})

    def test_crypto_background_retains_rollup_without_clients(self):
        adapter = BinanceAdapter()
        manager = CryptoManager([adapter], defaults=())
        instrument = adapter.builtin_instruments()[0]
        manager.instruments[instrument.symbol] = instrument
        with patch.object(manager, "_ensure_loaded"):
            self.assertEqual(manager.set_background([(instrument.symbol, TF)]), {})
            later = time.monotonic() + 2000
            manager._sync(later)
            self.assertIn((instrument.symbol, TF), manager.rollups)
            manager.set_background([])
            manager._sync(later + 2000)
            self.assertNotIn((instrument.symbol, TF), manager.rollups)


class ServiceTests(unittest.TestCase):
    def test_crypto_uses_running_manager_and_only_its_trade_channel(self):
        adapter = BinanceAdapter()
        manager = CryptoManager([adapter], defaults=())
        instrument = adapter.builtin_instruments()[0]
        manager.instruments[instrument.symbol] = instrument
        manager.status = "running"
        for channel in manager.channels:
            channel.status = "connected" if channel.spec.topic("trade", instrument.inst_id) is not None else "error"
        self.assertTrue(crypto_live(manager, instrument.symbol))
        rows = bars(101)
        for row in rows: row["coverage"] = "complete"
        rows[-2]["coverage"] = "partial"
        rows = payload(rows)["bars"]
        rows[-2]["flowWave"] = shared(None, "volume")
        feed = SimpleNamespace(error=None, snapshot_for=lambda *a, **k: {"bars": rows})
        manager.rollups[(instrument.symbol, TF)] = feed
        service = AlertService(None, None, None, lambda: manager, None)
        service.desired = {instrument.symbol}
        service.on_crypto(manager)
        captured = service.pending[instrument.symbol]["bars"]
        self.assertEqual(len(captured), 100)
        self.assertIs(captured[-1], rows[-2])
        self.assertIsNone(captured[-1]["flowWave"]["wt2"])
        service.pending.clear()
        service._capture_at.clear()
        for channel in manager.channels: channel.status = "error"
        service.on_crypto(manager)
        self.assertEqual(service.pending, {})
        self.assertEqual(service.runtime[instrument.symbol]["status"], "unavailable")

    def test_headless_service_delivers_once_and_recovers_without_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AlertStore(lambda: str(Path(folder) / "alerts.sqlite3"))
            manager = ingest.FeedManager()
            manager.set_pinned([(SYMBOL, 30)])
            manager.status = "connected"
            crypto = CryptoManager([], defaults=())
            sent = []
            notifier = SimpleNamespace(prepare=lambda: None, send=lambda events, **kw: sent.extend(events))
            service = AlertService(store, lambda: [SYMBOL], lambda: manager, lambda: crypto, notifier)
            # 用真实时钟附近的收盘时间，通知线程会做过期检查。
            rows = bars(101)
            last = int((time.time() + SHIFT) // TF) * TF - TF
            for i, row in enumerate(rows): row["time"] = last - (100 - i) * TF
            with patch("wt2_alerts.time.time", return_value=now_for(rows)):
                service.start()
                try:
                    deadline = time.monotonic() + 5
                    while SYMBOL not in service.desired and time.monotonic() < deadline: time.sleep(.02)
                    service._offer(payload(rows[:-1], captured=time.time()))
                    while SYMBOL not in store.snapshot()["states"] and time.monotonic() < deadline: time.sleep(.02)
                    rows[-1]["signal"] = 90
                    service._offer(payload(rows, captured=time.time()))
                    while not sent and time.monotonic() < deadline: time.sleep(.02)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(manager.clients, {})
                    self.assertIn((SYMBOL, TF), manager.background)
                finally:
                    service.stop()
                self.assertEqual(store.snapshot()["events"][0]["delivery"], "sent")
                restarted = AlertService(AlertStore(store.path_provider), lambda: [SYMBOL], lambda: manager,
                                         lambda: crypto, notifier)
                restarted.start()
                try:
                    restarted.reconcile()
                    restarted._offer(payload(rows, captured=time.time()))
                    time.sleep(1.2)
                    self.assertEqual(len(sent), 1)
                finally:
                    restarted.stop()

    def test_api_validates_settings_and_queues_notification(self):
        import asyncio
        import app as server
        with tempfile.TemporaryDirectory() as folder:
            store = AlertStore(lambda: str(Path(folder) / "alerts.sqlite3"))
            manager = ingest.FeedManager()
            crypto = CryptoManager([], defaults=())
            service = AlertService(store, lambda: [SYMBOL], lambda: manager, lambda: crypto, None)
            store.reconcile([SYMBOL])

            async def request(path, value):
                output = []
                async def receive():
                    return {"type": "http.request", "body": json.dumps(value).encode(), "more_body": False}
                async def send(item): output.append(item)
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                         "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                         "root_path": "", "headers": [(b"content-type", b"application/json")],
                         "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
                await server.app(scope, receive, send)
                return next(m["status"] for m in output if m["type"] == "http.response.start")
            with patch.object(server, "alerts", service):
                self.assertEqual(asyncio.run(request("/api/alerts/settings", {"enabled": False, "symbol": SYMBOL})), 200)
                self.assertEqual(store.snapshot()["disabledSymbols"], [SYMBOL])
                self.assertEqual(asyncio.run(request("/api/alerts/settings", {"enabled": "false"})), 422)
                self.assertEqual(asyncio.run(request("/api/alerts/settings", {"enabled": True, "symbol": "no-such"})), 400)
                self.assertEqual(asyncio.run(request("/api/alerts/test", {})), 202)
                self.assertTrue(service.tests.get_nowait())


if __name__ == "__main__":
    unittest.main()
