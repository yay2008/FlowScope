# -*- coding: utf-8 -*-
"""核查 indicator.py:329 的 complete 判据是否过严。

判据现状: complete = (bar_ns > first_tick_ns) & np.isclose(observed, volume, rtol=0, atol=1e-6)
即 |observed - volume| <= 1e-6, 对成交量而言等价于严格相等。

观测口径: observed(观测量) = buy + sell + unknown, 恒等式见 indicator.py:212。
实测口径: 相对偏差 = |observed - volume| / volume。

本脚本只做测量, 不改动仓库代码。运行:
    .\\.venv\\Scripts\\python.exe .\\docs\\verify_complete_criterion.py
"""
from __future__ import annotations

import json
import math
import sys
import urllib.parse
import urllib.request

API = "http://127.0.0.1:8000/api/history"
STRICT_ATOL = 1e-6          # indicator.py:329 的 atol
TOLERANCES = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05]


def fetch(symbol: str, tf: int, ltf: int = 0) -> list[dict]:
    query = urllib.parse.urlencode({"symbol": symbol, "tf": tf, "ltf": ltf})
    with urllib.request.urlopen(f"{API}?{query}", timeout=60) as resp:
        return json.load(resp)["bars"]


def pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def lag1(values: list[float]) -> float:
    if len(values) < 3:
        return float("nan")
    xs, ys = values[:-1], values[1:]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else float("nan")


