"""历史档案的兼容性、筛选、文件边界与 HTTP 入口; 只用临时文件, 不连接行情或模型。"""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.responses import FileResponse

import app as server
from analysis_history import AnalysisHistory, InvalidRecord, record_id

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII=")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "analysis"
        self.store = AnalysisHistory(lambda: self.root)

    def save(self, filename="142043-KQ.m@SHFE.fu-1m+10s.json", day="20261012", **changes):
        folder = self.root / day
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(filename).stem
        record = {
            "createdAt": "2026-10-12T14:20:43+08:00", "symbol": "KQ.m@SHFE.fu", "label": "燃油2611",
            "model": "deepseek-flash", "reasoningEffort": "high", "status": "done", "finish": "stop",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300}, "elapsed": 12.3, "error": None,
            "settings": {"mode": "cvd"}, "paper": [],
            "charts": [{"tf": 60, "image": f"{stem}-1.png", "meta": {"view": "candle"}},
                       {"tf": 10, "image": f"{stem}-2.png", "meta": {"view": "footprint"}}],
            "system": "原系统提示词", "prompt": "原始数值表", "reasoning": "先看大周期", "answer": "【结论】\n- 震荡\n【风险】\n- 数据不足",
        }
        record.update(changes)
        path = folder / filename
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        for chart in record.get("charts", []):
            if isinstance(chart, dict) and chart.get("image") and "/" not in chart["image"] and "\\" not in chart["image"]:
                (folder / chart["image"]).write_bytes(PNG)
        return record_id(day, filename), path

    def test_old_format_read_only_and_images(self):
        identity, path = self.save()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        result = self.store.list()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["excerpt"], "- 震荡")
        self.assertEqual(result["items"][0]["periods"], [60, 10])
        self.assertNotIn("answer", result["items"][0])
        self.assertNotIn("prompt", result["items"][0])
        detail = self.store.detail(identity)
        self.assertEqual((detail["label"], detail["reasoning"], detail["model"]), ("燃油2611", "先看大周期", "deepseek-flash"))
        self.assertEqual(detail["charts"][1]["imageUrl"], f"/api/analysis/history/{identity}/images/1")
        image, mime = self.store.image(identity, 1)
        self.assertEqual((image.read_bytes(), mime), (PNG, "image/png"))
        self.assertNotIn("system", detail)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_empty_directory_does_not_create_storage(self):
        self.assertEqual(self.store.list()["items"], [])
        self.assertFalse(self.root.exists())

    def test_filters_dual_periods_search_and_beijing_dates(self):
        first, _ = self.save()
        self.save("160000-BINANCE.BTCUSDT.P-5m.json", "20261011", symbol="BINANCE.BTCUSDT.P", label="比特币永续",
                  createdAt="2026-10-11T16:00:00+00:00", status="stopped", charts=[{"tf": 300}])
        self.save("153012-OKX.BTC-USDT-SWAP-1m.json", "20261008", symbol="OKX.BTC-USDT-SWAP", label="比特币",
                  createdAt="2026-10-08T15:30:12+08:00", status="error", charts=[{"tf": 60}])
        for filters in ({"symbol": "KQ.m@SHFE.fu"}, {"q": "燃油"}, {"q": "kq.M@shfe.FU"}, {"tf": 10}):
            with self.subTest(filters=filters):
                self.assertEqual(self.store.list(**filters)["items"][0]["id"], first)
                self.assertEqual(self.store.list(**filters)["total"], 1)
        self.assertEqual(self.store.list(tf=60)["total"], 2)
        self.assertEqual(self.store.list(q="比特", status="error")["total"], 1)
        self.assertEqual(self.store.list(date_from="2026-10-12", date_to="2026-10-12")["total"], 2)
        self.assertEqual(self.store.list(q="不存在")["total"], 0)
        self.assertEqual(self.store.list()["periods"], [10, 60, 300])

    def test_pagination_stable_with_same_second_and_serial_suffix(self):
        identities = []
        for index in range(23):
            identity, _ = self.save(f"142043-KQ.m@SHFE.fu-1m+10s-{index + 1}.json")
            identities.append(identity)
        first = self.store.list()
        second = self.store.list(offset=20)
        self.assertEqual((len(first["items"]), first["hasMore"], first["total"]), (20, True, 23))
        self.assertEqual((len(second["items"]), second["hasMore"]), (3, False))
        actual = [item["id"] for item in first["items"] + second["items"]]
        self.assertEqual(actual, sorted(identities, reverse=True))

    def test_partial_error_stopped_and_truncated_records(self):
        for status, finish in (("stopped", None), ("error", None), ("done", "length")):
            identity, _ = self.save(f"142043-KQ.m@SHFE.fu-{status}.json", status=status, finish=finish,
                                    answer="半句", error="上游中断" if status == "error" else None)
            detail = self.store.detail(identity)
            self.assertEqual((detail["status"], detail["answer"], detail["finish"]), (status, "半句", finish or ""))

    def test_missing_optional_fields_and_filename_time_fallback(self):
        identity, path = self.save()
        path.write_text(json.dumps({"symbol": "KQ.m@SHFE.fu", "answer": "老记录"}), encoding="utf-8")
        detail = self.store.detail(identity)
        self.assertEqual(detail["createdAt"], "2026-10-12T14:20:43+08:00")
        self.assertEqual(detail["label"], "KQ.m@SHFE.fu")
        self.assertEqual(detail["charts"], [])
        self.assertEqual(detail["status"], "unknown")

    def test_corrupt_record_and_temporary_file_do_not_hide_good_records(self):
        self.save()
        identity, path = self.save("142044-KQ.m@SHFE.fu-1m.json")
        path.write_text("{incomplete", encoding="utf-8")
        path.with_suffix(".json.tmp").write_text("{", encoding="utf-8")
        data = self.store.list()
        self.assertEqual((data["total"], data["skipped"]), (1, 1))
        with self.assertRaises(InvalidRecord):
            self.store.detail(identity)

    def test_missing_image_keeps_answer_visible(self):
        identity, _ = self.save()
        image, _ = self.store.image(identity, 0)
        image.unlink()
        detail = self.store.detail(identity)
        self.assertIn("震荡", detail["answer"])
        self.assertIsNone(detail["charts"][0]["imageUrl"])
        self.assertIsNotNone(detail["charts"][1]["imageUrl"])
        for index in (-1, 0, 2):
            with self.assertRaises(FileNotFoundError):
                self.store.image(identity, index)

    def test_parameter_validation(self):
        for filters in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"tf": -1}, {"status": "other"},
                        {"q": "a" * 121}, {"date_from": "2026-13-01"},
                        {"date_from": "2026-10-13", "date_to": "2026-10-12"}):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.store.list(**filters)

    def test_invalid_ids_and_path_traversal(self):
        self.save()
        for raw in ("../outside.json", "20261012/../../outside.json", "20261012/142043-x.json:stream",
                    "20261012\\142043-x.json", "20261301/142043-x.json"):
            identity = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.store.detail(identity)
        for identity in ("../", "%2F", "a", "=" * 2):
            with self.assertRaises(ValueError):
                self.store.detail(identity)

    def test_image_paths_from_json_cannot_escape_date_folder(self):
        for name in ("../../secret.png", "..\\..\\secret.png", "C:\\secret.png", "/secret.png", "secret.svg"):
            identity, path = self.save(charts=[])
            value = json.loads(path.read_text(encoding="utf-8"))
            value["charts"] = [{"tf": 60, "image": name}]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(name=name):
                self.assertIsNone(self.store.detail(identity)["charts"][0]["imageUrl"])
                with self.assertRaises(FileNotFoundError):
                    self.store.image(identity, 0)

    def test_resolved_paths_block_links_outside_storage(self):
        identity, path = self.save()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        linked_folder = self.root / "20261008"
        try:
            linked_folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("本机未开启创建符号链接权限")
        (outside / path.name).write_bytes(path.read_bytes())
        self.assertEqual(self.store.list()["total"], 1)
        with self.assertRaises(FileNotFoundError):
            self.store.detail(record_id("20261008", path.name))
        image, _ = self.store.image(identity, 0)
        image.unlink()
        (outside / "secret.png").write_bytes(PNG)
        image.symlink_to(outside / "secret.png")
        with self.assertRaises(FileNotFoundError):
            self.store.image(identity, 0)

    def test_endpoints_work_during_analysis_and_without_model_settings(self):
        identity, _ = self.save()
        with patch.object(server, "analysis_history", self.store), patch.object(server.analysis, "settings", side_effect=AssertionError("不能调模型")):
            server.analysis._busy.acquire()
            try:
                response = server.analysis_history_list()
                self.assertEqual(json.loads(response.body)["total"], 1)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(json.loads(server.analysis_history_detail(identity).body)["id"], identity)
                self.assertIsInstance(server.analysis_history_image(identity, 0), FileResponse)
                self.assertTrue(server.analysis._busy.locked())
            finally:
                server.analysis._busy.release()

    def test_endpoint_errors(self):
        identity, path = self.save()
        with patch.object(server, "analysis_history", self.store):
            for call, expected in ((lambda: server.analysis_history_detail("bad!"), 400),
                                   (lambda: server.analysis_history_list(limit=500), 400),
                                   (lambda: server.analysis_history_image(identity, 5), 404)):
                with self.assertRaises(HTTPException) as caught:
                    call()
                self.assertEqual(caught.exception.status_code, expected)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(HTTPException) as caught:
                server.analysis_history_detail(identity)
            self.assertEqual(caught.exception.status_code, 422)
            path.unlink()
            with self.assertRaises(HTTPException) as caught:
                server.analysis_history_detail(identity)
            self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
