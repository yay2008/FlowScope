"""盲盘训练的不可变一分钟数据集；不读取实时 Feed 或模拟账户。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

CST = timezone(timedelta(hours=8))
CONTRACT = re.compile(r"(?:SHFE|DCE|CZCE|INE|GFEX)\.[A-Za-z]+\d{3,4}\Z")
IDENTITY = re.compile(r"[a-f0-9]{32}\Z")
MAX_BARS = 100_000
MAX_CSV_BYTES = 20 * 1024 * 1024


class TrainingError(ValueError):
    pass


def number(value, label, low=0, high=1e12, *, integer=False):
    if isinstance(value, bool) or value is None:
        raise TrainingError(f"{label}必须是有效数字")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise TrainingError(f"{label}必须是有效数字") from None
    if not math.isfinite(value) or not low <= value <= high or (integer and value != int(value)):
        raise TrainingError(f"{label}须在 {low:g}～{high:g} 之间" + ("，且为整数" if integer else ""))
    return int(value) if integer else value


def timestamp(value):
    """UTC 秒；无时区 ISO 时间按北京时间。Tq datetime_nano 不经浮点转换。"""
    try:
        numeric = Decimal(str(value))
    except InvalidOperation:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            return timestamp(int((dt if dt.tzinfo else dt.replace(tzinfo=CST)).timestamp()))
        except (ValueError, TypeError, OverflowError):
            raise TrainingError("时间须为 ISO 日期时间、Unix 秒/毫秒/纳秒") from None
    if not numeric.is_finite() or numeric <= 0:
        raise TrainingError("时间戳无效")
    if numeric >= Decimal("1e17"):
        numeric /= 1_000_000_000
    elif numeric >= Decimal("1e14"):
        numeric /= 1_000_000
    elif numeric >= Decimal("1e11"):
        numeric /= 1000
    result = int(numeric)
    if not 631152000 <= result <= 4102444800:
        raise TrainingError("时间超出 1990～2100 年范围")
    return result


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def parse_csv(text: str, symbol: str):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise TrainingError("CSV 不能超过 20 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = reader.fieldnames or []
    # 接受标准 OHLCV 及天勤单/多合约导出；多合约时只读取明确选中的合约列。
    prefix = symbol + "." if symbol + ".open" in columns else ""
    required = [prefix + key for key in ("open", "high", "low", "close", "volume")]
    if not all(key in columns for key in required):
        raise TrainingError("CSV 缺少 open/high/low/close/volume，或所填合约与天勤表头不一致")
    time_key = next((key for key in ("datetime_nano", "time", "datetime") if key in columns), None)
    if not time_key:
        raise TrainingError("CSV 缺少 time、datetime 或 datetime_nano 时间列")
    rows = []
    try:
        for index, row in enumerate(reader):
            if index >= MAX_BARS:
                raise TrainingError("单个数据集最多 100000 根一分钟 K 线")
            bar = {key: row[prefix + key] for key in ("open", "high", "low", "close", "volume")}
            bar["time"] = row[time_key]
            for key in ("upper_limit", "lower_limit"):
                value = row.get(prefix + key) or row.get(key)
                if value:
                    bar[key] = value
            rows.append(bar)
    except (csv.Error, KeyError, TypeError):
        raise TrainingError("CSV 行格式不完整") from None
    return rows


def normalize_dataset(meta, rows):
    if not isinstance(meta, dict) or not isinstance(rows, list) or not 40 <= len(rows) <= MAX_BARS:
        raise TrainingError("数据集需要 40～100000 根一分钟 K 线")
    symbol = str(meta.get("symbol", "")).strip()
    if not CONTRACT.fullmatch(symbol):
        raise TrainingError("请填写商品期货实际月份合约，如 SHFE.fu2611；不接受主连或金融期货")
    tick = number(meta.get("priceTick"), "最小变动价位", 1e-6, 1e6)
    multiplier = number(meta.get("multiplier"), "合约乘数", 1e-6, 1e9)
    bars = []
    previous = 0
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise TrainingError(f"第 {index + 1} 根 K 线格式无效")
        ts = timestamp(raw.get("time"))
        if ts <= previous or ts % 60:
            raise TrainingError(f"第 {index + 1} 根时间须按一分钟起点严格递增，不能重复")
        bar = {key: number(raw.get(key), f"第 {index + 1} 根 {key}", 1e-9)
               for key in ("open", "high", "low", "close")}
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
            raise TrainingError(f"第 {index + 1} 根 OHLC 高低范围错误")
        for price in bar.values():
            if abs(price / tick - round(price / tick)) > 1e-5:
                raise TrainingError(f"第 {index + 1} 根价格不符合最小变动价位")
        bar.update(time=ts, volume=number(raw.get("volume"), "成交量", integer=True))
        for key in ("upper_limit", "lower_limit"):
            if raw.get(key) is not None:
                bar[key] = number(raw[key], key, 1e-9)
        bars.append(bar)
        previous = ts
    trading_time = meta.get("tradingTime") or {}
    if not isinstance(trading_time, dict):
        raise TrainingError("交易时段格式错误")
    # 只保存校验过的时段，避免把任意元数据混入数据集或前端。
    sessions = {}
    for key in ("day", "night"):
        pairs = trading_time.get(key, [])
        if not isinstance(pairs, list) or len(pairs) > 10:
            raise TrainingError("交易时段格式错误")
        sessions[key] = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise TrainingError("交易时段格式错误")
            start, end = map(clock_seconds, pair)
            if start >= end:
                raise TrainingError("交易时段结束须大于开始；跨午夜使用 25:00 等写法")
            sessions[key].append([start, end])
    result = {"version": 1, "symbol": symbol, "name": str(meta.get("name") or symbol)[:80],
              "priceTick": tick, "multiplier": multiplier, "period": 60,
              "source": str(meta.get("source") or "CSV 导入")[:160], "demo": meta.get("demo") is True,
              "sessions": sessions, "bars": bars}
    return result


def clock_seconds(value):
    try:
        hour, minute, second = (int(part) for part in str(value).split(":"))
        if not (0 <= hour <= 30 and 0 <= minute < 60 and 0 <= second < 60):
            raise ValueError()
        return hour * 3600 + minute * 60 + second
    except (ValueError, TypeError):
        raise TrainingError("交易时段须为 HH:MM:SS") from None


def market_block(ts):
    """日盘或一段跨午夜夜盘；首版不跨日/夜盘结算边界持仓。"""
    local = datetime.fromtimestamp(ts, CST)
    if local.hour >= 20:
        return local.date().isoformat(), "night"
    if local.hour < 8:
        return (local.date() - timedelta(days=1)).isoformat(), "night"
    return local.date().isoformat(), "day"


def connected(previous, current, dataset):
    if market_block(previous) != market_block(current):
        return False
    if current - previous == 60:
        return True
    day, kind = market_block(current)
    pairs = dataset["sessions"].get(kind, [])
    if not pairs or current - previous > 6 * 3600:
        return False
    midnight = int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp())
    # 仅当缺失分钟全部属于已知休市时段才跨过，否则在缺口处分段。
    return all(not any(start <= ts - midnight < end for start, end in pairs)
               for ts in range(previous + 60, current, 60))


def dataset_summary(identity, dataset):
    bars = dataset["bars"]
    return {key: dataset[key] for key in ("symbol", "name", "priceTick", "multiplier", "source", "demo")} | {
        "id": identity, "count": len(bars),
        "from": datetime.fromtimestamp(bars[0]["time"], CST).isoformat(),
        "to": datetime.fromtimestamp(bars[-1]["time"], CST).isoformat(),
        "breaks": sum(not connected(a["time"], b["time"], dataset) for a, b in zip(bars, bars[1:])),
        "hasSessions": bool(dataset["sessions"].get("day") or dataset["sessions"].get("night"))}


class DatasetStore:
    def __init__(self, root_provider):
        self.root_provider = root_provider

    def root(self):
        return Path(self.root_provider())

    def save(self, meta, rows):
        dataset = normalize_dataset(meta, rows)
        identity = hashlib.sha256(json.dumps(dataset, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()[:32]
        path = self.root() / "datasets" / f"{identity}.json"
        if not path.exists():
            atomic_json(path, dataset)
        return dataset_summary(identity, dataset)

    def load(self, identity):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise TrainingError("数据集编号无效")
        try:
            return json.loads((self.root() / "datasets" / f"{identity}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TrainingError("数据集不存在") from None

    def list(self):
        result = []
        for path in sorted((self.root() / "datasets").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if IDENTITY.fullmatch(path.stem):
                result.append(dataset_summary(path.stem, self.load(path.stem)))
        return result


def demo_dataset():
    """明确标记的合成样本，仅用于体验与离线验收。"""
    bars, price = [], 3000
    for day in range(5):
        start = datetime(2025, 1, 6 + day, 9, tzinfo=CST)
        for minute in range(120):
            delta = round(3 * math.sin(minute / 7 + day) + 2 * math.cos(minute / 3))
            close = price + delta
            bars.append({"time": int((start + timedelta(minutes=minute)).timestamp()),
                         "open": price, "high": max(price, close) + 2 + minute % 4,
                         "low": min(price, close) - 2 - minute % 3, "close": close,
                         "volume": 100 + (minute * 37) % 500})
            price = close
    return {"symbol": "SHFE.fu2505", "name": "合成演示 · 非真实行情", "priceTick": 1,
            "multiplier": 10, "demo": True, "source": "固定算法生成的合成样本"}, bars
