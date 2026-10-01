# -*- coding: utf-8 -*-
"""模拟交易: 按实时报价撮合的本地模拟账户, 不走 TqSdk 的交易接口。

为什么不用 TqSim / 快期模拟: TqSim 的账户只在内存里, 回收订阅、断线重连都会重建 TqApi,
账户随之清零; 快期模拟要在采集连接上下单, 一个出错的调用就可能拖停整条行情连接
(见 ingest.listed_symbols), 连带买卖量采集 —— 那些数据丢了补不回来。所以这里只**读**报价。

分工:

- 纯逻辑: ``market_status`` / ``trading_day`` / ``FeeTable`` / ``PaperBook``。输入报价快照
  和时间, 输出订单、成交与账户; 不碰 SDK、不读写文件、不加锁, 测试直接喂数据。
- ``PaperStore``: 整个账户一个 JSON(``data/paper/account.json``), 临时文件 + ``os.replace``
  原子替换。委托、成交、资金在同一个文件里, 不会出现"成交记下了、资金没扣"的半截状态。
  放在 data/ 下, 定期快照备份会一并带上。
- ``PaperService``: HTTP 层与采集线程之间的胶水。下单经 ``FeedManager.query`` 排进采集线程
  拿最新报价撮合; 挂单由采集循环每轮回调 ``on_loop`` 检查。

撮合规则(500ms 快照精度, 看不到排队, 结果只能当参考):

- 市价单: 买按卖一、卖按买一, 整笔一次成交; 对手价缺失(涨跌停)或一档量不够整笔就拒单。
- 限价单: 下单时已够得着对手价(买: 卖一 <= 限价)且一档量够, 按对手价立即成交; 否则挂着,
  之后价格**穿过**限价(买: 卖一或最新价 < 限价)才按限价成交, 只碰到不算 —— 没有排队信息,
  碰到就算成交会把结果算得太好。
- 只在交易时段内、且报价是本时段的才撮合: 节假日没有新报价, 旧报价不能拿来成交。
- 净持仓: 每个合约只有一个带符号的手数, 反向成交先平后开(反手)。平仓先平昨再平今。
- 保证金按 ``MARGIN_RATE`` 乘最新价估算; 手续费来自 docs/ 下的手续费率表, 查不到按 0 计。
"""
from __future__ import annotations

import calendar
import csv
import glob
import json
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from catalog import split_cont_symbol, split_product
from ingest import JOB_TIMEOUT_SEC, listed_symbols, validate_symbol

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))   # 北京时间没有夏令时; 不用 zoneinfo, Windows 上没有时区库
DEFAULT_CASH = 1_000_000.0
MIN_CASH, MAX_CASH = 10_000.0, 1e10
MARGIN_RATE = 0.15        # 保证金率估算值: 交易所 5%~12% 再加期货公司上浮, 实际按品种不同
MAX_ORDER_QTY = 500
# 开盘前集合竞价(8:55、20:55)的报价也算本时段的新报价。
AUCTION_LEAD = timedelta(minutes=15)
# 页面不再请求后, 这个合约的报价还跟踪多久(持仓与挂单合约一直跟踪)。
WATCH_TTL_SEC = 30
# 合约校验失败后多久再试: 页面每秒轮询, 不能每次都去合约服务查一个写错的代码。
VERIFY_RETRY_SEC = 30
COLD_WAIT_SEC = 1.0       # 第一次订阅的合约等报价下发的时间
MAX_TRADES = 5000
MAX_FINISHED_ORDERS = 200
STORE_VERSION = 1
FEE_PATTERN = os.path.join(BASE_DIR, "docs", "手续费最低品种排名_*.csv")
EXCHANGE_CODES = {"中金所": "CFFEX", "郑商所": "CZCE", "大商所": "DCE",
                  "广期所": "GFEX", "能源中心": "INE", "上期所": "SHFE"}


class OrderError(ValueError):
    """下单/撤单被拒; 消息直接给页面显示。"""


