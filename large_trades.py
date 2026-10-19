"""先筛原始成交事件, 再按图表周期汇总。统计只使用当前事件之前的样本。

期货输入快照区间量; 加密输入交易所逐笔/聚合成交, 同一毫秒、同一方向的连续成交合并成一个事件
(一张吃单扫多档、吃多个挂单时交易所会拆成多条)。只保留本次采集(含回填补判)的检测结果,
不从 OHLCV/买卖量历史反推大额事件。不同图表周期应共用一个 Detector。

判定: 金额达到该品种的门槛(主条件, 各品种初始值见 large_trade_defaults.py), 且超过基线事件量的 P99。
加密另存超过 P99、但不足金额门槛的相对异常, 不混入原大单统计。基线是最近 10 分钟交易时间的事件, 但不含最近 30 秒: 一阵密集大单
都和放量前的水平比, 不会被自己抬高的门槛压掉; 放量持续下去门槛照样跟上。相对均量的倍数与 Z 只供查看
(实测超过 P99 的事件都远高于均量 1.5 倍, 原来的倍数条件从没挡掉过事件, 已去掉)。
"""
from bisect import bisect_left, insort
from collections import deque
from functools import lru_cache
import json
import math
import os
import re

from history_store import ChangeMarks
from indicator import GAP_NS, TZ_SHIFT_S
from large_trade_defaults import MIN_NOTIONAL

NS = 1_000_000_000
WINDOW_CAP = 50_000    # 基线最多保留的事件数(币安 BTC 10 分钟约 9000 笔, 极端行情留足余量)
RAW_SLACK = 20_000     # 原始记录在基线之外多留的条数, 供修订重放
EVENT_CAP = 20_000     # 最多保留的大额事件数, 更早的只折进 CVD 起点
SOURCE_LABELS = {"snapshot": "期货快照估算", "trade": "交易所逐笔", "aggregate": "交易所聚合成交"}
_PRODUCT_RE = re.compile(r"(?:KQ\.[a-z]@)?([A-Z]+)\.([A-Za-z]+)\d*")


def product_of(symbol):
    """品种键: 期货去掉主连前缀和月份(KQ.m@SHFE.fu、SHFE.fu2611 -> SHFE.fu), 加密合约原样。"""
    match = _PRODUCT_RE.fullmatch(symbol or "")
    return f"{match[1]}.{match[2]}" if match else symbol


