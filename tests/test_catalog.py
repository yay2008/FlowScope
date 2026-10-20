"""合约目录的离线回归: 品种/月份解析、排序、缓存与离线回退。"""
import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

import catalog

CONT_NAMES = {"KQ.m@SHFE.fu": "燃油主连", "KQ.m@DCE.i": "铁矿石主连", "KQ.m@XX.zz": "ZZ主连"}
CONT_MAINS = {"KQ.m@SHFE.fu": "SHFE.fu2611", "KQ.m@DCE.i": "DCE.i2601", "KQ.m@XX.zz": "XX.zz2601"}
MONTH_NAMES = {"SHFE.fu2610": "燃油2610", "SHFE.fu2611": "燃油2611", "SHFE.fu2701": "燃油2701"}


class FakeQuote:
    def __init__(self, underlying=""):
        self.underlying_symbol = underlying


class FakeApi:
    """只实现目录查询用到的两个 SDK 方法; 不做任何网络访问, 也不订阅行情。"""

    def __init__(self, conts=(), months=(), pre_open_interest=None, quote_error=None):
        self.conts = list(conts)
        self.months = list(months)
        self.pre_open_interest = dict(pre_open_interest or {})
        self.quote_error = quote_error
        self.quotes = []
        self.info_calls = []

    def query_quotes(self, ins_class=None, exchange_id=None, product_id=None, expired=None):
        if ins_class is None:
            # "合约是否存在"的过滤查询(ingest.listed_symbols): 认识的代码都算存在
            return [*CONT_NAMES, *CONT_MAINS.values(), *MONTH_NAMES]
        return list(self.conts) if ins_class == "CONT" else list(self.months)

    def get_quote(self, symbol):
        self.quotes.append(symbol)
        if self.quote_error is not None:
            raise self.quote_error
        return FakeQuote(underlying=CONT_MAINS.get(symbol, ""))

    def query_symbol_info(self, symbols):
        self.info_calls.append(list(symbols))
        rows = []
        for symbol in symbols:
            rows.append({
                "instrument_id": symbol,
                "instrument_name": CONT_NAMES.get(symbol) or MONTH_NAMES.get(symbol, symbol),
                "underlying_symbol": CONT_MAINS.get(symbol, ""),
                "pre_open_interest": self.pre_open_interest.get(symbol, 0),
            })
        return pd.DataFrame(rows)


class FakeManager:
    """替身采集线程: query 用 FakeApi 当场执行(真实实现见 FeedManager.query)。"""

    def __init__(self, api=None, error=None, hang=False, status="connected"):
        self.api, self.error, self.hang, self.status = api, error, hang, status
        self.jobs = []

    async def query(self, fn, timeout=None):
        if self.status in {"stopped", "error"}:
            raise RuntimeError(f"行情线程未就绪({self.status})")
        self.jobs.append(fn)
        await asyncio.sleep(0)
        if self.hang:
            raise RuntimeError(f"合约查询超时({timeout:.0f} 秒)")
        if self.error is not None:
            raise self.error
        return fn(self.api)


class ParseTests(unittest.TestCase):
    def test_cont_and_month_symbols_map_to_the_same_variety(self):
        self.assertEqual(catalog.split_cont_symbol("KQ.m@SHFE.fu"), ("SHFE", "fu"))
        self.assertIsNone(catalog.split_cont_symbol("SHFE.fu2611"))
        self.assertEqual(catalog.split_product("KQ.m@SHFE.fu"), ("SHFE", "fu"))
        self.assertEqual(catalog.split_product("SHFE.fu2611"), ("SHFE", "fu"))
        self.assertEqual(catalog.split_product("CZCE.TA701"), ("CZCE", "TA"))
        self.assertIsNone(catalog.split_product("KQ.i@SHFE.fu"))

    def test_product_name_drops_the_cont_suffix(self):
        self.assertEqual(catalog.product_name("燃油主连", "fu"), "燃油")
        self.assertEqual(catalog.product_name("", "fu"), "FU")
        self.assertEqual(catalog.product_name(float("nan"), "i"), "I")

    def test_parameters_are_validated_before_reaching_the_sdk(self):
        self.assertEqual(catalog.normalize_exchange("shfe"), "SHFE")
        self.assertIsNone(catalog.normalize_exchange(""))
        self.assertEqual(catalog.normalize_product("TA"), "TA")
        for bad in ("SHFE.OR 1=1", "上海", "../etc"):
            with self.assertRaises(ValueError):
                catalog.normalize_exchange(bad)


