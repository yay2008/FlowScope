# -*- coding: utf-8 -*-
"""Volume Suite 指标计算(纯 pandas, 不依赖 tqSdk, 便于单测)。

口径移植自 TradingView 开源脚本 "Volume Suite - By Leviathan"
(参考 tqSdk 项目下 Volume_Suite_By_Leviathan.pine)。买卖量可选择
小周期 K 线阴阳归类，或 tick 快照按盘口估算方向，均不能还原逐笔成交。

tick 口径并列输出两套判向: buy/sell/unknown 为 Lee-Ready 新算法,
buyLegacy/sellLegacy 为原快照自身盘口算法(保留作对照, 见 _classify_ticks)。

输出为原始指标值; 阈值等级与颜色由前端按 cfg 实时计算,
切换显示模式/阈值类型不需要后端重算。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

DEFAULT_TF_SEC = 30       # 默认主周期(秒)
# 原生采集的主周期(秒): 各自订阅/判向/落盘, 常驻采集与加密回填只认它们。
NATIVE_TFS = [10, 30]
# 可选主周期(秒), 前后端共用。1 分钟及以上不单独采集, 由 30s 合成(见 rollup.py):
# 买卖量逐根相加, CVD 沿用 30s 的累计, 所以各周期在同一时刻的 CVD 对得上。
TF_OPTIONS = [10, 30, 60, 300, 900, 3600, 14400]
ROLLUP_BASE_TF = 30       # 合成大周期用的底层周期
BAR_NS = DEFAULT_TF_SEC * 10**9   # 默认主周期宽度(纳秒); 单周期调用方的兼容默认值
TZ_SHIFT_S = 8 * 3600    # tqSdk 时间戳是 UTC, +8h 转北京时间给前端展示
GAP_NS = 60 * 10**9      # 相邻快照间隔超过 60s 视为断档(休市/断线), 重置判向状态

# 逐 bar 并列保存的对照列: 新算法与旧算法同表输出, 便于直接比较
EXTRA_COLUMNS = ["unknown", "buyLegacy", "sellLegacy"]

BAR_COLUMNS = ["time", "open", "high", "low", "close", "volume",
               "buy", "sell", "unknown", "delta",
               "buyLegacy", "sellLegacy", "deltaLegacy",
               "cvd", "coverage", "hasBaseline"]

# 前端配色/阈值配置(阈值倍数与等级色同 Pine 默认值; 默认柱色为原版灰白/灰加透明度, 降低存在感)
CFG = {
    "mult": [1.5, 2.5, 3.5],              # 三级阈值倍数
    "rellen": 20,                     # RELATIVE 模式的相对均线长度
    "smalen": 300,                    # SMA 模式均线长度
    "zlen": 50,                       # Z-SCORE 窗口
    "colors": {
        "up": "rgba(209, 212, 220, 0.5)", "down": "rgba(149, 152, 161, 0.5)",
        "upLevels": ["#c8e6c9", "#a5d6a7", "#66bb6a"],
        "downLevels": ["#faa1a4", "#f77c80", "#f7525f"],
    },
    "modes": ["rvol", "crvol", "volume", "bsv", "delta", "cvd"],
    "tfOptions": TF_OPTIONS,          # 可选主周期(秒)
    # 买卖量拆分粒度(秒): 0 = 逐 tick 按盘口判定, 1/5/10/15/30 = 小周期阴阳归类(LTF 口径)
    "ltfOptions": [0, 1, 5, 10, 15, 30],
}


def bar_ns_for(tf_sec) -> int:
    """主周期(秒) -> bar 宽度(纳秒); 非法周期回落到默认值。"""
    return (tf_sec if tf_sec in TF_OPTIONS else DEFAULT_TF_SEC) * 10**9


def is_rollup(tf_sec) -> bool:
    """该周期是否由底层 30s 合成(不在原生采集之列)。"""
    return tf_sec in TF_OPTIONS and tf_sec not in NATIVE_TFS


def ltf_options(tf_sec) -> list[int]:
    """该主周期下合法的拆分粒度: 必须能整除主周期, 且不比主周期更粗。

    主周期 30s 及以上 -> 0/1/5/10/15/30; 主周期 10s -> 0/1/5/10。
    """
    bar = tf_sec if tf_sec in TF_OPTIONS else DEFAULT_TF_SEC
    return [s for s in CFG["ltfOptions"] if s == 0 or (s <= bar and bar % s == 0)]


def tick_bar_start(ns, bar_ns: int):
    """快照时间(纳秒) -> 所属 bar 的起点; 标量、Series 均可。

    快照 t 归入 (start, start + bar_ns]: 恰好落在边界上的快照记入前一根, 与 TqSdk
    K 线的切分一致。上期所/能源中心的快照都在 500ms 整点, 每根 bar 都有一个落在边界上,
    按 [start, end) 分桶会让相邻两根一多一少, 成交量几乎从不与 K 线相等。
    tqSdk 序列的 datetime 是 float64, 纳秒在这个量级上的分辨率只有 256ns,
    减 1 之前必须先转 int64。
    """
    ns = ns.astype("int64") if hasattr(ns, "astype") else int(ns)
    return (ns - 1) // bar_ns * bar_ns


def _finite(value):
    """None/NaN -> None, 其余转 float"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _classify_ticks(ticks: pd.DataFrame, previous_volume=None, previous_side=0.0, *,
                    previous_time=None, previous_price=None, previous_ask=None,
                    previous_bid=None, previous_carry=0.0) -> pd.DataFrame:
    """tick 序列 -> 逐 tick 的(datetime, last_price, dv, side, side_lr)。

    volume 是当日累计, 差分得快照区间量 dv; 跨交易日累计清零,
    diff 为负时该 tick 的累计值即新日已成交量。

    side(旧算法, 保留作对照): last_price >= ask_price1 -> 1(主动买),
    <= bid_price1 -> -1(主动卖), 落在盘口中间沿用上一笔方向。
    注意它比的是"本 tick 自己的"盘口, 而快照盘口是成交后的最新挂单:
    成交把盘口推走时会判反(吃掉卖一后买一上移到成交价, 就被记成主动卖),
    这正是新增 side_lr 的原因。

    side_lr(新算法, Lee-Ready 三级判定): 1=买, -1=卖, 0=未知
      1) 报价规则: 用"前一 tick 的"买卖一(即成交时的挂单),
         price >= prev_ask -> 买; price <= prev_bid -> 卖;
      2) 中点规则: 价差内部 price > 中点 -> 买, < 中点 -> 卖,
         恰在中点落到 3);
      3) 逐笔规则: price > prev_price -> 买, < prev_price -> 卖,
         price == prev_price(zero tick, 同价成交) -> 沿用已确定方向。
      三条都用不上才记 0(未知), 例如窗口首笔既无前一盘口也无前一成交价。
      注意完整 Lee-Ready 下未知量天然很少: 逐笔规则对任何价格变动都能定性,
      它只兜住"毫无判据"的残差, 而不是给每个中间价打问号。

    状态只在有成交(dv>0)的 tick 上推进: 无成交的报价更新不增加买卖量, 也不会
    改写沿用方向, 只把挂单刷成最新观测值。累计量清零(换日)或快照间隔超过
    GAP_NS(断档)时, prev_*/沿用方向全部作废, 避免跨日跨断档沿用旧方向。

    lr_price/lr_ask/lr_bid/lr_carry 记录每行之后的判向状态, 供增量重算把种子
    精确传回, 保证"增量结果 == 全量重算"。
    """
    df = ticks[["datetime", "last_price", "ask_price1", "bid_price1", "volume"]].dropna(
        subset=["datetime", "last_price", "volume"]).copy()
    df = df[(df["datetime"] > 0) & np.isfinite(df["last_price"]) &
            np.isfinite(df["volume"]) & (df["volume"] >= 0)]
    df["dv"] = 0.0
    df["side"] = 0.0
    df["side_lr"] = 0.0
    df["lr_price"] = np.nan
    df["lr_ask"] = np.nan
    df["lr_bid"] = np.nan
    df["lr_carry"] = 0.0
    if df.empty:
        return df
    dv = df["volume"].diff().fillna(0.0)
    if previous_volume is not None:
        dv.iloc[0] = df["volume"].iloc[0] - previous_volume
    day_roll = dv < 0
    dv[day_roll] = df["volume"][day_roll]
    df["dv"] = dv
    day_roll = day_roll.to_numpy()

    # ---- 旧算法(对照): 逐行按当期盘口判定, 中间价沿用上一笔方向 ----
    side = pd.Series(np.nan, index=df.index)
    up = df["last_price"] >= df["ask_price1"]
    down = df["last_price"] <= df["bid_price1"]
    side[up] = 1.0
    side[down] = -1.0
    df["side"] = side.ffill().fillna(previous_side)

    # ---- 新算法: 报价规则 -> 中点规则 -> 逐笔规则 ----
    times = df["datetime"].to_numpy(dtype=np.int64)
    price = df["last_price"].to_numpy(dtype=float)
    ask = df["ask_price1"].to_numpy(dtype=float)
    bid = df["bid_price1"].to_numpy(dtype=float)
    volume = df["dv"].to_numpy(dtype=float)

    prev_time = _finite(previous_time)
    prev_price = _finite(previous_price)
    prev_ask = _finite(previous_ask)
    prev_bid = _finite(previous_bid)
    carry = _finite(previous_carry)
    if carry not in (1.0, -1.0):
        carry = None

    side_lr = np.zeros(len(df))
    state_price = np.full(len(df), np.nan)
    state_ask = np.full(len(df), np.nan)
    state_bid = np.full(len(df), np.nan)
    state_carry = np.zeros(len(df))

    for i in range(len(df)):
        if day_roll[i] or (prev_time is not None and times[i] - prev_time > GAP_NS):
            prev_price = prev_ask = prev_bid = carry = None      # 换日/断档: 状态作废
        if volume[i] > 0:
            resolved = 0.0
            if prev_ask is not None and prev_bid is not None and prev_ask > prev_bid:
                if price[i] >= prev_ask:                          # 1) 吃掉前一卖一
                    resolved = 1.0
                elif price[i] <= prev_bid:                        #    砸掉前一买一
                    resolved = -1.0
                else:                                             # 2) 价差内部比中点
                    mid = (prev_ask + prev_bid) / 2
                    if price[i] > mid:
                        resolved = 1.0
                    elif price[i] < mid:
                        resolved = -1.0
            if resolved == 0.0 and prev_price is not None:        # 3) 逐笔规则
                if price[i] > prev_price:
                    resolved = 1.0
                elif price[i] < prev_price:
                    resolved = -1.0
                elif carry is not None:                           # zero tick 沿用
                    resolved = carry
            side_lr[i] = resolved
            if resolved != 0.0:
                carry = resolved
            prev_price = float(price[i])
        # 挂单在本行判定之后才刷新, 这样下一笔比较的才是"前一"盘口
        if np.isfinite(ask[i]) and np.isfinite(bid[i]) and ask[i] > 0 and bid[i] > 0:
            prev_ask = float(ask[i])
            prev_bid = float(bid[i])
        prev_time = float(times[i])
        state_price[i] = prev_price if prev_price is not None else np.nan
        state_ask[i] = prev_ask if prev_ask is not None else np.nan
        state_bid[i] = prev_bid if prev_bid is not None else np.nan
        state_carry[i] = carry if carry is not None else 0.0

    df["side_lr"] = side_lr
    df["lr_price"] = state_price
    df["lr_ask"] = state_ask
    df["lr_bid"] = state_bid
    df["lr_carry"] = state_carry
    return df


