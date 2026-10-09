"""模拟交易的离线回归: 交易时段、撮合规则、净持仓账务、手续费、持久化与采集线程回调。"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app as server
import ingest
import paper
from paper import FeeTable, OrderError, PaperBook, PaperService, PaperStore

CONTRACT = "SHFE.fu2611"
# 2026-09-30 是周三; 上期所燃油: 日盘三段 + 夜盘 21:00-23:00
NOW = datetime(2026, 9, 30, 10, 0, 1)
FU_TIME = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
           "night": [["21:00:00", "23:00:00"]]}
FEES = FeeTable({("SHFE", "fu"): {"mode": "ratio", "open": 1e-4, "closeYesterday": 1e-4, "closeToday": 0.0},
                 ("DCE", "c"): {"mode": "fixed", "open": 1.2, "closeYesterday": 1.2, "closeToday": 1.2}})


def raw_quote(**overrides):
    fields = {"instrument_name": "燃油2611", "ins_class": "FUTURE", "datetime": "2026-09-30 10:00:00.500000",
              "last_price": 3000.0, "bid_price1": 2999.0, "bid_volume1": 20, "ask_price1": 3000.0,
              "ask_volume1": 20, "upper_limit": 3300.0, "lower_limit": 2700.0, "price_tick": 1.0,
              "price_decs": 0, "volume_multiple": 10, "trading_time": FU_TIME, "expired": False,
              "underlying_symbol": ""}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def snap(contract=CONTRACT, **overrides):
    return paper.quote_snapshot(contract, raw_quote(**overrides))


def order(side, qty, kind="market", price=None, client_id=""):
    return {"symbol": CONTRACT, "side": side, "qty": qty, "type": kind, "price": price, "clientId": client_id}


def book(cash=1_000_000.0, fees=FEES):
    return PaperBook({"initialCash": cash}, fees)


class SessionTest(unittest.TestCase):
    def test_trading_day_rolls_evening_and_weekend(self):
        self.assertEqual(paper.trading_day(datetime(2026, 9, 30, 10, 0)), date(2026, 9, 30))
        self.assertEqual(paper.trading_day(datetime(2026, 9, 30, 21, 0)), date(2026, 10, 1))
        # 周五夜盘与它延续到周六凌晨的部分都属于下周一
        self.assertEqual(paper.trading_day(datetime(2026, 10, 2, 21, 0)), date(2026, 10, 5))
        self.assertEqual(paper.trading_day(datetime(2026, 10, 3, 1, 0)), date(2026, 10, 5))

    def test_session_start_day_night_and_breaks(self):
        start = paper.session_start
        self.assertEqual(start(FU_TIME, datetime(2026, 9, 30, 10, 0)), datetime(2026, 9, 30, 9, 0))
        # 同一个日盘块: 午休后的时段仍从 9:00 起算
        self.assertEqual(start(FU_TIME, datetime(2026, 9, 30, 14, 0)), datetime(2026, 9, 30, 9, 0))
        self.assertIsNone(start(FU_TIME, datetime(2026, 9, 30, 10, 20)))   # 10:15-10:30 小节休息
        self.assertIsNone(start(FU_TIME, datetime(2026, 9, 30, 12, 0)))    # 午休
        self.assertIsNone(start(FU_TIME, datetime(2026, 9, 30, 15, 0)))    # 收盘那一刻已不可成交
        self.assertEqual(start(FU_TIME, datetime(2026, 9, 30, 22, 0)), datetime(2026, 9, 30, 21, 0))

    def test_night_session_past_midnight_and_weekend(self):
        cross = {"day": FU_TIME["day"], "night": [["21:00:00", "26:30:00"]]}
        start = paper.session_start
        # 周二凌晨 1 点属于周一起算的夜盘
        self.assertEqual(start(cross, datetime(2026, 9, 29, 1, 0)), datetime(2026, 9, 28, 21, 0))
        # 周六凌晨 1 点: 周五夜盘的延续
        self.assertEqual(start(cross, datetime(2026, 10, 3, 1, 0)), datetime(2026, 10, 2, 21, 0))
        # 周一凌晨: 周日没有夜盘
        self.assertIsNone(start(cross, datetime(2026, 10, 5, 1, 0)))
        self.assertIsNone(start(cross, datetime(2026, 10, 4, 10, 0)))   # 周日白天
        self.assertIsNone(start({}, datetime(2026, 9, 30, 10, 0)))

    def test_market_status_needs_quote_from_this_session(self):
        # 节假日: 时钟在时段内, 但报价还是上一个交易日的
        stale = snap(datetime="2026-09-29 14:59:59.500000")
        self.assertEqual(paper.market_status(stale, NOW), (False, "本时段还没有新行情(可能休市)"))
        # 开盘前集合竞价的报价算本时段
        auction = snap(datetime="2026-09-30 08:58:00.000000")
        self.assertEqual(paper.market_status(auction, NOW), (True, ""))
        self.assertEqual(paper.market_status(snap(), datetime(2026, 9, 30, 12, 0)),
                         (False, "当前不在交易时段"))


class MarketOrderTest(unittest.TestCase):
    def test_buy_at_ask_and_sell_at_bid(self):
        account = book()
        buy = account.place(order("buy", 2), snap(), NOW)
        self.assertEqual((buy["status"], buy["fillPrice"]), ("filled", 3000.0))
        sell = account.place(order("sell", 2), snap(), NOW)
        self.assertEqual(sell["fillPrice"], 2999.0)
        self.assertEqual(account.positions, {})
        self.assertEqual(account.trades[-1]["pnl"], -20.0)   # 1 跳 x 10 x 2 手

    def test_rejects_when_book_side_missing_or_too_thin(self):
        account = book()
        with self.assertRaisesRegex(OrderError, "没有卖盘"):
            account.place(order("buy", 1), snap(ask_price1=float("nan")), NOW)
        with self.assertRaisesRegex(OrderError, "买一只有 3 手"):
            account.place(order("sell", 5), snap(bid_volume1=3), NOW)
        with self.assertRaisesRegex(OrderError, "不在交易时段"):
            account.place(order("buy", 1), snap(), datetime(2026, 9, 30, 12, 0))
        self.assertEqual(account.orders, [])   # 被拒的市价单不留记录

    def test_rejects_non_future_and_missing_contract_data(self):
        account = book()
        with self.assertRaisesRegex(OrderError, "不是期货"):
            account.place(order("buy", 1), snap(contract="KQ.i@SHFE.fu", ins_class="INDEX"), NOW)
        with self.assertRaisesRegex(OrderError, "合约资料"):
            account.place(order("buy", 1), snap(volume_multiple=0), NOW)
        with self.assertRaisesRegex(OrderError, "行情未就绪"):
            account.place(order("buy", 1), None, NOW)

    def test_same_client_id_only_trades_once(self):
        account = book()
        first = account.place(order("buy", 1, client_id="c1"), snap(), NOW)
        again = account.place(order("buy", 1, client_id="c1"), snap(), NOW)
        self.assertIs(first, again)
        self.assertEqual(account.positions[CONTRACT]["qty"], 1)
        self.assertEqual(len(account.trades), 1)


class NetPositionTest(unittest.TestCase):
    def test_add_reduce_reverse_and_flatten(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 2), snap(ask_price1=100.0), NOW)
        account.place(order("buy", 1), snap(ask_price1=103.0), NOW)
        self.assertEqual(account.positions[CONTRACT]["qty"], 3)
        self.assertAlmostEqual(account.positions[CONTRACT]["avgPrice"], 101.0)   # (2x100 + 103) / 3
        account.place(order("sell", 1), snap(bid_price1=105.0), NOW)
        self.assertEqual(account.trades[-1]["pnl"], 40.0)            # (105-101) x 10
        self.assertAlmostEqual(account.positions[CONTRACT]["avgPrice"], 101.0)   # 减仓不改均价
        # 反手: 平 2 手多, 再开 2 手空, 空单均价就是成交价
        account.place(order("sell", 4), snap(bid_price1=100.0), NOW)
        position = account.positions[CONTRACT]
        self.assertEqual((position["qty"], position["avgPrice"]), (-2, 100.0))
        self.assertEqual((account.trades[-1]["close"], account.trades[-1]["open"]), (2, 2))
        self.assertEqual(account.trades[-1]["pnl"], -20.0)           # (100-101) x 10 x 2
        flat = account.flatten(CONTRACT, snap(ask_price1=98.0), NOW)
        self.assertEqual((flat["side"], flat["qty"]), ("buy", 2))
        self.assertEqual(account.trades[-1]["pnl"], 40.0)            # 空单 (100-98) x 10 x 2
        self.assertEqual(account.positions, {})
        self.assertAlmostEqual(account.realized, 40.0 - 20.0 + 40.0)
        self.assertAlmostEqual(account.cash, 1_000_000.0 + 60.0)
        with self.assertRaisesRegex(OrderError, "没有持仓"):
            account.flatten(CONTRACT, snap(), NOW)

    def test_summary_marks_to_last_price(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 2), snap(ask_price1=3000.0), NOW)
        account.mark({CONTRACT: snap(last_price=3010.0)})
        summary = account.summary()
        self.assertEqual(summary["positions"][0]["floatPnl"], 200.0)
        self.assertEqual(summary["account"]["margin"], 2 * 3010.0 * 10 * paper.MARGIN_RATE)
        self.assertEqual(summary["account"]["equity"], 1_000_200.0)
        self.assertEqual(summary["account"]["available"],
                         summary["account"]["equity"] - summary["account"]["margin"])


class FeeTest(unittest.TestCase):
    def test_ratio_fee_and_free_close_today(self):
        account = book()
        account.place(order("buy", 2), snap(ask_price1=3000.0), NOW)
        self.assertAlmostEqual(account.trades[-1]["fee"], 3000 * 10 * 2 * 1e-4)
        account.place(order("sell", 2), snap(bid_price1=3000.0), NOW)
        self.assertEqual(account.trades[-1]["fee"], 0.0)   # 平今免费

    def test_close_yesterday_first_across_trading_days(self):
        account = book()
        account.place(order("buy", 2), snap(datetime="2026-09-29 10:00:00.000000"),
                      datetime(2026, 9, 29, 10, 0, 1))
        account.place(order("buy", 1), snap(), NOW)
        self.assertEqual(account.positions[CONTRACT]["todayQty"], 1)
        # 卖 2 手: 先平昨 2 手(收费), 今仓 1 手留着
        account.place(order("sell", 2), snap(bid_price1=3000.0), NOW)
        self.assertAlmostEqual(account.trades[-1]["fee"], 3000 * 10 * 2 * 1e-4)
        self.assertEqual(account.positions[CONTRACT]["todayQty"], 1)
        account.place(order("sell", 1), snap(bid_price1=3000.0), NOW)
        self.assertEqual(account.trades[-1]["fee"], 0.0)

    def test_fixed_fee_and_unknown_product(self):
        dce = snap(contract="DCE.c2701", ask_price1=2300.0)
        account = book()
        account.place({**order("buy", 3), "symbol": "DCE.c2701"}, dce, NOW)
        self.assertAlmostEqual(account.trades[-1]["fee"], 3.6)
        other = snap(contract="SHFE.rb2701")
        account.place({**order("buy", 1), "symbol": "SHFE.rb2701"}, other, NOW)
        self.assertEqual(account.trades[-1]["fee"], 0.0)

    def test_load_fee_table_from_ranking_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            header = "交易所,产品代码,产品名称,范围,投机套保,乘数,价格,名义金额,开仓元,平昨元,平今元,隔夜往返元,日内往返元,收费方式\n"
            rows = ("上期所,fu,燃料油,模板,投机,10.0,4000.0,40000.0,4.0,4.0,0.0,8.0,4.0,比例\n"
                    "郑商所,CY,棉纱,模板,投机,5.0,20000.0,100000.0,1.0,1.0,0.5,2.0,1.5,固定\n"
                    "火星所,xx,未知,模板,投机,1,1,1,1,1,1,2,2,固定\n")
            for name in ("手续费最低品种排名_2026-01-01.csv", "手续费最低品种排名_2026-08-19.csv"):
                with open(os.path.join(directory, name), "w", encoding="utf-8-sig") as handle:
                    handle.write(header + (rows if "08-19" in name else ""))
            table = FeeTable.load(os.path.join(directory, "手续费最低品种排名_*.csv"))
        self.assertEqual(table.source, "手续费最低品种排名_2026-08-19.csv")   # 取最新一份
        self.assertEqual(len(table.rows), 2)
        self.assertAlmostEqual(table.spec("SHFE.fu2611")["open"], 1e-4)
        self.assertEqual(table.spec("CZCE.CY701"), {"mode": "fixed", "open": 1.0,
                                                    "closeYesterday": 1.0, "closeToday": 0.5})
        self.assertEqual(table.spec("KQ.m@SHFE.fu")["mode"], "ratio")
        self.assertIsNone(table.spec("SSE.600000"))
        self.assertEqual(FeeTable.load(os.path.join(directory, "none_*.csv")).rows, {})


class FundsTest(unittest.TestCase):
    def test_opening_funds_include_spread_loss_at_last_price(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                account = book(cash=4510., fees=FeeTable())
                quote = snap(ask_price1=3002., bid_price1=2998., last_price=3000.)
                with self.assertRaisesRegex(OrderError, "资金不足"):
                    account.place(order(side, 1), quote, NOW)
                self.assertEqual(account.positions, {})

    def test_marketable_limit_funds_use_actual_fill_and_last_price(self):
        for side, limit in (("buy", 3010.), ("sell", 2990.)):
            with self.subTest(side=side):
                account = book(cash=4505., fees=FeeTable())
                quote = snap(ask_price1=3000., bid_price1=3000., last_price=3000.)
                filled = account.place(order(side, 1, "limit", limit), quote, NOW)
                self.assertEqual((filled["status"], filled["fillPrice"]), ("filled", 3000.))
                self.assertEqual(account.summary()["account"]["available"], 5.)

    def test_open_needs_margin_but_closing_is_always_allowed(self):
        account = book(cash=10_000.0, fees=FeeTable())
        # 一手保证金 3000 x 10 x 15% = 4500; 两手可以, 三手不够
        account.place(order("buy", 2), snap(), NOW)
        with self.assertRaisesRegex(OrderError, "资金不足"):
            account.place(order("buy", 1), snap(), NOW)
        # 大跌之后权益不够保证金, 平仓照样可以
        account.mark({CONTRACT: snap(last_price=2700.0)})
        closed = account.place(order("sell", 2), snap(bid_price1=2700.0), NOW)
        self.assertEqual(closed["status"], "filled")
        self.assertEqual(account.cash, 10_000.0 - 6000.0)

    def test_reverse_checks_only_the_new_side(self):
        account = book(cash=10_000.0, fees=FeeTable())
        account.place(order("buy", 2), snap(), NOW)
        account.place(order("sell", 4), snap(bid_price1=3000.0), NOW)   # 平 2 开 2, 保证金 9000
        self.assertEqual(account.positions[CONTRACT]["qty"], -2)
        with self.assertRaisesRegex(OrderError, "资金不足"):
            account.place(order("sell", 5), snap(bid_price1=3000.0), NOW)


class LimitOrderTest(unittest.TestCase):
    def test_marketable_limit_fills_at_better_opposite_price(self):
        account = book()
        placed = account.place(order("buy", 1, "limit", 3005.0), snap(ask_price1=3001.0), NOW)
        self.assertEqual((placed["status"], placed["fillPrice"]), ("filled", 3001.0))

    def test_resting_order_needs_price_to_trade_through(self):
        account = book(fees=FeeTable())
        placed = account.place(order("buy", 1, "limit", 2990.0), snap(), NOW)
        self.assertEqual(placed["status"], "open")
        # 卖一刚好碰到限价: 不算成交(看不到排队)
        self.assertEqual(account.match({CONTRACT: snap(ask_price1=2990.0, last_price=2990.0)}, NOW), [])
        # 最新价穿过限价: 按限价成交(挂单是被动成交, 不按更好的价)
        changed = account.match({CONTRACT: snap(ask_price1=2991.0, last_price=2989.0)}, NOW)
        self.assertEqual([item["id"] for item in changed], [placed["id"]])
        self.assertEqual((placed["status"], placed["fillPrice"]), ("filled", 2990.0))
        self.assertEqual(account.positions[CONTRACT]["avgPrice"], 2990.0)

    def test_sell_limit_rests_then_fills_when_bid_passes(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 1), snap(), NOW)
        placed = account.place(order("sell", 1, "limit", 3010.0), snap(), NOW)
        self.assertEqual(placed["status"], "open")
        account.match({CONTRACT: snap(bid_price1=3011.0, last_price=3010.0)}, NOW)
        self.assertEqual(placed["fillPrice"], 3010.0)
        self.assertEqual(account.trades[-1]["pnl"], 100.0)

    def test_resting_order_waits_while_market_closed(self):
        account = book()
        placed = account.place(order("buy", 1, "limit", 2990.0), snap(), datetime(2026, 9, 30, 12, 0))
        self.assertEqual(placed["status"], "open")   # 休市也能挂单
        cheap = {CONTRACT: snap(ask_price1=2980.0, last_price=2980.0)}
        self.assertEqual(account.match(cheap, datetime(2026, 9, 30, 12, 5)), [])
        self.assertEqual(len(account.match(cheap, datetime(2026, 9, 30, 13, 30, 1))), 1)

    def test_limit_price_validation(self):
        account = book()
        with self.assertRaisesRegex(OrderError, "整数倍"):
            account.place(order("buy", 1, "limit", 2990.5), snap(), NOW)
        with self.assertRaisesRegex(OrderError, "涨停价"):
            account.place(order("buy", 1, "limit", 3400.0), snap(), NOW)
        with self.assertRaisesRegex(OrderError, "跌停价"):
            account.place(order("sell", 1, "limit", 2600.0), snap(), NOW)

    def test_cancel(self):
        account = book()
        placed = account.place(order("buy", 1, "limit", 2990.0), snap(), NOW)
        self.assertEqual(account.cancel(placed["id"], NOW)["status"], "cancelled")
        with self.assertRaisesRegex(OrderError, "已经成交或撤销"):
            account.cancel(placed["id"], NOW)
        with self.assertRaisesRegex(OrderError, "找不到"):
            account.cancel("O999", NOW)
        self.assertEqual(account.match({CONTRACT: snap(ask_price1=2900.0)}, NOW), [])

    def test_resting_order_rejected_when_funds_gone_at_fill_time(self):
        account = book(cash=10_000.0, fees=FeeTable())
        placed = account.place(order("buy", 2, "limit", 2990.0), snap(), NOW)
        account.cash = 1_000.0   # 挂单期间别的仓位亏掉了资金
        account.match({CONTRACT: snap(ask_price1=2980.0)}, NOW)
        self.assertEqual(placed["status"], "rejected")
        self.assertIn("资金不足", placed["reason"])
        self.assertEqual(account.positions, {})

    def test_history_is_trimmed_but_open_orders_kept(self):
        account = book(fees=FeeTable())
        resting = account.place(order("buy", 1, "limit", 2800.0), snap(), NOW)
        with patch.object(paper, "MAX_FINISHED_ORDERS", 3), patch.object(paper, "MAX_TRADES", 4):
            for _ in range(5):
                account.place(order("buy", 1), snap(), NOW)
                account.place(order("sell", 1), snap(), NOW)
        self.assertIn(resting, account.orders)
        self.assertEqual(len([o for o in account.orders if o["status"] != "open"]), 3)
        self.assertEqual(len(account.trades), 4)


class StopTest(unittest.TestCase):
    def test_set_validates_side_tick_and_position(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 2), snap(), NOW)                       # 多 2 @3000, 最新价 3000
        account.set_stops(CONTRACT, 3050.0, 2950.0, snap())
        self.assertEqual((account.positions[CONTRACT]["tp"], account.positions[CONTRACT]["sl"]), (3050.0, 2950.0))
        self.assertEqual(account.summary()["positions"][0]["tp"], 3050.0)
        self.assertEqual(account.summary()["positions"][0]["multiplier"], 10)   # 图上划线算预估盈亏用
        with self.assertRaisesRegex(OrderError, "多单止盈价要高于最新价 3000"):
            account.set_stops(CONTRACT, 2990.0, None, snap())
        with self.assertRaisesRegex(OrderError, "多单止损价要低于最新价 3000"):
            account.set_stops(CONTRACT, None, 3000.0, snap())
        with self.assertRaisesRegex(OrderError, "整数倍"):
            account.set_stops(CONTRACT, 3050.5, None, snap())
        with self.assertRaisesRegex(OrderError, "没有最新价"):
            account.set_stops(CONTRACT, 3050.0, None, snap(last_price=float("nan")))
        with self.assertRaisesRegex(OrderError, "没有持仓"):
            account.set_stops("SHFE.fu2609", 3050.0, None, snap())
        self.assertEqual(account.positions[CONTRACT]["sl"], 2950.0)      # 被拒的设置不改原来的
        account.set_stops(CONTRACT, None, 2960.0, snap())                 # 只设止损: 止盈取消
        self.assertNotIn("tp", account.positions[CONTRACT])
        account.set_stops(CONTRACT, None, None, None)                     # 取消不用报价
        self.assertEqual({"tp", "sl"} & set(account.positions[CONTRACT]), set())

    def test_stop_loss_touch_closes_at_market(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 2), snap(), NOW)
        account.set_stops(CONTRACT, 3100.0, 2950.0, snap())
        self.assertEqual(account.trigger_stops({CONTRACT: snap(last_price=2951.0, bid_price1=2950.0)}, NOW), [])
        placed = account.trigger_stops({CONTRACT: snap(last_price=2950.0, bid_price1=2949.0)}, NOW)
        self.assertEqual([(item["side"], item["qty"], item["reason"]) for item in placed], [("sell", 2, "止损")])
        self.assertEqual(placed[0]["fillPrice"], 2949.0)                  # 碰到就触发, 按对手价成交
        self.assertEqual(account.positions, {})
        self.assertEqual(account.trades[-1]["pnl"], (2949.0 - 3000.0) * 10 * 2)

    def test_short_take_profit_and_stops_follow_the_position(self):
        account = book(fees=FeeTable())
        account.place(order("sell", 2), snap(), NOW)                      # 空 2 @2999
        with self.assertRaisesRegex(OrderError, "空单止盈价要低于最新价"):
            account.set_stops(CONTRACT, 3010.0, None, snap())
        account.set_stops(CONTRACT, 2900.0, 3050.0, snap())
        account.place(order("sell", 1), snap(), NOW)                      # 加仓、减仓都保留
        account.place(order("buy", 2), snap(), NOW)
        self.assertEqual((account.positions[CONTRACT]["qty"], account.positions[CONTRACT]["tp"]), (-1, 2900.0))
        placed = account.trigger_stops({CONTRACT: snap(last_price=2900.0, ask_price1=2901.0)}, NOW)
        self.assertEqual([(item["side"], item["qty"], item["reason"]) for item in placed], [("buy", 1, "止盈")])
        # 反手: 新方向的持仓不带旧的止盈止损
        account.place(order("buy", 1), snap(), NOW)
        account.set_stops(CONTRACT, 3100.0, 2900.0, snap())
        account.place(order("sell", 3), snap(), NOW)
        self.assertEqual(account.positions[CONTRACT]["qty"], -2)
        self.assertEqual({"tp", "sl"} & set(account.positions[CONTRACT]), set())

    def test_failed_close_retries_and_waits_for_the_session(self):
        account = book(fees=FeeTable())
        account.place(order("buy", 1), snap(), NOW)
        account.set_stops(CONTRACT, None, 2950.0, snap())
        lunch = datetime(2026, 9, 30, 12, 0, 0)
        self.assertEqual(account.trigger_stops({CONTRACT: snap(last_price=2940.0)}, lunch), [])   # 午休不触发
        self.assertEqual(account.stop_errors, {})
        limit_down = snap(last_price=2700.0, bid_price1=float("nan"), lower_limit=2700.0)
        self.assertEqual(account.trigger_stops({CONTRACT: limit_down}, NOW), [])
        self.assertIn("止损已触发", account.summary()["positions"][0]["stopError"])
        self.assertEqual(account.positions[CONTRACT]["sl"], 2950.0)       # 留着, 下一轮再试
        placed = account.trigger_stops({CONTRACT: snap(last_price=2710.0, bid_price1=2709.0)}, NOW)
        self.assertEqual(placed[0]["fillPrice"], 2709.0)
        self.assertEqual(account.stop_errors, {})

    def test_market_order_carries_stops_onto_the_position(self):
        account = book(fees=FeeTable())
        with self.assertRaisesRegex(OrderError, "多单止盈价要高于最新价 3000"):
            account.place(order("buy", 2) | {"tp": 2990.0}, snap(), NOW)
        with self.assertRaisesRegex(OrderError, "整数倍"):
            account.place(order("buy", 2) | {"sl": 2950.5}, snap(), NOW)
        self.assertEqual((account.orders, account.positions), ([], {}))  # 被拒的不下单
        placed = account.place(order("buy", 2) | {"tp": 3100.0, "sl": 2950.0}, snap(), NOW)
        self.assertEqual((placed["tp"], placed["sl"]), (3100.0, 2950.0))
        self.assertEqual((account.positions[CONTRACT]["tp"], account.positions[CONTRACT]["sl"]), (3100.0, 2950.0))
        # 只减仓或平仓的委托不能带; 反手的可以, 按新方向校验
        with self.assertRaisesRegex(OrderError, "只减仓或平仓"):
            account.place(order("sell", 2) | {"sl": 3050.0}, snap(), NOW)
        account.place(order("sell", 3) | {"tp": 2900.0, "sl": 3050.0}, snap(), NOW)
        self.assertEqual(account.positions[CONTRACT]["qty"], -1)
        self.assertEqual((account.positions[CONTRACT]["tp"], account.positions[CONTRACT]["sl"]), (2900.0, 3050.0))
        # 加仓只覆盖填了的那一项
        account.place(order("sell", 1) | {"tp": 2950.0}, snap(), NOW)
        self.assertEqual((account.positions[CONTRACT]["tp"], account.positions[CONTRACT]["sl"]), (2950.0, 3050.0))
        # 不带的委托(含平仓)不碰持仓的止盈止损
        account.place(order("buy", 1), snap(), NOW)
        self.assertEqual((account.positions[CONTRACT]["qty"], account.positions[CONTRACT]["tp"]), (-1, 2950.0))

    def test_limit_order_stops_attach_when_it_fills(self):
        account = book(fees=FeeTable())
        with self.assertRaisesRegex(OrderError, "多单止损价要低于委托价 2990"):
            account.place(order("buy", 1, "limit", 2990.0) | {"sl": 2995.0}, snap(), NOW)
        # 当场成交的限价单还要对最新价校验: 止损 3002 低于限价 3005, 但高于最新价 3000
        with self.assertRaisesRegex(OrderError, "多单止损价要低于最新价 3000"):
            account.place(order("buy", 1, "limit", 3005.0) | {"sl": 3002.0}, snap(ask_price1=3001.0), NOW)
        resting = account.place(order("buy", 1, "limit", 2990.0) | {"tp": 3050.0, "sl": 2980.0}, snap(), NOW)
        self.assertEqual((resting["status"], account.positions), ("open", {}))
        account.match({CONTRACT: snap(ask_price1=2991.0, last_price=2989.0)}, NOW)
        self.assertEqual(resting["status"], "filled")
        self.assertEqual((account.positions[CONTRACT]["tp"], account.positions[CONTRACT]["sl"]), (3050.0, 2980.0))
        # 成交时价格已经越过止损: 挂上之后紧接着触发
        account.flatten(CONTRACT, snap(), NOW)
        account.place(order("buy", 1, "limit", 2990.0) | {"sl": 2985.0}, snap(), NOW)
        gap = {CONTRACT: snap(ask_price1=2981.0, bid_price1=2980.0, last_price=2980.0)}
        account.match(gap, NOW)
        placed = account.trigger_stops(gap, NOW)
        self.assertEqual([(item["side"], item["reason"], item["fillPrice"]) for item in placed],
                         [("sell", "止损", 2980.0)])

    def test_resting_stops_skip_a_fill_that_only_reduces(self):
        account = book(fees=FeeTable())
        resting = account.place(order("sell", 1, "limit", 3010.0) | {"tp": 2950.0, "sl": 3060.0}, snap(), NOW)
        account.place(order("buy", 2), snap(), NOW)                       # 挂单期间开了多 2
        account.match({CONTRACT: snap(bid_price1=3011.0, last_price=3010.0)}, NOW)
        self.assertEqual(resting["status"], "filled")
        self.assertEqual(account.positions[CONTRACT]["qty"], 1)
        self.assertEqual({"tp", "sl"} & set(account.positions[CONTRACT]), set())   # 空单的止盈止损不挂到多单上


class RequestTest(unittest.TestCase):
    def test_normalize_request(self):
        good = paper.normalize_request({"symbol": " KQ.m@SHFE.fu ", "side": "buy", "qty": 2.0})
        self.assertEqual((good["symbol"], good["type"], good["qty"], good["price"]),
                         ("KQ.m@SHFE.fu", "market", 2, None))
        self.assertEqual((good["tp"], good["sl"]), (None, None))
        stops = paper.normalize_request({"symbol": CONTRACT, "side": "buy", "qty": 1, "tp": "3100", "sl": ""})
        self.assertEqual((stops["tp"], stops["sl"]), (3100.0, None))
        bad = [{"symbol": "x/../y", "side": "buy", "qty": 1},
               {"symbol": CONTRACT, "side": "buy", "qty": 1, "tp": "abc"},
               {"symbol": CONTRACT, "side": "buy", "qty": 1, "sl": -5},
               {"symbol": CONTRACT, "side": "long", "qty": 1},
               {"symbol": CONTRACT, "side": "buy", "qty": 1.5},
               {"symbol": CONTRACT, "side": "buy", "qty": True},
               {"symbol": CONTRACT, "side": "buy", "qty": float("nan")},
               {"symbol": CONTRACT, "side": "buy", "qty": paper.MAX_ORDER_QTY + 1},
               {"symbol": CONTRACT, "side": "buy", "qty": 1, "type": "stop"},
               {"symbol": CONTRACT, "side": "buy", "qty": 1, "type": "limit"},
               "not a dict"]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                paper.normalize_request(raw)


class FakeApi:
    """模拟交易用到的 SDK 方法: 合约存在性查询、报价对象与 wait_update。"""

    def __init__(self, quotes):
        self.quotes = quotes
        self.gets = []
        self.listings = 0
        self.waits = 0

    def query_quotes(self, exchange_id=None, product_id=None, **kwargs):
        self.listings += 1
        return list(self.quotes)

    def get_quote(self, symbol):
        self.gets.append(symbol)
        return self.quotes[symbol]

    def wait_update(self, deadline=None):
        self.waits += 1
        return True


class FakeManager:
    def __init__(self, api):
        self.api = api
        self.error = None
        self.calls = 0

    async def query(self, fn, timeout=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return fn(self.api)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "paper", "account.json")
        self.quotes = {"KQ.m@SHFE.fu": raw_quote(ins_class="CONT", instrument_name="燃油主连",
                                                  underlying_symbol=CONTRACT),
                       CONTRACT: raw_quote()}
        self.api = FakeApi(self.quotes)
        self.manager = FakeManager(self.api)
        self.service = self.make_service()

    def tearDown(self):
        self.directory.cleanup()

    def make_service(self):
        return PaperService(lambda: self.manager, PaperStore(lambda: self.path), FEES, clock=lambda: NOW)

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_state_resolves_main_contract_and_reports_session(self):
        state = self.run_async(self.service.state("KQ.m@SHFE.fu"))
        self.assertEqual(state["contract"], CONTRACT)
        self.assertEqual((state["quote"]["ask"], state["quote"]["open"]), (3000.0, True))
        self.assertEqual(state["fee"]["mode"], "ratio")
        self.assertIsNone(state["error"])
        self.assertEqual(state["account"]["equity"], paper.DEFAULT_CASH)
        # 确认过存在之后, 轮询不再排查询任务, 报价由采集循环刷新
        self.run_async(self.service.state("KQ.m@SHFE.fu"))
        self.assertEqual(self.manager.calls, 1)

    def test_unknown_symbol_is_not_requeried_every_poll(self):
        state = self.run_async(self.service.state("SHFE.fu9999"))
        self.assertIn("不存在", state["error"])
        self.assertIsNone(state["quote"])
        self.assertNotIn("SHFE.fu9999", self.api.gets)   # 不存在的代码绝不能 get_quote
        self.run_async(self.service.state("SHFE.fu9999"))
        self.assertEqual(self.manager.calls, 1)

    def test_place_persists_and_reloads(self):
        placed = self.run_async(self.service.place(
            {"symbol": "KQ.m@SHFE.fu", "side": "buy", "qty": 2, "clientId": "a"}))
        self.assertEqual((placed["contract"], placed["status"]), (CONTRACT, "filled"))
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["positions"][CONTRACT]["qty"], 2)
        state = self.run_async(self.make_service().state("KQ.m@SHFE.fu"))
        self.assertEqual(state["positions"][0]["qty"], 2)
        self.assertEqual([trade["contract"] for trade in state["contractTrades"]], [CONTRACT])

    def test_order_errors_come_back_as_order_error(self):
        with self.assertRaisesRegex(OrderError, "不存在"):
            self.run_async(self.service.place({"symbol": "SHFE.fu9999", "side": "buy", "qty": 1}))
        self.manager.error = RuntimeError("合约查询超时(12 秒)")
        with self.assertRaisesRegex(OrderError, "以委托与成交记录为准"):
            self.run_async(self.service.place({"symbol": CONTRACT, "side": "buy", "qty": 1}))

    def test_cold_quote_waits_one_round(self):
        self.quotes[CONTRACT] = raw_quote(datetime="")
        with self.assertRaisesRegex(OrderError, "本时段还没有新行情"):
            self.run_async(self.service.place({"symbol": CONTRACT, "side": "buy", "qty": 1}))
        self.assertEqual(self.api.waits, 1)

    def test_on_loop_matches_resting_orders_and_refreshes_after_reconnect(self):
        placed = self.run_async(self.service.place(
            {"symbol": CONTRACT, "side": "buy", "qty": 1, "type": "limit", "price": 2990.0}))
        self.assertEqual(placed["status"], "open")
        self.quotes[CONTRACT].ask_price1 = 2985.0   # 报价对象就地更新, 与 TqSdk 一样
        self.service.on_loop(self.api)
        self.assertEqual(placed["status"], "filled")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["positions"][CONTRACT]["qty"], 1)
        # 连接重建: 新 api 上重新取报价对象, 持仓合约照样跟踪
        other = FakeApi(self.quotes)
        self.service.on_loop(other)
        self.assertEqual(other.gets, [CONTRACT])
        self.assertEqual(other.listings, 0)   # 采集循环里不查合约服务

    def test_contracts_from_file_are_verified_before_get_quote(self):
        os.makedirs(os.path.dirname(self.path))
        positions = {CONTRACT: {"qty": 1, "avgPrice": 3000.0, "multiplier": 10, "todayQty": 0,
                                "tradingDay": "2026-09-29"},
                     "SHFE.fu9999": {"qty": 1, "avgPrice": 3000.0, "multiplier": 10, "todayQty": 0,
                                     "tradingDay": "2026-09-29"}}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"initialCash": 1e6, "cash": 1e6, "positions": positions}, handle)
        with patch("builtins.print"):
            self.service.on_loop(self.api)
            self.assertEqual(self.api.gets, [CONTRACT])    # 查不到的代码绝不 get_quote
            listings = self.api.listings
            self.service.on_loop(self.api)
        self.assertEqual(self.api.listings, listings)       # 确认过/刚失败过的都不再查
        self.assertEqual(self.service._book.marks, {CONTRACT: 3000.0})

    def test_reset_during_loop_is_not_overwritten_by_old_account(self):
        placed = self.run_async(self.service.place(
            {"symbol": CONTRACT, "side": "buy", "qty": 1, "type": "limit", "price": 2990.0}))
        self.quotes[CONTRACT].ask_price1 = 2985.0
        resolve = self.service._resolve

        def reset_midway(api, symbol):
            self.service.reset(300_000)   # 采集线程读报价时, HTTP 线程重置了账户
            return resolve(api, symbol)

        with patch.object(self.service, "_resolve", side_effect=reset_midway):
            self.service.on_loop(self.api)
        self.assertEqual(placed["status"], "open")   # 旧账户的挂单没有被拿去撮合
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["initialCash"], saved["positions"], saved["orders"]), (300_000, {}, []))
        self.assertEqual(saved["seq"], 1)             # 接着旧编号: 下一笔是 O2, 不会再出一个 O1

    def test_stops_persist_and_trigger_on_the_loop(self):
        self.run_async(self.service.place({"symbol": "KQ.m@SHFE.fu", "side": "buy", "qty": 1}))
        result = self.service.set_stops("KQ.m@SHFE.fu", None, 2990)     # 主连按标的月份合约的持仓
        self.assertEqual((result["contract"], result["tp"], result["sl"]), (CONTRACT, None, 2990.0))
        with self.assertRaisesRegex(OrderError, "止损价要是正数"):
            self.service.set_stops(CONTRACT, None, "abc")
        state = self.run_async(self.make_service().state("KQ.m@SHFE.fu"))
        self.assertEqual(state["positions"][0]["sl"], 2990.0)
        self.quotes[CONTRACT].last_price = 2990.0
        self.quotes[CONTRACT].bid_price1 = 2989.0
        self.service.on_loop(self.api)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["positions"], {})
        self.assertEqual((saved["orders"][-1]["reason"], saved["orders"][-1]["fillPrice"]), ("止损", 2989.0))

    def test_order_stops_go_through_the_service(self):
        placed = self.run_async(self.service.place(
            {"symbol": "KQ.m@SHFE.fu", "side": "buy", "qty": 1, "tp": 3100, "sl": "2950"}))
        self.assertEqual((placed["contract"], placed["tp"], placed["sl"]), (CONTRACT, 3100.0, 2950.0))
        state = self.run_async(self.make_service().state("KQ.m@SHFE.fu"))
        self.assertEqual((state["positions"][0]["tp"], state["positions"][0]["sl"]), (3100.0, 2950.0))
        with self.assertRaisesRegex(OrderError, "止盈价要是正数"):
            self.run_async(self.service.place({"symbol": CONTRACT, "side": "buy", "qty": 1, "tp": 0}))

    def test_watch_expires(self):
        self.run_async(self.service.state(CONTRACT))
        self.api.gets.clear()
        self.service.on_loop(self.api)
        self.assertEqual(self.service._watch.keys(), {CONTRACT})
        with patch.object(paper.time, "monotonic", return_value=paper.time.monotonic() + 3600):
            self.service.on_loop(self.api)
        self.assertEqual(self.service._watch, {})

    def test_cancel_and_reset(self):
        placed = self.run_async(self.service.place(
            {"symbol": CONTRACT, "side": "sell", "qty": 1, "type": "limit", "price": 3050.0}))
        self.assertEqual(self.service.cancel(placed["id"])["status"], "cancelled")
        summary = self.service.reset(200_000)
        self.assertEqual(summary["account"]["equity"], 200_000)
        state = self.run_async(self.service.state(CONTRACT))
        self.assertEqual((state["orders"], state["trades"]), ([], []))
        with self.assertRaisesRegex(OrderError, "初始资金"):
            self.service.reset(1)

    def test_corrupt_file_is_kept_aside(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with patch("builtins.print"):
            state = self.run_async(self.service.state(CONTRACT))
        self.assertEqual(state["account"]["equity"], paper.DEFAULT_CASH)
        leftovers = os.listdir(os.path.dirname(self.path))
        self.assertTrue(any(name.startswith("account.json.corrupt-") for name in leftovers))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = os.path.join(self.directory.name, "account.json")
        self.manager = FakeManager(FakeApi({CONTRACT: raw_quote()}))
        service = PaperService(lambda: self.manager, PaperStore(lambda: path), FEES, clock=lambda: NOW)
        self.patch = patch.object(server, "paper", service)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_rejections_are_400(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.paper_order({"symbol": CONTRACT, "side": "buy", "qty": 0}))
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            server.paper_cancel("O1")
        self.assertEqual(caught.exception.detail, "找不到这笔委托")
        with self.assertRaises(HTTPException):
            asyncio.run(server.paper_flatten(CONTRACT))
        with self.assertRaises(HTTPException):
            server.paper_reset(-5)
        with self.assertRaises(HTTPException) as caught:
            server.paper_stops({"symbol": CONTRACT, "tp": 3100, "sl": None})
        self.assertEqual(caught.exception.detail, "这个合约没有持仓")

    def test_order_round_trip(self):
        filled = asyncio.run(server.paper_order({"symbol": CONTRACT, "side": "buy", "qty": 1}))
        self.assertEqual(filled["status"], "filled")
        with self.assertRaises(HTTPException) as caught:
            server.paper_stops({"symbol": CONTRACT, "tp": 2990, "sl": None})
        self.assertIn("止盈价要高于最新价", caught.exception.detail)
        self.assertEqual(server.paper_stops({"symbol": CONTRACT, "tp": 3100, "sl": ""})["tp"], 3100.0)
        flat = asyncio.run(server.paper_flatten(CONTRACT))
        self.assertEqual((flat["side"], flat["status"]), ("sell", "filled"))


class LoopHookTest(unittest.TestCase):
    def test_hook_errors_do_not_escape_and_busy_is_cleared(self):
        manager = ingest.FeedManager()
        calls = []
        manager.loop_hooks = [lambda api: (_ for _ in ()).throw(RuntimeError("boom")),
                              lambda api: calls.append(api)]
        with patch("builtins.print"):
            manager._run_loop_hooks("api")
        self.assertEqual(calls, ["api"])
        self.assertIsNone(manager._busy_since)

    def test_hooks_skipped_while_rebuilding(self):
        manager = ingest.FeedManager()
        calls = []
        manager.loop_hooks = [calls.append]
        manager._rebuild_api.set()
        manager._run_loop_hooks("api")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
