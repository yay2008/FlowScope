# -*- coding: utf-8 -*-
"""加密货币永续合约行情(币安、OKX): 逐笔成交 -> 10s/30s bar, 广播、落盘、回填。

和 TqSdk 采集(ingest.py)完全分开: 每个交易所的每路 WebSocket 一个线程(Channel), 只管连接、
订阅增减与解析; 解析出的事件交给一个管理线程(CryptoManager), 由它独占 bar 窗口与历史文件、
重算快照、安排回填。app.py 按合约代码前缀分流, 对外接口与 FeedManager / Feed 一致。

合约代码: ``BINANCE.BTCUSDT.P``(沿用 TradingView 的写法)、``OKX.BTC-USDT-SWAP``(OKX 原生代码)、
``AGG.BTC``(多所汇总, 见 crypto_aggregate.py)。数量一律折成币(OKX 按张计, 乘合约面值),
不同交易所的量可以直接相加。

口径:

- 主动方向是交易所给的(币安 aggTrade 的 ``m``、OKX 的 ``side``), 不用估算; unknown 恒为 0,
  新旧两套判向列同值。
- 成交编号逐笔连续(两家都实测过), 漏没漏成交能精确判断。本次运行第一笔之前、以及编号接不上
  的地方记一个"洞": 洞两端所在的 bar 标为 partial, 中间整根整根的 bar 不补。
- 洞由回填线程(crypto_backfill.py)用归档包和 REST 补: 中间的 bar 直接写进历史文件, 两端 bar
  缺的那几笔插回内存窗口, 之后两端也是 complete。
- 交易所都没有 10s/30s K 线, 开高低收量由逐笔成交合成, 按成交时间切 [起点, 起点+周期)。编号
  连续而中间几根 bar 没有成交, 说明那段时间确实没成交: 补零量 bar, 价格沿用前收。

落盘: 买卖量沿用 HistoryStore(文件名规则同期货, 见 ingest.Feed._store); 开高低收量另存
``{key}_{tf}s_ohlc.csv``(期货的 K 线由服务端下发, 这边只能自己存)。
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import ingest
from indicator import BAR_COLUMNS, DEFAULT_TF_SEC, TF_OPTIONS, TZ_SHIFT_S, finalize_bars, ltf_options
from ingest import COMPUTE_RETRY_SEC, MAX_KLINES, feed_key, feed_label, validate_symbol

VENUE_NAMES = {"BINANCE": "币安", "OKX": "OKX", "AGG": "多所汇总"}
# 默认常驻采集: 币安与 OKX 的 BTC 永续(多所汇总 AGG.BTC 由这两路合成)。自选里的加密合约追加在后。
DEFAULT_SYMBOLS = ("BINANCE.BTCUSDT.P", "OKX.BTC-USDT-SWAP")
MAX_PINNED = 12            # 常驻采集最多几个合约(每个两路主周期)
# 成交量累加会带出浮点尾数(0.1 + 0.2), 出表前统一取到 8 位小数, 否则同一根 bar 每次重算的值
# 差一点点, 会被 HistoryStore 当成变化反复追加。
VOLUME_ROUND = 8
RECOMPUTE_SEC = 0.5        # 有页面在看的合约: 有新成交时最多每这么久重算一次快照
IDLE_RECOMPUTE_SEC = 5.0   # 没人看的合约: 有 bar 走完才算(要落盘), 否则最多每这么久算一次
RECV_TIMEOUT_SEC = 0.25    # 收消息的等待上限, 也决定停止与订阅变更的响应速度
STALE_SEC = 60.0           # 连接着却这么久一条消息都没有, 当作连接已死, 主动重连
OPEN_TIMEOUT_SEC = 10.0
MAX_BACKOFF_SEC = 60
TRIM_SLACK = 100           # 内存里的 bar 超出窗口这么多根才裁一次, 不必每笔成交都裁
IDLE_EVICT_SEC = 600       # 页面临时打开的合约, 最后一次需求之后保留多久
# 洞两端要插回内存的成交范围: 最长主周期的一根 bar(两个主周期的端点 bar 都在里面)
EDGE_MS = max(TF_OPTIONS) * 1000
HOLE_RETRY_SEC = 900       # 回填没补完的洞(REST 不通、归档包还没发布)多久后重试
WATCH_TTL_SEC = 30         # 自选面板要过的报价, 之后还订阅多久(面板 3 秒轮询一次)
TEMPORARY_BACKFILL_DAYS = 1  # 页面临时打开(不在常驻集合里)的合约只回填这么多天, 免得随手点一下就下载一周
BAR_FIELDS = ["open", "high", "low", "close", "volume"]


def venue_of(symbol) -> str | None:
    """合约代码的前缀 -> 交易所(BINANCE / OKX / AGG); 不是加密合约返回 None。"""
    if not isinstance(symbol, str):
        return None
    head = symbol.strip().split(".", 1)[0]
    return head if head in VENUE_NAMES and "." in symbol else None


def is_crypto(symbol) -> bool:
    return venue_of(symbol) is not None


def step_digits(step: float) -> int:
    """步长 -> 显示位数: 0.1 -> 1, 0.001 -> 3, 1 -> 0。"""
    text = f"{step:.10f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


@dataclass(frozen=True)
class Instrument:
    """一个永续合约。数量相关的字段都已折成币(OKX 的张 x 面值)。"""

    symbol: str                 # FlowScope 代码: BINANCE.BTCUSDT.P / OKX.BTC-USDT-SWAP
    venue: str                  # BINANCE / OKX
    inst_id: str                # 交易所原生代码: BTCUSDT / BTC-USDT-SWAP
    base: str                   # BTC
    label: str                  # 顶栏展示名
    tick_size: float            # 价格步长
    step_size: float            # 数量步长(币)
    contract_value: float = 1.0  # 一张合约是多少币; 币安的数量本来就是币, 为 1
    min_qty: float = 0.0        # 最小下单量(币)
    min_notional: float = 0.0   # 最小下单金额(USDT)
    max_leverage: int = 100
    rank: float = 0.0           # 24 小时成交额(USDT), 选择器排序用

    @property
    def price_digits(self) -> int:
        return step_digits(self.tick_size)

    @property
    def qty_digits(self) -> int:
        return step_digits(self.step_size)


class TradeBars:
    """逐笔成交 -> 主周期 bar 与 1 秒小 bar 的内存窗口。纯内存, 不碰网络和文件, 测试直接喂成交。

    bar 按成交时间(交易所时间, 毫秒)切分, 覆盖 [起点, 起点 + 周期)。三类数据按 bar 起点合在一起:

    - ``bars``: 本次运行收到(或回填插回)的成交合成的 bar;
    - ``stored``: 落盘的开高低收量(启动时读回, 回填写入后也放进来), 只留窗口内的;
    - ``holes``: 编号接不上的地方; 两端 bar 是 partial, 有落盘记录时用落盘的(回填过就是完整的)。
    """

    def __init__(self, tf: int, max_bars: int = MAX_KLINES):
        self.tf_ms = tf * 1000
        self.max_bars = max_bars
        # 起点(毫秒) -> [开, 高, 低, 收, 量, 主动买, 主动卖, 首笔编号, 末笔编号]; 补的零量 bar 编号为 None
        self.bars: dict[int, list] = {}
        # 1 秒小 bar 起点(毫秒) -> [开, 收, 量, 首笔编号, 末笔编号]; K 线口径的拆分粒度由它现算
        self.seconds: dict[int, list] = {}
        self.stored: dict[int, list] = {}    # 起点(毫秒) -> [开, 高, 低, 收, 量]
        # [左边最后一笔的编号, 其时间, 右边第一笔的编号, 其时间]; 左边为 None 表示本次运行的开头
        self.holes: list[list] = []
        self.last_id: int | None = None
        self.last_ms: int | None = None
        self.live_start: int | None = None  # 最新一笔实时成交所在 bar 的起点
        self.gaps = 0                        # 本次运行编号接不上的次数(断线重连)

    def bar_of(self, t_ms: int) -> int:
        return t_ms // self.tf_ms * self.tf_ms

    def load(self, frame: pd.DataFrame):
        """读回落盘的开高低收量(time 为展示秒, 同 HistoryStore)。"""
        starts = (frame["time"].to_numpy(dtype=np.int64) - TZ_SHIFT_S) * 1000
        self.stored = dict(zip(starts.tolist(), frame[BAR_FIELDS].to_numpy(dtype=float).tolist()))
        self._trim()

    def put_stored(self, starts, values):
        """回填写进文件的 bar 也放进窗口(只留窗口内的)。"""
        self.stored.update(zip(starts, values))
        self._trim()

    def add(self, trade_id: int, price: float, qty: float, t_ms: int, sell: bool) -> bool:
        """并入一笔实时成交; 重复或更早的编号(重连后服务端重发)忽略。返回是否采用。"""
        if self.last_id is not None and trade_id <= self.last_id:
            return False
        if self.last_ms is not None and t_ms < self.last_ms:
            t_ms = self.last_ms   # 交易所时间不会倒退; 万一倒退也不能打乱 bar 的时间顺序
        start = self.bar_of(t_ms)
        if self.last_id is None or trade_id != self.last_id + 1:
            # 本次运行的第一笔, 或编号接不上(断线): 中间的成交没收到, 记一个洞等回填。
            self.holes.append([self.last_id, self.last_ms, trade_id, t_ms])
            if self.last_id is not None:
                self.gaps += 1
        elif self.live_start is not None and start > self.live_start + self.tf_ms:
            self._fill_quiet(self.live_start, start)
        self._apply(trade_id, price, qty, t_ms, sell)
        self.last_id, self.last_ms, self.live_start = trade_id, t_ms, start
        if len(self.bars) > self.max_bars + TRIM_SLACK:
            self._trim()
        return True

    def insert(self, trades, before_id: int) -> bool:
        """把回填拿到的、洞两端 bar 里缺的成交插回窗口, 这个洞随之消失。

        trades: (编号, 价, 量, 毫秒, 是否主动卖) 的序列, 洞以外的编号忽略。洞已经裁出窗口时返回 False。
        """
        hole = next((item for item in self.holes if item[2] == before_id), None)
        if hole is None:
            return False
        after_id, after_ms, _, before_ms = hole
        for trade_id, price, qty, t_ms, sell in trades:
            if after_id is not None and trade_id <= after_id or trade_id >= before_id:
                continue
            self._apply(int(trade_id), float(price), float(qty), int(t_ms), bool(sell))
        self.holes.remove(hole)
        begin = self.bar_of(after_ms if after_ms is not None else before_ms)
        self._fill_quiet(begin, self.bar_of(before_ms))
        return True

    def _apply(self, trade_id, price, qty, t_ms, sell):
        """按编号先后更新 bar 与 1 秒小 bar: 编号更小的定开盘, 更大的定收盘, 与到达顺序无关。"""
        start = self.bar_of(t_ms)
        bar = self.bars.get(start)
        if bar is None or bar[7] is None:          # 新 bar, 或之前补的零量 bar
            bar = self.bars[start] = [price, price, price, price, 0.0, 0.0, 0.0, trade_id, trade_id]
        else:
            if trade_id < bar[7]:
                bar[0], bar[7] = price, trade_id
            if trade_id > bar[8]:
                bar[3], bar[8] = price, trade_id
            bar[1] = max(bar[1], price)
            bar[2] = min(bar[2], price)
        bar[4] += qty
        bar[6 if sell else 5] += qty
        second = t_ms // 1000 * 1000
        tick = self.seconds.get(second)
        if tick is None:
            self.seconds[second] = [price, price, qty, trade_id, trade_id]
        else:
            if trade_id < tick[3]:
                tick[0], tick[3] = price, trade_id
            if trade_id > tick[4]:
                tick[1], tick[4] = price, trade_id
            tick[2] += qty

    def _close_at(self, start):
        bar = self.bars.get(start)
        if bar is not None:
            return bar[3]
        stored = self.stored.get(start)
        return stored[3] if stored is not None else None

    def _fill_quiet(self, begin: int, end: int):
        """begin 与 end 两根 bar 之间(不含两端)既没有成交也没有落盘记录的 bar, 补成零量 bar。

        只在两端之间的成交编号确实连续时调用: 那段时间确实没成交, 价格沿用前收。
        """
        close = self._close_at(begin)
        for start in range(begin + self.tf_ms, end, self.tf_ms)[-self.max_bars:]:
            known = self._close_at(start)
            if known is not None:
                close = known
            elif close is not None:
                self.bars[start] = [close, close, close, close, 0.0, 0.0, 0.0, None, None]

    def _trim(self):
        """窗口只留最近 max_bars 根; 1 秒小 bar、落盘记录与洞跟着裁。"""
        keys = sorted(self.bars)
        for key in keys[:-self.max_bars]:
            del self.bars[key]
        newest = max(keys[-1] if keys else -1, max(self.stored, default=-1))
        floor = newest - (self.max_bars - 1) * self.tf_ms
        self.stored = {key: value for key, value in self.stored.items() if key >= floor}
        first_live = min(self.bars, default=None)
        if first_live is not None:
            self.seconds = {key: value for key, value in self.seconds.items() if key >= first_live}
            self.holes = [hole for hole in self.holes if self.bar_of(hole[3]) >= first_live]

    def hole_spans(self) -> list[tuple[int, int]]:
        """各个洞两端 bar 的起点(含): 落在其间的实时 bar 都可能缺量。"""
        return [(self.bar_of(after_ms if after_ms is not None else before_ms), self.bar_of(before_ms))
                for _, after_ms, _, before_ms in self.holes]

    def split(self, ltf: int) -> pd.DataFrame:
        """K 线口径(同 indicator.build_bars_from_ltf): ltf 秒小 bar 收 > 开整根记买, 收 < 开记卖,
        十字丢弃, 再汇总到主周期。小 bar 的开/收是其中第一笔/最后一笔成交价, 与交易所 K 线一致。

        返回按主周期起点(毫秒)索引的 buy/sell; ltf 必须整除主周期(由 Feed.request 保证)。
        """
        if not self.seconds:
            return pd.DataFrame({"buy": [], "sell": []}, dtype=float)
        seconds = pd.DataFrame([value[:3] for value in self.seconds.values()], index=list(self.seconds),
                               columns=["open", "close", "volume"]).sort_index()
        ltf_ms = ltf * 1000
        groups = seconds.groupby(seconds.index // ltf_ms * ltf_ms)
        first, last, volume = groups["open"].first(), groups["close"].last(), groups["volume"].sum()
        buy = volume.where(last > first, 0.0)
        sell = volume.where(last < first, 0.0)
        bar = buy.index // self.tf_ms * self.tf_ms
        return pd.DataFrame({"buy": buy.groupby(bar).sum(), "sell": sell.groupby(bar).sum()})

    def frame(self, ltf: int = 0) -> pd.DataFrame:
        """当前窗口 -> bars 表(BAR_COLUMNS, time 为展示秒)。

        每根 bar 取最好的来源: 完整的实时 bar > 落盘记录(买卖量留空、覆盖记 missing, 由
        HistoryStore.merge 用已保存的记录补上) > 落在洞两端的实时 bar(partial)。
        ltf=0 的买卖量就是交易所给的主动方向; ltf>0 为 K 线口径(见 split)。
        """
        keys = sorted(self.bars.keys() | self.stored.keys())[-self.max_bars:]
        if not keys:
            return pd.DataFrame(columns=BAR_COLUMNS)
        spans = self.hole_spans()
        split = self.split(ltf) if ltf else None
        split_buy = split["buy"].to_dict() if split is not None else None
        split_sell = split["sell"].to_dict() if split is not None else None
        rows = []
        for start in keys:
            bar = self.bars.get(start)
            partial = bar is not None and any(begin <= start <= end for begin, end in spans)
            if bar is None or (partial and start in self.stored):
                rows.append((start, *self.stored[start], np.nan, np.nan, "missing", False))
                continue
            buy, sell = bar[5], bar[6]
            if split is not None:
                # 补出来的零量 bar 没有 1 秒小 bar, 买卖量就是 0
                buy, sell = split_buy.get(start, 0.0), split_sell.get(start, 0.0)
            rows.append((start, *bar[:5], buy, sell, "partial" if partial else "complete", True))
        df = pd.DataFrame(rows, columns=["start", *BAR_FIELDS, "buy", "sell", "coverage", "hasBaseline"])
        df[["volume", "buy", "sell"]] = df[["volume", "buy", "sell"]].astype(float).round(VOLUME_ROUND)
        df["unknown"] = np.where(df["hasBaseline"], 0.0, np.nan)
        # 交易所给的方向没有新旧算法之分, 对照列与主列同值, 便于前端统一取列
        df["buyLegacy"] = df["buy"]
        df["sellLegacy"] = df["sell"]
        df["bar_ns"] = df["start"] * 10**6
        df = finalize_bars(df)
        df["time"] = (df["bar_ns"] // 10**9 + TZ_SHIFT_S).astype("int64")
        return df[BAR_COLUMNS]


def aggregate_trades(trades: pd.DataFrame, tf: int, ltfs, start_ms: int, end_ms: int) -> pd.DataFrame:
    """完整覆盖 [start_ms, end_ms) 的逐笔成交 -> 其中整根 bar 的开高低收量、主动买卖量与各拆分粒度的买卖量。

    trades: 列 id/price/qty/t/sell, 按编号升序, 必须包含这段时间里的全部成交(回填用, 见
    crypto_backfill)。没有成交的 bar 补零量 bar(价格沿用前收); 段首就没有成交的 bar 不知道价格, 丢弃。
    返回按 bar 起点(毫秒)索引, 列 open/high/low/close/volume/buy/sell 与各 ltf 的 buy{ltf}/sell{ltf}。
    """
    tf_ms = tf * 1000
    first = -(-start_ms // tf_ms) * tf_ms
    last = end_ms // tf_ms * tf_ms
    columns = [*BAR_FIELDS, "buy", "sell", *[f"{side}{ltf}" for ltf in ltfs if ltf for side in ("buy", "sell")]]
    if last <= first:
        return pd.DataFrame(columns=columns, dtype=float)
    data = trades[(trades["t"] >= first) & (trades["t"] < last)]
    bar = data["t"] // tf_ms * tf_ms
    groups = data.groupby(bar)
    qty = data["qty"]
    out = pd.DataFrame({"open": groups["price"].first(), "high": groups["price"].max(),
                        "low": groups["price"].min(), "close": groups["price"].last(),
                        "volume": groups["qty"].sum(),
                        "buy": qty.where(~data["sell"], 0.0).groupby(bar).sum(),
                        "sell": qty.where(data["sell"], 0.0).groupby(bar).sum()})
    for ltf in ltfs:
        if not ltf:
            continue
        ltf_ms = ltf * 1000
        micro = data["t"] // ltf_ms * ltf_ms
        micro_groups = data.groupby(micro)
        opened, closed = micro_groups["price"].first(), micro_groups["price"].last()
        volume = micro_groups["qty"].sum()
        owner = volume.index // tf_ms * tf_ms
        out[f"buy{ltf}"] = volume.where(closed > opened, 0.0).groupby(owner).sum()
        out[f"sell{ltf}"] = volume.where(closed < opened, 0.0).groupby(owner).sum()
    out = out.reindex(pd.RangeIndex(first, last, tf_ms))
    quiet = out["volume"].isna()
    if quiet.any():
        close = out["close"].ffill()
        for column in ["open", "high", "low", "close"]:
            out.loc[quiet, column] = close[quiet]
        out.loc[quiet, out.columns.difference(["open", "high", "low", "close"])] = 0.0
        out = out[out["close"].notna()]
    volumes = out.columns.difference(["open", "high", "low", "close"])
    out[volumes] = out[volumes].astype(float).round(VOLUME_ROUND)
    out.index.name = "start"
    return out[columns]


def catalog_product(instrument: Instrument) -> dict:
    """选择器的一行(字段同 catalog.build_products)。"""
    return {"exchangeId": instrument.venue, "productId": instrument.inst_id,
            "name": instrument.label.split(" 永续", 1)[0], "contSymbol": instrument.symbol,
            "mainSymbol": "", "openInterest": instrument.rank, "crypto": True}


def ticker_row(symbol: str, instrument: Instrument | None, ticker: dict) -> dict:
    """24 小时行情 -> 自选面板的一行(字段同 favorites.quote_row); 还没收到行情的字段留 None。"""
    last, opened = ticker.get("last"), ticker.get("open")
    change = None if last is None or not opened else last - opened
    label = instrument.label if instrument is not None else symbol
    name, _, venue = label.partition(" · ")
    return {"symbol": symbol, "name": name, "venue": venue, "insClass": "", "mainSymbol": "",
            "lastPrice": last, "basePrice": opened, "change": change, "changePct": ticker.get("changePct"),
            "openInterest": None, "volume": ticker.get("volume"), "amount": ticker.get("amount"),
            "priceDecs": instrument.price_digits if instrument is not None else 2, "expired": False}


def _cell(value) -> str:
    return repr(float(value))


class CandleStore:
    """主周期开高低收量的落盘文件: 只追加, 后写的行优先(读回时按时间去重)。

    time 为展示秒(同 HistoryStore)。只有管理线程读写。
    """

    COLUMNS = ["time", *BAR_FIELDS]

    def __init__(self, path, window: int = MAX_KLINES):
        self.path = Path(path)
        self.window = window
        self._written: dict[int, tuple] = {}   # 最近写过(或读回)的行, 值没变就不再写
        self._needs_header = True
        self._needs_newline = False

    def load(self) -> pd.DataFrame:
        """读回最近 window 根; 文件不存在或读不了按没有历史处理, 坏行跳过。"""
        frame = pd.DataFrame(columns=self.COLUMNS)
        size = self.path.stat().st_size if self.path.exists() else 0
        if size:
            try:
                frame = pd.read_csv(self.path, on_bad_lines="skip")
            except (pd.errors.ParserError, pd.errors.EmptyDataError, OSError, UnicodeDecodeError) as exc:
                print(f"[crypto] {self.path.name} 读取失败({exc}), 按没有历史处理", flush=True)
            with open(self.path, "rb") as handle:
                handle.seek(size - 1)
                self._needs_newline = handle.read(1) != b"\n"
        self._needs_header = size == 0
        frame = frame.reindex(columns=self.COLUMNS).apply(pd.to_numeric, errors="coerce").dropna()
        frame = frame.drop_duplicates("time", keep="last").sort_values("time").tail(self.window)
        frame["time"] = frame["time"].astype("int64")
        self._written = {int(row[0]): tuple(row[1:]) for row in frame.itertuples(index=False)}
        return frame.reset_index(drop=True)

    def save(self, bars: pd.DataFrame, final: bool = False):
        """追加与上次写入不同的 bar。

        final=False(实时): 只写本次运行合成的(hasBaseline)、已走完(不是最后一根)的 bar;
        final=True(回填): 整张表都是完整的 bar, 全部写。
        """
        done = bars if final else bars.iloc[:-1]
        if not final:
            done = done[done["hasBaseline"].astype(bool)]
        lines = []
        for row in done[self.COLUMNS].itertuples(index=False):
            key, values = int(row[0]), tuple(float(value) for value in row[1:])
            if self._written.get(key) == values:
                continue
            self._written[key] = values
            lines.append(",".join([str(key), *map(_cell, values)]) + "\n")
        if not lines:
            return
        payload = "".join(lines)
        if self._needs_header:
            payload = ",".join(self.COLUMNS) + "\n" + payload
        elif self._needs_newline:
            payload = "\n" + payload
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            handle.write(payload)
        self._needs_header = self._needs_newline = False
        if len(self._written) > 3 * self.window:
            self._written = dict(sorted(self._written.items())[-2 * self.window:])


class CryptoFeed(ingest.Feed):
    """加密合约的一个主周期。需求登记、快照读取、历史文件命名沿用 ingest.Feed, 数据换成逐笔成交;
    只有管理线程写数据与历史文件。"""

    def __init__(self, instrument: Instrument, tf: int = DEFAULT_TF_SEC):
        super().__init__(instrument.symbol, tf)
        self.instrument = instrument
        self.trades = TradeBars(self.tf)
        self.candles: CandleStore | None = None
        self.loaded = False
        self.last_compute = 0.0
        self.computed_start = None   # 上次重算时最新 bar 的起点: 变了说明有 bar 走完, 要落盘

    def cfg(self) -> dict:
        return {**super().cfg(), "priceDigits": self.instrument.price_digits,
                "volumeDigits": self.instrument.qty_digits}

    def load_history(self):
        """读回落盘的开高低收量(DATA_DIR 在调用时现取, 便于测试替换)。只在管理线程调用一次。"""
        path = os.path.join(ingest.DATA_DIR, f"{ingest._csv_key(self.symbol)}_{self.tf}s_ohlc.csv")
        self.candles = CandleStore(path, self.trades.max_bars)
        self.trades.load(self.candles.load())
        self.loaded = True

    def complete_starts(self) -> set[int]:
        """已经核对完整落盘的 bar 起点(毫秒); 回填据此跳过不用补的部分。"""
        store = self._store(0)
        store.refresh()
        return {(int(t) - TZ_SHIFT_S) * 1000 for t in store.values}

    def footprint_snapshot(self, demand=True):
        """足迹图暂不做: 给一份空足迹, 页面切到足迹图时不会一直等快照。"""
        self.request(demand=demand)
        with self._state_lock:
            return {"symbol": self.symbol, "tf": self.tf, "revision": self.revision,
                    "tickSize": None, "bars": []}

    def apply_backfill(self, bars: pd.DataFrame):
        """回填结果(aggregate_trades 的输出, 只含完整的 bar)写进各粒度历史与开高低收量文件。"""
        if bars.empty:
            return
        times = (bars.index.to_numpy(dtype=np.int64) // 1000 + TZ_SHIFT_S)
        for ltf in ltf_options(self.tf):
            buy = bars["buy" if ltf == 0 else f"buy{ltf}"].to_numpy(dtype=float)
            sell = bars["sell" if ltf == 0 else f"sell{ltf}"].to_numpy(dtype=float)
            rows = pd.DataFrame({"time": times, "buy": buy, "sell": sell, "unknown": 0.0,
                                 "buyLegacy": buy, "sellLegacy": sell,
                                 "coverage": "complete", "hasBaseline": True})
            store = self._store(ltf)
            store.refresh()
            store.save_completed(rows, final=True)
        candles = bars[BAR_FIELDS].reset_index(drop=True)
        candles.insert(0, "time", times)
        if self.candles is not None:
            self.candles.save(candles, final=True)
        self.trades.put_stored(bars.index.tolist(), bars[BAR_FIELDS].to_numpy(dtype=float).tolist())

    def recompute(self, broadcast):
        with self._state_lock:
            now = time.monotonic()
            ltfs = [ltf for ltf, expires in self._requested.items() if expires >= now]
            demand_version = self._demand_version
            previous = dict(self.snapshots)
        snapshots = {}
        messages = []
        revision = self.revision + 1
        for ltf in ltfs:
            bars = self.trades.frame(ltf)
            if bars.empty:
                continue
            if ltf == 0 and self.candles is not None:
                self.candles.save(bars)
            snapshots[ltf], message = self._publish(ltf, bars, previous, revision)
            if message:
                messages.append(message)
        self.last_compute = time.monotonic()
        self.computed_start = self.trades.live_start
        with self._state_lock:
            self.snapshots = snapshots
            self.revision = revision
            self._computed_version = demand_version
            self.error = None
            if snapshots:
                self.ready.set()
        for message in messages:
            broadcast(message)


def _connect(url: str):
    """真实连接(websockets 的同步客户端, 自带心跳); 测试与离线预览换成假连接。"""
    from websockets.sync.client import connect
    return connect(url, open_timeout=OPEN_TIMEOUT_SEC, close_timeout=2, max_size=2**22)


class Channel:
    """一路行情 WebSocket: 自己一个线程, 只管连接、订阅增减与解析, 事件交给管理线程。

    spec 由交易所适配器提供(见 binance_feed / okx_feed): url(topics)、subscribe_messages(topics)、
    change_messages(current, wanted)、parse(raw) -> 事件列表、ping(空闲时发的保活消息, 可为 None)。
    要订阅的 topic 由 wanted() 给出(管理线程算好的集合), 为空时不连接。
    """

    def __init__(self, key: str, spec, wanted, emit, connect=_connect):
        self.key = key
        self.spec = spec
        self.wanted = wanted
        self.emit = emit
        self._connect = connect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "idle"
        self.last_error: str | None = None
        self.connects = 0
        self.last_message_at: float | None = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name=f"crypto-{self.key}")
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self.status = "stopped"

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {"status": self.status, "lastError": self.last_error, "connects": self.connects,
                "topics": len(self.wanted()),
                "quietSec": None if self.last_message_at is None else round(now - self.last_message_at, 1)}

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            topics = self.wanted()
            if not topics:
                self.status = "idle"
                self._stop.wait(RECOMPUTE_SEC)
                continue
            try:
                self.status = "connecting"
                with self._connect(self.spec.url(topics)) as socket:
                    for message in self.spec.subscribe_messages(topics):
                        socket.send(message)
                    self.connects += 1
                    self.status = "connected"
                    self.last_error = None
                    backoff = 1
                    self._pump(socket, topics)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.status = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"[crypto] {self.key} 行情连接断开: {self.last_error}", flush=True)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)

    def _pump(self, socket, current):
        """收消息直到连接断开、停止或不再需要; 订阅集合变了就在这条连接上增减。"""
        last_message = last_ping = time.monotonic()
        while not self._stop.is_set():
            wanted = self.wanted()
            if not wanted:
                return
            if wanted != current:
                for message in self.spec.change_messages(current, wanted):
                    socket.send(message)
                current = wanted
            try:
                raw = socket.recv(timeout=RECV_TIMEOUT_SEC)
            except TimeoutError:
                raw = None
            now = time.monotonic()
            if raw is not None:
                last_message = self.last_message_at = now
                for event in self.spec.parse(raw):
                    self.emit(event)
            elif now - last_message >= STALE_SEC:
                raise ConnectionError(f"{STALE_SEC:.0f} 秒没有收到行情")
            ping = self.spec.ping
            if ping is not None and now - max(last_message, last_ping) >= self.spec.ping_sec:
                socket.send(ping)
                last_ping = now


class CryptoManager:
    """加密行情总管: 各交易所的 WebSocket 线程收数据, 本线程独占 bar 窗口与历史文件。

    采集哪些合约: 常驻集合(默认 + 自选, set_pinned)一直采; 页面临时打开的(ensure)最后一次需求
    之后保留 IDLE_EVICT_SEC。报价/盘口/标记价格按需订阅(touch): 自选面板、模拟交易各自声明要多久。
    """

    def __init__(self, adapters, *, connect=_connect, backfiller=None, defaults=DEFAULT_SYMBOLS):
        self.adapters = {adapter.venue: adapter for adapter in adapters}
        self.instruments: dict[str, Instrument] = {}
        for adapter in adapters:
            for instrument in adapter.builtin_instruments():
                self.instruments[instrument.symbol] = instrument
        self.defaults = [symbol for symbol in defaults if venue_of(symbol) in self.adapters]
        self.feeds: dict[tuple[str, int], CryptoFeed] = {}
        self.quotes: dict[str, dict] = {}          # 合约 -> {"ticker": {...}, "book": {...}, "mark": {...}}
        self.events: queue.Queue = queue.Queue()
        self.clients: dict[asyncio.Queue, tuple[str, int, int]] = {}   # 值: (合约, 主周期, 拆分粒度)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pinned: list[str] = list(self.defaults)
        self.pin_skipped: list[str] = []
        self._pin_request: list[str] = []          # 自选里的加密合约(按面板顺序), 见 set_pinned
        self._demand: dict[str, float] = {}         # 页面临时打开的合约 -> 最近一次需求
        self._interest: dict[tuple[str, str], float] = {}   # (合约, 报价种类) -> 截止时刻
        self._topics: dict[str, frozenset] = {}
        self._dirty: set[str] = set()
        self._holes: dict[tuple[str, int], dict] = {}       # (合约, 洞右端编号) -> 回填状态
        self.aggregates = None      # 多所汇总(crypto_aggregate.AggregateBook), 由 app 装上
        self.loop_hooks: list = []  # 每轮重算之后在本线程调用 fn(manager)(模拟交易撮合等)
        self.status = "stopped"
        self.channels = []
        for adapter in adapters:
            for spec in adapter.channels:
                key = f"{adapter.venue}.{spec.name}"
                self.channels.append(Channel(key, spec, self._wanted(key), self.events.put, connect))
        self.backfiller = backfiller
        if backfiller is not None:
            backfiller.bind(self.adapters)

    # ------------------------------------------------------------------ HTTP 线程调用
    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="crypto-manager")
            self._thread.start()

    def stop(self):
        self._stop.set()
        for channel in self.channels:
            channel.stop()
        if self.backfiller is not None:
            self.backfiller.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self.status = "stopped"

    def instrument(self, symbol: str) -> Instrument | None:
        with self._lock:
            return self.instruments.get(symbol)

    def instrument_list(self) -> list[Instrument]:
        with self._lock:
            return list(self.instruments.values())

    def require(self, symbol: str) -> Instrument:
        """合约代码 -> Instrument; 不认识就抛 ValueError(消息直接给页面)。"""
        instrument = self.instrument(validate_symbol(symbol))
        if instrument is None:
            raise ValueError(f"不支持的加密合约 {symbol}(合约列表里没有)")
        return instrument

    def ensure(self, symbol: str, tf: int = DEFAULT_TF_SEC):
        if venue_of(symbol) == "AGG" and self.aggregates is not None:
            return self.aggregates.ensure(symbol, tf)
        instrument = self.require(symbol)
        key = feed_key(instrument.symbol, tf)
        with self._lock:
            self._demand[instrument.symbol] = time.monotonic()
            for period in TF_OPTIONS:
                other = feed_key(instrument.symbol, period)
                if other not in self.feeds:
                    self.feeds[other] = CryptoFeed(instrument, period)
            feed = self.feeds[key]
        feed.request(demand=True)
        return feed

    def feed_retry_error(self, feed) -> str | None:
        """接口与 FeedManager 一致; 加密行情没有订阅冷却, 连接问题看 status_snapshot。"""
        return None

    def add_client(self, queue_: asyncio.Queue, symbol: str, ltf: int, footprint=False,
                   tf: int = DEFAULT_TF_SEC):
        key = feed_key(symbol, tf)
        with self._lock:
            self.clients[queue_] = (key[0], key[1], ltf)
            feed = self.feeds.get(key)
        if feed is None and self.aggregates is not None:
            feed = self.aggregates.feeds.get(key)
        if feed is not None:
            feed.request(ltf=ltf, demand=True)

    def remove_client(self, queue_: asyncio.Queue):
        with self._lock:
            subscription = self.clients.pop(queue_, None)
            if subscription is not None and subscription[0] in self._demand:
                self._demand[subscription[0]] = time.monotonic()   # 闲置期从最后一个客户端离开后起算

    def set_pinned(self, symbols) -> list[str]:
        """常驻采集集合: 默认合约 + 自选里的加密合约; 不认识的代码与超出名额的不常驻。

        合约列表是启动后才从交易所取到的, 所以每轮都按最新的列表重新解析(见 _resolve_pins):
        刚启动时还不认识的自选合约, 列表一到就开始采。
        """
        with self._lock:
            self._pin_request = list(symbols)
            return self._resolve_pins()

    def pinned_aggregates(self) -> list[str]:
        """默认与自选里的多所汇总(它们的各交易所合约已经展开进常驻集合)。"""
        with self._lock:
            return [symbol for symbol in dict.fromkeys([*self.defaults, *self._pin_request])
                    if venue_of(symbol) == "AGG"]

    def _resolve_pins(self) -> list[str]:
        """必须持有 self._lock。多所汇总展开成它的各交易所合约。"""
        ordered = []
        for symbol in dict.fromkeys([*self.defaults, *self._pin_request]):
            if venue_of(symbol) == "AGG":
                if self.aggregates is not None:
                    ordered.extend(self.aggregates.components(symbol))
            elif symbol in self.instruments:
                ordered.append(symbol)
        ordered = list(dict.fromkeys(ordered))
        self.pinned = ordered[:MAX_PINNED]
        self.pin_skipped = ordered[MAX_PINNED:]
        return list(self.pinned)

    def touch(self, symbol: str, kinds, seconds: float):
        """声明接下来 seconds 秒要这个合约的哪些报价(ticker / book / mark)。"""
        until = time.monotonic() + seconds
        with self._lock:
            for kind in kinds:
                key = (symbol, kind)
                self._interest[key] = max(self._interest.get(key, 0.0), until)

    def quote(self, symbol: str) -> dict:
        """最新报价快照(ticker / book / mark 各自的最新一条), 拷贝出去给 HTTP 线程用。"""
        with self._lock:
            return {kind: dict(value) for kind, value in self.quotes.get(symbol, {}).items()}

    def known(self, symbol: str) -> bool:
        """认识的加密合约(交易所合约列表里有, 或是能合成的多所汇总)。"""
        if venue_of(symbol) == "AGG":
            return self.aggregates is not None and bool(self.aggregates.components(symbol))
        return self.instrument(symbol) is not None

    def label(self, symbol: str) -> str:
        if venue_of(symbol) == "AGG" and self.aggregates is not None:
            return self.aggregates.label(symbol)
        instrument = self.instrument(symbol)
        return instrument.label if instrument is not None else symbol

    def catalog_groups(self) -> list[dict]:
        """合约选择器的加密分组: 每个交易所一组(按 24 小时成交额降序), 再加多所汇总。

        行的字段与期货品种一致(见 catalog.build_products); openInterest 一栏放 24 小时成交额(USDT),
        crypto=true 告诉前端没有月份可取, 二级只有永续本身。
        """
        groups = []
        instruments = self.instrument_list()
        for venue in self.adapters:
            items = sorted((item for item in instruments if item.venue == venue), key=lambda item: -item.rank)
            if items:
                groups.append({"exchangeId": venue, "exchangeName": f"{VENUE_NAMES[venue]}永续",
                               "products": [catalog_product(item) for item in items]})
        if self.aggregates is not None:
            group = self.aggregates.catalog_group(self)
            if group["products"]:
                groups.append(group)
        return groups

    def watch_rows(self, symbols) -> list[dict]:
        """自选面板的报价行(与 favorites.quote_row 同样的字段); 涨跌幅是 24 小时滚动的。

        顺带声明接下来一会儿要这些合约的 24 小时行情(常驻的本来就有)。
        """
        rows = []
        for symbol in symbols:
            if venue_of(symbol) == "AGG" and self.aggregates is not None:
                rows.append(self.aggregates.watch_row(self, symbol))
                continue
            self.touch(symbol, ["ticker"], WATCH_TTL_SEC)
            instrument = self.instrument(symbol)
            rows.append(ticker_row(symbol, instrument, self.quote(symbol).get("ticker") or {}))
        return rows

    def status_snapshot(self) -> dict:
        with self._lock:
            feeds = sorted(feed_label(key) for key in self.feeds)
            gaps = {key[0]: feed.trades.gaps for key, feed in self.feeds.items() if key[1] == min(TF_OPTIONS)}
            holes = len(self._holes)
            pinned, skipped = list(self.pinned), list(self.pin_skipped)
            aggregates = sorted(feed_label(key) for key in self.aggregates.feeds) if self.aggregates else []
        channels = {channel.key: channel.snapshot() for channel in self.channels}
        states = {item["status"] for item in channels.values() if item["status"] != "idle"}
        status = self.status if self.status != "running" else (
            "connected" if states <= {"connected"} else "error" if states == {"error"} else "connecting")
        return {"status": status,
                "lastError": next((item["lastError"] for item in channels.values() if item["lastError"]), None),
                "feeds": feeds, "collecting": pinned, "collectSkipped": skipped,
                "aggregates": aggregates,
                "channels": channels,
                # 本次运行成交编号接不上的次数: 每次对应一段断线, 由回填补上
                "gaps": gaps, "holes": holes,
                "backfill": self.backfiller.snapshot() if self.backfiller is not None else None}

    def broadcast(self, msg: dict):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict):
        with self._lock:
            clients = list(self.clients.items())
        for q, subscription in clients:
            if (msg.get("symbol"), msg.get("tf"), msg.get("ltf")) != subscription:
                continue
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 同 FeedManager: 通知 WS 关闭, 让客户端重连并重取快照。
                self.remove_client(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "symbol": subscription[0], "tf": subscription[1]})

    # ------------------------------------------------------------------ 订阅集合
    def _wanted(self, key: str):
        return lambda: self._topics.get(key, frozenset())

    def trade_symbols(self, now=None) -> list[str]:
        """要收逐笔成交的合约: 常驻 + 页面临时打开且未闲置的 + 正被页面看着的。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            symbols = list(self.pinned)
            symbols += [symbol for symbol, seen in self._demand.items() if now - seen < IDLE_EVICT_SEC]
            symbols += [symbol for symbol, _, _ in self.clients.values()]
        if self.aggregates is not None:
            symbols = [part for symbol in symbols for part in
                       (self.aggregates.components(symbol) if venue_of(symbol) == "AGG" else [symbol])]
        return [symbol for symbol in dict.fromkeys(symbols) if self.instrument(symbol) is not None]

    def _sync(self, now: float):
        """按需求算出各路连接要订阅的 topic, 建好(并读回历史)要采集的 Feed, 回收不再需要的。"""
        with self._lock:
            self._resolve_pins()
            # 与 ensure 同一把锁: 否则页面刚建的 Feed 可能在算完集合之后、回收之前被删掉。
            trade_symbols = self.trade_symbols(now)
            self._interest = {key: until for key, until in self._interest.items() if until > now}
            kinds = {(symbol, kind) for symbol in trade_symbols for kind in ("trade", "ticker")}
            kinds |= set(self._interest)
            self._demand = {symbol: seen for symbol, seen in self._demand.items()
                            if now - seen < IDLE_EVICT_SEC}
            watched = {symbol for symbol, _, _ in self.clients.values()}
            for key in [key for key in self.feeds if key[0] not in trade_symbols and key[0] not in watched]:
                del self.feeds[key]
            for symbol in trade_symbols:
                for tf in TF_OPTIONS:
                    if (symbol, tf) not in self.feeds:
                        self.feeds[(symbol, tf)] = CryptoFeed(self.instruments[symbol], tf)
            feeds = list(self.feeds.values())
        topics: dict[str, set] = {channel.key: set() for channel in self.channels}
        for symbol, kind in kinds:
            instrument = self.instrument(symbol)
            if instrument is None:
                continue
            adapter = self.adapters[instrument.venue]
            for spec in adapter.channels:
                topic = spec.topic(kind, instrument.inst_id)
                if topic is not None:
                    topics[f"{instrument.venue}.{spec.name}"].add(topic)
        self._topics = {key: frozenset(value) for key, value in topics.items()}
        for feed in feeds:
            self._ensure_loaded(feed)

    def _ensure_loaded(self, feed: CryptoFeed):
        if feed.loaded:
            return
        try:
            feed.load_history()
            self._dirty.add(feed.symbol)
        except Exception as exc:
            feed.loaded = True    # 读不回历史也照常采集, 不能每轮都重试
            print(f"[crypto] {feed_label((feed.symbol, feed.tf))} 读回历史失败: {exc}", flush=True)

    # ------------------------------------------------------------------ 管理线程
    def _run(self):
        self.status = "running"
        self._sync(time.monotonic())
        for channel in self.channels:
            channel.start()
        if self.backfiller is not None:
            self.backfiller.start()
            for venue in self.adapters:
                self.backfiller.submit(("instruments", venue))
        last_compute = 0.0
        while not self._stop.is_set():
            try:
                batch = [self.events.get(timeout=RECV_TIMEOUT_SEC)]
            except queue.Empty:
                batch = []
            while len(batch) < 5000:
                try:
                    batch.append(self.events.get_nowait())
                except queue.Empty:
                    break
            for event in batch:
                self._apply_event(event)
            now = time.monotonic()
            if now - last_compute >= RECOMPUTE_SEC:
                last_compute = now
                self._cycle(now)

    def _cycle(self, now: float):
        """每 RECOMPUTE_SEC 一轮: 收回填结果、同步订阅、安排回填、重算快照、跑回调。"""
        self._apply_results(now)
        self._sync(now)
        self._schedule_backfill(now)
        self._compute(now)
        for hook in list(self.loop_hooks):
            try:
                hook(self)
            except Exception as exc:
                print(f"[crypto] 循环回调失败: {type(exc).__name__}: {exc}", flush=True)

    def _apply_event(self, event):
        kind, symbol = event[0], event[1]
        if kind == "trade":
            applied = False
            for tf in TF_OPTIONS:
                feed = self.feeds.get((symbol, tf))
                if feed is None:
                    continue
                self._ensure_loaded(feed)
                applied |= feed.trades.add(*event[2:])
            if applied:
                self._dirty.add(symbol)
        else:
            with self._lock:
                entry = self.quotes.setdefault(symbol, {}).setdefault(kind, {})
                entry.update(event[2])
                entry["received"] = time.time()    # 本地收到的时刻: 判断盘口新不新鲜, 不受两边时钟差影响

    def _apply_results(self, now: float):
        if self.backfiller is None:
            return
        while True:
            try:
                result = self.backfiller.results.get_nowait()
            except queue.Empty:
                return
            kind = result[0]
            if kind == "instruments":
                _, venue, instruments = result
                with self._lock:
                    for instrument in instruments:
                        self.instruments[instrument.symbol] = instrument
                    # 已建好的 Feed 换上新的合约参数(显示位数等)
                    for feed in self.feeds.values():
                        fresh = self.instruments.get(feed.symbol)
                        if fresh is not None:
                            feed.instrument = fresh
                self.adapters[venue].instruments_loaded(instruments)
            elif kind == "bars":
                _, symbol, tf, bars = result
                feed = self.feeds.get((symbol, tf))
                if feed is not None:
                    self._ensure_loaded(feed)
                    try:
                        feed.apply_backfill(bars)
                    except Exception as exc:
                        print(f"[crypto] {feed_label((symbol, tf))} 回填写入失败: {exc}", flush=True)
                    self._dirty.add(symbol)
            elif kind == "done":
                _, symbol, before_id, edges, error = result
                hole = self._holes.get((symbol, before_id))
                if error is None:
                    for tf in TF_OPTIONS:
                        feed = self.feeds.get((symbol, tf))
                        if feed is not None:
                            feed.trades.insert(edges, before_id)
                    self._holes.pop((symbol, before_id), None)
                    self._dirty.add(symbol)
                elif hole is not None:
                    hole["running"] = False
                    hole["retry_at"] = now + HOLE_RETRY_SEC
                    hole["error"] = error

    def _schedule_backfill(self, now: float):
        """把各合约窗口里新出现的洞登记下来, 到点的交给回填线程(一个洞同时只有一个任务)。"""
        if self.backfiller is None:
            return
        tf = min(TF_OPTIONS)
        for symbol in self.trade_symbols(now):
            feed = self.feeds.get((symbol, tf))
            if feed is None:
                continue
            for after_id, after_ms, before_id, before_ms in feed.trades.holes:
                self._holes.setdefault((symbol, before_id), {
                    "after_id": after_id, "after_ms": after_ms, "before_ms": before_ms,
                    "running": False, "retry_at": 0.0, "error": None})
        with self._lock:
            pinned = set(self.pinned)
        for (symbol, before_id), hole in list(self._holes.items()):
            if hole["running"] or now < hole["retry_at"]:
                continue
            feeds = {tf: self.feeds.get((symbol, tf)) for tf in TF_OPTIONS}
            if any(feed is None for feed in feeds.values()):
                del self._holes[(symbol, before_id)]      # 合约已经不采集了, 洞也不用补了
                continue
            hole["running"] = True
            self.backfiller.submit(self.backfiller.job(
                feeds[tf].instrument, hole["after_id"], hole["after_ms"], before_id, hole["before_ms"],
                {period: feed.complete_starts() for period, feed in feeds.items()},
                days=None if symbol in pinned else TEMPORARY_BACKFILL_DAYS))

    def _compute(self, now: float):
        """有新成交或有新需求的 Feed 才重算; 单个 Feed 出错只记在它自己身上, 稍后重试。"""
        with self._lock:
            clients = list(self.clients.values())
            feeds = list(self.feeds.items())
        watched = set()
        for symbol, tf, ltf in clients:
            watched.add((symbol, tf))
            feed = self.feeds.get((symbol, tf))
            if feed is not None:
                feed.request(ltf=ltf, demand=False)   # 保持在看的拆分粒度不过期
        dirty, self._dirty = self._dirty, set()
        for key, feed in feeds:
            if not feed.loaded:
                continue
            with feed._state_lock:
                stale = feed._computed_version != feed._demand_version or feed.error is not None
            if feed.symbol not in dirty and not stale:
                continue
            if (not stale and key not in watched and feed.trades.live_start == feed.computed_start
                    and now - feed.last_compute < IDLE_RECOMPUTE_SEC):
                self._dirty.add(feed.symbol)    # 没人看、也没有 bar 走完: 攒一攒再算
                continue
            if now < feed.compute_retry_at:
                self._dirty.add(feed.symbol)
                continue
            try:
                feed.recompute(self.broadcast)
                feed.compute_retry_at = 0.0
            except Exception as exc:
                with feed._state_lock:
                    feed.error = f"行情处理失败: {exc}"
                    feed.compute_retry_at = now + COMPUTE_RETRY_SEC
                print(f"[crypto] {feed_label(key)} {feed.error}", flush=True)
        if self.aggregates is not None:
            self.aggregates.compute(self, dirty | {key[0] for key, _ in feeds if key in watched})
