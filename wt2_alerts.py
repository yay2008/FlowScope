"""自选五分钟收盘警报：消费图表同一份 FlowWave 结果，独立线程去重、落盘和通知。"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from wt2 import VERSION, finite

TF = 300
SHIFT = 8 * 3600                 # 项目图表时间是 UTC 秒 + 8 小时
FRESH_SECONDS = 90              # 更早的重连补回信号只入记录，不弹过期警报
SETTLE_SECONDS = 3


def zone_of(value):
    return "overbought" if value > 80 else "oversold" if value < 20 else "neutral"


def crypto_live(manager, symbol):
    if manager.status != "running":
        return False
    parts = manager.aggregates.components(symbol) if symbol.startswith("AGG.") and manager.aggregates else [symbol]
    required = set()
    for part in parts:
        instrument = manager.instrument(part)
        if instrument is None:
            return False
        for spec in manager.adapters[instrument.venue].channels:
            if spec.topic("trade", instrument.inst_id) is not None:
                required.add(f"{instrument.venue}.{spec.name}")
    connected = {channel.key for channel in manager.channels if channel.status == "connected"}
    return bool(required) and required <= connected


def closed_futures(rows, now, quote_time="", trading_time=None, healthy=True):
    """换根确认；收市最后一根另校验时段终点、本时段报价和连接健康。"""
    display_now = now + SHIFT
    closed = [row for row in rows[:-1] if row["time"] + TF + SETTLE_SECONDS <= display_now]
    if not rows or not healthy:
        return closed
    last = rows[-1]
    end = last["time"] + TF
    if end + SETTLE_SECONDS > display_now:
        return closed
    try:
        quote_stamp = datetime.fromisoformat(quote_time).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return closed
    # 时段表可能仍是前一交易日的；当天这根确实有末分钟报价才可收尾。
    if not end - 60 <= quote_stamp <= end + SETTLE_SECONDS:
        return closed
    ending = datetime.fromtimestamp(end, timezone.utc)
    for day in (ending.date(), ending.date() - timedelta(days=1)):
        midnight = datetime.combine(day, datetime.min.time(), timezone.utc)
        for periods in (trading_time or {}).values():
            for period in periods:
                try:
                    h, m, s = map(int, period[1].split(":"))
                    boundary = (midnight + timedelta(hours=h, minutes=m, seconds=s)).timestamp()
                except (ValueError, TypeError, IndexError):
                    continue
                if boundary == end:
                    return [*closed, last]
    return closed


class AlertStore:
    def __init__(self, path_provider):
        self.path_provider = path_provider
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        with self.lock:
            path = self.path_provider()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            connection = sqlite3.connect(path, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS states (
                        symbol TEXT PRIMARY KEY, contract TEXT NOT NULL, bar INTEGER NOT NULL,
                        zone TEXT NOT NULL, wt2 REAL NOT NULL, name TEXT NOT NULL,
                        last_seen INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, contract TEXT NOT NULL,
                        bar INTEGER NOT NULL, zone TEXT NOT NULL, wt2 REAL NOT NULL,
                        price REAL NOT NULL, name TEXT NOT NULL, created REAL NOT NULL,
                        delivery TEXT NOT NULL, detail TEXT);
                """)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(states)")}
                for column in ("last_seen", "gap"):
                    if column not in columns:
                        connection.execute(f"ALTER TABLE states ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
                # 同时开启两个进程时，读取状态、生成事件、推进游标仍在同一个写事务。
                connection.execute("BEGIN IMMEDIATE")
                with connection:
                    yield connection
            finally:
                connection.close()

    @staticmethod
    def config(db):
        row = db.execute("SELECT value FROM config WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {"enabled": True, "disabledSymbols": [], "members": []}

    @staticmethod
    def save_config(db, config):
        db.execute("INSERT OR REPLACE INTO config VALUES (1,?)", (json.dumps(config),))

    def reconcile(self, symbols):
        with self.db() as db:
            config = self.config(db)
            if config["members"] != symbols:
                removed = set(config["members"]) - set(symbols)
                for symbol in removed:
                    db.execute("DELETE FROM states WHERE symbol=?", (symbol,))
                    db.execute("UPDATE events SET delivery='cancelled' WHERE symbol=? AND delivery='pending'", (symbol,))
                config["members"] = list(symbols)
                config["disabledSymbols"] = [s for s in config["disabledSymbols"] if s in symbols]
                self.save_config(db, config)
            return config

    def configure(self, enabled, symbol=None):
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        with self.db() as db:
            config = self.config(db)
            if symbol is None:
                changed = config["enabled"] != enabled
                config["enabled"] = enabled
                affected = config["members"] if changed else []
            else:
                if symbol not in config["members"]:
                    raise ValueError("只能设置当前自选合约的警报")
                disabled = set(config["disabledSymbols"])
                changed = (symbol not in disabled) != enabled
                disabled.discard(symbol) if enabled else disabled.add(symbol)
                config["disabledSymbols"] = sorted(disabled)
                affected = [symbol] if changed else []
            for item in affected:
                # 重新启用从当前状态开始，关闭期间的旧信号不重放。
                db.execute("DELETE FROM states WHERE symbol=?", (item,))
                db.execute("UPDATE events SET delivery='cancelled' WHERE symbol=? AND delivery='pending'", (item,))
            self.save_config(db, config)

    def process(self, payload, now):
        symbol, contract, rows = payload["symbol"], payload["contract"], payload["bars"]
        with self.db() as db:
            config = self.config(db)
            if not config["enabled"] or symbol in config["disabledSymbols"] or symbol not in config["members"]:
                return {"status": "disabled"}
            old = db.execute("SELECT * FROM states WHERE symbol=?", (symbol,)).fetchone()
            if not rows:
                return {"status": "warming", "detail": "等待已收盘 K 线"}
            values = [r.get("flowWave", {}).get("wt2")
                      if r.get("flowWave", {}).get("version") == VERSION else None for r in rows]
            values = [v if finite(v) else None for v in values]
            cursor = max(old["bar"], old["last_seen"]) if old and old["contract"] == contract else None
            if cursor is not None and cursor > rows[-1]["time"]:
                return {"status": "monitoring" if not old["gap"] else "warming", **dict(old)}
            baseline = cursor is None or cursor < rows[0]["time"]
            prior = old["zone"] if not baseline and not old["gap"] else None
            # 回填可能修订已处理根的值；从修订后的状态接续，但不对同一根重新发警报。
            for row, value in zip(rows, values):
                if row["time"] == cursor:
                    prior = zone_of(value) if value is not None else None
            last_valid = None
            for row, value in zip(rows, values):
                if value is not None:
                    last_valid = (row, value)
                if not baseline and row["time"] <= cursor:
                    continue
                if value is None:
                    prior = None
                    continue
                current = zone_of(value)
                if not baseline and prior is not None and current != prior and current != "neutral":
                    identity = hashlib.sha256(f'v1|{symbol}|{contract}|{row["time"]}|{current}'.encode()).hexdigest()
                    age = now + SHIFT - (row["time"] + TF)
                    delivery = "pending" if 0 <= age <= FRESH_SECONDS else "expired"
                    db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (identity, symbol, contract, row["time"], current, value, row["close"],
                                payload.get("name", symbol), now, delivery, None))
                prior = current
            state = dict(old) if old and old["contract"] == contract else None
            if last_valid:
                row, value = last_valid
                state = {"symbol": symbol, "contract": contract, "bar": int(row["time"]),
                         "zone": zone_of(value), "wt2": value, "name": payload.get("name", symbol)}
            if state:
                state.update(last_seen=int(rows[-1]["time"]), gap=int(values[-1] is None))
                db.execute("""INSERT OR REPLACE INTO states
                    (symbol,contract,bar,zone,wt2,name,last_seen,gap)
                    VALUES (:symbol,:contract,:bar,:zone,:wt2,:name,:last_seen,:gap)""", state)
            if values[-1] is None:
                reason = rows[-1].get("flowWave", {}).get("reason")
                detail = {"volume": "成交量缺失，等待补齐或移出 WT2 计算窗口",
                          "price": "价格数据不完整，等待恢复",
                          "warming": "WT2 按公式窗口预热中"}.get(reason, "等待图表 WT2 计算结果")
                return {"status": "warming", "detail": detail, "wt2": None, "name": payload.get("name", symbol)}
            return {"status": "monitoring", **state}

    def recover(self):
        with self.db() as db:
            # OS 接收与 SQLite 无法组成原子事务。发送中被中断标明未知，不盲目重发造成重复响。
            db.execute("UPDATE events SET delivery='unknown', detail='发送时进程中断，避免重复通知未自动重发' WHERE delivery='sending'")

    def claim(self, now):
        with self.db() as db:
            config = self.config(db)
            rows = db.execute("SELECT * FROM events WHERE delivery='pending' ORDER BY bar, symbol").fetchall()
            batch = []
            for item in rows:
                row = dict(item)
                if not config["enabled"] or row["symbol"] in config["disabledSymbols"] or row["symbol"] not in config["members"]:
                    delivery = "cancelled"
                elif now + SHIFT - row["bar"] - TF > FRESH_SECONDS:
                    delivery = "expired"
                else:
                    delivery = "sending"
                    batch.append(row)
                db.execute("UPDATE events SET delivery=? WHERE id=?", (delivery, row["id"]))
            return batch

    def delivered(self, events, error=None):
        with self.db() as db:
            db.executemany("UPDATE events SET delivery=?,detail=? WHERE id=?",
                           [("failed" if error else "sent", error, event["id"]) for event in events])

    def snapshot(self):
        with self.db() as db:
            return {**self.config(db), "states": {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM states")},
                    "events": [dict(r) for r in db.execute("SELECT * FROM events ORDER BY created DESC,bar DESC LIMIT 50")]}


class AlertService:
    def __init__(self, store, favorites, futures, crypto, notifier):
        self.store, self.favorites = store, favorites
        self.futures, self.crypto, self.notifier = futures, crypto, notifier
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        self.desired = set()
        self.pending = {}
        self.runtime = {}
        self.notification = {"status": "starting", "error": None}
        self.tests = queue.Queue(maxsize=1)
        self.error = None
        self._capture_at = {}

    def start(self):
        if any(t.is_alive() for t in self.threads):
            return
        try:
            self.store.recover()
        except Exception as exc:
            self.error = f"警报状态恢复失败：{exc}"
            return  # 警报存储损坏不能拖停行情与模拟交易
        self.stop_event.clear()
        self.threads = [threading.Thread(target=fn, daemon=True, name=name) for fn, name in
                        ((self._run, "wt2-alerts"), (self._notify, "windows-alerts"))]
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=5)
        self.futures().set_background([])
        self.crypto().set_background([])

    def reconcile(self):
        symbols = self.favorites()
        config = self.store.reconcile(symbols)
        enabled = [s for s in symbols if config["enabled"] and s not in config["disabledSymbols"]]
        future, crypto = self.futures(), self.crypto()
        futures, coins, errors = [], [], {}
        for symbol in enabled:
            if symbol.startswith(("BINANCE.", "OKX.", "AGG.")):
                parts = crypto.aggregates.components(symbol) if symbol.startswith("AGG.") and crypto.aggregates else [symbol]
                if not parts or not all(part in crypto.pinned for part in parts):
                    errors[symbol] = "等待合约就绪或超出常驻采集上限"
                else:
                    coins.append((symbol, TF))
            elif (symbol, 30) not in future.pinned:
                errors[symbol] = "超出常驻采集上限，未监控"
            else:
                futures.append((symbol, TF))
        errors.update(future.set_background(futures))
        errors.update(crypto.set_background(coins))
        with self.lock:
            self.desired = set(enabled) - set(errors)
            self.runtime = {s: self.runtime.get(s, {"status": "warming"}) for s in self.desired}
            self.runtime.update({s: {"status": "unavailable", "detail": e} for s, e in errors.items()})
            self.pending = {s: p for s, p in self.pending.items() if s in self.desired}

    def _offer(self, payload):
        with self.lock:
            if payload["symbol"] in self.desired:
                self.pending[payload["symbol"]] = payload

    def _unavailable(self, symbol, detail):
        with self.lock:
            self.runtime[symbol] = {"status": "unavailable", "detail": detail, "checkedAt": time.time()}

    def _capture_keys(self, crypto):
        now = time.monotonic()
        if now - self._capture_at.get(crypto, 0) < 1:
            return []
        self._capture_at[crypto] = now
        with self.lock:
            return [s for s in self.desired if s.startswith(("BINANCE.", "OKX.", "AGG.")) == crypto]

    def on_futures(self, api):
        """在行情线程读取与图表相同的快照，只额外确认收盘。"""
        manager = self.futures()
        if manager.status != "connected":
            return
        for symbol in self._capture_keys(False):
            with manager._lock:
                feed = manager.feeds.get((symbol, TF))
                subscribed = feed is not None and manager._subscribed.get((feed.symbol, TF)) is feed
            if not subscribed or feed.klines is None or feed.error:
                if feed is not None and feed.error:
                    self._unavailable(symbol, feed.error)
                continue
            snapshot = feed.snapshot_for(0, demand=False)
            if not snapshot:
                continue
            rows = snapshot["bars"]
            base = feed.base()
            quote = base.quote if base is not None and base.contract == feed.contract else None
            sessions = getattr(quote, "trading_time", None)
            sessions = {k: list(sessions.get(k, []) if hasattr(sessions, "get") else getattr(sessions, k, []))
                        for k in ("day", "night")} if sessions else {}
            now = time.time()
            closed = closed_futures(rows, now, getattr(quote, "datetime", ""), sessions,
                                    healthy=manager.status == "connected" and not manager.probe_error)
            self._offer({"symbol": symbol, "contract": feed.contract, "bars": closed,
                         "name": getattr(quote, "instrument_name", "") or symbol, "captured": now})

    def on_crypto(self, manager):
        if manager.status != "running":
            return
        for symbol in self._capture_keys(True):
            if not crypto_live(manager, symbol):
                self._unavailable(symbol, "成交行情连接中断，等待恢复")
                continue
            with manager._lock:
                feed = manager.rollups.get((symbol, TF))
            if feed is None or feed.error:
                if feed is not None and feed.error:
                    self._unavailable(symbol, feed.error)
                continue
            snapshot = feed.snapshot_for(0, demand=False)
            if not snapshot:
                continue
            now = time.time()
            rows = [b for b in snapshot["bars"][:-1] if b["time"] + TF + SETTLE_SECONDS <= now + SHIFT]
            self._offer({"symbol": symbol, "contract": symbol, "bars": rows, "crypto": True,
                         "name": manager.label(symbol), "captured": now})

    def _run(self):
        due = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= due or self.wake.is_set():
                    self.wake.clear()
                    self.reconcile()
                    due = time.monotonic() + 3
                with self.lock:
                    pending, self.pending = self.pending, {}
                for symbol, payload in pending.items():
                    result = self.store.process(payload, time.time())
                    with self.lock:
                        self.runtime[symbol] = {**result, "checkedAt": payload["captured"]}
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(0.5)

    def _notify(self):
        try:
            self.notifier.prepare()
            self.notification = {"status": "ready", "error": None}
        except Exception as exc:
            self.notification = {"status": "error", "error": str(exc)}
        while not self.stop_event.wait(1):
            batch = []
            try:
                try:
                    self.tests.get_nowait()
                    self.notifier.send([], test=True)
                    self.notification = {"status": "ready", "error": None, "testedAt": time.time()}
                except queue.Empty:
                    pass
                batch = self.store.claim(time.time())
                if batch:
                    self.notifier.send(batch)
                    self.store.delivered(batch)
                    self.notification = {"status": "ready", "error": None, "sentAt": time.time()}
            except Exception as exc:
                self.notification = {"status": "error", "error": str(exc)}
                if batch:
                    self.store.delivered(batch, str(exc))

    def configure(self, enabled, symbol=None):
        self.store.reconcile(self.favorites())
        self.store.configure(enabled, symbol)
        self.wake.set()

    def test_notification(self):
        try:
            self.tests.put_nowait(True)
        except queue.Full:
            pass
        return {"queued": True}

    def snapshot(self):
        data = self.store.snapshot()
        with self.lock:
            runtime = {s: dict(v) for s, v in self.runtime.items()}
        for symbol in data["members"]:
            item = runtime.setdefault(symbol, {"status": "warming"})
            if not data["enabled"] or symbol in data["disabledSymbols"]:
                item["status"] = "disabled"
            elif item.get("checkedAt") and time.time() - item["checkedAt"] > 20:
                item.update(status="unavailable", detail="行情更新中断，等待恢复")
            else:
                connected = crypto_live(self.crypto(), symbol) if symbol.startswith(("BINANCE.", "OKX.", "AGG.")) else self.futures().status == "connected"
                if not connected:
                    item.update(status="unavailable", detail="行情连接中断，等待恢复")
        return {**data, "runtime": runtime, "notification": dict(self.notification), "error": self.error,
                "timeframe": TF, "overbought": 80, "oversold": 20, "running": bool(self.threads) and all(t.is_alive() for t in self.threads)}