def _num(value):
    """NaN/±inf/非数字一律回 None: NaN 不是合法 JSON。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value):
    """价格只认正数: TqSdk 没有对手盘时给 NaN, 个别字段缺省是 0。"""
    number = _num(value)
    return number if number is not None and number > 0 else None


def beijing_now() -> datetime:
    return datetime.now(CST).replace(tzinfo=None)


def parse_quote_time(text) -> datetime | None:
    """TqSdk 报价时间 ``2026-09-30 10:00:00.500000``(北京时间) -> naive datetime。"""
    value = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def chart_time(moment: datetime) -> int:
    """北京时间 -> 图表时间轴秒数: 与 indicator.TZ_SHIFT_S 同口径, 把北京时间当 UTC 显示。"""
    return calendar.timegm(moment.timetuple())


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def trading_day(moment: datetime) -> date:
    """所属交易日: 18 点以后算下一天, 周末顺延到周一(与 TqSdk 口径一致, 不含节假日)。"""
    day = moment.date() + timedelta(days=1 if moment.hour >= 18 else 0)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _seconds(text) -> int | None:
    try:
        hours, minutes, seconds = (int(part) for part in str(text).split(":"))
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def session_start(trading_time, now: datetime) -> datetime | None:
    """now 所在交易时段块(整段日盘或整段夜盘)的开始时刻; 不在任何时段内返回 None。

    夜盘跨零点时写成 ``21:00:00``-``26:30:00``, 所以昨天起算的夜盘也要看。周末不开盘:
    时段的起算日必须是周一到周五(周五夜盘延续到周六凌晨时起算日仍是周五)。
    节假日这里认不出来, 靠报价时间兜底(见 market_status)。
    """
    if not trading_time:
        return None
    for anchor in (now.date(), now.date() - timedelta(days=1)):
        if anchor.weekday() >= 5:
            continue
        base = datetime.combine(anchor, datetime.min.time())
        for key in ("day", "night"):
            periods = []
            for period in trading_time.get(key) or []:
                if len(period) < 2:
                    continue
                start, end = _seconds(period[0]), _seconds(period[1])
                if start is not None and end is not None and start < end:
                    periods.append((start, end))
            if not periods:
                continue
            block = base + timedelta(seconds=min(start for start, _ in periods))
            for start, end in periods:
                if base + timedelta(seconds=start) <= now < base + timedelta(seconds=end):
                    return block
    return None


def market_status(quote: dict, now: datetime) -> tuple[bool, str]:
    """能不能撮合: (是否可成交, 不可成交的原因)。

    光看时钟不够: 节假日、夜盘取消的晚上时钟也落在时段里, 盘口却是上一个交易日的旧值。
    所以还要求报价时间落在本时段块之内(含开盘前的集合竞价)。
    """
    start = session_start(quote.get("tradingTime"), now)
    if start is None:
        return False, "当前不在交易时段"
    moment = parse_quote_time(quote.get("datetime"))
    if moment is None or moment < start - AUCTION_LEAD:
        return False, "本时段还没有新行情(可能休市)"
    return True, ""


def _trading_time(value) -> dict:
    """TqSdk 的 TradingTime 对象(或测试里的 dict) -> ``{"day": [[起, 止], ...], "night": [...]}``。"""
    result = {}
    for key in ("day", "night"):
        periods = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        result[key] = [[str(item) for item in period] for period in (periods or [])]
    return result


def quote_snapshot(contract: str, quote) -> dict:
    """把 Quote 对象压成撮合与页面要用的字段; 缺数据留 None。"""
    multiplier = _num(getattr(quote, "volume_multiple", None))
    return {
        "contract": contract,
        "name": str(getattr(quote, "instrument_name", "") or contract),
        "insClass": str(getattr(quote, "ins_class", "") or ""),
        "datetime": str(getattr(quote, "datetime", "") or ""),
        "last": _price(getattr(quote, "last_price", None)),
        "bid": _price(getattr(quote, "bid_price1", None)),
        "bidVolume": _num(getattr(quote, "bid_volume1", None)),
        "ask": _price(getattr(quote, "ask_price1", None)),
        "askVolume": _num(getattr(quote, "ask_volume1", None)),
        "upper": _price(getattr(quote, "upper_limit", None)),
        "lower": _price(getattr(quote, "lower_limit", None)),
        "priceTick": _price(getattr(quote, "price_tick", None)),
        "priceDecs": int(getattr(quote, "price_decs", 0) or 0),
        "multiplier": multiplier if multiplier else None,
        "tradingTime": _trading_time(getattr(quote, "trading_time", None)),
        "expired": bool(getattr(quote, "expired", False)),
    }


class FeeTable:
    """品种 -> 手续费。比例收费存费率(乘成交额), 固定收费存元/手。

    数据来自 docs/fee_rate_ranking.py 生成的手续费率表(取文件名最新的一份)。表里只有按某个
    参考价折算好的"单手元", 比例收费的品种用 单手元 / 名义金额 还原费率; 按手数的零头
    (如股指的 0.01 元)因此被折进费率, 误差可以忽略。
    """

    def __init__(self, rows: dict | None = None, source: str | None = None):
        self.rows = rows or {}
        self.source = source

    @classmethod
    def load(cls, pattern: str = FEE_PATTERN) -> "FeeTable":
        paths = sorted(glob.glob(pattern))
        if not paths:
            return cls()
        rows = {}
        try:
            with open(paths[-1], "r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    exchange = EXCHANGE_CODES.get((row.get("交易所") or "").strip())
                    product = (row.get("产品代码") or "").strip().lower()
                    notional = _num(row.get("名义金额"))
                    values = [_num(row.get(key)) for key in ("开仓元", "平昨元", "平今元")]
                    if not exchange or not product or any(value is None for value in values):
                        continue
                    ratio = (row.get("收费方式") or "").strip() == "比例"
                    if ratio and not notional:
                        continue
                    scale = notional if ratio else 1.0
                    rows[(exchange, product)] = {
                        "mode": "ratio" if ratio else "fixed",
                        "open": values[0] / scale,
                        "closeYesterday": values[1] / scale,
                        "closeToday": values[2] / scale,
                    }
        except (OSError, csv.Error) as exc:
            print(f"[paper] 读取手续费率表失败: {exc}", flush=True)
            return cls()
        return cls(rows, os.path.basename(paths[-1]))

    def spec(self, contract: str) -> dict | None:
        parts = split_product(contract)
        return self.rows.get((parts[0], parts[1].lower())) if parts else None

    def fee(self, contract: str, price: float, multiplier: float, opening: int,
            close_yesterday: int, close_today: int) -> float:
        spec = self.spec(contract)
        if spec is None:
            return 0.0
        per_lot = price * multiplier if spec["mode"] == "ratio" else 1.0
        return per_lot * (spec["open"] * opening + spec["closeYesterday"] * close_yesterday
                          + spec["closeToday"] * close_today)


def normalize_request(raw) -> dict:
    """校验页面提交的委托; 不合法抛 OrderError(ValueError)。"""
    if not isinstance(raw, dict):
        raise OrderError("委托格式不对")
    symbol = validate_symbol(str(raw.get("symbol") or ""))
    side = raw.get("side")
    if side not in ("buy", "sell"):
        raise OrderError("方向只能是买或卖")
    kind = raw.get("type") or "market"
    if kind not in ("market", "limit"):
        raise OrderError("委托类型只能是市价或限价")
    qty = raw.get("qty")
    qty = _num(qty) if isinstance(qty, (int, float)) and not isinstance(qty, bool) else None
    if qty is None or qty != int(qty) or not 1 <= qty <= MAX_ORDER_QTY:
        raise OrderError(f"手数必须是 1~{MAX_ORDER_QTY} 的整数")
    price = None
    if kind == "limit":
        price = _price(raw.get("price"))
        if price is None:
            raise OrderError("限价单要填价格")
    return {"symbol": symbol, "side": side, "type": kind, "qty": int(qty), "price": price,
            "clientId": str(raw.get("clientId") or "")[:64]}


class PaperBook:
    """模拟账户的全部状态与规则(纯逻辑: 不加锁、不读写文件、不碰 SDK)。

    ``cash`` 是静态权益 = 初始资金 + 平仓盈亏 - 手续费; 动态权益再加各持仓的浮动盈亏。
    持仓的 ``todayQty`` 是其中今天开的手数(算平今手续费用), 换交易日时清零。
    """

    def __init__(self, state: dict | None = None, fees: FeeTable | None = None):
        state = state or {}
        self.initial_cash = float(state.get("initialCash", DEFAULT_CASH))
        self.cash = float(state.get("cash", self.initial_cash))
        self.realized = float(state.get("realizedPnl", 0.0))
        self.fees_paid = float(state.get("fees", 0.0))
        self.positions = {contract: dict(pos) for contract, pos in (state.get("positions") or {}).items()
                          if pos.get("qty")}
        self.orders = [dict(order) for order in state.get("orders") or []]
        self.trades = [dict(trade) for trade in state.get("trades") or []]
        self.seq = int(state.get("seq", 0))
        self.fees = fees if fees is not None else FeeTable()
        self.marks: dict[str, float] = {}   # 合约 -> 最新价(只在内存里, 重启后等报价补上)

    def to_dict(self) -> dict:
        return {"version": STORE_VERSION, "initialCash": self.initial_cash, "cash": self.cash,
                "realizedPnl": self.realized, "fees": self.fees_paid, "positions": self.positions,
                "orders": self.orders, "trades": self.trades, "seq": self.seq}

    def _next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"

    # ---------- 下单 / 撤单 ----------

    def place(self, request: dict, quote: dict | None, now: datetime) -> dict:
        """下单: 市价单当场成交或抛 OrderError; 限价单能成交就成交, 否则挂单。返回订单。

        同一个 clientId 只下一次: 双击、超时重试都拿回第一次的结果。
        """
        client_id = request.get("clientId") or ""
        if client_id:
            for order in self.orders:
                if order.get("clientId") == client_id:
                    return order
        side, qty, kind = request["side"], request["qty"], request["type"]
        self._check_tradable(quote)
        order = {"id": "", "clientId": client_id, "symbol": request.get("symbol") or quote["contract"],
                 "contract": quote["contract"], "side": side, "qty": qty, "type": kind,
                 "price": request.get("price"), "status": "open", "reason": "",
                 "createdAt": _stamp(now), "fillPrice": None, "filledAt": None}
        open_now, reason = market_status(quote, now)
        if kind == "market":
            if not open_now:
                raise OrderError(reason)
            price = self._taker_price(side, qty, quote)
            self._check_funds(quote["contract"], side, qty, price, quote, now)
        else:
            price = self._check_limit_price(order["price"], quote)
            order["price"] = price
            self._check_funds(quote["contract"], side, qty, price, quote, now)
            opposite = quote.get("ask") if side == "buy" else quote.get("bid")
            volume = quote.get("askVolume") if side == "buy" else quote.get("bidVolume")
            crosses = opposite is not None and (opposite <= price if side == "buy" else opposite >= price)
            # 够得着对手价就按对手价成交(可能比限价更好); 否则挂着等价格穿过限价。
            price = opposite if open_now and crosses and (volume or 0) >= qty else None
        order["id"] = self._next_id("O")
        self.orders.append(order)
        if price is not None:
            self._fill(order, price, quote, now)
        self._trim()
        return order

    def flatten(self, contract: str, quote: dict | None, now: datetime) -> dict:
        """按市价平掉该合约的全部持仓。"""
        position = self.positions.get(contract)
        if not position:
            raise OrderError("这个合约没有持仓")
        side = "sell" if position["qty"] > 0 else "buy"
        return self.place({"side": side, "qty": abs(position["qty"]), "type": "market",
                           "symbol": contract}, quote, now)

    def cancel(self, order_id: str, now: datetime) -> dict:
        for order in self.orders:
            if order["id"] == order_id:
                if order["status"] != "open":
                    raise OrderError("委托已经成交或撤销")
                order.update(status="cancelled", reason="已撤单", updatedAt=_stamp(now))
                self._trim()
                return order
        raise OrderError("找不到这笔委托")

    def match(self, quotes: dict, now: datetime) -> list[dict]:
        """用最新报价检查挂单; 返回状态变了的委托(成交, 或成交时资金不足被拒)。"""
        changed = []
        for order in self.orders:
            if order["status"] != "open":
                continue
            quote = quotes.get(order["contract"])
            if quote is None or not market_status(quote, now)[0] or not self._trades_through(order, quote):
                continue
            try:
                self._check_funds(order["contract"], order["side"], order["qty"], order["price"], quote, now)
            except OrderError as exc:
                order.update(status="rejected", reason=str(exc), updatedAt=_stamp(now))
            else:
                self._fill(order, order["price"], quote, now)
            changed.append(order)
        if changed:
            self._trim()
        return changed

    def mark(self, quotes: dict):
        """用最新价给持仓估值(浮动盈亏与保证金)。"""
        for contract in self.positions:
            last = (quotes.get(contract) or {}).get("last")
            if last is not None:
                self.marks[contract] = last

    # ---------- 规则 ----------

    @staticmethod
    def _check_tradable(quote: dict | None):
        if quote is None or not quote.get("contract"):
            raise OrderError("行情未就绪, 稍后再试")
        if quote.get("insClass") not in ("FUTURE", ""):
            raise OrderError(f"{quote['contract']} 不是期货合约, 不能交易")
        if quote.get("expired"):
            raise OrderError(f"{quote['contract']} 已下市")
        if not quote.get("priceTick") or not quote.get("multiplier"):
            raise OrderError("合约资料(最小变动价位/乘数)未就绪, 稍后再试")

    @staticmethod
    def _taker_price(side: str, qty: int, quote: dict) -> float:
        if side == "buy":
            price, volume, book, limit = quote.get("ask"), quote.get("askVolume"), "卖", "涨停"
        else:
            price, volume, book, limit = quote.get("bid"), quote.get("bidVolume"), "买", "跌停"
        if price is None:
            raise OrderError(f"没有{book}盘(可能{limit}), 市价单无法成交")
        if (volume or 0) < qty:
            raise OrderError(f"{book}一只有 {int(volume or 0)} 手, 市价单要求整笔成交")
        return price

    @staticmethod
    def _check_limit_price(price: float, quote: dict) -> float:
        tick = quote["priceTick"]
        steps = price / tick
        if abs(steps - round(steps)) > 1e-6:
            raise OrderError(f"价格要是最小变动价位 {tick:g} 的整数倍")
        price = round(round(steps) * tick, 10)
        if quote.get("upper") is not None and price > quote["upper"]:
            raise OrderError(f"价格高于涨停价 {quote['upper']:g}")
        if quote.get("lower") is not None and price < quote["lower"]:
            raise OrderError(f"价格低于跌停价 {quote['lower']:g}")
        return price

    @staticmethod
    def _trades_through(order: dict, quote: dict) -> bool:
        """挂单之后价格穿过限价才算成交: 买单要卖一或最新价低于限价, 卖单反之。"""
        limit = order["price"]
        if order["side"] == "buy":
            return any(price is not None and price < limit for price in (quote.get("ask"), quote.get("last")))
        return any(price is not None and price > limit for price in (quote.get("bid"), quote.get("last")))

    def _preview(self, contract: str, signed: int, price: float, multiplier: float, day: date):
        """算出成交后的持仓、平仓盈亏与手续费, 不改状态。"""
        old = self.positions.get(contract) or {}
        qty, avg = old.get("qty", 0), old.get("avgPrice", 0.0)
        today = old.get("todayQty", 0) if old.get("tradingDay") == day.isoformat() else 0
        closing = min(abs(signed), abs(qty)) if qty and (qty > 0) != (signed > 0) else 0
        opening = abs(signed) - closing
        close_yesterday = min(closing, max(abs(qty) - today, 0))
        close_today = closing - close_yesterday
        realized = (price - avg) * closing * multiplier * (1 if qty > 0 else -1) if closing else 0.0
        new_qty = qty + signed
        today -= close_today
        if opening:
            # 反手或新开仓: 均价就是成交价; 同向加仓: 按手数加权
            new_avg = price if closing or not qty else (avg * abs(qty) + price * opening) / (abs(qty) + opening)
            today += opening
        else:
            new_avg = avg if new_qty else 0.0
        fee = self.fees.fee(contract, price, multiplier, opening, close_yesterday, close_today)
        position = {"qty": new_qty, "avgPrice": new_avg, "multiplier": multiplier,
                    "todayQty": today, "tradingDay": day.isoformat()}
        return position, realized, fee, opening, closing

    def _check_funds(self, contract: str, side: str, qty: int, price: float, quote: dict, now: datetime):
        """开仓(含反手的开仓部分)要求成交后权益仍够付全部保证金; 只减仓不查。"""
        signed = qty if side == "buy" else -qty
        position, realized, fee, opening, _ = self._preview(
            contract, signed, price, quote["multiplier"], trading_day(now))
        if not opening:
            return
        others = [(key, pos) for key, pos in self.positions.items() if key != contract]
        equity = self.cash + realized - fee + sum(self._float_pnl(key, pos) for key, pos in others)
        equity += (price - position["avgPrice"]) * position["qty"] * position["multiplier"]
        other_margin = sum(self._margin(key, pos) for key, pos in others)
        need = abs(position["qty"]) * price * position["multiplier"] * MARGIN_RATE
        if equity - other_margin < need:
            raise OrderError(f"资金不足: 需要保证金 {need:,.0f}, 可用 {equity - other_margin:,.0f}")

    def _fill(self, order: dict, price: float, quote: dict, now: datetime):
        moment = parse_quote_time(quote.get("datetime")) or now
        day = trading_day(moment)
        signed = order["qty"] if order["side"] == "buy" else -order["qty"]
        position, realized, fee, opening, closing = self._preview(
            order["contract"], signed, price, quote["multiplier"], day)
        if position["qty"]:
            self.positions[order["contract"]] = position
        else:
            self.positions.pop(order["contract"], None)
        self.cash += realized - fee
        self.realized += realized
        self.fees_paid += fee
        self.marks[order["contract"]] = quote.get("last") or price
        self.trades.append({"id": self._next_id("T"), "orderId": order["id"], "contract": order["contract"],
                            "side": order["side"], "qty": order["qty"], "price": price,
                            "open": opening, "close": closing, "pnl": round(realized, 2),
                            "fee": round(fee, 2), "position": position["qty"],
                            "time": chart_time(moment), "at": _stamp(moment),
                            "tradingDay": day.isoformat()})
        order.update(status="filled", fillPrice=price, filledAt=_stamp(moment), updatedAt=_stamp(now))

    def _trim(self):
        """挂单全留; 已结束的委托和成交只留最近的, 账户文件不会无限长大。"""
        finished = [order for order in self.orders if order["status"] != "open"]
        drop = {id(order) for order in finished[:-MAX_FINISHED_ORDERS]} if len(finished) > MAX_FINISHED_ORDERS else set()
        if drop:
            self.orders = [order for order in self.orders if id(order) not in drop]
        if len(self.trades) > MAX_TRADES:
            self.trades = self.trades[-MAX_TRADES:]

    # ---------- 估值 ----------

    def _mark_of(self, contract: str, position: dict) -> float:
        return self.marks.get(contract, position["avgPrice"])

    def _float_pnl(self, contract: str, position: dict) -> float:
        return (self._mark_of(contract, position) - position["avgPrice"]) * position["qty"] * position["multiplier"]

    def _margin(self, contract: str, position: dict) -> float:
        return abs(position["qty"]) * self._mark_of(contract, position) * position["multiplier"] * MARGIN_RATE

    def summary(self) -> dict:
        positions = []
        float_total = margin_total = 0.0
        for contract, position in sorted(self.positions.items()):
            float_pnl = self._float_pnl(contract, position)
            margin = self._margin(contract, position)
            float_total += float_pnl
            margin_total += margin
            positions.append({"contract": contract, "qty": position["qty"],
                              "avgPrice": position["avgPrice"], "todayQty": position.get("todayQty", 0),
                              "last": self.marks.get(contract), "floatPnl": round(float_pnl, 2),
                              "margin": round(margin, 2)})
        equity = self.cash + float_total
        return {"account": {"initialCash": self.initial_cash, "cash": round(self.cash, 2),
                            "equity": round(equity, 2), "floatPnl": round(float_total, 2),
                            "margin": round(margin_total, 2), "available": round(equity - margin_total, 2),
                            "realizedPnl": round(self.realized, 2), "fees": round(self.fees_paid, 2),
                            "marginRate": MARGIN_RATE},
                "positions": positions}


class PaperStore:
    """``data/paper/account.json``; 路径用 provider 现取, 测试和离线预览会替换数据目录。"""

    def __init__(self, path_provider):
        self._path_provider = path_provider

    def path(self) -> str:
        return self._path_provider()

    def load(self, path: str) -> dict | None:
        """不存在返回 None; 损坏的文件改名留档后返回 None —— 账户不能静默丢, 也不能让服务起不来。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("不是 JSON 对象")
            return payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            aside = f"{path}.corrupt-{int(time.time())}"
            try:
                os.replace(path, aside)
            except OSError:
                aside = "(改名失败)"
            print(f"[paper] 模拟账户文件损坏({exc}), 已另存为 {aside}, 从新账户开始", flush=True)
            return None

    def save(self, path: str, state: dict):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, path)


