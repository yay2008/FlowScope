"""训练的因果性、成交、账务、断点恢复和输入边界；不连接行情服务。"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from training import Conflict, TrainingService
from training_data import CST, TrainingError, connected, normalize_dataset, parse_csv, timestamp


META = {"symbol": "SHFE.fu2505", "priceTick": 1, "multiplier": 10, "name": "燃油历史样本"}


def bars(count=100):
    start = datetime(2025, 1, 6, 9, tzinfo=CST)
    return [{"time": int((start + timedelta(minutes=i)).timestamp()), "open": 100,
             "high": 102, "low": 98, "close": 100, "volume": 1000} for i in range(count)]


class TrainingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = TrainingService(lambda: self.temp.name)
        self.choice = patch("training.random.SystemRandom.choice", lambda _, items: items[0])
        self.choice.start()
        self.addCleanup(self.choice.stop)
        self.seq = 0

    def create(self, data=None, **options):
        dataset = self.service.datasets.save(META, data or bars())
        self.state = self.service.create({"datasetId": dataset["id"], "warmup": 20, "steps": 30,
                                          "initialCash": 10000, "slippageTicks": 1,
                                          "openFee": 2, "closeFee": 2, **options})
        return self.state

    def command(self, action, **options):
        self.seq += 1
        self.state = self.service.command(self.state["id"], {"action": action, "revision": self.state["revision"],
                                                           "requestId": f"request-{self.seq}", **options})
        return self.state

    def test_order_waits_for_next_open_and_costs_are_accounted(self):
        data = bars()
        data[20].update(open=105, high=107, low=103, close=105)
        data[21].update(open=110, high=112, low=108, close=110)
        self.create(data)
        self.command("order", side="buy", qty=2)
        self.assertIsNone(self.state["position"])
        self.command("step")
        self.assertEqual(self.state["position"]["price"], 106)
        self.assertEqual(self.state["account"]["cash"], 9996)
        self.command("order", side="close")
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.assertEqual(self.state["trades"][0]["exitPrice"], 109)
        self.assertEqual(self.state["account"]["netPnl"], 52)

    def test_short_trade_and_percentage_fees(self):
        data = bars()
        data[21].update(open=90, high=92, low=88, close=90)
        self.create(data, feeMode="ratio", openFee=.001, closeFee=.001)
        self.command("order", side="sell", qty=1)
        self.command("step")
        self.command("order", side="close")
        self.command("step")
        trade = self.state["trades"][0]
        self.assertEqual(trade["price"], 99)
        self.assertEqual(trade["exitPrice"], 91)
        self.assertAlmostEqual(trade["netPnl"], 78.1)

    def test_both_protective_prices_in_bar_loss_first(self):
        data = bars()
        data[20].update(high=112, low=88)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90, tp=110)
        self.command("step")
        trade = self.state["trades"][0]
        self.assertEqual(trade["exitPrice"], 89)
        self.assertTrue(trade["ambiguous"])
        self.assertEqual(trade["reason"], "止损")

    def test_gap_stop_uses_open_not_stop_price(self):
        data = bars()
        data[21].update(open=80, high=82, low=78, close=80)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90, tp=110)
        self.command("step")
        self.command("step")
        self.assertEqual(self.state["trades"][0]["exitPrice"], 79)

    def test_gap_take_profit_precedes_later_bar_stop(self):
        data = bars()
        data[21].update(open=120, high=122, low=80, close=100)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90, tp=110)
        self.command("step")
        self.command("step")
        self.assertEqual(self.state["trades"][0]["exitPrice"], 119)
        self.assertFalse(self.state["trades"][0]["ambiguous"])

    def test_short_take_profit(self):
        data = bars()
        data[21].update(low=89, close=95)
        self.create(data)
        self.command("order", side="sell", qty=1, sl=110, tp=90)
        self.command("step")
        self.command("step")
        self.assertEqual(self.state["trades"][0]["exitPrice"], 91)

    def test_protective_change_only_affects_following_bars(self):
        data = bars()
        data[20].update(low=90, high=102)
        data[21].update(low=94)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=80)
        self.command("step")
        initial = self.state["position"]["initialRisk"]
        self.command("stops", sl=95, tp=110)
        self.assertIsNotNone(self.state["position"])
        self.assertEqual(self.state["position"]["initialRisk"], initial)
        self.command("step")
        self.assertEqual(self.state["trades"][0]["exitPrice"], 94)

    def test_opening_gap_invalidates_attached_stop(self):
        data = bars()
        data[20].update(open=80, high=82, low=78, close=80)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90)
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.assertEqual(self.state["account"]["fees"], 0)
        self.assertEqual(self.state["events"][-1]["kind"], "reject")

    def test_insufficient_margin_and_risk_reject_without_fees(self):
        self.create(initialCash=100)
        self.command("order", side="buy", qty=1)
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.assertEqual(self.state["account"]["cash"], 100)
        self.create(riskBudget=20)
        self.command("order", side="buy", qty=1, sl=90)
        self.command("step")
        self.assertIsNone(self.state["position"])

    def test_zero_volume_and_locked_limit_do_not_fill(self):
        data = bars()
        data[20]["volume"] = 0
        data[21].update(open=110, high=110, low=110, close=110, upper_limit=110)
        self.create(data)
        self.command("order", side="buy", qty=1)
        self.command("step")
        self.assertIsNotNone(self.state["pending"])
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.command("step")
        self.assertIsNotNone(self.state["position"])

    def test_cancel_has_no_fill(self):
        self.create()
        self.command("order", side="buy", qty=1)
        self.command("cancel")
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.assertEqual(self.state["account"]["netPnl"], 0)

    def test_triggered_protection_remains_pending_when_price_recovers(self):
        data = bars()
        data[21].update(open=80, high=80, low=80, close=80, volume=0)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90)
        self.command("step")
        self.command("step")
        self.assertIsNotNone(self.state["position"]["triggeredExit"])
        with self.assertRaises(TrainingError):
            self.command("stops", sl=70)
        self.command("step")
        self.assertIsNone(self.state["position"])
        self.assertEqual(self.state["trades"][0]["exitPrice"], 99)

    def test_review_restores_the_decision_and_pending_order(self):
        self.create()
        start = self.state["start"]
        self.command("order", side="buy", qty=1, sl=90, note="原始计划")
        self.command("step")
        self.command("stops", sl=95)
        self.command("finish")
        decision = self.service.view(self.state["id"], at=start)
        self.assertIsNone(decision["position"])
        self.assertEqual(decision["pending"]["note"], "原始计划")
        self.assertEqual(self.service.view(self.state["id"], at=start + 1)["position"]["sl"], 95)

    def test_intrabar_volume_is_shared_between_open_and_protection(self):
        data = bars()
        data[20].update(volume=1, high=112, low=88)
        self.create(data)
        self.command("order", side="buy", qty=1, sl=90, tp=110)
        self.command("step")
        self.assertIsNotNone(self.state["position"])
        self.assertEqual(self.state["position"]["triggeredExit"]["reason"], "止损")
        self.command("step")
        self.assertTrue(self.state["trades"][0]["ambiguous"])

    def test_no_future_data_and_no_skip_endpoint(self):
        data = bars()
        data[30].update(open=98765, high=98766, low=98764, close=98765)
        self.create(data)
        self.assertEqual(len(self.state["bars"]), 20)
        self.assertNotIn("98765", json.dumps(self.state))
        self.assertNotIn("2025-01-06", json.dumps(self.state))
        with self.assertRaises(TrainingError):
            self.service.view(self.state["id"], at=30)
        self.command("step")
        self.assertEqual(len(self.state["bars"]), 21)

    def test_future_change_does_not_change_visible_market_or_account(self):
        data = bars()
        one = self.create(data)
        data[-1].update(high=10000)
        two = self.create(data)
        for key in ("bars", "account", "position", "pending", "events", "equity"):
            self.assertEqual(one[key], two[key])

    def test_repeated_and_concurrent_commands(self):
        self.create()
        raw = {"action": "step", "revision": 0, "requestId": "same"}
        one = self.service.command(self.state["id"], raw)
        two = self.service.command(self.state["id"], raw)
        self.assertEqual(one, two)
        with self.assertRaises(Conflict):
            self.service.command(self.state["id"], {**raw, "requestId": "other"})
        with self.assertRaises(Conflict):
            self.service.command(self.state["id"], {**raw, "action": "finish"})

    def test_recreate_service_preserves_progress_account_and_protection(self):
        self.create()
        self.command("order", side="buy", qty=1, sl=90, tp=110)
        self.command("step")
        new = TrainingService(lambda: self.temp.name)
        self.assertEqual(new.view(self.state["id"]), self.state)

    def test_failed_save_does_not_commit_half_trade(self):
        self.create()
        self.command("order", side="buy", qty=1)
        before = copy.deepcopy(self.state)
        with patch("training.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.command("step")
        self.assertEqual(self.service.view(before["id"]), before)

    def test_finish_marks_open_position_without_using_future(self):
        self.create()
        self.command("order", side="buy", qty=1)
        self.command("step")
        self.command("finish")
        self.assertIsNotNone(self.state["position"])
        self.assertEqual(self.state["report"]["count"], 0)
        self.assertEqual(self.state["report"]["netPnl"], -12)
        later = self.service.view(self.state["id"], at=self.state["end"])
        self.assertEqual(later["report"]["netPnl"], -12)
        self.assertGreater(len(later["bars"]), len(self.state["bars"]))
        with self.assertRaises(TrainingError):
            self.command("step")

    def test_end_cancels_pending_without_phantom_fill(self):
        self.create(steps=10)
        for _ in range(9):
            self.command("step")
        self.command("order", side="buy", qty=1)
        self.command("step")
        self.assertEqual(self.state["status"], "completed")
        self.assertIsNone(self.state["pending"])
        self.assertIsNotNone(self.state["position"])

    def test_sample_does_not_cross_unknown_data_gap(self):
        data = bars()
        for row in data[35:]:
            row["time"] += 60
        self.create(data, steps=90)
        self.assertEqual(self.state["end"], 34)

    def test_input_validation_and_nonexistent_paths(self):
        self.create()
        for qty in (True, -1, .2, float("nan")):
            with self.assertRaises(TrainingError):
                self.command("order", side="buy", qty=qty)
        with self.assertRaises(TrainingError):
            self.service.view("../paper/account")
        with self.assertRaises(TrainingError):
            self.service.datasets.load("../paper/account")


class DatasetTest(unittest.TestCase):
    def test_timestamp_units_and_china_timezone(self):
        value = timestamp("2025-01-06 09:00:00")
        self.assertEqual(value, timestamp(str(value * 1000)))
        self.assertEqual(value, timestamp(str(value * 1000000000)))
        self.assertEqual(value, timestamp("2025-01-06T01:00:00Z"))

    def test_tq_csv_column_selection(self):
        source = "datetime_nano,SHFE.fu2505.open,SHFE.fu2505.high,SHFE.fu2505.low,SHFE.fu2505.close,SHFE.fu2505.volume\n1736125200000000000,100,102,98,101,12\n"
        result = parse_csv(source, "SHFE.fu2505")
        self.assertEqual(result[0]["open"], "100")
        with self.assertRaises(TrainingError):
            parse_csv(source, "SHFE.rb2505")

    def test_dataset_rejects_bad_values_and_continuous_contract(self):
        with self.assertRaises(TrainingError):
            normalize_dataset({**META, "symbol": "KQ.m@SHFE.fu"}, bars())
        for field, value in (("high", 90), ("volume", -1), ("close", float("inf")), ("time", "nope")):
            data = bars()
            data[1][field] = value
            with self.assertRaises(TrainingError):
                normalize_dataset(META, data)
        data = bars()
        data[1]["time"] = data[0]["time"]
        with self.assertRaises(TrainingError):
            normalize_dataset(META, data)

    def test_only_verified_breaks_can_be_skipped(self):
        meta = {**META, "tradingTime": {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"]]}}
        dataset = normalize_dataset(meta, bars())
        self.assertTrue(connected(timestamp("2025-01-06 10:14:00"), timestamp("2025-01-06 10:30:00"), dataset))
        self.assertFalse(connected(timestamp("2025-01-06 10:10:00"), timestamp("2025-01-06 10:30:00"), dataset))
        self.assertFalse(connected(timestamp("2025-01-06 10:14:00"), timestamp("2025-01-07 10:30:00"), dataset))


if __name__ == "__main__":
    unittest.main()
