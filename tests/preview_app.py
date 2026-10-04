"""离线交互验收：python tests/preview_app.py [--duration 300]。

仅生成模拟行情，使用临时 CSV；不连接 TqSdk 与交易所，不读取或改写真实历史。
加密永续用 ?symbol=BINANCE.BTCUSDT.P / OKX.BTC-USDT-SWAP / AGG.BTC 打开（模拟逐笔成交，回填最近一天）。
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import uvicorn

import app as server
import binance_feed
import catalog
import crypto_aggregate
import crypto_backfill
import crypto_feed
import ingest
import okx_feed
import paper
from backup import BackupScheduler
from indicator import tick_bar_start


class CatalogApi:
    """离线预览的合约目录: 用内置常用品种伪造合约服务, 不连 TqSdk。"""

    def __init__(self):
        self.products = {catalog.cont_symbol(exchange, product): (exchange, product, name)
                         for exchange, product, name in catalog.FALLBACK_PRODUCTS}
        self.names = {(exchange, product): name
                      for exchange, product, name in catalog.FALLBACK_PRODUCTS}

    def _month_name(self, symbol):
        exchange_id, _, rest = symbol.partition(".")
        product = rest.rstrip("0123456789")
        return f"{self.names.get((exchange_id, product), product)}{rest[len(product):]}"

    def _fake_open_interest(self, symbol):
        """假持仓量: 各品种量级不同、近月最大, 让预览的排序看起来合理(数值无含义)。"""
        exchange_id, _, rest = symbol.partition(".")
        product = rest.rstrip("0123456789")
        digits = rest[len(product):]
        base = sum(ord(char) for char in f"{exchange_id}.{product}") % 300 * 1000 + 20000
        month = int(digits) if digits.isdigit() else 2611
        return max(1000, base - abs(month - 2611) * 700)

    def query_quotes(self, ins_class=None, exchange_id=None, product_id=None, expired=None):
        if ins_class == "CONT" or exchange_id == "KQ":
            # 主连在合约服务里的交易所是 KQ ("合约是否存在"的检查按 KQ 查)
            return list(self.products)
        if exchange_id and product_id:
            return [f"{exchange_id}.{product_id}{month}" for month in ("2610", "2611", "2701")]
        return []

    def query_symbol_info(self, symbols):
        rows = []
        for symbol in symbols:
            known = self.products.get(symbol)
            rows.append({
                "instrument_id": symbol,
                "instrument_name": f"{known[2]}主连" if known else self._month_name(symbol),
                "underlying_symbol": f"{known[0]}.{known[1]}2611" if known else "",
                "pre_open_interest": self._fake_open_interest(symbol),
            })
        return pd.DataFrame(rows)

    def get_quote(self, symbol):
        """预览报价: 主周期订阅只需要 price_tick, 自选面板还要看得出涨跌, 模拟交易还要盘口。

        价格与预览 K 线同一水平(都在 3000 附近), 下单后的持仓线、挂单线才落在图上;
        昨结算按品种错开, 自选面板的涨跌幅各不相同。盘口随时间摆动(见 PreviewQuote)。
        """
        known = self.products.get(symbol)
        seed = sum(ord(char) for char in (f"{known[0]}.{known[1]}" if known else symbol.rstrip("0123456789")))
        price = 3000
        base = price - (seed % 21 - 10) * 5
        return PreviewQuote(
            price=float(price),
            price_tick=1.0,
            instrument_name=f"{known[2]}主连" if known else self._month_name(symbol),
            ins_class="CONT" if known else "FUTURE",
            underlying_symbol=f"{known[0]}.{known[1]}2611" if known else "",
            pre_settlement=float(base),
            pre_close=float(base),
            open_interest=float((seed % 90 + 10) * 1000),
            price_decs=0,
            expired=False,
            volume_multiple=10,
            upper_limit=float("nan"),
            lower_limit=float("nan"),
            # 全天都是交易时段, 预览随时能下单(周末仍按交易所规则休市)
            trading_time={"day": [["00:00:00", "24:00:00"]], "night": []},
        )

    def wait_update(self, deadline=None):
        return True


class PreviewQuote(SimpleNamespace):
    """与 TqSdk 的报价对象一样就地更新: 最新价随时间在基准价上下摆动, 盘口买一 = 最新价, 卖一高一跳。"""

    @property
    def last_price(self):
        return self.price + round(15 * math.sin(time.time() / 20))

    @property
    def bid_price1(self):
        return self.last_price

    @property
    def ask_price1(self):
        return self.last_price + self.price_tick

    @property
    def bid_volume1(self):
        return 5 + int(time.time()) % 20

    @property
    def ask_volume1(self):
        return 25 - int(time.time()) % 20

    @property
    def datetime(self):
        return paper.beijing_now().strftime("%Y-%m-%d %H:%M:%S.%f")


def tick_rows(first, count):
    ids = np.arange(first, first + count)
    price = 3000 + np.round(15 * np.sin(ids / 80) + 5 * np.sin(ids / 13))
    buy = ids % 5 < 3
    return pd.DataFrame({"id": ids, "datetime": 1_800_000_000_000_000_000 + ids * 500_000_000,
                         "last_price": price, "ask_price1": price + (~buy),
                         "bid_price1": price - buy, "volume": 100 + ids * 2})


def kline_rows(ticks, seconds=30):
    groups = ticks.groupby(tick_bar_start(ticks.datetime, seconds * 10**9))
    rows = groups.last_price.ohlc()
    rows["volume"] = groups.size() * 2
    return rows.reset_index()


SIM_STEP_MS = 2000      # 模拟成交: 每 2 秒一笔, 编号 = 时间 / 2 秒, 所以任何一段时间的成交都能当场算出来


def sim_trade(k: int, base: float, seed: int):
    """第 k 笔模拟成交: (编号, 价, 量, 毫秒, 是否主动卖)。推送与回填用同一个函数, 两边严丝合缝。"""
    price = round(base + 300 * math.sin(k / 900) + 40 * math.sin(k / 37 + seed), 1)
    return k, price, round(0.001 * (1 + (k * 7 + seed) % 50), 3), k * SIM_STEP_MS, (k * 3 + seed) % 5 >= 3


def sim_frame(start: int, end: int, base: float, seed: int) -> pd.DataFrame:
    rows = [sim_trade(k, base, seed) for k in range(-(-start // SIM_STEP_MS), -(-end // SIM_STEP_MS))]
    return pd.DataFrame(rows, columns=["id", "price", "qty", "t", "sell"])


class SimChannel:
    """离线预览的推送通道(接口同 crypto_feed.Channel 的 spec): topic 写成 "种类:合约"。"""

    name = "sim"
    ping = None
    ping_sec = 0.0

    def __init__(self, adapter):
        self.adapter = adapter

    def topic(self, kind, inst_id):
        return f"{kind}:{inst_id}" if kind in ("trade", "ticker", "book", "mark") else None

    def url(self, topics):
        return f"sim://{self.adapter.venue}?{','.join(sorted(topics))}"

    def subscribe_messages(self, topics):
        return []

    def change_messages(self, current, wanted):
        return [json.dumps({"topics": sorted(wanted)})]

    def parse(self, raw):
        return [tuple(event) for event in json.loads(raw)]


class SimSocket:
    """按真实时间吐出模拟成交; 每 0.5 秒再给一次 24 小时行情、五档盘口与标记价格。"""

    def __init__(self, adapter, url):
        self.adapter = adapter
        self.topics = set(url.split("?", 1)[1].split(",")) if "?" in url else set()
        self.next_k = int(time.time() * 1000) // SIM_STEP_MS
        self.next_quote = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        self.topics = set(json.loads(message)["topics"])

    def recv(self, timeout=None):
        deadline = time.time() + (timeout or 0)
        while True:
            events = []
            if self.next_k * SIM_STEP_MS <= time.time() * 1000:
                for inst_id, (symbol, base, seed) in self.adapter.sim.items():
                    if f"trade:{inst_id}" in self.topics:
                        events.append(["trade", symbol, *sim_trade(self.next_k, base, seed)])
                self.next_k += 1
            elif time.time() >= self.next_quote:
                self.next_quote = time.time() + 0.5
                events = self._quotes()
            if events:
                return json.dumps(events)
            if time.time() >= deadline:
                raise TimeoutError
            time.sleep(0.05)

    def _quotes(self):
        events = []
        k = int(time.time() * 1000) // SIM_STEP_MS
        for inst_id, (symbol, base, seed) in self.adapter.sim.items():
            price = sim_trade(k, base, seed)[1]
            opened = sim_trade(k - 43200, base, seed)[1]
            if f"ticker:{inst_id}" in self.topics:
                events.append(["ticker", symbol, {"last": price, "open": opened, "high": price + 300,
                                                  "low": price - 300, "volume": 1100.0, "amount": 1100.0 * price,
                                                  "changePct": (price - opened) / opened * 100}])
            if f"book:{inst_id}" in self.topics:
                events.append(["book", symbol, {"bids": [[round(price - 0.1 * i, 1), 0.5 + i] for i in range(5)],
                                                "asks": [[round(price + 0.1 * (i + 1), 1), 0.5 + i] for i in range(5)],
                                                "time": int(time.time() * 1000)}])
            if f"mark:{inst_id}" in self.topics:
                nxt = (int(time.time() * 1000) // 28_800_000 + 1) * 28_800_000
                events.append(["mark", symbol, {"markPrice": price, "fundingRate": 0.0001, "nextFundingTime": nxt}])
        return events


def preview_adapter(cls, sim):
    """真实适配器换成模拟数据: 合约参数用内置的, 推送、归档包、REST 都走模拟成交。"""

    class Adapter(cls):
        def __init__(self):
            super().__init__()
            self.sim = sim                      # 原生代码 -> (合约代码, 基准价, 种子)
            self.channels = [SimChannel(self)]

        def load_instruments(self, http):
            raise crypto_backfill.FetchError("离线预览只用内置合约")

        def archive_trades(self, http, instrument, day):
            raise crypto_backfill.NotPublished("离线预览没有归档包")

        def rest_trades(self, http, instrument, start, end, after_id, before_id):
            _, base, seed = self.sim[instrument.inst_id]
            return sim_frame(start, end, base, seed)

    return Adapter()


def preview_crypto():
    """离线预览的加密行情: 币安 BTC、OKX BTC 两路模拟成交, 回填最近一天, 外加多所汇总。"""
    adapters = [preview_adapter(binance_feed.BinanceAdapter, {"BTCUSDT": ("BINANCE.BTCUSDT.P", 85000.0, 1)}),
                preview_adapter(okx_feed.OkxAdapter, {"BTC-USDT-SWAP": ("OKX.BTC-USDT-SWAP", 85003.0, 2)})]
    venues = {adapter.venue: adapter for adapter in adapters}
    manager = crypto_feed.CryptoManager(adapters, backfiller=crypto_backfill.Backfiller(days=1),
                                        connect=lambda url: SimSocket(venues[url[6:].split("?")[0]], url))
    manager.aggregates = crypto_aggregate.AggregateBook(manager)
    return manager


class PreviewManager(ingest.FeedManager):
    def __init__(self):
        super().__init__()
        # 合约目录查询也走采集线程, 这里用假合约服务作答, 保持预览完全离线。
        self.catalog_api = CatalogApi()

    def _run(self):
        self._set_status("connected")
        while not self._stop.is_set():
            self._run_jobs(self.catalog_api)
            self._run_loop_hooks(self.catalog_api)   # 模拟交易: 刷新盘口、撮合挂单
            with self._lock:
                feeds = list(self.feeds.values())
                clients = list(self.clients.values())
            for feed in feeds:
                if feed.ticks is None:
                    feed.quote = SimpleNamespace(price_tick=1.)
                    feed.ticks = tick_rows(0, 10000)
                    feed.klines = kline_rows(feed.ticks, feed.tf)
                else:
                    fresh = tick_rows(int(feed.ticks.id.iloc[-1]) + 1, 1)
                    feed.ticks = pd.concat([feed.ticks, fresh], ignore_index=True).tail(10000)
                    boundary = tick_bar_start(int(fresh.datetime.iloc[0]), feed.bar_ns)
                    last = feed.klines.iloc[-1]
                    price = float(fresh.last_price.iloc[0])
                    if int(last.datetime) == boundary:
                        idx = feed.klines.index[-1]
                        feed.klines.loc[idx, ["high", "low", "close", "volume"]] = [
                            max(last.high, price), min(last.low, price), price, last.volume + 2]
                    else:
                        feed.klines = pd.concat(
                            [feed.klines, kline_rows(fresh, feed.tf)], ignore_index=True).tail(2000)
                for symbol, tf, ltf, footprint in clients:
                    if symbol == feed.symbol and tf == feed.tf:
                        feed.request(ltf=ltf, footprint=footprint)
                try:
                    with feed._state_lock:
                        requested = list(feed._requested)
                    for ltf in requested:
                        if ltf > 0:
                            feed.lower_klines[ltf] = (feed.klines if ltf == feed.tf
                                                      else kline_rows(feed.ticks, ltf))
                    # 模拟 K 线与 tick 分批到达；完整→部分→完整不得导致图表异常或重连循环。
                    idx = feed.klines.index[-1]
                    actual_volume = feed.klines.loc[idx, "volume"]
                    if int(feed.ticks.id.iloc[-1]) % 10 == 0:
                        feed.klines.loc[idx, "volume"] = actual_volume + 1
                    feed.recompute(self.broadcast)
                    feed.klines.loc[idx, "volume"] = actual_volume
                except Exception as exc:
                    self._set_status("error", str(exc))
                    raise
            self._stop.wait(.3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="flowscope-preview-") as directory:
        ingest.DATA_DIR = os.path.join(directory, "data")
        os.makedirs(ingest.DATA_DIR)
        server.manager = PreviewManager()
        server.crypto = preview_crypto()
        # 模拟数据的快照必须留在临时目录: 混进真实 backups/ 会被当成最新一份, 推迟真实备份。
        server.backups = BackupScheduler(lambda: ingest.DATA_DIR,
                                         lambda: os.path.join(directory, "backups"))
        # 预置几个自选, 打开页面就能看到面板(写在临时数据目录里, 不碰真实自选)。
        for symbol in ("KQ.m@SHFE.fu", "KQ.m@DCE.i", "KQ.m@CZCE.TA", "BINANCE.BTCUSDT.P", "AGG.BTC"):
            server.favorites.add(symbol)
        runner = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=8765, log_level="warning"))
        if args.duration:
            timer = threading.Timer(args.duration, lambda: setattr(runner, "should_exit", True))
            timer.daemon = True
            timer.start()
        print("模拟行情验收: http://127.0.0.1:8765/ (临时数据)", flush=True)
        runner.run()
