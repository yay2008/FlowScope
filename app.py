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

from indicator import CFG
from ingest import FeedManager, validate_symbol

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SYMBOL = "KQ.m@SHFE.fu"

app = FastAPI(title="FlowScope")
manager = FeedManager()


@app.on_event("startup")
async def _startup():
    manager.start(asyncio.get_running_loop())
    manager.ensure(DEFAULT_SYMBOL)


@app.on_event("shutdown")
async def _shutdown():
    manager.stop()


@app.get("/api/history")
async def history(symbol: str = DEFAULT_SYMBOL, ltf: int = 0):
    """返回最近 N 根 bar 的完整快照; 订阅未就绪时最多等 90 秒(闭市回填慢)。

    ltf: 买卖量拆分粒度(秒), 0=逐 tick 盘口判定, 1/5/15/30=小周期阴阳归类;
         切换后最多 1~2 秒由 ingest 线程重算
    """
    if ltf not in CFG["ltfOptions"]:
        ltf = 0
    try:
        normalized_symbol = validate_symbol(symbol)
        feed = manager.ensure(normalized_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.snapshot_for(ltf)
        if snapshot is not None:
            return snapshot
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if manager.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": normalized_symbol, "cfg": CFG, "bars": [], "pending": True,
            "status": manager.status_snapshot()}


@app.get("/api/status")
def status():
    """返回行情采集线程状态，便于前端和运维判断 pending 原因。"""
    return manager.status_snapshot()


@app.get("/api/footprint")
async def footprint(symbol: str = DEFAULT_SYMBOL):
    """返回 tick 窗口内各 bar 的分价位买卖量矩阵(足迹图)，口径同 ltf=0。

    快照未就绪时最多等 90 秒(闭市回填慢); tick 历史只有约 83 分钟，更早的 bar 无足迹。
    """
    try:
        normalized_symbol = validate_symbol(symbol)
        feed = manager.ensure(normalized_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = feed.footprint_snapshot()
        if snapshot is not None:
            return snapshot
        with feed._state_lock:
            if feed.error:
                raise HTTPException(status_code=400, detail=feed.error)
        if manager.status_snapshot()["status"] == "error":
            break
        await asyncio.sleep(0.2)
    return {"symbol": normalized_symbol, "tickSize": 1.0, "bars": [], "pending": True,
            "status": manager.status_snapshot()}


@app.websocket("/ws")
async def ws(websocket: WebSocket, symbol: str = DEFAULT_SYMBOL, ltf: int = 0):
    await websocket.accept()
    try:
        symbol = validate_symbol(symbol)
        manager.ensure(symbol)
        if ltf not in CFG["ltfOptions"]:
            ltf = 0
    except ValueError as exc:
        await websocket.close(code=1008, reason=str(exc))
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    manager.add_client(q, symbol, ltf)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # 心跳: 闭市无数据时也能及时发现断连
                await websocket.send_json({"type": "ping"})
                continue
            if msg.get("symbol") == symbol:
                await websocket.send_json(msg)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        manager.remove_client(q)


app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True))
