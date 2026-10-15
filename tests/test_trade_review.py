"""模拟交易 AI 日复盘：日期和现金流口径、服务端快照、流式档案。全程离线。"""
import asyncio
import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

import app as server
from analysis import CST, UpstreamError
from paper import FeeTable, PaperService, PaperStore
from paper_crypto import CryptoPaperService
from trade_review import MAX_PROMPT_BYTES, ReviewHistory, TradeReviewService, build_snapshot, prompt_text


NOW = datetime(2026, 10, 14, 21, 30, tzinfo=CST)
SETTINGS = {"api_key": "test-only", "model": "offline-review", "base_url": "", "reasoning_effort": "high"}


def trade(number, at, *, contract="SHFE.fu2611", pnl=0, fee=0, close=0, opening=1,
          trading_day="2026-10-14", side="buy", qty=1):
    result = {"id": f"T{number}", "orderId": f"O{number}", "contract": contract,
              "side": side, "qty": qty, "price": 3000, "open": opening, "close": close,
              "pnl": pnl, "fee": fee, "position": 0 if close else qty, "at": at}
    if trading_day is not None:
        result["tradingDay"] = trading_day
    return result


def book(trades=(), fundings=()):
    return {"version": 1, "initialCash": 100000, "cash": 100000, "realizedPnl": 0,
            "fees": 0, "funding": 0, "positions": {}, "orders": [],
            "trades": list(trades), "fundings": list(fundings), "seq": 100}


def chunk(content="复盘完成"):
    yield {"choices": [{"delta": {"reasoning_content": "核对成交"}, "finish_reason": None}]}
    yield {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}


def parse_event(text):
    head, data = text.strip().split("\n", 1)
    return head.removeprefix("event: "), json.loads(data.removeprefix("data: "))


def payload(result):
    return json.loads(result.body) if isinstance(result, JSONResponse) else result


def collect(service, request):
    async def run():
        job = service.start(request, asyncio.get_running_loop())
        return [parse_event(text) async for text in service.events(job) if not text.startswith(":")]
    return asyncio.run(run())


