"""加密永续模拟交易: 深度撮合、数量规则、限价穿价、杠杆与保证金、反手、资金费、强平、止盈止损, 以及 app 分流。"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from fastapi import HTTPException

import app as server
from binance_feed import BinanceAdapter
from paper import OrderError, PaperStore
from paper_crypto import CryptoBook, CryptoPaperService, normalize_request, quote_snapshot, sweep

BTC = BinanceAdapter().builtin_instruments()[0]
NOW = 1_791_000_000_000


def make_quote(bids=((100.0, 1.0), (99.9, 2.0)), asks=((100.1, 1.0), (100.2, 2.0)), last=100.0, mark=100.0,
               rate=None, next_time=None, fresh=True, instrument=BTC):
    received = time.time() - (0 if fresh else 60)
    raw = {"book": {"bids": [list(row) for row in bids], "asks": [list(row) for row in asks], "time": NOW,
                    "received": received},
           "ticker": {"last": last, "time": NOW},
           "mark": {"markPrice": mark, "fundingRate": rate, "nextFundingTime": next_time, "time": NOW}}
    return quote_snapshot(instrument.symbol, instrument, raw, time.time())


def order(side, qty, kind="market", price=None, client_id=""):
    return {"symbol": BTC.symbol, "side": side, "qty": qty, "type": kind, "price": price, "clientId": client_id}


class BookTests(unittest.TestCase):
    def test_sweep_walks_levels_within_the_limit(self):
        asks = [[100.1, 1.0], [100.2, 2.0]]
        self.assertAlmostEqual(sweep(asks, 2.0, None, "buy"), 100.15)
        self.assertIsNone(sweep(asks, 2.0, 100.1, "buy"))
        self.assertIsNone(sweep(asks, 4.0, None, "buy"))
        self.assertAlmostEqual(sweep([[100.0, 1.0], [99.9, 2.0]], 3.0, None, "sell"), (100.0 + 2 * 99.9) / 3)

    def test_market_order_fills_at_book_vwap_with_taker_fee(self):
        book = CryptoBook()
        filled = book.place(order("buy", 2.0), make_quote(), NOW)
        self.assertEqual((filled["status"], filled["fillPrice"]), ("filled", 100.15))
        self.assertAlmostEqual(book.fees_paid, 2 * 100.15 * 0.0005)
        self.assertEqual(book.positions[BTC.symbol], {"qty": 2.0, "avgPrice": 100.15})
        self.assertEqual(book.trades[-1]["liquidity"], "taker")

    def test_market_order_needs_depth_and_a_fresh_book(self):
        with self.assertRaisesRegex(OrderError, "盘口深度不足"):
            CryptoBook().place(order("buy", 5.0), make_quote(), NOW)
        with self.assertRaisesRegex(OrderError, "盘口"):
            CryptoBook().place(order("buy", 1.0), make_quote(fresh=False), NOW)

    def test_quantity_rules(self):
        with self.assertRaisesRegex(OrderError, "整数倍"):
            CryptoBook().place(order("buy", 0.0015), make_quote(), NOW)
        coarse = replace(BTC, min_qty=0.01, min_notional=5.0)
        with self.assertRaisesRegex(OrderError, "不能少于"):
            CryptoBook().place(order("buy", 0.005), make_quote(instrument=coarse), NOW)
        with self.assertRaisesRegex(OrderError, "最小下单金额"):
            CryptoBook().place(order("buy", 0.01), make_quote(instrument=coarse), NOW)

    def test_limit_order_fills_only_after_price_trades_through(self):
        book = CryptoBook()
        resting = book.place(order("sell", 1.0, "limit", 100.5), make_quote(), NOW)
        self.assertEqual(resting["status"], "open")
        touched = make_quote(bids=((100.5, 1.0),), asks=((100.6, 1.0),), last=100.5)
        self.assertFalse(book.match({BTC.symbol: touched}, NOW + 1000))     # 碰到不算
        through = make_quote(bids=((100.6, 1.0),), asks=((100.7, 1.0),), last=100.6)
        self.assertTrue(book.match({BTC.symbol: through}, NOW + 2000))
        self.assertEqual((resting["status"], resting["fillPrice"]), ("filled", 100.5))
        self.assertAlmostEqual(book.fees_paid, 100.5 * 0.0002)                 # 挂单费率
        self.assertEqual(book.positions[BTC.symbol]["qty"], -1.0)

    def test_marketable_limit_takes_only_levels_inside_the_limit(self):
        book = CryptoBook()
        self.assertEqual(book.place(order("buy", 1.0, "limit", 100.1), make_quote(), NOW)["fillPrice"], 100.1)
        self.assertEqual(book.place(order("buy", 2.0, "limit", 100.1), make_quote(), NOW)["status"], "open")

    def test_leverage_sets_the_margin_requirement(self):
        deep = make_quote(asks=((100.0, 5000.0),), bids=((99.9, 5000.0),))
        book = CryptoBook()
        with self.assertRaisesRegex(OrderError, "资金不足"):
            book.place(order("buy", 1000.0), deep, NOW)                       # 名义 10 万 / 10 倍 > 1 万
        book.set_leverage(BTC.symbol, 20, 125)
        book.place(order("buy", 1000.0), deep, NOW)
        self.assertAlmostEqual(book.summary()["account"]["margin"], 1000 * 100.0 / 20)
        with self.assertRaisesRegex(OrderError, "降低杠杆"):
            book.set_leverage(BTC.symbol, 1, 125)
        self.assertEqual(book.leverage_of(BTC.symbol), 20)
        with self.assertRaisesRegex(OrderError, "1~125"):
            book.set_leverage(BTC.symbol, 200, 125)

    def test_reversal_realizes_pnl_and_opens_the_other_side(self):
        book = CryptoBook()
        book.place(order("buy", 1.0), make_quote(), NOW)
        book.place(order("sell", 3.0), make_quote(bids=((100.0, 5.0),)), NOW)
        self.assertEqual(book.positions[BTC.symbol], {"qty": -2.0, "avgPrice": 100.0})
        self.assertAlmostEqual(book.realized, -0.1)

    def test_opening_funds_use_mark_price_for_both_sides(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                book = CryptoBook({"initialCash": 10.16})
                quote = make_quote(bids=((99., 10.),), asks=((101., 10.),), mark=100.)
                with self.assertRaisesRegex(OrderError, "资金不足"):
                    book.place(order(side, 1.), quote, NOW)
                self.assertEqual(book.positions, {})
                self.assertEqual(book.orders, [])

    def test_marketable_limit_checks_actual_fill_instead_of_limit(self):
        for side, limit in (("buy", 110.), ("sell", 90.)):
            with self.subTest(side=side):
                book = CryptoBook({"initialCash": 10.1})
                quote = make_quote(bids=((100., 10.),), asks=((100., 10.),), mark=100.)
                filled = book.place(order(side, 1., "limit", limit), quote, NOW)
                self.assertEqual((filled["status"], filled["fillPrice"]), ("filled", 100.))
                self.assertAlmostEqual(book.summary()["account"]["available"], .05)

    def test_fill_valuation_uses_current_quote_and_last_price_fallback(self):
        for mark in (100., None):
            with self.subTest(mark=mark):
                book = CryptoBook({"initialCash": 10.16})
                book.marks[BTC.symbol] = 90.  # 当前报价已比上一次估值更新。
                quote = make_quote(asks=((100., 10.),), mark=mark, last=100.)
                book.place(order("buy", 1.), quote, NOW)
                summary = book.summary()["account"]
                self.assertEqual(summary["floatPnl"], 0.)
                self.assertAlmostEqual(summary["available"], .11)

    def test_resting_order_rechecks_funds_at_current_mark(self):
        book = CryptoBook({"initialCash": 10.2})
        resting = book.place(order("buy", 1., "limit", 100.), make_quote(), NOW)
        self.assertEqual(resting["status"], "open")
        through = make_quote(bids=((98., 10.),), asks=((99., 10.),), mark=99., last=99.)
        self.assertTrue(book.match({BTC.symbol: through}, NOW + 1000))
        self.assertEqual(resting["status"], "rejected")
        self.assertEqual(book.positions, {})

    def test_reduction_still_allowed_with_insufficient_initial_margin(self):
        book = CryptoBook({"initialCash": 100.})
        book.place(order("buy", 9.), make_quote(asks=((100., 10.),)), NOW)
        quote = make_quote(bids=((90., 10.),), mark=90., last=90.)
        book.settle({BTC.symbol: quote}, NOW + 1000)
        self.assertLess(book.summary()["account"]["available"], 0.)
        filled = book.place(order("sell", 1.), quote, NOW + 1001)
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(book.positions[BTC.symbol]["qty"], 8.)

    def test_funding_settles_at_the_announced_time_with_the_rate_seen_before(self):
        book = CryptoBook()
        book.place(order("buy", 1.0), make_quote(), NOW)
        self.assertFalse(book.settle({BTC.symbol: make_quote(rate=0.0001, next_time=NOW + 1000)}, NOW))
        cash = book.cash
        after = make_quote(rate=0.0005, next_time=NOW + 8 * 3_600_000)      # 结算后费率换成下一期的
        self.assertTrue(book.settle({BTC.symbol: after}, NOW + 1000))
        self.assertAlmostEqual(book.cash - cash, -1.0 * 100.0 * 0.0001)    # 多头在正费率时付费
        self.assertEqual(len(book.fundings), 1)
        self.assertFalse(book.settle({BTC.symbol: after}, NOW + 2000))      # 同一期不重复结算

    def test_reopening_before_funding_charges_only_the_new_position(self):
        book = CryptoBook()
        quote = make_quote(rate=.001, next_time=NOW + 1000)
        book.place(order("buy", 1.), quote, NOW)
        book.settle({BTC.symbol: quote}, NOW)
        book.flatten(BTC.symbol, quote, NOW + 500)
        book.place(order("sell", 2.), quote, NOW + 750)
        # 新仓建立后还没跑过循环, 到点仍应按新空仓数量收费/付费。
        after = make_quote(rate=.002, next_time=NOW + 2000)
        book.settle({BTC.symbol: after}, NOW + 1000)
        self.assertEqual(len(book.fundings), 1)
        self.assertEqual(book.fundings[0]["qty"], -2.)
        self.assertAlmostEqual(book.funding, .2)

    def test_flat_resting_order_does_not_accrue_funding_for_later_position(self):
        book = CryptoBook()
        quote = make_quote(rate=.001, next_time=NOW + 1000)
        book.place(order("buy", 1., "limit", 90.), quote, NOW)
        book.settle({BTC.symbol: quote}, NOW)
        later = make_quote(rate=.002, next_time=NOW + 3000)
        book.place(order("buy", 1.), later, NOW + 2000)
        book.settle({BTC.symbol: later}, NOW + 2001)
        self.assertEqual(book.fundings, [])

    def test_liquidation_when_equity_falls_below_maintenance(self):
        book = CryptoBook({"initialCash": 100.0})
        book.set_leverage(BTC.symbol, 100, 125)
        deep = make_quote(asks=((100.0, 50.0),), bids=((99.9, 50.0),))
        book.place(order("buy", 9.0), deep, NOW)
        book.place(order("sell", 1.0, "limit", 120.0), deep, NOW)
        self.assertIsNotNone(book.summary()["positions"][0]["liqPrice"])
        self.assertTrue(book.settle({BTC.symbol: make_quote(mark=89.0)}, NOW + 1000))
        self.assertEqual(book.positions, {})
        self.assertEqual(book.trades[-1]["liquidity"], "liquidation")
        self.assertEqual({item["status"] for item in book.orders}, {"filled", "cancelled"})

    def test_stops_trigger_on_last_price_and_sweep_the_book(self):
        book = CryptoBook()
        book.place(order("buy", 2.0), make_quote(), NOW)                  # 多 2 @100.15, 最新价 100
        with self.assertRaisesRegex(OrderError, "多单止盈价要高于最新价 100"):
            book.set_stops(BTC.symbol, 99.5, None, make_quote())
        book.set_stops(BTC.symbol, 101.0, 99.0, make_quote())
        self.assertEqual(book.summary()["positions"][0]["sl"], 99.0)
        thin = make_quote(last=99.0, bids=((98.9, 0.5),))
        self.assertEqual(book.trigger_stops({BTC.symbol: thin}, NOW), [])
        self.assertIn("盘口深度不足", book.stop_errors[BTC.symbol])         # 吃不够, 下一轮再试
        placed = book.trigger_stops({BTC.symbol: make_quote(last=99.0, bids=((98.9, 1.0), (98.8, 2.0)))}, NOW)
        self.assertEqual([(item["side"], item["qty"], item["reason"]) for item in placed], [("sell", 2.0, "止损")])
        self.assertAlmostEqual(placed[0]["fillPrice"], 98.85)
        self.assertEqual((book.positions, book.stop_errors), ({}, {}))

    def test_orders_carry_stops_onto_the_position(self):
        book = CryptoBook()
        with self.assertRaisesRegex(OrderError, "空单止损价要高于最新价 100"):
            book.place(order("sell", 1.0) | {"sl": 99.0}, make_quote(), NOW)
        placed = book.place(order("sell", 1.0) | {"tp": 95.0, "sl": 103.0}, make_quote(), NOW)
        self.assertEqual((placed["tp"], placed["sl"]), (95.0, 103.0))
        self.assertEqual(book.summary()["positions"][0]["tp"], 95.0)
        with self.assertRaisesRegex(OrderError, "只减仓或平仓"):
            book.place(order("buy", 1.0) | {"tp": 105.0}, make_quote(), NOW)
        # 限价单按委托价校验, 成交时(挂单费率)挂上
        with self.assertRaisesRegex(OrderError, "多单止盈价要高于委托价 99"):
            book.place(order("buy", 3.0, "limit", 99.0) | {"tp": 98.0}, make_quote(), NOW)
        resting = book.place(order("buy", 3.0, "limit", 99.0) | {"tp": 104.0, "sl": 97.0}, make_quote(), NOW)
        self.assertTrue(book.match({BTC.symbol: make_quote(last=98.9)}, NOW))
        self.assertEqual(resting["status"], "filled")
        position = book.positions[BTC.symbol]
        self.assertEqual((position["qty"], position["tp"], position["sl"]), (2.0, 104.0, 97.0))   # 反手成多 2

    def test_stop_order_triggers_on_last_and_sweeps_with_taker_fee(self):
        book = CryptoBook()
        with self.assertRaisesRegex(OrderError, "买入条件单的触发价要高于最新价 100"):
            book.place(order("buy", 2.0, "stop", 100.0), make_quote(), NOW)
        placed = book.place(order("buy", 2.0, "stop", 100.5), make_quote(), NOW)
        self.assertEqual((placed["status"], placed["price"]), ("open", 100.5))
        self.assertFalse(book.match({BTC.symbol: make_quote(last=100.4)}, NOW))
        thin = make_quote(last=100.5, asks=((100.6, 1.0),))
        self.assertFalse(book.match({BTC.symbol: thin}, NOW))                 # 深度不够: 留着重试
        self.assertIn("盘口深度不足", placed["reason"])
        self.assertTrue(book.match({BTC.symbol: make_quote(last=100.5, asks=((100.6, 1.0), (100.8, 1.0)))}, NOW))
        self.assertAlmostEqual(placed["fillPrice"], 100.7)
        self.assertEqual((placed["reason"], book.trades[-1]["liquidity"]), ("", "taker"))
        self.assertAlmostEqual(book.fees_paid, 2 * 100.7 * 0.0005)

    def test_amend_never_moves_an_order_into_an_immediate_fill(self):
        book = CryptoBook()
        resting = book.place(order("sell", 1.0, "limit", 100.5), make_quote(), NOW)
        with self.assertRaisesRegex(OrderError, "立即成交"):
            book.amend(resting["id"], 100.0, make_quote(), NOW)                 # 够到买一
        self.assertEqual(book.amend(resting["id"], 100.3, make_quote(), NOW)["price"], 100.3)
        stop = book.place(order("sell", 1.0, "stop", 99.5), make_quote(), NOW)
        with self.assertRaisesRegex(OrderError, "卖出条件单的触发价要低于最新价 100"):
            book.amend(stop["id"], 100.2, make_quote(), NOW)
        self.assertEqual(book.amend(stop["id"], 99.0, make_quote(), NOW)["price"], 99.0)
        self.assertEqual([item["status"] for item in book.orders], ["open", "open"])

    def test_client_id_places_once(self):
        book = CryptoBook()
        first = book.place(order("buy", 1.0, client_id="x"), make_quote(), NOW)
        self.assertIs(book.place(order("buy", 1.0, client_id="x"), make_quote(), NOW), first)
        self.assertEqual(len(book.trades), 1)
        self.assertTrue(first["id"].startswith("C"))


class MarginOrderTests(unittest.TestCase):
    def request(self, margin, side="buy", kind="market", price=None, **extra):
        return {"symbol": BTC.symbol, "side": side, "margin": margin, "type": kind, "price": price, **extra}

    def test_normalization_accepts_either_coins_or_margin_and_rejects_invalid_amounts(self):
        self.assertEqual(normalize_request(self.request(20))["margin"], 20)
        self.assertNotIn("qty", normalize_request(self.request(20)))
        self.assertEqual(normalize_request(order("buy", 1))["qty"], 1)
        with self.assertRaisesRegex(OrderError, "只能填一项"):
            normalize_request(self.request(20, qty=1))
        for value in (0, -1, True, "20", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(OrderError, "保证金金额"):
                normalize_request(self.request(value))

    def test_market_amount_walks_both_sides_of_depth_and_rounds_down(self):
        for side, expected_qty in (("buy", 1.997), ("sell", 2.001)):
            with self.subTest(side=side):
                book = CryptoBook()
                quote = make_quote()
                filled = book.place(self.request(20, side), quote, NOW)
                self.assertEqual(filled["qty"], expected_qty)
                self.assertLessEqual(filled["qty"] * filled["fillPrice"], 200)
                levels = quote["asks"] if side == "buy" else quote["bids"]
                next_qty = expected_qty + quote["qtyStep"]
                self.assertGreater(next_qty * sweep(levels, next_qty, None, side), 200)
                self.assertAlmostEqual(book.fees_paid, expected_qty * filled["fillPrice"] * 0.0005)
                self.assertEqual((filled["margin"], filled["sizingLeverage"]), (20, 10))

    def test_exact_step_is_not_lost_to_float_rounding(self):
        filled = CryptoBook().place(self.request(20.03), make_quote(), NOW)
        self.assertEqual(filled["qty"], 2)
        self.assertAlmostEqual(filled["fillPrice"], 100.15)
        coarse = replace(BTC, step_size=0.1, min_qty=0.1, min_notional=0)
        quote = make_quote(instrument=coarse, bids=((2.9, 10),), asks=((3.0, 10),), last=3, mark=3)
        self.assertEqual(CryptoBook().place(self.request(0.09), quote, NOW)["qty"], 0.3)

    def test_current_contract_leverage_controls_sizing(self):
        book = CryptoBook()
        book.set_leverage(BTC.symbol, 20, 125)
        filled = book.place(self.request(10.015), make_quote(), NOW)
        self.assertEqual((filled["qty"], filled["sizingLeverage"]), (2, 20))

    def test_margin_still_requires_fresh_and_sufficient_depth(self):
        for quote, margin, message in ((make_quote(fresh=False), 20, "盘口"),
                                       (make_quote(), 40, "盘口深度不足"),
                                       (make_quote(asks=()), 20, "盘口深度不足")):
            with self.subTest(margin=margin, quote=quote), self.assertRaisesRegex(OrderError, message):
                book = CryptoBook()
                book.place(self.request(margin), quote, NOW)
            self.assertEqual((book.orders, book.positions, book.cash), ([], {}, 10000))

    def test_depth_beyond_five_levels_fills_without_repeated_clicks(self):
        bids = [(100 - i * 0.1, 0.1) for i in range(20)]
        asks = [(100.1 + i * 0.1, 0.1) for i in range(20)]
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                book = CryptoBook()
                request = self.request(10, side, clientId=f"deep-{side}")
                with self.assertRaisesRegex(OrderError, "盘口深度不足"):
                    book.place(request, make_quote(bids=bids[:5], asks=asks[:5]), NOW)
                self.assertEqual((book.orders, book.positions, book.cash), ([], {}, 10000))
                deep = make_quote(bids=bids, asks=asks)
                filled = book.place(request, deep, NOW + 100)
                self.assertEqual(filled["status"], "filled")
                self.assertGreater(filled["qty"], 0.5)       # 实际用到了第五档之后
                self.assertLessEqual(filled["qty"] * filled["fillPrice"], 100)
                self.assertIs(book.place(request, deep, NOW + 200), filled)
                self.assertEqual(len(book.trades), 1)

    def test_depth_error_reports_leveraged_notional_and_the_correct_side(self):
        for side, label, available in (("buy", "卖盘", "300.50"), ("sell", "买盘", "299.80")):
            with self.subTest(side=side):
                book = CryptoBook()
                book.set_leverage(BTC.symbol, 20, 125)
                with self.assertRaises(OrderError) as raised:
                    book.place(self.request(20, side), make_quote(), NOW)
                self.assertIn("本单需 400.00 USDT", str(raised.exception))
                self.assertIn(f"当前{label} 2 档可承接 {available} USDT", str(raised.exception))
                self.assertIn("减少保证金金额", str(raised.exception))
                self.assertEqual((book.orders, book.positions, book.cash), ([], {}, 10000))

    def test_depth_error_writes_quantities_in_full_at_the_step_precision(self):
        coin = replace(BTC, base="DOGE", step_size=1.0, min_qty=1.0)
        quote = make_quote(asks=((0.2, 1_000_000.0), (0.21, 234_567.0)), instrument=coin)
        with self.assertRaises(OrderError) as raised:
            CryptoBook().place({**order("buy", 2_000_000.0), "symbol": coin.symbol}, quote, NOW)
        self.assertIn("本单需 2,000,000 DOGE", str(raised.exception))
        self.assertIn("可承接 1,234,567 DOGE", str(raised.exception))
        quote = make_quote(asks=((100.1, 1000.0), (100.2, 234.5)))
        with self.assertRaises(OrderError) as raised:
            CryptoBook().place(order("buy", 1234.567), quote, NOW)
        self.assertIn("本单需 1,234.567 BTC", str(raised.exception))
        self.assertIn("可承接 1,234.500 BTC", str(raised.exception))

    def test_invalidated_depth_cannot_fill_even_with_a_recent_receive_time(self):
        raw = {"book": {"bids": [[100, 100]], "asks": [[100.1, 100]], "received": time.time(),
                        "valid": False, "reason": "盘口连接中断，正在重新同步"}}
        quote = quote_snapshot(BTC.symbol, BTC, raw, time.time())
        self.assertFalse(quote["open"])
        with self.assertRaisesRegex(OrderError, "重新同步"):
            CryptoBook().place(self.request(20), quote, NOW)

    def test_minimum_quantity_notional_and_maximum_notional_are_enforced(self):
        quote = make_quote(instrument=replace(BTC, min_notional=5))
        for margin, message in ((0.001, "保证金金额过小"), (0.1, "最小下单金额"), (1e308, "名义金额超过")):
            with self.subTest(margin=margin), self.assertRaisesRegex(OrderError, message):
                CryptoBook().place(self.request(margin), quote, NOW)

    def test_fees_need_additional_available_funds(self):
        quote = make_quote(mark=100.15)
        with self.assertRaisesRegex(OrderError, "资金不足"):
            CryptoBook({"cash": 20.03}).place(self.request(20.03), quote, NOW)
        book = CryptoBook({"cash": 20.2})
        book.place(self.request(20.03), quote, NOW)
        self.assertAlmostEqual(book.summary()["account"]["margin"], 20.03)
        self.assertAlmostEqual(book.fees_paid, 0.10015)

    def test_limit_and_stop_quantities_stay_fixed_after_price_and_leverage_changes(self):
        for kind, original, amended in (("limit", 99, 98), ("stop", 105, 106)):
            with self.subTest(kind=kind):
                book = CryptoBook()
                resting = book.place(self.request(original / 10, kind=kind, price=original, tp=120, sl=90),
                                     make_quote(), NOW)
                self.assertEqual((resting["status"], resting["qty"]), ("open", 1))
                book.set_leverage(BTC.symbol, 20, 125)
                book.amend(resting["id"], amended, make_quote(), NOW)
                trigger = 97 if kind == "limit" else 107
                quote = make_quote(bids=((trigger, 3),), asks=((trigger + .1, 3),), last=trigger, mark=trigger)
                book.match({BTC.symbol: quote}, NOW + 1000)
                self.assertEqual((resting["status"], resting["qty"]), ("filled", 1))
                self.assertEqual((book.positions[BTC.symbol]["tp"], book.positions[BTC.symbol]["sl"]), (120, 90))

    def test_immediately_marketable_limit_uses_the_order_price_for_sizing(self):
        filled = CryptoBook().place(self.request(20.1, kind="limit", price=100.5), make_quote(), NOW)
        self.assertEqual((filled["status"], filled["qty"], filled["fillPrice"]), ("filled", 2, 100.15))

    def test_duplicate_client_id_does_not_reconvert_at_new_prices(self):
        book = CryptoBook()
        request = self.request(20.03, clientId="margin-once")
        first = book.place(request, make_quote(), NOW)
        book.set_leverage(BTC.symbol, 20, 125)
        self.assertIs(book.place(request, make_quote(fresh=False), NOW + 1000), first)
        self.assertEqual((len(book.trades), first["qty"]), (1, 2))


class FakeCrypto:
    """假的加密行情: 只认 BTC 永续, 报价从 raw 里给; 记录 touch。"""

    def __init__(self):
        self.raw = {}
        self.touched = []

    def instrument(self, symbol):
        return BTC if symbol == BTC.symbol else None

    def touch(self, symbol, kinds, seconds):
        self.touched.append((symbol, tuple(kinds)))

    def quote(self, symbol):
        if symbol != BTC.symbol:
            raise AssertionError(f"不应获取 {symbol} 的盘口")
        return self.raw


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "paper", "crypto.json")
        self.crypto = FakeCrypto()
        self.crypto.raw = {"book": {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]], "time": NOW,
                                    "received": time.time()},
                           "ticker": {"last": 100.0}, "mark": {"markPrice": 100.0}}
        self.service = CryptoPaperService(lambda: self.crypto, PaperStore(lambda: self.path), clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def test_state_quotes_the_selected_contract(self):
        state = self.service.state(BTC.symbol)
        self.assertEqual((state["mode"], state["leverage"], state["error"]), ("crypto", 10, None))
        self.assertEqual(state["quote"]["ask"], 100.1)
        self.assertIn((BTC.symbol, ("book", "mark", "ticker")), self.crypto.touched)

    def test_panel_state_carries_five_levels_while_orders_sweep_the_full_depth(self):
        self.crypto.raw["book"]["bids"] = [[100.0 - i * 0.1, 0.1] for i in range(400)]
        self.crypto.raw["book"]["asks"] = [[100.1 + i * 0.1, 0.1] for i in range(400)]
        quote = self.service.state(BTC.symbol)["quote"]
        self.assertEqual((len(quote["bids"]), len(quote["asks"])), (5, 5))
        self.assertEqual((quote["bid"], quote["ask"]), (100.0, 100.1))
        placed = self.service.place(order("buy", 2.0))
        self.assertEqual(placed["status"], "filled")              # 吃到了第五档之后
        self.assertEqual(len(self.crypto.raw["book"]["asks"]), 400)

    def test_margin_orders_use_aggregate_leverage_and_persist_resolved_quantity(self):
        self.service.set_leverage("AGG.BTC", 20)
        placed = self.service.place({"symbol": "AGG.BTC", "side": "buy", "margin": 10.01})
        self.assertEqual((placed["contract"], placed["qty"], placed["margin"], placed["sizingLeverage"]),
                         (BTC.symbol, 2, 10.01, 20))
        reloaded = CryptoPaperService(lambda: self.crypto, PaperStore(lambda: self.path), clock=lambda: NOW)
        self.assertEqual(reloaded.state(BTC.symbol)["orders"][0], placed)
        flattened = reloaded.flatten("AGG.BTC")
        self.assertEqual((flattened["qty"], flattened["status"]), (2, "filled"))
        self.assertEqual(reloaded.state(BTC.symbol)["positions"], [])

    def test_aggregate_uses_binance_book_and_shares_positions_trades_and_leverage(self):
        self.crypto.raw["book"]["asks"] = [[100.1, 1.0], [100.2, 4.0]]
        self.crypto.raw["mark"]["markPrice"] = 102.0
        self.service.set_leverage("AGG.BTC", 20)
        filled = self.service.place(order("buy", 2.0) | {"symbol": "AGG.BTC"})
        self.assertEqual((filled["symbol"], filled["contract"], filled["status"]),
                         ("AGG.BTC", BTC.symbol, "filled"))
        self.assertAlmostEqual(filled["fillPrice"], 100.15)
        state = self.service.state("AGG.BTC")
        self.assertEqual((state["symbol"], state["contract"], state["error"], state["leverage"]),
                         ("AGG.BTC", BTC.symbol, None, 20))
        self.assertEqual(state["quote"]["contract"], BTC.symbol)
        self.assertEqual((state["quote"]["qtyStep"], state["quote"]["priceTick"]),
                         (BTC.step_size, BTC.tick_size))
        self.assertAlmostEqual(state["positions"][0]["floatPnl"], 3.7)
        self.assertAlmostEqual(state["positions"][0]["margin"], 10.2)

        self.service.place(order("sell", 0.5))                 # 单所页面减同一份仓
        aggregate, single = self.service.state("AGG.BTC"), self.service.state(BTC.symbol)
        for key in ("positions", "orders", "contractTrades", "leverage"):
            self.assertEqual(aggregate[key], single[key])
        self.assertEqual(len(aggregate["positions"]), 1)
        self.assertEqual(aggregate["positions"][0]["qty"], 1.5)
        self.assertEqual([trade["contract"] for trade in aggregate["contractTrades"]], [BTC.symbol] * 2)
        self.assertTrue(all(symbol == BTC.symbol for symbol, _ in self.crypto.touched))
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(list(saved["positions"]), [BTC.symbol])
        self.assertEqual(saved["leverage"], {BTC.symbol: 20})

        restored = CryptoPaperService(lambda: self.crypto, PaperStore(lambda: self.path), clock=lambda: NOW)
        self.assertEqual(restored.state("AGG.BTC")["contractTrades"], aggregate["contractTrades"])
        flattened = restored.flatten("AGG.BTC")
        self.assertEqual((flattened["contract"], flattened["qty"], flattened["fillPrice"]),
                         (BTC.symbol, 1.5, 100.0))
        self.assertEqual(restored.state(BTC.symbol)["positions"], [])

    def test_aggregate_orders_amend_cancel_and_match_on_the_binance_cycle(self):
        resting = self.service.place(order("buy", 1.0, "limit", 99.0) | {"symbol": "AGG.BTC"})
        self.assertEqual(self.service.amend(resting["id"], 99.5)["price"], 99.5)
        cancelled = self.service.place(order("sell", 1.0, "limit", 105.0) | {"symbol": "AGG.BTC"})
        self.assertEqual(self.service.cancel(cancelled["id"])["status"], "cancelled")
        triggered = self.service.place(order("buy", 1.0, "stop", 101.0) | {"symbol": "AGG.BTC"})
        self.crypto.raw["ticker"]["last"] = 101.0
        self.crypto.raw["book"].update(bids=[[100.9, 5.0]], asks=[[101.0, 5.0]])
        self.service.on_cycle(self.crypto)
        self.assertEqual((triggered["status"], triggered["fillPrice"]), ("filled", 101.0))
        self.assertEqual(resting["status"], "open")
        self.crypto.raw["ticker"]["last"] = 99.0
        self.crypto.raw["book"].update(bids=[[98.9, 5.0]], asks=[[99.0, 5.0]])
        self.service.on_cycle(self.crypto)
        self.assertEqual((resting["status"], resting["fillPrice"]), ("filled", 99.5))
        state = self.service.state("AGG.BTC")
        self.assertEqual((state["positions"][0]["contract"], state["positions"][0]["qty"]), (BTC.symbol, 2.0))
        self.assertTrue(all(symbol == BTC.symbol for symbol, _ in self.crypto.touched))

    def test_aggregate_still_requires_a_binance_contract_valid_quantity_and_fresh_depth(self):
        state = self.service.state("AGG.UNKNOWN")
        self.assertIsNone(state["contract"])
        self.assertIsNone(state["quote"])
        self.assertIn("BINANCE.UNKNOWNUSDT.P", state["error"])
        with self.assertRaisesRegex(OrderError, "不支持"):
            self.service.place(order("buy", 1.0) | {"symbol": "AGG.UNKNOWN"})
        with self.assertRaisesRegex(OrderError, "整数倍"):
            self.service.place(order("buy", 0.0015) | {"symbol": "AGG.BTC"})
        with self.assertRaisesRegex(OrderError, "盘口深度不足"):
            self.service.place(order("buy", 6.0) | {"symbol": "AGG.BTC"})
        self.crypto.raw["book"]["received"] = time.time() - 60
        self.assertFalse(self.service.state("AGG.BTC")["quote"]["open"])
        with self.assertRaisesRegex(OrderError, "盘口"):
            self.service.place(order("buy", 1.0) | {"symbol": "AGG.BTC"})
        self.assertEqual(self.service.state("AGG.BTC")["positions"], [])

    def test_orders_persist_and_resting_orders_fill_on_the_cycle(self):
        resting = self.service.place(order("sell", 1.0, "limit", 101.0))
        self.crypto.raw["book"] = {"bids": [[101.5, 5.0]], "asks": [[101.6, 5.0]], "received": time.time()}
        self.service.on_cycle(self.crypto)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["orders"][0]["status"], "filled")
        self.assertEqual(saved["positions"][BTC.symbol]["qty"], -1.0)
        self.assertEqual(resting["id"], "CO1")
        self.service.set_leverage(BTC.symbol, 5)
        summary = self.service.reset(500)
        self.assertEqual(summary["account"]["equity"], 500.0)
        self.assertEqual(self.service.state(BTC.symbol)["leverage"], 5)       # 重置保留杠杆设置

    def test_amend_through_the_service(self):
        resting = self.service.place(order("buy", 1.0, "limit", 99.0))
        self.assertEqual(self.service.amend(resting["id"], 99.5)["price"], 99.5)
        with self.assertRaisesRegex(OrderError, "立即成交"):
            self.service.amend(resting["id"], 100.1)
        with self.assertRaisesRegex(OrderError, "找不到"):
            self.service.amend("CO99", 99.0)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["orders"][0]["price"], 99.5)

    def test_aggregate_stops_set_through_the_service_fire_on_the_binance_cycle(self):
        self.service.place(order("sell", 1.0) | {"symbol": "AGG.BTC"})    # 空 1 @100
        result = self.service.set_stops("AGG.BTC", 95.0, None)
        self.assertEqual((result["contract"], result["tp"], result["sl"]), (BTC.symbol, 95.0, None))
        self.crypto.raw["ticker"] = {"last": 95.0}
        self.crypto.raw["book"] = {"bids": [[95.0, 5.0]], "asks": [[95.1, 5.0]], "received": time.time()}
        self.service.on_cycle(self.crypto)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["positions"], {})
        self.assertEqual((saved["orders"][-1]["reason"], saved["orders"][-1]["fillPrice"]), ("止盈", 95.1))

    def test_flat_at_funding_time_is_not_charged_after_reopening(self):
        now = [NOW]
        self.service._clock = lambda: now[0]
        self.crypto.raw["mark"].update(fundingRate=.001, nextFundingTime=NOW + 1000)
        self.service.place(order("buy", 1.))
        self.service.on_cycle(self.crypto)
        now[0] = NOW + 500
        self.service.flatten(BTC.symbol)
        now[0] = NOW + 1001
        self.service.on_cycle(self.crypto)
        self.assertEqual(self.service.state(BTC.symbol)["positions"], [])

        now[0] = NOW + 2000
        self.crypto.raw["mark"].update(fundingRate=.002, nextFundingTime=NOW + 3000)
        self.service.place(order("buy", 2.))
        self.service.on_cycle(self.crypto)
        self.assertEqual(self.service.state(BTC.symbol)["fundings"], [])

        now[0] = NOW + 3000
        self.crypto.raw["mark"].update(fundingRate=.003, nextFundingTime=NOW + 4000)
        self.service.on_cycle(self.crypto)
        self.service.on_cycle(self.crypto)
        state = self.service.state(BTC.symbol)
        self.assertEqual(len(state["fundings"]), 1)
        self.assertAlmostEqual(state["account"]["funding"], -.4)
        with open(self.path, encoding="utf-8") as handle:
            self.assertAlmostEqual(json.load(handle)["funding"], -.4)


class NoFutures:
    """加密合约的模拟交易请求不该碰期货账户。"""

    def __getattr__(self, name):
        raise AssertionError(f"不应调用期货模拟账户的 {name}")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        crypto = FakeCrypto()
        crypto.raw = {"book": {"bids": [[100.0, 5.0]], "asks": [[100.1, 5.0]], "received": time.time()},
                      "ticker": {"last": 100.0}, "mark": {"markPrice": 100.0}}
        service = CryptoPaperService(lambda: crypto,
                                     PaperStore(lambda: os.path.join(self.temp.name, "crypto.json")))
        self.patches = [patch.object(server, "crypto_paper", service), patch.object(server, "paper", NoFutures())]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_crypto_requests_go_to_the_crypto_account(self):
        self.assertEqual(asyncio.run(server.paper_state(BTC.symbol))["mode"], "crypto")
        placed = asyncio.run(server.paper_order(order("sell", 1.0, "limit", 105.0)))
        self.assertEqual(server.paper_amend(placed["id"], {"price": 104.0})["price"], 104.0)
        self.assertEqual(server.paper_cancel(placed["id"])["status"], "cancelled")
        self.assertEqual(server.paper_leverage(BTC.symbol, 3)["account"]["equity"], 10000.0)
        self.assertEqual(server.paper_reset(cash=300, symbol=BTC.symbol)["account"]["initialCash"], 300.0)
        with self.assertRaises(HTTPException) as caught:
            server.paper_leverage("SHFE.rb2601", 3)
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            asyncio.run(server.paper_flatten(BTC.symbol))                        # 没有持仓
        asyncio.run(server.paper_order(order("buy", 1.0)))
        self.assertEqual(server.paper_stops({"symbol": BTC.symbol, "tp": None, "sl": 90})["sl"], 90.0)

    def test_margin_api_routes_to_crypto_and_invalid_requests_return_400(self):
        placed = asyncio.run(server.paper_order({"symbol": BTC.symbol, "side": "buy", "margin": 10.01}))
        self.assertEqual((placed["qty"], placed["status"]), (1, "filled"))
        for fields in ({"margin": -10}, {"margin": 10, "qty": 1}):
            with self.subTest(fields=fields), self.assertRaises(HTTPException) as caught:
                asyncio.run(server.paper_order({"symbol": BTC.symbol, "side": "buy", **fields}))
            self.assertEqual(caught.exception.status_code, 400)

    def test_aggregate_requests_keep_the_chart_symbol_and_trade_the_binance_contract(self):
        symbol = "AGG.BTC"
        state = asyncio.run(server.paper_state(symbol))
        self.assertEqual((state["symbol"], state["contract"], state["error"]), (symbol, BTC.symbol, None))
        server.paper_leverage(symbol, 3)
        filled = asyncio.run(server.paper_order(order("buy", 1.0) | {"symbol": symbol}))
        self.assertEqual(filled["contract"], BTC.symbol)
        state = asyncio.run(server.paper_state(symbol))
        self.assertEqual((state["leverage"], state["contractTrades"][0]["contract"]), (3, BTC.symbol))
        stops = server.paper_stops({"symbol": symbol, "tp": None, "sl": 90})
        self.assertEqual((stops["contract"], stops["sl"]), (BTC.symbol, 90.0))
        placed = asyncio.run(server.paper_order(order("sell", 1.0, "limit", 105.0) | {"symbol": symbol}))
        self.assertEqual(server.paper_amend(placed["id"], {"price": 104.0})["price"], 104.0)
        self.assertEqual(server.paper_cancel(placed["id"])["status"], "cancelled")
        flattened = asyncio.run(server.paper_flatten(symbol))
        self.assertEqual((flattened["contract"], flattened["status"]), (BTC.symbol, "filled"))
        self.assertEqual(asyncio.run(server.paper_state(BTC.symbol))["positions"], [])
        self.assertEqual(server.paper_reset(cash=300, symbol=symbol)["account"]["initialCash"], 300.0)


if __name__ == "__main__":
    unittest.main()
