"""FlowWave 的权威计算：行情快照、图表和警报共用结果与缺口恢复规则。"""
from __future__ import annotations

import math

VERSION = 1
CONTEXT_BARS = 800
CHECKPOINT = 64       # 增量计算只存最近这么多根和每隔这么多根一个的状态(见 FlowWaveSeries)

_A9, _A6 = 2 / (9 + 1), 2 / (6 + 1)    # EMA(9) / EMA(6) 的系数
_N, _SX, _SXX = 21, 210, 2870          # 21 根线性回归


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_price(bar):
    return (all(finite(bar.get(k)) for k in ("time", "open", "high", "low", "close"))
            and bar["low"] <= min(bar["open"], bar["close"])
            and bar["high"] >= max(bar["open"], bar["close"]))


def valid_bar(bar):
    return valid_price(bar) and finite(bar.get("volume")) and bar["volume"] >= 0


def _row(bar):
    """输入 bar -> (时间, 开, 高, 低, 收, 量或 None, 价格可用); 价格不可用时开高低收记 None, 便于逐根比较。"""
    ok = valid_price(bar) and bar.get("priceValid") is not False
    volume = bar.get("volume")
    if not (finite(volume) and volume >= 0 and bar.get("volumeValid") is not False and bar.get("valid") is not False):
        volume = None
    if not ok:
        return bar.get("time"), None, None, None, None, volume, False
    return bar["time"], bar["open"], bar["high"], bar["low"], bar["close"], volume, True


def _advance(seg, price, volume):
    """价格连续的一段里再走一根; 量缺失只影响 MFI 的 3 根窗口，不重置价格 EMA/RMA。

    seg 是这段到上一根为止的状态(None 表示这根是段首):
    (段内序号, 最近 3 个价, EMA9 中线, EMA9 偏离, EMA6 tci, RMA3 涨, RMA3 跌, 涨累计, 跌累计,
     最近 8 根的量(不含段首), 最近 20 个 wt1)
    """
    if seg is None:
        i, prices, wt1s, vols = 0, (), (), ()
        center = price
        distance = abs(price - center)
        tci = ru = rd = None
        total_up = total_down = 0.0 + 0.0
        deviation = price - center
    else:
        i, prices, center, distance, tci, ru, rd, total_up, total_down, vols, wt1s = seg
        i += 1
        center = _A9 * price + (1 - _A9) * center
        deviation = price - center
        distance = _A9 * abs(deviation) + (1 - _A9) * distance
    ratio = deviation / (0.025 * distance) if distance else None
    if ratio is not None:
        tci = ratio if tci is None else _A6 * ratio + (1 - _A6) * tci
    current_tci = tci if ratio is not None else None
    if i:
        diff = price - prices[-1]
        up, down = max(diff, 0), -min(diff, 0)
        if i < 3:
            total_up += up
            total_down += down
            if i == 2:
                ru, rd = total_up / 3, total_down / 3
        else:
            ru, rd = (up + 2 * ru) / 3, (down + 2 * rd) / 3
        vols = (vols + (volume,))[-8:]           # 段首的量用不到(MFI 与缺量判断都从第 2 根起)
    window = prices + (price,)
    wt1 = None
    if i >= 2 and current_tci is not None and ru is not None and rd is not None and not (ru == rd == 0):
        count = 3 if i >= 3 else 2
        flows = vols[-count:]
        if all(v is not None for v in flows):
            pos = neg = 0.0
            for k in range(count):
                cur, prev = window[k - count], window[k - count - 1]
                money = cur * flows[k]
                if cur > prev:
                    pos += money
                elif cur < prev:
                    neg += money
            mf = 100 - 100 / (1 + (pos / neg if neg else 1e10))
            rsi = 100 if rd == 0 else 100 - 100 / (1 + ru / rd)
            wt1 = (current_tci + 50 + mf + rsi) / 3
    recent = wt1s + (wt1,)
    last6 = recent[-6:]
    wt2 = sum(last6) / 6 if i >= 5 and all(v is not None for v in last6) else None
    wave = None
    if i >= _N - 1 and all(v is not None for v in recent[-_N:]):
        span = recent[-_N:]
        sy = sum(span)
        sxy = sum(j * v for j, v in enumerate(span))
        slope = (_N * sxy - _SX * sy) / (_N * _SXX - _SX * _SX)
        wave = (sy - slope * _SX) / _N + slope * (_N - 1)
    reason = None if wt2 is not None else "volume" if any(v is None for v in vols) else "warming"
    seg = (i, window[-3:], center, distance, tci, ru, rd, total_up, total_down, vols, recent[-(_N - 1):])
    return seg, {"version": VERSION, "wt2": wt2, "wave": wave, "reason": reason}


