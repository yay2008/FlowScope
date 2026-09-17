# -*- coding: utf-8 -*-
"""自选合约: 服务端持久化 + 面板用的轻量报价快照。

分工:

- ``FavoriteStore``: 只保存一串有序的合约代码, 落盘到 ``data/favorites.json``
  (临时文件 + ``os.replace`` 原子替换)。读写不碰行情, 所以收藏/取消收藏是即时的。
- ``read_quotes``: 采集线程里读一组合约的报价快照。**只 get_quote 读对象, 不订阅
  K 线/tick, 也不额外 wait_update** —— 采集循环每秒本来就在 wait_update, 报价对象
  一直是活的; 只有第一次见到的合约才等一次, 否则面板每秒轮询会把采集线程挤住。
- ``FavoritesService``: 把两者拼成 HTTP 层要的返回值, 带一个小 TTL 缓存去重。

行情源不可用时自选列表照样可读可改: ``symbols`` 是本地文件, 报价缺失就留空。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

from ingest import JOB_TIMEOUT_SEC, validate_symbol

MAX_FAVORITES = 40        # 自选上限: 面板一屏能看过来, 也限制轮询的报价数量
QUOTES_TTL_SEC = 1.5      # 报价快照缓存: 多个页面/标签同时轮询只查一次
COLD_WAIT_SEC = 1.0       # 新合约首次订阅后等报价下发的时间
STORE_VERSION = 1


class FavoriteStore:
    """``data/favorites.json`` 的有序合约代码列表。

    路径用 provider 每次现取: 测试和离线预览会替换 ``ingest.DATA_DIR``,
    缓存必须跟着换文件, 不能抓着进程启动时那一个路径。文件被外部改动
    (手工编辑, 或换了数据目录)时按 mtime 重新读入。
    """

    def __init__(self, path_provider):
        self._path_provider = path_provider
        self._lock = threading.RLock()
        self._path: str | None = None
        self._stamp: tuple[int, int] | None = None
        self._symbols: list[str] = []

    def symbols(self) -> list[str]:
        with self._lock:
            self._sync()
            return list(self._symbols)

    def add(self, symbol: str) -> list[str]:
        """追加到末尾(幂等); 已存在时顺序不变, 避免点一下收藏就把列表跳乱。"""
        value = validate_symbol(symbol)
        with self._lock:
            self._sync()
            if value not in self._symbols:
                if len(self._symbols) >= MAX_FAVORITES:
                    raise ValueError(f"自选最多 {MAX_FAVORITES} 个")
                self._symbols.append(value)
                self._save()
            return list(self._symbols)

    def remove(self, symbol: str) -> list[str]:
        value = validate_symbol(symbol)
        with self._lock:
            self._sync()
            if value in self._symbols:
                self._symbols.remove(value)
                self._save()
            return list(self._symbols)

    def _sync(self):
        """必须在持有 self._lock 时调用; 文件或数据目录变了就重读。"""
        path = self._path_provider()
        stamp = _file_stamp(path)
        if path == self._path and stamp == self._stamp:
            return
        self._path = path
        self._symbols = self._load(path)
        self._stamp = stamp

    def _load(self, path: str) -> list[str]:
        """文件不存在、损坏或格式不对都当空列表: 自选丢了是小事, 起不来是大事。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return []
        raw = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        symbols = []
        for item in raw:
            try:
                value = validate_symbol(str(item))
            except ValueError:
                continue
            if value not in symbols:
                symbols.append(value)
        return symbols[:MAX_FAVORITES]

    def _save(self):
        assert self._path is not None
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {"version": STORE_VERSION, "symbols": self._symbols}
        temporary = f"{self._path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self._path)   # 原子替换: 不会读到写了一半的自选
        self._stamp = _file_stamp(self._path)


