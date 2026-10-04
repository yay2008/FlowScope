# -*- coding: utf-8 -*-
"""OKX U 本位永续(BTC-USDT-SWAP 等): 推送解析、REST 与每日归档包(供 crypto_feed / crypto_backfill 使用)。

合约代码直接用 OKX 的: ``OKX.BTC-USDT-SWAP``。OKX 的数量按张计, 一律乘合约面值(ctVal, BTC 永续
一张 0.01 BTC)折成币, 才能和币安的量相加。

- 推送: 逐笔成交要走 business 通道的 ``trades-all``(每笔一条, 编号连续); public 通道的 ``trades``
  会把同一笔吃单合并。行情、五档盘口、标记价格、资金费率在 public 通道。连接 30 秒没有数据会被
  断开, 空闲时发 "ping" 保活。
- REST: 合约列表; ``history-trades`` 每次 100 笔、限频 20 次/2 秒, 只能往更早翻 —— 补一天
  (约 130 万笔)要 20 多分钟, 所以整天的尽量用归档包。
- 归档包: static.okx.com 每日一个, **按北京时间切日**, 约在次日 07:30(北京时间)之后发布, 没有校验文件;
  读回后检查编号连续、时间落在当天, 不对就不用。
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd

from crypto_backfill import FetchError, RateLimited, empty_trades
from crypto_feed import Instrument

PREFIX = "OKX."
PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"
BUSINESS_WS = "wss://ws.okx.com:8443/ws/v5/business"
REST = "https://www.okx.com"
ARCHIVE = "https://static.okx.com/cdn/okex/traderecords/trades/daily"
PAGE = 100                  # history-trades 每次最多 100 笔
REQUEST_GAP_SEC = 0.11      # 限频 20 次/2 秒
SUBSCRIBE_CHUNK = 50
BEIJING = timezone(timedelta(hours=8))
# 合约列表取不到时内置的合约: 合约, 币, 价格步长, 数量步长(张), 合约面值(交易所公布的参数)。
BUILTIN = (("BTC-USDT-SWAP", "BTC", 0.1, 0.01, 0.01),)


def symbol_of(inst_id: str) -> str:
    """BTC-USDT-SWAP -> OKX.BTC-USDT-SWAP"""
    return f"{PREFIX}{inst_id}"


def make_instrument(inst_id, base, tick, lot, contract_value, min_size=None, max_leverage=100, rank=0.0):
    step = lot * contract_value
    return Instrument(symbol=symbol_of(inst_id), venue="OKX", inst_id=inst_id, base=base,
                      label=f"{inst_id.removesuffix('-SWAP')} 永续 · OKX", tick_size=tick,
                      step_size=float(f"{step:.12g}"), contract_value=contract_value,
                      min_qty=float(f"{(min_size or lot) * contract_value:.12g}"),
                      max_leverage=max_leverage, rank=rank)


def _number(value):
    return None if value in (None, "") else float(value)


class OkxChannel:
    """一路推送连接(见 crypto_feed.Channel): topic 写成 "频道|合约", 连上后发 subscribe。"""

    ping = "ping"
    ping_sec = 20.0

    def __init__(self, name: str, url: str, kinds: dict[str, list[str]], adapter):
        self.name = name
        self._url = url
        self.kinds = kinds               # 报价种类 -> OKX 频道(一个种类可能要订两个频道)
        self.adapter = adapter

    def topic(self, kind: str, inst_id: str) -> str | None:
        channels = self.kinds.get(kind)
        return "+".join(f"{channel}|{inst_id}" for channel in channels) if channels else None

    def url(self, topics) -> str:
        return self._url

    @staticmethod
    def _args(topics):
        args = []
        for topic in sorted(topics):
            for part in topic.split("+"):
                channel, inst_id = part.split("|", 1)
                args.append({"channel": channel, "instId": inst_id})
        return args

    def _messages(self, op, topics):
        args = self._args(topics)
        return [json.dumps({"op": op, "args": args[index:index + SUBSCRIBE_CHUNK]})
                for index in range(0, len(args), SUBSCRIBE_CHUNK)]

    def subscribe_messages(self, topics) -> list[str]:
        return self._messages("subscribe", topics)

    def change_messages(self, current, wanted) -> list[str]:
        return self._messages("subscribe", wanted - current) + self._messages("unsubscribe", current - wanted)

    def parse(self, raw) -> list[tuple]:
        if raw == "pong":
            return []
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(message, dict):
            return []
        if message.get("event") == "error":
            print(f"[crypto] OKX 订阅出错: {message.get('msg')}", flush=True)
            return []
        arg, data = message.get("arg") or {}, message.get("data")
        inst_id = arg.get("instId")
        if not inst_id or not isinstance(data, list) or not data:
            return []
        symbol = symbol_of(inst_id)
        size = self.adapter.contract_values.get(inst_id)
        channel = arg.get("channel")
        try:
            if channel == "trades-all":
                if size is None:
                    return []            # 合约面值未知, 量折不成币, 宁可不要
                return [("trade", symbol, int(item["tradeId"]), float(item["px"]), float(item["sz"]) * size,
                         int(item["ts"]), item["side"] == "sell") for item in data]
            item = data[0]
            if channel == "tickers":
                last, opened = float(item["last"]), float(item["open24h"])
                volume = float(item["volCcy24h"])
                return [("ticker", symbol, {"last": last, "open": opened, "high": float(item["high24h"]),
                                            "low": float(item["low24h"]), "volume": volume,
                                            "amount": volume * last,
                                            "changePct": (last - opened) / opened * 100 if opened else None,
                                            "time": int(item["ts"])})]
            if channel == "books5" and size is not None:
                return [("book", symbol, {"bids": [[float(row[0]), float(row[1]) * size] for row in item["bids"]],
                                          "asks": [[float(row[0]), float(row[1]) * size] for row in item["asks"]],
                                          "time": int(item["ts"])})]
            if channel == "mark-price":
                return [("mark", symbol, {"markPrice": float(item["markPx"]), "time": int(item["ts"])})]
            if channel == "funding-rate":
                return [("mark", symbol, {"fundingRate": _number(item.get("fundingRate")),
                                          "nextFundingTime": int(item["fundingTime"]) if item.get("fundingTime") else None})]
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return []
        return []


class OkxAdapter:
    venue = "OKX"
    archive_tz_ms = 8 * 3_600_000        # 归档包按北京时间切日

    def __init__(self):
        self.contract_values = {inst_id: value for inst_id, _, _, _, value in BUILTIN}
        self.channels = [
            OkxChannel("business", BUSINESS_WS, {"trade": ["trades-all"]}, self),
            OkxChannel("public", PUBLIC_WS, {"ticker": ["tickers"], "book": ["books5"],
                                             "mark": ["mark-price", "funding-rate"]}, self),
        ]
        self._last_request = 0.0

    def builtin_instruments(self) -> list[Instrument]:
        return [make_instrument(inst_id, base, tick, lot, value) for inst_id, base, tick, lot, value in BUILTIN]

    def instruments_loaded(self, instruments):
        self.contract_values = {**self.contract_values,
                                **{item.inst_id: item.contract_value for item in instruments}}

    def _get(self, http, path, params):
        """按限频间隔发请求; OKX 限频时也可能回 200 + 错误码, 一并处理。"""
        wait = REQUEST_GAP_SEC - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        body, _ = http.json(f"{REST}{path}", params)
        code = str(body.get("code", "0")) if isinstance(body, dict) else "0"
        if code in ("50011", "50061"):
            raise RateLimited(f"OKX 限频({body.get('msg')})", 2.0)
        if code != "0":
            raise FetchError(f"OKX {path}: {code} {body.get('msg')}")
        return body.get("data") or []

    def load_instruments(self, http) -> list[Instrument]:
        rows = self._get(http, "/api/v5/public/instruments", {"instType": "SWAP"})
        try:
            tickers = self._get(http, "/api/v5/market/tickers", {"instType": "SWAP"})
            rank = {item["instId"]: float(item.get("volCcy24h") or 0) * float(item.get("last") or 0)
                    for item in tickers}
        except FetchError:
            rank = {}
        instruments = []
        for item in rows:
            if (item.get("settleCcy") != "USDT" or item.get("ctType") != "linear"
                    or item.get("state") != "live"):
                continue
            try:
                instruments.append(make_instrument(
                    item["instId"], item.get("ctValCcy") or item["instId"].split("-")[0],
                    float(item["tickSz"]), float(item["lotSz"]), float(item["ctVal"]),
                    float(item.get("minSz") or item["lotSz"]), int(float(item.get("lever") or 100)),
                    rank.get(item["instId"], 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        if not instruments:
            raise FetchError("OKX 合约列表为空")
        return instruments

    def archive_trades(self, http, instrument: Instrument, day_start: int) -> pd.DataFrame:
        """某个北京时间日的全部成交(归档包); 还没发布抛 NotPublished, 内容不完整抛 FetchError。"""
        day = datetime.fromtimestamp(day_start / 1000, tz=BEIJING)
        inst_id = instrument.inst_id
        url = f"{ARCHIVE}/{day:%Y%m%d}/{inst_id}-trades-{day:%Y-%m-%d}.zip"
        body, _ = http.get(url)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            frame = pd.read_csv(io.BytesIO(archive.read(archive.namelist()[0])),
                                usecols=["trade_id", "side", "price", "size", "created_time"])
        trades = pd.DataFrame({"id": frame["trade_id"].astype("int64"), "price": frame["price"].astype(float),
                               "qty": frame["size"].astype(float) * instrument.contract_value,
                               "t": frame["created_time"].astype("int64"),
                               "sell": frame["side"].astype(str).eq("sell")}).sort_values("id", ignore_index=True)
        if trades.empty:
            return trades
        ids = trades["id"].to_numpy()
        if ids[-1] - ids[0] + 1 != len(ids) or trades["t"].min() < day_start or trades["t"].max() >= day_start + 86_400_000:
            raise FetchError(f"OKX {inst_id} {day:%Y-%m-%d} 归档包不完整")
        return trades

    def rest_trades(self, http, instrument: Instrument, start: int, end: int,
                    after_id: int | None, before_id: int | None) -> pd.DataFrame:
        """[start, end) 里的成交: 只能往更早翻, 所以从段尾(或洞右端那笔)开始倒着取。"""
        inst_id = instrument.inst_id
        if before_id is not None:
            params = {"instId": inst_id, "type": 1, "after": before_id, "limit": PAGE}
        else:
            params = {"instId": inst_id, "type": 2, "after": end, "limit": PAGE}
        frames = []
        while True:
            rows = self._get(http, "/api/v5/market/history-trades", params)
            if not rows:
                break
            frames.append(pd.DataFrame({"id": [int(row["tradeId"]) for row in rows],
                                        "price": [float(row["px"]) for row in rows],
                                        "qty": [float(row["sz"]) * instrument.contract_value for row in rows],
                                        "t": [int(row["ts"]) for row in rows],
                                        "sell": [row["side"] == "sell" for row in rows]}))
            oldest_id = min(int(row["tradeId"]) for row in rows)
            oldest_t = min(int(row["ts"]) for row in rows)
            if oldest_t < start or (after_id is not None and oldest_id <= after_id + 1) or len(rows) < PAGE:
                break
            params = {"instId": inst_id, "type": 1, "after": oldest_id, "limit": PAGE}
        if not frames:
            return empty_trades()
        return pd.concat(frames, ignore_index=True).drop_duplicates("id").sort_values("id", ignore_index=True)