def _step(state, row, tf, continuous):
    """state = (已走根数, 上一根时间, 段状态) 或 None(从头开始) -> (新状态, 这根的结果)。

    价格缺失/加密时间断档才分段；正常休市不分段。
    """
    seq, previous, seg = state if state is not None else (0, None, None)
    time, _, high, low, close, volume, ok = row
    if not ok or (continuous and tf and previous is not None and time != previous + tf):
        seg = None
    if not ok:
        return (seq + 1, time, None), {"version": VERSION, "wt2": None, "wave": None, "reason": "price"}
    seg, value = _advance(seg, (high + low + close) / 3, volume)
    return (seq + 1, time, seg), value


def calculate_flowwave(bars, *, tf=None, continuous=False):
    """价格缺失/加密时间断档才分段；正常休市不分段。仅量缺失保留价格状态。

    零成交量是有效值。单根缺量使当根及后 7 根 WT2 不可用，随后自然恢复；
    价格重新开始后按公式预热，不另设 100 根门槛。回填重算同一份历史即可恢复。
    priceValid/volumeValid 由行情层区分 OHLCV 与买卖量的覆盖，避免误伤期货。
    """
    state, output = None, []
    for bar in bars:
        state, value = _step(state, _row(bar), tf, continuous)
        output.append(value)
    return output


def calculate_wt2(bars, *, tf=None, continuous=False):
    return [value["wt2"] for value in calculate_flowwave(bars, tf=tf, continuous=continuous)]


class FlowWaveSeries:
    """实时窗口的增量计算: 与上一轮逐根比输入, 从第一根改动的接着前一根的状态往后算。

    每根的结果只取决于它及以前的输入, 所以窗口滑动时留下的各根原样沿用(同一个 dict), 也不会因为
    滑到窗口开头而丢掉预热; 平时每轮只算最后一两根。状态只存最近 CHECKPOINT 根和每隔 CHECKPOINT
    根一个, 中间要用时从最近的存档往后重放。只在所属行情线程里用。
    """

    def __init__(self, tf=None, continuous=False):
        self.tf, self.continuous = tf, continuous
        self._rows = []        # 各根输入(_row)
        self._values = []      # 各根结果
        self._states = []      # 各根算完后的状态, 不存的为 None
        self._before = None    # 窗口第一根之前的状态(None = 从头开始)
        self._index = {}       # 时间 -> 下标

    def _state_after(self, index):
        """上一轮窗口第 index 根算完后的状态(-1 是窗口之前); 没存就从最近的存档重放。"""
        start = index
        while start >= 0 and self._states[start] is None:
            start -= 1
        state = self._states[start] if start >= 0 else self._before
        for row in self._rows[start + 1:index + 1]:
            state, _ = _step(state, row, self.tf, self.continuous)
        return state

    def update(self, bars, context=None):
        """本轮窗口(按时间升序) -> 各根结果。

        context() 给出窗口之前的若干根, 只在接不上上一轮(第一次算、窗口跳开)时用来预热窗口开头。
        """
        rows = [_row(bar) for bar in bars]
        offset = self._index.get(rows[0][0]) if rows else None
        if offset is None:
            before = None
            for row in map(_row, context() if context is not None and rows else []):
                before, _ = _step(before, row, self.tf, self.continuous)
            start, state, values, states = 0, before, [], []
        else:
            limit = min(len(rows), len(self._rows) - offset)
            start = 0
            while start < limit and rows[start] == self._rows[offset + start]:
                start += 1
            before = self._state_after(offset - 1)
            state = self._state_after(offset + start - 1) if start else before
            values = self._values[offset:offset + start]
            states = self._states[offset:offset + start]
        for row in rows[start:]:
            state, value = _step(state, row, self.tf, self.continuous)
            states.append(state)
            values.append(value)
        # 移出最近 CHECKPOINT 根的状态只留存档(已走根数是 CHECKPOINT 的倍数的)
        recent = len(states) - CHECKPOINT
        old_recent = len(self._rows) - CHECKPOINT - (offset or 0)
        for i in range(max(0, min(start, old_recent)), recent):
            if states[i] is not None and states[i][0] % CHECKPOINT:
                states[i] = None
        self._rows, self._values, self._states, self._before = rows, values, states, before
        self._index = {row[0]: i for i, row in enumerate(rows)}
        return values
