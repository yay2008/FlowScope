# -*- coding: utf-8 -*-
"""合约目录: 品种(主连)与月份合约列表, 供前端合约选择器使用。

数据全部来自 TqSdk 合约服务, 且只用静态合约查询(不订阅行情):

- 品种: ``query_quotes(ins_class="CONT")`` 取全部主连, ``query_symbol_info`` 取中文名
  (形如「燃油主连」)与当前主力合约, 再按主力合约的昨日持仓量排序, 让活跃品种排在前面。
- 月份: 该品种未下市合约, 同样按昨日持仓量降序, 并标出主力(与主连的标的合约一致)。

持仓量的**计边口径**必须归一, 否则菜单和自选面板的数字对不上: 静态查询的
``pre_open_interest`` 在上期所/能源中心/郑商所/大商所是**双边**计量, 其余交易所是单边;
而报价对象的 ``open_interest`` 一律是单边。两处都拿单边后, 同一个合约在同一时刻的数字
才可比(未归一前双边恰好是单边的 2 倍, 整张菜单显示大一倍)。

SDK 不是线程安全的, 所有调用都必须落在采集线程上, 所以这里只有纯函数
(``build_products`` / ``build_months``); ``CatalogService`` 负责把它们通过
``FeedManager.submit_job`` 排进采集线程, 并在 FastAPI 事件循环里等待结果。

行情源不可用时不报错、不阻塞页面: 目录回退到内置常用品种表, 月份列表留空
(前端至少还能选「主力」, 任意合约仍可写在 URL 的 ``?symbol=`` 里)。
"""
from __future__ import annotations

import re
import threading
import time

from ingest import JOB_TIMEOUT_SEC, listed_symbols, validate_symbol

# 交易所展示顺序按国内期货成交量习惯排列, 与交易所代码一一对应。
EXCHANGES = (
    ("SHFE", "上期所"),
    ("DCE", "大商所"),
    ("CZCE", "郑商所"),
    ("INE", "能源中心"),
    ("GFEX", "广期所"),
    ("CFFEX", "中金所"),
)
EXCHANGE_NAMES = dict(EXCHANGES)
EXCHANGE_ORDER = {code: index for index, (code, _) in enumerate(EXCHANGES)}
CONT_SUFFIX = "主连"
CONT_PREFIX = "KQ.m@"

# 静态合约查询的 pre_open_interest 是双边计量的交易所(TqSdk ins_schema 的定义)。
# 其余交易所(中金所/广期所)与报价对象一样是单边, 不需要归一。实测中金所/广期所
# 静态值 ÷ 报价值 = 1.000, 这四个恰好 = 2.000, 与此列表一致。
BILATERAL_OPEN_INTEREST_EXCHANGES = frozenset({"SHFE", "INE", "CZCE", "DCE"})

CATALOG_TTL_SEC = 300      # 品种目录(含主力合约映射)的缓存时长
MONTHS_TTL_SEC = 60        # 月份列表的缓存时长: 持仓量天天变, 歇一会儿就重取

EXCHANGE_RE = re.compile(r"[A-Za-z]{2,8}\Z")
PRODUCT_RE = re.compile(r"[A-Za-z0-9]{1,8}\Z")
# 品种代码 + 3~4 位交割月: SHFE.fu2611 / CZCE.TA701
MONTH_SYMBOL_RE = re.compile(r"([A-Za-z]+)\.([A-Za-z]+)(?:\d{3,4})?\Z")

# 行情源不可用时的兜底列表: 交易所, 品种代码, 中文名。只覆盖常见活跃品种, 不追求全。
FALLBACK_PRODUCTS = (
    ("SHFE", "rb", "螺纹钢"), ("SHFE", "ru", "天然橡胶"), ("SHFE", "cu", "沪铜"),
    ("SHFE", "au", "沪金"), ("SHFE", "ag", "沪银"), ("SHFE", "fu", "燃油"),
    ("SHFE", "bu", "沥青"), ("SHFE", "sp", "纸浆"), ("SHFE", "ni", "沪镍"),
    ("SHFE", "al", "沪铝"), ("SHFE", "zn", "沪锌"), ("SHFE", "hc", "热卷"),
    ("INE", "sc", "原油"), ("INE", "lu", "低硫燃料油"), ("INE", "nr", "20号胶"),
    ("DCE", "i", "铁矿石"), ("DCE", "m", "豆粕"), ("DCE", "y", "豆油"),
    ("DCE", "p", "棕榈油"), ("DCE", "c", "玉米"), ("DCE", "j", "焦炭"),
    ("DCE", "jm", "焦煤"), ("DCE", "pp", "聚丙烯"), ("DCE", "l", "塑料"),
    ("DCE", "v", "PVC"), ("DCE", "eg", "乙二醇"), ("DCE", "eb", "苯乙烯"),
    ("DCE", "pg", "液化石油气"), ("DCE", "cs", "玉米淀粉"), ("DCE", "lh", "生猪"),
    ("DCE", "jd", "鸡蛋"),
    ("CZCE", "TA", "PTA"), ("CZCE", "MA", "甲醇"), ("CZCE", "SA", "纯碱"),
    ("CZCE", "FG", "玻璃"), ("CZCE", "SR", "白糖"), ("CZCE", "CF", "棉花"),
    ("CZCE", "OI", "菜油"), ("CZCE", "RM", "菜粕"), ("CZCE", "PF", "短纤"),
    ("CZCE", "UR", "尿素"), ("CZCE", "AP", "苹果"), ("CZCE", "SM", "锰硅"),
    ("GFEX", "si", "工业硅"), ("GFEX", "lc", "碳酸锂"),
    ("CFFEX", "IF", "沪深300"), ("CFFEX", "IC", "中证500"), ("CFFEX", "IH", "上证50"),
    ("CFFEX", "IM", "中证1000"), ("CFFEX", "T", "十年国债"),
)


