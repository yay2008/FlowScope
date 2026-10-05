# -*- coding: utf-8 -*-
"""加密永续的模拟交易: U 本位、全仓、单向净持仓; 按推送的五档盘口撮合, 计手续费、资金费, 爆仓强平。

和期货模拟账户(paper.py)分开记账: 币种(USDT 对人民币)、数量(小数的币对整数手)、杠杆、
24 小时交易、资金费都不一样, 放在一个账户里没有意义。账户在 ``data/paper/crypto.json``,
同样是临时文件 + ``os.replace`` 原子替换(复用 paper.PaperStore)。

规则(结果只作参考 —— 看不到排队, 五档盘口 500ms 一次):

- 数量以币计(OKX 也折成币), 必须是数量步长的整数倍、不少于最小下单量; 名义金额不少于最小下单金额。
- 市价单: 按五档盘口逐档吃到够为止, 按成交均价成交(吃单费率); 五档都吃完还不够就拒单。
- 限价单: 下单时就够得着(买: 卖一不高于限价)且五档里限价以内的量够, 立即按均价成交(吃单费率);
  否则挂单, 之后最新价或对手一档**穿过**限价才按限价成交(挂单费率), 碰到不算。
- 杠杆每个合约一个(默认 10 倍), 初始保证金 = 名义金额 / 杠杆。开仓(含反手的开仓部分)要求成交后
  权益仍够付全部持仓的初始保证金; 只减仓不查。
- 资金费: 到交易所公布的结算时刻, 按结算前最后看到的资金费率与标记价格结算(费率为正时多头付费)。
  服务没开着的时候错过的结算不补。
- 强平: 权益低于全部持仓的维持保证金(名义金额 x MAINTENANCE_RATE)时, 按标记价格平掉全部持仓。
- 估值(浮动盈亏、保证金、强平)按标记价格, 没有标记价格时用最新价。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from crypto_feed import is_crypto, venue_of
from ingest import validate_symbol
from paper import MAX_FINISHED_ORDERS, MAX_TRADES, OrderError, PaperStore, _num

DEFAULT_CASH = 10_000.0           # USDT
MIN_CASH, MAX_CASH = 10.0, 1e9
DEFAULT_LEVERAGE = 10
MAINTENANCE_RATE = 0.005          # 维持保证金率(交易所最低一档约 0.4%, 取整些偏保守)
MAX_NOTIONAL = 10_000_000.0       # 单笔名义金额上限, 防手滑
# 交易所默认(最低一档)费率: (挂单, 吃单)
FEES = {"BINANCE": (0.0002, 0.0005), "OKX": (0.0002, 0.0005)}
BOOK_STALE_SEC = 15.0             # 盘口多久没更新就不撮合(断线、还没订阅上)
WATCH_SEC = 30                    # 面板在看的合约, 盘口/标记价格再订阅多久
HOLD_SEC = 60                     # 有持仓或挂单的合约, 每轮续订这么久
MAX_FUNDINGS = 500
STORE_VERSION = 1
CST = timezone(timedelta(hours=8))
EPS = 1e-12


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime("%Y-%m-%d %H:%M:%S")


def chart_time(ms: int) -> int:
    """毫秒时间戳 -> 图表时间轴秒数(北京时间当 UTC 显示, 同 indicator.TZ_SHIFT_S)。"""
    return ms // 1000 + 8 * 3600


def _round(value: float) -> float:
    """数量的浮点尾数(0.1 + 0.2)抹掉; 接近 0 的当 0。"""
    value = round(value, 12)
    return 0.0 if abs(value) < EPS else value


def sweep(levels, qty: float, limit: float | None, side: str) -> float | None:
    """按盘口逐档吃 qty 的成交均价; limit 以外的档位不吃; 不够量返回 None。"""
    filled = cost = 0.0
    for price, size in levels or []:
        if limit is not None and (price > limit + EPS if side == "buy" else price < limit - EPS):
            break
        take = min(size, qty - filled)
        filled += take
        cost += take * price
        if filled >= qty - EPS:
            return cost / qty
    return None


def quote_snapshot(symbol: str, instrument, raw: dict, now: float) -> dict:
    """推送缓存(manager.quote) -> 撮合与页面要用的字段; 缺数据留 None。"""
    book, ticker, mark = raw.get("book") or {}, raw.get("ticker") or {}, raw.get("mark") or {}
    bids, asks = book.get("bids") or [], book.get("asks") or []
    received = book.get("received")
    fresh = received is not None and now - received <= BOOK_STALE_SEC
    times = [item.get("time") for item in (book, ticker, mark) if item.get("time")]
    return {"contract": symbol, "name": instrument.label, "open": fresh,
            "reason": "" if fresh else "盘口还没收到(或已断线)",
            "datetime": _stamp(max(times)) if times else "",
            "last": ticker.get("last"), "bids": bids, "asks": asks,
            "bid": bids[0][0] if bids else None, "bidVolume": bids[0][1] if bids else None,
            "ask": asks[0][0] if asks else None, "askVolume": asks[0][1] if asks else None,
            "markPrice": mark.get("markPrice"), "fundingRate": mark.get("fundingRate"),
            "nextFundingTime": mark.get("nextFundingTime"),
            "priceTick": instrument.tick_size, "priceDecs": instrument.price_digits,
            "qtyStep": instrument.step_size, "qtyDecs": instrument.qty_digits,
            "minQty": instrument.min_qty, "minNotional": instrument.min_notional,
            "maxLeverage": instrument.max_leverage, "unit": instrument.base}


def normalize_request(raw) -> dict:
    """校验页面提交的委托(数量是小数的币); 不合法抛 OrderError。"""
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
    if qty is None or qty <= 0:
        raise OrderError("数量要是正数")
    price = None
    if kind == "limit":
        price = _num(raw.get("price"))
        if price is None or price <= 0:
            raise OrderError("限价单要填价格")
    return {"symbol": symbol, "side": side, "type": kind, "qty": qty, "price": price,
            "clientId": str(raw.get("clientId") or "")[:64]}


class CryptoBook:
    """加密模拟账户的全部状态与规则(纯逻辑: 不加锁、不读写文件、不碰行情连接)。

    ``cash`` 是静态权益 = 初始资金 + 平仓盈亏 - 手续费 + 资金费收支; 动态权益再加浮动盈亏。
    """

    def __init__(self, state: dict | None = None):
        state = state or {}
        self.initial_cash = float(state.get("initialCash", DEFAULT_CASH))
        self.cash = float(state.get("cash", self.initial_cash))
        self.realized = float(state.get("realizedPnl", 0.0))
        self.fees_paid = float(state.get("fees", 0.0))
        self.funding = float(state.get("funding", 0.0))
        self.positions = {symbol: dict(pos) for symbol, pos in (state.get("positions") or {}).items()
                          if pos.get("qty")}
        self.leverage = {symbol: int(value) for symbol, value in (state.get("leverage") or {}).items()}
        self.orders = [dict(order) for order in state.get("orders") or []]
        self.trades = [dict(trade) for trade in state.get("trades") or []]
        self.fundings = [dict(item) for item in state.get("fundings") or []]
        self.seq = int(state.get("seq", 0))
        self.marks: dict[str, float] = {}          # 合约 -> 估值价(标记价格优先), 只在内存里
        self._funding_due: dict[str, int] = {}     # 合约 -> 下一次资金费结算时刻(毫秒)
        self._funding_rate: dict[str, float] = {}  # 合约 -> 结算前最后看到的资金费率

    def to_dict(self) -> dict:
        return {"version": STORE_VERSION, "initialCash": self.initial_cash, "cash": self.cash,
                "realizedPnl": self.realized, "fees": self.fees_paid, "funding": self.funding,
                "positions": self.positions, "leverage": self.leverage, "orders": self.orders,
                "trades": self.trades, "fundings": self.fundings, "seq": self.seq}

    def _next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"C{prefix}{self.seq}"     # C 开头: 与期货账户的委托编号区分(撤单按前缀分流)

    def leverage_of(self, symbol: str) -> int:
        return self.leverage.get(symbol, DEFAULT_LEVERAGE)

    def set_leverage(self, symbol: str, leverage, max_leverage: int):
        value = _num(leverage)
        if value is None or value != int(value) or not 1 <= value <= max_leverage:
            raise OrderError(f"杠杆要是 1~{max_leverage} 的整数")
        old = self.leverage_of(symbol)
        self.leverage[symbol] = int(value)
        if self._available() < 0:
            self.leverage[symbol] = old
            raise OrderError("降低杠杆后保证金不够, 先减仓")

    # ---------- 下单 / 撤单 ----------

    def place(self, request: dict, quote: dict | None, now_ms: int) -> dict:
        """下单: 市价单当场成交或抛 OrderError; 限价单能成交就成交, 否则挂单。同一个 clientId 只下一次。"""
        client_id = request.get("clientId") or ""
        if client_id:
            for order in self.orders:
                if order.get("clientId") == client_id:
                    return order
        if quote is None:
            raise OrderError("行情未就绪, 稍后再试")
        side, kind = request["side"], request["type"]
        qty = self._check_qty(request["qty"], quote)
        order = {"id": "", "clientId": client_id, "symbol": request.get("symbol") or quote["contract"],
                 "contract": quote["contract"], "side": side, "qty": qty, "type": kind,
                 "price": request.get("price"), "status": "open", "reason": "",
                 "createdAt": _stamp(now_ms), "fillPrice": None, "filledAt": None}
        book = quote["asks"] if side == "buy" else quote["bids"]
        maker, taker = FEES.get(venue_of(quote["contract"]), FEES["BINANCE"])
        if kind == "market":
            if not quote["open"]:
                raise OrderError(quote["reason"])
            price = sweep(book, qty, None, side)
            if price is None:
                raise OrderError("五档盘口的量不够这笔市价单")
            self._check_notional(qty, price, quote)
            self._check_funds(quote["contract"], side, qty, price, taker, quote)
        else:
            limit = self._check_limit_price(order["price"], quote)
            order["price"] = limit
            self._check_notional(qty, limit, quote)
            price = sweep(book, qty, limit, side) if quote["open"] else None
            self._check_funds(quote["contract"], side, qty, limit if price is None else price, taker, quote)
        order["id"] = self._next_id("O")
        self.orders.append(order)
        if price is not None:
            self._fill(order, price, taker, "taker", quote, now_ms)
        self._trim()
        return order

    def flatten(self, symbol: str, quote: dict | None, now_ms: int) -> dict:
        position = self.positions.get(symbol)
        if not position:
            raise OrderError("这个合约没有持仓")
        side = "sell" if position["qty"] > 0 else "buy"
        return self.place({"side": side, "qty": abs(position["qty"]), "type": "market", "symbol": symbol},
                          quote, now_ms)

    def cancel(self, order_id: str, now_ms: int) -> dict:
        for order in self.orders:
            if order["id"] == order_id:
                if order["status"] != "open":
                    raise OrderError("委托已经成交或撤销")
                order.update(status="cancelled", reason="已撤单", updatedAt=_stamp(now_ms))
                self._trim()
                return order
        raise OrderError("找不到这笔委托")

    def match(self, quotes: dict, now_ms: int) -> bool:
        """用最新报价检查挂单(按挂单费率成交); 返回有没有状态变化。"""
        changed = False
        for order in self.orders:
            if order["status"] != "open":
                continue
            quote = quotes.get(order["contract"])
            if quote is None or not quote["open"] or not self._trades_through(order, quote):
                continue
            maker, _ = FEES.get(venue_of(order["contract"]), FEES["BINANCE"])
            try:
                self._check_funds(order["contract"], order["side"], order["qty"], order["price"], maker, quote)
            except OrderError as exc:
                order.update(status="rejected", reason=str(exc), updatedAt=_stamp(now_ms))
            else:
                self._fill(order, order["price"], maker, "maker", quote, now_ms)
            changed = True
        if changed:
            self._trim()
        return changed

    def settle(self, quotes: dict, now_ms: int) -> bool:
        """估值、资金费结算、强平; 返回账户有没有变化(要不要存盘)。"""
        changed = False
        for symbol, quote in quotes.items():
            mark = quote.get("markPrice") or quote.get("last")
            if mark:
                self.marks[symbol] = mark
            due, rate = self._funding_due.get(symbol), self._funding_rate.get(symbol)
            position = self.positions.get(symbol)
            if due is not None and now_ms >= due and rate is not None and position and mark:
                amount = -position["qty"] * mark * rate      # 正数 = 收到
                self.cash += amount
                self.funding += amount
                self.fundings.append({"contract": symbol, "time": chart_time(due), "at": _stamp(due),
                                      "rate": rate, "markPrice": mark, "qty": position["qty"],
                                      "amount": round(amount, 6)})
                self.fundings = self.fundings[-MAX_FUNDINGS:]
                changed = True
            self._schedule_funding(symbol, quote, now_ms)
        return self._liquidate(quotes, now_ms) or changed

    def _schedule_funding(self, symbol: str, quote: dict, now_ms: int):
        """只为现有持仓登记未来结算; 空仓和到期状态一并清理, 费率不跨期沿用。"""
        due = self._funding_due.get(symbol)
        upcoming = quote.get("nextFundingTime")
        if symbol in self.positions and upcoming and upcoming > now_ms:
            if upcoming != due:
                self._funding_rate.pop(symbol, None)
            self._funding_due[symbol] = upcoming
            if quote.get("fundingRate") is not None:
                self._funding_rate[symbol] = quote["fundingRate"]
        elif symbol not in self.positions or (due is not None and now_ms >= due):
            self._funding_due.pop(symbol, None)
            self._funding_rate.pop(symbol, None)

    # ---------- 规则 ----------

    @staticmethod
    def _check_qty(qty: float, quote: dict) -> float:
        step = quote["qtyStep"]
        steps = qty / step
        if abs(steps - round(steps)) > 1e-6:
            raise OrderError(f"数量要是 {step:g} 的整数倍")
        qty = _round(round(steps) * step)
        if qty < quote["minQty"] - EPS:
            raise OrderError(f"数量不能少于 {quote['minQty']:g}")
        return qty

    @staticmethod
    def _check_notional(qty: float, price: float, quote: dict):
        notional = qty * price
        if quote.get("minNotional") and notional < quote["minNotional"] - EPS:
            raise OrderError(f"名义金额 {notional:,.2f} 低于最小下单金额 {quote['minNotional']:g} USDT")
        if notional > MAX_NOTIONAL:
            raise OrderError(f"名义金额超过 {MAX_NOTIONAL:,.0f} USDT")

    @staticmethod
    def _check_limit_price(price: float, quote: dict) -> float:
        tick = quote["priceTick"]
        steps = price / tick
        if abs(steps - round(steps)) > 1e-6:
            raise OrderError(f"价格要是最小变动价位 {tick:g} 的整数倍")
        return round(round(steps) * tick, 10)

    @staticmethod
    def _trades_through(order: dict, quote: dict) -> bool:
        """挂单之后价格穿过限价才算成交: 买单要卖一或最新价低于限价, 卖单反之。"""
        limit = order["price"]
        if order["side"] == "buy":
            return any(price is not None and price < limit for price in (quote.get("ask"), quote.get("last")))
        return any(price is not None and price > limit for price in (quote.get("bid"), quote.get("last")))

    def _preview(self, symbol: str, signed: float, price: float, fee_rate: float):
        """算出成交后的持仓、平仓盈亏与手续费, 不改状态。"""
        old = self.positions.get(symbol) or {}
        qty, avg = old.get("qty", 0.0), old.get("avgPrice", 0.0)
        closing = min(abs(signed), abs(qty)) if qty and (qty > 0) != (signed > 0) else 0.0
        opening = _round(abs(signed) - closing)
        realized = (price - avg) * closing * (1 if qty > 0 else -1) if closing else 0.0
        new_qty = _round(qty + signed)
        if opening:
            # 反手或新开仓: 均价就是成交价; 同向加仓: 按数量加权
            new_avg = price if closing or not qty else (avg * abs(qty) + price * opening) / (abs(qty) + opening)
        else:
            new_avg = avg if new_qty else 0.0
        fee = abs(signed) * price * fee_rate
        return {"qty": new_qty, "avgPrice": new_avg}, realized, fee, opening, closing

    def _mark_of(self, symbol: str, position: dict) -> float:
        return self.marks.get(symbol, position["avgPrice"])

    def _quote_mark(self, symbol: str, quote: dict, fallback: float) -> float:
        """资金校验和成交后估值使用同一价格: 当前标记价、最新价、缓存价、最后才是成交价。"""
        return quote.get("markPrice") or quote.get("last") or self.marks.get(symbol, fallback)

    def _float_pnl(self, symbol: str, position: dict) -> float:
        return (self._mark_of(symbol, position) - position["avgPrice"]) * position["qty"]

    def _margin(self, symbol: str, position: dict) -> float:
        return abs(position["qty"]) * self._mark_of(symbol, position) / self.leverage_of(symbol)

    def _available(self) -> float:
        equity = self.cash + sum(self._float_pnl(key, pos) for key, pos in self.positions.items())
        return equity - sum(self._margin(key, pos) for key, pos in self.positions.items())

    def _check_funds(self, symbol: str, side: str, qty: float, price: float, fee_rate: float, quote: dict):
        """开仓(含反手的开仓部分)要求成交后权益仍够付全部持仓的初始保证金; 只减仓不查。"""
        signed = qty if side == "buy" else -qty
        position, realized, fee, opening, _ = self._preview(symbol, signed, price, fee_rate)
        if not opening:
            return
        others = [(key, pos) for key, pos in self.positions.items() if key != symbol]
        mark = self._quote_mark(symbol, quote, price)
        equity = self.cash + realized - fee + sum(self._float_pnl(key, pos) for key, pos in others)
        equity += (mark - position["avgPrice"]) * position["qty"]
        need = sum(self._margin(key, pos) for key, pos in others)
        need += abs(position["qty"]) * mark / self.leverage_of(symbol)
        if equity < need:
            raise OrderError(f"资金不足: 需要保证金 {need:,.2f} USDT, 权益 {equity:,.2f}")

    def _fill(self, order: dict, price: float, fee_rate: float, liquidity: str, quote: dict, now_ms: int):
        symbol = order["contract"]
        was_flat = symbol not in self.positions
        signed = order["qty"] if order["side"] == "buy" else -order["qty"]
        position, realized, fee, opening, closing = self._preview(symbol, signed, price, fee_rate)
        if position["qty"]:
            self.positions[symbol] = position
        else:
            self.positions.pop(symbol, None)
        if was_flat or not position["qty"]:
            # 新仓不能继承空仓前的结算; 从成交时的报价登记下一期, 不必等下一轮循环。
            self._funding_due.pop(symbol, None)
            self._funding_rate.pop(symbol, None)
            self._schedule_funding(symbol, quote, now_ms)
        self.cash += realized - fee
        self.realized += realized
        self.fees_paid += fee
        self.marks[order["contract"]] = self._quote_mark(order["contract"], quote, price)
        self.trades.append({"id": self._next_id("T"), "orderId": order["id"], "contract": order["contract"],
                            "side": order["side"], "qty": order["qty"], "price": price, "liquidity": liquidity,
                            "open": opening, "close": closing, "pnl": round(realized, 6),
                            "fee": round(fee, 6), "position": position["qty"],
                            "time": chart_time(now_ms), "at": _stamp(now_ms)})
        order.update(status="filled", fillPrice=price, filledAt=_stamp(now_ms), updatedAt=_stamp(now_ms))

    def _liquidate(self, quotes: dict, now_ms: int) -> bool:
        """权益低于维持保证金: 按标记价格平掉全部持仓(收吃单费), 挂单全部撤掉。"""
        if not self.positions:
            return False
        equity = self.cash + sum(self._float_pnl(key, pos) for key, pos in self.positions.items())
        maintenance = sum(abs(pos["qty"]) * self._mark_of(key, pos) * MAINTENANCE_RATE
                          for key, pos in self.positions.items())
        if equity >= maintenance:
            return False
        for symbol, position in list(self.positions.items()):
            side = "sell" if position["qty"] > 0 else "buy"
            order = {"id": self._next_id("O"), "clientId": "", "symbol": symbol, "contract": symbol, "side": side,
                     "qty": abs(position["qty"]), "type": "market", "price": None, "status": "open",
                     "reason": "强平", "createdAt": _stamp(now_ms), "fillPrice": None, "filledAt": None}
            self.orders.append(order)
            _, taker = FEES.get(venue_of(symbol), FEES["BINANCE"])
            self._fill(order, self._mark_of(symbol, position), taker, "liquidation",
                       quotes.get(symbol) or {}, now_ms)
            order["reason"] = "强平"
        for order in self.orders:
            if order["status"] == "open":
                order.update(status="cancelled", reason="强平时撤单", updatedAt=_stamp(now_ms))
        self._trim()
        return True

    def _trim(self):
        finished = [order for order in self.orders if order["status"] != "open"]
        if len(finished) > MAX_FINISHED_ORDERS:
            drop = {id(order) for order in finished[:-MAX_FINISHED_ORDERS]}
            self.orders = [order for order in self.orders if id(order) not in drop]
        if len(self.trades) > MAX_TRADES:
            self.trades = self.trades[-MAX_TRADES:]

    # ---------- 估值 ----------

    def _liquidation_price(self, symbol: str, position: dict) -> float | None:
        """只看这一个持仓(其它持仓按现价不动)的估算强平价; 算不出或不会强平时返回 None。"""
        qty = position["qty"]
        others = sum(self._float_pnl(key, pos) - abs(pos["qty"]) * self._mark_of(key, pos) * MAINTENANCE_RATE
                     for key, pos in self.positions.items() if key != symbol)
        denominator = qty - abs(qty) * MAINTENANCE_RATE
        if not denominator:
            return None
        price = (qty * position["avgPrice"] - self.cash - others) / denominator
        return price if price > 0 else None

    def summary(self) -> dict:
        positions = []
        float_total = margin_total = 0.0
        for symbol, position in sorted(self.positions.items()):
            float_pnl = self._float_pnl(symbol, position)
            margin = self._margin(symbol, position)
            float_total += float_pnl
            margin_total += margin
            positions.append({"contract": symbol, "qty": position["qty"], "avgPrice": position["avgPrice"],
                              "last": self.marks.get(symbol), "floatPnl": round(float_pnl, 4),
                              "margin": round(margin, 4), "leverage": self.leverage_of(symbol),
                              "liqPrice": self._liquidation_price(symbol, position)})
        equity = self.cash + float_total
        return {"account": {"initialCash": self.initial_cash, "cash": round(self.cash, 4),
                            "equity": round(equity, 4), "floatPnl": round(float_total, 4),
                            "margin": round(margin_total, 4), "available": round(equity - margin_total, 4),
                            "realizedPnl": round(self.realized, 4), "fees": round(self.fees_paid, 4),
                            "funding": round(self.funding, 4), "currency": "USDT"},
                "positions": positions}


class CryptoPaperService:
    """加密模拟账户 + 报价跟踪; HTTP 层与加密行情管理线程(on_cycle)都只跟这个类打交道。

    报价来自 CryptoManager 的推送缓存(线程安全的拷贝), 下单不用排进行情线程。
    """

    def __init__(self, manager_provider, store: PaperStore, *, clock=lambda: int(time.time() * 1000)):
        self._manager = manager_provider
        self._store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._book: CryptoBook | None = None
        self._book_path: str | None = None
        self.save_error: str | None = None

    def _book_now(self) -> CryptoBook:
        """必须持锁调用; 数据目录换了(测试/预览)就重新读。"""
        path = self._store.path()
        if self._book is None or path != self._book_path:
            self._book = CryptoBook(self._store.load(path))
            self._book_path = path
        return self._book

    def _save(self, book: CryptoBook):
        try:
            self._store.save(self._book_path, book.to_dict())
            self.save_error = None
        except OSError as exc:
            self.save_error = f"模拟账户保存失败: {exc}"
            print(f"[paper] {self.save_error}", flush=True)

    def _tradable(self, symbol: str):
        """合约 -> Instrument; 多所汇总与不认识的代码不能交易。"""
        value = validate_symbol(symbol)
        if venue_of(value) == "AGG":
            raise OrderError("多所汇总不能交易, 请切到单个交易所的合约下单")
        instrument = self._manager().instrument(value)
        if instrument is None:
            raise OrderError(f"不支持的加密合约 {value}")
        return value, instrument

    def _quote(self, symbol: str, instrument, seconds: float = WATCH_SEC) -> dict:
        manager = self._manager()
        manager.touch(symbol, ["book", "mark", "ticker"], seconds)
        return quote_snapshot(symbol, instrument, manager.quote(symbol), time.time())

    def state(self, symbol: str) -> dict:
        """页面数据: 账户、持仓、委托、成交、资金费, 以及当前合约的盘口与杠杆。"""
        value = validate_symbol(symbol)
        error = quote = instrument = None
        try:
            value, instrument = self._tradable(value)
            quote = self._quote(value, instrument)
        except OrderError as exc:
            error = str(exc)
        with self._lock:
            book = self._book_now()
            mark = quote and (quote.get("markPrice") or quote.get("last"))
            if mark:
                book.marks[value] = mark
            open_orders = [order for order in book.orders if order["status"] == "open"]
            finished = [order for order in book.orders if order["status"] != "open"][-10:]
            maker, taker = FEES.get(venue_of(value), FEES["BINANCE"])
            return {**book.summary(), "mode": "crypto", "symbol": value,
                    "contract": value if instrument is not None else None,
                    "quote": quote, "leverage": book.leverage_of(value),
                    "fee": {"mode": "crypto", "maker": maker, "taker": taker}, "feeSource": None,
                    "orders": open_orders + finished[::-1], "trades": book.trades[-50:][::-1],
                    "contractTrades": [trade for trade in book.trades if trade["contract"] == value][-500:],
                    "fundings": book.fundings[-20:][::-1],
                    "error": error or self.save_error}

    def place(self, raw) -> dict:
        request = normalize_request(raw)
        symbol, instrument = self._tradable(request["symbol"])
        quote = self._quote(symbol, instrument)
        with self._lock:
            book = self._book_now()
            order = book.place(request, quote, self._clock())
            self._save(book)
            return order

    def flatten(self, symbol: str) -> dict:
        symbol, instrument = self._tradable(symbol)
        quote = self._quote(symbol, instrument)
        with self._lock:
            book = self._book_now()
            order = book.flatten(symbol, quote, self._clock())
            self._save(book)
            return order

    def cancel(self, order_id: str) -> dict:
        with self._lock:
            book = self._book_now()
            order = book.cancel(order_id, self._clock())
            self._save(book)
            return order

    def set_leverage(self, symbol: str, leverage) -> dict:
        symbol, instrument = self._tradable(symbol)
        with self._lock:
            book = self._book_now()
            book.set_leverage(symbol, leverage, instrument.max_leverage)
            self._save(book)
            return book.summary()

    def reset(self, initial_cash: float) -> dict:
        cash = _num(initial_cash)
        if cash is None or not MIN_CASH <= cash <= MAX_CASH:
            raise OrderError(f"初始资金要在 {MIN_CASH:,.0f} ~ {MAX_CASH:,.0f} USDT 之间")
        with self._lock:
            old = self._book_now()
            # 编号接着旧账户往下排(同期货账户), 杠杆设置保留
            self._book = CryptoBook({"initialCash": cash, "seq": old.seq, "leverage": old.leverage})
            self._save(self._book)
            return self._book.summary()

    def on_cycle(self, manager):
        """加密行情管理线程每轮调用: 续订持仓/挂单合约的盘口与标记价格, 撮合挂单, 结算资金费, 检查强平。"""
        with self._lock:
            book = self._book_now()
            symbols = set(book.positions) | {order["contract"] for order in book.orders if order["status"] == "open"}
        if not symbols:
            return
        quotes = {}
        for symbol in symbols:
            instrument = manager.instrument(symbol)
            if instrument is None or not is_crypto(symbol):
                continue
            manager.touch(symbol, ["book", "mark", "ticker"], HOLD_SEC)
            quotes[symbol] = quote_snapshot(symbol, instrument, manager.quote(symbol), time.time())
        with self._lock:
            book = self._book_now()      # 重新取: 读报价期间可能有人重置了账户
            now = self._clock()
            changed = book.settle(quotes, now)
            changed = book.match(quotes, now) or changed
            if changed:
                self._save(book)
