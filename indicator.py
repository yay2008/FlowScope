# -*- coding: utf-8 -*-
"""Volume Suite 指标计算(纯 pandas, 不依赖 tqSdk, 便于单测)。

口径移植自 TradingView 开源脚本 "Volume Suite - By Leviathan"
(参考 tqSdk 项目下 Volume_Suite_By_Leviathan.pine)。唯一区别:
买卖量拆分用上期所 500ms tick 快照按买一/卖一判定主动方向,
TV 原版用 1 秒 K 线 close vs open 近似, 本模块粒度更细。

输出为原始指标值; 阈值等级与颜色由前端按 cfg 实时计算,
切换显示模式/阈值类型不需要后端重算。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

BAR_NS = 30 * 10**9      # 30 秒 K 线宽度(纳秒)
TZ_SHIFT_S = 8 * 3600    # tqSdk 时间戳是 UTC, +8h 转北京时间给前端展示

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
    # 买卖量拆分粒度(秒): 0 = 逐 tick 按盘口判定, 1/5/15/30 = 小周期阴阳归类(LTF 口径)
    "ltfOptions": [0, 1, 5, 15, 30],
}


def split_ticks_to_bars(ticks: pd.DataFrame, ltf_sec: int = 0) -> pd.DataFrame:
    """tick 序列 -> 每根 30s bar 的主动买/卖量。

    ltf_sec = 0: 逐 tick 判定, last_price >= ask_price1 -> 主动买, <= bid_price1 -> 主动卖,
                 落在盘口中间沿用上一笔方向(最细粒度, 优于 TV 的 LTF 口径)。
    ltf_sec > 0: 先把 tick 聚成该秒级小周期, 按小周期阴阳整体归类
                 (close>open 全量记买, close<open 全量记卖, 十字线丢弃),
                 等价于原 Volume Suite 指标的 LTF Timeframe 口径。
    volume 是当日累计, 差分得单笔量; 跨交易日(夜盘 21:00)累计清零,
    diff 为负时该 tick 的累计值即新日已成交量。
    """
    df = ticks[["datetime", "last_price", "ask_price1", "bid_price1", "volume"]].dropna(
        subset=["datetime", "last_price"])
    if df.empty:
        return pd.DataFrame(columns=["buy", "sell"], index=pd.Index([], name="bar_ns"))

    dv = df["volume"].diff().fillna(0.0)
    neg = dv < 0
    dv[neg] = df["volume"][neg]

    if ltf_sec > 0:
        # 小周期聚合(仅支持能整除 30s 的粒度, 小周期不会横跨 30s bar 边界)
        micro_ns = ltf_sec * 10**9
        micro = (df["datetime"] // micro_ns) * micro_ns
        g = df.assign(dv=dv).groupby(micro)
        m_open = g["last_price"].first()
        m_close = g["last_price"].last()
        m_vol = g["dv"].sum()
        res = pd.DataFrame({
            "buy": m_vol.where(m_close > m_open, 0.0),
            "sell": m_vol.where(m_close < m_open, 0.0),
        })
        bar_key = (res.index // BAR_NS) * BAR_NS
        return res.groupby(bar_key)[["buy", "sell"]].sum().rename_axis("bar_ns")

    side = pd.Series(np.nan, index=df.index)
    up = df["last_price"] >= df["ask_price1"]
    down = df["last_price"] <= df["bid_price1"]
    side[up] = 1.0
    side[down] = -1.0
    side = side.ffill().fillna(0.0)

    df["buy"] = dv.where(side == 1, 0.0)
    df["sell"] = dv.where(side == -1, 0.0)
    bar_ns = (df["datetime"] // BAR_NS) * BAR_NS
    return df.groupby(bar_ns)[["buy", "sell"]].sum().rename_axis("bar_ns")


def finalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """补齐 delta/cvd 列(在合并 CSV 历史之后也可重复调用)"""
    df["delta"] = df["buy"] - df["sell"]
    covered = df["delta"].notna()
    df["cvd"] = df["delta"].fillna(0).cumsum().where(covered)
    return df


def build_bars(klines: pd.DataFrame, ticks: pd.DataFrame, ltf_sec: int = 0) -> pd.DataFrame:
    """30s K线 + tick 买卖量 -> bars 表(time 为北京时间戳秒, 已 +8h)

    ltf_sec: 买卖量拆分粒度(秒), 0 = 逐 tick 按盘口判定, 见 split_ticks_to_bars
    """
    k = klines[["datetime", "open", "high", "low", "close", "volume"]].dropna(
        subset=["datetime", "close"])
    if k.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close",
                                     "volume", "buy", "sell", "delta", "cvd"])
    bs = split_ticks_to_bars(ticks, ltf_sec)
    df = (k.rename(columns={"datetime": "bar_ns"})
           .set_index("bar_ns")
           .join(bs, how="left"))
    df = finalize_bars(df.reset_index())
    df["time"] = (df["bar_ns"] // 10**9 + TZ_SHIFT_S).astype("int64")
    return df[["time", "open", "high", "low", "close", "volume", "buy", "sell", "delta", "cvd"]]


def bars_to_records(bars: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON 可序列化的 dict 列表(NaN -> None)"""
    recs = bars.to_dict("records")
    return [
        {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
        for row in recs
    ]
