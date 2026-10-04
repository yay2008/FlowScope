"""按合约+周期保存每一根走完的 bar。

存储形态: **每个 feed 一个 v3 CSV**, 行内用 ``source`` 列区分数据来源:

- ``complete``: 与本合约 K 线成交量核对通过的量, CVD 亦按此口径累计;
- ``partial`` : 只有前置快照、尚未核对通过的估算量(闭市回填/断线补算);
- ``legacy``  : 旧算法(v2 及更早文件)的结果, 只作对照来源, 永不改写。

旧版本把完整量与估算量分写在 ``X_v3.csv`` / ``X_v3_estimated.csv`` 两个文件里,
同一批 bar 要写两遍、读两遍。现在合成一张表: 相同时间戳后写的行优先,
所以一行从 partial 升级为 complete 只需追加一行, 不需要跨文件去重。

兼容性:

- 既有 ``X_v3.csv`` 若没有 ``source`` 列, 全部按 ``complete`` 读;
- 但"只按 complete 读"还不够: 这种旧文件的表头只有 6 列, 而当前实现每行多写一个
  ``source`` 列。若只在文件尾追加新行, 表头与数据行的字段数就不一致, pandas 全量读
  会在第一条数据行整表失败(``Expected 6 fields in line N, saw 7``)。所以**读到不是
  当前格式的主文件时先原地补齐**: 补表头、给旧行补 ``complete``, 行序与取值不变,
  之后照常追加。(``tests/test_data.py`` 里有这条故障的复现用例。)
- 既有 ``X_v3_estimated.csv`` 仍会被读入(只读), 与主文件取并集, 主文件优先;
  此后不再写它, 也不删它 —— 迁移是纯增量的, 不需要一次性转换脚本。

读路径分两档, 因为瓶颈完全不同:

- **全量读**(启动、文件被截断): pandas 的 C 版 ``read_csv`` 一次解析, 再向量化地
  组装字典, 5 万行约 130 ms —— 与旧实现同一量级;
- **增量读**(``refresh()``): 只解析上次偏移量之后的新字节 —— 每根 bar 一两行,
  pandas 的固定开销反而更贵。历史行的累计和不必重算, 因为新增时间戳都排在末尾。
  无变化时 ``refresh()`` 只是一次 ``stat``。
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd

from indicator import EXTRA_COLUMNS

VALUE_COLUMNS = ["time", "buy", "sell", *EXTRA_COLUMNS]
HEADER = [*VALUE_COLUMNS, "source"]
SOURCE_COMPLETE = "complete"
SOURCE_PARTIAL = "partial"
SOURCE_LEGACY = "legacy"
_SOURCES = (SOURCE_COMPLETE, SOURCE_PARTIAL, SOURCE_LEGACY)


def _norm(value):
    """NaN/缺失 -> None; 用于让"未记录"和"空值"在比较时相等(NaN != NaN)。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _cell(value):
    """写盘用的单元格: None/NaN 写空字段, 读回是 NaN, 二者比较结果一致。"""
    return "" if value is None else repr(float(value))


def _row_line(time, buy, sell, extras, source=SOURCE_COMPLETE):
    return ",".join([str(int(time)), _cell(buy), _cell(sell),
                     *[("" if extras.get(c) is None else _cell(extras[c])) for c in EXTRA_COLUMNS],
                     source]) + "\n"


def _number_or_nan(value):
    """空字段/NaN -> NaN, 其余 -> float; 增量行组装成数组时用。"""
    number = _norm(value)
    return np.nan if number is None else number


def _extras_from_values(values):
    """(unknown, buyLegacy, sellLegacy) 原始值 -> 只保留有值的列。"""
    extras = {}
    for column, value in zip(EXTRA_COLUMNS, values):
        number = _norm(value)
        if number is not None:
            extras[column] = number
    return extras