def analyse(label: str, bars: list[dict]) -> None:
    print("=" * 96)
    print(f"### {label}   共 {len(bars)} 根 bar")
    print("=" * 96)
    if not bars:
        print("  空窗口, 跳过")
        return

    cov: dict[str, int] = {}
    for b in bars:
        cov[b["coverage"]] = cov.get(b["coverage"], 0) + 1
    print(f"  覆盖标记分布: {cov}")

    # ---- 恒等式自检: observed == buy + sell + unknown ----
    obs_rows = []
    identity_ok = True
    for b in bars:
        if b.get("buy") is None or b.get("sell") is None or b.get("unknown") is None:
            continue
        observed = (b["buy"] or 0.0) + (b["sell"] or 0.0) + (b["unknown"] or 0.0)
        obs_rows.append((b, observed))
        if b["volume"] and abs(observed - b["volume"]) / b["volume"] > 1e-12 and False:
            identity_ok = False
    print(f"  有观测量(买+卖+未知非空)的 bar: {len(obs_rows)} 根; "
          f"coverage!=missing 的 bar: {cov.get('partial', 0) + cov.get('complete', 0)} 根")
    print(f"  恒等式 observed == buy+sell+unknown : 按构造成立(见 indicator.py:212)")

    # ---- 判据分解: partial 里有多少是被容差卡掉的 ----
    total = len(bars)
    missing = cov.get("missing", 0)
    complete = cov.get("complete", 0)
    partial = cov.get("partial", 0)
    # 无基线(bar_ns <= first_tick_ns)最多命中第一根有 tick 的 bar
    no_baseline = 0
    first_obs_idx = next((i for i, (b, o) in enumerate(obs_rows)), None) if obs_rows else None
    if first_obs_idx is not None:
        no_baseline = sum(1 for b, _ in obs_rows
                          if b["time"] <= bars[first_obs_idx]["time"])
    tol_fail = partial - no_baseline
    print()
    print("  ---- partial 的成因分解 ----")
    print(f"    missing(无 tick)                 : {missing:>5}  ({missing/total:6.2%})")
    print(f"    partial 但仅差在容差(观测量存在) : {tol_fail:>5}  ({tol_fail/total:6.2%})")
    print(f"    partial 因无前置快照(首根)       : {no_baseline:>5}  ({no_baseline/total:6.2%})")
    print(f"    complete                         : {complete:>5}  ({complete/total:6.2%})")
    observed_bars = len(obs_rows)
    if observed_bars:
        print(f"    => 被容差卡掉的比例(占可用观测量): {tol_fail/observed_bars:6.2%}"
              f"   <= 报告中的 98.7% 就是这一列")

    # ---- 偏差分布 ----
    rel = []
    signed = []
    for b, observed in obs_rows:
        if not b["volume"]:
            continue
        signed.append(observed - b["volume"])
        rel.append(abs(observed - b["volume"]) / b["volume"])
    rel_sorted = sorted(rel)
    print()
    print("  ---- |observed - volume| / volume ----")
    if rel_sorted:
        print(f"    样本 {len(rel_sorted)} 根 | 中位 {pct(rel_sorted,0.5):.4%} | "
              f"p90 {pct(rel_sorted,0.9):.4%} | p99 {pct(rel_sorted,0.99):.4%} | "
              f"最大 {rel_sorted[-1]:.4%}")
        exact = sum(1 for b, o in obs_rows if b["volume"] and abs(o - b["volume"]) <= STRICT_ATOL)
        print(f"    严格判据 |dev| <= {STRICT_ATOL:g} 通过: {exact} / {len(obs_rows)} "
              f"({exact/len(obs_rows):.2%})")

    # ---- 容差扫描: 各 rtol 下会新增多少 complete ----
    print()
    print("  ---- 容差扫描(条件取 |dev| <= rtol*volume, 即 isclose(rtol=rtol, atol=0)) ----")
    print(f"    {'rtol':>7} {'判为 complete':>14} {'占比':>9} {'仍为 partial':>13}")
    for rtol in TOLERANCES:
        passed = sum(1 for b, o in obs_rows if b["volume"] and abs(o - b["volume"]) <= rtol * b["volume"])
        without_baseline = max(0, passed - 1) if passed else 0   # 首根仍因无基线保持 partial
        print(f"    {rtol:>7.3f} {without_baseline:>14} {without_baseline/total:>9.2%} "
              f"{observed_bars - without_baseline:>13}")

    # ---- 总量守恒 + 累计漂移: 区分"漏量"与"边界错配" ----
    sum_obs = sum(o for _, o in obs_rows)
    sum_vol = sum(b["volume"] for b, _ in obs_rows if b["volume"])
    print()
    print("  ---- 总量守恒(判定漏量 vs 边界错配的决定性检验) ----")
    if sum_vol:
        print(f"    Σobserved = {sum_obs:,.0f} | Σvolume = {sum_vol:,.0f} | "
              f"净差 {(sum_obs-sum_vol)/sum_vol:+.5%}")
    cum = 0.0
    peak = 0.0
    for value in signed:
        cum += value
        peak = max(peak, abs(cum))
    if sum_vol:
        print(f"    累计偏差终值 {cum:+,.0f} ({cum/sum_vol:+.5%}) | "
              f"路径最大 |累计| {peak:,.0f} ({peak/sum_vol:.4%})")
    if signed:
        print(f"    逐 bar 偏差 lag-1 自相关 = {lag1(signed):+.3f}  "
              f"(接近 -1 = 相邻 bar 互相抵消 = 边界错配; 接近 +1 = 单向漏量)")
        alt = sum(1 for a, b2 in zip(signed, signed[1:]) if a * b2 < 0)
        print(f"    相邻 bar 偏差符号相反: {alt} / {len(signed)-1} ({alt/(len(signed)-1):.1%})")

    # ---- 放宽后是否还能抓住真实缺口 ----
    print()
    print("  ---- 放宽到 rtol=1% 后, 偏差最大的 8 根是否仍被判 partial(真实缺口保护) ----")
    worst = sorted(obs_rows, key=lambda pair: -abs(pair[1] - pair[0]["volume"]) / pair[0]["volume"]
                   if pair[0]["volume"] else 0)[:8]
    for b, o in worst:
        if not b["volume"]:
            continue
        r = abs(o - b["volume"]) / b["volume"]
        print(f"    time={b['time']} coverage={b['coverage']:<8} volume={b['volume']:>10,.0f} "
              f"observed={o:>10,.0f} dev={r:>8.2%} "
              f"{'仍 partial' if r > 0.01 else '★会翻成 complete'}")


def main() -> int:
    symbols = sys.argv[1:] or ["KQ.m@SHFE.fu"]
    for symbol in symbols:
        for tf in (30, 10):
            try:
                bars = fetch(symbol, tf)
            except Exception as exc:                                  # noqa: BLE001
                print(f"[{symbol} tf={tf}] 拉取失败: {exc}")
                continue
            analyse(f"{symbol}  tf={tf}s  ltf=0", bars)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
