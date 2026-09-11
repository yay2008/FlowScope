# -*- coding: utf-8 -*-
"""FastAPI 入口: 静态页面 + REST 历史 + WebSocket 实时推送。

启动: uvicorn app:app --host 127.0.0.1 --port 8000
或直接运行 run.bat
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from indicator import CFG, DEFAULT_TF_SEC, ltf_options
from ingest import FeedManager, period_cfg, validate_symbol, validate_tf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SYMBOL = "KQ.m@SHFE.fu"

app = FastAPI(title="FlowScope")
manager = FeedManager()


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


@app.on_event("startup")
async def _startup():
    manager.start(asyncio.get_running_loop())
    manager.ensure(DEFAULT_SYMBOL)


@app.on_event("shutdown")
async def _shutdown():
    manager.stop()


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
    """返回行情采集线程状态，便于前端和运维判断 pending 原因。"""
    return manager.status_snapshot()


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


app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True))