class PaperService:
    """模拟账户 + 报价跟踪; HTTP 层与采集线程都只跟这个类打交道。

    线程分工: ``_refs``(SDK 报价对象)只在采集线程里读写(下单任务与 ``on_loop`` 都在那里跑);
    账户、报价快照和跟踪集合在锁里, HTTP 线程读它们拼页面数据。
    """

    def __init__(self, manager_provider, store: PaperStore, fees: FeeTable | None = None, *,
                 timeout: float = JOB_TIMEOUT_SEC, clock=beijing_now):
        self._manager = manager_provider
        self._store = store
        self._fees = fees
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._book: PaperBook | None = None
        self._book_path: str | None = None
        self.save_error: str | None = None
        self._api = None
        self._refs: dict[str, object] = {}
        self._quotes: dict[str, dict] = {}    # 实际合约 -> 报价快照
        self._aliases: dict[str, str] = {}    # 主连 -> 当前标的月份合约
        self._watch: dict[str, float] = {}    # 页面在看的代码 -> 最近请求时刻
        self._verified: set[str] = set()      # 确认过合约服务里有的代码(与连接无关)
        self._verify_failed: dict[str, tuple[float, str]] = {}

    # ---------- 账户 ----------

    def _book_now(self) -> PaperBook:
        """必须持锁调用; 数据目录换了(测试/预览)就重新读。"""
        path = self._store.path()
        if self._book is None or path != self._book_path:
            if self._fees is None:
                self._fees = FeeTable.load()
            self._book = PaperBook(self._store.load(path), self._fees)
            self._book_path = path
        return self._book

    def _save(self, book: PaperBook):
        """必须持锁调用。写盘失败不回滚内存: 下一次改动会再写, 失败原因显示在页面上。"""
        try:
            self._store.save(self._book_path, book.to_dict())
            self.save_error = None
        except OSError as exc:
            self.save_error = f"模拟账户保存失败: {exc}"
            print(f"[paper] {self.save_error}", flush=True)

    def reset(self, initial_cash: float) -> dict:
        cash = _num(initial_cash)
        if cash is None or not MIN_CASH <= cash <= MAX_CASH:
            raise OrderError(f"初始资金要在 {MIN_CASH:,.0f} ~ {MAX_CASH:,.0f} 之间")
        with self._lock:
            old = self._book_now()
            # 编号接着旧账户往下排: 还开着的旧页面点"撤 O3"时, 不能撤到新账户里恰好同号的另一笔。
            self._book = PaperBook({"initialCash": cash, "seq": old.seq}, self._fees)
            self._save(self._book)
            return self._book.summary()

    def cancel(self, order_id: str) -> dict:
        with self._lock:
            book = self._book_now()
            order = book.cancel(order_id, self._clock())
            self._save(book)
            return order

    # ---------- 采集线程里执行的部分 ----------

    def _ref(self, api, symbol: str):
        if self._api is not api:
            # 连接重建过: 旧连接的报价对象不会再更新, 全部作废重取。
            self._api = api
            self._refs.clear()
        ref = self._refs.get(symbol)
        if ref is None:
            ref = api.get_quote(symbol)
            self._refs[symbol] = ref
        return ref

    def _verify(self, api, symbol: str):
        """合约服务里查得到才能 get_quote: 不存在的代码会拖停整条连接(见 ingest.listed_symbols)。"""
        with self._lock:
            if symbol in self._verified:
                return
        if symbol not in listed_symbols(api, [symbol]):
            with self._lock:
                self._verify_failed[symbol] = (time.monotonic(), f"合约 {symbol} 不存在")
            raise OrderError(f"合约 {symbol} 不存在")
        with self._lock:
            self._verified.add(symbol)
            self._verify_failed.pop(symbol, None)

    def _resolve(self, api, symbol: str) -> tuple[str, dict | None]:
        """代码 -> (实际合约, 报价快照)。主连换成当前标的月份合约: 主连本身不能下单。

        调用前 symbol 必须已经确认存在; 标的合约来自 TqSdk 自己的报价, 不必再查。
        """
        ref = self._ref(api, symbol)
        contract = symbol
        if split_cont_symbol(symbol) is not None:
            contract = str(getattr(ref, "underlying_symbol", "") or "")
            if not contract:
                return symbol, None    # 主连报价还没下发, 不知道标的是谁
            ref = self._ref(api, contract)
        snapshot = quote_snapshot(contract, ref)
        with self._lock:
            self._quotes[contract] = snapshot
            if contract != symbol:
                self._aliases[symbol] = contract
                self._verified.add(contract)   # 标的来自 TqSdk 自己的报价, 一定存在
        return contract, snapshot

    def _fresh_quote(self, api, symbol: str) -> dict | None:
        """下单用的报价: 第一次订阅的合约先等一轮行情下发。"""
        self._verify(api, symbol)
        _, snapshot = self._resolve(api, symbol)
        if snapshot is None or not snapshot["datetime"]:
            try:
                api.wait_update(deadline=time.time() + COLD_WAIT_SEC)
            except Exception:
                pass
            _, snapshot = self._resolve(api, symbol)
        return snapshot

    def _place_job(self, api, request: dict) -> dict:
        quote = self._fresh_quote(api, request["symbol"])
        with self._lock:
            book = self._book_now()
            order = book.place(request, quote, self._clock())
            self._save(book)
            return order

    def _flatten_job(self, api, symbol: str) -> dict:
        quote = self._fresh_quote(api, symbol)
        with self._lock:
            book = self._book_now()
            contract = quote["contract"] if quote else self._aliases.get(symbol, symbol)
            order = book.flatten(contract, quote, self._clock())
            self._save(book)
            return order

    def _track_job(self, api, symbol: str):
        self._verify(api, symbol)
        self._resolve(api, symbol)

    def on_loop(self, api):
        """采集线程每轮 wait_update 之后调用: 刷新在看/持仓/挂单合约的报价, 撮合挂单。

        页面在看的代码由 track 任务确认过存在, 下单时成交的合约来自 TqSdk 自己的报价;
        只有从账户文件读回来、本进程还没确认过的合约才在这里查一次合约服务(查不到就隔
        VERIFY_RETRY_SEC 再试), 之后每轮都不再发查询。
        """
        now = time.monotonic()
        with self._lock:
            book = self._book_now()
            self._watch = {symbol: seen for symbol, seen in self._watch.items()
                           if now - seen < WATCH_TTL_SEC and symbol in self._verified}
            symbols = list(dict.fromkeys([*self._watch, *book.positions,
                                          *(order["contract"] for order in book.orders
                                            if order["status"] == "open")]))
            failed = {symbol for symbol, (at, _) in self._verify_failed.items()
                      if now - at < VERIFY_RETRY_SEC}
        for symbol in symbols:
            if symbol in failed:
                continue
            try:
                self._verify(api, symbol)
                self._resolve(api, symbol)
            except Exception as exc:
                print(f"[paper] 读取 {symbol} 报价失败: {exc}", flush=True)
        with self._lock:
            # 重新取账户: 读报价期间可能有人重置了账户, 不能拿旧账户撮合再把它写回文件。
            book = self._book_now()
            book.mark(self._quotes)
            if book.match(self._quotes, self._clock()):
                self._save(book)

    # ---------- HTTP 层调用 ----------

    async def _query(self, fn):
        return await self._manager().query(fn, self._timeout)

    async def _submit(self, fn, action: str) -> dict:
        try:
            return await self._query(fn)
        except OrderError:
            raise
        except Exception as exc:
            # 超时的任务可能已经在采集线程里执行完了: 结果以成交记录为准(同一 clientId 重发不会重复下单)。
            raise OrderError(f"{action}没有完成({exc}), 请以委托与成交记录为准") from None

    async def place(self, raw) -> dict:
        request = normalize_request(raw)
        return await self._submit(lambda api: self._place_job(api, request), "下单")

    async def flatten(self, symbol: str) -> dict:
        value = validate_symbol(symbol)
        return await self._submit(lambda api: self._flatten_job(api, value), "平仓")

    async def state(self, symbol: str) -> dict:
        """页面数据: 账户、持仓、委托、成交, 以及当前合约的盘口与交易状态。"""
        value = validate_symbol(symbol)
        error = None
        with self._lock:
            known = value in self._verified
            failed = self._verify_failed.get(value)
        if failed and time.monotonic() - failed[0] < VERIFY_RETRY_SEC:
            error = failed[1]
        elif not known:
            try:
                await self._query(lambda api: self._track_job(api, value))
            except Exception as exc:
                error = str(exc)
        with self._lock:
            if value in self._verified:
                self._watch[value] = time.monotonic()
            book = self._book_now()
            contract = self._aliases.get(value, value if split_cont_symbol(value) is None else None)
            quote = self._quotes.get(contract) if contract else None
            if quote is not None:
                open_now, reason = market_status(quote, self._clock())
                quote = {**quote, "open": open_now, "reason": reason}
            open_orders = [order for order in book.orders if order["status"] == "open"]
            finished = [order for order in book.orders if order["status"] != "open"][-10:]
            return {**book.summary(),
                    "symbol": value, "contract": contract, "quote": quote,
                    "fee": book.fees.spec(contract) if contract else None,
                    "feeSource": book.fees.source,
                    "orders": open_orders + finished[::-1],
                    "trades": book.trades[-50:][::-1],
                    "contractTrades": [trade for trade in book.trades if trade["contract"] == contract][-500:],
                    "error": error or self.save_error}
