import tempfile
import unittest
from pathlib import Path

import pandas as pd

from indicator import BAR_NS, build_bars, build_bars_from_ltf, build_footprint, _classify_ticks
from history_store import HEADER, HistoryStore, _row_line


BASE = 1_800_000_000_000_000_000
TZ_SHIFT = 8 * 3600    # indicator 把 UTC 时间戳 +8h 存成北京时间


def bar_time(offset_sec):
    """第 offset_sec 秒那根 bar 落盘时的 time 值(与 indicator 的时区口径一致)。"""
    return (BASE + offset_sec * 10**9) // 10**9 + TZ_SHIFT


def ticks(offsets=(0, 1, 30, 31, 60), prices=(101, 103, 101, 103, 103), volumes=(100, 110, 120, 130, 140)):
    return pd.DataFrame({"datetime": [BASE + int(s * 1e9) for s in offsets],
                         "last_price": prices, "ask_price1": prices,
                         "bid_price1": [p - 1 for p in prices], "volume": volumes})


def klines():
    return pd.DataFrame({"datetime": [BASE + i * BAR_NS for i in range(3)],
                         "open": [101.] * 3, "high": [103.] * 3, "low": [101.] * 3,
                         "close": [103.] * 3, "volume": [20, 20, 10]})


class DataTests(unittest.TestCase):
    def test_kline_mode_uses_candle_volume_and_ignores_doji(self):
        k = klines().iloc[:1].copy()
        k["volume"] = 200
        lower = pd.DataFrame({"datetime": [BASE + s * 10**9 for s in [0, 10, 20]],
                              "open": [100, 100, 100], "close": [101, 99, 100],
                              "volume": [100, 60, 40]})
        bar = build_bars_from_ltf(k, lower, 10).iloc[0]
        self.assertEqual((bar.buy, bar.sell, bar.delta), (100, 60, 40))
        self.assertEqual(bar.coverage, "complete")
        partial = build_bars_from_ltf(k, lower.iloc[1:], 10).iloc[0]
        self.assertEqual(partial.coverage, "partial")
        self.assertFalse(partial.hasBaseline)
        missing = build_bars_from_ltf(k, lower.iloc[:0], 10).iloc[0]
        self.assertEqual(missing.coverage, "missing")
        self.assertTrue(pd.isna(missing.delta))

    def test_kline_mode_groups_candles_by_main_bar_and_detects_gaps(self):
        k = klines()
        k["volume"] = [10, 20, 0]
        lower = pd.DataFrame({"datetime": [BASE + s * 10**9 for s in [0, 30, 40, 50, 60, 70, 80]],
                              "open": [100] * 7, "close": [101, 99, 99, 99, 100, 100, 100],
                              "volume": [10, 10, 5, 5, 0, 0, 0]})
        result = build_bars_from_ltf(k, lower, 10)
        self.assertEqual(result.delta.tolist(), [10, -20, 0])
        self.assertEqual(result.coverage.tolist(), ["partial", "complete", "complete"])

    def test_footprint_preserves_actual_prices_without_metadata(self):
        result = build_footprint(klines(), ticks())
        self.assertIsNone(result["tickSize"])
        # levels 第 4 位是新算法的未知量; 101 档是下移一笔, 新算法记卖不记买
        self.assertEqual(result["bars"][1]["levels"], [[101., 0., 10., 0.], [103., 10., 0., 0.]])

    def test_price_step_comes_from_metadata_even_with_sparse_trades(self):
        result = build_footprint(klines(), ticks(), .5)
        self.assertEqual(result["tickSize"], .5)
        self.assertEqual(result["bars"][1]["levels"][0][0], 101.)

    def test_partial_first_bar_and_complete_following_bars(self):
        bars = build_bars(klines(), ticks())
        self.assertEqual(bars.coverage.tolist(), ["partial", "complete", "complete"])
        self.assertTrue(pd.isna(bars.iloc[0].cvd))

    def test_volume_mismatch_is_not_complete(self):
        k = klines()
        k.loc[1, "volume"] = 30
        self.assertEqual(build_bars(k, ticks()).iloc[1].coverage, "partial")

    def test_daily_volume_reset_uses_new_accumulated_volume(self):
        data = ticks(offsets=(0, 1, 2), prices=(101, 101, 101), volumes=(100, 3, 8))
        self.assertEqual(_classify_ticks(data).dv.tolist(), [0, 3, 5])

    def test_partial_never_persisted_and_confirmed_history_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HistoryStore(Path(folder) / "v2.csv")
            bars = build_bars(klines(), ticks())
            store.save_completed(bars)
            self.assertEqual(list(store.values), [int(bars.iloc[1].time)])
            clipped = build_bars(klines(), ticks().iloc[3:])
            merged = store.merge(clipped)
            self.assertEqual(merged.iloc[1].buy, 10)
            self.assertEqual(merged.iloc[1].coverage, "complete")

    def test_cvd_stays_fixed_after_window_shift_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v2.csv"
            store = HistoryStore(path)
            bars = build_bars(klines(), ticks())
            store.save_completed(bars)
            expected = store.with_cvd(bars).iloc[-1].cvd
            restarted = HistoryStore(path)
            shifted = restarted.with_cvd(bars.iloc[-1:].copy())
            self.assertEqual(expected, 20)
            self.assertEqual(shifted.iloc[-1].cvd, expected)

    def test_legacy_kept_unchanged_and_displayed_as_estimated_cvd(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = Path(folder) / "old.csv"
            bars = build_bars(klines(), ticks())
            stamp = int(bars.iloc[0].time)
            content = f"time,buy,sell\n{stamp},10,0\n{stamp},99,0\n"
            legacy.write_text(content)
            store = HistoryStore(Path(folder) / "v2.csv", legacy)
            result = store.with_cvd(store.merge(bars))
            self.assertEqual(result.iloc[0].coverage, "legacy")
            self.assertEqual(result.iloc[0].cvd, 99)
            self.assertTrue(pd.isna(result.iloc[0].cvdConfirmed))
            self.assertEqual(legacy.read_text(), content)

    def test_flowmeter_remains_visible_when_all_tick_volumes_differ_from_klines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v2.csv"
            store = HistoryStore(path)
            k = klines()
            k["volume"] += 7
            bars = build_bars(k, ticks())
            self.assertTrue(bars.coverage.eq("partial").all())
            store.save_completed(bars)
            result = store.with_cvd(bars)
            self.assertEqual(result.cvd.tolist(), [10, 10, 20])
            self.assertEqual(result.cvdOpen.tolist(), [0, 10, 10])
            self.assertTrue(result.cvdConfirmed.isna().all())
            self.assertEqual(store.values, {})
            restarted = HistoryStore(path)
            shifted = build_bars(k, ticks().iloc[3:])
            shifted = restarted.with_cvd(restarted.merge(shifted))
            self.assertEqual(shifted.iloc[1].cvd, 10)
            self.assertEqual(shifted.iloc[2].cvd, 20)

    def test_confirmed_replacement_does_not_double_count_estimated_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HistoryStore(Path(folder) / "v2.csv")
            k = klines()
            k["volume"] += 7
            store.save_completed(build_bars(k, ticks()))
            full = build_bars(klines(), ticks())
            store.save_completed(full)
            result = store.with_cvd(full)
            self.assertEqual(result.iloc[-1].cvd, 20)
            self.assertEqual(result.iloc[-1].cvdConfirmed, 10)

    def test_completed_and_estimated_share_one_file(self):
        """完整量与估算量写同一个文件, 靠 source 列区分; 不再产生 _estimated 副本。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            store = HistoryStore(path)
            k = klines()
            k["volume"] += 7
            store.save_completed(build_bars(k, ticks()))          # 三根都没核对通过
            self.assertEqual(sorted(store.estimates), [bar_time(0), bar_time(30)])
            store.save_completed(build_bars(klines(), ticks()))   # 第二根核对通过
            self.assertEqual(sorted(store.values), [bar_time(30)])
            sources = pd.read_csv(path)["source"].tolist()
            self.assertIn("partial", sources)
            self.assertIn("complete", sources)
            self.assertFalse(Path(folder).joinpath("v3_estimated.csv").exists())
            # 重启后完整量仍然压过同一时间戳的估算量
            restarted = HistoryStore(path)
            self.assertEqual(restarted.values, store.values)
            self.assertEqual(restarted.merge(build_bars(klines(), ticks())).coverage.tolist(),
                             ["partial", "complete", "complete"])

    def test_legacy_v3_file_without_source_column_reads_as_complete(self):
        """既有文件没有 source 列时按 complete 读; 表头在加载时被补齐到当前格式。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy\n"
                            "1000,10,4,0,9,5\n1001,20,6,0,19,7\n")
            store = HistoryStore(path)
            self.assertEqual(store.values, {1000: (10.0, 4.0), 1001: (20.0, 6.0)})
            self.assertEqual(store.estimates, {})
            self.assertEqual(store.extra[1000], {"unknown": 0.0, "buyLegacy": 9.0,
                                                 "sellLegacy": 5.0})
            self.assertEqual(path.read_text().splitlines()[0], ",".join(HEADER))

    def test_legacy_header_file_is_upgraded_before_appending(self):
        """旧表头文件被追加新格式行之前先补齐, 否则字段数不一致会让整表读失败。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy\n1000,10,4,0,9,5\n")
            store = HistoryStore(path)
            store._append([_row_line(1001, 20.0, 6.0, {}, "partial")])
            frame = pd.read_csv(path)                     # 修复前这里抛 ParserError
            self.assertEqual(frame["source"].tolist(), ["complete", "partial"])
            restarted = HistoryStore(path)
            self.assertEqual(restarted.values, {1000: (10.0, 4.0)})
            self.assertEqual(restarted.estimates, {1001: (20.0, 6.0)})
            self.assertEqual(restarted.extra[1000], {"unknown": 0.0, "buyLegacy": 9.0,
                                                     "sellLegacy": 5.0})

    def test_mixed_width_file_is_repaired_on_load(self):
        """线上故障复现: 旧表头(6 列)后面被追加了新格式(7 列)的行。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy\n"
                            "1000,10,4,0,9,5\n"
                            "1001,20,6,0,19,7,partial\n")
            store = HistoryStore(path)                    # 修复前这里抛 ParserError
            self.assertEqual(store.values, {1000: (10.0, 4.0)})
            self.assertEqual(store.estimates, {1001: (20.0, 6.0)})
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], ",".join(HEADER))
            self.assertTrue(all(len(line.split(",")) == len(HEADER) for line in lines))

    def test_unparsable_file_falls_back_to_line_reader(self):
        """pandas 整表失败时退回逐行解析: 能识别的行照常读入(缺列补空), 坏行跳过。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy,source\n"
                            "1000,10,4,0,9,5,complete\n"
                            "1001,20,6,0\n"                    # 缺列 -> pandas 整表失败
                            "not-a-time,30,8,0,29,9,complete\n"  # 坏行 -> 跳过
                            "1002,30,8,0,29,9,complete\n")
            store = HistoryStore(path)
            self.assertEqual(sorted(store.values), [1000, 1001, 1002])
            self.assertEqual(store.values[1000], (10.0, 4.0))
            self.assertEqual(store.values[1001], (20.0, 6.0))
            self.assertEqual(store.extra[1001], {"unknown": 0.0})   # 缺的两列留空

    def test_incremental_read_tolerates_short_rows(self):
        """增量读遇到缺列的行不能崩(对照列按空处理)。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            store = HistoryStore(path)
            store.save_completed(build_bars(klines(), ticks()))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("9999,7,3\n")                    # 只有三列
            self.assertTrue(store.refresh())
            self.assertEqual(store.values[9999], (7.0, 3.0))

    def test_append_writes_header_once_and_keeps_existing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            store = HistoryStore(path)
            store.save_completed(build_bars(klines(), ticks()))
            store._append([_row_line(99999, 30.0, 8.0, {})])
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], "time,buy,sell,unknown,buyLegacy,sellLegacy,source")
            self.assertEqual(sum(1 for line in lines if line.startswith("time,")), 1)
            self.assertEqual(HistoryStore(path).values[99999], (30.0, 8.0))

    def test_append_repairs_file_missing_trailing_newline(self):
        """手工编辑过的文件最后一行没有换行时, 追加不能把两行粘在一起。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy,source\n"
                            "1000,10,4,0,,,complete")      # 故意不带结尾换行
            store = HistoryStore(path)
            store._append([_row_line(1001, 20.0, 6.0, {})])
            reloaded = HistoryStore(path)
            self.assertEqual(reloaded.values, {1000: (10.0, 4.0), 1001: (20.0, 6.0)})

    def test_refresh_reads_only_appended_bytes(self):
        """文件被外部追加后 refresh() 补齐内存视图, 无变化时不做任何解析。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            store = HistoryStore(path)
            store.save_completed(build_bars(klines(), ticks()))
            self.assertFalse(store.refresh())            # 无变化
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("9999,7,3,0,,\n")           # complete, 对照列留空
            self.assertTrue(store.refresh())
            self.assertEqual(store.values[9999], (7.0, 3.0))
            self.assertFalse(store.refresh())            # 已经读到末尾
            # 半个行(没有换行)不算数, 补齐后再读
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("10000,8,2,0,,")
            self.assertFalse(store.refresh())
            self.assertNotIn(10000, store.values)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n")
            self.assertTrue(store.refresh())
            self.assertEqual(store.values[10000], (8.0, 2.0))

    def test_refresh_after_truncation_rebuilds_from_scratch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            store = HistoryStore(path)
            store.save_completed(build_bars(klines(), ticks()))
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy,source\n5000,1,1,0,,,complete\n")
            self.assertTrue(store.refresh())
            self.assertEqual(store.values, {5000: (1.0, 1.0)})
            self.assertEqual(store.estimates, {})

    def test_duplicate_rows_use_last_value_and_last_legacy_column(self):
        """同一时间戳重复行: buy/sell 与对照列都取最后一次出现的值。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            path.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy,source\n"
                            "1000,10,4,0,9,5,complete\n"
                            "1000,11,3,0,8,6,complete\n")
            store = HistoryStore(path)
            self.assertEqual(store.values[1000], (11.0, 3.0))
            self.assertEqual(store.extra[1000], {"unknown": 0.0, "buyLegacy": 8.0,
                                                 "sellLegacy": 6.0})

    def test_legacy_estimated_file_is_read_but_never_rewritten(self):
        """迁移期兼容: 既有 _estimated.csv 仍被读入, 但不再被写入。"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v3.csv"
            estimated = Path(folder) / "v3_estimated.csv"
            estimated.write_text("time,buy,sell,unknown,buyLegacy,sellLegacy\n1001,5,1,0,4,2\n")
            before = estimated.read_text()
            k = klines()
            k["volume"] += 7
            store = HistoryStore(path)
            store.save_completed(build_bars(k, ticks()))
            self.assertEqual(store.estimates[1001], (5.0, 1.0))
            self.assertEqual(store.estimated_extra[1001], {"unknown": 0.0, "buyLegacy": 4.0,
                                                           "sellLegacy": 2.0})
            self.assertEqual(estimated.read_text(), before)


if __name__ == "__main__":
    unittest.main()