def cont_symbol(exchange_id: str, product_id: str) -> str:
    return f"{CONT_PREFIX}{exchange_id}.{product_id}"


def split_cont_symbol(symbol: str) -> tuple[str, str] | None:
    """``KQ.m@SHFE.fu`` -> ``("SHFE", "fu")``; 不是主连代码时返回 None。"""
    if not symbol.startswith(CONT_PREFIX):
        return None
    rest = symbol[len(CONT_PREFIX):]
    exchange_id, _, product_id = rest.partition(".")
    if not exchange_id or not product_id:
        return None
    return exchange_id.upper(), product_id


def split_product(symbol: str) -> tuple[str, str] | None:
    """``SHFE.fu2611`` -> ``("SHFE", "fu")``, ``KQ.m@SHFE.fu`` -> ``("SHFE", "fu")``。

    只用于把 URL 里的合约反查回品种(前端联动状态), 不做严格校验:
    查不出来就把下拉设为「自定义」, 不阻塞手填合约。指数、外盘等其它代码一律认不出。
    """
    parts = split_cont_symbol(symbol)
    if parts is not None:
        return parts
    match = MONTH_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        return None
    return match.group(1).upper(), match.group(2)


def normalize_exchange(exchange_id: str | None) -> str | None:
    """校验交易所代码, 避免把任意字符串透传给 SDK 查询。"""
    value = (exchange_id or "").strip()
    if not value:
        return None
    if EXCHANGE_RE.fullmatch(value) is None:
        raise ValueError("交易所代码无效")
    return value.upper()


def normalize_product(product_id: str | None) -> str | None:
    value = (product_id or "").strip()
    if not value:
        return None
    if PRODUCT_RE.fullmatch(value) is None:
        raise ValueError("品种代码无效")
    return value


