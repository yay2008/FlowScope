"""加密永续模拟交易: 五档撮合、数量规则、限价穿价、杠杆与保证金、反手、资金费、强平, 以及 app 分流。"""
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
from paper_crypto import CryptoBook, CryptoPaperService, quote_snapshot, sweep

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
        with self.assertRaisesRegex(OrderError, "五档"):
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

    def test_client_id_places_once(self):
        book = CryptoBook()
        first = book.place(order("buy", 1.0, client_id="x"), make_quote(), NOW)
        self.assertIs(book.place(order("buy", 1.0, client_id="x"), make_quote(), NOW), first)
        self.assertEqual(len(book.trades), 1)
        self.assertTrue(first["id"].startswith("C"))


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

    def test_state_quotes_and_refuses_aggregates(self):
        state = self.service.state(BTC.symbol)
        self.assertEqual((state["mode"], state["leverage"], state["error"]), ("crypto", 10, None))
        self.assertEqual(state["quote"]["ask"], 100.1)
        self.assertIn((BTC.symbol, ("book", "mark", "ticker")), self.crypto.touched)
        self.assertIn("多所汇总", self.service.state("AGG.BTC")["error"])
        with self.assertRaisesRegex(OrderError, "多所汇总"):
            self.service.place(order("buy", 1.0) | {"symbol": "AGG.BTC"})

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
        self.assertEqual(server.paper_cancel(placed["id"])["status"], "cancelled")
        self.assertEqual(server.paper_leverage(BTC.symbol, 3)["account"]["equity"], 10000.0)
        self.assertEqual(server.paper_reset(cash=300, symbol=BTC.symbol)["account"]["initialCash"], 300.0)
        with self.assertRaises(HTTPException) as caught:
            server.paper_leverage("SHFE.rb2601", 3)
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            asyncio.run(server.paper_flatten(BTC.symbol))                        # 没有持仓


if __name__ == "__main__":
    unittest.main()
