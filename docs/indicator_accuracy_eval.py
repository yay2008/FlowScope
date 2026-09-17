# -*- coding: utf-8 -*-
"""FlowScope 指标对 2026-09-16 行情的“准确度”评估 (最终版).

结论性输出 + JSON。前视收益 = close[i] -> close[i+h]; 信号只用 i 及之前的数据。
同向基准 = 与该信号同阴阳类别的全部 bar 在同一 h 上的命中率, 用来剥离当日趋势。
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import defaultdict

import numpy as np

LEVELS = [1.5, 2.5, 3.5]
HORIZONS = [1, 3, 6, 12, 30]
RNG = np.random.default_rng(20260916)


def bj(ts):
    return (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(ts))).strftime("%m-%d %H:%M")


def hour_of(ts):
    return (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(ts))).hour


RECOMPUTE_JS = "docs/indicator_recompute.js"


def load(path, source="lr"):
    out = subprocess.run(["node", RECOMPUTE_JS, path, source, "0"],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)["bars"]


def block_bootstrap_p(hits, base_rate, h, rounds=4000):
    n = len(hits)
    block = max(1, min(h, n))
    nb = int(np.ceil(n / block))
    dev = abs(hits.mean() - base_rate)
    starts = RNG.integers(0, n, size=(rounds, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    sample = hits[idx.reshape(rounds, -1)[:, :n]]
    centered = sample - sample.mean(axis=1, keepdims=True) + base_rate
    return (int((np.abs(centered.mean(axis=1) - base_rate) >= dev).sum()) + 1) / (rounds + 1)


class Eval:
    def __init__(self, rows):
        self.rows = rows
        self.time = np.array([r["time"] for r in rows], dtype=np.int64)
        self.close = np.array([r["c"] for r in rows], dtype=float)
        self.open = np.array([r["o"] for r in rows], dtype=float)
        self.up = self.close > self.open
        self.sig = defaultdict(dict)

    def add(self, name, idx, direction):
        for i in idx:
            self.sig[name][int(i)] = direction

    def fwd(self, i, h):
        j = i + h
        return None if j >= len(self.close) else self.close[j] - self.close[i]

    def base(self, h, direction, sel=None):
        m = self.up if direction == 1 else ~self.up
        if sel is not None:
            m = m & np.array([i in sel for i in range(len(self.close))])
        r = np.array([self.fwd(i, h) for i in np.flatnonzero(m) if self.fwd(i, h) is not None])
        return (float(r.mean()), float((r > 0).mean()), len(r)) if len(r) else (None, None, 0)

    def stats(self, name, h, sel):
        pts = [(i, d) for i, d in self.sig[name].items() if i in sel]
        if len(pts) < 5:
            return None
        rets = np.array([self.fwd(i, h) * d for i, d in pts if self.fwd(i, h) is not None])
        hits = (rets > 0).astype(float)
        dirs = [d for i, d in pts if self.fwd(i, h) is not None]
        b1, b0 = self.base(h, 1, sel), self.base(h, -1, sel)
        if len(set(dirs)) == 1:
            br, bm = (b1[1], b1[0]) if dirs[0] == 1 else (b0[1], b0[0])
        else:
            w = dirs.count(1) / len(dirs)
            br = w * b1[1] + (1 - w) * b0[1]
            bm = w * b1[0] + (1 - w) * b0[0]
        return {"n": int(len(rets)), "hit": float(hits.mean()), "base": float(br),
                "lift": float(hits.mean() - br), "p": block_bootstrap_p(hits, br, h),
                "mean_ret": float(rets.mean()), "base_mean": float(bm)}


def build(rows, minutes=5):
    """minutes: 事件去重的最小间隔(分钟), 用于把成一串的信号合并成一个“事件”。"""
    n = len(rows)
    ev = Eval(rows)
    g = lambda k, i: rows[i].get(k)
    nz = lambda x: x is not None and np.isfinite(x)
    R = range(n)

    def events(idx):
        out, last = [], None
        for i in sorted(idx):
            if last is None or rows[i]["time"] - last >= minutes * 60:
                out.append(i)
                last = rows[i]["time"]
        return out

    groups = {
        "RVOL≥1.5 阳线": ([i for i in R if nz(g("rvol", i)) and g("rvol", i) >= 1.5 and rows[i]["c"] > rows[i]["o"]], 1),
        "RVOL≥2.5 阳线": ([i for i in R if nz(g("rvol", i)) and g("rvol", i) >= 2.5 and rows[i]["c"] > rows[i]["o"]], 1),
        "RVOL≥1.5 阴线": ([i for i in R if nz(g("rvol", i)) and g("rvol", i) >= 1.5 and rows[i]["c"] <= rows[i]["o"]], -1),
        "Delta买脉冲≥1.5": ([i for i in R if nz(g("rpos", i)) and g("rpos", i) >= 1.5], 1),
        "Delta买脉冲≥2.5": ([i for i in R if nz(g("rpos", i)) and g("rpos", i) >= 2.5], 1),
        "Delta卖脉冲≤-1.5": ([i for i in R if nz(g("rneg", i)) and g("rneg", i) <= -1.5], -1),
        "CRVOL 3根↑": ([i for i in R if i >= 3 and nz(g("crv", i)) and nz(g("crv", i - 3)) and g("crv", i) - g("crv", i - 3) > 0], 1),
        "CRVOL 3根↓": ([i for i in R if i >= 3 and nz(g("crv", i)) and nz(g("crv", i - 3)) and g("crv", i) - g("crv", i - 3) < 0], -1),
        "CVD 3根↑": ([i for i in R if i >= 3 and nz(g("cvd", i)) and nz(g("cvd", i - 3)) and g("cvd", i) - g("cvd", i - 3) > 0], 1),
        "CVD 3根↓": ([i for i in R if i >= 3 and nz(g("cvd", i)) and nz(g("cvd", i - 3)) and g("cvd", i) - g("cvd", i - 3) < 0], -1),
        "LSMA波↑": ([i for i in R if i >= 1 and nz(g("wave", i)) and nz(g("wave", i - 1)) and g("wave", i) > g("wave", i - 1)], 1),
        "LSMA波↓": ([i for i in R if i >= 1 and nz(g("wave", i)) and nz(g("wave", i - 1)) and g("wave", i) < g("wave", i - 1)], -1),
        "LSMA↑&斜率>0": ([i for i in R if i >= 1 and nz(g("wave", i)) and nz(g("wave", i - 1)) and nz(g("crvSlope", i))
                          and g("wave", i) > g("wave", i - 1) and g("crvSlope", i) > 0], 1),
        "WT2<20 超卖": ([i for i in R if nz(g("wt2", i)) and g("wt2", i) < 20], 1),
        "WT2>80 超买": ([i for i in R if nz(g("wt2", i)) and g("wt2", i) > 80], -1),
    }
    ev.events = {}
    for name, (idx, d) in groups.items():
        ev.add(name, idx, d)
        ev.events[name] = events(idx)
    return ev


def sel_of(ev, keep):
    return {i for i in range(ev.time.size) if keep(ev.time[i])}


def print_table(ev, title, keep, out_json=None):
    sel = sel_of(ev, keep)
    ids = sorted(sel)
    lines = [f"\n### {title}  (bar {bj(ev.time[ids[0]])}~{bj(ev.time[ids[-1]])}, n={len(ids)}, "
             f"价格 {ev.close[ids[0]]:.0f}→{ev.close[ids[-1]]:.0f} {(ev.close[ids[-1]]/ev.close[ids[0]]-1)*100:+.2f}%)"]
    lines.append(f"{'信号':<18}{'根数':>5} | " + " | ".join(f"{'%+d根'%h:>15}" for h in HORIZONS))
    lines.append("-" * 100)
    recs = []
    for name in ev.sig:
        cells, row = [], {"signal": name}
        for h in HORIZONS:
            s = ev.stats(name, h, sel)
            if s is None:
                cells.append(f"{'-':>15}")
                continue
            mark = "*" if s["p"] < 0.05 else " "
            cells.append(f"{s['hit']*100:5.1f}%{mark}({s['base']*100:4.1f})")
            row[f"h{h}"] = s
        lines.append(f"{name:<18}{sum(1 for i in ev.sig[name] if i in sel):>5} | " + " | ".join(cells))
        recs.append(row)
    b1 = {h: ev.base(h, 1, sel) for h in HORIZONS}
    b0 = {h: ev.base(h, -1, sel) for h in HORIZONS}
    lines.append("-" * 100)
    lines.append(f"{'阳线基准':<18}{b1[HORIZONS[0]][2]:>5} | " + " | ".join(f"{b1[h][1]*100:14.1f}%" for h in HORIZONS))
    lines.append(f"{'阴线基准':<18}{b0[HORIZONS[0]][2]:>5} | " + " | ".join(f"{b0[h][1]*100:14.1f}%" for h in HORIZONS))
    lines.append("格式: 命中率(同向基准, 基准同样只取本窗口)  * = 块 bootstrap p<0.05")
    txt = "\n".join(lines)
    print(txt)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump({"title": title, "base_up": b1, "base_dn": b0, "rows": recs}, fh,
                      ensure_ascii=False, indent=1)
    return txt


def coverage_report(rows, keep, label):
    sel = [r for r in rows if keep(r["time"])]
    cov = defaultdict(int)
    for r in sel:
        cov[r.get("coverage")] += 1
    zeros = sum(1 for r in sel if (r["buy"] == 0 or r["sell"] == 0))
    print(f"\n[数据质量] {label}: n={len(sel)} 覆盖={dict(cov)} | "
          f"买或卖为 0 的 bar={zeros} ({zeros/len(sel)*100:.1f}%)")
    ohlc_bad = sum(1 for r in sel if r["h"] < r["l"] or not (r["l"] <= r["o"] <= r["h"]) or not (r["l"] <= r["c"] <= r["h"]))
    print(f"            OHLC 自洽性异常={ohlc_bad} | 成交量>0 的 bar={sum(1 for r in sel if r['v'] > 0)}")
    return {"n": len(sel), "coverage": dict(cov), "one_sided": zeros}


def corr_delta_price(rows, keep, label):
    sel = [r for r in rows if keep(r["time"])]
    d = np.array([r["delta"] for r in sel], dtype=float)
    p = np.array([r["c"] - r["o"] for r in sel], dtype=float)
    ok = np.isfinite(d) & np.isfinite(p)
    print(f"[同步性] {label}: corr(delta, 本根涨跌) = {np.corrcoef(d[ok], p[ok])[0,1]:.3f}  "
          f"corr(CVD变化, 价格变化) = {np.corrcoef(np.diff(np.array([r['cvd'] for r in sel], dtype=float)), np.diff(p))[0,1]:.3f}")


def main():
    date_of = lambda t: (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(t))).strftime("%m-%d")
    day = lambda t: date_of(t) == "09-16" and 9 <= hour_of(t) < 15
    d0915 = lambda t: date_of(t) == "09-15" and 9 <= hour_of(t) < 15

    print("=" * 108)
    print("FlowScope 指标准确度评估 —— 2026-09-16 日盘 (09:00-10:04, 数据窗口上限)")
    print("=" * 108)

    k10 = load(".tmp-test/api_k10_ltf5.json")
    t10 = load(".tmp-test/api_t10.json")
    k30 = load(".tmp-test/api_k30.json")

    coverage_report(k10, day, "10s K线口径(ltf=5) 今日日盘")
    coverage_report(t10, day, "10s tick口径 今日日盘")
    corr_delta_price(k10, day, "10s K线口径")
    corr_delta_price(t10, day, "10s tick口径")

    ev10 = build(k10)
    ev10t = build(t10)
    ev30 = build(k30)

    txt = []
    txt.append(print_table(ev10, "A. 10s / K线口径 ltf=5 —— 今日日盘", day, ".tmp-test/final_day10_kline.json"))
    txt.append(print_table(ev10t, "B. 10s / tick口径 Lee-Ready —— 今日日盘", day, ".tmp-test/final_day10_tick.json"))
    txt.append(print_table(ev30, "C. 30s / K线口径 ltf=10 —— 今日日盘", day, ".tmp-test/final_day30_kline.json"))
    txt.append(print_table(ev30, "D. 30s 对照窗口: 09-15 日盘 (上一个交易日, 同一批指标)", d0915,
                           ".tmp-test/final_prev_day30.json"))
    with open(".tmp-test/final_report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(txt))


if __name__ == "__main__":
    main()
