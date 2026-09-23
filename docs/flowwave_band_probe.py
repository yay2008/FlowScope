"""FlowWave 主图叠加(回归通道)的设计依据探针 —— 只读, 不修改任何运行时代码。

问题: FlowWave 的 wave/wt2 是 0~100 的振荡值, 能不能像布林带一样画到主图(价格轴)?
本脚本用真实 30s 快照回答两件事:
  1. wave/wt2 的取值范围(是否真的被约束在 0~100)与触轨频率;
  2. 两类映射方案的量化对比:
     A. 振荡值线性映射到滚动价格区间(伪布林带) —— 带线几乎不动, 包含率也远低于布林带;
     B. 价格回归通道(本项目的选择) —— linreg(close,21) ± k 倍回归残差标准差,
        包含率与布林带同量级, 且轨道本身有价格含义。
  3. wt2 越界(上穿 80 / 下穿 20)的稀疏度, 用来判断"打点"会不会糊图。

用法:
    .\\.venv\\Scripts\\python.exe docs\\flowwave_band_probe.py [derived.json]

输入是 `/api/history` 的快照经 `static/app.js` 的 derive 逻辑复算后的 JSON
(数组, 每项含 time/open/high/low/close/volume 与 lsma(=wave)/wt2 等字段)。
默认读取 .run-tmp/evaluation-2026-09-16/derived-30s.json(该目录在 .gitignore 里, 可能被清理);
需要重新生成时按 docs/indicator-accuracy-2026-09-16.md 的步骤先取快照再复算。
"""

import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / ".run-tmp" / "evaluation-2026-09-16" / "derived-30s.json"


def pct(vals, q):
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def dist(name, vals):
    xs = [v for v in vals if v is not None]
    print(f"  {name:>4}: n={len(xs):3d} min={min(xs):8.2f} p1={pct(xs, .01):7.2f} "
          f"p50={pct(xs, .5):7.2f} p99={pct(xs, .99):7.2f} max={max(xs):8.2f}")


def linreg_vals(v, w):
    """与 static/app.js 的 linreg 同一个公式: 最近 w 点拟合线在末点的取值。"""
    out = [None] * len(v)
    sx = w * (w - 1) / 2
    sxx = w * (w - 1) * (2 * w - 1) / 6
    for i in range(w - 1, len(v)):
        sy = sum(v[i - w + 1:i + 1])
        sxy = sum(j * v[i - w + 1 + j] for j in range(w))
        slope = (w * sxy - sx * sy) / (w * sxx - sx * sx)
        out[i] = (sy - slope * sx) / w + slope * (w - 1)
    return out


def share(inside, tot):
    """包含率: 样本太少(快照很小或全被预热吃掉)时返回不可用, 不强行给数。"""
    if tot <= 0:
        return "样本不足"
    return f"{inside/tot*100:5.1f}%"


def main(argv):
    src = Path(argv[1]) if len(argv) > 1 else DEFAULT_SRC
    if not src.exists():
        raise SystemExit(f"找不到输入: {src}\n先按 docs/indicator-accuracy-2026-09-16.md 生成快照并复算。")
    bars = json.loads(src.read_text(encoding="utf-8"))
    if isinstance(bars, dict):
        bars = bars.get("bars", [])
    n = len(bars)
    if n < 60:
        raise SystemExit(f"输入只有 {n} 根 bar, 不够覆盖最长 50 根窗口 + 预热, 无法复算。")
    waves = [b.get("lsma", b.get("wave")) for b in bars]
    wt2s = [b.get("wt2") for b in bars]
    close = [b["close"] for b in bars]
    high = [b["high"] for b in bars]
    low = [b["low"] for b in bars]
    print(f"样本: {src.name}, {n} 根 30s bar")

    print("\n[1] FlowWave 取值范围(是否真的被约束在 0~100)")
    dist("wave", waves)
    dist("wt2", wt2s)
    for name, xs in (("wave", waves), ("wt2", wt2s)):
        xs = [v for v in xs if v is not None]
        if not xs:
            continue
        print(f"  {name}: >100 占 {sum(v > 100 for v in xs)/len(xs)*100:5.1f}%   "
              f"<0 占 {sum(v < 0 for v in xs)/len(xs)*100:5.1f}%   "
              f">80 占 {sum(v > 80 for v in xs)/len(xs)*100:5.1f}%   "
              f"<20 占 {sum(v < 20 for v in xs)/len(xs)*100:5.1f}%")

    print("\n[2] 方案A(未采用): 振荡值线性映射到滚动价格区间 price(L) = Pmin + L/100*(Pmax-Pmin)")
    print("    '带线完全不动' = 与上一根的上下轨一模一样(极值没被刷新), 也就是阶梯而不是曲线")
    for W in (20, 50, 100, 200):
        inside = above = below = tot = flat = 0
        for i in range(W - 1, n):
            pmin, pmax = min(low[i - W + 1:i + 1]), max(high[i - W + 1:i + 1])
            if pmax == pmin:
                continue
            up, dn, c = pmin + 0.8 * (pmax - pmin), pmin + 0.2 * (pmax - pmin), close[i]
            tot += 1
            if c > up:
                above += 1
            elif c < dn:
                below += 1
            else:
                inside += 1
            if i > W and min(low[i - W:i]) == pmin and max(high[i - W:i]) == pmax:
                flat += 1
        print(f"    W={W:3d}  收盘在带内 {share(inside, tot)}  高于上轨 {share(above, tot)}  "
              f"低于下轨 {share(below, tot)}   带线不动 {share(flat, tot)}")

    print("\n[3] 方案B(本项目采用): 价格回归通道 linreg(close, W) ± k 倍回归残差标准差")
    for W in (21, 50):
        mid = linreg_vals(close, W)
        resid = [None if mid[i] is None else close[i] - mid[i] for i in range(n)]
        for k in (1.5, 2.0, 2.5):
            inside = tot = 0
            for i in range(2 * W, n):
                sd = st.pstdev([r for r in resid[i - W + 1:i + 1] if r is not None])
                if not sd or mid[i] is None:
                    continue
                tot += 1
                if abs(resid[i]) <= k * sd:
                    inside += 1
            print(f"    linreg({W}) ±{k}σ  收盘在带内 {share(inside, tot)}")
    inside = tot = 0
    for i in range(20, n):
        w = close[i - 19:i + 1]
        m, sd = st.fmean(w), st.pstdev(w)
        if not sd:
            continue
        tot += 1
        if abs(close[i] - m) <= 2 * sd:
            inside += 1
    print(f"    对照 布林带(20, 2σ)            收盘在带内 {share(inside, tot)}")

    print("\n[4] 打点密度: wt2 首次越界的次数(连续越界只算第一根)")
    up_ev = down_ev = 0
    for i in range(n):
        if wt2s[i] is None:
            continue
        prev = wt2s[i - 1] if i > 0 else None
        if wt2s[i] > 80 and (prev is None or prev <= 80):
            up_ev += 1
        if wt2s[i] < 20 and (prev is None or prev >= 20):
            down_ev += 1
    print(f"    {n} 根里 上穿 80 {up_ev} 次, 下穿 20 {down_ev} 次")


if __name__ == "__main__":
    main(sys.argv)
