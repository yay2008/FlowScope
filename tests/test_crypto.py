"""加密永续行情: bar 窗口与洞、回填(归档包/REST/两端插回)、推送解析、管理线程, 以及 app 按前缀分流。"""
import asyncio
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
from fastapi import HTTPException

import app as server
import crypto_feed
import ingest
from binance_feed import BinanceAdapter
from crypto_backfill import DAY_MS, Backfiller, FetchError, NotPublished, RestBlocked
from crypto_feed import (CandleStore, Channel, CryptoFeed, CryptoManager, Instrument, TradeBars,
                         aggregate_trades)
from indicator import TF_OPTIONS, TZ_SHIFT_S, ltf_options
from okx_feed import OkxAdapter

DAY0 = 1_790_985_600_000            # 2026-10-03 00:00 UTC
BASE = DAY0 + 11 * 3_600_000         # 一个整点, 30s 对齐
BTC = BinanceAdapter().builtin_instruments()[0]
FAKE = Instrument(symbol="FAKE.X", venue="FAKE", inst_id="X", base="X", label="X 永续 · 测试",
                  tick_size=0.1, step_size=0.001)


def display(ms_offset, tf=10, base=BASE):
    """距 base 的毫秒偏移 -> 所在 bar 的展示秒。"""
    start = (base + ms_offset) // (tf * 1000) * tf * 1000
    return start // 1000 + TZ_SHIFT_S


def make_trades(start_ms, count, step_ms=5000, first_id=1, quiet=()):
    """确定性的逐笔成交; quiet 里的序号不出现(时间空着, 编号照样连续 = 那段时间确实没成交)。"""
    rows = []
    trade_id = first_id
    for index in range(count):
        if index in quiet:
            continue
        rows.append((trade_id, 100.0 + (index * 7 % 11) - (index * 3 % 5), 0.001 * (1 + index % 7),
                     start_ms + index * step_ms + 2000, index % 3 == 0))
        trade_id += 1
    return pd.DataFrame(rows, columns=["id", "price", "qty", "t", "sell"])


