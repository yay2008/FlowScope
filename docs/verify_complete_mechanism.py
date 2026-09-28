# -*- coding: utf-8 -*-
"""判定偏差成因: 快照跨 bar 边界错配 vs 真实漏量。

三个检验:
A. 分块放大: 把相邻 n 根 bar 的 observed/volume 各自求和后再比。若偏差来自
   "跨边界的量被记到相邻 bar", 相邻分块会把错配抵消掉, 相对偏差应大致按 1/n 衰减;
   若是随机漏 tick, 分块不会改善(漏掉的量不会回来)。
B. 等效秒数: 偏差中位 × bar 宽度。快照跨界的量约等于"卡在边界上的那半个快照间隔",
   所以该乘积应与 bar 宽度无关、且等于快照间隔的量级(约 0.2~0.5s)。漏量没有这个性质。
C. 累计漂移: 见 verify_complete_criterion.py(路径有界 = 不丢量)。

运行:
    .\\.venv\\Scripts\\python.exe .\\docs\\verify_complete_mechanism.py
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

API = "http://127.0.0.1:8000/api/history"


def fetch(symbol: str, tf: int) -> list[dict]:
    query = urllib.parse.urlencode({"symbol": symbol, "tf": tf, "ltf": 0})
    with urllib.request.urlopen(f"{API}?{query}", timeout=60) as resp:
        return json.load(resp)["bars"]


def median(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def block_deviation(rows: list[tuple[float, float]], n: int) -> tuple[float, float, int]:
    """rows: [(observed, volume)] -> (相对偏差中位, p90, 分块数)"""
    devs = []
    for start in range(0, len(rows), n):
        chunk = rows[start:start + n]
        if len(chunk) < n:
            break
        obs = sum(o for o, _ in chunk)
        vol = sum(v for _, v in chunk)
        if vol:
            devs.append(abs(obs - vol) / vol)
    devs.sort()
    if not devs:
        return float("nan"), float("nan"), 0
    return median(devs), devs[int(0.9 * (len(devs) - 1))], len(devs)


def main() -> int:
    print("=" * 92)
    print("偏差成因判定: 边界错配 vs 真实漏量")
    print("=" * 92)
    for symbol in ("KQ.m@SHFE.fu", "KQ.m@SHFE.rb"):
        for tf in (30, 10):
            bars = fetch(symbol, tf)
            rows = [(b["buy"] + b["sell"] + b["unknown"], b["volume"])
                    for b in bars
                    if b.get("buy") is not None and b.get("volume")]
            if len(rows) < 64:
                print(f"\n[{symbol} tf={tf}] 可用 bar 太少({len(rows)}), 跳过")
                continue
            base_med, base_p90, _ = block_deviation(rows, 1)
            print(f"\n[{symbol} tf={tf}s] 可用 bar {len(rows)} 根, "
                  f"bar 宽 {tf}s")
            print(f"    {'分块':>6} {'分块数':>7} {'相对偏差中位':>13} {'p90':>9} "
                  f"{'相对 1 根的倍数':>16}")
            for n in (1, 2, 4, 8, 16):
                med, p90, count = block_deviation(rows, n)
                if count < 8:
                    break
                ratio = med / base_med if base_med else float("nan")
                print(f"    {n:>4} 根 {count:>7} {med:>13.3%} {p90:>9.3%} {ratio:>16.2f}×")
            print(f"    => 等效秒数 = 偏差中位 × bar 宽 = {base_med * tf:.3f}s "
                  f"(1 根 bar 的分块), {block_deviation(rows,4)[0] * 4 * tf:.3f}s (4 根合并)")
            zero = sum(1 for b in bars if b.get("buy") is not None and b["volume"] == 0)
            print(f"    零成交 bar(volume==0, 判据必然通过): {zero} 根")
            print(f"    1 根分块 p90 = {base_p90:.2%} -> 任何 rtol < {base_p90:.2%} "
                  f"都会漏掉至少 10% 的 bar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
