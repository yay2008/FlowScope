# -*- coding: utf-8 -*-
"""回溯核对补编报告当日(2026-09-16)的 98.7% 数字。

主文件每行自带 source 列(complete / partial / legacy, 见 history_store.HEADER);
没有该列的旧行由 _upgrade_layout 补成 complete。_estimated.csv 是只读旧来源,
不参与判断, 这里不读它。

运行:
    .\\.venv\\Scripts\\python.exe .\\docs\\verify_complete_history.py
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from collections import defaultdict

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def day_of(time_s: int) -> str:
    """落盘 time 已是 +8h 的北京墙钟数值, 按 UTC 读即为北京日期。"""
    return dt.datetime.fromtimestamp(time_s, dt.timezone.utc).strftime("%Y-%m-%d")


def load(path: str) -> tuple[dict[str, list[int]], list[int]]:
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # [complete, partial]
    order: list[int] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                time_s = int(float(row["time"]))
            except (KeyError, TypeError, ValueError):
                continue
            source = (row.get("source") or "complete").strip() or "complete"
            order.append(time_s)
            idx = 1 if source == "partial" else 0      # legacy 单列, 不混入 complete
            by_day[day_of(time_s)][idx] += 1
    return by_day, order


def legacy_partial(path: str, main_times: set[int]) -> dict[str, int]:
    """source 列出现之前的旧设计: 主文件=核对通过, _estimated.csv=判为 partial 的估算行。

    这些行没有 source 列, 只能靠"在旧估算文件里且不在主文件里"识别。
    """
    estimated = path.replace(".csv", "_estimated.csv")
    by_day: dict[str, int] = defaultdict(int)
    if not os.path.exists(estimated):
        return by_day
    with open(estimated, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                time_s = int(float(row["time"]))
            except (KeyError, TypeError, ValueError):
                continue
            if time_s not in main_times:
                by_day[day_of(time_s)] += 1
    return by_day


def main() -> int:
    for name in ("KQ_m_SHFE_fu_30s_ltf0_v3", "KQ_m_SHFE_fu_10s_ltf0_v3",
                 "KQ_m_SHFE_rb_30s_ltf0_v3"):
        path = os.path.join(DATA, f"{name}.csv")
        if not os.path.exists(path):
            continue
        by_day, order = load(path)
        old_partial = legacy_partial(path, set(order))
        for day, count in old_partial.items():
            by_day[day][1] += count
        unsorted = sum(1 for a, b in zip(order, order[1:]) if b < a)
        print("=" * 84)
        print(f"### {name}   主文件行数 {len(order)}   乱序处 {unsorted}   "
              f"(09-17 前的 partial 来自旧 _estimated.csv)")
        print("=" * 84)
        print(f"  {'北京日期':<12} {'complete':>9} {'partial':>9} {'partial 占比':>13}")
        for day in sorted(by_day)[-16:]:
            complete, partial = by_day[day]
            total = complete + partial
            if total < 20:
                continue
            flag = "   <= 补编报告当日" if day == "2026-09-16" else ""
            print(f"  {day:<12} {complete:>9,} {partial:>9,} {partial/total:>12.1%}{flag}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
