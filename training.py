"""确定性盲盘训练：下一根开盘撮合、服务端游标、独立持久账户和复盘。

一分钟 OHLC 模型，不伪造盘口。首版单仓、整笔开平，不跨日盘/夜盘块。
所有改变都经过版本检查和原子保存；HTTP 从不返回尚未揭示的 bar。
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from training_data import (CST, IDENTITY, DatasetStore, TrainingError, atomic_json,
                           connected, number)


class Conflict(TrainingError):
    pass


def money(value):
    return round(float(value), 8)


def config_from(raw):
    mode = raw.get("feeMode", "fixed")
    if mode not in ("fixed", "ratio"):
        raise TrainingError("手续费模式须为按手或按成交额")
    return {"initialCash": number(raw.get("initialCash", 100000), "初始资金", 100, 1e9),
            "marginRate": number(raw.get("marginRate", .15), "保证金率", .001, 1),
            "slippageTicks": number(raw.get("slippageTicks", 1), "滑点", 0, 100, integer=True),
            "feeMode": mode,
            "openFee": number(raw.get("openFee", 2), "开仓手续费", 0, .1 if mode == "ratio" else 1e5),
            "closeFee": number(raw.get("closeFee", 2), "日内平仓手续费", 0, .1 if mode == "ratio" else 1e5),
            "riskBudget": number(raw.get("riskBudget", 0), "单笔风险预算", 0, 1e9)}


def execution_price(price, buy, dataset, config):
    tick = Decimal(str(dataset["priceTick"]))
    value = Decimal(str(price)) + tick * config["slippageTicks"] * (1 if buy else -1)
    value = (value / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick
    if value <= 0:
        raise TrainingError("滑点导致成交价无效，请减少滑点")
    return float(value)


def commission(price, qty, opening, dataset, config):
    rate = config["openFee" if opening else "closeFee"]
    return money(rate * qty * (price * dataset["multiplier"] if config["feeMode"] == "ratio" else 1))


def stop_values(raw, direction, reference, tick):
    values = {}
    for key, label in (("sl", "止损"), ("tp", "止盈")):
        value = raw.get(key)
        if value in (None, ""):
            values[key] = None
            continue
        value = number(value, label, tick, 1e12)
        if abs(value / tick - round(value / tick)) > 1e-6:
            raise TrainingError(f"{label}须为最小变动价位 {tick:g} 的整数倍")
        profit_side = (value - reference) * direction
        if (key == "sl" and profit_side >= 0) or (key == "tp" and profit_side <= 0):
            raise TrainingError(f"{label}必须在当前价格的{'亏损' if key == 'sl' else '盈利'}一侧")
        values[key] = value
    return values


def account(state, dataset, index=None):
    index = state["cursor"] if index is None else index
    last = dataset["bars"][index]["close"]
    pos = state["position"]
    floating = (last - pos["price"]) * pos["direction"] * pos["qty"] * dataset["multiplier"] if pos else 0
    margin = last * pos["qty"] * dataset["multiplier"] * state["config"]["marginRate"] if pos else 0
    return {"cash": money(state["cash"]), "equity": money(state["cash"] + floating),
            "floating": money(floating), "margin": money(margin),
            "available": money(state["cash"] + floating - margin), "fees": money(state["fees"]),
            "netPnl": money(state["cash"] + floating - state["config"]["initialCash"])}


def event(state, kind, **details):
    state["events"].append({"id": len(state["events"]) + 1, "index": state["cursor"],
                            "kind": kind, **details})


def sample_position(pos, price, multiplier):
    pnl = (price - pos["price"]) * pos["direction"] * pos["qty"] * multiplier
    pos["mfe"] = max(pos["mfe"], pnl)
    pos["mae"] = min(pos["mae"], pnl)


def close_position(state, dataset, reference, reason, *, ambiguous=False, at_open=False):
    pos, config = state["position"], state["config"]
    price = execution_price(reference, pos["direction"] < 0, dataset, config)
    fee = commission(price, pos["qty"], False, dataset, config)
    gross = money((price - pos["price"]) * pos["direction"] * pos["qty"] * dataset["multiplier"])
    sample_position(pos, price, dataset["multiplier"])
    net = money(gross - fee - pos["entryFee"])
    trade = {**copy.deepcopy(pos), "exitIndex": state["cursor"], "exitPrice": price,
             "exitFee": fee, "grossPnl": gross, "netPnl": net, "reason": reason,
             "ambiguous": ambiguous, "r": net / pos["initialRisk"] if pos["initialRisk"] else None}
    state["trades"].append(trade)
    state["cash"] = money(state["cash"] + gross - fee)
    state["fees"] = money(state["fees"] + fee)
    event(state, "fill", side="sell" if pos["direction"] > 0 else "buy", price=price,
          qty=pos["qty"], reason=reason, ambiguous=ambiguous, phase="open" if at_open else "intrabar")
    state["position"] = None


def stops_hit(pos, bar, opening=False):
    direction, sl, tp = pos["direction"], pos["sl"], pos["tp"]
    low = high = bar["open"]
    if not opening:
        low, high = bar["low"], bar["high"]
    loss = sl is not None and (low <= sl if direction > 0 else high >= sl)
    profit = tp is not None and (high >= tp if direction > 0 else low <= tp)
    if loss:
        return "止损", bar["open"] if opening else sl, profit and not opening
    if profit:
        return "止盈", bar["open"] if opening else tp, False
    return None


def record_frame(state, dataset):
    summary = account(state, dataset)
    if state["frames"] and state["frames"][-1]["index"] == state["cursor"]:
        state["frames"].pop()
    state["frames"].append({"index": state["cursor"], "account": summary,
                            "position": copy.deepcopy(state["position"]), "pending": copy.deepcopy(state["pending"])})


def advance(state, dataset):
    state["cursor"] += 1
    bar = dataset["bars"][state["cursor"]]
    # 无成交或已知一字涨跌停不声称能够成交；同一分钟共享成交量上限。
    locked = bar["high"] == bar["low"] and bar["close"] in (bar.get("upper_limit"), bar.get("lower_limit"))
    capacity = 0 if locked else bar["volume"]
    pos = state["position"]
    if pos:
        sample_position(pos, bar["open"], dataset["multiplier"])
        triggered = pos.get("triggeredExit")
        hit = (triggered["reason"], bar["open"], triggered["ambiguous"]) if triggered else stops_hit(pos, bar, opening=True)
        if hit and capacity >= pos["qty"]:
            capacity -= pos["qty"]
            close_position(state, dataset, hit[1], hit[0] + "（开盘）", ambiguous=hit[2], at_open=True)
        elif hit:
            pos["triggeredExit"] = {"reason": hit[0], "ambiguous": hit[2]}
    pending = state["pending"]
    if pending:
        pos = state["position"]
        if pending["side"] == "close" and not pos:
            state["pending"] = None
            event(state, "cancel", reason="持仓已由止盈止损平掉，撤销待平仓指令")
        elif capacity >= pending["qty"]:
            state["pending"] = None
            if pending["side"] == "close":
                capacity -= pos["qty"]
                close_position(state, dataset, bar["open"], "手动平仓", at_open=True)
            else:
                direction = 1 if pending["side"] == "buy" else -1
                price = execution_price(bar["open"], direction > 0, dataset, state["config"])
                fee = commission(price, pending["qty"], True, dataset, state["config"])
                try:
                    stops = stop_values(pending, direction, price, dataset["priceTick"])
                    margin = price * pending["qty"] * dataset["multiplier"] * state["config"]["marginRate"]
                    if margin + fee > state["cash"]:
                        raise TrainingError("开盘成交价下保证金不足")
                    risk = abs(price - stops["sl"]) * pending["qty"] * dataset["multiplier"] if stops["sl"] else None
                    if risk and state["config"]["riskBudget"] and risk > state["config"]["riskBudget"]:
                        raise TrainingError("开盘成交价下超过本局单笔风险预算")
                except TrainingError as exc:
                    event(state, "reject", reason=str(exc))
                else:
                    capacity -= pending["qty"]
                    state["cash"] = money(state["cash"] - fee)
                    state["fees"] = money(state["fees"] + fee)
                    state["position"] = {"direction": direction, "qty": pending["qty"], "price": price,
                                         "entryIndex": state["cursor"], "entryFee": fee,
                                         "initialRisk": risk, "initialSl": stops["sl"], **stops,
                                         "note": pending["note"], "mfe": 0, "mae": 0}
                    event(state, "fill", side=pending["side"], price=price, qty=pending["qty"], reason="开仓", phase="open")
        else:
            event(state, "wait", reason="本根无可用成交量或已知一字涨跌停，委托继续等待")
    pos = state["position"]
    if pos:
        hit = stops_hit(pos, bar)
        if hit and capacity >= pos["qty"]:
            close_position(state, dataset, hit[1], hit[0], ambiguous=hit[2])
        elif hit:
            pos["triggeredExit"] = {"reason": hit[0], "ambiguous": hit[2]}
            event(state, "wait", reason="保护平仓已触发但本根无法成交，等待后续开盘")
    if state["position"]:
        sample_position(state["position"], bar["close"], dataset["multiplier"])
    if state["cursor"] >= state["end"]:
        finish(state, "到达本段行情末尾")
    record_frame(state, dataset)


def finish(state, reason):
    if state["pending"]:
        event(state, "cancel", reason="训练结束，撤销未成交指令")
    state["pending"] = None
    state["status"] = "completed"
    event(state, "finish", reason=reason)


def apply_command(state, dataset, raw):
    if state["status"] != "active":
        raise TrainingError("训练已结束，只能复盘或重新开始")
    kind = raw.get("action")
    if kind == "step":
        advance(state, dataset)
    elif kind == "order":
        if state["pending"]:
            raise TrainingError("已有待成交指令，请先推进或撤销")
        side = raw.get("side")
        if side not in ("buy", "sell", "close"):
            raise TrainingError("方向须为买入、卖出或平仓")
        if side == "close":
            if not state["position"]:
                raise TrainingError("没有可平持仓")
            pending = {"side": side, "qty": state["position"]["qty"], "sl": None, "tp": None, "note": ""}
        else:
            if state["position"]:
                raise TrainingError("首版每次持有一笔仓位，请先平仓")
            qty = number(raw.get("qty"), "手数", 1, 500, integer=True)
            stops = stop_values(raw, 1 if side == "buy" else -1,
                                dataset["bars"][state["cursor"]]["close"], dataset["priceTick"])
            pending = {"side": side, "qty": qty, **stops, "note": str(raw.get("note") or "")[:500]}
        state["pending"] = pending
        event(state, "order", **pending, reason="等待下一根开盘")
    elif kind == "cancel":
        if not state["pending"]:
            raise TrainingError("没有待成交指令")
        state["pending"] = None
        event(state, "cancel", reason="手动撤销")
    elif kind == "stops":
        pos = state["position"]
        if not pos:
            raise TrainingError("没有持仓，开仓保护价请在下单时填写")
        if pos.get("triggeredExit"):
            raise TrainingError("保护平仓已经触发，等待成交期间不能修改保护价")
        stops = stop_values(raw, pos["direction"], dataset["bars"][state["cursor"]]["close"], dataset["priceTick"])
        old = {key: pos[key] for key in ("sl", "tp")}
        widened = old["sl"] is not None and (stops["sl"] is None or (stops["sl"] - old["sl"]) * pos["direction"] < 0)
        pos.update(stops)
        event(state, "stops", before=old, **stops, widened=widened,
              reason=str(raw.get("note") or "调整保护价")[:500])
    elif kind == "finish":
        finish(state, "手动结束；未平仓按最后已揭示收盘价估值")
    else:
        raise TrainingError("未知训练操作")
    if kind != "step":
        record_frame(state, dataset)


def report(state, dataset):
    trades = state["trades"]
    wins = [trade["netPnl"] for trade in trades if trade["netPnl"] > 0]
    losses = [trade["netPnl"] for trade in trades if trade["netPnl"] < 0]
    peak, drawdown = state["config"]["initialCash"], 0
    for frame in state["frames"]:
        equity = frame["account"]["equity"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    entries = [*trades, *([state["position"]] if state["position"] else [])]
    return {**account(state, dataset), "count": len(trades),
            "winRate": len(wins) / len(trades) if trades else None,
            "averageWin": sum(wins) / len(wins) if wins else None,
            "averageLoss": sum(losses) / len(losses) if losses else None,
            "profitFactor": sum(wins) / abs(sum(losses)) if losses else None,
            "maxDrawdown": money(drawdown), "closedNetPnl": money(sum(t["netPnl"] for t in trades)),
            "entries": len(entries), "protectedEntries": sum(t["initialSl"] is not None for t in entries),
            "widenedStops": sum(e["kind"] == "stops" and e["widened"] for e in state["events"]),
            "ambiguousTrades": sum(t["ambiguous"] for t in trades),
            "sampling": "权益回撤按一分钟收盘采样；逐笔最大浮盈/浮亏按开盘、收盘及成交价格采样"}


class TrainingService:
    def __init__(self, root_provider):
        self.datasets = DatasetStore(root_provider)
        self.lock = threading.RLock()

    def _path(self, identity):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise TrainingError("训练编号无效")
        return self.datasets.root() / "sessions" / f"{identity}.json"

    def _load(self, identity):
        try:
            return json.loads(self._path(identity).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TrainingError("训练记录不存在") from None

    def create(self, raw):
        if not isinstance(raw, dict):
            raise TrainingError("训练配置格式错误")
        request_id = str(raw.get("requestId") or uuid.uuid4().hex)
        if len(request_id) > 100:
            raise TrainingError("请求编号过长")
        identity = hashlib.sha256(("create:" + request_id).encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self._path(identity).exists():
                old = self._load(identity)
                if old["creationFingerprint"] != fingerprint:
                    raise Conflict("请求编号已用于另一组训练配置")
                return self.view(identity)
            dataset_id = raw.get("datasetId")
            dataset = self.datasets.load(dataset_id)
            bars = dataset["bars"]
            warmup = number(raw.get("warmup", 200), "背景根数", 20, 500, integer=True)
            steps = number(raw.get("steps", 90), "训练根数", 10, 1000, integer=True)
            warmup = min(warmup, len(bars) - 10)
            start_date = raw.get("startDate") or ""
            if start_date:
                try:
                    datetime.strptime(start_date, "%Y-%m-%d")
                except (ValueError, TypeError):
                    raise TrainingError("指定日期格式须为 YYYY-MM-DD") from None
            # 从每一根向右计算没有缺口、且不跨日夜盘块的可用范围。
            ends = [len(bars) - 1] * len(bars)
            for i in range(len(bars) - 2, -1, -1):
                ends[i] = ends[i + 1] if connected(bars[i]["time"], bars[i + 1]["time"], dataset) else i
            candidates = [i for i in range(warmup - 1, len(bars) - 10)
                          if ends[i] - i >= 10 and (not start_date or
                              datetime.fromtimestamp(bars[i]["time"], CST).date().isoformat() == start_date)]
            if not candidates:
                raise TrainingError("所选日期没有足够的连续行情，请减少背景根数或补充数据；至少需要 10 根训练行情")
            full = [i for i in candidates if ends[i] - i >= steps]
            start = random.SystemRandom().choice(full or candidates)
            end = min(start + steps, ends[start])
            state = {"version": 1, "revision": 0, "id": identity, "datasetId": dataset_id,
                     "createdAt": datetime.now(timezone.utc).isoformat(), "creationFingerprint": fingerprint,
                     "status": "active", "start": start, "cursor": start, "end": end,
                     "warmupStart": max(0, start - warmup + 1), "hideDate": raw.get("hideDate", True) is True,
                     "config": config_from(raw), "cash": 0, "fees": 0, "position": None,
                     "pending": None, "trades": [], "events": [], "frames": [], "receipts": {}}
            state["cash"] = state["config"]["initialCash"]
            record_frame(state, dataset)
            atomic_json(self._path(identity), state)
            return self.view(identity)

    def command(self, identity, raw):
        if not isinstance(raw, dict):
            raise TrainingError("操作格式错误")
        request_id = raw.get("requestId")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise TrainingError("每次操作必须携带 requestId")
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        with self.lock:
            state = self._load(identity)
            if request_id in state["receipts"]:
                if state["receipts"][request_id] != fingerprint:
                    raise Conflict("请求编号已用于另一项操作")
                return self.view(identity)
            revision = number(raw.get("revision"), "训练版本", 0, 1e9, integer=True)
            if revision != state["revision"]:
                raise Conflict("训练已在另一页面推进，请刷新状态后重试")
            dataset = self.datasets.load(state["datasetId"])
            apply_command(state, dataset, raw)
            state["revision"] += 1
            state["receipts"][request_id] = fingerprint
            state["receipts"] = dict(list(state["receipts"].items())[-128:])
            atomic_json(self._path(identity), state)
            return self.view(identity)

    def view(self, identity, at=None):
        with self.lock:
            state = self._load(identity)
        dataset = self.datasets.load(state["datasetId"])
        bars = dataset["bars"]
        completed = state["status"] == "completed"
        cursor = state["cursor"]
        if at is not None:
            if not completed:
                raise TrainingError("训练进行中不能跳转时间")
            cursor = number(at, "复盘位置", state["start"], state["end"], integer=True)
        hidden = state["hideDate"] and not completed
        # 盲盘时日期整体平移到固定锚点，保持分钟位置；不把原始时间发送给客户端。
        offset = 8 * 3600
        if hidden:
            local_midnight = ((bars[state["start"]]["time"] + offset) // 86400) * 86400
            offset += 946857600 - local_midnight  # 固定日期，仅用于图表坐标

        def display_bar(bar):
            return {key: bar[key] for key in ("open", "high", "low", "close", "volume")} | {"time": bar["time"] + offset}

        def displayed(item):
            result = copy.deepcopy(item)
            for key in ("index", "entryIndex", "exitIndex"):
                if key in result:
                    result[key.replace("Index", "Time") if key != "index" else "time"] = bars[result[key]]["time"] + offset
            return result

        frame = next((f for f in reversed(state["frames"]) if f["index"] <= cursor), state["frames"][0])
        reviewing = at is not None
        position = frame["position"] if reviewing else state["position"]
        pending = frame["pending"] if reviewing else state["pending"]
        summary = frame["account"] if reviewing else account(state, dataset)
        events = [displayed(e) for e in state["events"] if e["index"] <= cursor]
        return {"id": identity, "revision": state["revision"], "status": state["status"],
                "name": (dataset["symbol"].rstrip("0123456789") + (" · 合成演示" if dataset["demo"] else " · 盲盘")) if hidden else dataset["name"],
                "symbol": dataset["symbol"].rstrip("0123456789") if hidden else dataset["symbol"],
                "demo": dataset["demo"], "hideDate": hidden, "priceTick": dataset["priceTick"],
                "multiplier": dataset["multiplier"], "config": state["config"],
                "cursor": state["cursor"], "viewCursor": cursor, "start": state["start"], "end": state["end"],
                "bars": [display_bar(b) for b in bars[state["warmupStart"]:cursor + 1]],
                "clock": datetime.fromtimestamp(bars[cursor]["time"] + 60, CST).strftime("%H:%M" if hidden else "%Y-%m-%d %H:%M"),
                "position": displayed(position) if position else None, "pending": pending,
                "account": summary, "events": events, "trades": [displayed(t) for t in state["trades"] if t["exitIndex"] <= cursor],
                "equity": [{"time": bars[f["index"]]["time"] + offset, "value": f["account"]["equity"]}
                           for f in state["frames"] if f["index"] <= cursor],
                "report": report(state, dataset) if completed else None,
                "finishedCursor": state["cursor"] if completed else None}

    def list_sessions(self):
        result = []
        with self.lock:
            files = sorted((self.datasets.root() / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[:100]:
                if not IDENTITY.fullmatch(path.stem):
                    continue
                state = self._load(path.stem)
                dataset = self.datasets.load(state["datasetId"])
                hidden = state["hideDate"] and state["status"] == "active"
                result.append({"id": path.stem, "name": dataset["symbol"].rstrip("0123456789") if hidden else dataset["name"],
                               "demo": dataset["demo"], "status": state["status"], "createdAt": state["createdAt"],
                               "progress": state["cursor"] - state["start"], "total": state["end"] - state["start"],
                               "netPnl": account(state, dataset)["netPnl"]})
        return result
