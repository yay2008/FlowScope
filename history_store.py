"""分别保存核对通过和待核对的估算量；旧 CSV 保留 legacy 标记用于显示。"""
from pathlib import Path

import numpy as np
import pandas as pd


class HistoryStore:
    def __init__(self, path, legacy_path=None):
        self.path = Path(path)
        self.values = self._read(self.path)
        self.legacy = self._read(Path(legacy_path)) if legacy_path else {}
        self.estimated_path = self.path.with_name(self.path.stem + "_estimated.csv")
        self.estimates = self._read(self.estimated_path)
        self._index()

    @staticmethod
    def _read(path):
        if not path.exists():
            return {}
        frame = pd.read_csv(path)
        required = ["time", "buy", "sell"]
        if not set(required).issubset(frame.columns):
            raise ValueError(f"历史文件缺少列: {path.name}")
        frame = frame[required].apply(pd.to_numeric, errors="coerce").dropna()
        frame = frame[np.isfinite(frame).all(axis=1) & (frame[["buy", "sell"]] >= 0).all(axis=1)]
        return {int(r.time): (float(r.buy), float(r.sell)) for r in frame.itertuples(index=False)}

    def _index(self):
        self.times = np.array(sorted(self.values), dtype=np.int64)
        self.sums = np.r_[0., np.cumsum([self.values[t][0] - self.values[t][1] for t in self.times])]
        display = {**self.legacy, **self.estimates, **self.values}
        self.display_times = np.array(sorted(display), dtype=np.int64)
        self.display_sums = np.r_[0., np.cumsum([display[t][0] - display[t][1] for t in self.display_times])]

    def merge(self, bars):
        """完整实时观测优先；否则使用已确认记录，最后才使用旧格式估算。"""
        result = bars.copy()
        for values, quality in [(self.values, "complete"), (self.estimates, "partial"), (self.legacy, "legacy")]:
            mask = result.coverage.ne("complete") & result.time.isin(values)
            if quality != "complete":
                # 不用旧估算覆盖具备前置快照的新数据；窗口左边缘则优先回填已保存估算。
                mask &= ~result.get("hasBaseline", pd.Series(False, index=result.index))
                if quality == "legacy":
                    mask &= ~result.time.isin(self.estimates)
            times = result.loc[mask, "time"]
            if not times.empty:
                result.loc[mask, "buy"] = times.map(lambda t: values[int(t)][0])
                result.loc[mask, "sell"] = times.map(lambda t: values[int(t)][1])
                result.loc[mask, "coverage"] = quality
        result["delta"] = result["buy"] - result["sell"]
        return result

    def save_completed(self, bars):
        if bars.empty:
            return
        done = bars.iloc[:-1]
        confirmed = done.coverage.eq("complete")
        estimated = done.coverage.eq("partial") & (done.get("hasBaseline", False) | ~done.time.isin(self.estimates))
        changed = False
        for mask, values, path in [(confirmed, self.values, self.path),
                                   (estimated, self.estimates, self.estimated_path)]:
            updates = []
            for row in done[mask].itertuples(index=False):
                timestamp = int(row.time)
                pair = (float(row.buy), float(row.sell))
                if np.isfinite(pair).all() and values.get(timestamp) != pair:
                    updates.append((timestamp, *pair))
            if updates:
                pd.DataFrame(updates, columns=["time", "buy", "sell"]).to_csv(
                    path, mode="a", header=not path.exists(), index=False)
                for timestamp, buy, sell in updates:
                    values[timestamp] = (buy, sell)
                changed = True
        if changed:
            self._index()

    def with_cvd(self, bars):
        """显示 CVD 累计可用估算量；核对通过的累计量独立保留，二者均固定基准。"""
        result = bars.copy()
        before = np.searchsorted(self.times, result["time"].to_numpy(dtype=np.int64), side="left")
        result["cvdConfirmed"] = (self.sums[before] + result["delta"]).where(result.coverage.eq("complete"))
        before = np.searchsorted(self.display_times, result["time"].to_numpy(dtype=np.int64), side="left")
        available = result["delta"].notna()
        result["cvdOpen"] = pd.Series(self.display_sums[before], index=result.index).where(available)
        result["cvd"] = (result["cvdOpen"] + result["delta"]).where(available)
        return result

    @property
    def base(self):
        return int(self.display_times[0]) if len(self.display_times) else None
