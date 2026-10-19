"""只算尾部(增量重算)的新旧对拍: 同一串确定的行情分别喂给改动前的代码(取自 git)和当前工作区的代码,
逐轮比较推送消息、快照、内存里的完整窗口, 最后比较写出的历史文件。不连网、不碰 data/。

- 加密(crypto): 币安 + OKX 的 BTC, 多所汇总、1 分钟大周期、K 线拆分口径都开着; 断线留洞、回填(含失败)、
  长时间没成交、窗口滑动与裁剪、页面断开再连、重启后读回同一个数据目录。
- 期货(futures): 模拟 TqSdk 的 tick 与 K 线序列(原地改末根、追加新根); 改窗口中间的旧 K 线、断线重连、
  换日成交量清零、合约乘数后到、足迹图开关、拆分口径、别处写入历史文件、1 分钟大周期。

用法(仓库根目录):
    .venv\\Scripts\\python.exe docs\\tail_recompute_check.py                  # 两个市场, 旧代码取 HEAD
    .venv\\Scripts\\python.exe docs\\tail_recompute_check.py --market futures
    .venv\\Scripts\\python.exe docs\\tail_recompute_check.py --base 8692d7b   # 提交了这次改动之后要指回改动前
新旧(和两个市场)同时开子进程跑; 开发机(单核)上加密一份约 1 小时、期货约半小时, 多核的台式机快得多。
完全相同时退出码 0, 否则打印最先不同的几轮, 退出码 1, 并保留工作目录供排查。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = {"crypto": 1400, "futures": 1200}


def _sha(text):
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _count_tails(ingest, tails):
    """新代码才有只算尾部: 数一下 _publish 有多少次是只算尾部的, 免得两边一致只是因为都在整窗口算。"""
    if not hasattr(ingest.Feed, "_tail_since"):
        return
    publish = ingest.Feed._publish

    def counted(self, ltf, bars, previous, revision, tail=None):
        tails[tail is not None] += 1
        return publish(self, ltf, bars, previous, revision, tail)
    ingest.Feed._publish = counted


def _write_files(out, data_dir):
    for name in sorted(os.listdir(data_dir)):
        with open(os.path.join(data_dir, name), "rb") as handle:
            out.write(f"file {name} {hashlib.sha1(handle.read()).hexdigest()[:16]}\n")


# ---------------------------------------------------------------------------------------------- 加密
def run_crypto(clock, out, data_dir, tails):
    os.environ["LARGE_TRADE_SETTINGS"] = json.dumps({"*": {"minNotional": 20000, "warmup": 50,
                                                           "windowSec": 120, "lagSec": 5}})
    import pandas as pd
    import ingest
    import large_trades
    import crypto_feed
    from binance_feed import BinanceAdapter
    from okx_feed import OkxAdapter
    from crypto_aggregate import AggregateBook
    from indicator import NATIVE_TFS, ltf_options

    _count_tails(ingest, tails)
    ingest.DATA_DIR = data_dir
    large_trades.EVENT_CAP = 40
    large_trades.RAW_SLACK = 300
    crypto_feed.TRIM_SLACK = 10
    crypto_feed.SNAPSHOT_BARS = 30
    ingest.SNAPSHOT_BARS = 30
    init = crypto_feed.TradeBars.__init__
    crypto_feed.TradeBars.__init__ = lambda self, tf, max_bars=60: init(self, tf, max_bars)

    symbols = ["BINANCE.BTCUSDT.P", "OKX.BTC-USDT-SWAP"]
    rng = random.Random(7)
    start_ms = 1_790_000_000_000 // 30000 * 30000
    oracle = {s: [] for s in symbols}     # 全部成交(含掉线没收到的), 回填从这里取
    state = {s: {"id": 5_000_000 + 1000 * i, "price": 100000.0, "t": start_ms} for i, s in enumerate(symbols)}
    sent = []

    def gen_until(end_ms):
        rows = []
        for symbol in symbols:
            st = state[symbol]
            while True:
                phase = (st["t"] // 45000) % 7        # 有时整段没成交(空 bar), 有时很密
                rate = 0.0 if phase == 3 else (12.0 if phase == 5 else 3.0)
                step = 9000 if rate == 0 else int(rng.expovariate(rate) * 1000) + 1
                if st["t"] + step >= end_ms:
                    break
                st["t"] += step
                if rate == 0:
                    continue
                st["id"] += 1
                st["price"] = round(st["price"] * (1 + rng.gauss(0, 0.0002)), 1)
                qty = round(rng.lognormvariate(-3, 1.6), 3) or 0.001
                row = (st["id"], st["price"], qty, st["t"], rng.random() < 0.5)
                oracle[symbol].append(row)
                rows.append((st["t"], symbol, row))
        rows.sort(key=lambda item: (item[0], item[1]))
        return rows

    class FakeBackfiller:
        """收任务, 隔几轮按 oracle 交回结果(同 crypto_backfill 的消息格式); 每 5 个有 1 个回填失败。"""

        def __init__(self):
            self.results = queue.Queue()
            self.pending = []
            self.count = 0

        def bind(self, adapters):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def snapshot(self):
            return {}

        def job(self, instrument, after_id, after_ms, before_id, before_ms, complete, days=None):
            start = after_ms if after_id is not None else (before_ms - 400_000) // 30000 * 30000
            return (instrument.symbol, start, before_ms, after_id, before_id, complete)

        def submit(self, job):
            self.count += 1
            self.pending.append((clock[0] + 4 + (self.count % 3), self.count, job))

        def answer(self):
            due = [item for item in self.pending if item[0] <= clock[0]]
            self.pending = [item for item in self.pending if item[0] > clock[0]]
            for _, number, (symbol, start, end, after_id, before_id, complete) in due:
                if number % 5 == 4:
                    self.results.put(("done", symbol, before_id, None, "测试: 回填失败"))
                    continue
                rows = [r for r in oracle[symbol] if start <= r[3] < end + 1 and r[0] < before_id
                        and (after_id is None or r[0] > after_id)]
                trades = pd.DataFrame(rows, columns=["id", "price", "qty", "t", "sell"])
                for tf in NATIVE_TFS:
                    bars = crypto_feed.aggregate_trades(trades, tf, ltf_options(tf), start, end + 1)
                    skip = complete.get(tf, set())
                    bars = bars[~bars.index.isin(skip)] if skip else bars
                    if not bars.empty:
                        self.results.put(("bars", symbol, tf, bars))
                if rows:
                    events, spans, rel, rel_spans = large_trades.judge_history(
                        symbol, crypto_feed.large_trade_source(symbol), rows, include_relative=True)
                    if events or spans or rel or rel_spans:
                        self.results.put(("large", symbol, events, spans, rel, rel_spans))
                edge_lo = end // 30000 * 30000
                edges = [r for r in rows
                         if r[3] >= edge_lo or (after_id is not None and r[3] < start // 30000 * 30000 + 30000)]
                self.results.put(("done", symbol, before_id, edges, None))

    def record(cycle, manager):
        feeds = dict(manager.feeds)
        feeds.update({("AGG",) + key: feed for key, feed in manager.aggregates.feeds.items()})
        feeds.update({("ROLL",) + key: feed for key, feed in manager.rollups.items()})
        msgs = "\n".join(json.dumps(m, sort_keys=True, default=str) for m in sent)
        snaps, latest = [], []
        for key, feed in sorted(feeds.items(), key=lambda item: str(item[0])):
            snaps.append(f"{key}:{json.dumps(feed.snapshots, sort_keys=True, default=str)}")
            for ltf, (revision, frame) in sorted(feed.latest.items()):
                latest.append(f"{key}:{ltf}:{revision}:{frame.dtypes.to_dict()}:"
                              f"{frame.to_json(orient='split', double_precision=15)}")
        out.write(f"{cycle} msgs={_sha(msgs)} n={len(sent)} snaps={_sha(chr(10).join(snaps))} "
                  f"latest={_sha(chr(10).join(latest))}\n")
        sent.clear()

    def build():
        manager = crypto_feed.CryptoManager([BinanceAdapter(), OkxAdapter()], connect=None,
                                            backfiller=FakeBackfiller())
        manager.aggregates = AggregateBook(manager)
        manager.set_pinned(["AGG.BTC"])
        manager.broadcast = sent.append
        manager._sync(clock[0])
        return manager

    def watch(manager, clients, symbol, tf, ltf):
        manager.ensure(symbol, tf)
        key = object()
        manager.add_client(key, symbol, ltf, tf=tf)
        clients[(symbol, tf, ltf)] = key

    def run(manager, cycles, offset, drops=()):
        clients = {}
        for symbol, tf, ltf in [("BINANCE.BTCUSDT.P", 10, 0), ("BINANCE.BTCUSDT.P", 30, 0),
                                ("BINANCE.BTCUSDT.P", 10, 5), ("AGG.BTC", 10, 0), ("AGG.BTC", 30, 1),
                                ("BINANCE.BTCUSDT.P", 60, 0), ("AGG.BTC", 60, 0)]:
            watch(manager, clients, symbol, tf, ltf)
        sim = state["BINANCE.BTCUSDT.P"]["t"]
        for i in range(cycles):
            cycle = offset + i
            clock[0] += 1.0
            sim += 1000
            for _, symbol, row in gen_until(sim):
                if any(symbol == s and lo <= cycle < hi for s, lo, hi in drops):
                    continue                          # 掉线: 这段没收到
                manager._apply_event(("trade", symbol, *row))
            manager.backfiller.answer()
            if cycle == offset + 200:                 # 页面断开, 过一阵又打开
                manager.remove_client(clients.pop(("BINANCE.BTCUSDT.P", 10, 5)))
            if cycle == offset + 260:
                watch(manager, clients, "BINANCE.BTCUSDT.P", 10, 5)
            manager._cycle(clock[0])
            record(cycle, manager)

    run(build(), 900, 0, drops=[("BINANCE.BTCUSDT.P", 300, 330), ("OKX.BTC-USDT-SWAP", 650, 655)])
    gen_until(state["BINANCE.BTCUSDT.P"]["t"] + 120_000)   # 重启: 中间两分钟的成交没收到
    run(build(), 500, 900, drops=[("OKX.BTC-USDT-SWAP", 1200, 1203)])


# ---------------------------------------------------------------------------------------------- 期货
def run_futures(clock, out, data_dir, tails):
    os.environ["LARGE_TRADE_SETTINGS"] = json.dumps({"*": {"warmup": 30, "windowSec": 120, "lagSec": 3,
                                                           "minQty": 3}})
    import numpy as np
    import pandas as pd
    import ingest
    from indicator import TZ_SHIFT_S, tick_bar_start

    _count_tails(ingest, tails)
    ingest.DATA_DIR = data_dir
    ingest.SNAPSHOT_BARS = 60
    ns = 10**9
    tick_ms, window_ticks, window_bars = 1500, 600, 150
    rng = random.Random(11)
    symbol, base_ns = "KQ.m@SHFE.fu", 1_800_000_000 * ns
    sent = []

    class Market:
        """模拟 TqSdk: tick 序列(编号连续, 成交量是当日累计)和各周期 K 线序列。"""

        def __init__(self):
            self.id, self.t, self.price, self.volume = 0, base_ns, 3000.0, 1000.0
            rows = [self._tick() for _ in range(window_ticks)]
            self.ticks = pd.DataFrame(rows)
            self.klines = {tf: self._history(tf) for tf in (5, 10, 30, 60)}
            for tick in rows:
                for tf in self.klines:
                    self._apply(tf, tick)

        def _tick(self):
            self.id += 1
            self.t += tick_ms * 1_000_000
            if rng.random() < 0.7:
                self.price += rng.choice((-1, 0, 1))
                self.volume += rng.choice((1, 1, 2, 3, 5, 8, 40))
            bid = self.price - (rng.random() < 0.5)
            return {"id": self.id, "datetime": float(self.t), "last_price": self.price, "ask_price1": bid + 1,
                    "bid_price1": bid, "volume": self.volume}

        @staticmethod
        def _history(tf):
            first = tick_bar_start(base_ns + tick_ms * 1_000_000, tf * ns)
            i = np.arange(-window_bars, 0)
            mid = 3000 + np.round(10 * np.sin(i / 7))
            return pd.DataFrame({"datetime": (first + i * tf * ns).astype(float), "open": mid, "high": mid + 2,
                                 "low": mid - 2, "close": mid + 1, "volume": 50.0 + i % 9})

        def _apply(self, tf, tick, dv=None):
            frame = self.klines[tf]
            start = float(tick_bar_start(int(tick["datetime"]), tf * ns))
            dv = 0.0 if dv is None else dv
            last = frame.index[-1]
            if frame.at[last, "datetime"] == start:      # SDK 原地改末根
                frame.loc[last, ["high", "low", "close", "volume"]] = [
                    max(frame.at[last, "high"], tick["last_price"]), min(frame.at[last, "low"], tick["last_price"]),
                    tick["last_price"], frame.at[last, "volume"] + dv]
            else:
                row = pd.DataFrame([{"datetime": start, "open": tick["last_price"], "high": tick["last_price"],
                                     "low": tick["last_price"], "close": tick["last_price"], "volume": dv}])
                self.klines[tf] = pd.concat([frame, row], ignore_index=True).tail(window_bars).reset_index(drop=True)

        def step(self, day_roll=False):
            before = self.volume
            if day_roll:
                self.volume = 0.0
            tick = self._tick()
            dv = tick["volume"] - (0.0 if day_roll else before)
            self.ticks = pd.concat([self.ticks, pd.DataFrame([tick])], ignore_index=True).tail(window_ticks)
            for tf in self.klines:
                self._apply(tf, tick, dv)

        def reconnect(self, lost):
            """断线: 中间 lost 笔没收到, tick 窗口整个换成新的(与旧窗口不重叠)。"""
            for _ in range(lost):
                tick = self._tick()
                for tf in self.klines:
                    self._apply(tf, tick, 1.0)
            rows = []
            for _ in range(window_ticks):
                tick = self._tick()
                rows.append(tick)
                for tf in self.klines:
                    self._apply(tf, tick, 1.0)
            self.ticks = pd.DataFrame(rows)

    def confirmed(part, buy, sell):
        rows = pd.DataFrame({"time": (part.datetime // ns + TZ_SHIFT_S).astype("int64"), "buy": buy,
                             "sell": sell, "unknown": 0.0, "buyLegacy": buy, "sellLegacy": sell,
                             "coverage": "complete", "hasBaseline": True})
        rows["delta"] = rows.buy - rows.sell
        return rows

    market = Market()
    quote = SimpleNamespace(price_tick=1.0, volume_multiple=None, price_decs=0, trading_time=None)
    feeds = {}
    for tf in (10, 30):
        feeds[tf] = ingest.Feed(symbol, tf)
        feeds[tf].quote = quote
    rollup = ingest.FuturesRollupFeed(symbol, 60, lambda: feeds[30])
    older = market.klines[30].iloc[:100]           # 30s 窗口之前一段已核对的历史: 合并历史与 CVD 起点要用
    feeds[30]._store(0).save_completed(confirmed(older, older.volume * 0.6, older.volume * 0.4), final=True)

    def sync(feed):
        feed.ticks = market.ticks
        feed.klines = market.klines[feed.tf]
        feed.lower_klines = {feed.tf: feed.klines}
        with feed._state_lock:
            wanted = [ltf for ltf in feed._requested if ltf > 0]
        for ltf in wanted:
            feed.lower_klines[ltf] = market.klines[ltf] if ltf in market.klines else feed.klines

    for step in range(ROUNDS["futures"]):
        clock[0] += tick_ms / 1000
        if step == 600:
            market.reconnect(40)
        else:
            market.step(day_roll=step == 900)
        if step == 40:
            quote.volume_multiple = 10                  # 合约乘数后到: 检测器从整个 tick 窗口补算
        if step % 97 == 50:                             # SDK 修订了前面一根 K 线的量
            for frame in market.klines.values():
                frame.loc[frame.index[-4], "volume"] += 1
        if step == 1100:                                # 别处(回填、另一个进程)往历史文件写了窗口中间几根
            part = market.klines[30].iloc[-30:-25]
            feeds[30]._store(0).save_completed(confirmed(part, part.volume, 0.0), final=True)
        feeds[30].request(ltf=5)
        if 300 <= step < 500 or step >= 1050:
            feeds[10].request(footprint=True)
        for feed in (feeds[10], feeds[30]):
            sync(feed)
            feed.recompute(sent.append)
        rollup.klines = market.klines[60]
        rollup.request(ltf=0)
        rollup.recompute(sent.append)
        msgs = "\n".join(json.dumps(m, sort_keys=True, default=str) for m in sent)
        snaps, latest = [], []
        for name, feed in (("10", feeds[10]), ("30", feeds[30]), ("60", rollup)):
            snaps.append(json.dumps([feed.snapshots, feed.footprint], sort_keys=True, default=str))
            for ltf, (revision, frame) in sorted(feed.latest.items()):
                latest.append(f"{name}:{ltf}:{revision}:{frame.dtypes.to_dict()}:"
                              f"{frame.to_json(orient='split', double_precision=15)}")
        out.write(f"{step} msgs={_sha(msgs)} n={len(sent)} snaps={_sha(chr(10).join(snaps))} "
                  f"latest={_sha(chr(10).join(latest))}\n")
        sent.clear()


def child(market, code, out_path, data_dir):
    """子进程: 只认 code 目录里的代码(-I 不把脚本目录放进 sys.path), 时钟换成可控的, 不读 .env。"""
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, code)
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    clock = [1000.0]
    time.monotonic = lambda: clock[0]
    tails = [0, 0]
    with open(out_path, "w", encoding="utf-8", buffering=1) as out:
        (run_crypto if market == "crypto" else run_futures)(clock, out, data_dir, tails)
        _write_files(out, data_dir)
    if sum(tails):
        print(f"整窗口 {tails[0]} 次, 只算尾部 {tails[1]} 次", flush=True)


def extract(base, target):
    """git 里 base 版本的仓库根目录各 .py -> target。"""
    names = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "--name-only", base], check=True,
                           capture_output=True, text=True).stdout.split()
    target.mkdir(parents=True)
    for name in names:
        if name.endswith(".py"):
            content = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{name}"], check=True,
                                     capture_output=True).stdout
            (target / name).write_bytes(content)


def lines(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="只算尾部的新旧对拍")
    parser.add_argument("--market", choices=["crypto", "futures", "all"], default="all")
    parser.add_argument("--base", default="HEAD", help="改动前的代码取 git 的哪个版本(默认 HEAD)")
    parser.add_argument("--keep", action="store_true", help="一致也保留工作目录")
    parser.add_argument("--child", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(*args.child)
        return 0
    markets = ["crypto", "futures"] if args.market == "all" else [args.market]
    work = Path(tempfile.mkdtemp(prefix="tail-check-"))
    extract(args.base, work / "old")
    print(f"旧代码: git {args.base}; 新代码: {ROOT}; 工作目录: {work}", flush=True)
    runs = []
    for market in markets:
        for side, code in (("old", work / "old"), ("new", ROOT)):
            data = work / f"{market}_{side}_data"
            data.mkdir()
            log = open(work / f"{market}_{side}.log", "w", encoding="utf-8")
            command = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--child",
                       market, str(code), str(work / f"{market}_{side}.txt"), str(data)]
            runs.append((market, side, subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=work), log))
    started = time.time()
    while any(process.poll() is None for _, _, process, _ in runs):
        time.sleep(30)
        done = ", ".join(f"{market}/{side} {len(lines(work / f'{market}_{side}.txt'))}/{ROUNDS[market]}"
                         for market, side, _, _ in runs)
        print(f"[{int(time.time() - started)}s] {done}", flush=True)
    ok = True
    for market in markets:
        failed = [side for m, side, process, _ in runs if m == market and process.returncode != 0]
        if failed:
            ok = False
            print(f"{market}: {'/'.join(failed)} 运行出错, 看 {work}/{market}_*.log")
            continue
        old, new = lines(work / f"{market}_old.txt"), lines(work / f"{market}_new.txt")
        stats = [line for line in lines(work / f"{market}_new.log") if line.startswith("整窗口")]
        if old == new:
            files = sum(line.startswith("file ") for line in new)
            print(f"{market}: 完全相同 —— {len(new) - files} 轮的推送、快照、窗口, 加上 {files} 个文件。"
                  f"新代码 {stats[-1] if stats else ''}")
        else:
            ok = False
            print(f"{market}: 不一致 (旧 {len(old)} 行, 新 {len(new)} 行), 最先不同的几轮:")
            shown = 0
            for index, (a, b) in enumerate(zip(old, new)):
                if a != b:
                    print(f"  旧 {a}\n  新 {b}")
                    shown += 1
                    if shown >= 5:
                        break
    if ok and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    elif not ok:
        print(f"工作目录保留在 {work}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