class OpenInterestBasisTests(unittest.TestCase):
    """静态查询的 pre_open_interest 计边口径: 上期所/能源中心/郑商所/大商所是双边。"""

    def test_bilateral_exchanges_are_halved(self):
        # 实测: 这四家的静态值恰好是报价值的 2 倍(报价值一律单边)。
        for symbol in ("SHFE.fu2611", "INE.sc2611", "CZCE.SR701", "DCE.i2601"):
            with self.subTest(symbol=symbol):
                self.assertEqual(catalog.single_side_open_interest(symbol, 381862), 190931)

    def test_unilateral_exchanges_are_untouched(self):
        # 中金所/广期所的静态值本来就是单边, 除以 2 会把数字改错。
        for symbol in ("CFFEX.IF2612", "GFEX.si2611"):
            with self.subTest(symbol=symbol):
                self.assertEqual(catalog.single_side_open_interest(symbol, 161098), 161098)

    def test_unknown_exchange_is_not_scaled(self):
        """新增交易所时宁可少归一(排序仍对), 也不要凭猜测把数字改错。"""
        self.assertEqual(catalog.single_side_open_interest("XX.zz2601", 5), 5)
        self.assertEqual(catalog.single_side_open_interest("", 5), 5)
        self.assertEqual(catalog.single_side_open_interest("SHFE.fu2611", None), 0.0)

    def test_normalisation_does_not_change_the_ranking(self):
        """缩放系数对同一品种恒定, 所以按持仓量排序的结果不变。"""
        api = FakeApi(conts=["KQ.m@SHFE.fu", "KQ.m@DCE.i", "KQ.m@CFFEX.IF"],
                      pre_open_interest={"SHFE.fu2611": 210000, "DCE.i2601": 320000,
                                         "CFFEX.IF2612": 90000})
        products = catalog.build_products(api)
        catalog.rank_products(products)
        self.assertEqual([p["productId"] for p in products], ["i", "fu", "IF"])

    def test_catalog_and_watch_report_the_same_basis(self):
        """菜单里的主力行与自选面板的实时持仓量必须同口径(都是单边), 否则数字看起来打架。

        静态值是双边的(上期所), 归一后应恰好是报价值的量级; 未归一时这里会是 2 倍。
        """
        static_value = 381862
        live_single_side = 190931
        api = FakeApi(conts=["KQ.m@SHFE.fu"], pre_open_interest={"SHFE.fu2611": static_value})
        products = catalog.build_products(api)
        self.assertEqual(products[0]["openInterest"], live_single_side)


