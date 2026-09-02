# -*- coding: utf-8 -*-
"""FastAPI 入口: 静态页面 + REST 历史 + WebSocket 实时推送。

启动: uvicorn app:app --host 127.0.0.1 --port 8000
或直接运行 run.bat
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from indicator import CFG
from ingest import FeedManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SYMBOL = "KQ.m@SHFE.fu"

app = FastAPI(title="Volume Suite Web")
manager = FeedManager()


@app.on_event("startup")
async def _startup():
    manager.start(asyncio.get_running_loop())
    manager.ensure(DEFAULT_SYMBOL)


@app.get("/api/history")
def history(symbol: str = DEFAULT_SYMBOL, ltf: int = 0):
    """返回最近 N 根 bar 的完整快照; 订阅未就绪时最多等 90 秒(闭市回填慢)。

    ltf: 买卖量拆分粒度(秒), 0=逐 tick 盘口判定, 1/5/15/30=小周期阴阳归类;
         切换后最多 1~2 秒由 ingest 线程重算
    """
    if ltf not in CFG["ltfOptions"]:
        ltf = 0
    feed = manager.ensure(symbol)
    if feed.ltf != ltf:
        feed.ltf = ltf
        feed.dirty = True
    # 等待重算完成(dirty 清除); 闭市时 ingest 循环也在每秒轮询, 通常 1~2 秒内完成
    deadline = time.time() + 15
    while feed.dirty and time.time() < deadline:
        time.sleep(0.2)
    if not feed.ready.wait(timeout=90):
        return {"symbol": symbol, "cfg": CFG, "bars": [], "pending": True}
    return feed.snapshot


@app.websocket("/ws")
async def ws(websocket: WebSocket, symbol: str = DEFAULT_SYMBOL):
    await websocket.accept()
    manager.ensure(symbol)
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    manager.clients.add(q)
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
        manager.clients.discard(q)


app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True))
