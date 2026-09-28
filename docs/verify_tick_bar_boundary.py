# -*- coding: utf-8 -*-
"""核对 tick -> bar 的边界归属: 快照区间量按哪种口径分桶, 才与 TqSdk K 线逐根相等。

背景: 早先按 datetime // bar_ns 分桶(左闭右开 [start, end)), 实时数据里只有约 4.5%
的 bar 与 K 线成交量严格相等; 偏差在相邻 bar 之间一正一负(lag-1 自相关约 -0.47,
"每根 bar 有一个边界快照被算进邻居"的理论值是 -0.5)。本脚本直接拿原始 tick 验证归属规则:

  [start,end)   早先的实现
  (start,end]   恰好落在边界上的快照归前一根(现行实现, 见 indicator.tick_bar_start)
  其余          边界整体平移若干毫秒, 用来排除时间戳偏移

每种口径同时比较成交量与开高低收, 最后再用线上的 indicator.build_bars 判一遍覆盖。
2026-09-28 七个交易所主连的结论: (start,end] 的量与收盘价 100% 相等; 开盘价在任何口径下
都只对上一半左右(K 线开盘价不是首个快照的最新价), 覆盖判定只用量, 不受影响。

注意 TqSdk 序列的 datetime 是 float64, 纳秒时间戳在这个量级上的分辨率只有 256ns,
"减 1 纳秒"在 float 上不生效, 所以这里一律先转 int64 再分桶。

一次性研究脚本: 只读行情, 不写仓库数据。会单独登录一次 TqSdk(读 .env),
请先停掉 FlowScope 服务, 避免同一账号同时两个连接:
    $env:PYTHONIOENCODING = "utf-8"
    .\\.venv\\Scripts\\python.exe .\\docs\\verify_tick_bar_boundary.py [合约 ...]
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from tqsdk import TqApi, TqAuth

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from indicator import TZ_SHIFT_S, build_bars  # noqa: E402
# 每个交易所各取一个活跃主连: 时间戳精度与撮合时段各不相同
DEFAULT_SYMBOLS = ["KQ.m@SHFE.fu", "KQ.m@SHFE.rb", "KQ.m@DCE.m", "KQ.m@CZCE.MA",
                   "KQ.m@INE.sc", "KQ.m@GFEX.lc", "KQ.m@CFFEX.IF"]
PERIODS = (10, 30)
MS = 10**6
SHIFTS = [("[start,end)", 0), ("(start,end]", 1), ("-500ms", -500 * MS),
          ("+250ms", 250 * MS), ("+500ms", 500 * MS), ("+1s", 1000 * MS)]
TICKS = 10000
KLINES = 1500


def tick_frame(ticks: pd.DataFrame) -> pd.DataFrame:
    """原始 tick -> (datetime int64, last_price, dv); 首条没有前一快照, 区间量未知, 丢弃。"""
    df = ticks[["datetime", "last_price", "volume"]].dropna()
    df = df[(df.datetime > 0) & np.isfinite(df.last_price) & (df.volume >= 0)].copy()
    df["datetime"] = df["datetime"].astype("int64")
    dv = df["volume"].diff()
    roll = dv < 0                      # 跨交易日累计量清零
    dv[roll] = df["volume"][roll]
    df["dv"] = dv
    return df.iloc[1:].reset_index(drop=True)


def kline_frame(klines: pd.DataFrame) -> pd.DataFrame:
    k = klines[["datetime", "open", "high", "low", "close", "volume"]].dropna().copy()
    k = k[k.datetime > 0]
    k["datetime"] = k["datetime"].astype("int64")
    return k.set_index("datetime")


def compare(ticks: pd.DataFrame, klines: pd.DataFrame, width: int, shift: int) -> dict:
    """按 shift 口径分桶, 与 K 线逐根比较; 只取两端各留一根余量的完整覆盖区间。"""
    start = ((ticks["datetime"] - shift) // width) * width
    grouped = ticks.groupby(start)
    traded = ticks[ticks["dv"] > 0]
    traded_grouped = traded.groupby(((traded["datetime"] - shift) // width) * width)
    agg = pd.DataFrame({
        "vol": grouped["dv"].sum(),
        "close": grouped["last_price"].last(),
        "open": traded_grouped["last_price"].first(),
        "high": traded_grouped["last_price"].max(),
        "low": traded_grouped["last_price"].min(),
    })
    lo = int(ticks["datetime"].iloc[0]) + 2 * width
    hi = int(ticks["datetime"].iloc[-1]) - 2 * width
    k = klines[(klines.index >= lo) & (klines.index <= hi)]
    if k.empty:
        return {"bars": 0}
    joined = k.join(agg, rsuffix="_t", how="left")
    joined["vol_t"] = joined["vol_t"].fillna(0.0) if "vol_t" in joined else joined["vol"]
    dev = joined["vol_t"] - joined["volume"]

    def same(column):
        left, right = joined[column], joined[f"{column}_t"]
        valid = right.notna()
        return float(np.isclose(left[valid], right[valid], rtol=0, atol=1e-9).mean()) if valid.any() else float("nan")

    return {
        "bars": len(joined),
        "vol_exact": float((dev.abs() < 0.5).mean()),
        "dev_median": float((dev.abs() / joined["volume"].where(joined["volume"] > 0)).median()),
        "close": same("close"), "open": same("open"), "high": same("high"), "low": same("low"),
    }


def production_coverage(raw_ticks: pd.DataFrame, raw_klines: pd.DataFrame, width: int) -> dict:
    """用线上同一条路径(indicator.build_bars)判覆盖, 只统计 tick 窗口内部的 bar。"""
    bars = build_bars(raw_klines, raw_ticks, 0, bar_ns=width)
    t = raw_ticks["datetime"].dropna()
    t = t[t > 0].astype("int64")
    start = (bars["time"] - TZ_SHIFT_S) * 10**9
    inside = bars[(start >= int(t.iloc[0]) + 2 * width) & (start <= int(t.iloc[-1]) - 2 * width)]
    return inside["coverage"].value_counts().to_dict()


def wait_ready(api: TqApi, serials: list[pd.DataFrame], timeout: float = 60.0):
    """闭市时初始回填不一定触发 wait_update 返回, 用短 deadline 轮询到末行有数据。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        api.wait_update(deadline=time.time() + 1)
        if all(not np.isnan(serial["datetime"].iloc[-1]) for serial in serials):
            api.wait_update(deadline=time.time() + 1)
            return True
    return False


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
    if not user or not password:
        print("未设置 TQ_USER/TQ_PASS")
        return 1
    symbols = sys.argv[1:] or DEFAULT_SYMBOLS
    api = TqApi(auth=TqAuth(user, password))
    try:
        serials = {}
        for symbol in symbols:
            serials[symbol] = (api.get_tick_serial(symbol, data_length=TICKS),
                               {p: api.get_kline_serial(symbol, p, data_length=KLINES) for p in PERIODS})
        wait_ready(api, [s for t, ks in serials.values() for s in (t, *ks.values())])
        snapshot = {symbol: (tick.copy(), {p: k.copy() for p, k in ks.items()})
                    for symbol, (tick, ks) in serials.items()}
    finally:
        api.close()

    summary = []
    for symbol, (raw_ticks, raw_klines) in snapshot.items():
        ticks = tick_frame(raw_ticks)
        if ticks.empty:
            print(f"\n[{symbol}] 没有 tick, 跳过")
            continue
        t = ticks["datetime"]
        span = (t.iloc[-1] - t.iloc[0]) / 1e9 / 60
        on_half = float((t % (500 * MS) == 0).mean())
        print(f"\n[{symbol}] tick {len(ticks)} 条, 跨度 {span:.0f} 分钟, "
              f"时间戳恰为 500ms 整数倍 {on_half:.1%}, 同一时间戳重复 {int(t.duplicated().sum())} 条")
        for period in PERIODS:
            width = period * 10**9
            klines = kline_frame(raw_klines[period])
            on_edge = int((t % width == 0).sum())
            print(f"  {period}s: 恰好落在 bar 边界上的 tick {on_edge} 条")
            print(f"    {'口径':<12} {'bar':>5} {'量相等':>8} {'偏差中位':>9} "
                  f"{'收':>7} {'开':>7} {'高':>7} {'低':>7}")
            best = None
            for label, shift in SHIFTS:
                row = compare(ticks, klines, width, shift)
                if not row["bars"]:
                    print(f"    {label:<12} 无完整覆盖的 bar")
                    continue
                print(f"    {label:<12} {row['bars']:>5} {row['vol_exact']:>8.1%} {row['dev_median']:>9.2%} "
                      f"{row['close']:>7.1%} {row['open']:>7.1%} {row['high']:>7.1%} {row['low']:>7.1%}")
                if best is None or row["vol_exact"] > best[1]["vol_exact"]:
                    best = (label, row)
            if best:
                covered = production_coverage(raw_ticks, raw_klines[period], width)
                summary.append((symbol, period, best[0], best[1]["vol_exact"], best[1]["bars"], covered))

    print("\n==== 各合约量相等比例最高的口径 / 线上 build_bars 的覆盖判定 ====")
    for symbol, period, label, exact, bars, covered in summary:
        total = sum(covered.values()) or 1
        print(f"  {symbol:<16} {period:>2}s  {label:<12} {exact:.1%}  ({bars} 根)   "
              f"complete {covered.get('complete', 0) / total:.1%}  {covered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
