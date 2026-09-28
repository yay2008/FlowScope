"""判向算法回归: 新算法(Lee-Ready)四条规则 + 旧算法对照。

新算法 = 报价规则(比"前一"盘口) -> 中点规则 -> 逐笔规则, 三条都用不上记未知。
旧算法 = 比"本 tick 自己"的盘口, 中间价沿用上一笔方向(side 列, 原样保留)。
"""
import unittest

import pandas as pd

from indicator import GAP_NS, _classify_ticks, split_ticks_to_bars, build_bars
from test_data import BASE, klines, ticks


def frame(rows):
    """rows: (offset_sec, last_price, ask_price1, bid_price1, cumulative_volume)"""
    return pd.DataFrame({
        "datetime": [BASE + int(r[0] * 10**9) for r in rows],
        "last_price": [r[1] for r in rows],
        "ask_price1": [r[2] for r in rows],
        "bid_price1": [r[3] for r in rows],
        "volume": [r[4] for r in rows],
    })


class QuoteRuleTests(unittest.TestCase):
    def test_price_compared_against_previous_quote_not_own_snapshot(self):
        """规则1: 成交吃掉卖一、盘口随即上移, 旧算法会把主动买记成主动卖。"""
        data = frame([(0, 4286, 4287, 4286, 100),      # 无前一盘口 -> 未知
                      (1, 4287, 4288, 4287, 200)])     # 吃掉 4287 的卖一(前一卖一)
        result = _classify_ticks(data)
        # 新算法: 4287 >= 前一卖一 4287 -> 买
        self.assertEqual(result.side_lr.tolist(), [0.0, 1.0])
        # 旧算法: 自己的盘口已上移, 4287 <= 自己的买一 4287 -> 误判为卖
        self.assertEqual(result.side.tolist(), [-1.0, -1.0])

    def test_sell_when_price_hits_previous_bid(self):
        data = frame([(0, 4287, 4287, 4286, 100),
                      (1, 4286, 4287, 4286, 200)])
        self.assertEqual(_classify_ticks(data).side_lr.tolist(), [0.0, -1.0])

    def test_locked_quote_falls_through_to_tick_rule(self):
        """买一 == 卖一时报价规则无信息量, 不应同时命中买卖两侧。"""
        data = frame([(0, 4287, 4287, 4287, 100),      # 锁价, 建立盘口
                      (1, 4287, 4287, 4287, 200),      # 锁价 + 无前价 -> 未知
                      (2, 4288, 4287, 4287, 300)])     # 逐笔规则: 价格上移 -> 买
        result = _classify_ticks(data)
        self.assertEqual(result.side_lr.tolist(), [0.0, 0.0, 1.0])


class MidpointRuleTests(unittest.TestCase):
    def test_in_spread_price_uses_midpoint_then_tick_rule(self):
        """规则2: 价差内部比中点; 恰在中点落到逐笔规则。"""
        data = frame([(0, 4280, 4290, 4280, 100),      # 价差 10 跳, 中间价 4285
                      (1, 4289, 4290, 4280, 200),      # 高于中点 -> 买
                      (2, 4281, 4290, 4280, 300),      # 低于中点 -> 卖
                      (3, 4285, 4290, 4280, 400)])     # 恰在中点 -> 逐笔(4285>4281) -> 买
        self.assertEqual(_classify_ticks(data).side_lr.tolist(), [0.0, 1.0, -1.0, 1.0])

    def test_exactly_midpoint_without_any_basis_is_unknown(self):
        """规则3: 中点 + 无前价 + 无历史方向 = 真判不出来, 记未知而不是猜。"""
        data = frame([(0, 4285, 4290, 4280, 100),
                      (1, 4285, 4290, 4280, 200)])
        result = _classify_ticks(data)
        self.assertEqual(result.side_lr.tolist(), [0.0, 0.0])
        self.assertEqual(result.dv.tolist(), [0.0, 100.0])


class CarryTests(unittest.TestCase):
    def test_zero_tick_carries_instead_of_becoming_unknown(self):
        """同价成交沿用方向; 若打成未知会把大量成交量挤出 delta。"""
        data = frame([(0, 4285, 4285, 4280, 100),
                      (1, 4285, 4285, 4280, 200),      # 贴卖一 -> 买, 建立方向
                      (2, 4285, 4290, 4280, 200),      # 纯报价走阔, 无成交
                      (3, 4285, 4290, 4280, 250)])     # 同价 + 恰在中点 -> 沿用买
        result = _classify_ticks(data)
        self.assertEqual(result.dv.tolist(), [0.0, 100.0, 0.0, 50.0])
        self.assertEqual(result.side_lr.tolist(), [0.0, 1.0, 0.0, 1.0])
        self.assertEqual(result.lr_carry.iloc[3], 1.0)

    def test_legacy_algorithm_still_carries_previous_side_as_documented(self):
        """旧算法口径必须原样保留(它是新算法的对照基准)。"""
        data = frame([(0, 4285, 4287, 4283, 100),      # 自己的盘口中间, 无历史
                      (1, 4287, 4287, 4286, 200),      # 贴卖一 -> 买
                      (2, 4285, 4287, 4283, 300)])     # 又在自己的盘口中间 -> 沿用买
        result = _classify_ticks(data)
        self.assertEqual(result.side.tolist(), [0.0, 1.0, 1.0])
        # 新算法改用前一盘口(4287/4286), 4285 低于中点 4286.5 -> 卖
        self.assertEqual(result.side_lr.tolist(), [0.0, 1.0, -1.0])


