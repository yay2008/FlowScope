import tempfile
import unittest
from pathlib import Path

import pandas as pd

from indicator import BAR_NS, build_bars, build_bars_from_ltf, build_footprint, _classify_ticks
from history_store import HistoryStore


BASE = 1_800_000_000_000_000_000


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


if __name__ == "__main__":
    unittest.main()
