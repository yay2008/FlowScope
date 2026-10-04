# -*- coding: utf-8 -*-
"""FastAPI 入口: 静态页面 + REST 历史 + WebSocket 实时推送。

启动: uvicorn app:app --host 127.0.0.1 --port 8000
或直接运行 run.bat
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import time

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import crypto_feed
import ingest
from backup import BackupScheduler
from binance_feed import BinanceAdapter
from catalog import CatalogService
from crypto_aggregate import AggregateBook
from crypto_backfill import Backfiller
from favorites import FavoriteStore, FavoritesService, MAX_FAVORITES
from indicator import CFG, DEFAULT_TF_SEC, TF_OPTIONS, ltf_options
from ingest import FeedManager, validate_symbol, validate_tf
from okx_feed import OkxAdapter
from paper import DEFAULT_CASH, PaperService, PaperStore
from paper_crypto import CryptoPaperService

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SYMBOL = "KQ.m@SHFE.fu"
FAVORITES_FILE = "favorites.json"
# 常驻采集的主周期: 每个周期的历史各自落盘, 都要在无人看图时继续积累。
COLLECT_TFS = TF_OPTIONS

app = FastAPI(title="FlowScope")
manager = FeedManager()
# 加密永续(币安 BINANCE.、OKX OKX.、多所汇总 AGG.)有自己的连接与线程, 与 TqSdk 互不影响;
# 按合约代码分流, 见 manager_for。
crypto = crypto_feed.CryptoManager([BinanceAdapter(), OkxAdapter()], backfiller=Backfiller())
crypto.aggregates = AggregateBook(crypto)
# 用 provider 而不是 manager 实例: 测试与离线预览会替换 app.manager,
# 目录缓存和自选报价必须跟着当前实例走。
catalog = CatalogService(lambda: manager)
# 自选文件落在数据目录里(DATA_DIR 在调用时现取, 便于测试/预览替换)。
favorites = FavoritesService(lambda: manager,
                             FavoriteStore(lambda: os.path.join(ingest.DATA_DIR, FAVORITES_FILE)))
backups = BackupScheduler(lambda: ingest.DATA_DIR)
# 模拟交易账户同样落在数据目录里, 随定期快照一起备份。
paper = PaperService(lambda: manager,
                     PaperStore(lambda: os.path.join(ingest.DATA_DIR, "paper", "account.json")))
# 加密永续另一个账户(USDT、小数数量、杠杆、资金费), 报价来自加密行情的推送缓存。
crypto_paper = CryptoPaperService(lambda: crypto,
                                  PaperStore(lambda: os.path.join(ingest.DATA_DIR, "paper", "crypto.json")))


def manager_for(symbol: str):
    """合约代码 -> 负责它的采集: 加密合约走 CryptoManager, 其余都是 TqSdk 的 FeedManager。

    两者接口一致(ensure / feed_retry_error / add_client / remove_client / status_snapshot)。
    """
    return crypto if crypto_feed.is_crypto(symbol) else manager


def require_feed(symbol: str, tf: int):
    """校验合约并取得 Feed; 订阅失败且未过冷却期时快速失败(不干等 90 秒)。

    失败不是永久状态: 冷却期一过 FeedManager 会自动重订, 这里只需把剩余等待时间告诉调用方。
    """
    try:
        normalized_symbol = validate_symbol(symbol)
        feed = manager_for(normalized_symbol).ensure(normalized_symbol, tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    retry_error = manager_for(feed.symbol).feed_retry_error(feed)
    if retry_error:
        raise HTTPException(status_code=400, detail=retry_error)
    return feed


def collection_keys() -> list[tuple[str, int]]:
    """常驻采集集合: 默认合约 + 自选(按面板顺序), 每个合约采全部主周期。

    只管 TqSdk 合约; 加密合约由 CryptoManager 自己常驻采集(手工写进自选文件的也排除)。
    """
    symbols = list(dict.fromkeys(symbol for symbol in [DEFAULT_SYMBOL, *favorites.symbols()]
                                 if not crypto_feed.is_crypto(symbol)))
    return [(symbol, tf) for symbol in symbols for tf in COLLECT_TFS]


def sync_collection():
    manager.set_pinned(collection_keys())
    # 自选里的加密合约(按面板顺序)接在默认的 BTC 永续后面常驻采集
    crypto.set_pinned([symbol for symbol in favorites.symbols() if crypto_feed.is_crypto(symbol)])


@app.on_event("startup")
async def _startup():
    # 挂单撮合跟着采集循环走: 每轮 wait_update 之后用最新报价检查一次。
    if paper.on_loop not in manager.loop_hooks:
        manager.loop_hooks.append(paper.on_loop)
    if crypto_paper.on_cycle not in crypto.loop_hooks:
        crypto.loop_hooks.append(crypto_paper.on_cycle)
    manager.start(asyncio.get_running_loop())
    crypto.start(asyncio.get_running_loop())
    sync_collection()
    backups.start()


@app.on_event("shutdown")
async def _shutdown():
    manager.stop()
    crypto.stop()
    backups.stop()


@app.get("/api/history")
async def history(symbol: str = DEFAULT_SYMBOL, ltf: int = 0, tf: int = DEFAULT_TF_SEC):
    """返回最近 N 根 bar 的完整快照; 订阅未就绪时最多等 90 秒(闭市回填慢)。

    tf : 主图周期(秒), 10 或 30; 非法值回落到 30
    ltf: 买卖量拆分粒度(秒), 0=逐 tick 盘口判定, 1/5/10/15/30=小周期阴阳归类;
         必须能整除主周期, 否则回落为 0; 切换后最多 1~2 秒由 ingest 线程重算
    """
    tf = validate_tf(tf)
    if ltf not in ltf_options(tf):
        ltf = 0
    feed = require_feed(symbol, tf)
    source = manager_for(feed.symbol)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.snapshot_for(ltf)
        if snapshot is not None:
            return snapshot
        # 订阅失败(含冷却期内的重试中)都走这里, 不再等到 90 秒超时才回话。
        retry_error = source.feed_retry_error(feed)
        if retry_error:
            raise HTTPException(status_code=400, detail=retry_error)
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if source.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": feed.symbol, "cfg": feed.cfg(), "tf": tf, "ltf": ltf,
            "bars": [], "pending": True, "status": source.status_snapshot()}


@app.get("/api/status")
def status():
    """返回行情采集线程状态(含常驻采集集合)与数据备份状态，便于判断 pending 原因。

    顶层字段是 TqSdk 采集; 加密行情(币安、OKX、回填)的状态单独放在 ``crypto`` 里。
    """
    return {**manager.status_snapshot(), "crypto": crypto.status_snapshot(), "backup": backups.status()}


@app.get("/api/symbols")
async def symbols(exchange: str | None = None, product: str | None = None, refresh: bool = False):
    """合约选择器的数据源: 品种(主连)目录, 以及指定品种的月份合约。

    - 不带参数: 按交易所分组的全部主连品种, 组内按主力合约持仓量降序, 附当前主力合约代码。
    - 带 exchange + product: 额外返回该品种未下市月份合约(持仓量降序, 标出主力)。
    - 查询在采集线程里发给 TqSdk 合约服务并缓存; 行情源不可用时回退到内置常用品种表
      (`source="fallback"`), 月份列表为空, 页面仍能选到常用主力。
    - 期货分组之后接加密分组(各交易所永续 + 多所汇总, 按 24 小时成交额降序, 见
      CryptoManager.catalog_groups); 加密品种没有月份, 二级只有永续本身, 不去问 TqSdk。
    """
    crypto_product = bool(exchange) and exchange.strip().upper() in crypto_feed.VENUE_NAMES
    try:
        if crypto_product:
            exchange = product = None
        payload = await catalog.payload(exchange=exchange, product=product, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**payload, "groups": payload["groups"] + crypto.catalog_groups()}


@app.get("/api/symbol")
async def symbol_label(symbol: str = DEFAULT_SYMBOL):
    """顶栏只读展示用的合约名(中文名); 取不到时回退成合约代码。"""
    try:
        if crypto_feed.is_crypto(symbol):
            value = validate_symbol(symbol)
            return {"symbol": value, "label": crypto.label(value)}
        return await catalog.symbol_label(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/favorites")
def favorites_list():
    """自选合约代码列表(服务端保存, 顺序即面板显示顺序)。

    顺带同步常驻采集集合: 自选文件被手工改过时, 面板下一次读列表就能跟上。
    """
    sync_collection()
    return {"symbols": favorites.symbols(), "max": MAX_FAVORITES}


@app.post("/api/favorites")
async def favorites_add(symbol: str):
    """收藏一个合约; 幂等, 重复收藏不改变顺序。

    自选都会常驻订阅, 所以新代码先到合约服务确认存在: 混进一个不存在的代码, 采集线程
    每次重试订阅都会被它拖住(见 ingest.listed_symbols)。行情源不可用、查不了时照常收藏,
    订阅时还有同样的检查兜底。
    """
    try:
        value = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if crypto_feed.is_crypto(value):
        # 加密合约不问 TqSdk: 交易所合约列表里有(或是能合成的多所汇总)才收
        if value not in favorites.symbols() and not crypto.known(value):
            raise HTTPException(status_code=400, detail=f"不支持的加密合约 {value}")
    elif value not in favorites.symbols() and await favorites.verify(value) is False:
        raise HTTPException(status_code=400, detail=f"合约 {value} 不存在, 请检查代码")
    return _favorite_change(favorites.add, value)


@app.delete("/api/favorites")
def favorites_remove(symbol: str):
    """取消收藏。"""
    return _favorite_change(favorites.remove, symbol)


def _favorite_change(action, symbol: str):
    try:
        symbols = action(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sync_collection()
    return {"symbols": symbols, "max": MAX_FAVORITES}


@app.get("/api/watch")
async def watch(symbols: str = "", refresh: bool = False):
    """自选面板用的轻量报价快照(最新价/涨跌幅/持仓量), `symbols` 用逗号分隔。

    只读报价对象, 不订阅 K 线与 tick; 行情源不可用时返回空报价 + error,
    面板仍然列出代码并能点击切换。
    """
    wanted = []
    for item in symbols.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            value = validate_symbol(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if value not in wanted:
            wanted.append(value)
    if len(wanted) > MAX_FAVORITES:
        raise HTTPException(status_code=400, detail=f"一次最多查询 {MAX_FAVORITES} 个合约")
    coins = [value for value in wanted if crypto_feed.is_crypto(value)]
    futures = [value for value in wanted if value not in coins]
    payload = await favorites.watch(futures, refresh=refresh)
    if not coins:
        return payload
    # 加密合约的报价来自推送(24 小时行情), 与 TqSdk 无关: TqSdk 不可用时它们照样有报价,
    # 期货那几行留空并在 error 里说明。
    rows = {row["symbol"]: row for row in payload["quotes"] + crypto.watch_rows(coins)}
    return {"quotes": [rows[value] for value in wanted if value in rows], "source": "live",
            "error": payload["error"]}


@app.get("/api/footprint")
async def footprint(symbol: str = DEFAULT_SYMBOL, tf: int = DEFAULT_TF_SEC):
    """返回 tick 窗口内各 bar 的分价位买卖量矩阵(足迹图)，口径同 ltf=0。

    tf: 主图周期(秒), 足迹跟着主图周期走。
    快照未就绪时最多等 90 秒(闭市回填慢)；历史覆盖取决于 tick 窗口和持续采集。
    """
    tf = validate_tf(tf)
    feed = require_feed(symbol, tf)
    source = manager_for(feed.symbol)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.footprint_snapshot()
        if snapshot is not None:
            return snapshot
        retry_error = source.feed_retry_error(feed)
        if retry_error:
            raise HTTPException(status_code=400, detail=retry_error)
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if source.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": feed.symbol, "tickSize": None, "bars": [], "pending": True,
            "status": source.status_snapshot()}


@app.get("/api/paper")
async def paper_state(symbol: str = DEFAULT_SYMBOL):
    """模拟交易面板: 账户、持仓、委托、成交, 以及当前合约(主连换成标的月份)的盘口与交易状态。

    加密合约走另一个账户(``mode="crypto"``, 见 paper_crypto.py)。
    """
    try:
        if crypto_feed.is_crypto(symbol):
            return crypto_paper.state(symbol)
        return await paper.state(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/paper/orders")
async def paper_order(order: dict = Body(...)):
    """下单: ``{symbol, side: buy|sell, qty, type: market|limit, price?, clientId?}``。

    市价单当场成交或返回 400(对手盘不够、不在交易时段等); 限价单够得着就成交, 否则挂单。
    同一个 clientId 只下一次, 页面双击或超时重发不会重复成交。
    加密合约的数量是小数的币, 按五档盘口撮合(见 paper_crypto.py)。
    """
    try:
        if crypto_feed.is_crypto(str(order.get("symbol") or "") if isinstance(order, dict) else ""):
            return crypto_paper.place(order)
        return await paper.place(order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/paper/orders/{order_id}")
def paper_cancel(order_id: str):
    """撤掉一笔挂单。加密账户的委托编号以 C 开头。"""
    try:
        if order_id.startswith("C"):
            return crypto_paper.cancel(order_id)
        return paper.cancel(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/paper/flatten")
async def paper_flatten(symbol: str):
    """按市价平掉该合约(主连按当前标的月份)的全部持仓。"""
    try:
        if crypto_feed.is_crypto(symbol):
            return crypto_paper.flatten(symbol)
        return await paper.flatten(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/paper/reset")
def paper_reset(cash: float = DEFAULT_CASH, symbol: str = ""):
    """清空模拟账户(持仓、委托、成交), 以新的初始资金重新开始。

    symbol 是加密合约时重置的是加密账户(初始资金按 USDT), 否则是期货账户。
    """
    try:
        if crypto_feed.is_crypto(symbol):
            return crypto_paper.reset(cash)
        return paper.reset(cash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/paper/leverage")
def paper_leverage(symbol: str, leverage: int):
    """设置加密合约的杠杆(每个合约一个, 默认 10 倍); 降杠杆后保证金不够会被拒。"""
    try:
        if not crypto_feed.is_crypto(symbol):
            raise ValueError("只有加密合约能设杠杆")
        return crypto_paper.set_leverage(symbol, leverage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws")
async def ws(websocket: WebSocket, symbol: str = DEFAULT_SYMBOL, ltf: int = 0, footprint: bool = False,
             tf: int = DEFAULT_TF_SEC):
    await websocket.accept()
    try:
        symbol = validate_symbol(symbol)
        tf = validate_tf(tf)
        source = manager_for(symbol)
        feed = source.ensure(symbol, tf)
        if ltf not in ltf_options(tf):
            ltf = 0
        retry_error = source.feed_retry_error(feed)
        if retry_error:
            raise ValueError(retry_error)
    except ValueError as exc:
        await websocket.close(code=1008, reason=str(exc))
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    source.add_client(q, symbol, ltf, footprint, tf)
    try:
        # 先注册，再读取快照。期间入队的旧增量可由 revision 排除，避免 REST→WS 空窗。
        deadline = time.monotonic() + 90
        while True:
            snapshot = feed.snapshot_for(ltf)
            fp = feed.footprint_snapshot() if footprint else None
            if snapshot is not None and (not footprint or fp is not None):
                break
            if time.monotonic() >= deadline:
                await websocket.close(code=1013, reason="等待行情超时，请重试")
                return
            await asyncio.sleep(0.1)
        await websocket.send_json({"type": "snapshot", **snapshot, "footprint": fp})
        bar_revision = snapshot["revision"]
        fp_revision = fp["revision"] if fp else -1
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # 心跳: 闭市无数据时也能及时发现断连
                state = source.status_snapshot()
                with feed._state_lock:
                    error = feed.error
                await websocket.send_json({"type": "ping", "status": state, "error": error})
                continue
            if msg.get("type") == "resync":
                await websocket.close(code=1013, reason="客户端积压，请重取快照")
                return
            is_fp = msg.get("type") in {"footprints", "footprint_snapshot"}
            revision = msg.get("revision", -1)
            if revision <= (fp_revision if is_fp else bar_revision):
                continue
            if msg.get("symbol") == symbol:
                await websocket.send_json(msg)
                if is_fp:
                    fp_revision = revision
                else:
                    bar_revision = revision
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        source.remove_client(q)


# Windows 注册表可能把 SVG 识别为 image/svg，浏览器需要标准 MIME 类型。
mimetypes.add_type("image/svg+xml", ".svg")
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True))