class UnknownBucketTests(unittest.TestCase):
    def test_unknown_volume_kept_out_of_both_sides_but_in_observed(self):
        data = frame([(0, 4285, 4290, 4280, 100),
                      (1, 4285, 4290, 4280, 200)])
        bar = split_ticks_to_bars(data).loc[BASE]
        self.assertEqual((bar.buy, bar.sell, bar.unknown, bar.observed), (0., 0., 100., 100.))
        self.assertEqual(bar.buy + bar.sell + bar.unknown, bar.observed)

    def test_legacy_drops_in_spread_volume_while_new_algorithm_records_unknown(self):
        """旧算法没有未知桶, 盘口中间且无历史方向时这笔量两侧都不记。"""
        data = frame([(0, 4285, 4290, 4280, 100),
                      (1, 4285, 4290, 4280, 200)])
        bar = split_ticks_to_bars(data).loc[BASE]
        self.assertEqual(bar.buyLegacy + bar.sellLegacy, 0.0)
        self.assertEqual(bar.observed, 100.0)
        self.assertEqual(bar.unknown, 100.0)


class StateResetTests(unittest.TestCase):
    def test_quote_only_update_adds_no_volume_and_keeps_carry(self):
        """规则4: 无成交的报价更新不加量, 也不参与判向。"""
        data = frame([(0, 4287, 4287, 4286, 100),
                      (1, 4287, 4287, 4286, 150),      # 有成交 -> 报价规则定买
                      (2, 4287, 4291, 4290, 150)])     # 纯报价上移, 无成交
        result = _classify_ticks(data)
        self.assertEqual(result.dv.tolist(), [0.0, 50.0, 0.0])
        self.assertEqual(result.side_lr.iloc[1], 1.0)
        self.assertEqual(result.side_lr.iloc[2], 0.0)   # 无成交 -> 不判向、不加量
        self.assertEqual(result.lr_carry.iloc[2], 1.0)  # 沿用方向未被报价更新改写

    def test_new_trading_day_resets_direction_state(self):
        data = frame([(0, 4287, 4287, 4286, 1000),
                      (1, 4287, 4287, 4286, 1100),     # 买, 建立方向
                      (2, 4287, 4287, 4286, 50)])      # 累计量清零 -> 换日
        result = _classify_ticks(data)
        self.assertEqual(result.dv.iloc[2], 50.0)       # 累计清零后按新日已成交量
        self.assertEqual(result.side_lr.iloc[2], 0.0)   # 不沿用昨日方向 -> 未知

    def test_data_gap_resets_direction_state(self):
        gap = GAP_NS // 10**9 + 60
        data = frame([(0, 4287, 4287, 4286, 100),
                      (1, 4287, 4287, 4286, 150),
                      (1 + gap, 4287, 4287, 4286, 200)])
        self.assertEqual(_classify_ticks(data).side_lr.iloc[2], 0.0)

    def test_short_pause_still_carries_direction(self):
        """低于阈值的间隔不算断档, 报价规则照常生效。"""
        data = frame([(0, 4287, 4287, 4286, 100),
                      (1, 4287, 4287, 4286, 150),
                      (11, 4287, 4287, 4286, 200)])
        self.assertEqual(_classify_ticks(data).side_lr.iloc[2], 1.0)


class ParallelAlgorithmTests(unittest.TestCase):
    def test_both_algorithms_are_emitted_side_by_side(self):
        bars = build_bars(klines(), ticks())
        self.assertEqual(bars.buy.tolist(), [10., 10., 10.])
        self.assertEqual(bars.sell.tolist(), [0., 10., 0.])
        self.assertEqual(bars.buyLegacy.tolist(), [10., 20., 10.])
        self.assertEqual(bars.sellLegacy.tolist(), [0., 0., 0.])
        self.assertEqual(bars.delta.tolist(), [10., 0., 10.])
        self.assertEqual(bars.deltaLegacy.tolist(), [10., 20., 10.])

    def test_volume_is_conserved_across_the_three_buckets(self):
        bars = split_ticks_to_bars(ticks())
        self.assertTrue((bars.buy + bars.sell + bars.unknown == bars.observed).all())

    def test_bar_columns_are_stable(self):
        bars = build_bars(klines(), ticks())
        for column in ["buy", "sell", "unknown", "delta", "buyLegacy",
                       "sellLegacy", "deltaLegacy", "cvd", "coverage"]:
            self.assertIn(column, bars.columns)


if __name__ == "__main__":
    unittest.main()
