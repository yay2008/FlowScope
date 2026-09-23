"""自选的离线回归: 文件持久化、报价快照、TTL 缓存与接口校验。"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app as server
import favorites as fav
import ingest


class FakeApi:
    """只实现自选报价用到的两个 SDK 方法。"""

    def __init__(self, quotes=None, missing=()):
        self.quotes = dict(quotes or {})
        self.missing = set(missing)
        self.waits = 0
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        if symbol in self.missing or symbol not in self.quotes:
            raise RuntimeError("合约不存在")
        return self.quotes[symbol]

    def wait_update(self, deadline=None):
        self.waits += 1
        return True


class FakeManager:
    """替身采集线程: query 当场用 FakeApi 执行。"""

    def __init__(self, api=None, error=None):
        self.api = api if api is not None else FakeApi()
        self.error = error
        self.calls = 0

    async def query(self, fn, timeout=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return fn(self.api)


def quote(**kwargs):
    fields = {"instrument_name": "", "ins_class": "FUTURE", "underlying_symbol": "",
              "last_price": float("nan"), "pre_settlement": float("nan"),
              "pre_close": float("nan"), "open_interest": float("nan"),
              "volume": float("nan"), "amount": float("nan"),
              "price_decs": 0, "expired": False}
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "favorites.json")
        self.store = fav.FavoriteStore(lambda: self.path)

    def test_add_remove_keeps_order_and_is_idempotent(self):
        self.assertEqual(self.store.symbols(), [])
        self.store.add("KQ.m@SHFE.fu")
        self.store.add("SHFE.fu2611")
        self.store.add("KQ.m@SHFE.fu")   # 重复收藏不改变顺序
        self.assertEqual(self.store.symbols(), ["KQ.m@SHFE.fu", "SHFE.fu2611"])
        self.assertEqual(self.store.remove("KQ.m@SHFE.fu"), ["SHFE.fu2611"])
        self.assertEqual(self.store.remove("KQ.m@SHFE.fu"), ["SHFE.fu2611"])

    def test_symbols_survive_a_restart(self):
        self.store.add("KQ.m@DCE.i")
        reopened = fav.FavoriteStore(lambda: self.path)
        self.assertEqual(reopened.symbols(), ["KQ.m@DCE.i"])
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["symbols"], ["KQ.m@DCE.i"])

    def test_broken_or_missing_file_is_treated_as_empty(self):
        self.assertEqual(self.store.symbols(), [])          # 文件还不存在
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(self.store.symbols(), [])
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"symbols": ["KQ.m@SHFE.fu", "bad code", "", "KQ.m@SHFE.fu"]}, handle)
        self.assertEqual(self.store.symbols(), ["KQ.m@SHFE.fu"])   # 非法项与重复项被丢掉

    def test_external_edit_is_picked_up(self):
        """手工改 favorites.json 后不必重启服务。"""
        self.store.add("KQ.m@SHFE.fu")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"symbols": ["KQ.m@DCE.i", "KQ.m@SHFE.fu"]}, handle)
        self.assertEqual(self.store.symbols(), ["KQ.m@DCE.i", "KQ.m@SHFE.fu"])

    def test_invalid_symbol_is_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            self.store.add("SHFE fu!!")
        self.assertFalse(os.path.exists(self.path))

    def test_list_is_capped(self):
        for index in range(fav.MAX_FAVORITES):
            self.store.add(f"SHFE.t{index:02d}")
        with self.assertRaises(ValueError):
            self.store.add("SHFE.overflow")
        self.assertEqual(len(self.store.symbols()), fav.MAX_FAVORITES)

    def test_data_directory_change_reloads_the_new_file(self):
        self.store.add("KQ.m@SHFE.fu")
        other = os.path.join(self.directory.name, "other.json")
        self.assertEqual(fav.FavoriteStore(lambda: other).symbols(), [])


class QuoteRowTests(unittest.TestCase):
    def test_change_is_measured_against_previous_settlement(self):
        row = fav.quote_row("SHFE.fu2611", quote(instrument_name="燃油2611", last_price=4412,
                                                 pre_settlement=4493, pre_close=4488, price_decs=0))
        self.assertAlmostEqual(row["changePct"], (4412 - 4493) / 4493 * 100)
        self.assertEqual(row["name"], "燃油2611")

    def test_previous_close_is_the_fallback_base(self):
        row = fav.quote_row("SHFE.fu2611", quote(last_price=110, pre_close=100))
        self.assertAlmostEqual(row["changePct"], 10.0)

    def test_missing_numbers_become_null_and_stay_json_safe(self):
        row = fav.quote_row("SHFE.fu2611", quote())
        self.assertIsNone(row["lastPrice"])
        self.assertIsNone(row["changePct"])
        self.assertIsNone(row["openInterest"])
        self.assertIsNone(row["volume"])
        self.assertIsNone(row["amount"])
        self.assertEqual(row["name"], "SHFE.fu2611")
        json.dumps(row)   # NaN 会让前端 JSON.parse 直接报错, 这里必须能序列化

    def test_turnover_fields_come_through_for_liquidity_comparison(self):
        """成交量/成交额用来判断流动性, 必须原样透出; 缺数据时留 null。"""
        row = fav.quote_row("SHFE.fu2611", quote(instrument_name="燃油2611", last_price=4158,
                                                 volume=556133, amount=23124010140.0))
        self.assertEqual(row["volume"], 556133)
        self.assertEqual(row["amount"], 23124010140.0)
        json.dumps(row)

    def test_empty_row_keeps_the_same_keys_as_a_live_row(self):
        """取不到报价的空行不能少字段, 否则前端要靠 undefined 兜底。"""
        empty = fav._empty_row("SHFE.gone")
        live = fav.quote_row("SHFE.fu2611", quote())
        self.assertEqual(set(empty) - set(live), {"preSettlement"})
        self.assertEqual(set(live) - set(empty), {"basePrice"})

    def test_main_continuous_keeps_its_underlying(self):
        row = fav.quote_row("KQ.m@SHFE.fu", quote(instrument_name="燃油主连", ins_class="CONT",
                                                  last_price=4412, pre_settlement=4493,
                                                  underlying_symbol="SHFE.fu2611"))
        self.assertEqual(row["insClass"], "CONT")
        self.assertEqual(row["mainSymbol"], "SHFE.fu2611")


class ReadQuotesTests(unittest.TestCase):
    def test_rows_follow_the_requested_order_and_survive_bad_symbols(self):
        api = FakeApi({"SHFE.fu2611": quote(instrument_name="燃油2611", last_price=4412)})
        rows = fav.read_quotes(api, ["SHFE.fu2611", "SHFE.gone"], set(), wait_sec=0)
        self.assertEqual([row["symbol"] for row in rows], ["SHFE.fu2611", "SHFE.gone"])
        self.assertIsNone(rows[1]["lastPrice"])

    def test_only_new_symbols_wait_for_their_first_quote(self):
        api = FakeApi({"SHFE.fu2611": quote(last_price=4412)})
        warm = set()
        fav.read_quotes(api, ["SHFE.fu2611"], warm, wait_sec=0)
        self.assertEqual(api.waits, 1)
        fav.read_quotes(api, ["SHFE.fu2611"], warm, wait_sec=0)
        self.assertEqual(api.waits, 1)   # 已经热了: 轮询不再打断采集循环


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "favorites.json")
        self.store = fav.FavoriteStore(lambda: self.path)

    def service(self, manager):
        return fav.FavoritesService(lambda: manager, self.store, quotes_ttl=60,
                                    timeout=1.0, wait_sec=0)

    def test_watch_returns_live_rows_and_reuses_the_cached_snapshot(self):
        api = FakeApi({"SHFE.fu2611": quote(instrument_name="燃油2611", last_price=4412,
                                            pre_settlement=4493)})
        manager = FakeManager(api)
        service = self.service(manager)
        data = asyncio.run(service.watch(["SHFE.fu2611"]))
        self.assertEqual(data["source"], "live")
        self.assertEqual(data["quotes"][0]["name"], "燃油2611")
        asyncio.run(service.watch(["SHFE.fu2611"]))
        self.assertEqual(manager.calls, 1)   # TTL 内多个页面轮询只查一次

    def test_empty_watchlist_never_touches_the_ingest_thread(self):
        manager = FakeManager()
        data = asyncio.run(self.service(manager).watch([]))
        self.assertEqual(data, {"quotes": [], "source": "live", "error": None})
        self.assertEqual(manager.calls, 0)

    def test_unavailable_ingest_thread_still_lists_the_codes(self):
        service = self.service(FakeManager(error=RuntimeError("行情线程未就绪(error)")))
        data = asyncio.run(service.watch(["SHFE.fu2611"]))
        self.assertEqual(data["source"], "unavailable")
        self.assertEqual(data["quotes"], [])
        self.assertIn("行情线程未就绪", data["error"])

    def test_store_and_quotes_are_independent(self):
        service = self.service(FakeManager(error=RuntimeError("断线")))
        self.assertEqual(service.add("KQ.m@SHFE.fu"), ["KQ.m@SHFE.fu"])
        self.assertEqual(service.symbols(), ["KQ.m@SHFE.fu"])
        self.assertEqual(service.remove("KQ.m@SHFE.fu"), [])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        paths = patch.object(ingest, "DATA_DIR", self.directory.name)
        paths.start()
        self.addCleanup(paths.stop)
        self.service = fav.FavoritesService(
            lambda: FakeManager({"SHFE.fu2611": quote(instrument_name="燃油2611", last_price=4412)}),
            fav.FavoriteStore(lambda: os.path.join(self.directory.name, "favorites.json")),
            timeout=1.0, wait_sec=0)
        patched = patch.object(server, "favorites", self.service)
        patched.start()
        self.addCleanup(patched.stop)

    def test_add_list_remove_round_trip(self):
        self.assertEqual(server.favorites_list(), {"symbols": [], "max": fav.MAX_FAVORITES})
        self.assertEqual(server.favorites_add("KQ.m@SHFE.fu")["symbols"], ["KQ.m@SHFE.fu"])
        self.assertEqual(server.favorites_add("SHFE.fu2611")["symbols"],
                         ["KQ.m@SHFE.fu", "SHFE.fu2611"])
        self.assertEqual(server.favorites_remove("KQ.m@SHFE.fu")["symbols"], ["SHFE.fu2611"])

    def test_bad_symbol_and_full_list_are_client_errors(self):
        with self.assertRaises(HTTPException) as caught:
            server.favorites_add("SHFE fu!!")
        self.assertEqual(caught.exception.status_code, 400)
        for index in range(fav.MAX_FAVORITES):
            self.service.add(f"SHFE.t{index:02d}")
        with self.assertRaises(HTTPException):
            server.favorites_add("SHFE.overflow")

    def test_watch_validates_and_deduplicates_symbols(self):
        data = asyncio.run(server.watch("KQ.m@SHFE.fu, KQ.m@SHFE.fu ,SHFE.fu2611"))
        self.assertEqual([row["symbol"] for row in data["quotes"]],
                         ["KQ.m@SHFE.fu", "SHFE.fu2611"])
        with self.assertRaises(HTTPException):
            asyncio.run(server.watch("bad code"))
        too_many = ",".join(f"SHFE.x{index}" for index in range(fav.MAX_FAVORITES + 1))
        with self.assertRaises(HTTPException):
            asyncio.run(server.watch(too_many))

    def test_default_symbol_is_untouched_by_favorites(self):
        self.assertEqual(server.DEFAULT_SYMBOL, "KQ.m@SHFE.fu")
        self.assertEqual(server.favorites_list()["symbols"], [])


if __name__ == "__main__":
    unittest.main()
