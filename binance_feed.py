# -*- coding: utf-8 -*-
"""币安 U 本位永续: 推送解析、REST 与每日归档包(供 crypto_feed / crypto_backfill 使用)。

合约代码沿用 TradingView 的写法 ``BINANCE.BTCUSDT.P``。

- 推送: ``/market`` 路径收逐笔成交(aggTrade, ``m=true`` 表示买方挂单即主动卖)、24 小时行情与
  标记价格/资金费率; 五档盘口(depth5@500ms)只在 ``/public`` 路径上有。2026-10 实测旧路径
  ``/ws/``、``/stream`` 能握手却收不到任何消息, 所以必须带 ``/market``、``/public``。
  同一条连接上可以随时 SUBSCRIBE / UNSUBSCRIBE, 增减合约不用断线。
- REST(fapi): 合约列表与最近的逐笔成交。部分地区返回 451(开发服务器就是), 此时只用归档包。
  权重上限每分钟 2400, 按响应头 ``X-MBX-USED-WEIGHT-1M`` 留余量地等。
- 归档包: data.binance.vision 每日一个, 按 UTC 切日, 第二天约 07:20 UTC 发布, 附 SHA256 校验文件;
  成交编号与推送里的 ``a`` 是同一套(实测首尾能接上)。
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import time
import zipfile
from datetime import datetime, timezone

import pandas as pd

from crypto_backfill import FetchError, empty_trades
from crypto_feed import Instrument

PREFIX = "BINANCE."
SUFFIX = ".P"
MARKET_WS = "wss://fstream.binance.com/market/stream"
PUBLIC_WS = "wss://fstream.binance.com/public/stream"
REST = "https://fapi.binance.com"
ARCHIVE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
WEIGHT_LIMIT = 1800        # 每分钟 2400 的权重上限, 留出余量给别的程序
PAGE = 1000                # aggTrades 每次最多 1000 笔
SUBSCRIBE_CHUNK = 100      # 一条 SUBSCRIBE 最多带这么多个 stream
ARCHIVE_COLUMNS = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
                   "transact_time", "is_buyer_maker"]
# 合约列表取不到(REST 不通)时内置的合约: 交易对, 币, 价格步长, 数量步长(交易所公布的参数)。
BUILTIN = (("BTCUSDT", "BTC", 0.1, 0.001), ("ETHUSDT", "ETH", 0.01, 0.001))


def symbol_of(pair: str) -> str:
    """BTCUSDT -> BINANCE.BTCUSDT.P"""
    return f"{PREFIX}{pair}{SUFFIX}"


def make_instrument(pair, base, tick, step, min_qty=0.0, min_notional=0.0, rank=0.0) -> Instrument:
    return Instrument(symbol=symbol_of(pair), venue="BINANCE", inst_id=pair, base=base,
                      label=f"{pair} 永续 · 币安", tick_size=tick, step_size=step, min_qty=min_qty or step,
                      min_notional=min_notional, max_leverage=125 if base in ("BTC", "ETH") else 75, rank=rank)


def _number(value):
    return None if value in (None, "") else float(value)


class BinanceChannel:
    """一路推送连接(见 crypto_feed.Channel): 组合流, topic 形如 btcusdt@aggTrade。"""

    ping = None          # 心跳由 websockets 的 ping/pong 处理, 不用发应用层消息
    ping_sec = 0.0

    def __init__(self, name: str, url: str, kinds: dict[str, str]):
        self.name = name
        self.base_url = url
        self.kinds = kinds               # 报价种类 -> stream 后缀
        self._ids = itertools.count(1)

    def topic(self, kind: str, inst_id: str) -> str | None:
        suffix = self.kinds.get(kind)
        return f"{inst_id.lower()}@{suffix}" if suffix else None

    def url(self, topics) -> str:
        return f"{self.base_url}?streams={'/'.join(sorted(topics))}"

    def subscribe_messages(self, topics) -> list[str]:
        return []                        # 初始订阅已经写在 URL 里

    def change_messages(self, current, wanted) -> list[str]:
        messages = []
        for method, items in (("SUBSCRIBE", sorted(wanted - current)), ("UNSUBSCRIBE", sorted(current - wanted))):
            for index in range(0, len(items), SUBSCRIBE_CHUNK):
                messages.append(json.dumps({"method": method, "params": items[index:index + SUBSCRIBE_CHUNK],
                                            "id": next(self._ids)}))
        return messages

    def parse(self, raw) -> list[tuple]:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return []
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict) or not data.get("s"):
            return []                    # 订阅应答 {"result": null, "id": 1} 等
        symbol = symbol_of(data["s"])
        try:
            kind = data.get("e")
            if kind == "aggTrade":
                return [("trade", symbol, int(data["a"]), float(data["p"]), float(data["q"]),
                         int(data["T"]), bool(data["m"]))]
            if kind == "24hrTicker":
                return [("ticker", symbol, {"last": float(data["c"]), "open": float(data["o"]),
                                            "high": float(data["h"]), "low": float(data["l"]),
                                            "volume": float(data["v"]), "amount": float(data["q"]),
                                            "changePct": float(data["P"]), "time": int(data["E"])})]
            if kind == "markPriceUpdate":
                return [("mark", symbol, {"markPrice": float(data["p"]), "indexPrice": _number(data.get("i")),
                                          "fundingRate": _number(data.get("r")),
                                          "nextFundingTime": int(data["T"]) if data.get("T") else None,
                                          "time": int(data["E"])})]
            if kind == "depthUpdate":
                return [("book", symbol, {"bids": [[float(p), float(q)] for p, q in data["b"]],
                                          "asks": [[float(p), float(q)] for p, q in data["a"]],
                                          "time": int(data["E"])})]
        except (KeyError, TypeError, ValueError):
            return []
        return []


class BinanceAdapter:
    venue = "BINANCE"
    archive_tz_ms = 0                    # 归档包按 UTC 切日

    def __init__(self):
        self.channels = [
            BinanceChannel("market", MARKET_WS, {"trade": "aggTrade", "ticker": "ticker", "mark": "markPrice@1s"}),
            BinanceChannel("public", PUBLIC_WS, {"book": "depth5@500ms"}),
        ]

    def builtin_instruments(self) -> list[Instrument]:
        return [make_instrument(pair, base, tick, step) for pair, base, tick, step in BUILTIN]

    def instruments_loaded(self, instruments):
        """接口与 OKX 一致(OKX 要据此把张数折成币); 币安的数量本来就是币。"""

    @staticmethod
    def _pace(headers: dict):
        """本分钟权重快用完就等到下一分钟。"""
        try:
            used = int(headers.get("x-mbx-used-weight-1m", 0))
        except ValueError:
            return
        if used >= WEIGHT_LIMIT:
            time.sleep(61 - time.time() % 60)

    def load_instruments(self, http) -> list[Instrument]:
        """全部在交易的 USDT 永续, 带价格/数量步长、最小下单量与 24 小时成交额(排序用)。"""
        info, headers = http.json(f"{REST}/fapi/v1/exchangeInfo")
        self._pace(headers)
        try:
            tickers, headers = http.json(f"{REST}/fapi/v1/ticker/24hr")
            self._pace(headers)
            rank = {item["symbol"]: float(item.get("quoteVolume") or 0) for item in tickers}
        except FetchError:
            rank = {}
        instruments = []
        for item in info.get("symbols", []):
            if (item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT"
                    or item.get("status") != "TRADING"):
                continue
            filters = {entry.get("filterType"): entry for entry in item.get("filters", [])}
            try:
                tick = float(filters["PRICE_FILTER"]["tickSize"])
                lot = filters["LOT_SIZE"]
                step, min_qty = float(lot["stepSize"]), float(lot.get("minQty") or 0)
                notional = float((filters.get("MIN_NOTIONAL") or {}).get("notional") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            instruments.append(make_instrument(item["symbol"], item.get("baseAsset") or item["symbol"],
                                               tick, step, min_qty, notional, rank.get(item["symbol"], 0.0)))
        if not instruments:
            raise FetchError("币安合约列表为空")
        return instruments

    def archive_trades(self, http, instrument: Instrument, day_start: int) -> pd.DataFrame:
        """某个 UTC 日的全部成交(归档包); 还没发布抛 NotPublished, 校验不符抛 FetchError。"""
        date = datetime.fromtimestamp(day_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        pair = instrument.inst_id
        url = f"{ARCHIVE}/{pair}/{pair}-aggTrades-{date}.zip"
        checksum, _ = http.get(f"{url}.CHECKSUM")
        body, _ = http.get(url)
        expected = checksum.split()[0].decode("ascii", "replace").lower() if checksum.split() else ""
        if hashlib.sha256(body).hexdigest() != expected:
            raise FetchError(f"{pair} {date} 归档包校验不符")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            raw = archive.read(archive.namelist()[0])
        header = raw.startswith(b"agg_trade_id")   # 早年的包没有表头
        frame = pd.read_csv(io.BytesIO(raw), header=0 if header else None,
                            names=None if header else ARCHIVE_COLUMNS)
        trades = pd.DataFrame({"id": frame["agg_trade_id"].astype("int64"),
                               "price": frame["price"].astype(float),
                               "qty": frame["quantity"].astype(float),
                               "t": frame["transact_time"].astype("int64"),
                               "sell": frame["is_buyer_maker"].astype(str).str.lower().eq("true")})
        return trades.sort_values("id", ignore_index=True)

    def rest_trades(self, http, instrument: Instrument, start: int, end: int,
                    after_id: int | None, before_id: int | None) -> pd.DataFrame:
        """[start, end)(不超过 1 小时)里的成交: 知道左边那笔的编号就按编号接着取, 否则按时间取。"""
        pair = instrument.inst_id
        params = {"symbol": pair, "limit": PAGE}
        if after_id is not None:
            params["fromId"] = after_id + 1
        else:
            params.update(startTime=start, endTime=end - 1)   # 接口要求时间窗口小于 1 小时
        frames = []
        while True:
            rows, headers = http.json(f"{REST}/fapi/v1/aggTrades", params)
            self._pace(headers)
            if not rows:
                break
            frames.append(pd.DataFrame({"id": [int(row["a"]) for row in rows],
                                        "price": [float(row["p"]) for row in rows],
                                        "qty": [float(row["q"]) for row in rows],
                                        "t": [int(row["T"]) for row in rows],
                                        "sell": [bool(row["m"]) for row in rows]}))
            last_id, last_t = int(rows[-1]["a"]), int(rows[-1]["T"])
            # 不满一页: 时间窗口里的成交取完了, 或者已经追到最新; 越过段尾或洞右端也停
            if len(rows) < PAGE or last_t >= end or (before_id is not None and last_id >= before_id - 1):
                break
            params = {"symbol": pair, "limit": PAGE, "fromId": last_id + 1}
        if not frames:
            return empty_trades()
        return pd.concat(frames, ignore_index=True).drop_duplicates("id").sort_values("id", ignore_index=True)