def _text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _number(value) -> float:
    """持仓量/成交量转 float; NaN、None 和脏值一律记 0, 免得排序被 NaN 打乱。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else number  # NaN != NaN


def _records(info) -> list[dict]:
    """query_symbol_info 返回 DataFrame; 兼容测试里的普通列表。"""
    if info is None:
        return []
    if hasattr(info, "to_dict"):
        return info.to_dict("records")
    return list(info)


def exchange_of(symbol: str) -> str:
    """``SHFE.fu2611`` -> ``SHFE``; 没有交易所前缀时返回空串。"""
    return symbol.partition(".")[0].strip().upper()


def single_side_open_interest(symbol: str, value):
    """把静态查询的昨日持仓量归一到单边, 便于与报价对象的 ``open_interest`` 直接比较。

    上期所/能源中心/郑商所/大商所双边计量, 除以 2; 其余交易所原样返回。
    只影响数值大小, 不影响排序(同一品种的缩放系数相同)。
    """
    number = _number(value)
    if exchange_of(symbol) in BILATERAL_OPEN_INTEREST_EXCHANGES:
        return number / 2
    return number


def product_name(instrument_name: str, product_id: str) -> str:
    """「燃油主连」->「燃油」; 名称缺失时退回品种代码。"""
    name = _text(instrument_name)
    if name.endswith(CONT_SUFFIX):
        name = name[: -len(CONT_SUFFIX)].strip()
    return name or product_id.upper()


def build_products(api, symbols=None) -> list[dict]:
    """列出全部主连品种: 中文名、当前主力合约, 以及用作排序键的主力合约持仓量。

    主连合约自身不带持仓量(CONT 的 pre_open_interest 恒为 0), 所以排序键要再查一次
    它们的标的(即真实的主力合约)。两次都是合约服务的静态查询, 合计约 0.5 秒,
    不需要订阅 88 路行情 —— 冷启动时也不会把采集线程堵上好几秒。
    """
    conts = list(symbols) if symbols is not None else list(
        api.query_quotes(ins_class="CONT", expired=False))
    if not conts:
        return []
    cont_rows = {_text(row.get("instrument_id")): row
                 for row in _records(api.query_symbol_info(conts))}
    mains = [_text(cont_rows.get(symbol, {}).get("underlying_symbol")) for symbol in conts]
    open_interest = {}
    wanted = sorted({main for main in mains if main})
    if wanted:
        for row in _records(api.query_symbol_info(wanted)):
            symbol = _text(row.get("instrument_id"))
            open_interest[symbol] = single_side_open_interest(
                symbol, row.get("pre_open_interest"))
    products = []
    for symbol, main in zip(conts, mains):
        parts = split_cont_symbol(symbol)
        if parts is None:
            continue
        exchange_id, product_id = parts
        products.append({
            "exchangeId": exchange_id,
            "productId": product_id,
            "name": product_name(cont_rows.get(symbol, {}).get("instrument_name"), product_id),
            "contSymbol": symbol,
            "mainSymbol": main,
            # 昨日持仓量(单边): 只用于把活跃品种排到前面, 不当作实时值展示。
            "openInterest": open_interest.get(main, 0.0),
        })
    return products


def rank_products(products: list[dict]) -> None:
    """就地按主力合约持仓量降序排列。88 个品种按代码排没人找得到, 所以按活跃度排。"""
    products.sort(key=lambda item: item["openInterest"], reverse=True)


def group_products(products: list[dict]) -> list[dict]:
    """按交易所分组, 组内保持传入顺序(即持仓量降序)。"""
    buckets: dict[str, list[dict]] = {}
    for product in products:
        buckets.setdefault(product["exchangeId"], []).append(product)
    groups = []
    for exchange_id, _ in EXCHANGES:
        if buckets.get(exchange_id):
            groups.append({"exchangeId": exchange_id, "exchangeName": EXCHANGE_NAMES[exchange_id],
                           "products": buckets.pop(exchange_id)})
    # 交易所代码可能不在常用列表里(新增交易所时不至于整组丢失)。
    for exchange_id in sorted(buckets):
        groups.append({"exchangeId": exchange_id, "exchangeName": exchange_id,
                       "products": buckets[exchange_id]})
    return groups


def build_months(api, exchange_id: str, product_id: str, main_symbol: str = "") -> list[dict]:
    """该品种未下市月份合约, 按主力先后(昨日持仓量降序)排列。

    排序键同样取静态查询里的昨日持仓量: 合约目录一次要列十几个月份, 为排序去订阅
    十来路行情既慢又会把订阅池越撑越大, 而昨日持仓量足以把主力排在第一位 ——
    「谁是主力」另有权威来源(主连的标的合约), 不靠这个排序猜。
    """
    symbols = list(api.query_quotes(ins_class="FUTURE", exchange_id=exchange_id,
                                    product_id=product_id, expired=False))
    if not symbols:
        return []
    rows = {_text(row.get("instrument_id")): row
            for row in _records(api.query_symbol_info(symbols))}
    months = []
    for symbol in symbols:
        row = rows.get(symbol, {})
        months.append({
            "symbol": symbol,
            "name": _text(row.get("instrument_name")) or symbol,
            # 昨日持仓量, 已归一到单边(见 single_side_open_interest)。
            "openInterest": single_side_open_interest(symbol, row.get("pre_open_interest")),
            "isMain": bool(main_symbol) and symbol == main_symbol,
        })
    months.sort(key=lambda item: item["openInterest"], reverse=True)
    return months


def fallback_catalog(error: str | None = None) -> dict:
    """行情源不可用时的目录: 内置常用品种, 没有主力合约映射。"""
    products = [{
        "exchangeId": exchange_id,
        "productId": product_id,
        "name": name,
        "contSymbol": cont_symbol(exchange_id, product_id),
        "mainSymbol": "",
        "openInterest": 0.0,
    } for exchange_id, product_id, name in FALLBACK_PRODUCTS]
    return {"groups": group_products(products), "source": "fallback", "error": error}


class CatalogService:
    """带缓存与回退的合约目录。

    ``manager_provider`` 每次调用都取当前的 FeedManager: 测试和离线预览会替换
    ``app.manager``, 缓存服务不能抓着启动时那一个实例不放。
    """

    def __init__(self, manager_provider, *, catalog_ttl: float = CATALOG_TTL_SEC,
                 months_ttl: float = MONTHS_TTL_SEC, timeout: float = JOB_TIMEOUT_SEC):
        self._manager = manager_provider
        self._catalog_ttl = catalog_ttl
        self._months_ttl = months_ttl
        self._timeout = timeout
        self._lock = threading.Lock()
        self._catalog: tuple[float, dict] | None = None
        self._months: dict[tuple[str, str], tuple[float, dict]] = {}

    async def payload(self, exchange: str | None = None, product: str | None = None,
                      refresh: bool = False) -> dict:
        """选择器需要的全部数据: 品种目录 + (可选)指定品种的月份列表。"""
        exchange_id = normalize_exchange(exchange)
        product_id = normalize_product(product)
        catalog = await self._catalog_payload(refresh)
        result = {"groups": catalog["groups"], "source": catalog["source"],
                  "error": catalog["error"], "months": [], "monthsError": None}
        if exchange_id and product_id:
            result["months"], result["monthsError"] = await self._months_payload(
                exchange_id, product_id, self._main_symbol(catalog, exchange_id, product_id), refresh)
        return result

    async def _catalog_payload(self, refresh: bool) -> dict:
        with self._lock:
            cached = self._catalog
        if cached is not None and not refresh and time.monotonic() - cached[0] < self._catalog_ttl:
            return cached[1]
        error = None
        try:
            data = await self._query(lambda api: group_products(ranked_products(api)))
            data = {"groups": data, "source": "live", "error": None}
        except Exception as exc:
            # 行情源不可用时选择器仍然要能用: 回退到内置常用品种, 错误只作提示。
            error = f"{type(exc).__name__}: {exc}"
            data = fallback_catalog(error)
        if data["source"] == "live":
            with self._lock:
                self._catalog = (time.monotonic(), data)
        return data

    async def _months_payload(self, exchange_id: str, product_id: str, main_symbol: str,
                              refresh: bool) -> tuple[list[dict], str | None]:
        key = (exchange_id, product_id)
        with self._lock:
            cached = self._months.get(key)
        if cached is not None and not refresh and time.monotonic() - cached[0] < self._months_ttl:
            return cached[1], None
        try:
            months = await self._query(
                lambda api: build_months(api, exchange_id, product_id, main_symbol))
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._months[key] = (time.monotonic(), months)
        return months, None

    @staticmethod
    def _main_symbol(catalog: dict, exchange_id: str, product_id: str) -> str:
        for group in catalog["groups"]:
            for item in group["products"]:
                if item["exchangeId"] == exchange_id and item["productId"] == product_id:
                    return item["mainSymbol"]
        return ""

    async def symbol_label(self, symbol: str) -> dict:
        """``{"symbol", "label"}``: 顶栏只读展示用的合约名。

        名称一律取标的合约(月份合约)的中文名: 主连合自带的 ``instrument_name`` 是
        「燃油主连」这类统称, 随主力换月不会变, 而顶栏要回答的是"现在到底在看哪个合约"。
        任何一步取不到就回退到原始代码, 展示位永远有东西可显示。
        """
        value = validate_symbol(symbol)
        try:
            label = await self._query(lambda api: resolve_label(api, value))
        except Exception:
            label = ""
        return {"symbol": value, "label": label or value}

    async def _query(self, fn):
        """把一次 SDK 查询交给采集线程执行, 并等待结果(超时按失败处理)。"""
        return await self._manager().query(fn, self._timeout)


def resolve_label(api, symbol: str) -> str:
    """合约的展示名(采集线程里执行): 主连先解析出标的合约, 再取它的中文名。

    为什么不直接读 quote 的 ``instrument_name``: 主连的报价对象来自一次 ``get_quote``
    订阅, 冷启动或闭市时可能还没下发; 而 ``query_symbol_info`` 是合约服务的静态查询,
    走的是和品种目录同一条路径。整条链上任何一步拿不到, 就回退到当前已有的名字或代码。

    代码来自 URL, 可能是写错的: 先确认合约服务里有它(见 ingest.listed_symbols),
    否则 get_quote / query_symbol_info 收到不存在的代码会让整条连接停摆。
    """
    try:
        if symbol not in listed_symbols(api, [symbol]):
            return symbol
    except Exception:
        return symbol
    contract = symbol
    if split_cont_symbol(symbol) is not None:
        try:
            underlying = _text(getattr(api.get_quote(symbol), "underlying_symbol", ""))
        except Exception:
            underlying = ""
        if underlying:
            contract = underlying
    try:
        rows = _records(api.query_symbol_info([contract]))
    except Exception:
        rows = []
    name = _text(rows[0].get("instrument_name")) if rows else ""
    return name or contract


def ranked_products(api) -> list[dict]:
    """采集线程里的完整目录查询: 全部主连 -> 中文名/主力合约/持仓量 -> 按持仓量排序。"""
    products = build_products(api)
    rank_products(products)
    return products
