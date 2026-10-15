"""先筛原始成交事件, 再按图表周期汇总。统计只使用当前事件之前的样本。

期货输入快照区间量; 加密输入交易所逐笔/聚合成交。只保留本次采集的检测结果,
不从 OHLCV/买卖量历史反推大额事件。不同图表周期应共用一个 Detector。
"""
from bisect import bisect_left, insort
from collections import deque
from functools import lru_cache
import json
import math
import os

from indicator import GAP_NS, TZ_SHIFT_S

NS = 1_000_000_000
SOURCE_LABELS = {"snapshot": "期货快照估算", "trade": "交易所逐笔", "aggregate": "交易所聚合成交"}


def default_settings(source):
    return {"window": 2000, "warmup": 200, "quantile": 0.99, "minRatio": 1.5,
            "minQty": 20.0 if source == "snapshot" else 0.0,
            "minNotional": 0.0 if source == "snapshot" else 10000.0}


@lru_cache(maxsize=4)
def _overrides(text):
    """LARGE_TRADE_SETTINGS 的 JSON; 按原文缓存, 每个 Feed 建检测器时不必重复解析。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LARGE_TRADE_SETTINGS 不是合法 JSON: {exc}") from None
    if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
        raise ValueError('LARGE_TRADE_SETTINGS 须是 {"合约代码": {"参数": 值}}')
    return value


def settings_for(symbol, source):
    settings = default_settings(source)
    # 可按完整合约代码覆盖, '*' 为全部合约的公共覆盖; 非法配置明确报错。留空等于不覆盖。
    overrides = _overrides(os.getenv("LARGE_TRADE_SETTINGS", "").strip() or "{}")
    for key in ("*", symbol):
        settings.update({k: v for k, v in overrides.get(key, {}).items() if k in settings})
    for key in ("window", "warmup", "quantile", "minRatio", "minQty", "minNotional"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"大额成交 {key} 必须是有限数值")
        if key in ("window", "warmup"):
            if not float(value).is_integer():
                raise ValueError(f"大额成交 {key} 必须是整数")
            settings[key] = int(value)
        else:
            settings[key] = float(value)
    if not (2 <= settings["warmup"] <= settings["window"] <= 10000 and
            0 < settings["quantile"] < 1 and settings["minRatio"] >= 1 and
            settings["minQty"] >= 0 and settings["minNotional"] >= 0):
        raise ValueError("大额成交参数超出范围")
    return settings


_warned = set()


def _settings_or_default(symbol, source):
    """可选配置写错只让该合约退回默认门槛并提示一次, 不能让建 Feed 失败、拖垮整条采集。"""
    try:
        return settings_for(symbol, source)
    except ValueError as exc:
        if str(exc) not in _warned:
            _warned.add(str(exc))
            print(f"[large] {exc}; {symbol} 等合约按默认参数检测大额成交", flush=True)
        return default_settings(source)


class LargeTradeDetector:
    def __init__(self, symbol, source="snapshot", settings=None):
        self.symbol, self.source = symbol, source
        self.settings = _settings_or_default(symbol, source) if settings is None else settings
        # key -> (时间ns, 价格, 量, 方向, 重置基线, 可用, 与上一条之间没丢数据)
        self.raw = {}
        self.events = {}       # key -> 已筛选事件; 不随图表窗口移动清零
        self.readiness = {}    # key -> (时间ns, 已判定, 与上一条之间没丢数据)
        self.base_delta = 0.0
        self.since = None
        self.version = 0       # 每次有记录变化加一, 供按版本复用汇总结果
        self._last_crypto = None
        self._cache = None
        self._window = deque()
        self._sorted = []
        self._sum = self._sum2 = 0.0
        self._spans = None

    def config(self):
        return {**self.settings, "source": self.source, "label": SOURCE_LABELS[self.source],
                "symbol": self.symbol, "history": "本次采集; 旧K线历史不回推"}

    def _clear_window(self):
        self._window.clear()
        self._sorted.clear()
        self._sum = self._sum2 = 0.0

    def _push(self, q):
        if len(self._window) == self.settings["window"]:
            old = self._window.popleft()
            self._sorted.pop(bisect_left(self._sorted, old))
            self._sum -= old
            self._sum2 -= old * old
        self._window.append(q)
        insort(self._sorted, q)
        self._sum += q
        self._sum2 += q * q

    def _process(self, key, row):
        ns, price, qty, side, reset, usable, joined = row
        if reset:
            self._clear_window()
        ready = usable and len(self._window) >= self.settings["warmup"]
        self.readiness[key] = (ns, ready, joined)
        if not usable or qty <= 0:
            return
        if ready:
            n = len(self._window)
            mean = self._sum / n
            sd = math.sqrt(max(0.0, self._sum2 / n - mean * mean))
            pos = (n - 1) * self.settings["quantile"]
            lo, hi = math.floor(pos), math.ceil(pos)
            threshold = self._sorted[lo] + (self._sorted[hi] - self._sorted[lo]) * (pos - lo)
            ratio = qty / mean if mean else 0.0
            z = (qty - mean) / sd if sd > 0 else None
            notional = price * qty
            if (qty > threshold and ratio >= self.settings["minRatio"] and
                    qty >= self.settings["minQty"] and notional >= self.settings["minNotional"]):
                self.events[key] = {"ns": ns, "price": price, "qty": qty, "side": side,
                                    "ratio": ratio, "z": z, "threshold": threshold}
                self._cache = None
        self._push(qty)

    def update(self, rows):
        """已按 key 排序的原始记录。重复不计, 修订从该记录起重放, 保留此前的统计基线。"""
        changes = [(key, row) for key, row in rows if self.raw.get(key) != row]
        if self.raw:
            first = next(iter(self.raw))
            changes = [(key, row) for key, row in changes if key >= first]
        if not changes:
            return
        old_last = next(reversed(self.raw)) if self.raw else None
        earliest = min(key for key, _ in changes)
        self.raw.update(changes)
        if self.since is None:
            self.since = min(row[0] for _, row in changes)
        replay = old_last is not None and earliest <= old_last
        if replay:
            self.raw = dict(sorted(self.raw.items()))
            self._clear_window()
            # 重建修订之前的基线; 不修改此前已确定的大额事件。
            for key, row in self.raw.items():
                if key >= earliest:
                    break
                if row[4]:
                    self._clear_window()
                if row[5] and row[2] > 0:
                    self._push(row[2])
            self.events = {key: value for key, value in self.events.items() if key < earliest}
            self._cache = None
        tail = ((key, row) for key, row in self.raw.items() if key >= earliest) if replay else changes
        for key, row in tail:
            self._process(key, row)
        # 保留足够的修订前置基线, 同时限制原始样本占用。
        limit = 20000 + self.settings["window"]
        if len(self.raw) > limit + 1024:
            keep = list(self.raw)[-limit:]
            self.raw = {key: self.raw[key] for key in keep}
            self.readiness = {key: self.readiness[key] for key in keep}
        if len(self.events) > 20000:
            removed = list(self.events)[:-20000]
            for key in removed:
                event = self.events.pop(key)
                self.base_delta += event["qty"] * event["side"]
            self._cache = None
        self._spans = None
        self.version += 1

    def sync_futures(self, frame, changed_ns=None, initial=False):
        if frame is None or len(frame) < 2:
            return
        edge = changed_ns or 0
        if initial and self.raw:
            edge = max(edge, next(reversed(self.raw.values()))[0])
        if edge:
            # 增量推送只读取变化段及一条前置快照, 不逐行扫描整个 SDK 窗口。
            frame = frame.iloc[max(0, int(frame["datetime"].searchsorted(edge)) - 1):]
        rows = []
        previous = None
        previous_id = None
        # _key 有序号时标识独立 tick; 无序号数据回退到时间戳, 不能据此检查漏号。
        identities = (frame["_key"] if "_key" in frame and frame.attrs.get("key_column") != "datetime"
                      else [None] * len(frame))
        last_key = next(reversed(self.raw)) if self.raw else None
        for identity, row in zip(identities, frame.itertuples()):
            ns = int(row.datetime)
            key = int(identity) if identity is not None else ns
            if previous is None and (last_key is None or key > last_key):
                # 新窗口与已采集数据不重叠, 第一条只能作为累计量的起点。
                rows.append((key, (ns, float(row.last_price), 0.0, 0, True, False, False)))
            if previous is not None and ns >= edge:
                # 天勤只在行情变化时推快照, 序号连续时再久的间隔也只是没成交(含午休、收盘), 区间量照常可用;
                # 无序号时才用 GAP_NS 推断断线, 与判向的断档口径一致。
                if identity is not None and previous_id is not None:
                    joined = identity == previous_id + 1
                else:
                    joined = ns - int(previous.datetime) <= GAP_NS
                # 累计量回落是换交易日(首笔含集合竞价), 本条量不可用并重新预热。
                reset = row.volume < previous.volume or not joined
                qty = float(row.volume - previous.volume)
                rows.append((key, (ns, float(row.last_price), max(qty, 0.0), int(row.side_lr),
                                   reset, not reset, joined)))
            previous = row
            previous_id = identity
        self.update(rows)

    def trade(self, identity, price, qty, ms, sell):
        identity, ns = int(identity), int(ms) * 1_000_000
        if self._last_crypto is not None and identity <= self._last_crypto[0]:
            return
        joined = (self._last_crypto is not None and identity == self._last_crypto[0] + 1 and
                  ns >= self._last_crypto[1])
        # 坏价格/量的一笔也要占住编号, 否则下一笔会被当成漏号而重新预热; 本身记为不可判定。
        valid = math.isfinite(price) and math.isfinite(qty) and price > 0 and qty >= 0
        self.update([(identity, (ns, float(price) if valid else 0.0, float(qty) if valid else 0.0,
                                 -1 if sell else 1, not joined, valid, joined))])
        self._last_crypto = (identity, ns)

    def event_rows(self):
        if self._cache is None:
            self._cache = sorted(self.events.values(), key=lambda e: e["ns"])
        return self._cache

    def spans(self):
        """已判定数据覆盖的时间段 [(起, 止)], 按时间排序。

        未判定的记录切断区间; 它与上一条之间没丢数据时, 区间延伸到它前 1ns:
        收盘前最后一笔之后到下一笔之前确实没有成交, 收盘那根 K 线不应一直算作部分覆盖。
        """
        if self._spans is None:
            spans = []
            open_span = False
            for key in sorted(self.readiness):
                ns, ready, joined = self.readiness[key]
                if ready and open_span and joined:
                    spans[-1][1] = ns
                elif ready:
                    spans.append([ns, ns])
                    open_span = True
                else:
                    if open_span and joined:
                        spans[-1][1] = max(spans[-1][1], ns - 1)
                    open_span = False
            self._spans = [(lo, hi) for lo, hi in spans]
        return self._spans


def decorate_bars(bars, detectors, tf):
    """向行情 JSON 附加独立的原始成交统计, 不受 FlowMeter 买卖口径影响。

    K 线、事件与覆盖区间都按时间排序, 每个数据源用指针各扫一遍。
    """
    if not bars:
        return
    starts = [int(b["time"]) * NS for b in bars]
    ends = [min(t + tf * NS, starts[i+1] if i+1 < len(starts) else t + tf*NS) for i,t in enumerate(starts)]
    summaries = [{"buy": 0.0, "sell": 0.0, "unknown": 0.0, "count": 0, "maxQty": 0.0,
                  "maxRatio": 0.0, "maxZ": None, "cvd": 0.0} for _ in bars]
    coverage = [[] for _ in bars]
    for detector in detectors:
        # 期货归属 (start,end], 逐笔归属 [start,end); 用整数纳秒避免边界浮点舍入。
        shift = TZ_SHIFT_S * NS - (1 if detector.source == "snapshot" else 0)
        spans = [(lo+shift, hi+shift) for lo,hi in detector.spans()]
        rows = detector.event_rows()
        collected = None   # 有原始记录(含预热)的时间段
        if detector.since is not None and detector.raw:
            collected = (max(detector.since, next(iter(detector.raw.values()))[0]) + shift,
                         next(reversed(detector.raw.values()))[0] + shift)
        event_index, span_index, cumulative = 0, 0, detector.base_delta
        for i, bar in enumerate(bars):
            s = summaries[i]
            while event_index < len(rows) and rows[event_index]["ns"] + shift < ends[i]:
                event = rows[event_index]
                event_index += 1
                cumulative += event["qty"] * event["side"]
                if event["ns"] + shift < starts[i]:
                    continue
                side = "buy" if event["side"] > 0 else "sell" if event["side"] < 0 else "unknown"
                s[side] += event["qty"]
                s["count"] += 1
                s["maxQty"] = max(s["maxQty"], event["qty"])
                s["maxRatio"] = max(s["maxRatio"], event["ratio"])
                if event["z"] is not None:
                    s["maxZ"] = max(s["maxZ"] or 0.0, event["z"])
            s["cvd"] += cumulative
            # 跳过已结束的区间; 与本根相交的区间通常只有一两个。
            while span_index < len(spans) and spans[span_index][1] < starts[i]:
                span_index += 1
            quality = "missing"
            k = span_index
            while k < len(spans) and spans[k][0] < ends[i]:
                if spans[k][0] <= starts[i] and spans[k][1] >= ends[i]-1:
                    quality = "complete"
                    break
                quality = "partial"
                k += 1
            if quality == "missing" and collected and collected[0] < ends[i] and collected[1] >= starts[i]:
                quality = "warming"
            coverage[i].append(quality)
    for i, bar in enumerate(bars):
        s = summaries[i]
        quality = ("complete" if coverage[i] and all(v == "complete" for v in coverage[i]) else
                   "partial" if s["count"] or any(v in {"complete", "partial"} for v in coverage[i]) else
                   "warming" if "warming" in coverage[i] else "missing")
        s["coverage"] = quality
        s["delta"] = s["buy"] - s["sell"]
        volume = bar.get("volume") or 0.0
        total = s["buy"] + s["sell"] + s["unknown"]
        s["share"] = total / volume if quality == "complete" and volume > 0 and total <= volume+1e-8 else None
        for key,value in list(s.items()):
            if isinstance(value, float):
                s[key] = round(value, 8)
        # 未采集/预热不是零成交, 累计值也不应在这些区间画成确定读数。
        if quality in {"missing", "warming"}:
            s["cvd"] = None
        bar["large"] = s