def split_ticks_to_bars(ticks: pd.DataFrame, ltf_sec: int = 0, *, classified=None,
                        bar_ns: int = BAR_NS) -> pd.DataFrame:
    """tick 序列 -> 每根主周期 bar 的主动买/卖/未知量。

    bar_ns: 主周期宽度(纳秒), 由调用方按 tf 传入(见 bar_ns_for); 快照按 (start, end]
            归属(见 tick_bar_start)。
    ltf_sec = 0: 按 tick 快照估算(见 _classify_ticks)。buy/sell/unknown 为新算法
                 (Lee-Ready), buyLegacy/sellLegacy 为旧算法, 同表并列便于对照;
                 恒等式 buy + sell + unknown == observed。
    ltf_sec > 0: 先把 tick 聚成该秒级小周期, 按小周期阴阳整体归类
                 (close>open 全量记买, close<open 全量记卖, 十字线丢弃),
                 仅供旧算法回归使用；线上 K 线口径使用 build_bars_from_ltf。
                 该口径没有 tick 判向, unknown 恒为 0, 对照列与主列同值。
    """
    columns = ["buy", "sell", "unknown", "buyLegacy", "sellLegacy", "observed"]
    df = _classify_ticks(ticks) if classified is None else classified.copy()
    if df.empty:
        return pd.DataFrame(columns=columns, index=pd.Index([], dtype="int64", name="bar_ns"))

    if ltf_sec > 0:
        # 小周期聚合(粒度须整除主周期, 小周期不会横跨主周期 bar 边界)
        micro_ns = ltf_sec * 10**9
        micro = tick_bar_start(df["datetime"], micro_ns)
        g = df.groupby(micro)
        m_open = g["last_price"].first()
        m_close = g["last_price"].last()
        m_vol = g["dv"].sum()
        res = pd.DataFrame({
            "buy": m_vol.where(m_close > m_open, 0.0),
            "sell": m_vol.where(m_close < m_open, 0.0),
            "unknown": 0.0,
            "observed": m_vol,
        })
        res["buyLegacy"] = res["buy"]
        res["sellLegacy"] = res["sell"]
        return res.groupby((res.index // bar_ns) * bar_ns)[columns].sum().rename_axis("bar_ns")

    df["buy"] = df["dv"].where(df["side_lr"] == 1, 0.0)
    df["sell"] = df["dv"].where(df["side_lr"] == -1, 0.0)
    df["unknown"] = df["dv"].where(df["side_lr"] == 0, 0.0)
    df["buyLegacy"] = df["dv"].where(df["side"] == 1, 0.0)
    df["sellLegacy"] = df["dv"].where(df["side"] == -1, 0.0)
    df["observed"] = df["dv"]
    return df.groupby(tick_bar_start(df["datetime"], bar_ns))[columns].sum().rename_axis("bar_ns")


def build_footprint(klines: pd.DataFrame, ticks: pd.DataFrame, tick_size=None, *, classified=None,
                    coverage=None, bar_ns: int = BAR_NS) -> dict:
    """主周期 K线 + tick -> 足迹矩阵(每根 bar 各价格档位的主动买/卖量)。

    bar_ns: 主周期宽度(纳秒); 足迹跟着主图周期走。
    口径同 split_ticks_to_bars(ltf=0); 只输出 klines 中存在且被 tick 覆盖的 bar
    tickSize 来自合约 price_tick；未知时为 None。始终按原始成交价聚合。
    返回 {"tickSize": float,
          "bars": [{"time": 北京时间戳秒, "levels": [[price, buy, sell, unknown], ...按价格升序]}]}
    """
    tick_size = float(tick_size) if tick_size is not None else None
    if tick_size is not None and (not math.isfinite(tick_size) or tick_size <= 0):
        tick_size = None
    df = _classify_ticks(ticks) if classified is None else classified.copy()
    if df.empty:
        return {"tickSize": tick_size, "bars": []}

    df["price"] = df["last_price"]
    df["buy"] = df["dv"].where(df["side_lr"] == 1, 0.0)
    df["sell"] = df["dv"].where(df["side_lr"] == -1, 0.0)
    df["unknown"] = df["dv"].where(df["side_lr"] == 0, 0.0)
    df["bar_ns"] = tick_bar_start(df["datetime"], bar_ns)
    levels = df.groupby(["bar_ns", "price"])[["buy", "sell", "unknown"]].sum()
    # 500ms 快照常见无成交 tick(dv=0), 过滤零量档位避免 "0×0" 幽灵行;
    # 只判成"未知"的档位仍有成交量, 必须保留, 否则足迹图会静默丢量。
    levels = levels[(levels["buy"] > 0) | (levels["sell"] > 0) | (levels["unknown"] > 0)]

    bar_times = {int(ns): int(ns // 10**9 + TZ_SHIFT_S)
                 for ns in klines["datetime"].dropna()}
    bars = []
    for key, group in levels.groupby(level=0):
        time_s = bar_times.get(key)
        if time_s is None:
            continue
        lvl = [[float(price), float(buy), float(sell), float(unknown)]
               for price, buy, sell, unknown in zip(group.index.get_level_values("price"),
                                                    group["buy"], group["sell"], group["unknown"])]
        bars.append({"time": time_s, "levels": lvl,
                     "coverage": (coverage or {}).get(time_s, "partial")})
    return {"tickSize": tick_size, "bars": bars}


def finalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """补齐 delta 列(在合并 CSV 历史之后也可重复调用)

    delta/cvd 一律走新算法(buy/sell); deltaLegacy 保留旧算法口径供对照。
    """
    df["delta"] = df["buy"] - df["sell"]
    if "buyLegacy" in df and "sellLegacy" in df:
        df["deltaLegacy"] = df["buyLegacy"] - df["sellLegacy"]
    covered = df["delta"].notna()
    if "coverage" in df:
        covered &= df["coverage"].eq("complete")
    df["cvd"] = df["delta"].fillna(0).cumsum().where(covered)
    return df


def build_bars(klines: pd.DataFrame, ticks: pd.DataFrame, ltf_sec: int = 0, *, classified=None,
               aggregates=None, first_tick_ns=None, bar_ns: int = BAR_NS) -> pd.DataFrame:
    """主周期 K线 + tick 买卖量 -> bars 表(time 为北京时间戳秒, 已 +8h)

    ltf_sec: 买卖量拆分粒度(秒), 0 = 逐 tick 按盘口判定, 见 split_ticks_to_bars
    bar_ns : 主周期宽度(纳秒), 只影响 tick 聚合的分桶; OHLC 仍来自 klines 本身
    first_tick_ns: 基线快照时间, 它自己的区间量未知。bar 覆盖 (start, end](见 tick_bar_start),
                   起点不早于基线的 bar 才可能被完整观测。
    """
    k = klines[["datetime", "open", "high", "low", "close", "volume"]].dropna(
        subset=["datetime", "close"])
    if k.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    classified = _classify_ticks(ticks) if classified is None else classified
    bs = (split_ticks_to_bars(ticks, ltf_sec, classified=classified, bar_ns=bar_ns)
          if aggregates is None else aggregates)
    df = (k.rename(columns={"datetime": "bar_ns"})
           .set_index("bar_ns")
           .join(bs, how="left"))
    df = df.reset_index()
    df["coverage"] = np.where(df["observed"].notna(), "partial", "missing")
    if first_tick_ns is None and not classified.empty:
        first_tick_ns = int(classified["datetime"].iloc[0])
    if first_tick_ns is not None:
        baseline = df["bar_ns"] >= first_tick_ns
        complete = baseline & np.isclose(df["observed"], df["volume"], rtol=0, atol=1e-6)
        df.loc[complete, "coverage"] = "complete"
        df["hasBaseline"] = baseline & df["observed"].notna()
    else:
        df["hasBaseline"] = False
    df = finalize_bars(df)
    df["time"] = (df["bar_ns"] // 10**9 + TZ_SHIFT_S).astype("int64")
    return df[BAR_COLUMNS]


def bars_to_records(bars: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON 可序列化的 dict 列表(NaN -> None)"""
    recs = bars.to_dict("records")
    return [
        {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
        for row in recs
    ]


def build_bars_from_ltf(klines: pd.DataFrame, lower: pd.DataFrame, ltf_sec: int, *,
                        bar_ns: int = BAR_NS) -> pd.DataFrame:
    """Pine 风格：直接按小周期 K 线阴阳归类其 volume，再汇总为主周期 bar。"""
    bar_sec = bar_ns // 10**9
    if ltf_sec not in ltf_options(bar_sec) or ltf_sec <= 0:
        raise ValueError("K线拆分粒度无效")
    data = lower[["datetime", "open", "close", "volume"]].dropna().copy()
    data = data[(data.datetime > 0) & np.isfinite(data).all(axis=1) & (data.volume >= 0)]
    data = data.drop_duplicates("datetime", keep="last").sort_values("datetime")
    data["buy"] = data.volume.where(data.close > data.open, 0.)
    data["sell"] = data.volume.where(data.close < data.open, 0.)
    data["unknown"] = 0.
    # K线口径没有 tick 判向, 对照列与主列同值, 便于前端统一取列
    data["buyLegacy"] = data["buy"]
    data["sellLegacy"] = data["sell"]
    data["observed"] = data.volume
    grouped = data.groupby(data.datetime // bar_ns * bar_ns)
    aggregates = grouped[["buy", "sell", "unknown", "buyLegacy",
                          "sellLegacy", "observed"]].sum().rename_axis("bar_ns")
    result = build_bars(klines, None, ltf_sec, classified=pd.DataFrame(), aggregates=aggregates,
                        first_tick_ns=int(data.datetime.iloc[0]) if not data.empty else None,
                        bar_ns=bar_ns)
    if not result.empty:
        # 未覆盖整根主周期（包括最左侧截断和右侧未结束的 bar）保留部分覆盖标记。
        counts = grouped.size()
        keys = (result.time - TZ_SHIFT_S) * 10**9
        incomplete = keys.map(counts).fillna(0) < bar_sec // ltf_sec
        result.loc[incomplete & result.coverage.eq("complete"), "coverage"] = "partial"
        result = finalize_bars(result)
    return result