def _source_codes(frame):
    """source 列 -> 0/1/2(complete/partial/legacy); 没有该列按 complete。"""
    codes = np.zeros(len(frame), dtype=np.int8)
    if "source" in frame.columns:
        source = frame["source"].astype(str)
        codes[source.eq(SOURCE_PARTIAL).to_numpy()] = 1
        codes[source.eq(SOURCE_LEGACY).to_numpy()] = 2
    return codes


def _extras_columns(frame):
    """取三列对照列的 numpy 视图; 缺列时用 NaN 顶替。"""
    columns = []
    for column in EXTRA_COLUMNS:
        if column in frame.columns:
            columns.append(pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float))
        else:
            columns.append(np.full(len(frame), np.nan))
    return columns


class HistoryStore:
    """单合约单周期的历史 bar 表; 只有采集线程读写, 不做跨线程加锁。"""

    def __init__(self, path, legacy_path=None):
        self.path = Path(path)
        # 旧的估算量文件: 只读来源, 迁移完成后不再写入。
        self.estimated_path = self.path.with_name(self.path.stem + "_estimated.csv")
        legacy_paths = ([legacy_path] if isinstance(legacy_path, (str, Path))
                        else list(legacy_path or []))
        self.values: dict[int, tuple[float, float]] = {}
        self.estimates: dict[int, tuple[float, float]] = {}
        self._legacy: dict[int, tuple[float, float]] = {}
        self.extra: dict[int, dict] = {}
        self._legacy_extra: dict[int, dict] = {}
        # 旧估算文件的对照列, 优先级低于主文件。
        self.estimated_extra: dict[int, dict] = {}
        self._legacy_paths = [Path(item) for item in legacy_paths]
        self._offset = 0
        self._size = 0
        self._needs_header = True
        self._dirty = True
        self._read_initial()
        self._index()

    # ------------------------------------------------------------------ 读取
    @staticmethod
    def _size_of(path):
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _upgrade_layout(self):
        """把表头与当前格式不一致的主文件原地补成当前格式; 返回是否改写过。

        旧版 v3 文件的表头只有 6 列(没有 ``source``), 而当前实现每行多写一个来源列。
        只往文件尾追加新行会让表头与数据行的字段数不一致, 于是**下次全量读整表失败**
        (``Expected 6 fields in line N, saw 7``), feed 之后每次重试都在同一处失败。
        这里先把旧文件补齐: 补表头、给旧行补 ``complete``, 行序与取值都不变。
        只处理主文件 —— legacy 与旧估算文件是只读来源, 从不追加, 也不该被改写。
        """
        header_line = ",".join(HEADER)
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as handle:
                first = handle.readline()
                if not first or first.rstrip("\r\n") == header_line:
                    return False
                handle.seek(0)
                lines = handle.read().splitlines()
        except (OSError, UnicodeDecodeError):
            return False
        width = len(HEADER)
        payload = [header_line]
        for line in lines:
            if not line.strip() or line.startswith("time,"):
                continue                       # 空行与重复表头都不是数据
            fields = line.split(",")
            if len(fields) > width:
                fields = fields[:width]
            elif len(fields) < width:
                # 旧 6 列行(少来源列)与更短的残行: 补空列, 来源按 complete。
                fields = fields + [""] * (width - 1 - len(fields)) + [SOURCE_COMPLETE]
            payload.append(",".join(fields))
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(temp, "w", encoding="utf-8", newline="") as handle:
                handle.write("\n".join(payload) + "\n")
            os.replace(temp, self.path)
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass
            return False
        return True

    def _read_initial(self):
        """完整读一次: legacy -> 旧估算文件 -> 主文件, 后者优先级更高。可反复调用。

        顺序与旧版完全一致(旧版先读主文件与估算文件, 再 ``extra.update`` 覆盖),
        因此同一时间戳上 complete 压 partial、主文件压旧估算。
        """
        self.values.clear()
        self.estimates.clear()
        self._legacy.clear()
        self.extra.clear()
        self._legacy_extra.clear()
        self.estimated_extra.clear()
        for item in self._legacy_paths:
            # legacy 文件本来就只有三列, 不解析对照列。
            self._accept_frame(item, default_code=2, want_extras=False)
        self._accept_frame(self.estimated_path, default_code=1)
        # 旧格式的主文件必须先补齐再读, 否则 pandas 会因字段数不一致整表失败。
        self._upgrade_layout()
        self._size = self._size_of(self.path)
        if self._size:
            self._accept_frame(self.path)
        self._offset = self._size
        self._needs_header = self._size == 0
        # 文件不以换行结束时(手工编辑过), 追加前要先补一个换行。
        self._needs_newline = self._missing_trailing_newline()

    def _missing_trailing_newline(self):
        if self._size == 0:
            return False
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._size - 1)
                return handle.read(1) != b"\n"
        except OSError:
            return False

    def _accept_frame(self, path, default_code=None, want_extras=True):
        """读入整个文件并合并; 文件不存在时按空表处理, 缺列时抛错(与旧版一致)。

        ``default_code`` 为主文件以外的只读来源指定来源码(1=partial, 2=legacy);
        主文件始终按 ``source`` 列判定, 没有该列的旧 v3 文件按 complete 处理。

        pandas 的 C 解析器遇到字段数与表头不一致的行会**整表**失败, 而这种坏行
        (手工编辑、写入中断)不该让 feed 永久卡死, 所以失败时退回逐行解析:
        能识别的行照常合并, 坏行跳过 —— 与增量读同一套规则。
        """
        if not path.exists():
            return
        try:
            frame = pd.read_csv(path)
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            print(f"[history] {path.name} 解析失败({exc}), 退回逐行读取", flush=True)
            self._accept(self._batch_from_lines(path, default_code, want_extras),
                         estimated=default_code == 1)
            return
        if not {"time", "buy", "sell"}.issubset(frame.columns):
            raise ValueError(f"历史文件缺少列: {path.name}")
        numeric = frame[["time", "buy", "sell"]].apply(pd.to_numeric, errors="coerce")
        finite = np.isfinite(numeric.to_numpy(dtype=float)).all(axis=1)
        finite &= (numeric[["buy", "sell"]].to_numpy(dtype=float) >= 0).all(axis=1)
        if default_code is None:
            codes = _source_codes(frame)
        else:
            codes = np.full(len(frame), default_code, dtype=np.int8)
        extras = _extras_columns(frame) if want_extras else None
        self._accept((numeric["time"].to_numpy(dtype=float)[finite].astype(np.int64),
                      numeric["buy"].to_numpy(dtype=float)[finite],
                      numeric["sell"].to_numpy(dtype=float)[finite],
                      codes[finite].astype(np.int8),
                      [column[finite] for column in extras] if extras else None),
                     estimated=default_code == 1)

    def _batch_from_lines(self, path, default_code, want_extras):
        """逐行解析整个文件, 只在 pandas 整表失败时当退路用; 无有效行返回 None。"""
        times, buys, sells, codes = [], [], [], []
        extras = [[] for _ in EXTRA_COLUMNS]
        try:
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return None
        for line in text.splitlines():
            row = self._parse_line(line)
            if row is None:
                continue
            times.append(row[0])
            buys.append(row[1])
            sells.append(row[2])
            for column, value in zip(extras, row[3]):
                column.append(_number_or_nan(value))
            codes.append(row[4] if default_code is None else default_code)
        if not times:
            return None
        return (np.array(times, dtype=np.int64), np.array(buys, dtype=float),
                np.array(sells, dtype=float), np.array(codes, dtype=np.int8),
                [np.array(column, dtype=float) for column in extras] if want_extras else None)

    def _parse_region(self, path, start, end):
        """增量解析 [start, end) 字节; 返回 (行列表, 实际读到的结束偏移)。

        结束偏移停在最后一个换行之后: 采集线程写盘与刷新可能交错, 半行留给下次读。
        """
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                chunk = handle.read(max(0, end - start))
        except OSError:
            return None, start
        cut = chunk.rfind(b"\n") + 1
        chunk = chunk[:cut]
        rows = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            row = self._parse_line(line)
            if row is not None:
                rows.append(row)
        if not rows:
            return None, start + cut
        return ([np.array([r[0] for r in rows], dtype=np.int64),
                 np.array([r[1] for r in rows], dtype=float),
                 np.array([r[2] for r in rows], dtype=float),
                 np.array([r[4] for r in rows], dtype=np.int8),
                 [np.array([_number_or_nan(r[3][i]) for r in rows], dtype=float)
                  for i in range(len(EXTRA_COLUMNS))]],
                start + cut)

    @staticmethod
    def _parse_line(line):
        """一行 -> (time, buy, sell, (对照列原始值...), source code); 坏行返回 None。"""
        if not line or line.startswith("time"):
            return None
        fields = line.split(",")
        if len(fields) < 3:
            return None
        try:
            timestamp = int(float(fields[0]))
            buy = float(fields[1])
            sell = float(fields[2])
        except ValueError:
            return None
        if not (np.isfinite(buy) and np.isfinite(sell)) or buy < 0 or sell < 0:
            return None
        source = fields[3 + len(EXTRA_COLUMNS)].strip() if len(fields) > 3 + len(EXTRA_COLUMNS) else ""
        code = 1 if source == SOURCE_PARTIAL else (2 if source == SOURCE_LEGACY else 0)
        # 没有 source 列(旧 v3 文件)或取值未知 -> complete。
        # 缺列的行补空: 调用方按固定列数取对照列, 短元组会让增量读直接崩。
        extras = list(fields[3:3 + len(EXTRA_COLUMNS)])
        extras += [""] * (len(EXTRA_COLUMNS) - len(extras))
        return timestamp, buy, sell, tuple(extras), code

    def _accept(self, batch, estimated=False):
        """把一批行并入内存表: (times, buys, sells, 来源码, 对照列数组或 None)。

        buy/sell 后写的行覆盖先写的行(与旧版逐行赋值一致); 对照列也是后写覆盖先写
        (见 ``_merge_extras``), 因此同一批里的重复行以最后一行为准。

        ``estimated=True`` 表示这批行来自只读的旧估算文件, 它的对照列要放在
        ``estimated_extra``(优先级低于主文件), 不能混进 ``extra``。
        """
        if batch is None:
            return
        self._dirty = True
        times, buys, sells, codes, extras = batch
        partial_mask = codes == 1
        legacy_mask = codes == 2
        complete_mask = ~(partial_mask | legacy_mask)
        # 对照列顺序与旧版一致: 先 complete(与"主文件 update 估算文件"等价, 同一批里
        # complete 优先), 再 partial / legacy。
        if extras is not None and complete_mask.any():
            self._merge_extras(self.extra, times, extras, complete_mask)
        if legacy_mask.any():
            self._legacy.update({int(t): (b, s) for t, b, s in
                                 zip(times[legacy_mask].tolist(), buys[legacy_mask].tolist(),
                                     sells[legacy_mask].tolist())})
            if extras is not None and not estimated:
                self._merge_extras(self._legacy_extra, times, extras, legacy_mask)
        if complete_mask.any():
            self.values.update({int(t): (b, s) for t, b, s in
                                zip(times[complete_mask].tolist(), buys[complete_mask].tolist(),
                                    sells[complete_mask].tolist())})
        if partial_mask.any():
            self.estimates.update({int(t): (b, s) for t, b, s in
                                   zip(times[partial_mask].tolist(), buys[partial_mask].tolist(),
                                       sells[partial_mask].tolist())})
            if extras is not None:
                self._merge_extras(self.estimated_extra if estimated else self.extra,
                                   times, extras, partial_mask)

    @staticmethod
    def _merge_extras(target, times, extras, mask):
        """对照列: 每个 (时间戳, 列) 以**最后一次**出现的非空值生效。

        旧版是逐列 ``extra.setdefault(ts, {})[column] = value``: 行字典只建一次,
        同一列后来的值会直接覆盖 —— 所以"首次"只体现在"哪个时间戳先建了字典"上,
        值本身是 last-wins。这里按列顺序后写覆盖先写, 与之等价。
        """
        stored = times[mask].tolist()
        for column, values in zip(EXTRA_COLUMNS, extras):
            for timestamp, value in zip(stored, values[mask].tolist()):
                if not np.isfinite(value):
                    continue
                row = target.get(timestamp)
                if row is None:
                    row = {}
                    target[timestamp] = row
                row[column] = value

    def refresh(self):
        """发现主文件增长就只解析新增字节; 无变化时只是一次 stat。

        legacy 与旧估算文件是只读来源, 构造时读一次即可, 不参与周期刷新。
        """
        size = self._size_of(self.path)
        if size < self._offset:
            # 文件被截断或替换(手工清理、换数据目录): 丢弃内存状态重新完整读取。
            self._read_initial()
            return True
        if size == self._offset:
            return False
        batch, end = self._parse_region(self.path, self._offset, size)
        self._offset = end
        if batch is None:
            return False
        self._accept(batch)
        return True

    # ------------------------------------------------------------------ 索引
    def _index(self):
        """重建排序时间轴与累计和(仅在历史内容变化后调用)。"""
        display = {**self._legacy, **self.estimates, **self.values}
        self.times = np.array(sorted(self.values), dtype=np.int64)
        self.sums = np.r_[0., np.cumsum([self.values[t][0] - self.values[t][1] for t in self.times])]
        self.display_times = np.array(sorted(display), dtype=np.int64)
        self.display_sums = np.r_[0., np.cumsum([display[t][0] - display[t][1] for t in self.display_times])]
        self._dirty = False

    def _ensure_index(self):
        """落盘只改字典, 索引推迟到真正要读的时候再建: 一根 bar 一次不必重建全表。"""
        if self._dirty:
            self._index()

    def _fill_extra(self, bars):
        """补齐对照列: 旧历史行本身即旧算法口径; 其余从已保存行读取。"""
        for column in EXTRA_COLUMNS:
            if column not in bars:
                bars[column] = np.nan
        legacy = bars.coverage.eq(SOURCE_LEGACY)
        if legacy.any():
            bars.loc[legacy, "buyLegacy"] = bars.loc[legacy, "buy"]
            bars.loc[legacy, "sellLegacy"] = bars.loc[legacy, "sell"]
            bars.loc[legacy, "unknown"] = 0.0
        if not self.extra and not self.estimated_extra:
            return bars
        # 三列都齐时无需回填: 这一步在每根 bar 的 recompute 里都会跑。
        missing_columns = [c for c in EXTRA_COLUMNS if bars[c].isna().any()]
        if not missing_columns:
            return bars
        # 对照列优先级与 buy/sell 一致: 核对通过 > 待核对估算。
        extra = dict(self.estimated_extra)
        extra.update(self.extra)
        stored = bars["time"].to_numpy(dtype=np.int64)
        for column in missing_columns:
            missing = bars[column].isna().to_numpy()
            if not missing.any():
                continue
            filled = np.array([extra.get(int(t), {}).get(column, np.nan)
                               for t in stored], dtype=float)
            take = missing & np.isfinite(filled)
            if take.any():
                bars.loc[take, column] = filled[take]
        return bars

    def merge(self, bars):
        """完整实时观测优先；否则使用已确认记录，最后才使用旧格式估算。

        换用已保存的记录时对照列也换成它自己的(由 _fill_extra 回填): 实时窗口的对照列
        只覆盖半根 bar, 与保存的 buy/sell 拼成一行会被 save_completed 当成变化写回。
        """
        self._ensure_index()
        result = bars.copy()
        extras = [column for column in EXTRA_COLUMNS if column in result]
        for values, quality in [(self.values, SOURCE_COMPLETE),
                                (self.estimates, SOURCE_PARTIAL),
                                (self._legacy, SOURCE_LEGACY)]:
            mask = result.coverage.ne(SOURCE_COMPLETE) & result.time.isin(values)
            if quality != SOURCE_COMPLETE:
                # 不用旧估算覆盖具备前置快照的新数据；窗口左边缘则优先回填已保存估算。
                mask &= ~result.get("hasBaseline", pd.Series(False, index=result.index))
                if quality == SOURCE_LEGACY:
                    mask &= ~result.time.isin(self.estimates)
            times = result.loc[mask, "time"]
            if not times.empty:
                result.loc[mask, "buy"] = times.map(lambda t: values[int(t)][0])
                result.loc[mask, "sell"] = times.map(lambda t: values[int(t)][1])
                result.loc[mask, "coverage"] = quality
                result.loc[mask, extras] = np.nan
        result = self._fill_extra(result)
        result["delta"] = result["buy"] - result["sell"]
        result["deltaLegacy"] = result["buyLegacy"] - result["sellLegacy"]
        return result

    # ------------------------------------------------------------------ 落盘
    def _append(self, lines):
        """一次性顺序追加若干行; 首行之前按需写表头。"""
        if not lines:
            return
        payload = "".join(lines)
        if self._needs_header:
            payload = ",".join(HEADER) + "\n" + payload
        elif self._needs_newline:
            payload = "\n" + payload
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
        self._needs_header = False
        self._needs_newline = False
        self._offset += len(payload.encode("utf-8"))
        self._size = self._offset

    def save_completed(self, bars, final=False):
        """把已走完的 bar 落盘; 只有数值或对照列真的变了才追加。

        核对通过的行写 ``complete``, 只有前置快照的估算行写 ``partial`` —— 两者靠
        source 列区分, 所以重启后估算量不会冒充已核对的量。
        最后一根默认当作还没走完; ``final=True`` 表示整张表都已走完(加密行情的历史回填)。
        """
        if bars.empty:
            return
        done = bars if final else bars.iloc[:-1]
        estimated = (done.coverage.eq(SOURCE_PARTIAL)
                     & (done.get("hasBaseline", False) | ~done.time.isin(self.estimates)))
        changed = False
        for mask, values, quality in [(done.coverage.eq(SOURCE_COMPLETE), self.values,
                                       SOURCE_COMPLETE),
                                      (estimated, self.estimates, SOURCE_PARTIAL)]:
            lines = []
            for row in done[mask].itertuples(index=False):
                timestamp = int(row.time)
                buy, sell = _norm(row.buy), _norm(row.sell)
                if buy is None or sell is None:
                    continue
                stored = self.extra.get(timestamp, {})
                extras = {c: _norm(getattr(row, c, np.nan)) for c in EXTRA_COLUMNS}
                # 对照列也要参与比较, 否则 buy/sell 不变而 unknown 变化时不会落盘;
                # 比较用 _norm 归一, 保证空值不会让同一行被反复追加。
                if values.get(timestamp) != (buy, sell) or any(stored.get(c) != extras[c]
                                                               for c in EXTRA_COLUMNS):
                    lines.append(_row_line(timestamp, buy, sell, extras, quality))
                    values[timestamp] = (buy, sell)
                    self.extra[timestamp] = dict(extras)
                    changed = True
            self._append(lines)
        if changed:
            self._dirty = True

    def with_cvd(self, bars):
        """显示 CVD 累计可用估算量；核对通过的累计量独立保留，二者均固定基准。"""
        self._ensure_index()
        result = bars.copy()
        before = np.searchsorted(self.times, result["time"].to_numpy(dtype=np.int64), side="left")
        result["cvdConfirmed"] = (self.sums[before] + result["delta"]).where(result.coverage.eq(SOURCE_COMPLETE))
        before = np.searchsorted(self.display_times, result["time"].to_numpy(dtype=np.int64), side="left")
        available = result["delta"].notna()
        result["cvdOpen"] = pd.Series(self.display_sums[before], index=result.index).where(available)
        result["cvd"] = (result["cvdOpen"] + result["delta"]).where(available)
        return result

    @property
    def legacy(self):
        """旧算法(v2 及更早)记录, 只读对照用。"""
        return dict(self._legacy)

    @property
    def base(self):
        return int(self.display_times[0]) if len(self.display_times) else None
