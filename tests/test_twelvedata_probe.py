"""探针的延迟口径、密钥脱敏，以及本地 HTTP/WebSocket 的实际交互。"""
import asyncio
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import serve

from docs import twelvedata_probe as probe


def payload(start):
    return {"status": "ok", "meta": {"symbol": "XAU/USD", "interval": "1min"},
            "values": [{"datetime": datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "open": "2000", "high": "2002", "low": "1999", "close": "2001"}]}


class MeasurementTests(unittest.TestCase):
    def test_historical_and_open_bars_do_not_inflate_publication_delay(self):
        tracker = probe.CandleObservations()
        old, current = probe.parse_candles(payload(0)), probe.parse_candles(payload(120))
        self.assertEqual(tracker.observe(old + current, 130, 132), [])
        self.assertEqual(tracker.observe(old + current, 150, 152), [])
        observations = tracker.observe(current, 195, 197)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["observed_delay_s"], 17)
        self.assertEqual(observations[0]["poll_window_lower_s"], 0)
        changed = probe.parse_candles(payload(120))
        changed[0]["close"] = 2002
        self.assertEqual(tracker.observe(changed, 210, 212), [])
        self.assertEqual(tracker.revisions, 1)

    def test_poll_bracket_uses_last_successful_request_not_assumed_interval(self):
        tracker = probe.CandleObservations()
        old = probe.parse_candles(payload(60))
        tracker.observe(old, 130, 132)
        tracker.observe(old, 190, 192)  # 新 K线已收盘但还没发布。
        observation = tracker.observe(probe.parse_candles(payload(120)), 240, 244)[0]
        self.assertEqual(observation["poll_window_lower_s"], 10)
        self.assertEqual(observation["poll_window_upper_s"], 64)

    def test_wrong_interval_or_http_200_api_error_cannot_pass(self):
        wrong = payload(120)
        wrong["meta"]["interval"] = "1day"
        with self.assertRaises(ValueError):
            probe.parse_candles(wrong)
        with self.assertRaises(probe.ApiFailure) as caught:
            probe.parse_candles({"status": "error", "code": 403, "message": "Upgrade"})
        self.assertEqual(caught.exception.code, 403)
        wrong = payload(120)
        wrong["values"][0]["high"] = "1990"
        with self.assertRaises(ValueError):
            probe.parse_candles(wrong)

    def test_seconds_milliseconds_and_negative_latency_are_preserved(self):
        for timestamp, unit in [(1700000000, "seconds"), (1700000000000, "milliseconds")]:
            result = probe.price_sample({"event": "price", "symbol": "XAU/USD",
                                         "timestamp": timestamp, "price": 2000}, 1700000000.25)
            self.assertEqual(result["observed_delay_s"], .25)
            self.assertEqual(result["timestamp_unit"], unit)
        result = probe.price_sample({"event": "price", "symbol": "XAU/USD",
                                     "timestamp": 1700000001, "price": 2000}, 1700000000)
        self.assertEqual(result["observed_delay_s"], -1)

    def test_mixed_subscription_status_must_include_requested_symbol(self):
        event = {"status": "warning", "success": [{"symbol": "EUR/USD"}],
                 "fails": [{"symbol": "XAU/USD", "message": "Not allowed"}]}
        self.assertIs(probe.subscription_result(event), False)
        self.assertIsNone(probe.subscription_result({"status": "ok", "success": [{"symbol": "EUR/USD"}]}))

    def test_errors_and_reports_redact_raw_and_url_encoded_key(self):
        key = "fake+key/ secret"
        value = {"errors": [key, probe.quote(key, safe=""), probe.quote_plus(key)]}
        cleaned = probe.redact(value, key)
        self.assertEqual(cleaned["errors"], ["<REDACTED>"] * 3)


class ProbeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_local_probe(self, denied=False, no_prices=False, error_code=403,
                              max_requests=2, cancel=False):
        key = "test+secret/ only"
        requests = []
        actions = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                requests.append(query)
                body = ({"status": "error", "code": error_code, "message": f"Denied {key}"}
                        if denied else payload(int(time.time() // 60) * 60 - 60))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("api-credits-left", "7")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        async def ws_handler(socket):
            event = json.loads(await socket.recv())
            actions.append(event)
            if denied:
                await socket.send(json.dumps({"event": "subscribe-status", "status": "warning",
                    "success": [{"symbol": "EUR/USD"}], "fails": [{"symbol": "XAU/USD", "message": key}]}))
                await socket.wait_closed()
                return
            await socket.send(json.dumps({"event": "subscribe-status", "status": "ok",
                                          "success": [{"symbol": "XAU/USD"}]}))
            if not no_prices:
                await socket.send(json.dumps({"event": "price", "symbol": "XAU/USD",
                                              "timestamp": int(time.time()), "price": 2000.5}))
            async for message in socket:
                actions.append(json.loads(message))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            async with serve(ws_handler, "127.0.0.1", 0) as ws_server:
                port = ws_server.sockets[0].getsockname()[1]
                with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
                    config = probe.Config(duration=.25, poll_interval=.05, timeout=2,
                                          max_rest_requests=max_requests, no_proxy=True, output=Path(directory))
                    with patch.object(probe, "HEARTBEAT_SECONDS", .05):
                        task = asyncio.create_task(probe.run_probe(config, key,
                            rest_url=f"http://127.0.0.1:{server.server_port}/time_series",
                            ws_url=f"ws://127.0.0.1:{port}/quotes/price"))
                        if cancel:
                            asyncio.get_running_loop().call_later(.15, task.cancel)
                        report = await task
                    persisted = (Path(directory) / "report.json").read_text(encoding="utf-8")
                    events = (Path(directory) / "events.jsonl").read_text(encoding="utf-8")
                    self.assertEqual(json.loads(persisted), report)
                    self.assertNotIn(key, persisted + events + output.getvalue())
                    self.assertTrue(all(json.loads(line) for line in events.splitlines()))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(requests[0]["apikey"], [key])
        self.assertEqual(requests[0]["interval"], ["1min"])
        self.assertEqual(requests[0]["timezone"], ["UTC"])
        self.assertEqual(actions[0]["params"]["symbols"], "XAU/USD")
        return report, actions

    async def test_both_transports_receive_data_and_send_heartbeat(self):
        report, actions = await self.run_local_probe()
        self.assertEqual(report["rest"]["availability"], "available")
        self.assertEqual(report["rest"]["requests"], 2)
        self.assertEqual(report["rest"]["stop_reason"], "request_budget_reached")
        self.assertEqual(report["rest"]["close_to_first_observation_s"]["count"], 0)
        self.assertEqual(report["websocket"]["availability"], "available")
        self.assertEqual(report["websocket"]["price_events"], 1)
        self.assertIn({"action": "heartbeat"}, actions)

    async def test_auth_rejection_stops_without_retrying_or_leaking_key(self):
        report, _ = await self.run_local_probe(denied=True)
        self.assertEqual(report["rest"]["requests"], 1)
        self.assertEqual(report["rest"]["stop_reason"], "api_403")
        self.assertEqual(report["websocket"]["stop_reason"], "subscription_rejected")
        self.assertEqual(report["rest"]["availability"], "not_confirmed")
        self.assertEqual(report["websocket"]["availability"], "not_confirmed")

    async def test_subscription_ack_without_prices_does_not_pass_stream_test(self):
        report, _ = await self.run_local_probe(no_prices=True)
        self.assertIs(report["websocket"]["subscribed"], True)
        self.assertEqual(report["websocket"]["availability"], "not_confirmed")
        self.assertEqual(report["websocket"]["observed_delay_s"]["count"], 0)

    async def test_quota_error_is_preserved_when_request_budget_also_exhausted(self):
        report, _ = await self.run_local_probe(denied=True, error_code=429, max_requests=1)
        self.assertEqual(report["rest"]["requests"], 1)
        self.assertEqual(report["rest"]["stop_reason"], "api_429")

    async def test_cancellation_writes_partial_report(self):
        report, _ = await self.run_local_probe(cancel=True)
        self.assertIs(report["interrupted"], True)
        self.assertEqual(report["rest"]["availability"], "available")
        self.assertEqual(report["websocket"]["price_events"], 1)


if __name__ == "__main__":
    unittest.main()
