"""大额检测: 先筛原始事件, 再聚合；独立于图表周期与买卖量拆分口径。"""
import copy
import json
import math
import os
import queue
import unittest
from unittest.mock import patch

import pandas as pd
from types import SimpleNamespace

from indicator import GAP_NS, TZ_SHIFT_S
from large_trades import (LargeTradeDetector, decorate_bars, default_settings, judge_history, product_of,
                          settings_for, NS)
from large_trade_defaults import MIN_NOTIONAL
from tick_analytics import TickAnalytics

BASE = 1_800_000_000 * NS  # 整 30 秒


def detector(source="snapshot", **options):
    # lagSec=0: 此前的事件立即进基线, 便于单独检查其它规则; 滞后另有测试
    settings = dict(windowSec=1800, lagSec=0, warmup=3, quantile=0.99, minQty=20, minNotional=0)
    settings.update(options)
    return LargeTradeDetector("TEST.X", source, settings)


def baseline(d):
    """基线里的 (交易时钟, 量): 已进时间窗的, 加上还在滞后队列里的(lagSec=0 时只有最新一条)。"""
    return [*d._window, *d._lagged]


def qtys(d):
    return [q for _, q in baseline(d)]


def raw(i, q=1, side=1, reset=False, usable=True, price=100, joined=None):
    ns = BASE + i * NS
    return ns, (ns, price, q, side, reset, usable, not reset if joined is None else joined)


def bars(tf, start=0, count=3, volume=1000):
    return [{"time": BASE // NS + TZ_SHIFT_S + start + i * tf, "volume": volume} for i in range(count)]


def futures_ticks():
    """200 个正量事件预热后, 两个不同 SDK 序号在同一时刻分别成交 40/80 手。"""
    offsets = [*range(201), 201, 201, 202]
    return pd.DataFrame({"id": range(len(offsets)), "datetime": [BASE + i * NS for i in offsets],
                         "last_price": 100., "ask_price1": 100., "bid_price1": 99.,
                         "volume": [*range(100, 301), 340, 420, 421]})


class FuturesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ticks = futures_ticks()
        self.analytics = TickAnalytics()
        self.detector = detector(warmup=200)

    def sync(self, ticks, initial=False):
        self.analytics.update(ticks)
        self.detector.sync_futures(self.analytics.frame, self.analytics.changed_ns, initial)

    def summary(self):
        values = bars(30, start=180, count=1)
        decorate_bars(values, [self.detector], 30)
        return values[0]["large"]

    def test_same_timestamp_ticks_keep_both_events_and_repeated_sync_is_stable(self):
        self.sync(self.ticks, initial=True)
        summary = self.summary()
        self.assertEqual((summary["count"], summary["buy"], summary["delta"], summary["cvd"]),
                         (2, 120, 120, 120))
        expected_events = copy.deepcopy(self.detector.events)
        expected_window = baseline(self.detector)
        for initial in (False, False, True, False):
            self.detector.sync_futures(self.analytics.frame, self.analytics.changed_ns, initial)
            self.assertEqual(self.detector.events, expected_events)
            self.assertEqual(baseline(self.detector), expected_window)
            self.assertEqual(self.summary(), summary)

    def test_same_timestamp_ticks_arriving_in_separate_updates_are_not_revisions(self):
        self.sync(self.ticks.iloc[:202], initial=True)
        self.assertEqual(self.summary()["buy"], 40)
        self.sync(self.ticks.iloc[:203])
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (2, 120))
        self.sync(self.ticks)
        self.assertEqual((self.summary()["count"], self.summary()["cvd"]), (2, 120))

    def test_revising_either_same_timestamp_tick_matches_full_replay(self):
        for volumes, expected_qty in [([301, 381, 382], 80), ([340, 341, 342], 40)]:
            with self.subTest(volumes=volumes):
                self.analytics = TickAnalytics()
                self.detector = detector(warmup=200)
                self.sync(self.ticks, initial=True)
                revised = self.ticks.copy()
                revised.loc[201:, "volume"] = volumes
                self.sync(revised)
                fresh = detector(warmup=200)
                full = TickAnalytics()
                full.update(revised)
                fresh.sync_futures(full.frame, full.changed_ns, initial=True)
                self.assertEqual(self.detector.events, fresh.events)
                self.assertEqual(baseline(self.detector), baseline(fresh))
                self.assertEqual((self.summary()["count"], self.summary()["buy"], self.summary()["cvd"]),
                                 (1, expected_qty, expected_qty))

    def test_new_window_at_same_timestamp_resets_baseline_without_overlap(self):
        self.sync(self.ticks.iloc[:202], initial=True)
        tail = self.ticks.iloc[202:].copy()
        tail.loc[203, "volume"] = 920
        peer = TickAnalytics()
        peer.update(tail)
        self.detector.sync_futures(peer.frame, peer.changed_ns, initial=True)
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (1, 40))
        self.assertEqual(qtys(self.detector), [500])

    def test_timestamp_fallback_is_not_checked_as_a_consecutive_sdk_id(self):
        ticks = self.ticks.drop(columns="id")
        ticks.loc[202:, "datetime"] += NS
        self.sync(ticks, initial=True)
        self.assertEqual((self.summary()["count"], self.summary()["buy"]), (2, 120))


