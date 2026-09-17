# -*- coding: utf-8 -*-
"""评估 FlowScope 各指标在今天(2026-09-16)行情中的准确度。

数据来自正在运行的服务 /api/history(已落盘+实时 bar), 指标计算严格移植
static/app.js 的前端逻辑(ema/rma/rsi/linreg/sma/TCI/MFI/CRVOL), 保证与
页面实际看到的数值一致。评估口径: 指标信号方向 vs 后续 N 根 bar 收益方向。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys

import requests

API = "http://127.0.0.1:8000/api/history"
SYMBOL = "KQ.m@SHFE.fu"

# ---------- app.js 常量 ----------
MULT = [1.5, 2.5, 3.5]        # 三级阈值
RELLEN = 20                   # 相对均线长度
ZLEN = 50
LW = dict(n1=9, n2=6, n3=3, n4=21, ob=80, os=20, slopeLen=10)
EMA_PERIODS = [21, 55, 100, 200]


# ---------- 指标函数(移植自 app.js, 数组语义完全一致) ----------
def ema(v, n):
    out = [None] * len(v)
    a = 2 / (n + 1)
    prev = None
    for i, x in enumerate(v):
        if x is None:
            continue
        prev = x if prev is None else a * x + (1 - a) * prev
        out[i] = prev
    return out


def rma(v, n):
    out = [None] * len(v)
    s = 0.0
    prev = None
    for i in range(len(v)):
        x = 0.0 if v[i] is None else v[i]
        s += x
        if i >= n:
            s -= 0.0 if v[i - n] is None else v[i - n]
        if i == n - 1:
            prev = s / n
        elif i > n - 1:
            prev = (x + (n - 1) * prev) / n
        out[i] = prev if i >= n - 1 else None
    return out


def rsi(v, n):
    up = [None] * len(v)
    dn = [None] * len(v)
    for i in range(1, len(v)):
        c = v[i] - v[i - 1]
        up[i] = max(c, 0.0)
        dn[i] = -min(c, 0.0)
    ru, rd = rma(up, n), rma(dn, n)
    return [None if (u is None or d is None or (u == 0 and d == 0))
             else (100 if d == 0 else 100 - 100 / (1 + u / d))
             for u, d in zip(ru, rd)]


def linreg(v, n):
    out = [None] * len(v)
    sx = n * (n - 1) / 2
    sxx = n * (n - 1) * (2 * n - 1) / 6
    for i in range(n - 1, len(v)):
        sy = sxy = 0.0
        ok = True
        for j in range(n):
            y = v[i - n + 1 + j]
            if y is None:
                ok = False
                break
            sy += y
            sxy += j * y
        if not ok:
            continue
        slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
        out[i] = (sy - slope * sx) / n + slope * (n - 1)
    return out


def sma_strict(v, n):
    out = [None] * len(v)
    s = 0.0
    bad = 0
    for i in range(len(v)):
        if v[i] is None:
            bad += 1
        else:
            s += v[i]
        if i >= n:
            if v[i - n] is None:
                bad -= 1
            else:
                s -= v[i - n]
        out[i] = s / n if (i >= n - 1 and bad == 0) else None
    return out


def rolling_sma(v, n):
    return sma_strict(v, n)


def rolling_z(v, n):
    out = [None] * len(v)
    s = s2 = 0.0
    for i in range(len(v)):
        x = v[i]
        if x is not None:
            s += x
            s2 += x * x
        if i >= n:
            y = v[i - n]
            if y is not None:
                s -= y
                s2 -= y * y
        if i >= n - 1:
            mean = s / n
            var = s2 / n - mean * mean
            if var > 0:
                out[i] = ((0.0 if x is None else x) - mean) / math.sqrt(var)
    return out


def derive_lw(bars, crv):
    """LSMA x CRVOL: 返回 wave / wt2 / crvSlope / wt1。"""
    n = len(bars)
    hlc3 = [(b["high"] + b["low"] + b["close"]) / 3 for b in bars]
    vol = [b["volume"] or 0 for b in bars]

    e1 = ema(hlc3, LW["n1"])
    dev = [None if e1[i] is None else hlc3[i] - e1[i] for i in range(n)]
    e2 = ema([None if x is None else abs(x) for x in dev], LW["n1"])
    cci = [None if (dev[i] is None or not e2[i]) else dev[i] / (0.025 * e2[i])
           for i in range(n)]
    tci = [None if x is None else x + 50 for x in ema(cci, LW["n2"])]

    rmf = [hlc3[i] * vol[i] for i in range(n)]
    mf = [None] * n
    for i in range(LW["n3"] - 1, n):
        pos = neg = 0.0
        for j in range(max(i - LW["n3"] + 1, 1), i + 1):
            if hlc3[j] > hlc3[j - 1]:
                pos += rmf[j]
            elif hlc3[j] < hlc3[j - 1]:
                neg += rmf[j]
        mf[i] = 100 - 100 / (1 + (1e10 if neg == 0 else pos / neg))

    rsi3 = rsi(hlc3, LW["n3"])
    wt1 = [None if (tci[i] is None or mf[i] is None or rsi3[i] is None)
           else (tci[i] + mf[i] + rsi3[i]) / 3 for i in range(n)]
    wt2 = sma_strict(wt1, 6)
    wave = linreg(wt1, LW["n4"])

    reg = linreg(crv, LW["slopeLen"])
    crv_slope = [None if (reg[i] is None or i == 0 or reg[i - 1] is None)
                 else reg[i] - reg[i - 1] for i in range(n)]
    return {"wt1": wt1, "wt2": wt2, "wave": wave, "crvSlope": crv_slope}


def derive(bars, use_legacy=False):
    n = len(bars)
    vol = [b["volume"] for b in bars]
    buy = [b.get("buyLegacy" if use_legacy else "buy") for b in bars]
    sell = [b.get("sellLegacy" if use_legacy else "sell") for b in bars]
    delta = [None if (buy[i] is None or sell[i] is None) else buy[i] - sell[i]
             for i in range(n)]
    close = [b["close"] for b in bars]
    posd = [None if d is None else (d if d > 0 else 0.0) for d in delta]
    negd = [None if d is None else (d if d < 0 else 0.0) for d in delta]

    sma_vol20 = rolling_sma(vol, RELLEN)
    sma_pos20 = rolling_sma(posd, RELLEN)
    sma_neg20 = rolling_sma(negd, RELLEN)
    sma_buy20 = rolling_sma(buy, RELLEN)
    sma_sell20 = rolling_sma(sell, RELLEN)

    rvol = [None if not sma_vol20[i] else vol[i] / sma_vol20[i] for i in range(n)]
    rpos = [None if (posd[i] is None or not sma_pos20[i]) else posd[i] / sma_pos20[i]
            for i in range(n)]
    rneg = [None if (negd[i] is None or not sma_neg20[i]) else negd[i] / sma_neg20[i]
            for i in range(n)]
    rbuy = [None if (buy[i] is None or not sma_buy20[i]) else buy[i] / sma_buy20[i]
            for i in range(n)]
    rsell = [None if (sell[i] is None or not sma_sell20[i]) else sell[i] / sma_sell20[i]
             for i in range(n)]

    crv = [None] * n
    acc = 0.0
    for i in range(n):
        if rvol[i] is None:
            continue
        acc += rvol[i] if bars[i]["close"] > bars[i]["open"] else -rvol[i]
        crv[i] = acc

    out = dict(vol=vol, buy=buy, sell=sell, delta=delta, rvol=rvol, rpos=rpos,
               rneg=rneg, rbuy=rbuy, rsell=rsell, crv=crv,
               ema={p: ema(close, p) for p in EMA_PERIODS})
    out["lw"] = derive_lw(bars, crv)
    return out


# ---------- 评估工具 ----------
def hit_rate(signals, fwd, k):
    """signals[i] in {1,-1,None}; fwd[i] = close[i+k]-close[i]; 返回命中率与样本数。"""
    hits = tot = 0
    for i, s in enumerate(signals):
        if s is None or fwd[i] is None or fwd[i] == 0:
            continue
        tot += 1
        hits += 1 if s * fwd[i] > 0 else 0
    return (hits / tot if tot else None), tot


def crosses(a, b):
    """a 上穿 b 返回 1, 下穿返回 -1。"""
    out = [None] * len(a)
    for i in range(1, len(a)):
        if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
            continue
        if a[i - 1] <= b[i - 1] and a[i] > b[i]:
            out[i] = 1
        elif a[i - 1] >= b[i - 1] and a[i] < b[i]:
            out[i] = -1
    return out


def fmt_pct(x):
    return f"{x*100:.1f}%" if x is not None else "  -  "


def fmt_n(x):
    return str(x) if x is not None else "-"


def evaluate(bars, tf_label, k_list=(1, 3, 5, 10)):
    n = len(bars)
    d = derive(bars, use_legacy=False)
    d_leg = derive(bars, use_legacy=True)
    close = [b["close"] for b in bars]
    results = {}

    # 前向收益(到 k 根之后), 越界为 None
    fwd = {k: [None] * n for k in k_list}
    for k in k_list:
        for i in range(n - k):
            fwd[k][i] = close[i + k] - close[i]

    # 1) K线方向本身的前瞻持续性(基准)
    kline_dir = [1 if b["close"] > b["open"] else -1 for b in bars]
    results["K线方向(基准)"] = {f"前{ k}根": hit_rate(kline_dir, fwd[k], k) for k in k_list}

    # 2) Delta 方向
    results["Delta 净方向"] = {f"前{ k}根": hit_rate([None if x is None else (1 if x > 0 else -1)
                                               for x in d["delta"]], fwd[k], k)
                          for k in k_list}
    results["DeltaLegacy 净方向"] = {f"前{ k}根": hit_rate([None if x is None else (1 if x > 0 else -1)
                                                    for x in [(None if (b.get("buyLegacy") is None
                                                                       or b.get("sellLegacy") is None)
                                                              else b["buyLegacy"] - b["sellLegacy"])
                                                              for b in bars]], fwd[k], k)
                                for k in k_list}

    # 3) RVOL 放量脉冲: 超阈值 + K线方向
    for lvl_idx, name in [(0, "RVOL≥1.5级"), (1, "RVOL≥2.5级"), (2, "RVOL≥3.5级")]:
        th = MULT[lvl_idx]
        sig = [None if d["rvol"][i] is None or d["rvol"][i] < th else kline_dir[i]
               for i in range(n)]
        results[f"{name}脉冲跟进"] = {f"前{ k}根": hit_rate(sig, fwd[k], k) for k in k_list}

    # 4) CRVOL 斜率方向
    results["CRVOL 斜率方向"] = {f"前{ k}根": hit_rate([None if d["lw"]["crvSlope"][i] is None
                                               else (1 if d["lw"]["crvSlope"][i] > 0 else -1)
                                               for i in range(n)], fwd[k], k)
                          for k in k_list}

    # 5) LSMA: wave 上穿/下穿 wt2
    xw = crosses(d["lw"]["wave"], d["lw"]["wt2"])
    results["LSMA wave 穿 wt2"] = {f"前{ k}根": hit_rate(xw, fwd[k], k) for k in k_list}

    # 6) LSMA: wt2 进入超买/超卖区(反转)
    ob_sig = [None if d["lw"]["wt2"][i] is None else
              (-1 if d["lw"]["wt2"][i] > LW["ob"] else (1 if d["lw"]["wt2"][i] < LW["os"] else None))
              for i in range(n)]
    results["LSMA 超买超卖"] = {f"前{ k}根": hit_rate(ob_sig, fwd[k], k) for k in k_list}

    # 7) 判向算法 vs 当根 K线方向的一致率(不是预测, 是口径自洽)
    def agree(buy_key, sell_key):
        tot = hit = 0
        for i, b in enumerate(bars):
            bg, sl = b[buy_key], b[sell_key]
            if bg is None or sl is None or bg == sl:
                continue
            tot += 1
            hit += 1 if ((bg > sl) == (b["close"] > b["open"])) else 0
        return (hit / tot if tot else None), tot

    results["_agree_lr"] = agree("buy", "sell")
    results["_agree_legacy"] = agree("buyLegacy", "sellLegacy")

    # 8) Delta 方向 vs 当根 K线方向(同期一致率)
    def same_bar(delta_vals):
        tot = hit = 0
        for i in range(n):
            if delta_vals[i] is None or delta_vals[i] == 0:
                continue
            tot += 1
            hit += 1 if ((delta_vals[i] > 0) == (close[i] > bars[i]["open"])) else 0
        return (hit / tot if tot else None), tot

    results["_samebar_delta"] = same_bar(d["delta"])
    results["_samebar_deltaLegacy"] = same_bar(
        [None if (b.get("buyLegacy") is None or b.get("sellLegacy") is None)
         else b["buyLegacy"] - b["sellLegacy"] for b in bars])

    # 9) CVD 与价格的相关性(趋势确认力度)
    def corr(a, b):
        pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
        if len(pairs) < 5:
            return None, len(pairs)
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        vy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if vx == 0 or vy == 0:
            return None, len(pairs)
        return cov / (vx * vy), len(pairs)

    cvd = [b.get("cvd") for b in bars]
    crv = d["crv"]
    results["_corr_cvd_price"] = corr(cvd, close)
    results["_corr_crv_price"] = corr(crv, close)
    results["_corr_delta_price"] = corr(d["delta"], [close[i] - bars[i]["open"]
                                                    for i in range(n)])

    # 10) 数据质量: 覆盖标记分布 + 买卖量与成交量的守恒检查
    cov = {}
    for b in bars:
        cov[b["coverage"]] = cov.get(b["coverage"], 0) + 1
    results["_coverage"] = cov
    obs_err = []
    for b in bars:
        if b.get("unknown") is None:
            continue
        tot = (b["buy"] or 0) + (b["sell"] or 0) + (b["unknown"] or 0)
        if b["volume"]:
            obs_err.append(abs(tot - b["volume"]) / b["volume"])
    results["_vol_conserv_max"] = max(obs_err) if obs_err else None
    results["_unknown_ratio"] = (sum(b["unknown"] or 0 for b in bars)
                                 / sum(b["volume"] or 0 for b in bars)
                                 if any(b["volume"] for b in bars) else None)
    return results, d, d_leg, fwd


def session_filter(bars, since="2026-09-15 21:00"):
    """保留 2026-09-16 交易日的 bar(含 09-15 夜盘 21:00 起)。

    b.time 是后端加过 +8h 的北京墙钟数值, 用 utcfromtimestamp 解读才不重复加时区。
    """
    cutoff_dt = dt.datetime.strptime(since, "%Y-%m-%d %H:%M")
    cutoff = int(cutoff_dt.replace(tzinfo=dt.timezone.utc).timestamp())
    return [b for b in bars if b["time"] >= cutoff]


def bj_time(ts):
    """b.time -> 北京墙钟字符串(把数值当 UTC 读, 即北京墙钟)。"""
    return dt.datetime.utcfromtimestamp(ts)


def main():
    print("=" * 78)
    print("FlowScope 指标准确度评估 - 今天 2026-09-16(周三) KQ.m@SHFE.fu")
    print("=" * 78)
    for tf in (30, 10):
        raw = requests.get(API, params={"symbol": SYMBOL, "ltf": 0, "tf": tf},
                           timeout=20).json()
        bars = raw["bars"]
        sess = session_filter(bars)
        comp = sum(b["coverage"] == "complete" for b in sess)
        t0 = bj_time(sess[0]["time"]).strftime("%m-%d %H:%M")
        t1 = bj_time(sess[-1]["time"]).strftime("%m-%d %H:%M")
        print(f"\n### 主周期 {tf}s: 今日交易时段 {t0} ~ {t1}, {len(sess)} 根, "
              f"complete={comp}, partial={len(sess)-comp}")
        if len(sess) < 60:
            print("  今日样本不足, 跳过")
            continue

        results, d, d_leg, fwd = evaluate(sess, f"{tf}s")

        print(f"\n  {'指标信号':<20} " + " ".join(f"{f'前{k}根命中率':>10}" for k in (1, 3, 5, 10)))
        print(f"  {'-'*20} " + " ".join("-" * 10 for _ in (1, 3, 5, 10)))
        for name in ["K线方向(基准)", "Delta 净方向", "DeltaLegacy 净方向",
                     "RVOL≥1.5级脉冲跟进", "RVOL≥2.5级脉冲跟进", "RVOL≥3.5级脉冲跟进",
                     "CRVOL 斜率方向", "LSMA wave 穿 wt2", "LSMA 超买超卖"]:
            cells = " ".join(f"{fmt_pct(results[name][f'前{k}根'][0]):>10}"
                             f"({fmt_n(results[name][f'前{k}根'][1]):>4})"
                             for k in (1, 3, 5, 10))
            print(f"  {name:<20} {cells}")

        print("\n  判向口径自洽(主动方向 vs 当根 K线阴阳):")
        print(f"    Lee-Ready 新算法  一致率 {fmt_pct(results['_agree_lr'][0])} "
              f"n={results['_agree_lr'][1]}")
        print(f"    旧算法(自身盘口)  一致率 {fmt_pct(results['_agree_legacy'][0])} "
              f"n={results['_agree_legacy'][1]}")
        print(f"    Delta 同期对齐 K线  新 {fmt_pct(results['_samebar_delta'][0])} / "
              f"旧 {fmt_pct(results['_samebar_deltaLegacy'][0])}")

        print("\n  累计型指标与价格相关性(Pearson, 趋势确认力度):")
        print(f"    CVD  vs 收盘价  r={results['_corr_cvd_price'][0]:+.3f} "
              f"n={results['_corr_cvd_price'][1]}")
        print(f"    CRVOL vs 收盘价 r={results['_corr_crv_price'][0]:+.3f} "
              f"n={results['_corr_crv_price'][1]}")
        print(f"    Delta vs 实体    r={results['_corr_delta_price'][0]:+.3f} "
              f"n={results['_corr_delta_price'][1]}")

        print("\n  数据质量(今天):")
        print(f"    覆盖标记 {results['_coverage']}")
        print(f"    买卖量守恒偏差最大 {results['_vol_conserv_max']:.2e}"
              if results["_vol_conserv_max"] is not None else "    买卖量守恒 无样本")
        print(f"    未知量占比 {results['_unknown_ratio']*100:.2f}%"
              if results["_unknown_ratio"] is not None else "    未知量 无样本")

        # 末点读数(与页面对账)
        b = sess[-1]
        i = len(sess) - 1
        print(f"\n  最新 bar 读数(对账页面): 时间 {bj_time(b['time']):%H:%M:%S}"
              f"  C:{b['close']} V:{b['volume']:.0f}")
        print(f"    Delta={d['delta'][i]:+.0f}  CVD={b['cvd']:+.0f}  "
              f"RVOL={d['rvol'][i]:.2f}  CRVOL={d['crv'][i]:+.1f}")
        print(f"    LSMA wave={d['lw']['wave'][i]:.1f}  wt2={d['lw']['wt2'][i]:.1f}  "
              f"CRVOL斜率={d['lw']['crvSlope'][i]:+.2f}")


if __name__ == "__main__":
    main()
