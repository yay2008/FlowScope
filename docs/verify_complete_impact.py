# -*- coding: utf-8 -*-
"""核查建议的落地效果: cvdConfirmed 可用性、偏差与 bar 量的关系、历史落盘分布。

回答三个问题:
1. 现在有多少 bar 的 cvdConfirmed 非空(用户能不能用这条线)?
2. 放宽到 rtol=0.01 之后还剩多少 partial(建议够不够)?
3. 偏差是不是主要由"低量 bar 的边界效应"贡献(相对容差是不是合适的工具)?

运行:
    .\\.venv\\Scripts\\python.exe .\\docs\\verify_complete_impact.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import urllib.parse
import urllib.request

API = "http://127.0.0.1:8000/api/history"
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def fetch(symbol: str, tf: int, ltf: int = 0) -> dict:
    query = urllib.parse.urlencode({"symbol": symbol, "tf": tf, "ltf": ltf})
    with urllib.request.urlopen(f"{API}?{query}", timeout=60) as resp:
        return json.load(resp)


def quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def section_live(symbol: str, tf: int) -> None:
    payload = fetch(symbol, tf)
    bars = payload["bars"]
    if not bars:
        print(f"  [{symbol} tf={tf}] 空窗口")
        return
    has_cvd_conf = "cvdConfirmed" in bars[-1]
    from collections import Counter
    cov = Counter(b["coverage"] for b in bars)
    conf = sum(1 for b in bars if b.get("cvdConfirmed") is not None)
    cvd = sum(1 for b in bars if b.get("cvd") is not None)
    print(f"  [{symbol} tf={tf}s] {len(bars)} 根 | 覆盖 {dict(cov)}")
    if has_cvd_conf:
        print(f"      cvd 非空 {cvd} 根 ({cvd/len(bars):.1%}) | "
              f"cvdConfirmed 非空 {conf} 根 ({conf/len(bars):.1%})"
              f"   <= 建议要救的就是这一列")
    else:
        print("      响应里没有 cvdConfirmed 字段")

    rows = [(b, (b["buy"] or 0) + (b["sell"] or 0) + (b["unknown"] or 0))
            for b in bars if b.get("buy") is not None and b["volume"]]
    if not rows:
        return

    # 偏差 vs bar 量: 分位分组
    ordered = sorted(rows, key=lambda pair: pair[0]["volume"])
    buckets = 5
    print(f"      {'按 volume 五分位':<16} {'bar 量中位':>10} {'偏差中位':>10} {'偏差p90':>10} "
          f"{'rtol=1% 通过率':>14}")
    for i in range(buckets):
        chunk = ordered[i * len(ordered) // buckets:(i + 1) * len(ordered) // buckets]
        if not chunk:
            continue
        vols = [b["volume"] for b, _ in chunk]
        devs = [abs(o - b["volume"]) / b["volume"] for b, o in chunk]
        ok = sum(1 for b, o in chunk if abs(o - b["volume"]) <= 0.01 * b["volume"])
        lo, hi = int(vols[0]), int(vols[-1])
        print(f"      Q{i+1} [{lo:>7,}~{hi:>7,}] {quantile(vols,0.5):>10,.0f} "
              f"{quantile(devs,0.5):>10.1%} {quantile(devs,0.9):>10.1%} {ok/len(chunk):>13.1%}")

    # 放宽后 cvdConfirmed 还能连成线吗
    for rtol in (0.01, 0.02, 0.05):
        passed = sum(1 for b, o in rows if abs(o - b["volume"]) <= rtol * b["volume"])
        print(f"      rtol={rtol:<5} -> complete {passed}/{len(bars)} ({passed/len(bars):.1%}), "
              f"cvdConfirmed 仍有 {len(bars)-passed} 根断点")


def section_store() -> None:
    print()
    print("---- 历史落盘证据: 已核对(complete) vs 估算(partial) 行数 ----")
    for path in sorted(glob.glob(os.path.join(DATA, "*.csv"))):
        name = os.path.basename(path)
        if name.endswith("_estimated.csv") or "_v2" in name or "_v3_estimated" in name:
            continue
        estimated = path.replace(".csv", "_estimated.csv")
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                head = handle.readline().strip().split(",")
                rows = sum(1 for _ in handle)
        except OSError:
            continue
        est_rows = 0
        if os.path.exists(estimated):
            with open(estimated, newline="", encoding="utf-8") as handle:
                handle.readline()
                est_rows = sum(1 for _ in handle)
        total = rows + est_rows
        if total:
            print(f"  {name:<52} complete={rows:>6,}  partial估算={est_rows:>6,}  "
                  f"partial 占比={est_rows/total:>6.1%}")


def main() -> int:
    print("=" * 96)
    print("建议落地效果核查")
    print("=" * 96)
    for symbol in ("KQ.m@SHFE.fu", "KQ.m@SHFE.rb"):
        for tf in (30, 10):
            try:
                section_live(symbol, tf)
            except Exception as exc:                                   # noqa: BLE001
                print(f"  [{symbol} tf={tf}] 失败: {exc}")
            print()
    section_store()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