def default_settings(source, symbol=None):
    """内置参数。金额门槛取该品种的初始值; 表里没有的期货回落到 20 手, 加密回落到 1 万 USDT。"""
    # 基线 10 分钟: 30 分钟时放量后半小时都压着相对门槛, 行情转淡后迟迟不出点。
    # 最近 30 秒不进基线: 连串大单不被前面几笔抬高的门槛压掉。
    settings = {"windowSec": 600, "lagSec": 30, "warmup": 200, "quantile": 0.99,
                "minQty": 0.0, "minNotional": 0.0 if source == "snapshot" else 10000.0}
    floor = MIN_NOTIONAL.get(product_of(symbol)) if symbol else None
    if floor is not None:
        settings["minNotional"] = float(floor)
    elif source == "snapshot":
        settings["minQty"] = 20.0
    return settings


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
    settings = default_settings(source, symbol)
    # 依次按 '*'(全部合约)、品种(SHFE.fu)、完整合约代码覆盖; 非法配置明确报错。留空等于不覆盖。
    overrides = _overrides(os.getenv("LARGE_TRADE_SETTINGS", "").strip() or "{}")
    for key in dict.fromkeys(("*", product_of(symbol), symbol)):
        settings.update({k: v for k, v in overrides.get(key, {}).items() if k in settings})
    for key in ("windowSec", "lagSec", "warmup", "quantile", "minQty", "minNotional"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"大额成交 {key} 必须是有限数值")
        if key in ("windowSec", "lagSec", "warmup"):
            if not float(value).is_integer():
                raise ValueError(f"大额成交 {key} 必须是整数")
            settings[key] = int(value)
        else:
            settings[key] = float(value)
    if not (60 <= settings["windowSec"] <= 86400 and 0 <= settings["lagSec"] < settings["windowSec"] and
            2 <= settings["warmup"] <= WINDOW_CAP and
            0 < settings["quantile"] < 1 and
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
        return default_settings(source, symbol)


def _scan(entries, spans, open_span):
    """按序号顺序扫 (时间ns, 已判定, 与上一条之间没丢数据), 把已判定数据覆盖的时间段接到 spans 后面。

    未判定的记录切断区间; 它与上一条之间没丢数据时, 区间延伸到它前 1ns:
    收盘前最后一笔之后到下一笔之前确实没有成交, 收盘那根 K 线不应一直算作部分覆盖。
    返回 (spans, 扫完时区间是否还开着), 裁剪原始记录时据此接着扫。
    """
    for ns, ready, joined in entries:
        if ready and open_span and joined:
            spans[-1][1] = ns
        elif ready:
            spans.append([ns, ns])
            open_span = True
        else:
            if open_span and joined:
                spans[-1][1] = max(spans[-1][1], ns - 1)
            open_span = False
    return spans, open_span


def _merge_spans(live, history, floor):
    merged = []
    for lo, hi in sorted([*live, *history]):
        if floor is not None:
            if hi <= floor:
                continue
            lo = max(lo, floor + 1)
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


class _EventLayer:
    """一层大额事件: 事件、CVD 起点(已丢弃事件的净量)、覆盖起点与回填补判的区间, 按事件数各自裁剪。
    检测器本身就是原大单层, detector.relative 是加密另存的空心层(超过 P99 但不足金额线);
    两层共用检测器的判定与实时覆盖, 各自保留事件, 空心层不会挤掉原大单。"""

    def __init__(self, detector):
        self.detector, self.source = detector, detector.source
        self.events = {}       # key -> 已筛选事件; 不随图表窗口移动清零
        self.base_delta = 0.0
        self.since = None
        self._history = []     # 回填补判来的区间
        self._events_floor = None   # 已丢弃的最新一个事件的时刻
        self._cache = self._spans = None
        self._index_cache = None

    def event_rows(self):
        if self._cache is None:
            self._cache = sorted(self.events.values(), key=lambda e: e["ns"])
        return self._cache

    def event_index(self):
        """(按时间排好的事件, 各自的时刻, 逐个累加到第 k 个事件之前的 CVD): 汇总只看最近几根时,
        二分跳过更早的事件。累加从 base_delta 起、按同一顺序逐个加, 与从头扫一遍逐位一致(末尾多一个"全部加完")。"""
        rows = self.event_rows()
        if self._index_cache is None or self._index_cache[0] is not rows:
            before, cumulative = [], self.base_delta
            for event in rows:
                before.append(cumulative)
                cumulative += event["qty"] * event["side"]
            before.append(cumulative)
            self._index_cache = (rows, [event["ns"] for event in rows], before)
        return self._index_cache

    def collected(self):
        """有原始记录(含预热)的时间段 (起, 止), 没有时 None。事件丢弃过的部分不算。"""
        return self.detector._collected(self.since, self._events_floor)

    def spans(self):
        """已判定数据覆盖的时间段 [(起, 止)], 按时间排序: 当前原始记录(见 _scan)接在已裁掉部分之后,
        再并上回填补判的区间; 相接的合并, 事件已丢弃的部分去掉。"""
        if self._spans is None:
            self._spans = _merge_spans(self.detector._live_coverage(), self._history, self._events_floor)
        return self._spans

    def _trim(self):
        """事件太多时丢掉最早的(按时间), 其净量折进 CVD 起点; 那以前的 K 线不再算作已覆盖。返回裁没裁。"""
        if len(self.events) <= EVENT_CAP + EVENT_CAP // 20:   # 攒一点再裁, 不必每来一个事件就排序
            return False
        ordered = sorted(self.events.items(), key=lambda item: item[1]["ns"])
        for key, event in ordered[:-EVENT_CAP]:
            del self.events[key]
            self.base_delta += event["qty"] * event["side"]
            self._events_floor = event["ns"] if self._events_floor is None else max(self._events_floor, event["ns"])
        self._cache = self._spans = None
        return True


class LargeTradeDetector(_EventLayer):
    def __init__(self, symbol, source="snapshot", settings=None):
        self.symbol, self.source = symbol, source
        super().__init__(self)
        self.settings = _settings_or_default(symbol, source) if settings is None else settings
        # key -> (时间ns, 价格, 量, 方向, 重置基线, 可用, 与上一条之间没丢数据)
        self.raw = {}
        self.readiness = {}    # key -> (时间ns, 已判定, 与上一条之间没丢数据); 随 raw 裁剪
        self.version = 0       # 每次有记录变化加一, 供按版本复用汇总结果
        # 每手对应的标的数量: 加密的量已是币, 期货等报价里的合约乘数(Feed 拿到后设置)
        self.multiplier = None if source == "snapshot" else 1.0
        self._last_crypto = None
        self._run = None        # 加密: 正在合并的事件 (key, 时间ns, 方向)
        self._window = deque()  # 基线 (交易时钟, 量)
        self._lagged = deque()  # 还在滞后期里的 (交易时钟, 量), 到期才进基线
        self._sorted = []
        self._sum = self._sum2 = 0.0
        self._clock, self._last_ns = 0, None
        # 已裁掉的原始记录留下的覆盖区间与扫描状态; 实时覆盖(两层共用)
        self._archived, self._archived_open = [], False
        self._live_spans = None
        # 实时覆盖扫到哪了 (区间, 扫完时区间是否还开着, 扫到的最后一个 key): 新记录只接着往后扫(见 _live_coverage)
        self._live_scan = None
        self.relative = _EventLayer(self) if source != "snapshot" else None
        # 各 Feed 上次来取之后, 汇总结果最早从哪个时刻(ns)起可能变了; 只重新汇总那以后的几根(见 ingest.Feed)
        self.changes = ChangeMarks()

    def _layers(self):
        return (self,) if self.relative is None else (self, self.relative)

    def config(self):
        return {**self.settings, "source": self.source, "label": SOURCE_LABELS[self.source],
                "symbol": self.symbol, "multiplier": self.multiplier,
                "history": "本次采集; 旧K线历史不回推" if self.source == "snapshot"
                else "本次采集, 断线/关机缺口回填后补判最近 6 小时; 已完整落盘的旧K线不回推"}

    def _clear_window(self):
        self._window.clear()
        self._lagged.clear()
        self._sorted.clear()
        self._sum = self._sum2 = 0.0

    def _advance(self, ns):
        """交易时钟: 相邻记录的间隔按 GAP_NS 封顶, 午休、收盘到夜盘不占基线的时间窗。"""
        if self._last_ns is not None and ns > self._last_ns:
            self._clock += min(ns - self._last_ns, GAP_NS)
        self._last_ns = ns if self._last_ns is None else max(self._last_ns, ns)

    def _promote(self):
        """滞后期满的事件进基线。lagSec=0 时此前的事件全部进入(判定只看本条之前的)。"""
        cutoff = self._clock - self.settings["lagSec"] * NS
        while self._lagged and self._lagged[0][0] <= cutoff:
            self._push(*self._lagged.popleft())

    def _evict(self):
        """丢掉时间窗以外的旧事件, 但至少留 warmup 个: 冷门品种按笔数补足, 不会一直预热。"""
        cutoff = self._clock - self.settings["windowSec"] * NS
        warmup = self.settings["warmup"]
        while self._window and (len(self._window) > WINDOW_CAP or
                                (len(self._window) > warmup and self._window[0][0] < cutoff)):
            _, old = self._window.popleft()
            self._sorted.pop(bisect_left(self._sorted, old))
            self._sum -= old
            self._sum2 -= old * old

    def _push(self, clock, q):
        self._window.append((clock, q))
        insort(self._sorted, q)
        self._sum += q
        self._sum2 += q * q

    def _process(self, key, row, judge=True):
        """推进基线; judge=False 只重建基线(修订重放), 不改判定结果。"""
        ns, price, qty, side, reset, usable, joined = row
        self._advance(ns)
        if reset:
            self._clear_window()
        self._promote()
        self._evict()
        if judge:
            ready = usable and len(self._window) >= self.settings["warmup"]
            if self._live_scan is not None and key <= self._live_scan[2]:
                self._live_scan = None     # 改的是已经扫过的记录: 下次从头扫
            self.readiness[key] = (ns, ready, joined)
            if ready and qty > 0:
                self._judge(key, ns, price, qty, side)
        if usable and qty > 0:
            self._lagged.append((self._clock, qty))

    def _judge(self, key, ns, price, qty, side):
        n = len(self._window)
        mean = self._sum / n
        sd = math.sqrt(max(0.0, self._sum2 / n - mean * mean))
        pos = (n - 1) * self.settings["quantile"]
        lo, hi = math.floor(pos), math.ceil(pos)
        threshold = self._sorted[lo] + (self._sorted[hi] - self._sorted[lo]) * (pos - lo)
        ratio = qty / mean if mean else 0.0
        z = (qty - mean) / sd if sd > 0 else None
        floor = self.settings["minNotional"]
        # 合约乘数未知时不能判断金额门槛, 宁可不报。
        rich = floor <= 0 or (self.multiplier is not None and price * qty * self.multiplier >= floor)
        # 合并事件可能从空心层跨过金额线进入原大单层; 两层始终互斥。
        for layer in self._layers():
            if layer.events.pop(key, None) is not None:
                layer._cache = None
        if qty > threshold and qty >= self.settings["minQty"]:
            layer = self if rich else self.relative
            if layer is not None:
                layer.events[key] = {"ns": ns, "price": price, "qty": qty, "side": side,
                                     "ratio": ratio, "z": z, "threshold": threshold}
                layer._cache = None

    def _trim_events(self):
        trimmed = [layer._trim() for layer in self._layers()]
        if any(trimmed):
            self.changes.mark(float("-inf"))   # CVD 起点与覆盖起点变了, 每根都要重算

    def update(self, rows):
        """已按 key 排序的原始记录。重复不计, 修订从该记录起重放, 保留此前的统计基线。"""
        changes = [(key, row) for key, row in rows if self.raw.get(key) != row]
        if self.raw:
            first = next(iter(self.raw))
            changes = [(key, row) for key, row in changes if key >= first]
        if not changes:
            return
        old_last = next(reversed(self.raw)) if self.raw else None
        # 覆盖与"已采集"的末端从上一条记录延过来; 重放的各条(含它们的事件)都在 tail 里, 下面逐条取最早
        touched = next(reversed(self.raw.values()))[0] if self.raw else float("-inf")
        earliest = min(key for key, _ in changes)
        self.raw.update(changes)
        self._begin(min(row[0] for _, row in changes))
        replay = old_last is not None and earliest <= old_last
        if replay:
            self._live_scan = None           # 改到了扫过的记录, 实时覆盖从头扫
            self.raw = dict(sorted(self.raw.items()))
            self._clear_window()
            self._clock, self._last_ns = 0, None
            # 重建修订之前的基线(含交易时钟, 时间窗只看相对间隔); 不修改此前已确定的大额事件。
            for key, row in self.raw.items():
                if key >= earliest:
                    break
                self._process(key, row, judge=False)
            for layer in self._layers():
                layer.events = {key: value for key, value in layer.events.items() if key < earliest}
                layer._cache = None
        tail = ((key, row) for key, row in self.raw.items() if key >= earliest) if replay else changes
        for key, row in tail:
            touched = min(touched, row[0])
            self._process(key, row)
        self.changes.mark(touched)
        self._settle()

    def _append(self, key, row):
        """update 的快速路径: key 比已有的都大(加密成交编号只增不减), 不必查重、不会重放。"""
        # 覆盖区间与"已采集"的末端从上一条记录延到这一条: 上一条所在的那根起都可能变
        previous = next(reversed(self.raw.values()))[0] if self.raw else float("-inf")
        self.changes.mark(min(previous, row[0]))
        self.raw[key] = row
        self._begin(row[0])
        self._process(key, row)
        self._settle()

    def _begin(self, ns):
        """本次采集的第一条记录: 两层的覆盖起点都从这里算。"""
        for layer in self._layers():
            if layer.since is None:
                layer.since = ns

    def _settle(self):
        # 原始记录要能在修订重放时重建当前基线, 另外多留一段; 再早的只留下覆盖区间。攒够一批再裁。
        limit = len(self._window) + len(self._lagged) + RAW_SLACK
        if len(self.raw) > limit + max(1024, limit // 4):
            keys = list(self.raw)
            if self._live_scan is not None and keys[-limit - 1] > self._live_scan[2]:
                self._live_scan = None     # 裁掉的里头有还没扫过的: 下次从已裁部分接着从头扫
            self._archived, self._archived_open = _scan(
                (self.readiness[key] for key in keys[:-limit]), self._archived, self._archived_open)
            self.raw = {key: self.raw[key] for key in keys[-limit:]}
            self.readiness = {key: self.readiness[key] for key in keys[-limit:]}
        self._trim_events()
        self._live_spans = None
        for layer in self._layers():
            layer._spans = None
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
                # 累计量回落是换交易日: 首笔含集合竞价, 本条量不可用; 序号连续说明没丢数据, 基线沿用,
                # 否则夜盘开盘最活跃的一两分钟都在预热。
                roll = row.volume < previous.volume
                qty = float(row.volume - previous.volume)
                rows.append((key, (ns, float(row.last_price), max(qty, 0.0), int(row.side_lr),
                                   not joined, joined and not roll, joined)))
            previous = row
            previous_id = identity
        self.update(rows)

    def trade(self, identity, price, qty, ms, sell):
        identity, ns = int(identity), int(ms) * 1_000_000
        if self._last_crypto is not None and identity <= self._last_crypto[0]:
            return
        joined = (self._last_crypto is not None and identity == self._last_crypto[0] + 1 and
                  ns >= self._last_crypto[1])
        self._last_crypto = (identity, ns)
        # 坏价格/量的一笔也要占住编号, 否则下一笔会被当成漏号而重新预热; 本身记为不可判定。
        valid = math.isfinite(price) and math.isfinite(qty) and price > 0 and qty >= 0
        side = -1 if sell else 1
        run = self._run
        if valid and qty > 0 and joined and run is not None and run[1:] == (ns, side):
            self._extend(run[0], float(price), float(qty))
            return
        self._append(identity, (ns, float(price) if valid else 0.0, float(qty) if valid else 0.0,
                                side, not joined, valid, joined))
        self._run = (identity, ns, side) if valid and qty > 0 else None

    def _extend(self, key, price, qty):
        """同一毫秒、同一方向的连续成交多半是一张吃单扫了多档或吃了多个挂单: 并进当前事件,
        价格取成交量加权。按判定首笔时的同一基线重判(两笔之间基线没动过, 本事件还在滞后队列末尾)。"""
        ns, p0, q0, side, reset, usable, joined = self.raw[key]
        self.changes.mark(ns)
        total = q0 + qty
        vwap = (p0 * q0 + price * qty) / total
        self.raw[key] = (ns, vwap, total, side, reset, usable, joined)
        self._lagged[-1] = (self._lagged[-1][0], total)
        if self.readiness[key][1]:
            self._judge(key, ns, vwap, total, side)
        self.version += 1

    def absorb(self, events, spans, relative_events=None, relative_spans=None):
        """并入回填补判的结果: 洞里(本次运行开始前或断线期间)的事件 {成交编号: 事件} 与已判定区间。
        它们只参与展示和 CVD, 不动实时基线。

        加密另带空心层的事件与区间; 两个都不给表示这次没判空心层, 它的覆盖不变, 只给事件时区间同原大单。
        同一编号以量大的为准(合并得更全: 缺口边缘、重试时可能只拿到合并事件的一部分), 放进它判在的那层,
        两层始终互斥。早于任一层已丢弃事件时刻的不再并入: 那以前的净量已折进 CVD 起点, 再并就重复计。"""
        layers = self._layers()
        batches = [(self, events, spans)]
        if self.relative is not None and (relative_events is not None or relative_spans is not None):
            batches.append((self.relative, relative_events or {}, spans if relative_spans is None else relative_spans))
        floors = [layer._events_floor for layer in layers if layer._events_floor is not None]
        floor = max(floors) if floors else None
        changed = False
        for target, batch, covered in batches:
            if floor is not None:
                batch = {key: event for key, event in batch.items() if event["ns"] > floor}
                covered = [(max(lo, floor + 1), hi) for lo, hi in covered if hi > floor]
            if not batch and not covered:
                continue
            for key, event in batch.items():
                current = next((layer.events[key] for layer in layers if key in layer.events), None)
                if current is not None and current["qty"] > event["qty"]:
                    continue
                for layer in layers:
                    layer.events.pop(key, None)
                target.events[key] = event
            target._history = sorted([*target._history, *(tuple(span) for span in covered)])
            starts = [lo for lo, _ in covered] + [event["ns"] for event in batch.values()]
            target.since = min(starts) if target.since is None else min(target.since, *starts)
            changed = True
        if not changed:
            return
        self.changes.mark(float("-inf"))
        for layer in layers:
            layer._cache = layer._spans = None
        self._trim_events()
        self.version += 1

    def _collected(self, since, floor):
        if since is None or not self.raw:
            return None
        start = since if floor is None else max(since, floor + 1)
        return start, next(reversed(self.raw.values()))[0]

    def _live_coverage(self):
        """实时采集(含已裁掉的原始记录)覆盖的时间段, 两层共用。

        加密的记录按 key 只增不减地追加, 只把上次扫过之后的新记录接着扫下去(_scan 是从左往右折叠,
        结果与从头扫一遍相同); 改到扫过的记录(期货修订)才从头扫。
        """
        if self._live_spans is None:
            state = self._live_scan
            if state is None:
                live, open_span = _scan((self.readiness[key] for key in sorted(self.readiness)),
                                        [list(span) for span in self._archived], self._archived_open)
                last = max(self.readiness, default=float("-inf"))
            else:
                live, open_span, last = state
                fresh = []
                for key in reversed(self.readiness):
                    if key <= last:
                        break
                    fresh.append(key)
                if fresh:
                    live, open_span = _scan((self.readiness[key] for key in reversed(fresh)), live, open_span)
                    last = fresh[0]
            self._live_scan = (live, open_span, last)
            self._live_spans = [(lo, hi) for lo, hi in live]
        return self._live_spans


def judge_history(symbol, source, trades, *, include_relative=False):
    """回填线程用: 按编号顺序把一段历史成交 (编号, 价, 量, 毫秒, 是否主动卖) 过一遍独立的检测器
    (编号接不上处重新预热), 返回可交给 absorb 的 (事件, 覆盖区间)。
    include_relative=True 时另外返回 (相对异常事件, 相对异常覆盖区间)。"""
    detector = LargeTradeDetector(symbol, source)
    for identity, price, qty, ms, sell in trades:
        detector.trade(identity, price, qty, ms, sell)
    result = (dict(detector.events), detector.spans())
    if include_relative:
        relative = detector.relative
        # 两层独立裁剪, 回填时也保留各自真正覆盖的区间。
        return (*result, dict(relative.events) if relative else {}, relative.spans() if relative else [])
    return result


def decorate_bars(bars, detectors, tf):
    """向行情 JSON 附加独立的原始成交统计, 不受 FlowMeter 买卖口径影响: 原大单进 large, 加密空心层进 largeRelative。

    K 线、事件与覆盖区间都按时间排序, 每个数据源用指针各扫一遍。空心层没有事件的根只给
    {count: 0, coverage, cvd}, 省掉一串零(前端读明细前补齐, 见 FlowLargeTrades.relativeOf)。
    """
    if not bars:
        return
    starts = [int(b["time"]) * NS for b in bars]
    ends = [min(t + tf * NS, starts[i+1] if i+1 < len(starts) else t + tf*NS) for i,t in enumerate(starts)]
    _decorate_layer(bars, detectors, starts, ends, "large")
    relative = [d.relative for d in detectors if d.relative is not None]
    if relative:
        _decorate_layer(bars, relative, starts, ends, "largeRelative", compact=True)


def _decorate_layer(bars, detectors, starts, ends, field, compact=False):
    summaries = [{"buy": 0.0, "sell": 0.0, "unknown": 0.0, "count": 0, "maxQty": 0.0,
                  "maxRatio": 0.0, "maxZ": None, "cvd": 0.0} for _ in bars]
    coverage = [[] for _ in bars]
    for detector in detectors:
        # 期货归属 (start,end], 逐笔归属 [start,end); 用整数纳秒避免边界浮点舍入。
        shift = TZ_SHIFT_S * NS - (1 if detector.source == "snapshot" else 0)
        spans = [(lo+shift, hi+shift) for lo,hi in detector.spans()]
        rows, times, before = detector.event_index()
        collected = detector.collected()   # 有原始记录(含预热)的时间段
        if collected is not None:
            collected = (collected[0] + shift, collected[1] + shift)
        # 第一根之前的事件只进累计: 直接跳到第一根起点, 累计值取缓存里逐个加到那儿的
        event_index = bisect_left(times, starts[0] - shift)
        span_index, cumulative = 0, before[event_index]
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
        bar[field] = {"count": 0, "coverage": quality, "cvd": s["cvd"]} if compact and not s["count"] else s
