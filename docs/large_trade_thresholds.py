# -*- coding: utf-8 -*-
"""重新估算各品种的大额成交金额门槛(large_trade_defaults.py 里的初始值).

门槛取"一个事件的成交金额"的 P99, 保留两位有效数字。事件的口径与检测器(large_trades.py)一致:

- **期货**: 事件 = 相邻两个快照(序号连续)之间的成交量, 金额 = 手数 x 合约乘数 x 价格。
  用 TqSdk 取每个主连最近 1 万个快照(活跃品种约一个半小时)。开盘(北京时间 21:00、09:00、09:30、
  13:00、13:30)后 15 分钟内的事件不计 —— 开盘那阵放量会把门槛抬得过高, 平时几乎不出点;
  换交易日的首笔(含集合竞价)也不算。
- **加密**: 事件 = 同一毫秒、同一方向、编号连续的成交合并成的一笔(近似一张吃单; 币安 aggTrade 按价位拆、
  OKX trades-all 按对手单拆), 金额 = 价格 x 币数量。用交易所某一天的公开归档包。

用法::

    python docs/large_trade_thresholds.py futures            # 全部期货主连
    python docs/large_trade_thresholds.py crypto 2026-10-14 BINANCE.BTCUSDT.P OKX.BTC-USDT-SWAP

输出可直接粘进 large_trade_defaults.py 的字典条目。期货凭据取自 .env 的 ``TQ_USER``/``TQ_PASS``。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH = 6   # 天勤不释放已订阅的序列, 分批订阅、每批重连, 免得内存吃满
NS = 1_000_000_000
SESSION_OPENS = [(21, 0), (9, 0), (9, 30), (13, 0), (13, 30)]   # 北京时间; 10:30 小节休息后复盘不算
OPEN_SKIP_NS = 15 * 60 * NS     # 开盘后这么久的事件不计


def round2(value: float) -> int:
    """两位有效数字。"""
    digits = int(math.floor(math.log10(value))) - 1
    return int(round(value, -digits))


def futures_events(ticks: pd.DataFrame) -> np.ndarray:
    """快照 -> 计入样本的事件手数: 序号连续、量为正、不在开盘后 OPEN_SKIP_NS 内。

    开盘按北京时间认(夜盘 21:00、日盘 09:00、股指与国债 09:30、午后 13:00 / 13:30): 只看快照间隔的话,
    窗口恰好从开盘开始时前面没有空档, 开盘那阵就漏剔了。"""
    t = ticks[(ticks.datetime > 0) & ticks.volume.notna()]
    times = t.datetime.to_numpy(np.int64)
    dv = np.diff(t.volume.to_numpy(float))
    clock = (times // NS + 8 * 3600) % 86400
    opening = np.zeros(len(times), dtype=bool)
    for hour, minute in SESSION_OPENS:
        since = clock - (hour * 3600 + minute * 60)
        opening |= (since >= 0) & (since < OPEN_SKIP_NS // NS)
    return dv[(np.diff(t.id.to_numpy()) == 1) & (dv > 0) & ~opening[1:]]


def futures_p99(ticks: pd.DataFrame, multiple: float) -> tuple[float, int] | None:
    qty = futures_events(ticks)
    if len(qty) < 200:
        return None
    price = float(np.nanmedian(ticks.last_price.to_numpy()))
    return float(np.quantile(qty, .99)) * price * multiple, len(qty)


def futures() -> dict[str, int]:
    from dotenv import load_dotenv
    from tqsdk import TqApi, TqAuth
    load_dotenv(os.path.join(ROOT, ".env"))
    auth = TqAuth(os.getenv("TQ_USER"), os.getenv("TQ_PASS"))
    api = TqApi(auth=auth)
    try:
        symbols = sorted(s for s in api.query_quotes(ins_class="CONT", expired=False) if s.startswith("KQ.m@"))
    finally:
        api.close()
    result = {}
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        api = TqApi(auth=auth)
        try:
            quotes, ticks = {}, {}
            for s in batch:
                try:
                    quotes[s] = api.get_quote(s)
                    ticks[s] = api.get_tick_serial(s, data_length=10000)
                except Exception as exc:     # 个别冷门合约取 tick 超时: 跳过, 沿用原值
                    print(f"# {s} 跳过: {type(exc).__name__}", file=sys.stderr, flush=True)
            deadline = time.time() + 45
            while time.time() < deadline:
                api.wait_update(deadline=time.time() + 5)
                if all(len(t) and (t.datetime > 0).sum() >= len(t) - 20 for t in ticks.values()):
                    break
            for s in ticks:
                stats = futures_p99(ticks[s].copy(), float(quotes[s].volume_multiple))
                if stats is not None:
                    result[s.removeprefix("KQ.m@")] = round2(stats[0])
        finally:
            api.close()
        print(f"# {i + len(batch)}/{len(symbols)}", file=sys.stderr, flush=True)
    return result


def fetch(url: str) -> bytes:
    # OKX 接口拒绝 urllib 默认的 User-Agent
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlowScope"})
    return urllib.request.urlopen(request, timeout=120).read()


def archive(symbol: str, day: str) -> np.ndarray:
    """某天每个事件(同毫秒同向连续成交合并后)的金额(USDT)。归档包只当数据读。"""
    venue, rest = symbol.split(".", 1)
    if venue == "BINANCE":
        pair = rest.removesuffix(".P")
        url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{pair}/{pair}-aggTrades-{day}.zip"
        columns, ct_val = ["agg_trade_id", "price", "quantity", "transact_time", "is_buyer_maker"], 1.0
    else:
        url = (f"https://static.okx.com/cdn/okex/traderecords/trades/daily/{day.replace('-', '')}/"
               f"{rest}-trades-{day}.zip")
        columns = ["trade_id", "price", "size", "created_time", "side"]
        # OKX 的量按张计, 乘合约面值折成币
        info = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={rest}"
        ct_val = float(json.loads(fetch(info))["data"][0]["ctVal"])
    with zipfile.ZipFile(io.BytesIO(fetch(url))) as z:
        frame = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), usecols=columns)[columns]
    frame = frame.sort_values(columns[0])
    ids, ms, side = (frame[c].to_numpy() for c in (columns[0], columns[3], columns[4]))
    notional = frame[columns[1]].to_numpy(float) * frame[columns[2]].to_numpy(float) * ct_val
    joined = np.r_[False, (np.diff(ids) == 1) & (np.diff(ms) == 0) & (side[1:] == side[:-1])]
    return np.bincount(np.cumsum(~joined) - 1, weights=notional)


def crypto(day: str, symbols: list[str]) -> dict[str, int]:
    return {s: round2(float(np.quantile(archive(s, day), .99))) for s in symbols}


if __name__ == "__main__":
    if sys.argv[1:2] == ["futures"]:
        table = futures()
    elif sys.argv[1:2] == ["crypto"] and len(sys.argv) > 3:
        table = crypto(sys.argv[2], sys.argv[3:])
    else:
        sys.exit(__doc__)
    for key, value in sorted(table.items()):
        print(f'    "{key}": {value:_},')