class BuildTests(unittest.TestCase):
    def test_products_are_sorted_by_open_interest_and_grouped_by_exchange(self):
        # 排序键取自主力合约(CONT 自身的 pre_open_interest 恒为 0), 并归一到单边:
        # SHFE/DCE 减半, 不认识的 XX 原样保留。
        api = FakeApi(conts=["KQ.m@DCE.i", "KQ.m@SHFE.fu", "KQ.m@XX.zz"],
                      pre_open_interest={"SHFE.fu2611": 210000, "DCE.i2601": 320000, "XX.zz2601": 5})
        products = catalog.build_products(api)
        self.assertEqual({p["productId"]: p["name"] for p in products},
                         {"i": "铁矿石", "fu": "燃油", "zz": "ZZ"})
        self.assertEqual({p["productId"]: p["mainSymbol"] for p in products},
                         {"i": "DCE.i2601", "fu": "SHFE.fu2611", "zz": "XX.zz2601"})
        catalog.rank_products(products)
        self.assertEqual([p["productId"] for p in products], ["i", "fu", "zz"])
        self.assertEqual([int(p["openInterest"]) for p in products], [160000, 105000, 5])
        groups = catalog.group_products(products)
        self.assertEqual([g["exchangeId"] for g in groups], ["SHFE", "DCE", "XX"])
        self.assertEqual([p["productId"] for p in groups[0]["products"]], ["fu"])
        self.assertEqual(groups[0]["exchangeName"], "上期所")

    def test_catalog_query_does_not_subscribe_any_quotes(self):
        api = FakeApi(conts=["KQ.m@SHFE.fu"], pre_open_interest={"SHFE.fu2611": 210000})
        catalog.ranked_products(api)
        catalog.build_months(api, "SHFE", "fu", main_symbol="SHFE.fu2611")
        self.assertEqual(api.quotes, [])   # 目录只用静态查询, 不订阅行情

    def test_months_are_sorted_by_open_interest_and_flag_the_main_contract(self):
        api = FakeApi(months=["SHFE.fu2610", "SHFE.fu2611", "SHFE.fu2701"],
                      pre_open_interest={"SHFE.fu2611": 428100, "SHFE.fu2701": 266684,
                                         "SHFE.fu2610": 53900})
        months = catalog.build_months(api, "SHFE", "fu", main_symbol="SHFE.fu2611")
        self.assertEqual([m["symbol"] for m in months],
                         ["SHFE.fu2611", "SHFE.fu2701", "SHFE.fu2610"])
        self.assertEqual([m["isMain"] for m in months], [True, False, False])
        self.assertEqual(months[0]["name"], "燃油2611")
        # 上期所是双边计量, 这里要显示单边值(与自选面板同口径)
        self.assertEqual(int(months[0]["openInterest"]), 214050)

    def test_unknown_exchange_still_shows_up_in_the_groups(self):
        groups = catalog.group_products([{"exchangeId": "AAA", "productId": "x", "name": "X",
                                          "contSymbol": "KQ.m@AAA.x", "mainSymbol": "",
                                          "openInterest": 0.0}])
        self.assertEqual([g["exchangeId"] for g in groups], ["AAA"])


def daily_rows(*days):
    return pd.DataFrame([{"datetime": pd.Timestamp(day, tz="Asia/Shanghai").value, "volume": volume}
                         for day, volume in days])


