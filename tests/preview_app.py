"""离线交互验收：python tests/preview_app.py [--duration 300]。

仅生成模拟行情，使用临时 CSV；不连接 TqSdk，不读取或改写真实历史。
"""
import argparse
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
import ingest


def tick_rows(first, count):
    ids = np.arange(first, first + count)
    price = 3000 + np.round(15 * np.sin(ids / 80) + 5 * np.sin(ids / 13))
    buy = ids % 5 < 3
    return pd.DataFrame({"id": ids, "datetime": 1_800_000_000_000_000_000 + ids * 500_000_000,
                         "last_price": price, "ask_price1": price + (~buy),
                         "bid_price1": price - buy, "volume": 100 + ids * 2})


def kline_rows(ticks, seconds=30):
    width = seconds * 10**9
    groups = ticks.groupby(ticks.datetime // width * width)
    rows = groups.last_price.ohlc()
    rows["volume"] = groups.size() * 2
    return rows.reset_index()


class PreviewManager(ingest.FeedManager):
    def _run(self):
        self._set_status("connected")
        while not self._stop.is_set():
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
                    boundary = int(fresh.datetime.iloc[0]) // feed.bar_ns * feed.bar_ns
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
        ingest.DATA_DIR = directory
        server.manager = PreviewManager()
        runner = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=8765, log_level="warning"))
        if args.duration:
            timer = threading.Timer(args.duration, lambda: setattr(runner, "should_exit", True))
            timer.daemon = True
            timer.start()
        print("模拟行情验收: http://127.0.0.1:8765/ (临时数据)", flush=True)
        runner.run()
