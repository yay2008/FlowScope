"""分别保存核对通过和待核对的估算量；旧 CSV 保留 legacy 标记用于显示。

主文件(v3)保存当前算法的 buy/sell/unknown, 并列保存旧算法对照列
(buyLegacy/sellLegacy)。旧算法的 CSV(v2 及更早格式)只作为回填来源:
它们里面的 buy/sell 本身就是旧算法口径, 读进来按 legacy 覆盖标记显示,
不会被误当成新算法结果, 也永远不会被改写。
"""
from pathlib import Path

import numpy as np
import pandas as pd

from indicator import EXTRA_COLUMNS


def _norm(value):
    """NaN/缺失 -> None; 用于让"未记录"和"空值"在比较时相等(NaN != NaN)。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


class HistoryStore:
    def __init__(self, path, legacy_path=None):
        self.path = Path(path)
        self.values, extra = self._read(self.path)
        self.legacy = {}
        for item in ([legacy_path] if isinstance(legacy_path, (str, Path))
                     else list(legacy_path or [])):
            self.legacy.update(self._read(Path(item))[0])
        self.estimated_path = self.path.with_name(self.path.stem + "_estimated.csv")
        self.estimates, estimated_extra = self._read(self.estimated_path)
        self.estimated_extra = estimated_extra
        # 对照列优先级与 buy/sell 一致: 核对通过 > 待核对估算
        self.extra = dict(estimated_extra)
        self.extra.update(extra)
        self._index()

    @staticmethod
    def _read(path):
        if not path.exists():
            return {}, {}
        frame = pd.read_csv(path)
        required = ["time", "buy", "sell"]
        if not set(required).issubset(frame.columns):
            raise ValueError(f"历史文件缺少列: {path.name}")
        numeric = frame[required].apply(pd.to_numeric, errors="coerce").dropna()
        numeric = numeric[np.isfinite(numeric).all(axis=1) & (numeric[["buy", "sell"]] >= 0).all(axis=1)]
        values = {int(r.time): (float(r.buy), float(r.sell)) for r in numeric.itertuples(index=False)}
        extra = {}
        columns = [c for c in EXTRA_COLUMNS if c in frame.columns]
        if columns:
            times = pd.to_numeric(frame["time"], errors="coerce")
            for column in columns:
                series = pd.to_numeric(frame[column], errors="coerce")
                for time, value in zip(times, series):
                    if pd.isna(time) or pd.isna(value):
                        continue
                    extra.setdefault(int(time), {})[column] = float(value)
        return values, extra

    def _index(self):
        self.times = np.array(sorted(self.values), dtype=np.int64)
        self.sums = np.r_[0., np.cumsum([self.values[t][0] - self.values[t][1] for t in self.times])]
        display = {**self.legacy, **self.estimates, **self.values}
        self.display_times = np.array(sorted(display), dtype=np.int64)
        self.display_sums = np.r_[0., np.cumsum([display[t][0] - display[t][1] for t in self.display_times])]

    def _fill_extra(self, bars):
        """补齐对照列: 旧历史行本身即旧算法口径; 其余从主文件读取。"""
        for column in EXTRA_COLUMNS:
            if column not in bars:
                bars[column] = np.nan
        legacy = bars.coverage.eq("legacy")
        if legacy.any():
            bars.loc[legacy, "buyLegacy"] = bars.loc[legacy, "buy"]
            bars.loc[legacy, "sellLegacy"] = bars.loc[legacy, "sell"]
            bars.loc[legacy, "unknown"] = 0.0
        if not self.extra:
            return bars
        stored = bars["time"].to_numpy(dtype=np.int64)
        for column in EXTRA_COLUMNS:
            missing = bars[column].isna().to_numpy()
            if not missing.any():
                continue
            filled = np.array([self.extra.get(int(t), {}).get(column, np.nan)
                               for t in stored], dtype=float)
            take = missing & np.isfinite(filled)
            if take.any():
                bars.loc[take, column] = filled[take]
        return bars

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
        result = self._fill_extra(result)
        result["delta"] = result["buy"] - result["sell"]
        result["deltaLegacy"] = result["buyLegacy"] - result["sellLegacy"]
        return result

    def save_completed(self, bars):
        if bars.empty:
            return
        done = bars.iloc[:-1]
        confirmed = done.coverage.eq("complete")
        estimated = done.coverage.eq("partial") & (done.get("hasBaseline", False) | ~done.time.isin(self.estimates))
        changed = False
        for mask, values, extra, path in [(confirmed, self.values, self.extra, self.path),
                                          (estimated, self.estimates, self.estimated_extra, self.estimated_path)]:
            records = []
            for row in done[mask].itertuples(index=False):
                timestamp = int(row.time)
                pair = (float(row.buy), float(row.sell))
                if not np.isfinite(pair).all():
                    continue
                stored = extra.get(timestamp, {})
                extras = {c: _norm(getattr(row, c, np.nan)) for c in EXTRA_COLUMNS}
                # 对照列也要参与比较, 否则 buy/sell 不变而 unknown 变化时不会落盘;
                # 比较用 _norm 归一, 保证空值不会让同一行被反复追加。
                if values.get(timestamp) != pair or any(stored.get(c) != extras[c] for c in EXTRA_COLUMNS):
                    records.append((timestamp, pair[0], pair[1], extras))
            if records:
                pd.DataFrame([(int(t), b, s, *[e[c] for c in EXTRA_COLUMNS])
                              for t, b, s, e in records],
                             columns=["time", "buy", "sell", *EXTRA_COLUMNS]).to_csv(
                    path, mode="a", header=not path.exists(), index=False)
                for timestamp, buy, sell, extras in records:
                    timestamp = int(timestamp)
                    values[timestamp] = (float(buy), float(sell))
                    extra[timestamp] = dict(extras)
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