def _file_stamp(path: str):
    """(mtime_ns, size); 文件不存在时 None —— 用来发现自选文件被外部改动。"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _number(value):
    """NaN/±inf/非数字一律回 None —— NaN 不是合法 JSON, 前端 JSON.parse 会直接报错。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def quote_row(symbol: str, quote) -> dict:
    """把 Quote 对象压成面板需要的一行; 缺数据的字段留 None, 前端显示占位符。"""
    last = _number(getattr(quote, "last_price", None))
    # 期货涨跌幅惯例是相对昨结算; 拿不到就退回昨收, 两者都拿不到就没有涨跌。
    base = _number(getattr(quote, "pre_settlement", None))
    if base in (None, 0):
        base = _number(getattr(quote, "pre_close", None))
    change = None if last is None or base in (None, 0) else last - base
    change_pct = None if change is None else change / base * 100
    return {
        "symbol": symbol,
        "name": str(getattr(quote, "instrument_name", "") or symbol),
        "insClass": str(getattr(quote, "ins_class", "") or ""),
        "mainSymbol": str(getattr(quote, "underlying_symbol", "") or ""),
        "lastPrice": last,
        "basePrice": base,
        "change": change,
        "changePct": change_pct,
        "openInterest": _number(getattr(quote, "open_interest", None)),
        "priceDecs": int(getattr(quote, "price_decs", 0) or 0),
        "expired": bool(getattr(quote, "expired", False)),
    }


def read_quotes(api, symbols, warm: set[str], wait_sec: float = COLD_WAIT_SEC) -> list[dict]:
    """采集线程里读报价快照(按传入顺序输出, 取不到的留空行)。

    ``warm`` 由调用方持有, 记录已经订阅过的合约: 只有新合约才等一次 wait_update。
    """
    quotes = {}
    for symbol in symbols:
        try:
            quotes[symbol] = api.get_quote(symbol)
        except Exception:
            quotes[symbol] = None
    cold = [symbol for symbol, quote in quotes.items() if quote is not None and symbol not in warm]
    if cold:
        try:
            api.wait_update(deadline=time.time() + wait_sec)
        except Exception:
            pass
        warm.update(cold)
    return [quote_row(symbol, quotes[symbol]) if quotes[symbol] is not None else _empty_row(symbol)
            for symbol in symbols]


def _empty_row(symbol: str) -> dict:
    return {"symbol": symbol, "name": symbol, "insClass": "", "mainSymbol": "", "lastPrice": None,
            "preSettlement": None, "change": None, "changePct": None, "openInterest": None,
            "priceDecs": 0, "expired": False}


class FavoritesService:
    """自选列表 + 报价快照; HTTP 层只跟这个类打交道。"""

    def __init__(self, manager_provider, store: FavoriteStore, *, quotes_ttl: float = QUOTES_TTL_SEC,
                 timeout: float = JOB_TIMEOUT_SEC, wait_sec: float = COLD_WAIT_SEC):
        self._manager = manager_provider
        self._store = store
        self._quotes_ttl = quotes_ttl
        self._timeout = timeout
        self._wait_sec = wait_sec
        self._lock = threading.Lock()
        self._warm: set[str] = set()
        self._cache: tuple[float, tuple[str, ...], dict] | None = None

    def symbols(self) -> list[str]:
        return self._store.symbols()

    def add(self, symbol: str) -> list[str]:
        return self._store.add(symbol)

    def remove(self, symbol: str) -> list[str]:
        return self._store.remove(symbol)

    async def watch(self, symbols: list[str], refresh: bool = False) -> dict:
        """返回 ``{"quotes": [...], "source": "live"|"unavailable", "error": ...}``。

        报价拿不到不算失败: 面板仍然要能列出代码、点着切换。
        """
        wanted = tuple(symbols)
        if not wanted:
            return {"quotes": [], "source": "live", "error": None}
        with self._lock:
            cached = self._cache
        if cached is not None and not refresh and cached[1] == wanted \
                and time.monotonic() - cached[0] < self._quotes_ttl:
            return cached[2]
        try:
            rows = await self._manager().query(
                lambda api: read_quotes(api, wanted, self._warm, self._wait_sec), self._timeout)
            payload = {"quotes": rows, "source": "live", "error": None}
        except Exception as exc:
            payload = {"quotes": [], "source": "unavailable",
                       "error": f"{type(exc).__name__}: {exc}"}
        if payload["source"] == "live":
            with self._lock:
                self._cache = (time.monotonic(), wanted, payload)
        return payload
