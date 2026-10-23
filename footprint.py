"""真实分价成交量的合并、周期汇总与显示分档；不从 OHLC 推算成交。

显示分档只定一个底档：前端按屏幕上的行高再把相邻档合并成底档的整数倍(放大变细、缩小变粗)。
"""
from __future__ import annotations

import bisect
import math
from decimal import Decimal
from functools import reduce

from indicator import VOLUME_ROUND

MIN_TF = 30
CACHE_BARS = 800
BASE_ROWS = 32   # 底档让中位跨度的一根大约有这么多行(1-2-5 进位后 13~32 行), 给前端留出合并余地


def common_tick(steps):
    steps = [Decimal(str(step)) for step in steps
             if step is not None and math.isfinite(step) and step > 0]
    if not steps:
        return None
    scale = 10 ** max(0, -min(step.as_tuple().exponent for step in steps))
    return reduce(math.gcd, (int(step * scale) for step in steps)) / scale


def _levels(totals):
    return [[price, *(round(v, VOLUME_ROUND) for v in totals[price])] for price in sorted(totals)]


def _add(totals, levels):
    for price, buy, sell, *unknown in levels:
        row = totals.setdefault(price, [0.0, 0.0, 0.0])
        for i, value in enumerate((buy, sell, unknown[0] if unknown else 0.0)):
            row[i] += value


def _merged(rows, cached):
    # 上游的价位列表发布后不再就地修改；未变化的历史行可以直接复用。
    if cached is not None and len(rows) == len(cached[0]) and all(
            old is new for old, new in zip(cached[0], rows)):
        return cached
    totals = {}
    for levels in rows:
        _add(totals, levels)
    levels = _levels(totals)
    return rows, levels, sum(sum(level[1:]) for level in levels)


def combine(sources, cache=None):
    """各所同一时刻、同一实际价格的量相加；缺所或任一所不完整时只标 partial。"""
    parts = [{bar["time"]: bar for bar in (source or {}).get("bars", [])} for source in sources]
    cache = {} if cache is None else cache
    kept = {}
    bars = []
    for t in sorted({t for part in parts for t in part})[-CACHE_BARS:]:
        items = [part.get(t) for part in parts]
        complete = all(bar is not None and bar["coverage"] == "complete" for bar in items)
        kept[t] = _merged([bar["levels"] for bar in items if bar is not None], cache.get(t))
        bars.append({"time": t, "levels": kept[t][1],
                     "coverage": "complete" if complete else "partial"})
    cache.clear()
    cache.update(kept)
    return {"tickSize": common_tick((source or {}).get("tickSize") for source in sources), "bars": bars}


def rollup(source, candles, tf, cache=None):
    """按主图的真实 K 线起点汇总 30s 足迹；用主图总量核对覆盖，跨休市不串桶。"""
    starts = [bar["time"] for bar in candles]
    buckets = {}
    cache = {} if cache is None else cache
    kept = {}
    for bar in source.get("bars", []):
        i = bisect.bisect_right(starts, bar["time"]) - 1
        if i < 0 or bar["time"] >= starts[i] + tf:
            continue
        buckets.setdefault(i, []).append(bar)
    bars = []
    for i, items in sorted(buckets.items()):
        kept[starts[i]] = _merged([bar["levels"] for bar in items], cache.get(starts[i]))
        _, levels, known = kept[starts[i]]
        complete = all(bar["coverage"] == "complete" for bar in items)
        volume = candles[i].get("volume")
        complete &= (volume is not None and math.isfinite(volume)
                     and math.isclose(known, volume, rel_tol=1e-8, abs_tol=10 ** -VOLUME_ROUND))
        bars.append({"time": starts[i], "levels": levels,
                     "coverage": "complete" if complete else "partial"})
    cache.clear()
    cache.update(kept)
    return {"tickSize": source.get("tickSize"), "bars": bars[-CACHE_BARS:]}


def for_display(raw, cache=None):
    """期货与加密都用最小价位 1-2-5 倍的统一网格；保留 raw，切周期/合所时不会累积舍入误差。

    底档按全部足迹单根价格跨度的中位数定，一根插针不会把整段拉粗；priceTick 给前端换算格子边界与再合并。
    """
    tick = raw.get("tickSize")
    cache = {} if cache is None else cache
    # 跨度先换成整数 tick 再除，避免 6.4000000000000006 这类浮点误差多进一档
    spans = sorted(round((bar["levels"][-1][0] - bar["levels"][0][0]) / tick)
                   for bar in raw["bars"] if tick and len(bar["levels"]) > 1)
    if not spans:
        cache.clear()
        return {**raw, "priceTick": tick}
    ticks = max(1, -(-spans[len(spans) // 2] // BASE_ROWS))
    magnitude = 10 ** int(math.floor(math.log10(ticks)))
    multiple = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= ticks)
    if multiple == 1:
        cache.clear()
        return {**raw, "priceTick": tick}
    step = float(Decimal(str(tick)) * multiple)
    kept = {}
    bars = []
    for bar in raw["bars"]:
        cached = cache.get(bar["time"])
        if cached is not None and cached[0] is bar["levels"] and cached[1] == step:
            levels = cached[2]
        else:
            totals = {}
            for price, *volumes in bar["levels"]:
                # 先还原整数 tick，避免 100.1 / 0.1 的浮点误差把成交划进相邻档；档位标在本档最低价。
                index = round(price / tick) // multiple
                price = float(Decimal(str(step)) * index)
                _add(totals, [[price, *volumes]])
            levels = _levels(totals)
        kept[bar["time"]] = bar["levels"], step, levels
        bars.append({**bar, "levels": levels})
    cache.clear()
    cache.update(kept)
    return {**raw, "tickSize": step, "priceTick": tick, "bars": bars}


def message(data, previous):
    """初次、删点或步长改变时整份替换，其余只推送实际改变的 bar。"""
    removed = previous and ({bar["time"] for bar in previous["bars"]}
                            - {bar["time"] for bar in data["bars"]})
    if (previous is None or data["tickSize"] != previous["tickSize"]
            or data.get("priceTick") != previous.get("priceTick") or removed):
        return {"type": "footprint_snapshot", **data}
    old = {bar["time"]: bar for bar in previous["bars"]}
    changed = [bar for bar in data["bars"] if old.get(bar["time"]) != bar]
    return {"type": "footprints", **data, "bars": changed} if changed else None