class TradeBarsTests(unittest.TestCase):
    def test_ohlcv_and_taker_side_come_straight_from_trades(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 0.5, BASE + 1000, False)
        bars.add(2, 103.0, 0.25, BASE + 2000, True)
        bars.add(3, 99.0, 0.125, BASE + 3000, True)
        bars.add(4, 101.0, 1.0, BASE + 12000, False)
        frame = bars.frame()
        first = frame.iloc[0]
        self.assertEqual((first.open, first.high, first.low, first.close), (100.0, 103.0, 99.0, 99.0))
        self.assertEqual((first.volume, first.buy, first.sell, first.unknown), (0.875, 0.5, 0.375, 0.0))
        # 本次运行的第一笔之前是个洞; 之后编号连续就是完整的
        self.assertEqual(list(frame.coverage), ["partial", "complete"])
        self.assertEqual(len(bars.holes), 1)

    def test_quiet_bars_with_contiguous_ids_become_zero_volume_bars(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 1.0, BASE + 1000, False)
        bars.add(2, 105.0, 1.0, BASE + 41000, False)
        frame = bars.frame()
        self.assertEqual(list(frame.time), [display(i * 10000) for i in range(5)])
        quiet = frame.iloc[1:4]
        self.assertTrue((quiet.volume == 0).all() and (quiet.close == 100.0).all())
        self.assertEqual(list(frame.coverage), ["partial"] + ["complete"] * 4)

    def test_id_gap_marks_both_ends_partial_and_does_not_fill(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 1.0, BASE + 1000, False)
        bars.add(2, 100.0, 1.0, BASE + 11000, False)
        bars.add(9, 101.0, 1.0, BASE + 41000, True)
        frame = bars.frame()
        self.assertEqual(list(frame.time), [display(0), display(10000), display(40000)])
        self.assertEqual(list(frame.coverage), ["partial", "partial", "partial"])
        self.assertEqual(bars.gaps, 1)
        self.assertEqual(bars.holes[1], [2, BASE + 11000, 9, BASE + 41000])

    def test_inserting_the_missing_trades_closes_a_gap(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 1.0, BASE + 1000, False)
        bars.add(2, 100.0, 1.0, BASE + 11000, False)
        bars.add(9, 101.0, 1.0, BASE + 41000, True)
        bars.holes.pop(0)                       # 只看断线这个洞
        # 3~8 号: 第二根 bar 里两笔, 第三根 bar 没有成交, 第四根三笔, 第五根(洞右端)一笔在 9 号之前
        missing = [(3, 99.0, 1.0, BASE + 12000, True), (4, 98.0, 1.0, BASE + 15000, True),
                   (5, 102.0, 1.0, BASE + 31000, False), (6, 103.0, 1.0, BASE + 32000, False),
                   (7, 104.0, 1.0, BASE + 33000, False), (8, 105.0, 2.0, BASE + 40500, False)]
        self.assertTrue(bars.insert(missing, 9))
        frame = bars.frame()
        self.assertEqual(list(frame.time), [display(i * 10000) for i in range(5)])
        self.assertEqual(list(frame.coverage), ["complete"] * 5)
        self.assertEqual(frame.iloc[2].volume, 0.0)                    # 确实没成交的 bar 补零量
        self.assertEqual(frame.iloc[2].close, 98.0)
        edge = frame.iloc[4]                                           # 编号更小的定开盘
        self.assertEqual((edge.open, edge.close, edge.volume, edge.buy, edge.sell), (105.0, 101.0, 3.0, 2.0, 1.0))
        self.assertFalse(bars.insert(missing, 9))                      # 洞已经没了

    def test_backfilled_bars_replace_partial_but_not_complete_live_bars(self):
        bars = TradeBars(10)
        bars.add(10, 100.0, 1.0, BASE + 21000, False)
        bars.add(11, 101.0, 1.0, BASE + 31000, False)
        bars.put_stored([BASE, BASE + 10000, BASE + 20000, BASE + 30000],
                        [[1, 1, 1, 1, 1]] * 3 + [[9, 9, 9, 9, 9]])
        frame = bars.frame()
        self.assertEqual(list(frame.coverage), ["missing", "missing", "missing", "complete"])
        self.assertEqual(frame.iloc[2].close, 1.0)                     # 洞右端: 用落盘的完整 bar
        self.assertEqual(frame.iloc[3].close, 101.0)                   # 完整的实时 bar 不被覆盖
        self.assertTrue(np.isnan(frame.iloc[0].buy))                   # 买卖量留给 HistoryStore 补

    def test_replayed_or_older_trades_are_ignored(self):
        bars = TradeBars(10)
        self.assertTrue(bars.add(5, 100.0, 1.0, BASE + 1000, False))
        self.assertFalse(bars.add(5, 100.0, 1.0, BASE + 1000, False))
        self.assertFalse(bars.add(4, 100.0, 1.0, BASE + 900, False))
        self.assertEqual(bars.frame().iloc[0].volume, 1.0)

    def test_float_dust_is_rounded_away(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 0.1, BASE + 1000, False)
        bars.add(2, 100.0, 0.2, BASE + 2000, False)
        self.assertEqual(bars.frame().iloc[0].volume, 0.3)

    def test_kline_split_classifies_whole_micro_bars(self):
        bars = TradeBars(10)
        bars.add(1, 100.0, 1.0, BASE + 1000, False)
        bars.add(2, 101.0, 1.0, BASE + 2000, True)
        bars.add(3, 102.0, 1.0, BASE + 4000, False)
        bars.add(4, 102.0, 0.5, BASE + 5000, False)
        bars.add(5, 101.0, 1.5, BASE + 6000, True)
        bars.add(6, 101.0, 4.0, BASE + 11000, False)
        frame = bars.frame(5)
        self.assertEqual((frame.iloc[0].buy, frame.iloc[0].sell), (3.0, 2.0))
        self.assertEqual((frame.iloc[1].buy, frame.iloc[1].sell), (0.0, 0.0))

    def test_window_keeps_latest_bars_only(self):
        bars = TradeBars(10, max_bars=5)
        for i in range(200):
            bars.add(i + 1, 100.0 + i % 3, 1.0, BASE + i * 10000, False)
        frame = bars.frame()
        self.assertEqual(len(frame), 5)
        self.assertEqual(frame.iloc[-1].time, display(199 * 10000))
        self.assertLessEqual(len(bars.bars), 5 + crypto_feed.TRIM_SLACK)
        self.assertGreaterEqual(min(bars.seconds), min(bars.bars))
        self.assertEqual(bars.holes, [])                               # 开头那个洞早已出了窗口


