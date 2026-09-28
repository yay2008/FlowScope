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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import ingest
from backup import BackupScheduler
from catalog import CatalogService
from favorites import FavoriteStore, FavoritesService, MAX_FAVORITES
from indicator import CFG, DEFAULT_TF_SEC, TF_OPTIONS, ltf_options
from ingest import FeedManager, period_cfg, validate_symbol, validate_tf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SYMBOL = "KQ.m@SHFE.fu"
FAVORITES_FILE = "favorites.json"
# 常驻采集的主周期: 每个周期的历史各自落盘, 都要在无人看图时继续积累。
COLLECT_TFS = TF_OPTIONS

app = FastAPI(title="FlowScope")
manager = FeedManager()
# 用 provider 而不是 manager 实例: 测试与离线预览会替换 app.manager,
# 目录缓存和自选报价必须跟着当前实例走。
catalog = CatalogService(lambda: manager)
# 自选文件落在数据目录里(DATA_DIR 在调用时现取, 便于测试/预览替换)。
favorites = FavoritesService(lambda: manager,
                             FavoriteStore(lambda: os.path.join(ingest.DATA_DIR, FAVORITES_FILE)))
backups = BackupScheduler(lambda: ingest.DATA_DIR)


def require_feed(symbol: str, tf: int):
    """校验合约并取得 Feed; 订阅失败且未过冷却期时快速失败(不干等 90 秒)。

    失败不是永久状态: 冷却期一过 FeedManager 会自动重订, 这里只需把剩余等待时间告诉调用方。
    """
    try:
        normalized_symbol = validate_symbol(symbol)
        feed = manager.ensure(normalized_symbol, tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    retry_error = manager.feed_retry_error(feed)
    if retry_error:
        raise HTTPException(status_code=400, detail=retry_error)
    return feed


def collection_keys() -> list[tuple[str, int]]:
    """常驻采集集合: 默认合约 + 自选(按面板顺序), 每个合约采全部主周期。"""
    symbols = list(dict.fromkeys([DEFAULT_SYMBOL, *favorites.symbols()]))
    return [(symbol, tf) for symbol in symbols for tf in COLLECT_TFS]


def sync_collection():
    manager.set_pinned(collection_keys())


@app.on_event("startup")
async def _startup():
    manager.start(asyncio.get_running_loop())
    sync_collection()
    backups.start()


@app.on_event("shutdown")
async def _shutdown():
    manager.stop()
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

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.snapshot_for(ltf)
        if snapshot is not None:
            return snapshot
        # 订阅失败(含冷却期内的重试中)都走这里, 不再等到 90 秒超时才回话。
        retry_error = manager.feed_retry_error(feed)
        if retry_error:
            raise HTTPException(status_code=400, detail=retry_error)
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if manager.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": feed.symbol, "cfg": period_cfg(tf), "tf": tf, "ltf": ltf,
            "bars": [], "pending": True, "status": manager.status_snapshot()}


@app.get("/api/status")
def status():
    """返回行情采集线程状态(含常驻采集集合)与数据备份状态，便于判断 pending 原因。"""
    return {**manager.status_snapshot(), "backup": backups.status()}


@app.get("/api/symbols")
async def symbols(exchange: str | None = None, product: str | None = None, refresh: bool = False):
    """合约选择器的数据源: 品种(主连)目录, 以及指定品种的月份合约。

    - 不带参数: 按交易所分组的全部主连品种, 组内按主力合约持仓量降序, 附当前主力合约代码。
    - 带 exchange + product: 额外返回该品种未下市月份合约(持仓量降序, 标出主力)。
    - 查询在采集线程里发给 TqSdk 合约服务并缓存; 行情源不可用时回退到内置常用品种表
      (`source="fallback"`), 月份列表为空, 页面仍能选到常用主力。
    """
    try:
        return await catalog.payload(exchange=exchange, product=product, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/symbol")
async def symbol_label(symbol: str = DEFAULT_SYMBOL):
    """顶栏只读展示用的合约名(中文名); 取不到时回退成合约代码。"""
    try:
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
def favorites_add(symbol: str):
    """收藏一个合约; 幂等, 重复收藏不改变顺序。"""
    return _favorite_change(favorites.add, symbol)


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
    return await favorites.watch(wanted, refresh=refresh)


@app.get("/api/footprint")
async def footprint(symbol: str = DEFAULT_SYMBOL, tf: int = DEFAULT_TF_SEC):
    """返回 tick 窗口内各 bar 的分价位买卖量矩阵(足迹图)，口径同 ltf=0。

    tf: 主图周期(秒), 足迹跟着主图周期走。
    快照未就绪时最多等 90 秒(闭市回填慢)；历史覆盖取决于 tick 窗口和持续采集。
    """
    tf = validate_tf(tf)
    feed = require_feed(symbol, tf)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.footprint_snapshot()
        if snapshot is not None:
            return snapshot
        retry_error = manager.feed_retry_error(feed)
        if retry_error:
            raise HTTPException(status_code=400, detail=retry_error)
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if manager.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": feed.symbol, "tickSize": None, "bars": [], "pending": True,
            "status": manager.status_snapshot()}


@app.websocket("/ws")
async def ws(websocket: WebSocket, symbol: str = DEFAULT_SYMBOL, ltf: int = 0, footprint: bool = False,
             tf: int = DEFAULT_TF_SEC):
    await websocket.accept()
    try:
        symbol = validate_symbol(symbol)
        tf = validate_tf(tf)
        feed = manager.ensure(symbol, tf)
        if ltf not in ltf_options(tf):
            ltf = 0
        retry_error = manager.feed_retry_error(feed)
        if retry_error:
            raise ValueError(retry_error)
    except ValueError as exc:
        await websocket.close(code=1008, reason=str(exc))
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    manager.add_client(q, symbol, ltf, footprint, tf)
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
                state = manager.status_snapshot()
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
        manager.remove_client(q)


# Windows 注册表可能把 SVG 识别为 image/svg，浏览器需要标准 MIME 类型。
mimetypes.add_type("image/svg+xml", ".svg")
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True))