class DailyApi(FakeApi):
    def __init__(self, *args, daily=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.daily = daily or {}
        self.daily_calls = []

    def get_trading_calendar(self, start_dt, end_dt):
        dates = pd.date_range(start_dt, end_dt)
        return pd.DataFrame({"date": dates, "trading": [
            day.weekday() < 5 and not ("2026-10-01" <= day.strftime("%Y-%m-%d") <= "2026-10-07")
            for day in dates]})

    def create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def get_kline_serial(self, symbol, duration_seconds, data_length):
        self.daily_calls.append((symbol, duration_seconds, data_length))
        value = self.daily.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


class DailyVolumeTests(unittest.TestCase):
    def test_only_requested_trading_day_counts_and_missing_is_not_zero(self):
        rows = daily_rows(("2026-10-16", 400), ("2026-10-19", 1200), ("2026-10-20", 9999))
        self.assertEqual(catalog.previous_daily_volume(rows, "2026-10-19"), 1200)
        self.assertIsNone(catalog.previous_daily_volume(rows, "2026-10-18"))
        for value in (None, float("nan"), float("inf"), -1):
            self.assertIsNone(catalog.previous_daily_volume(daily_rows(("2026-10-19", value)), "2026-10-19"))
        self.assertEqual(catalog.previous_daily_volume(daily_rows(("2026-10-19", 0)), "2026-10-19"), 0)
        self.assertIsNone(catalog.previous_daily_volume([{"datetime": 0, "volume": 0}], "2026-10-19"))

    def test_calendar_handles_weekends_holidays_and_night_session(self):
        reader, api = catalog.DailyVolumes(), DailyApi()
        for now, expected in (
            ("2026-10-19T10:00:00+08:00", "2026-10-16"),
            ("2026-10-20T17:59:00+08:00", "2026-10-19"),
            ("2026-10-20T21:00:00+08:00", "2026-10-20"),
            ("2026-10-17T10:00:00+08:00", "2026-10-16"),
            ("2026-10-08T09:00:00+08:00", "2026-09-30"),
        ):
            with self.subTest(now=now):
                self.assertEqual(reader.snapshot(api, [], datetime.fromisoformat(now))[1], expected)

    def test_subscriptions_are_deferred_deduplicated_and_reset_on_reconnect(self):
        async def run():
            now = datetime.fromisoformat("2026-10-20T10:00:00+08:00")
            reader = catalog.DailyVolumes()
            api = DailyApi(daily={"SHFE.fu2611": daily_rows(("2026-10-19", 12345))})
            first, date = reader.snapshot(api, ["SHFE.fu2611", "SHFE.fu2611", ""], now)
            self.assertEqual(first, {"SHFE.fu2611": None})
            self.assertEqual(date, "2026-10-19")
            self.assertEqual(api.daily_calls, [])  # HTTP 查询不等待 SDK 下载日线。
            await asyncio.sleep(0)
            self.assertEqual(reader.snapshot(api, ["SHFE.fu2611"], now)[0], {"SHFE.fu2611": 12345})
            self.assertEqual(api.daily_calls, [("SHFE.fu2611", 86400, 3)])
            fresh = DailyApi(daily={"SHFE.fu2611": daily_rows(("2026-10-19", 67890))})
            self.assertIsNone(reader.snapshot(fresh, ["SHFE.fu2611"], now)[0]["SHFE.fu2611"])
            await asyncio.sleep(0)
            self.assertEqual(reader.snapshot(fresh, ["SHFE.fu2611"], now)[0]["SHFE.fu2611"], 67890)
        asyncio.run(run())

    def test_one_failed_contract_does_not_hide_other_volumes_and_can_retry(self):
        async def run():
            now = datetime.fromisoformat("2026-10-20T10:00:00+08:00")
            reader = catalog.DailyVolumes()
            api = DailyApi(daily={"SHFE.fu2611": RuntimeError("unavailable"),
                                 "SHFE.fu2701": daily_rows(("2026-10-19", 12))})
            with patch("catalog.time.monotonic", return_value=100):
                reader.snapshot(api, api.daily, now)
                await asyncio.sleep(0)
                self.assertEqual(reader.snapshot(api, api.daily, now)[0], {"SHFE.fu2611": None, "SHFE.fu2701": 12})
                self.assertEqual(len(api.daily_calls), 2)
            api.daily["SHFE.fu2611"] = daily_rows(("2026-10-19", 34))
            with patch("catalog.time.monotonic", return_value=161):
                reader.snapshot(api, api.daily, now)
                await asyncio.sleep(0)
                self.assertEqual(reader.snapshot(api, api.daily, now)[0]["SHFE.fu2611"], 34)
        asyncio.run(run())

    def test_service_adds_main_and_month_volumes_without_changing_interest_or_rank(self):
        async def run():
            api = DailyApi(conts=["KQ.m@SHFE.fu"], months=["SHFE.fu2611", "SHFE.fu2701"],
                           pre_open_interest={"SHFE.fu2611": 400, "SHFE.fu2701": 200},
                           daily={"SHFE.fu2611": daily_rows(("2026-10-19", 123)),
                                  "SHFE.fu2701": daily_rows(("2026-10-19", 999))})
            service = catalog.CatalogService(lambda: FakeManager(api))
            await service.payload(exchange="SHFE", product="fu")
            await asyncio.sleep(0)
            data = await service.payload(exchange="SHFE", product="fu")
            product = data["groups"][0]["products"][0]
            self.assertEqual((product["openInterest"], product["previousVolume"]), (200, 123))
            self.assertEqual([(m["symbol"], m["openInterest"], m["previousVolume"]) for m in data["months"]],
                             [("SHFE.fu2611", 200, 123), ("SHFE.fu2701", 100, 999)])
            self.assertEqual(data["previousVolumeDate"], "2026-10-19")
            self.assertNotIn("previousVolume", service._catalog[1]["groups"][0]["products"][0])
        with patch("catalog.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-20T10:00:00+08:00")
            asyncio.run(run())


class ServiceTests(unittest.TestCase):
    def test_live_catalog_is_cached_and_served_with_months(self):
        api = FakeApi(conts=["KQ.m@SHFE.fu"], months=["SHFE.fu2611", "SHFE.fu2701"],
                      pre_open_interest={"SHFE.fu2611": 428100, "SHFE.fu2701": 266684})
        manager = FakeManager(api=api)
        service = catalog.CatalogService(lambda: manager)
        data = asyncio.run(service.payload(exchange="SHFE", product="fu"))
        self.assertEqual(data["source"], "live")
        self.assertIsNone(data["error"])
        self.assertEqual([m["symbol"] for m in data["months"]], ["SHFE.fu2611", "SHFE.fu2701"])
        self.assertTrue(data["months"][0]["isMain"])
        self.assertEqual(data["groups"][0]["products"][0]["contSymbol"], "KQ.m@SHFE.fu")
        info_calls = len(api.info_calls)
        asyncio.run(service.payload(exchange="SHFE", product="fu"))
        self.assertEqual(len(api.info_calls), info_calls)  # 静态目录命中缓存; 日线只读已有引用。

    def test_offline_catalog_falls_back_to_builtin_varieties(self):
        service = catalog.CatalogService(lambda: FakeManager(error=RuntimeError("行情线程未就绪")))
        data = asyncio.run(service.payload(exchange="SHFE", product="fu"))
        self.assertEqual(data["source"], "fallback")
        self.assertIn("行情线程未就绪", data["error"])
        self.assertEqual(data["months"], [])
        self.assertIn("monthsError", data)
        products = [p for group in data["groups"] for p in group["products"]]
        self.assertIn("fu", [p["productId"] for p in products])

    def test_hanging_query_times_out_without_blocking_forever(self):
        service = catalog.CatalogService(lambda: FakeManager(hang=True), timeout=0.05)
        data = asyncio.run(service.payload())
        self.assertEqual(data["source"], "fallback")
        self.assertIn("超时", data["error"])

    def test_cont_symbol_label_resolves_to_the_current_underlying_contract(self):
        """顶栏展示名要跟着主力换月走, 所以不能用主连自带的「燃油主连」。"""
        api = FakeApi()
        service = catalog.CatalogService(lambda: FakeManager(api=api))
        data = asyncio.run(service.symbol_label("KQ.m@SHFE.fu"))
        self.assertEqual(data, {"symbol": "KQ.m@SHFE.fu", "label": "燃油2611"})
        self.assertEqual(api.quotes, ["KQ.m@SHFE.fu"])   # 只读一次报价, 不订阅 K线/tick

    def test_month_symbol_label_uses_its_own_name(self):
        service = catalog.CatalogService(lambda: FakeManager(api=FakeApi()))
        data = asyncio.run(service.symbol_label("SHFE.fu2610"))
        self.assertEqual(data["label"], "燃油2610")

    def test_symbol_label_falls_back_to_the_code_when_queries_fail(self):
        """行情源不可用时展示位不能空着, 也不能让顶栏报错。"""
        service = catalog.CatalogService(lambda: FakeManager(error=RuntimeError("行情线程未就绪")))
        data = asyncio.run(service.symbol_label("KQ.m@SHFE.fu"))
        self.assertEqual(data, {"symbol": "KQ.m@SHFE.fu", "label": "KQ.m@SHFE.fu"})

    def test_unknown_symbol_label_never_reaches_the_quote_or_info_query(self):
        """URL 里写错的代码: 对不存在的代码调 get_quote/query_symbol_info 会让整条连接停摆,
        所以先确认存在, 查不到就直接回退成代码本身。"""
        api = FakeApi()
        api.query_symbol_info = None   # 被调用就会 TypeError
        for symbol in ("SHFE.fu9999", "KQ.m@SHFE.zz"):
            self.assertEqual(catalog.resolve_label(api, symbol), symbol)
        self.assertEqual(api.quotes, [])

    def test_symbol_label_rejects_malformed_code(self):
        service = catalog.CatalogService(lambda: FakeManager(api=FakeApi()))
        with self.assertRaises(ValueError):
            asyncio.run(service.symbol_label("../etc/passwd"))

    def test_resolve_label_survives_a_failing_quote_lookup(self):
        """报价取不到标的合约时, 退而使用主连自身的名字, 而不是抛错或空着。"""
        api = FakeApi(quote_error=RuntimeError("闭市无报价"))
        self.assertEqual(catalog.resolve_label(api, "KQ.m@SHFE.fu"), "燃油主连")
        self.assertEqual(catalog.resolve_label(api, "SHFE.fu2611"), "燃油2611")


if __name__ == "__main__":
    unittest.main()