class DetectionTests(unittest.TestCase):
    def test_previous_baseline_only_and_absolute_floor(self):
        d = detector()
        d.update([raw(0, 1), raw(1, 2), raw(2, 3), raw(3, 30)])
        e = d.event_rows()[0]
        self.assertEqual(e["ratio"], 15)  # 均值是 (1+2+3)/3, 不能包含本次的 30
        self.assertAlmostEqual(e["z"], 28 / math.sqrt(2 / 3))
        self.assertAlmostEqual(e["threshold"], 2.98)
        unchanged = copy.deepcopy(e)
        d.update([raw(4, 1000)])
        self.assertEqual(d.event_rows()[0], unchanged)
        small = detector()
        small.update([raw(0), raw(1), raw(2), raw(3, 10)])
        self.assertEqual(small.events, {})  # 虽然 10 倍, 仍小于 20 手

    def test_warmup_constant_volume_quantile_and_rolling_window(self):
        d = detector(windowSec=3)
        d.update([raw(0, 100), raw(1, 100), raw(2, 100), raw(3, 100)])
        self.assertEqual(d.events, {})  # 不能因 std=0 或 >=P99 就把普通成交当大额
        d.update([raw(4), raw(5), raw(6), raw(7, 30)])  # 3 秒前的 100 已滑出时间窗
        self.assertEqual(d.event_rows()[0]["ratio"], 30)
        self.assertIsNone(d.event_rows()[0]["z"])
        json.dumps(d.event_rows(), allow_nan=False)

    def test_duplicates_and_revisions_match_full_replay(self):
        rows = [raw(0), raw(1), raw(2), raw(3, 40), raw(4, 80), raw(5)]
        d = detector()
        d.update(rows)
        expected = copy.deepcopy(d.events)
        d.update(rows)
        self.assertEqual(d.events, expected)
        rows[3] = raw(3, 2)
        d.update([rows[3]])
        fresh = detector()
        fresh.update(rows)
        self.assertEqual(d.events, fresh.events)
        self.assertEqual(baseline(d), baseline(fresh))
        self.assertEqual(d.readiness, fresh.readiness)

    def test_crypto_notional_floor_id_gap_and_duplicate(self):
        d = detector("aggregate", minQty=0, minNotional=10000)
        for i in range(1, 4):
            d.trade(i, 100, 1, BASE // 1_000_000 + i, False)
        d.trade(4, 100, 50, BASE // 1_000_000 + 4, False)
        self.assertEqual(d.events, {})  # 只有 5000 USDT
        d.trade(5, 100, 120, BASE // 1_000_000 + 5, True)
        self.assertEqual(d.event_rows()[0]["side"], -1)
        d.trade(5, 100, 120, BASE // 1_000_000 + 5, True)
        self.assertEqual(len(d.events), 1)
        d.trade(9, 100, 1000, BASE // 1_000_000 + 9, False)
        self.assertEqual(len(d.events), 1)  # 漏号后重建基线, 首笔不发信号
        self.assertFalse(d.readiness[9][1])

    def test_lag_judges_a_burst_against_the_level_before_it(self):
        def burst(lag):
            d = detector(windowSec=600, lagSec=lag)
            d.update([raw(i) for i in range(300)])               # 300 秒, 每秒 1 手
            d.update([raw(300 + j, 50) for j in range(60)])      # 接着连续 60 秒每秒 50 手
            return [(e["ns"] - BASE) // NS - 300 for e in d.event_rows()]
        lagged, instant = burst(30), burst(0)
        self.assertEqual(lagged[:30], list(range(30)), "滞后期内的连串大单都和放量前的水平比")
        self.assertTrue(all(j < 40 for j in lagged), "放量持续超过滞后期, 门槛照样抬高, 不会满屏")
        self.assertEqual(instant, list(range(5)), "不滞后: 第六笔起被前几笔抬高的门槛压掉")

    def test_lag_delays_warmup_and_reset_clears_lagged_events(self):
        d = detector(windowSec=600, lagSec=30, warmup=3)
        d.update([raw(i) for i in range(30)])
        self.assertFalse(any(ready for _, ready, _ in d.readiness.values()), "前 30 秒都还在滞后期, 基线是空的")
        d.update([raw(i) for i in range(30, 34)])
        self.assertTrue(d.readiness[BASE + 33 * NS][1])
        d.update([raw(34, reset=True)])
        self.assertEqual(qtys(d), [1], "断档后滞后队列一并清掉, 只剩断档后这一条")

    def test_crypto_same_millisecond_same_side_trades_merge_into_one_event(self):
        d = detector("aggregate", minQty=0, minNotional=10000)
        ms = BASE // 1_000_000
        for i in range(1, 5):
            d.trade(i, 100, 1, ms + i, False)
        d.trade(5, 100, 40, ms + 10, False)        # 4000 USDT, 单看不够
        d.trade(6, 101, 40, ms + 10, False)        # 同一毫秒同向: 并成 80
        self.assertEqual(d.events, {})
        d.trade(7, 102, 40, ms + 10, False)        # 120 × 均价 101 = 12120 USDT, 立即报出
        self.assertEqual(list(d.events), [5])
        self.assertEqual((d.events[5]["qty"], d.events[5]["price"], d.events[5]["side"]), (120, 101, 1))
        self.assertEqual(qtys(d)[-1], 120, "基线里是一个 120 的事件, 不是三个 40")
        d.trade(8, 100, 40, ms + 10, True)         # 方向变了: 另起一个事件
        d.trade(9, 100, 40, ms + 10, True)
        d.trade(10, 100, 40, ms + 11, True)        # 换了毫秒: 另起
        d.trade(12, 100, 200, ms + 11, True)       # 漏号: 不合并, 重新预热
        self.assertEqual(sorted(d.raw), [1, 2, 3, 4, 5, 8, 10, 12])
        self.assertEqual(d.raw[8][2], 80)
        self.assertEqual(list(d.events), [5])
        self.assertFalse(d.readiness[12][1])

    def test_trimmed_records_keep_their_coverage(self):
        ms = BASE // 1_000_000
        with patch("large_trades.RAW_SLACK", 50):
            d = detector("trade", minQty=0, minNotional=0, windowSec=60)
            for i in range(1, 3001):                   # 300 秒, 每 0.1 秒一笔
                d.trade(i, 100, 1 + i % 3, ms + i * 100, i % 2 == 0)
        self.assertLess(len(d.raw), 2000, "原始记录裁过")
        values = bars(30, count=10)
        decorate_bars(values, [d], 30)
        self.assertEqual([v["large"]["coverage"] for v in values], ["partial"] + ["complete"] * 9,
                         "裁掉的记录照样算已采集, 不能显示成未采集")
        self.assertTrue(all(v["large"]["cvd"] is not None for v in values))

    def test_absorbed_history_is_shown_without_touching_the_live_baseline(self):
        ms = BASE // 1_000_000
        env = '{"TEST.X": {"warmup": 3, "lagSec": 0, "minNotional": 0}}'
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": env}):
            # 回填的洞: 编号 1~59 (0~29 秒), 61~119 (30~59 秒) —— 60 号缺, 检测器在那里重新预热
            history = [(i, 100.0, 50.0 if i in (20, 90) else 1.0, ms + i * 500, i == 90) for i in range(1, 120) if i != 60]
            events, spans = judge_history("TEST.X", "trade", history)
        self.assertEqual(sorted(events), [20, 90])
        self.assertEqual(len(spans), 2, "编号接不上处切开")
        d = detector("trade", minQty=0, minNotional=0)
        for i in range(200, 260):                    # 实时从第 60 秒开始
            d.trade(i, 100, 1, ms + 60_000 + (i - 200) * 500, False)
        window = baseline(d)
        version = d.version
        d.absorb(events, spans)
        d.absorb(events, spans)                      # 重派的洞再交一次: 不重复计数
        self.assertEqual(baseline(d), window)
        self.assertGreater(d.version, version)
        self.assertEqual(d.since, spans[0][0])
        values = bars(30, count=3)
        decorate_bars(values, [d], 30)
        first, second, live = (v["large"] for v in values)
        self.assertEqual((first["count"], first["buy"], first["coverage"]), (1, 50, "partial"))
        self.assertEqual((second["count"], second["sell"], second["cvd"]), (1, 50, 0))
        self.assertEqual(live["cvd"], 0)

    def test_event_cap_drops_the_oldest_by_time_and_stops_covering_before_them(self):
        with patch("large_trades.EVENT_CAP", 2):
            d = detector()
            d.update([raw(i) for i in range(100, 105)] + [raw(105, 40), raw(106, 40, -1)])
            old = {key: {"ns": BASE + t * NS, "price": 100, "qty": 30, "side": 1, "ratio": 5, "z": None,
                         "threshold": 2} for key, t in [(1, 10), (2, 20)]}
            d.absorb(old, [(BASE + 5 * NS, BASE + 25 * NS)])      # 后并进来但时间更早
        self.assertEqual(sorted(d.events), [BASE + 105 * NS, BASE + 106 * NS])
        self.assertEqual(d.base_delta, 60)
        self.assertEqual(d.spans()[0], (BASE + 20 * NS + 1, BASE + 25 * NS))
        values = bars(30, count=4)
        decorate_bars(values, [d], 30)
        self.assertEqual(values[0]["large"]["coverage"], "partial")
        self.assertEqual((values[3]["large"]["count"], values[3]["large"]["cvd"]), (2, 60))

    def test_futures_reset_volume_gap_id_gap_and_nonoverlapping_window(self):
        def frame(offsets, volumes, ids=None):
            result = pd.DataFrame({"datetime": [BASE + i * NS for i in offsets], "volume": volumes,
                                   "last_price": 100., "side_lr": 1})
            result["_key"] = offsets if ids is None else ids
            return result
        initial = frame(range(5), [100, 101, 102, 103, 143])
        d = detector()
        d.sync_futures(initial, initial=True)
        self.assertEqual(len(d.events), 1)
        for tail in [frame([4, 10, 11, 12], [143, 5000, 5300, 5600]),  # 断线补量
                     frame([4, 5, 6, 7], [143, 5000, 5300, 5600], [4, 8, 9, 10]),
                     frame([5, 6, 7], [5000, 5300, 5600])]:  # 无重叠的新窗口
            case = detector()
            case.sync_futures(initial)
            case.sync_futures(tail, changed_ns=int(tail.datetime.iloc[0]))
            self.assertEqual(len(case.events), 1)
            self.assertLess(len(case._window), case.settings["warmup"])

    def test_trading_day_roll_skips_auction_tick_but_keeps_baseline(self):
        def frame(offsets, volumes, ids):
            return pd.DataFrame({"datetime": [BASE + i * NS for i in offsets], "volume": volumes,
                                 "last_price": 100., "side_lr": 1, "_key": ids})
        # 15:00 收盘后 6 小时到夜盘: 首笔累计量回落到 3000(集合竞价), 之后正常成交
        d = detector()
        d.sync_futures(frame(range(5), [50000, 50001, 50002, 50003, 50004], range(5)), initial=True)
        d.sync_futures(frame([4, 21600, 21601, 21602], [50004, 3000, 3001, 3041], range(4, 8)),
                       changed_ns=BASE + 21600 * NS)
        self.assertEqual(qtys(d), [1, 1, 1, 1, 1, 40], "序号连续: 基线沿用, 集合竞价那笔不进基线")
        self.assertEqual([ready for _, ready, _ in d.readiness.values()][-3:], [False, True, True])
        self.assertEqual([e["qty"] for e in d.event_rows()], [40], "开盘后立即可判定")
        # 序号断了才重新预热
        gap = detector()
        gap.sync_futures(frame(range(5), [50000, 50001, 50002, 50003, 50004], range(5)), initial=True)
        gap.sync_futures(frame([4, 21600, 21601, 21602], [50004, 3000, 3001, 3041], [4, 9, 10, 11]),
                         changed_ns=BASE + 21600 * NS)
        self.assertEqual(gap.events, {})
        self.assertLess(len(gap._window), gap.settings["warmup"])

    def test_baseline_window_is_trading_time_and_keeps_warmup_count(self):
        # 时间窗按交易时钟: 6 小时休市只算 GAP_NS(60 秒), 收盘前的事件开盘后仍在 2 分钟窗口里
        d = detector(windowSec=120)
        d.update([raw(i) for i in range(10)])
        d.update([raw(21600 + i) for i in range(5)])
        self.assertEqual(len(baseline(d)), 15)
        d.update([raw(21605 + i) for i in range(60)])
        self.assertEqual(len(baseline(d)), 65, "交易时间超过 2 分钟的收盘前事件滑出")
        # 冷门品种: 时间窗里不够 warmup 个时往前补足, 不会一直预热
        thin = detector(windowSec=60, warmup=5)
        thin.update([raw(i * 600) for i in range(8)])
        self.assertEqual(len(thin._window), 5)
        self.assertTrue(thin.readiness[BASE + 7 * 600 * NS][1])

    def test_futures_notional_floor_uses_contract_multiplier(self):
        d = detector(minQty=0, minNotional=1_000_000)
        rows = [raw(0), raw(1), raw(2), raw(3, 40, price=2000), raw(4, 60, price=2000)]
        d.update(rows)
        self.assertEqual(d.events, {}, "合约乘数未知: 判断不了金额, 不报")
        d = detector(minQty=0, minNotional=1_000_000)
        d.multiplier = 10.0
        d.update(rows)
        self.assertEqual([e["qty"] for e in d.event_rows()], [60], "40 手 × 2000 × 10 = 80 万不够, 60 手 = 120 万")
        self.assertEqual(d.config()["multiplier"], 10.0)

    def test_product_defaults_and_overrides_by_product(self):
        self.assertEqual(product_of("KQ.m@SHFE.fu"), "SHFE.fu")
        self.assertEqual(product_of("SHFE.fu2611"), "SHFE.fu")
        self.assertEqual(product_of("CZCE.TA611"), "CZCE.TA")
        self.assertEqual(product_of("BINANCE.BTCUSDT.P"), "BINANCE.BTCUSDT.P")
        self.assertEqual(product_of("OKX.BTC-USDT-SWAP"), "OKX.BTC-USDT-SWAP")
        for symbol, source in [("KQ.m@SHFE.fu", "snapshot"), ("SHFE.fu2611", "snapshot"),
                               ("BINANCE.BTCUSDT.P", "aggregate"), ("OKX.BTC-USDT-SWAP", "trade")]:
            settings = settings_for(symbol, source)
            self.assertEqual(settings["minNotional"], MIN_NOTIONAL[product_of(symbol)])
            self.assertEqual(settings["minQty"], 0)
            self.assertEqual((settings["windowSec"], settings["lagSec"]), (600, 30))
        unknown = settings_for("SHFE.zz2611", "snapshot")
        self.assertEqual((unknown["minNotional"], unknown["minQty"]), (0, 20), "表里没有的品种沿用 20 手")
        self.assertEqual(settings_for("BINANCE.XYZUSDT.P", "aggregate")["minNotional"], 10000)
        env = '{"*": {"warmup": 50}, "SHFE.fu": {"minNotional": 5000000}, "SHFE.fu2611": {"quantile": 0.995}}'
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": env}):
            month = settings_for("SHFE.fu2611", "snapshot")
            self.assertEqual((month["warmup"], month["minNotional"], month["quantile"]), (50, 5_000_000, 0.995))
            self.assertEqual(settings_for("KQ.m@SHFE.fu", "snapshot")["quantile"], 0.99)

    def test_futures_quiet_gap_with_consecutive_ids_keeps_baseline_and_judges_tick(self):
        def frame(offsets, volumes, ids=None):
            result = pd.DataFrame({"datetime": [BASE + i * NS for i in offsets], "volume": volumes,
                                   "last_price": 100., "side_lr": 1})
            result["_key"] = range(len(offsets)) if ids is None else ids
            return result
        # 200 条预热后静默 6 秒、2 分钟各一次, 序号连续: 都不是断档, 静默后的 300 手照常判为大额。
        offsets = [*range(201), 207, 327, 328]
        volumes = [*range(100, 301), 302, 304, 604]
        d = detector(warmup=200)
        d.sync_futures(frame(offsets, volumes), initial=True)
        self.assertEqual([e["qty"] for e in d.event_rows()], [300])
        self.assertEqual(len(baseline(d)), 203)
        self.assertTrue(all(ready for _, ready, _ in list(d.readiness.values())[201:]))
        # 时间戳回退键不能检查漏号: 超过 GAP_NS 才当断档, 短静默照常。
        fallback = frame(offsets, volumes).assign(_key=lambda f: f.datetime)
        fallback.attrs["key_column"] = "datetime"
        d = detector(warmup=200)
        d.sync_futures(fallback, initial=True)
        self.assertTrue(d.readiness[BASE + 207 * NS][1])
        self.assertGreater(120 * NS, GAP_NS)
        self.assertFalse(d.readiness[BASE + 327 * NS][1])
        self.assertEqual(d.events, {})

    def test_session_close_bar_is_complete_when_no_tick_was_missed(self):
        # 最后一笔在收盘前 0.5 秒; 下一笔是换交易日(累计量回落)的重置记录。
        def frame(ids):
            offsets = [*range(-5, 30), 29.5, 400]
            volumes = [*range(100, 135), 136, 3]
            return pd.DataFrame({"datetime": [BASE + int(i * NS) for i in offsets], "volume": volumes,
                                 "last_price": 100., "side_lr": 1, "_key": ids})
        for ids, expected in [(range(37), "complete"), ([*range(36), 99], "partial")]:
            with self.subTest(ids=ids):
                d = detector()
                d.sync_futures(frame(ids), initial=True)
                values = bars(30, count=1)
                decorate_bars(values, [d], 30)
                self.assertEqual(values[0]["large"]["coverage"], expected)

    def test_crypto_invalid_trade_keeps_sequence_without_resetting_baseline(self):
        d = detector("trade", minQty=0, minNotional=0)
        for i in range(1, 5):
            d.trade(i, 100, 1, BASE // 1_000_000 + i, False)
        d.trade(5, 100, 0, BASE // 1_000_000 + 5, False)            # 零量成交: 没东西可判, 区间不断
        d.trade(6, float("nan"), 3, BASE // 1_000_000 + 6, False)   # 坏价格: 本条不判, 但不是漏号
        d.trade(7, 100, 50, BASE // 1_000_000 + 7, False)
        self.assertEqual(len(baseline(d)), 5)
        self.assertTrue(d.readiness[5][1])
        self.assertFalse(d.readiness[6][1])
        self.assertTrue(d.readiness[7][1])
        self.assertEqual([e["qty"] for e in d.event_rows()], [50])
        json.dumps(d.raw[6], allow_nan=False)

    def test_bad_environment_setting_falls_back_to_defaults_instead_of_breaking_feeds(self):
        for text in ["{", "[]", '{"*": 5}', '{"*": {"minQty": "多"}}', '{"*": {"windowSec": 10}}',
                     '{"*": {"lagSec": 600}}', '{"*": {"lagSec": -1}}', '{"*": {"lagSec": 1.5}}']:
            with self.subTest(text=text), patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": text}):
                with self.assertRaises(ValueError):
                    settings_for("TEST.X", "snapshot")
                with patch("builtins.print"):
                    self.assertEqual(LargeTradeDetector("TEST.X").settings, default_settings("snapshot"))
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": "  "}):
            self.assertEqual(settings_for("TEST.X", "trade"), default_settings("trade"))

    def test_environment_overrides_are_per_symbol_and_validated(self):
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": '{"*":{"warmup":50},"TEST.X":{"minQty":80}}'}):
            self.assertEqual(settings_for("TEST.X", "snapshot")["minQty"], 80)
            self.assertEqual(settings_for("OTHER.Y", "snapshot")["minQty"], 20)
            self.assertEqual(settings_for("TEST.X", "snapshot")["warmup"], 50)
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": '{"*":{"windowSec":30,"warmup":20}}'}):
            with self.assertRaises(ValueError):
                settings_for("TEST.X", "snapshot")


class AggregationTests(unittest.TestCase):
    def test_period_boundary_signed_sums_share_and_cvd_window_anchor(self):
        for source, expected_index in [("snapshot", 0), ("trade", 1)]:
            d = detector(source)
            rows = [raw(i) for i in range(-5, 32)]
            rows[15] = raw(10, 40, 1)
            rows[25] = raw(20, 80, -1)
            rows[30] = raw(25, 120, 0)
            d.update(rows)
            small, big = bars(10), bars(30, count=1)
            decorate_bars(small, [d], 10)
            decorate_bars(big, [d], 30)
            self.assertEqual(small[expected_index]["large"]["buy"], 40)
            full = big[0]["large"]
            self.assertEqual((full["buy"], full["sell"], full["unknown"], full["count"]), (40, 80, 120, 3))
            self.assertEqual(full["delta"], -40)
            self.assertEqual(full["cvd"], -40)
            self.assertEqual(full["coverage"], "complete")
            self.assertEqual(full["share"], .24)
            self.assertEqual(sum(b["large"]["count"] for b in small), full["count"])
            self.assertEqual(sum(b["large"]["delta"] for b in small), full["delta"])
            self.assertEqual(small[-1]["large"]["cvd"], full["cvd"])
            shifted = bars(10, start=20, count=1)
            decorate_bars(shifted, [d], 10)
            self.assertEqual(shifted[0]["large"]["cvd"], -40)  # 滚动图窗不重置 CVD

    def test_unknown_and_uncollected_are_not_zero_selling(self):
        d = detector()
        d.update([raw(1), raw(2), raw(3), raw(4, 40, 0), raw(5)])
        values = bars(10, start=-20, count=4)
        decorate_bars(values, [d], 10)
        self.assertEqual(values[0]["large"]["coverage"], "missing")
        self.assertIsNone(values[0]["large"]["cvd"])
        s = values[2]["large"]
        self.assertEqual((s["unknown"], s["delta"], s["count"]), (40, 0, 1))
        self.assertEqual(s["coverage"], "partial")
        self.assertIsNone(s["share"])

    def test_multiple_sources_keep_baselines_separate_and_missing_venue_is_partial(self):
        a, b = detector("aggregate"), detector("trade")
        a.update([raw(i) for i in range(-5, 32)])
        b.update([raw(i, 100) for i in range(-5, 32)])
        a.update([raw(10, 40)])
        b.update([raw(10, 40)])  # 同样 40, 对另一数据源不是大额
        full = bars(30, count=1)
        decorate_bars(full, [a, b], 30)
        self.assertEqual(full[0]["large"]["buy"], 40)
        self.assertEqual(full[0]["large"]["coverage"], "complete")
        decorate_bars(full, [a, detector("trade")], 30)
        self.assertEqual(full[0]["large"]["coverage"], "partial")
        self.assertIsNone(full[0]["large"]["share"])


class RelativeLayerTests(unittest.TestCase):
    def test_relative_and_original_layers_share_p99_but_never_double_count(self):
        d = detector("aggregate", minQty=0, minNotional=10000)
        d.update([raw(i) for i in range(3)] + [raw(3, 50), raw(4, 100, -1), raw(5, 30, price=1000)])
        self.assertEqual([e["qty"] for e in d.event_rows()], [100])
        self.assertEqual([e["qty"] for e in d.relative.event_rows()], [50])
        self.assertFalse(set(d.events) & set(d.relative.events))
        self.assertEqual(qtys(d), [1, 1, 1, 50, 100, 30], "所有有效事件仍进入同一个基线")
        values = bars(10, count=1)
        decorate_bars(values, [d], 10)
        main, relative = values[0]["large"], values[0]["largeRelative"]
        self.assertEqual((main["count"], main["sell"], main["delta"], main["cvd"]), (1, 100, -100, -100))
        self.assertEqual((relative["count"], relative["buy"], relative["delta"], relative["cvd"]), (1, 50, 50, 50))
        self.assertIsNone(detector().relative, "期货不新增相对异常层")
        no_floor = detector("trade", minQty=0, minNotional=0)
        no_floor.update([raw(i) for i in range(3)] + [raw(3, 50)])
        self.assertFalse(no_floor.relative.events)

    def test_merged_event_promotes_from_hollow_to_original_without_changing_baseline(self):
        d = detector("trade", minQty=0, minNotional=10000)
        ms = BASE // 1_000_000
        for i in range(1, 5):
            d.trade(i, 100, 1, ms+i, False)
        d.trade(5, 100, 60, ms+5, False)
        self.assertEqual(list(d.relative.events), [5])
        self.assertEqual(len(d.relative.event_rows()), 1)  # 先建缓存, 再跨过金额线。
        d.trade(6, 100, 40, ms+5, False)
        self.assertEqual(list(d.events), [5])
        self.assertEqual(d.events[5]["qty"], 100)
        self.assertEqual(d.relative.event_rows(), [])
        self.assertEqual(qtys(d), [1, 1, 1, 1, 100])
        d.trade(6, 100, 40, ms+5, False)
        self.assertEqual(d.events[5]["qty"], 100, "重复推送不重复累计")

    def test_revised_event_replays_both_layers_and_preserves_earlier_results(self):
        rows = [raw(i) for i in range(4)] + [raw(4, 150), raw(5, 160)]
        d = detector("trade", minQty=0, minNotional=10000)
        d.update(rows)
        d.update([raw(4, 50)])
        fresh = detector("trade", minQty=0, minNotional=10000)
        fresh.update(rows[:4] + [raw(4, 50), rows[5]])
        self.assertEqual(d.events, fresh.events)
        self.assertEqual(d.relative.events, fresh.relative.events)
        self.assertEqual(list(d.relative.events), [BASE+4*NS])
        d.update([raw(4, 1)])
        self.assertFalse(d.relative.events, "撤回后空心标记不能残留")

    def test_each_venue_and_timeframe_aggregate_layers_independently(self):
        sources = [detector("aggregate", minQty=0, minNotional=10000), detector("trade", minQty=0, minNotional=10000)]
        for d in sources:
            d.update([raw(i) for i in range(-5, 32)])
            d.update([raw(10, 60), raw(20, 120, -1)])
        small, big = bars(10), bars(30, count=1)
        decorate_bars(small, sources, 10)
        decorate_bars(big, sources, 30)
        for field in ("large", "largeRelative"):
            s = big[0][field]
            self.assertEqual(s["coverage"], "complete")
            self.assertEqual(sum(b[field]["count"] for b in small), s["count"])
            self.assertEqual(sum(b[field].get("delta", 0) for b in small), s["delta"])
            self.assertEqual(small[-1][field]["cvd"], s["cvd"])
        empty = next(b["largeRelative"] for b in small if not b["largeRelative"]["count"])
        self.assertEqual(empty, {"count": 0, "coverage": "complete", "cvd": empty["cvd"]}, "空心层没事件的根只给次数、覆盖和 CVD")
        self.assertIsNotNone(empty["cvd"])
        self.assertIn("delta", next(b["large"] for b in small if not b["large"]["count"]), "原大单层照旧给全")
        self.assertEqual((big[0]["large"]["buy"], big[0]["large"]["sell"]), (0, 240))
        self.assertEqual(big[0]["largeRelative"]["buy"], 120, "两所各6000 USDT仍是空心, 相加后不能升层")
        decorate_bars(big, [sources[0], detector("trade")], 30)
        self.assertEqual(big[0]["largeRelative"]["coverage"], "partial")
        self.assertIsNone(big[0]["largeRelative"]["share"])

    def test_each_layer_has_its_own_retention_coverage_and_cvd(self):
        d = detector("trade", minQty=0, minNotional=10000)
        d.update([raw(i) for i in range(-5, 120)])
        event = lambda t, qty: {"ns": BASE+t*NS, "price": 100, "qty": qty, "side": 1, "ratio": 20, "z": None, "threshold": 1}
        with patch("large_trades.EVENT_CAP", 2):
            d.absorb({t: event(t, 100) for t in (10, 20, 40)}, [],
                     {t: event(t, 50) for t in (30, 50, 60)}, [])
        self.assertEqual(sorted(d.events), [20, 40])
        self.assertEqual(sorted(d.relative.events), [50, 60])
        self.assertEqual((d.base_delta, d.relative.base_delta), (100, 50))
        self.assertEqual(d.spans()[0][0], BASE+10*NS+1)
        self.assertEqual(d.relative.spans()[0][0], BASE+30*NS+1)
        values = bars(30, count=4)
        decorate_bars(values, [d], 30)
        self.assertEqual(values[0]["large"]["count"], 1)
        self.assertEqual(values[0]["largeRelative"]["count"], 0)
        self.assertEqual(values[-1]["large"]["cvd"], 300)
        self.assertEqual(values[-1]["largeRelative"]["cvd"], 150)

    def test_backfill_carries_both_layers_and_is_idempotent_without_touching_live_baseline(self):
        ms = BASE // 1_000_000
        history = [(i, 100, 50 if i == 250 else 150 if i == 280 else 1, ms+i*1000, False) for i in range(300)]
        with patch.dict(os.environ, {"LARGE_TRADE_SETTINGS": "{}"}):
            result = judge_history("TEST.X", "trade", history, include_relative=True)
        events, spans, relative, relative_spans = result
        self.assertEqual(list(events), [280])
        self.assertEqual(list(relative), [250])
        d = detector("trade", minQty=0, minNotional=10000)
        d.update([raw(i) for i in range(400, 410)])
        original = baseline(d)
        d.absorb(*result)
        first = (dict(d.events), dict(d.relative.events), d.spans(), d.relative.spans())
        d.absorb(*result)
        self.assertEqual((d.events, d.relative.events, d.spans(), d.relative.spans()), first)
        self.assertEqual(baseline(d), original)
        self.assertEqual(d.relative.spans()[0], relative_spans[0])
        d.absorb({}, [], {280: {**events[280], "qty": 20}}, [])
        self.assertEqual(d.events, events, "缺口边缘的不完整空心事件不能撤掉已确认原大单")
        self.assertNotIn(280, d.relative.events)

    def test_absorb_keeps_the_more_complete_merge_of_the_same_trade(self):
        d = detector("trade", minQty=0, minNotional=10000)
        ms = BASE // 1_000_000
        for i in range(1, 5):
            d.trade(i, 100, 1, ms+i, False)
        d.trade(5, 100, 40, ms+5, False)
        d.trade(6, 100, 20, ms+5, False)             # 同毫秒合并成 60 币, 仍不足金额线
        full = dict(d.relative.events[5])
        self.assertEqual(full["qty"], 60)
        d.absorb({}, [], {5: {**full, "qty": 20}}, [])
        self.assertEqual(d.relative.events[5], full, "只拿到一部分的补判不能把实时合并的量改小")
        values = bars(30, count=1)
        decorate_bars(values, [d], 30)
        self.assertEqual((values[0]["largeRelative"]["buy"], values[0]["largeRelative"]["cvd"]), (60, 60))
        d.absorb({5: {**full, "qty": 120}}, [], {}, [])
        self.assertEqual((d.events[5]["qty"], d.relative.events), (120, {}), "合并得更全、跨过金额线的升为原大单")

    def test_absorb_skips_results_older_than_either_layer_has_dropped(self):
        d = detector("trade", minQty=0, minNotional=10000)
        d.update([raw(i) for i in range(-5, 120)])
        event = lambda t, qty: {"ns": BASE+t*NS, "price": 100, "qty": qty, "side": 1, "ratio": 20, "z": None, "threshold": 1}
        with patch("large_trades.EVENT_CAP", 2):
            d.absorb({t: event(t, 100) for t in (10, 20, 40)}, [], {}, [])
            self.assertEqual((sorted(d.events), d.base_delta, d._events_floor), ([20, 40], 100, BASE+10*NS))
            # 10 号已丢弃、净量折进原大单 CVD 起点; 重试时同一笔被判成空心, 不能再进空心层重复计
            d.absorb({}, [], {10: event(10, 100), 15: event(15, 50)}, [(BASE, BASE+30*NS)])
        self.assertEqual(sorted(d.relative.events), [15])
        self.assertEqual(d.relative.base_delta, 0)
        self.assertEqual(d.relative._history, [(BASE+10*NS+1, BASE+30*NS)], "丢弃时刻以前不再算空心层已覆盖")
        version = d.version
        d.absorb({10: event(10, 100)}, [(BASE, BASE+10*NS)])
        self.assertEqual((sorted(d.events), d.base_delta, d.version), ([20, 40], 100, version), "原大单层也不重复并入")

    def test_absorb_without_relative_results_leaves_the_hollow_coverage_alone(self):
        d = detector("trade", minQty=0, minNotional=10000)
        span = [(BASE, BASE+10*NS)]
        d.absorb({}, span)
        self.assertEqual((d.spans(), d.relative.spans()), (span, []), "这次没判空心层, 不能显示成已覆盖、零异常")
        d.absorb({}, span, None, [(BASE, BASE+5*NS)])
        self.assertEqual(d.relative.spans(), [(BASE, BASE+5*NS)], "只给空心区间也要并入")
        d.absorb({}, [(BASE+20*NS, BASE+30*NS)], {}, None)
        self.assertEqual(d.relative.spans(), [(BASE, BASE+5*NS), (BASE+20*NS, BASE+30*NS)], "只给空心事件时区间同原大单")


class FeedIntegrationTests(unittest.TestCase):
    def test_futures_native_periods_and_rollup_share_detector_and_publish_revisions(self):
        import ingest
        manager = ingest.FeedManager()
        small = manager._new_feed(("SHFE.fu2611", 10))
        manager.feeds[(small.symbol, 10)] = small
        big = manager._new_feed((small.symbol, 30))
        manager.feeds[(big.symbol, 30)] = big
        rollup = manager._new_feed((big.symbol, 60))
        self.assertIs(small.large_trades, big.large_trades)
        self.assertIs(rollup.large_sources()[0], big.large_trades)
        d = small.large_trades
        d.settings = detector().settings
        d.update([raw(i) for i in range(-5, 32)])
        d.update([raw(10, 40)])
        frame = pd.DataFrame(bars(30, count=1))
        first, _ = big._snapshot_message(0, frame, {}, 1, 0)
        other, _ = big._snapshot_message(5, frame, {}, 1, 0)
        self.assertIs(first["bars"][0]["large"], other["bars"][0]["large"])  # 各粒度同一份, 不重算
        self.assertEqual(first["cfg"]["largeTrades"][0]["source"], "snapshot")
        d.update([raw(10, 1)])
        revised, message = big._snapshot_message(0, frame, {0: first}, 2, 0)
        self.assertEqual(first["bars"][0]["large"]["count"], 1)  # 旧快照不能被就地改掉
        self.assertEqual(revised["bars"][0]["large"]["count"], 0)
        self.assertEqual(message["bars"][0]["large"]["count"], 0)

    def test_futures_feed_waits_for_multiplier_then_backfills_whole_tick_window(self):
        import tempfile
        import ingest
        from test_data import ticks, klines
        with tempfile.TemporaryDirectory() as temp, patch.object(ingest, "DATA_DIR", temp):
            feed = ingest.Feed("SHFE.fu2611")
            feed.quote = SimpleNamespace(price_tick=1.)        # 报价还没带合约乘数
            feed.klines = klines()
            feed.ticks = ticks()
            feed.recompute(lambda _: None)
            self.assertEqual(feed.large_trades.raw, {})
            feed.quote = SimpleNamespace(price_tick=1., volume_multiple=10)
            feed.ticks = ticks(offsets=(0.5, 1, 30.5, 31, 60.5, 61), prices=(101, 103, 101, 103, 103, 103),
                               volumes=(100, 110, 120, 130, 140, 150))
            feed.recompute(lambda _: None)
            self.assertEqual(feed.large_trades.multiplier, 10.0)
            self.assertEqual(len(feed.large_trades.raw), 6, "不能只取最新一次变化, 整个窗口都要补上")
            self.assertEqual(feed.cfg()["largeTrades"][0]["multiplier"], 10.0)

    def test_crypto_manager_deduplicates_shared_detector_and_backfill_does_not_fake_signals(self):
        from binance_feed import BinanceAdapter
        from crypto_feed import CryptoManager
        adapter = BinanceAdapter()
        instrument = adapter.builtin_instruments()[0]
        manager = CryptoManager([adapter])
        for tf in (10, 30):
            feed = manager._new_feed(instrument, tf)
            feed.loaded = True
            manager.feeds[(instrument.symbol, tf)] = feed
        a, b = manager.feeds.values()
        self.assertIs(a.large_trades, b.large_trades)
        self.assertIsNone(b.trades.large_trades)  # 只由一个周期喂成交
        self.assertEqual(a.large_trades.source, "aggregate")
        a.large_trades.settings = detector("aggregate", minQty=0, minNotional=10000).settings
        for i, qty in enumerate([1, 1, 1, 120], 1):
            manager._apply_event(("trade", instrument.symbol, i, 100., qty, BASE // 1_000_000 + i * 1000, False))
        self.assertIs(a.large_trades, b.large_trades)
        self.assertEqual(len(a.large_trades.raw), 4)
        self.assertEqual(len(a.large_trades.events), 1)
        manager._apply_event(("trade", instrument.symbol, 4, 100., 120, BASE // 1_000_000 + 4000, False))
        self.assertEqual(len(a.large_trades.events), 1)
        manager._apply_event(("trade", instrument.symbol, 9, 100., 1, BASE // 1_000_000 + 9000, False))
        a.trades.insert([(5, 100., 1000, BASE // 1_000_000 + 5000, True)], before_id=9)
        self.assertEqual(len(a.large_trades.events), 1)
        self.assertFalse(a.large_trades.readiness[9][1])


    def test_crypto_manager_absorbs_backfill_judgement_into_the_shared_detector(self):
        from binance_feed import BinanceAdapter
        from crypto_feed import CryptoManager
        adapter = BinanceAdapter()
        instrument = adapter.builtin_instruments()[0]
        results = queue.Queue()
        manager = CryptoManager([adapter], backfiller=SimpleNamespace(results=results, bind=lambda adapters: None))
        for tf in (10, 30):
            feed = manager._new_feed(instrument, tf)
            feed.loaded = True
            manager.feeds[(instrument.symbol, tf)] = feed
        event = {"ns": BASE, "price": 100.0, "qty": 3.0, "side": -1, "ratio": 9.0, "z": None, "threshold": 1.0}
        results.put(("large", instrument.symbol, {7: event}, [(BASE - NS, BASE + NS)]))
        results.put(("large", "BINANCE.NOPEUSDT.P", {8: event}, []))       # 已经不采集的合约: 忽略
        manager._apply_results(0.0)
        detector = manager.feeds[(instrument.symbol, 10)].large_trades
        self.assertEqual(detector.events, {7: event})
        self.assertEqual(detector.spans(), [(BASE - NS, BASE + NS)])
        self.assertIn(instrument.symbol, manager._dirty)

        relative = {**event, "qty": 1, "ns": BASE+NS}
        results.put(("large", instrument.symbol, {}, [], {9: relative}, [(BASE, BASE+2*NS)]))
        manager._apply_results(0.0)
        self.assertEqual(detector.relative.events, {9: relative})
        self.assertEqual(detector.relative.spans(), [(BASE, BASE+2*NS)])
        feed = manager.feeds[(instrument.symbol, 30)]
        frame = pd.DataFrame(bars(30, count=1))
        first, _ = feed._snapshot_message(0, frame, {}, 1, 0)
        other, _ = feed._snapshot_message(5, frame, {}, 1, 0)
        self.assertIs(first["bars"][0]["largeRelative"], other["bars"][0]["largeRelative"])
        self.assertEqual(first["bars"][0]["largeRelative"]["count"], 1)
        detector.absorb({9: {**relative, "qty": 120}}, [], {}, [])
        revised, message = feed._snapshot_message(0, frame, {0: first}, 2, 0)
        self.assertEqual(first["bars"][0]["largeRelative"]["count"], 1, "缓存不能就地修改旧快照")
        self.assertEqual(revised["bars"][0]["largeRelative"]["count"], 0)
        self.assertEqual(message["bars"][0]["largeRelative"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
