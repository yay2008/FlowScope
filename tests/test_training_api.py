"""实际 ASGI 请求验证训练接口；不依赖 httpx 或启动真实行情。"""
import asyncio
import json
import tempfile
import time
import unittest

from fastapi import FastAPI

from training import TrainingService
from training_api import TrainingDownloads, make_training_router
from training_data import demo_dataset


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = TrainingService(lambda: self.temp.name)
        self.jobs = TrainingDownloads(self.service.datasets, lambda *_: demo_dataset())
        self.app = FastAPI()
        self.app.include_router(make_training_router(self.service, self.jobs))

    def request(self, method, path, body=None):
        async def run():
            messages = []
            path_only, _, query = path.partition("?")
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                     "scheme": "http", "path": "/api/training" + path_only, "raw_path": path_only.encode(),
                     "query_string": query.encode(), "root_path": "", "headers": [(b"content-type", b"application/json")],
                     "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}

            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode() if body is not None else b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await self.app(scope, receive, send)
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            return status, json.loads(content)
        return asyncio.run(run())

    def create(self):
        status, dataset = self.request("POST", "/datasets/demo", {})
        self.assertEqual(status, 200)
        status, session = self.request("POST", "/sessions", {"datasetId": dataset["id"], "requestId": "create-one"})
        self.assertEqual(status, 200)
        return session

    def test_indicator_config_matches_live_without_loading_market_history(self):
        from indicator import CFG
        status, config = self.request("GET", "/indicator-config")
        self.assertEqual(status, 200)
        self.assertEqual(config, CFG)
        self.assertEqual(self.service.datasets.list(), [])

    def test_full_http_cycle_and_version_conflict(self):
        session = self.create()
        identity = session["id"]
        payload = {"action": "order", "side": "buy", "qty": 1, "revision": 0, "requestId": "open"}
        status, pending = self.request("POST", f"/sessions/{identity}/commands", payload)
        self.assertEqual(status, 200)
        self.assertIsNone(pending["position"])
        status, advanced = self.request("POST", f"/sessions/{identity}/commands", {"action": "step", "revision": 1, "requestId": "next"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(advanced["position"])
        status, _ = self.request("POST", f"/sessions/{identity}/commands", {"action": "step", "revision": 1, "requestId": "stale"})
        self.assertEqual(status, 409)
        status, loaded = self.request("GET", f"/sessions/{identity}")
        self.assertEqual(loaded, advanced)
        status, final = self.request("POST", f"/sessions/{identity}/commands", {"action": "finish", "revision": 2, "requestId": "done"})
        self.assertEqual(final["status"], "completed")
        self.assertIsNotNone(final["report"])
        self.assertEqual(self.request("GET", "/sessions")[1][0]["id"], identity)

    def test_active_lookahead_and_full_dataset_are_inaccessible(self):
        session = self.create()
        self.assertEqual(self.request("GET", f"/sessions/{session['id']}?at={session['end']}")[0], 400)
        self.assertEqual(self.request("GET", "/datasets")[0], 200)
        self.assertNotIn("bars", self.request("GET", "/datasets")[1][0])

    def test_download_uses_background_loader_and_publishes_dataset(self):
        status, job = self.request("POST", "/downloads", {"symbol": "SHFE.fu2505"})
        self.assertEqual(status, 200)
        for _ in range(100):
            result = self.request("GET", f"/downloads/{job['id']}")[1]
            if result["status"] != "running":
                break
            time.sleep(.01)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["dataset"]["demo"])

    def test_import_rejects_missing_ohlc_and_bad_contract(self):
        self.assertEqual(self.request("POST", "/datasets/import", {"symbol": "../x", "csv": "time,buy,sell\n1,2,3"})[0], 400)
        self.assertEqual(self.request("POST", "/downloads", {"symbol": "KQ.m@SHFE.fu"})[0], 400)

    def test_csv_import_to_trainable_immutable_dataset(self):
        meta, rows = demo_dataset()
        csv = "time,open,high,low,close,volume\n" + "\n".join(
            ",".join(str(row[key]) for key in ("time", "open", "high", "low", "close", "volume")) for row in rows)
        payload = {**meta, "csv": csv}
        status, first = self.request("POST", "/datasets/import", payload)
        self.assertEqual(status, 200)
        status, second = self.request("POST", "/datasets/import", payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["count"], len(rows))
        status, created = self.request("POST", "/sessions", {"datasetId": first["id"], "requestId": "imported"})
        self.assertEqual(status, 200)
        self.assertEqual(created["status"], "active")


if __name__ == "__main__":
    unittest.main()