class SnapshotTest(unittest.TestCase):
    def test_futures_natural_day_excludes_previous_night_and_includes_same_night(self):
        fills = [trade(1, "2026-10-13 21:01:00", trading_day="2026-10-14")]
        fills += [trade(i, f"2026-10-14 10:{i - 2:02d}:00") for i in range(2, 62)]
        fills += [trade(62, "2026-10-14 21:01:00", trading_day="2026-10-15")]
        source = book(fills)
        before = copy.deepcopy(source)
        result = build_snapshot("futures", "2026-10-14", source, NOW)
        self.assertEqual(result["date"], "2026-10-14")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["summary"]["tradeCount"], 61)
        self.assertEqual(len(result["trades"]), 61, "不能沿用 /api/paper 最近 50 笔的限制")
        self.assertEqual(result["summary"]["firstAt"], "2026-10-14 10:00:00")
        self.assertEqual(result["summary"]["lastAt"], "2026-10-14 21:01:00")
        self.assertNotIn("T1", {item["id"] for item in result["trades"]})
        self.assertIn("T62", {item["id"] for item in result["trades"]})
        self.assertEqual(result["dateBasis"], "北京时间自然日")
        self.assertEqual(source, before, "构造复盘快照不能修改模拟账户")

    def test_default_date_uses_beijing_today_even_at_night_or_on_weekend(self):
        for account in ("futures", "crypto"):
            for moment in (NOW, datetime(2026, 10, 17, 21, 30, tzinfo=CST)):
                with self.subTest(account=account, moment=moment):
                    result = build_snapshot(account, "", book(), moment)
                    self.assertEqual((result["date"], result["defaultDate"]),
                                     (moment.date().isoformat(), moment.date().isoformat()))

    def test_natural_day_midnight_boundaries_and_cash_flow(self):
        fills = [trade(1, "2026-10-13 23:59:59.999999", pnl=1000, fee=9, close=1),
                 trade(2, "2026-10-14 00:00:00", pnl=4, fee=1, close=1),
                 trade(3, "2026-10-14 23:59:59.999999", pnl=-2, fee=1, close=1,
                       trading_day="2026-10-15"),
                 trade(4, "2026-10-15 00:00:00", pnl=1000, fee=9, close=1)]
        for account in ("futures", "crypto"):
            with self.subTest(account=account):
                result = build_snapshot(account, "2026-10-14", book(fills), NOW)
                self.assertEqual([item["id"] for item in result["trades"]], ["T2", "T3"])
                self.assertEqual(result["summary"]["tradeCount"], 2)
                self.assertEqual((result["summary"]["realizedPnl"], result["summary"]["fees"],
                                  result["summary"]["netPnl"]), (2, 2, 0))
                self.assertEqual(result["range"], {"from": "2026-10-14T00:00:00+08:00",
                                                   "to": "2026-10-15T00:00:00+08:00"})

    def test_date_selection_uses_beijing_time_before_filtering(self):
        fills = [trade(1, "2026-10-13T15:59:59.999999+00:00"),
                 trade(2, "2026-10-13T16:00:00+00:00"),
                 trade(3, "2026-10-14T15:59:59.999999+00:00"),
                 trade(4, "2026-10-14T16:00:00+00:00")]
        for account in ("futures", "crypto"):
            with self.subTest(account=account):
                result = build_snapshot(account, "2026-10-14", book(fills), NOW)
                self.assertEqual([item["id"] for item in result["trades"]], ["T2", "T3"])
                result = build_snapshot(account, "", book(), datetime(2026, 10, 13, 16, tzinfo=timezone.utc))
                self.assertEqual(result["defaultDate"], "2026-10-14")

    def test_trading_day_metadata_does_not_control_natural_day_selection(self):
        fills = [trade(1, "2026-10-13 21:01:00", trading_day="2026-10-14"),
                 trade(2, "2026-10-14 21:01:00", trading_day="2026-10-15"),
                 trade(3, "2026-10-14 22:01:00", trading_day=None),
                 trade(4, "2026-10-14 23:01:00", trading_day="not-a-date")]
        result = build_snapshot("futures", "2026-10-14", book(fills), NOW)
        self.assertEqual([item["id"] for item in result["trades"]], ["T2", "T3", "T4"])
        self.assertFalse(any("交易日规则" in item for item in result["warnings"]))
        self.assertNotIn("tradingDay", result["trades"][0])

    def test_funding_uses_the_same_exclusive_midnight_boundary(self):
        funding = [{"contract": "BINANCE.BTCUSDT.P", "at": at, "amount": amount}
                   for at, amount in (("2026-10-13T15:59:59+00:00", 1000),
                                      ("2026-10-13T16:00:00+00:00", -2),
                                      ("2026-10-14 23:59:59.999999", 3),
                                      ("2026-10-14T16:00:00+00:00", 1000))]
        result = build_snapshot("crypto", "2026-10-14", book(fundings=funding), NOW)
        self.assertEqual(len(result["fundings"]), 2)
        self.assertEqual((result["summary"]["funding"], result["summary"]["netPnl"]), (1, 1))

    def test_crypto_uses_beijing_calendar_and_funding(self):
        fills = [trade(1, "2026-10-14 00:01:00", contract="BINANCE.BTCUSDT.P",
                       pnl=5, fee=1, close=1, opening=0, trading_day=None),
                 trade(2, "2026-10-15 00:01:00", contract="BINANCE.BTCUSDT.P",
                       pnl=100, fee=0, close=1, opening=0, trading_day=None)]
        funding = [{"contract": "BINANCE.BTCUSDT.P", "at": "2026-10-14 08:00:00",
                    "rate": 0.001, "markPrice": 3000, "qty": 1, "amount": -2},
                   {"contract": "BINANCE.BTCUSDT.P", "at": "2026-10-15 08:00:00",
                    "rate": 0.001, "markPrice": 3000, "qty": 1, "amount": -50}]
        result = build_snapshot("crypto", "2026-10-14", book(fills, funding), NOW)
        self.assertEqual((result["currency"], result["summary"]["tradeCount"]), ("USDT", 1))
        self.assertEqual(len(result["fundings"]), 1)
        self.assertEqual((result["summary"]["realizedPnl"], result["summary"]["fees"],
                          result["summary"]["funding"], result["summary"]["netPnl"]), (5, 1, -2, 2))
        self.assertEqual(result["defaultDate"], "2026-10-14")

    def test_reversal_counts_one_close_and_wins_use_gross_pnl(self):
        fills = [trade(1, "2026-10-14 09:00:00", pnl=1, fee=3, close=1, opening=2),
                 trade(2, "2026-10-14 09:10:00", pnl=-2, fee=1, close=2, opening=0),
                 trade(3, "2026-10-14 09:20:00", pnl=0, fee=1, close=0, opening=1)]
        summary = build_snapshot("futures", "2026-10-14", book(fills), NOW)["summary"]
        self.assertEqual((summary["tradeCount"], summary["closeCount"]), (3, 2))
        self.assertEqual((summary["wins"], summary["losses"], summary["breakeven"]), (1, 1, 0))
        self.assertEqual(summary["winRate"], 50)
        self.assertEqual(summary["netPnl"], -6)

    def test_losing_streak_and_realized_cash_flow_drawdown(self):
        fills = [trade(1, "2026-10-14 09:00:00", pnl=8, fee=1, close=1, opening=0),
                 trade(2, "2026-10-14 09:01:00", pnl=-3, fee=1, close=1, opening=0),
                 trade(3, "2026-10-14 09:02:00", pnl=-2, fee=1, close=1, opening=0),
                 trade(4, "2026-10-14 09:03:00", pnl=5, fee=1, close=1, opening=0)]
        summary = build_snapshot("futures", "2026-10-14", book(fills), NOW)["summary"]
        self.assertEqual(summary["maxLosingStreak"], 2)
        self.assertEqual(summary["maxDrawdown"], 7)  # 累计净额 7→3→0，不是账户权益回撤
        self.assertEqual(summary["netPnl"], 4)

    def test_invalid_date_is_rejected(self):
        for day in ("2026-02-30", "2026/10/14", "../2026-10-14", "2026-10-14x", "9999-12-31"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                build_snapshot("futures", day, book(), NOW)

    def test_raw_trade_amounts_prevent_small_fees_from_disappearing(self):
        fills = [trade(i, "2026-10-14 10:00:00", fee=0) |
                 {"pnlRaw": 0.0, "feeRaw": 0.0049} for i in range(100)]
        result = build_snapshot("futures", "2026-10-14", book(fills), NOW)
        self.assertEqual((result["summary"]["fees"], result["summary"]["netPnl"]), (0.49, -0.49))
        self.assertEqual(len(result["trades"]), 100)
        self.assertFalse(any("舍入" in warning for warning in result["warnings"]))

    def test_raw_realized_pnl_is_aggregated_before_rounding(self):
        fills = [trade(i, "2026-10-14 10:00:00", pnl=0, fee=0, close=1, opening=0) |
                 {"pnlRaw": 0.0049, "feeRaw": 0.0} for i in range(100)]
        summary = build_snapshot("futures", "2026-10-14", book(fills), NOW)["summary"]
        self.assertEqual((summary["realizedPnl"], summary["netPnl"]), (0.49, 0.49))

    def test_raw_funding_amounts_are_aggregated_before_rounding(self):
        fundings = [{"contract": "BINANCE.BTCUSDT.P", "at": "2026-10-14 08:00:00",
                     "rate": 0.0001, "markPrice": 100, "qty": 1,
                     "amount": 0, "amountRaw": 0.0000049} for _ in range(100)]
        result = build_snapshot("crypto", "2026-10-14", book(fundings=fundings), NOW)
        self.assertEqual(result["summary"]["funding"], 0.00049)
        self.assertEqual(result["summary"]["netPnl"], 0.00049)

    def test_legacy_rounded_records_warn_about_precision(self):
        result = build_snapshot("futures", "2026-10-14",
                                book([trade(1, "2026-10-14 10:00:00", pnl=1.23, fee=0.01)]), NOW)
        self.assertTrue(any("舍入" in warning or "精度" in warning for warning in result["warnings"]))

    def test_invalid_money_values_are_skipped_with_warning(self):
        valid = trade(1, "2026-10-14 10:00:00", pnl=2, fee=1)
        invalid = trade(2, "2026-10-14 10:01:00", pnl=float("nan"), fee=1)
        result = build_snapshot("futures", "2026-10-14", book([valid, invalid]), NOW)
        self.assertEqual(result["summary"]["tradeCount"], 1)
        self.assertEqual(result["summary"]["netPnl"], 1)
        self.assertTrue(any("数值" in warning for warning in result["warnings"]))

        bad_funding = {"contract": "BINANCE.BTCUSDT.P", "at": "2026-10-14 08:00:00",
                       "rate": 0.001, "markPrice": 100, "qty": 1, "amount": float("inf")}
        crypto = build_snapshot("crypto", "2026-10-14", book(fundings=[bad_funding]), NOW)
        self.assertEqual((crypto["summary"]["funding"], crypto["fundings"]), (0, []))
        self.assertTrue(any("数值" in warning for warning in crypto["warnings"]))

    def test_retention_limit_warns_that_older_results_may_be_missing(self):
        fills = [trade(i, "2026-10-14 10:00:00") for i in range(5000)]
        result = build_snapshot("futures", "2026-10-14", book(fills), NOW)
        self.assertEqual(result["summary"]["tradeCount"], 5000)
        self.assertTrue(any("裁剪" in message for message in result["warnings"]))

    def test_prompt_samples_details_but_keeps_full_day_metrics_and_extremes(self):
        fills = [trade(i, "2026-10-14 10:00:00", fee=1) for i in range(600)]
        fills[257] = trade(257, "2026-10-14 10:00:00", pnl=500, fee=1, close=1, opening=0)
        fills[359] = trade(359, "2026-10-14 10:00:00", pnl=-500, fee=1, close=1, opening=0)
        snapshot = build_snapshot("futures", "2026-10-14", book(fills), NOW)
        detail = json.loads(prompt_text(snapshot).split("\n", 2)[2])
        self.assertEqual(snapshot["summary"]["tradeCount"], 600)
        self.assertEqual(snapshot["summary"]["netPnl"], -600)
        self.assertEqual(detail["summary"], snapshot["summary"])
        self.assertEqual(detail["detailCoverage"]["retainedTradeCount"], 600)
        self.assertLessEqual(detail["detailCoverage"]["sentTradeCount"], 500)
        self.assertEqual({"T257", "T359"} & {item["id"] for item in detail["trades"]},
                         {"T257", "T359"})
        self.assertTrue(any("抽取" in message for message in snapshot["warnings"]))

    def test_prompt_byte_budget_with_long_reasons_and_funding(self):
        contract = "BINANCE." + "超长合约" * 25
        fills = [trade(i, "2026-10-14 10:00:00", contract=contract, fee=1,
                       trading_day=None) for i in range(1200)]
        fills[257].update(pnl=500, close=1, open=0)
        fills[359].update(pnl=-500, close=1, open=0)
        fundings = [{"contract": contract, "at": "2026-10-14 08:00:00", "rate": 0.001,
                     "markPrice": 3000, "qty": 1, "amount": -0.01} for _ in range(500)]
        source = book(fills, fundings)
        source["orders"] = [{"id": f"O{i}", "type": "limit", "reason": "执行偏差" * 60}
                            for i in range(1200)]
        snapshot = build_snapshot("crypto", "2026-10-14", source, NOW)
        prompt = prompt_text(snapshot)
        detail = json.loads(prompt.split("\n", 2)[2])
        self.assertLessEqual(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)
        self.assertEqual(detail["summary"], snapshot["summary"])
        self.assertEqual(detail["detailCoverage"]["retainedTradeCount"], 1200)
        self.assertEqual(detail["detailCoverage"]["retainedFundingCount"], 500)
        self.assertEqual(detail["detailCoverage"]["sentTradeCount"], len(detail["trades"]))
        self.assertEqual(detail["detailCoverage"]["sentFundingCount"], len(detail["fundings"]))
        self.assertLess(len(detail["trades"]) + len(detail["fundings"]), 1700)
        self.assertEqual({"T257", "T359"} & {item["id"] for item in detail["trades"]},
                         {"T257", "T359"})


class SourceSnapshotTest(unittest.TestCase):
    def test_small_fees_match_actual_book_cash_after_many_fills(self):
        from paper import FeeTable, PaperBook
        from tests.test_paper import NOW as fill_now, order, snap

        fees = FeeTable({("SHFE", "fu"): {"mode": "fixed", "open": 0.0049,
                                           "closeYesterday": 0.0049, "closeToday": 0.0049}})
        ledger = PaperBook(fees=fees)
        for _ in range(100):
            ledger.place(order("buy", 1), snap(), fill_now)
        self.assertTrue(all(t["fee"] == 0 and t["feeRaw"] == 0.0049 for t in ledger.trades))
        result = build_snapshot("futures", "2026-09-30", ledger.to_dict(), fill_now)
        self.assertEqual(result["summary"]["fees"], 0.49)
        self.assertAlmostEqual(ledger.initial_cash - ledger.cash, 0.49, places=6)

    def test_account_services_copy_without_market_access_or_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("futures", "crypto"):
                with self.subTest(kind=kind):
                    path = Path(directory) / f"{kind}.json"
                    state = book([trade(1, "2026-10-14 10:00:00")])
                    path.write_text(json.dumps(state), encoding="utf-8")
                    original = path.read_bytes()

                    def forbidden_manager():
                        raise AssertionError("读取复盘快照不应访问行情")

                    store = PaperStore(lambda: str(path))
                    service = (PaperService(forbidden_manager, store, FeeTable()) if kind == "futures"
                               else CryptoPaperService(forbidden_manager, store))
                    with patch.object(store, "save", side_effect=AssertionError("读取快照不应落盘")):
                        first = service.review_snapshot()
                        first["trades"][0]["pnl"] = 999
                        second = service.review_snapshot()
                    self.assertEqual(second["trades"][0]["pnl"], 0)
                    self.assertEqual(path.read_bytes(), original)

                    service.save_error = "模拟账户保存失败: 磁盘已满"
                    failed = service.review_snapshot()
                    self.assertIn("磁盘已满", failed["saveError"])
                    review = build_snapshot(kind, "2026-10-14", failed, NOW)
                    self.assertTrue(any("磁盘已满" in warning for warning in review["warnings"]))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "reviews"
        self.actual = book([trade(1, "2026-10-14 10:00:00", pnl=12, fee=2,
                                  close=1, opening=0)])
        self.calls = []

    def service(self, transport=None):
        def provider(account):
            self.calls.append(account)
            return self.actual

        return TradeReviewService(provider, lambda: self.root,
                                  transport=transport or (lambda settings, payload, cancelled: chunk()),
                                  settings=lambda: SETTINGS, clock=lambda: NOW)

    def test_empty_day_does_not_start_transport_or_create_record(self):
        called = []

        def transport(settings, payload, cancelled):
            called.append(payload)
            yield from chunk()

        service = self.service(transport)
        with self.assertRaises(ValueError):
            collect(service, {"account": "futures", "date": "2026-10-13"})
        self.assertEqual(called, [])
        self.assertFalse(self.root.exists())

    def test_model_without_answer_is_error_and_does_not_save_empty_report(self):
        def reasoning_only(settings, payload, cancelled):
            yield {"choices": [{"delta": {"reasoning_content": "核对账本"}, "finish_reason": "stop"}]}

        events = collect(self.service(reasoning_only), {"account": "futures", "date": "2026-10-14"})
        self.assertEqual(events[-1][0], "error")
        self.assertIn("没有返回", events[-1][1]["message"])
        self.assertIsNone(events[-1][1]["saved"])
        self.assertFalse(self.root.exists())

    def test_stream_saves_trusted_snapshot_and_history(self):
        sent = []

        def transport(settings, payload, cancelled):
            sent.append(payload)
            yield from chunk("减少追单，核对止损执行。")

        service = self.service(transport)
        events = collect(service, {"account": "futures", "date": "2026-10-14",
                                   "trades": [trade(999, "2026-10-14 12:00:00", pnl=999999)],
                                   "summary": {"netPnl": 999999}})
        self.assertIn("delta", [kind for kind, _ in events])
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(self.calls, ["futures"])
        self.assertEqual(len(sent), 1)
        prompt = json.dumps(sent[0]["messages"], ensure_ascii=False)
        self.assertIn("12", prompt)
        self.assertNotIn("999999", prompt, "模型输入必须来自服务端账本")

        history = ReviewHistory(lambda: self.root)
        listing = history.list(symbol="futures")
        self.assertEqual(listing["total"], 1)
        item = listing["items"][0]
        self.assertEqual((item["account"], item["reviewDate"]), ("futures", "2026-10-14"))
        detail = history.detail(item["id"])
        self.assertEqual(detail["snapshot"]["summary"]["netPnl"], 10)
        self.assertEqual(detail["answer"], "减少追单，核对止损执行。")

    def test_snapshot_is_read_only(self):
        service = self.service()
        before = copy.deepcopy(self.actual)
        result = service.snapshot("futures", "2026-10-14")
        self.assertEqual(result["summary"]["netPnl"], 10)
        self.assertEqual(self.actual, before)

    def test_model_and_saved_report_only_receive_selected_natural_day(self):
        self.actual = book([trade(1, "2026-10-13 21:00:00", pnl=1000, close=1),
                            trade(2, "2026-10-14 09:00:00", pnl=5, fee=1, close=1),
                            trade(3, "2026-10-14 21:00:00", pnl=7, fee=1, close=1,
                                  trading_day="2026-10-15"),
                            trade(4, "2026-10-15 00:00:00", pnl=1000, close=1)])
        sent = []

        def transport(settings, payload, cancelled):
            sent.append(payload)
            yield from chunk()

        events = collect(self.service(transport), {"account": "futures", "date": "2026-10-14"})
        system, user = sent[0]["messages"]
        self.assertIn("北京时间自然日", system["content"])
        self.assertNotIn("包含前一晚夜盘", system["content"])
        model_snapshot = json.loads(user["content"].split("\n", 2)[2])
        self.assertEqual([item["id"] for item in model_snapshot["trades"]], ["T2", "T3"])
        self.assertEqual(model_snapshot["summary"]["netPnl"], 10)
        self.assertEqual(model_snapshot["range"]["to"], "2026-10-15T00:00:00+08:00")
        self.assertEqual(events[-1][0], "done")
        history = ReviewHistory(lambda: self.root)
        detail = history.detail(history.list(symbol="futures")["items"][0]["id"])
        self.assertEqual([item["id"] for item in detail["snapshot"]["trades"]], ["T2", "T3"])
        self.assertEqual(detail["snapshot"]["dateBasis"], "北京时间自然日")

    def test_upstream_error_after_partial_output_is_saved(self):
        def failing(settings, payload, cancelled):
            yield {"choices": [{"delta": {"content": "先核对手续费"}, "finish_reason": None}]}
            raise UpstreamError("上游中断")

        events = collect(self.service(failing), {"account": "futures", "date": "2026-10-14"})
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["status"], "error")
        self.assertIsNotNone(events[-1][1]["saved"])
        listing = ReviewHistory(lambda: self.root).list(symbol="futures")
        detail = ReviewHistory(lambda: self.root).detail(listing["items"][0]["id"])
        self.assertEqual((detail["status"], detail["answer"]), ("error", "先核对手续费"))

    def test_disconnect_saves_partial_review(self):
        gate = threading.Event()

        def endless(settings, payload, cancelled):
            yield {"choices": [{"delta": {"content": "先看两笔平仓"}, "finish_reason": None}]}
            gate.wait(5)
            for _ in range(100):
                yield {"choices": [{"delta": {"content": "继续"}, "finish_reason": None}]}

        service = self.service(endless)

        async def run():
            job = service.start({"account": "futures", "date": "2026-10-14"}, asyncio.get_running_loop())
            stream = service.events(job)
            async for text in stream:
                if text.startswith("event: delta"):
                    break
            await stream.aclose()
            gate.set()

        asyncio.run(run())
        self.assertTrue(service._busy.acquire(timeout=5), "复盘线程没有停止")
        service._busy.release()
        listing = ReviewHistory(lambda: self.root).list(symbol="futures")
        detail = ReviewHistory(lambda: self.root).detail(listing["items"][0]["id"])
        self.assertEqual((detail["status"], detail["answer"]), ("stopped", "先看两笔平仓"))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "reviews"
        state = book([trade(1, "2026-10-14 10:00:00", pnl=4, fee=1, close=1, opening=0)])
        service = TradeReviewService(lambda account: state, lambda: self.root,
                                     transport=lambda settings, payload, cancelled: chunk(),
                                     settings=lambda: SETTINGS, clock=lambda: NOW)
        self.patches = [patch.object(server, "trade_review", service),
                        patch.object(server, "review_history", ReviewHistory(lambda: self.root))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def call(func, *args, **kwargs):
        result = func(*args, **kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    def test_snapshot_and_invalid_date_http(self):
        result = payload(self.call(server.review_snapshot, account="futures", date="2026-10-14"))
        self.assertEqual(result["summary"]["netPnl"], 3)
        with self.assertRaises(HTTPException) as caught:
            self.call(server.review_snapshot, account="futures", date="2026-02-30")
        self.assertEqual(caught.exception.status_code, 400)

    def test_review_stream_and_history_http(self):
        async def run():
            response = await server.review({"account": "futures", "date": "2026-10-14"})
            self.assertIsInstance(response, StreamingResponse)
            return [parse_event(text) async for text in response.body_iterator if not text.startswith(":")]

        events = asyncio.run(run())
        self.assertEqual(events[-1][0], "done")
        listing = payload(self.call(server.review_history_list, account="futures"))
        self.assertEqual(listing["total"], 1)
        identity = listing["items"][0]["id"]
        detail = payload(self.call(server.review_history_detail, identity))
        self.assertEqual(detail["snapshot"]["summary"]["netPnl"], 3)


if __name__ == "__main__":
    unittest.main()
