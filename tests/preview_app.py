"""离线交互验收：python tests/preview_app.py [--duration 300]。

仅生成模拟行情，使用临时 CSV；不连接 TqSdk，不读取或改写真实历史。
"""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import uvicorn

import app as server
import catalog
import ingest
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
        """预览报价: 主周期订阅只需要 price_tick, 自选面板还要看得出涨跌。"""
        seed = sum(ord(char) for char in symbol)
        known = self.products.get(symbol)
        price = 1000 + seed % 4000
        base = price - (seed % 21 - 10) * 5
        return SimpleNamespace(
            price_tick=1.0,
            instrument_name=f"{known[2]}主连" if known else self._month_name(symbol),
            ins_class="CONT" if known else "FUTURE",
            underlying_symbol=f"{known[0]}.{known[1]}2611" if known else "",
            last_price=float(price),
            pre_settlement=float(base),
            pre_close=float(base),
            open_interest=float((seed % 90 + 10) * 1000),
            price_decs=0,
            expired=False,
        )

    def wait_update(self, deadline=None):
        return True


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


class PreviewManager(ingest.FeedManager):
    def __init__(self):
        super().__init__()
        # 合约目录查询也走采集线程, 这里用假合约服务作答, 保持预览完全离线。
        self.catalog_api = CatalogApi()

    def _run(self):
        self._set_status("connected")
        while not self._stop.is_set():
            self._run_jobs(self.catalog_api)
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
        # 模拟数据的快照必须留在临时目录: 混进真实 backups/ 会被当成最新一份, 推迟真实备份。
        server.backups = BackupScheduler(lambda: ingest.DATA_DIR,
                                         lambda: os.path.join(directory, "backups"))
        # 预置几个自选, 打开页面就能看到面板(写在临时数据目录里, 不碰真实自选)。
        for symbol in ("KQ.m@SHFE.fu", "KQ.m@DCE.i", "KQ.m@CZCE.TA"):
            server.favorites.add(symbol)
        runner = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=8765, log_level="warning"))
        if args.duration:
            timer = threading.Timer(args.duration, lambda: setattr(runner, "should_exit", True))
            timer.daemon = True
            timer.start()
        print("模拟行情验收: http://127.0.0.1:8765/ (临时数据)", flush=True)
        runner.run()