class AggregateTests(unittest.TestCase):
    def test_bulk_aggregation_matches_live_bars(self):
        trades = make_trades(BASE, 400, step_ms=1700, quiet=range(40, 60))
        live = {tf: TradeBars(tf) for tf in TF_OPTIONS}
        for row in trades.itertuples(index=False):
            for bars in live.values():
                bars.add(int(row.id), float(row.price), float(row.qty), int(row.t), bool(row.sell))
        end = int(trades.t.iloc[-1]) // 30000 * 30000
        for tf in TF_OPTIONS:
            bulk = aggregate_trades(trades, tf, ltf_options(tf), BASE, end)
            for ltf in ltf_options(tf):
                frame = live[tf].frame(ltf).set_index("time")
                times = bulk.index // 1000 + TZ_SHIFT_S
                expected = frame.loc[times]
                np.testing.assert_allclose(bulk[["open", "high", "low", "close", "volume"]].to_numpy(),
                                           expected[["open", "high", "low", "close", "volume"]].to_numpy())
                buy, sell = ("buy", "sell") if ltf == 0 else (f"buy{ltf}", f"sell{ltf}")
                np.testing.assert_allclose(bulk[buy].to_numpy(), expected["buy"].to_numpy(), err_msg=f"{tf}/{ltf}")
                np.testing.assert_allclose(bulk[sell].to_numpy(), expected["sell"].to_numpy())

    def test_quiet_bars_carry_close_and_leading_quiet_bars_are_dropped(self):
        trades = make_trades(BASE + 25000, 3, step_ms=20000)       # 第一笔在 27 秒
        bulk = aggregate_trades(trades, 10, [0], BASE, BASE + 90000)
        self.assertEqual(list(bulk.index), [BASE + 20000 + i * 10000 for i in range(7)])
        self.assertEqual(bulk.loc[BASE + 30000, "volume"], 0.0)
        self.assertEqual(bulk.loc[BASE + 30000, "close"], bulk.loc[BASE + 20000, "close"])   # 价格沿用前收


class CandleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "candles.csv")

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read().splitlines()

    def test_live_bars_are_written_once_and_again_when_they_change(self):
        bars = TradeBars(10)
        for i, offset in enumerate([1000, 11000, 21000]):
            bars.add(i + 10, 100.0 + i, 1.0, BASE + offset, False)
        store = CandleStore(self.path)
        store.load()
        store.save(bars.frame())
        store.save(bars.frame())
        self.assertEqual(len(self.rows()), 3)                     # 表头 + 两根走完的 bar
        bars.insert([(9, 90.0, 1.0, BASE + 500, True)], 10)        # 回填补上第一根缺的那笔
        store.save(bars.frame())
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(self.rows()[-1].startswith(f"{display(0)},90.0,"))
        reloaded = CandleStore(self.path).load()
        self.assertEqual(list(reloaded.time), [display(0), display(10000)])
        self.assertEqual(reloaded.iloc[0].open, 90.0)              # 后写的行优先

    def test_reload_takes_latest_rows_and_skips_bad_lines(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("time,open,high,low,close,volume\n10,1.0,1.0,1.0,1.0,1.0\n20,2.0,2.0,2.0,2.0,2.0\n"
                         "broken line\n20,2.5,2.5,2.5,2.5,2.5\n30,3.0,3.0,3.0,3.0,3.0")
        frame = CandleStore(self.path, window=2).load()
        self.assertEqual(list(frame.time), [20, 30])
        self.assertEqual(frame.iloc[0].close, 2.5)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def feed(self, trades=(), tf=10):
        feed = CryptoFeed(BTC, tf)
        feed.load_history()
        for trade in trades:
            feed.trades.add(*trade)
        feed.recompute(lambda _: None)
        return feed

    def test_snapshot_carries_exact_split_and_instrument_digits(self):
        feed = self.feed([(1, 100.0, 0.5, BASE + 1000, False), (2, 100.1, 0.25, BASE + 2000, True)])
        snapshot = feed.snapshot_for(0)
        self.assertEqual((snapshot["cfg"]["priceDigits"], snapshot["cfg"]["volumeDigits"]), (1, 3))
        self.assertEqual(snapshot["cfg"]["ltfOptions"], [0, 1, 5, 10])
        bar = snapshot["bars"][-1]
        self.assertEqual((bar["buy"], bar["sell"], bar["delta"]), (0.5, 0.25, 0.25))

    def test_restart_restores_candles_and_confirmed_volume(self):
        self.feed([(i + 1, 100.0 + i, 1.0 + i, BASE + i * 10000 + 1000, i % 2 == 1) for i in range(4)])
        restarted = self.feed()
        bars = restarted.snapshot_for(0)["bars"]
        self.assertEqual([bar["close"] for bar in bars], [100.0, 101.0, 102.0])
        self.assertEqual([(bar["buy"], bar["sell"]) for bar in bars], [(1.0, 0.0), (0.0, 2.0), (3.0, 0.0)])
        self.assertEqual([bar["coverage"] for bar in bars], ["partial", "complete", "complete"])
        self.assertEqual(bars[-1]["cvd"], 2.0)

    def test_backfill_writes_every_granularity_and_shows_up_complete(self):
        feed = self.feed([(500, 100.0, 1.0, BASE + 60000 + 1000, False)], tf=30)
        trades = make_trades(BASE, 12, step_ms=5000)
        feed.apply_backfill(aggregate_trades(trades, 30, ltf_options(30), BASE, BASE + 60000))
        feed.recompute(lambda _: None)
        bars = feed.snapshot_for(0)["bars"]
        self.assertEqual([bar["coverage"] for bar in bars], ["complete", "complete", "partial"])
        self.assertEqual(feed.complete_starts(), {BASE, BASE + 30000})
        for ltf in ltf_options(30):
            self.assertEqual(len(feed._store(ltf).values), 2, ltf)
        feed.request(ltf=5, demand=True)
        feed.recompute(lambda _: None)
        self.assertEqual(feed.snapshot_for(5)["bars"][0]["coverage"], "complete")
        self.assertEqual(len(CryptoFeed(BTC, 30)._store(0).values), 2)   # 写进了文件

    def test_footprint_is_empty_instead_of_pending(self):
        self.assertEqual(self.feed([(1, 100.0, 1.0, BASE + 1000, False)]).footprint_snapshot()["bars"], [])


class FakeVenue:
    """假交易所: 成交表在内存里; 归档包只有 published_before 之前的日子, REST 可以设成被拒。"""

    venue = "FAKE"
    archive_tz_ms = 0

    def __init__(self, trades, published_before=None, blocked=False, channels=()):
        self.trades = trades
        self.published_before = published_before
        self.blocked = blocked
        self.channels = list(channels)
        self.calls = []

    def builtin_instruments(self):
        return [FAKE]

    def instruments_loaded(self, instruments):
        pass

    def load_instruments(self, http):
        raise FetchError("测试不取合约列表")

    def archive_trades(self, http, instrument, day):
        self.calls.append(("day", day))
        if self.published_before is None or day >= self.published_before:
            raise NotPublished("还没发布")
        return self.trades[(self.trades.t >= day) & (self.trades.t < day + DAY_MS)]

    def rest_trades(self, http, instrument, start, end, after_id, before_id):
        self.calls.append(("rest", start, end))
        if self.blocked:
            raise RestBlocked("HTTP 451")
        # 故意多给一点段外的成交: 回填线程自己按时间和编号筛
        return self.trades[(self.trades.t >= start - 3000) & (self.trades.t < end + 3000)]


def drain(results):
    items = []
    while not results.empty():
        items.append(results.get_nowait())
    return items


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.trades = make_trades(DAY0, 2 * 17280 + 1800, quiet=range(20000, 20010))
        # 第一笔实时成交不在 30 秒整点上: 它之前同一根 bar 里还有几笔要插回内存
        live = self.trades[self.trades.t >= DAY0 + 2 * DAY_MS + 2 * 3_600_000 + 20000].iloc[0]
        self.before_id, self.before_ms = int(live.id), int(live.t)

    def run_job(self, venue, complete=None, days=2):
        backfiller = Backfiller(days=days)
        backfiller.bind({"FAKE": venue})
        job = backfiller.job(FAKE, None, None, self.before_id, self.before_ms,
                             complete or {tf: set() for tf in TF_OPTIONS})
        backfiller._fill(job)
        return job, drain(backfiller.results), backfiller

    def test_segments_go_newest_first_by_archive_day_then_hour(self):
        okx = SimpleNamespace(archive_tz_ms=8 * 3_600_000)
        segments = Backfiller().segments_of(okx, DAY0 + 14 * 3_600_000, DAY0 + DAY_MS + 18 * 3_600_000 + 5)
        # OKX 按北京时间切日: UTC 16:00 是日界; 中间那一整天走归档包
        self.assertEqual(segments[0], ("hour", None, DAY0 + DAY_MS + 18 * 3_600_000, DAY0 + DAY_MS + 18 * 3_600_000 + 5))
        self.assertIn(("day", DAY0 + 16 * 3_600_000, DAY0 + 16 * 3_600_000, DAY0 + DAY_MS + 16 * 3_600_000), segments)
        self.assertEqual(segments[-1], ("hour", None, DAY0 + 14 * 3_600_000, DAY0 + 15 * 3_600_000))

    def test_start_hole_uses_archive_days_and_rest_hours_and_returns_the_edge(self):
        venue = FakeVenue(self.trades, published_before=DAY0 + 2 * DAY_MS)
        job, results, _ = self.run_job(venue)
        self.assertIn(("day", DAY0 + DAY_MS), venue.calls)
        self.assertNotIn(("day", DAY0 + 2 * DAY_MS), venue.calls)       # 半天的走 REST
        bars = [item for item in results if item[0] == "bars"]
        done = results[-1]
        self.assertEqual(done[:3], ("done", "FAKE.X", self.before_id))
        self.assertIsNone(done[4])
        known = self.trades[self.trades.id < self.before_id]
        for tf in TF_OPTIONS:
            got = pd.concat([item[3] for item in bars if item[2] == tf]).sort_index()
            expected = aggregate_trades(known, tf, ltf_options(tf), job.start_ms, self.before_ms + 1)
            pd.testing.assert_frame_equal(got, expected, check_freq=False, check_names=False)
        edge = known[known.t >= self.before_ms // 30000 * 30000]
        self.assertEqual(len(edge), 4)
        self.assertEqual([row[0] for row in done[3]], edge.id.tolist())

    def test_unpublished_archive_day_falls_back_to_rest(self):
        venue = FakeVenue(self.trades, published_before=DAY0 + DAY_MS)
        _, results, _ = self.run_job(venue)
        self.assertIn(("rest", DAY0 + DAY_MS, DAY0 + DAY_MS + 3_600_000), venue.calls)
        self.assertIsNone(results[-1][4])

    def test_blocked_rest_keeps_archive_days_and_reports_the_hole_unfinished(self):
        venue = FakeVenue(self.trades, published_before=DAY0 + 2 * DAY_MS, blocked=True)
        _, results, backfiller = self.run_job(venue)
        days = {item[3].index.min() // DAY_MS for item in results if item[0] == "bars"}
        self.assertEqual(days, {(DAY0 + DAY_MS) // DAY_MS})
        self.assertIsNone(results[-1][3])
        self.assertIn("REST", results[-1][4])
        self.assertIn("FAKE", backfiller.snapshot()["restBlocked"])
        rest_calls = [call for call in venue.calls if call[0] == "rest"]
        self.assertEqual(len(rest_calls), 1)                            # 被拒一次, 之后不再试

    def test_complete_bars_are_not_fetched_again(self):
        venue = FakeVenue(self.trades, published_before=DAY0 + 2 * DAY_MS)
        complete = {tf: set(range(DAY0 + DAY_MS, DAY0 + 2 * DAY_MS, tf * 1000)) for tf in TF_OPTIONS}
        self.run_job(venue, complete)
        self.assertNotIn(("day", DAY0 + DAY_MS), venue.calls)


class FakeSpec:
    """假推送通道: topic 形如 X@trade; 订阅变化发一条 JSON 记录。"""

    name = "trades"
    ping = None
    ping_sec = 0.0

    def topic(self, kind, inst_id):
        return f"{inst_id}@{kind}" if kind == "trade" else None

    def url(self, topics):
        return "fake://" + ",".join(sorted(topics))

    def subscribe_messages(self, topics):
        return []

    def change_messages(self, current, wanted):
        return [json.dumps({"sub": sorted(wanted - current), "unsub": sorted(current - wanted)})]

    def parse(self, raw):
        data = json.loads(raw)
        return [("trade", "FAKE.X", data["id"], data["price"], data["qty"], data["t"], data["sell"])]


class FakeSocket:
    def __init__(self, messages, drop=False):
        self.messages = list(messages)
        self.drop = drop
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        if self.drop:
            raise ConnectionError("测试断线")
        time.sleep(timeout or 0)
        raise TimeoutError


def trade_message(row):
    return json.dumps({"id": int(row.id), "price": float(row.price), "qty": float(row.qty),
                       "t": int(row.t), "sell": bool(row.sell)})


def wait_for(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("等待超时")
        time.sleep(0.05)


class ChannelTests(unittest.TestCase):
    def test_subscriptions_follow_the_wanted_set_on_the_same_connection(self):
        wanted = {"value": frozenset({"X@trade"})}
        sockets, events = [], []

        def connect(url):
            sockets.append(FakeSocket([json.dumps({"id": 1, "price": 1.0, "qty": 1.0, "t": 1, "sell": False})]))
            return sockets[-1]

        channel = Channel("FAKE.trades", FakeSpec(), lambda: wanted["value"], events.append, connect)
        channel.start()
        try:
            wait_for(lambda: events)
            wanted["value"] = frozenset({"X@trade", "Y@trade"})
            wait_for(lambda: sockets[0].sent)
            self.assertEqual(json.loads(sockets[0].sent[0]), {"sub": ["Y@trade"], "unsub": []})
            self.assertEqual(channel.snapshot()["status"], "connected")
        finally:
            channel.stop()
        self.assertEqual(len(sockets), 1)                               # 增减订阅不断线
        self.assertEqual(events[0][:3], ("trade", "FAKE.X", 1))

    def test_silent_connection_is_dropped(self):
        channel = Channel("FAKE.trades", FakeSpec(), lambda: frozenset({"X@trade"}), lambda _: None)
        with patch.object(crypto_feed, "STALE_SEC", 0.3):
            with self.assertRaises(ConnectionError):
                channel._pump(FakeSocket([]), frozenset({"X@trade"}))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(ingest, "DATA_DIR", self.temp.name),
                        patch.dict(crypto_feed.VENUE_NAMES, {"FAKE": "测试"})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_live_stream_and_backfill_meet_without_a_seam(self):
        trades = make_trades(DAY0, 17280 + 900)
        cut = int(np.searchsorted(trades.t.to_numpy(), DAY0 + DAY_MS + 1_800_000))
        live = trades.iloc[cut:cut + 30]
        venue = FakeVenue(trades.iloc[:cut + 30], published_before=DAY0 + DAY_MS, channels=[FakeSpec()])
        manager = CryptoManager([venue], backfiller=Backfiller(days=1), defaults=("FAKE.X",),
                                connect=lambda url: FakeSocket([trade_message(row) for row in live.itertuples()]))

        async def exercise():
            manager.start(asyncio.get_running_loop())
            try:
                def settled():
                    feed = manager.feeds.get(("FAKE.X", 30))
                    if feed is None:
                        return False
                    snapshot = feed.snapshot_for(0)
                    # 收完实时成交、洞已补完, 且回填后的完整快照已发布; 无客户端时重算会延后。
                    return (feed.trades.last_id == int(live.id.iloc[-1])
                            and not feed.trades.holes and not manager._holes
                            and snapshot is not None and len(snapshot["bars"]) == ingest.SNAPSHOT_BARS
                            and all(bar["coverage"] == "complete" for bar in snapshot["bars"][:-1]))

                await asyncio.to_thread(wait_for, settled, 20)
            finally:
                manager.stop()

        with patch("builtins.print"):
            asyncio.run(exercise())
        bars = manager.feeds[("FAKE.X", 30)].snapshot_for(0)["bars"]
        self.assertEqual(len(bars), 800)
        self.assertEqual({bar["coverage"] for bar in bars[:-1]}, {"complete"})   # 实时第一根也补全了
        known = trades.iloc[:cut + 30]
        last = known[known.t // 30000 * 30000 == known.t.iloc[-1] // 30000 * 30000]
        self.assertAlmostEqual(bars[-1]["volume"], round(last.qty.sum(), 8))
        status = manager.status_snapshot()
        self.assertEqual((status["holes"], status["gaps"]["FAKE.X"]), (0, 0))
        self.assertEqual(status["backfill"]["restBlocked"], {})

    def test_unknown_symbols_and_pinned_limit(self):
        manager = CryptoManager([FakeVenue(make_trades(DAY0, 1), channels=[FakeSpec()])], defaults=("FAKE.X",))
        with self.assertRaisesRegex(ValueError, "不支持"):
            manager.ensure("FAKE.Y", 10)
        self.assertEqual(manager.set_pinned(["FAKE.Y", "FAKE.X"]), ["FAKE.X"])
        manager.touch("FAKE.X", ["book"], 30)
        manager._sync(time.monotonic())
        self.assertEqual(manager._topics["FAKE.trades"], frozenset({"X@trade"}))   # 这个假通道不收盘口


class ParserTests(unittest.TestCase):
    def test_binance_messages(self):
        market, public = BinanceAdapter().channels
        trade = market.parse(json.dumps({"stream": "btcusdt@aggTrade", "data": {
            "e": "aggTrade", "s": "BTCUSDT", "a": 7, "p": "85000.1", "q": "0.002", "T": 1700, "m": True}}))
        self.assertEqual(trade, [("trade", "BINANCE.BTCUSDT.P", 7, 85000.1, 0.002, 1700, True)])
        ticker = market.parse(json.dumps({"data": {"e": "24hrTicker", "s": "BTCUSDT", "c": "2", "o": "1", "h": "3",
                                                   "l": "1", "v": "10", "q": "15", "P": "100", "E": 5}}))
        self.assertEqual(ticker[0][2]["changePct"], 100.0)
        mark = market.parse(json.dumps({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "1", "i": "1",
                                                 "r": "0.0001", "T": 99, "E": 5}}))
        self.assertEqual((mark[0][2]["fundingRate"], mark[0][2]["nextFundingTime"]), (0.0001, 99))
        book = public.parse(json.dumps({"data": {"e": "depthUpdate", "s": "BTCUSDT", "b": [["1", "2"]],
                                                 "a": [["1.1", "3"]], "E": 5}}))
        self.assertEqual(book[0][2]["asks"], [[1.1, 3.0]])
        self.assertEqual(market.parse('{"result": null, "id": 1}'), [])
        self.assertEqual(market.topic("trade", "BTCUSDT"), "btcusdt@aggTrade")
        self.assertIsNone(market.topic("book", "BTCUSDT"))
        changes = [json.loads(m) for m in market.change_messages(frozenset({"a"}), frozenset({"b"}))]
        self.assertEqual([(m["method"], m["params"]) for m in changes], [("SUBSCRIBE", ["b"]), ("UNSUBSCRIBE", ["a"])])
        self.assertIn("/market/stream?streams=", market.url({"btcusdt@aggTrade"}))

    def test_okx_messages_convert_contracts_to_coins(self):
        business, public = OkxAdapter().channels
        trade = business.parse(json.dumps({"arg": {"channel": "trades-all", "instId": "BTC-USDT-SWAP"}, "data": [
            {"tradeId": "11", "px": "85000", "sz": "2.5", "side": "sell", "ts": "1700"}]}))
        self.assertEqual(trade[0][:4] + trade[0][5:], ("trade", "OKX.BTC-USDT-SWAP", 11, 85000.0, 1700, True))
        self.assertAlmostEqual(trade[0][4], 0.025)                      # 2.5 张 x 0.01 BTC
        book = public.parse(json.dumps({"arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"}, "data": [
            {"bids": [["1", "100", "0", "1"]], "asks": [["2", "50", "0", "1"]], "ts": "5"}]}))
        self.assertEqual(book[0][2]["bids"], [[1.0, 1.0]])
        funding = public.parse(json.dumps({"arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
                                           "data": [{"fundingRate": "0.0002", "fundingTime": "99"}]}))
        self.assertEqual(funding[0][2], {"fundingRate": 0.0002, "nextFundingTime": 99})
        self.assertEqual(public.parse("pong"), [])
        self.assertEqual(business.parse(json.dumps({"arg": {"channel": "trades-all", "instId": "ETH-USDT-SWAP"},
                                                    "data": [{"tradeId": "1", "px": "1", "sz": "1", "side": "buy",
                                                              "ts": "1"}]})), [])   # 面值未知不收
        args = json.loads(public.subscribe_messages({public.topic("mark", "BTC-USDT-SWAP")})[0])["args"]
        self.assertEqual(args, [{"channel": "mark-price", "instId": "BTC-USDT-SWAP"},
                                {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}])


class FakeHttp:
    """按 URL 回放响应; 记录请求参数。"""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return (value(params) if callable(value) else value), {}

    def json(self, url, params=None):
        body, headers = self.get(url, params)
        return json.loads(body), headers


def zipped(name, text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class ArchiveAndRestTests(unittest.TestCase):
    def test_binance_archive_with_and_without_header_and_checksum(self):
        url = "https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-10-03.zip"
        for text in ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
                     "2,1.5,0.1,1,1,1790985600010,true\n1,1.4,0.2,1,1,1790985600001,false\n",
                     "1,1.4,0.2,1,1,1790985600001,false\n2,1.5,0.1,1,1,1790985600010,true\n"):
            body = zipped("x.csv", text)
            http = FakeHttp({url: body, url + ".CHECKSUM": f"{hashlib.sha256(body).hexdigest()}  x.zip".encode()})
            trades = BinanceAdapter().archive_trades(http, BTC, DAY0)
            self.assertEqual(trades.id.tolist(), [1, 2])
            self.assertEqual(trades.sell.tolist(), [False, True])
        http = FakeHttp({url: body, url + ".CHECKSUM": b"0" * 64})
        with self.assertRaisesRegex(FetchError, "校验"):
            BinanceAdapter().archive_trades(http, BTC, DAY0)

    def test_binance_rest_pages_forward_by_id_until_the_hole_ends(self):
        url = "https://fapi.binance.com/fapi/v1/aggTrades"

        def page(params):
            first = params.get("fromId", 100)
            return json.dumps([{"a": first + i, "p": "1", "q": "1", "T": DAY0 + (first + i) * 1000, "m": False}
                               for i in range(1000)]).encode()

        http = FakeHttp({url: page})
        trades = BinanceAdapter().rest_trades(http, BTC, DAY0, DAY0 + 3_600_000, 99, 2500)
        self.assertEqual([params.get("fromId") for _, params in http.requests], [100, 1100, 2100])
        self.assertEqual((trades.id.min(), trades.id.is_monotonic_increasing), (100, True))
        http = FakeHttp({url: page})
        BinanceAdapter().rest_trades(http, BTC, DAY0, DAY0 + 3_600_000, None, None)
        self.assertEqual(http.requests[0][1]["endTime"] - http.requests[0][1]["startTime"], 3_599_999)

    def test_okx_rest_pages_backward_and_archive_checks_completeness(self):
        adapter = OkxAdapter()
        okx_btc = adapter.builtin_instruments()[0]
        url = "https://www.okx.com/api/v5/market/history-trades"

        def page(params):
            top = int(params["after"]) if params["type"] == 1 else 5000
            rows = [{"tradeId": str(top - 1 - i), "px": "1", "sz": "1", "side": "buy", "ts": str(DAY0 + top - 1 - i)}
                    for i in range(100)]
            return json.dumps({"code": "0", "data": rows}).encode()

        http = FakeHttp({url: page})
        with patch("okx_feed.REQUEST_GAP_SEC", 0):
            trades = adapter.rest_trades(http, okx_btc, DAY0 + 4700, DAY0 + 5000, None, None)
        self.assertEqual(len(http.requests), 4)
        self.assertEqual((trades.id.max(), trades.qty.iloc[0]), (4999, 0.01))
        day = DAY0 - 8 * 3_600_000                                   # 北京时间 2026-10-03 00:00
        archive = (f"https://static.okx.com/cdn/okex/traderecords/trades/daily/20261003/"
                   f"BTC-USDT-SWAP-trades-2026-10-03.zip")
        header = "instrument_name,trade_id,side,price,size,created_time,source\n"
        good = header + f"BTC-USDT-SWAP,1,buy,1,2,{day + 5},0\nBTC-USDT-SWAP,2,sell,1,3,{day + 6},0\n"
        np.testing.assert_allclose(
            adapter.archive_trades(FakeHttp({archive: zipped("a.csv", good)}), okx_btc, day).qty, [0.02, 0.03])
        bad = header + f"BTC-USDT-SWAP,1,buy,1,2,{day + 5},0\nBTC-USDT-SWAP,3,sell,1,3,{day + 6},0\n"
        with self.assertRaisesRegex(FetchError, "不完整"):
            adapter.archive_trades(FakeHttp({archive: zipped("a.csv", bad)}), okx_btc, day)


class NoTqSdk:
    """加密合约的请求不该碰 TqSdk: 任何调用都直接让测试失败。"""

    def __getattr__(self, name):
        raise AssertionError(f"加密合约不应调用 TqSdk 采集的 {name}")


class AppRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = patch.object(ingest, "DATA_DIR", self.temp.name)
        self.paths.start()
        self.crypto = CryptoManager([BinanceAdapter(), OkxAdapter()])
        feed = self.crypto.ensure(BTC.symbol, 30)
        self.crypto._sync(time.monotonic())
        feed.trades.add(1, 100.0, 1.0, BASE + 1000, False)
        feed.recompute(lambda _: None)
        self.patches = [patch.object(server, "crypto", self.crypto), patch.object(server, "manager", NoTqSdk())]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.paths.stop()
        self.temp.cleanup()

    def test_history_and_footprint_come_from_the_crypto_manager(self):
        snapshot = asyncio.run(server.history(BTC.symbol, 0, 30))
        self.assertEqual((snapshot["symbol"], snapshot["bars"][-1]["buy"]), (BTC.symbol, 1.0))
        self.assertEqual(asyncio.run(server.footprint(BTC.symbol, 30))["bars"], [])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.history("BINANCE.NOPEUSDT.P", 0, 30))
        self.assertEqual(caught.exception.status_code, 400)

    def test_ws_sends_snapshot_and_updates(self):
        feed = self.crypto.feeds[(BTC.symbol, 30)]

        class Socket:
            def __init__(self):
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def send_json(self, message):
                self.messages.put_nowait(message)

            async def close(self, **kwargs):
                self.messages.put_nowait({"closed": kwargs})

            async def receive(self):
                return await asyncio.wait_for(self.messages.get(), timeout=2)

        async def exercise():
            self.crypto.loop = asyncio.get_running_loop()
            socket = Socket()
            task = asyncio.create_task(server.ws(socket, BTC.symbol, 0, False, 30))
            try:
                first = await socket.receive()
                self.assertEqual((first["type"], first["cfg"]["priceDigits"]), ("snapshot", 1))
                feed.trades.add(2, 101.0, 2.0, BASE + 2000, True)
                feed.recompute(self.crypto.broadcast)
                update = await socket.receive()
                self.assertEqual((update["type"], update["bars"][-1]["sell"]), ("bars", 2.0))
            finally:
                task.cancel()
                await task
            self.assertEqual(self.crypto.clients, {})

        asyncio.run(exercise())

    def test_label_collection_and_status(self):
        self.assertEqual(asyncio.run(server.symbol_label(BTC.symbol))["label"], "BTCUSDT 永续 · 币安")
        self.assertEqual(asyncio.run(server.symbol_label("OKX.BTC-USDT-SWAP"))["label"], "BTC-USDT 永续 · OKX")
        with patch.object(server.favorites, "symbols", lambda: [BTC.symbol, "SHFE.rb2601"]):
            symbols = {symbol for symbol, _ in server.collection_keys()}
        self.assertEqual(symbols, {server.DEFAULT_SYMBOL, "SHFE.rb2601"})
        with patch.object(server, "manager", SimpleNamespace(status_snapshot=lambda: {"status": "connected"})):
            self.assertIn(f"{BTC.symbol}@30s", server.status()["crypto"]["feeds"])


if __name__ == "__main__":
    unittest.main()
