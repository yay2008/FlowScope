"""共享结果贯穿行情快照、修订消息、图表和警报；不同覆盖类型的恢复边界。"""
import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import ingest
from crypto_feed import CryptoRollupFeed
from wt2 import calculate_wt2
from wt2_alerts import AlertStore
from test_wt2_alerts import bars, now_for, ROOT, SYMBOL


class SharedFlowWaveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        paths = patch.object(ingest, "DATA_DIR", folder.name)
        paths.start()
        self.addCleanup(paths.stop)

    def test_aggregate_volume_hole_preserves_primary_price_state_and_recovers_in_eight_bars(self):
        rows = [{**b, "coverage": "complete"} for b in bars(180)]
        expected = calculate_wt2(rows)
        confirmed = {t: (1, 1) for t in range(rows[0]["time"], rows[-1]["time"] + 300, 30)}
        primary = SimpleNamespace(tf=30, latest_window=lambda _: None,
                                  _store=lambda _: SimpleNamespace(values=confirmed))
        feed = CryptoRollupFeed("AGG.ZEC", 300, lambda: None, lambda: [primary])
        rows[100]["coverage"] = "partial"
        output = feed.flowwave_records(rows)
        self.assertEqual([b["flowWave"]["wt2"] for b in output[100:108]], [None] * 8)
        self.assertEqual(output[100]["flowWave"]["reason"], "volume")
        for row, value in zip(output[108:], expected[108:]):
            self.assertAlmostEqual(row["flowWave"]["wt2"], value, places=10)
        # 主交易所也缺了价，才重置价格递归状态。
        del confirmed[rows[100]["time"]]
        self.assertEqual(feed.flowwave_records(rows)[100]["flowWave"]["reason"], "price")

    def test_futures_direction_coverage_does_not_invalidate_native_ohlcv(self):
        rows = [{**b, "coverage": "missing"} for b in bars(40)]
        feed = ingest.Feed(SYMBOL, 300)
        expected = calculate_wt2(rows)
        self.assertEqual([b["flowWave"]["wt2"] for b in feed.flowwave_records(rows)], expected)

    def test_snapshot_revision_is_immutable_and_republishes_dependent_values(self):
        feed = ingest.Feed(SYMBOL, 300)
        frame = pd.DataFrame(bars(150))
        with patch.object(feed, "_decorate_large"):
            first, _ = feed._snapshot_message(0, frame, {}, 1, 0)
            saved = json.dumps(first, sort_keys=True)
            broken = frame.copy()
            broken.loc[100, "volume"] = None
            second, message = feed._snapshot_message(0, broken, {0: first}, 2, 0)
            self.assertEqual(json.dumps(first, sort_keys=True), saved)
            changed = {b["time"] for b in message["bars"]}
            self.assertTrue(set(frame.time.iloc[100:108]) <= changed)
            self.assertIsNone(second["bars"][107]["flowWave"]["wt2"])
            restored, message = feed._snapshot_message(0, frame, {0: second}, 3, 0)
            self.assertEqual(restored["bars"], first["bars"])
            self.assertTrue(set(frame.time.iloc[100:108]) <= {b["time"] for b in message["bars"]})

    def test_sliding_window_keeps_values_and_pushes_only_new_bars(self):
        feed = ingest.Feed(SYMBOL, 300)
        frame = pd.DataFrame(bars(400))
        with patch.object(feed, "_decorate_large"), patch.object(ingest, "SNAPSHOT_BARS", 100):
            first, _ = feed._snapshot_message(0, frame.iloc[:300], {}, 1, 0)
            # 窗口里快照之前的各根用来预热: 快照第一根已经有值
            self.assertIsNotNone(first["bars"][0]["flowWave"]["wt2"])
            second, message = feed._snapshot_message(0, frame.iloc[1:301], {0: first}, 2, 0)
            self.assertEqual([b["time"] for b in message["bars"]], [int(frame.time.iloc[300])])
            self.assertTrue(all(a["flowWave"] is b["flowWave"] for a, b in zip(first["bars"][1:], second["bars"][:-1])))
            expected = calculate_wt2(bars(400)[:301])[-100:]
            self.assertEqual([b["flowWave"]["wt2"] for b in second["bars"]], expected)
            # 其它口径沿用 tick 口径这一轮的结果
            split, _ = feed._snapshot_message(5, frame.iloc[1:301], {}, 2, 0)
            self.assertTrue(all(a["flowWave"] is b["flowWave"] for a, b in zip(split["bars"], second["bars"])))

    def test_browser_and_alert_consume_exact_same_authoritative_values(self):
        feed = ingest.Feed(SYMBOL, 300)
        rows = feed.flowwave_records(bars(40))
        store = AlertStore(lambda: ingest.DATA_DIR + "/alerts.sqlite3")
        store.reconcile([SYMBOL])
        result = store.process(dict(symbol=SYMBOL, contract=SYMBOL, bars=rows), now_for(rows))
        self.assertEqual(result["status"], "monitoring")
        self.assertEqual(result["wt2"], rows[-1]["flowWave"]["wt2"])
        # 人为改变前端价格；若重新计算而非读取权威结果，这里必然不同。
        for row in rows:
            row.update(open=1, high=1, low=1, close=1, volume=0)
        script = "const I=require('./static/indicators.js');const b=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(I.deriveLw(b,b.map(()=>0)).wt2));"
        run = subprocess.run(["node", "-e", script], cwd=ROOT, input=json.dumps(rows),
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), [b["flowWave"]["wt2"] for b in rows])

    def test_volume_split_uses_same_ohlcv_reference(self):
        rows = [{**b, "coverage": "complete"} for b in bars(40)]
        feed = ingest.Feed("BINANCE.ZECUSDT.P", 300)
        tick = feed.flowwave_records(rows)
        split = [{**b, "coverage": "missing"} for b in rows]
        result = feed.flowwave_records(split, reference=tick)
        self.assertEqual([b["flowWave"] for b in result], [b["flowWave"] for b in tick])
