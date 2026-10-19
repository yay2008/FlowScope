"""训练 HTTP 接口与独立天勤下载线程；不在实时采集线程中下载历史。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from indicator import CFG
from training import Conflict
from training_data import CONTRACT, TrainingError, demo_dataset, number, parse_csv


class TrainingDownloads:
    def __init__(self, datasets, downloader=None):
        self.datasets = datasets
        self.downloader = downloader or download_tq
        self.lock = threading.Lock()
        self.jobs = {}
        self.stop_event = threading.Event()

    def start(self, raw):
        symbol = str(raw.get("symbol") or "").strip()
        mode = raw.get("mode", "recent")
        if mode not in ("recent", "range"):
            raise TrainingError("下载模式无效")
        if not CONTRACT.fullmatch(symbol):
            raise TrainingError("请填写实际商品合约，如 SHFE.fu2611，历史下载不使用主连")
        options = {"symbol": symbol, "mode": mode,
                   "count": number(raw.get("count", 5000), "最近根数", 100, 10000, integer=True)}
        if mode == "range":
            try:
                start, end = date.fromisoformat(raw["startDate"]), date.fromisoformat(raw["endDate"])
                if not 0 <= (end - start).days <= 365:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise TrainingError("历史日期范围无效，单次最多一年") from None
            options.update(startDate=start.isoformat(), endDate=end.isoformat())
        with self.lock:
            if any(job["status"] == "running" for job in self.jobs.values()):
                raise Conflict("已有行情下载任务，请等待它完成")
            self.stop_event.clear()
            identity = uuid.uuid4().hex
            job = {"id": identity, "status": "running", "progress": 0, "message": "正在连接天勤…"}
            self.jobs = dict(list(self.jobs.items())[-19:])
            self.jobs[identity] = job
        threading.Thread(target=self._run, args=(identity, options), daemon=True, name="training-download").start()
        return self.get(identity)

    def _update(self, identity, **values):
        with self.lock:
            self.jobs[identity].update(values)

    def _run(self, identity, options):
        try:
            meta, rows = self.downloader(options, self.datasets.root(),
                                         lambda **values: self._update(identity, **values), self.stop_event)
            result = self.datasets.save(meta, rows)
            self._update(identity, status="completed", progress=100, dataset=result, message="历史数据已保存")
        except Exception as exc:
            message = str(exc)
            for key in ("TQ_USER", "TQ_PASS"):
                value = os.getenv(key)
                if value:
                    message = message.replace(value, "[已隐藏]")
            self._update(identity, status="failed", message=(message or type(exc).__name__)[:600])

    def get(self, identity):
        with self.lock:
            if identity not in self.jobs:
                raise TrainingError("下载任务不存在或服务已重启，请刷新数据列表")
            return dict(self.jobs[identity])

    def stop(self):
        self.stop_event.set()


def download_tq(options, root: Path, progress, stopped):
    from tqsdk import TqApi, TqAuth
    from tqsdk.tools import DataDownloader

    user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
    if not user or not password:
        raise TrainingError("请在 .env 配置 TQ_USER / TQ_PASS，或使用 CSV 导入")
    api = None
    temp_path = None
    deadline = time.monotonic() + (300 if options["mode"] == "range" else 120)

    def wait():
        if stopped.is_set():
            raise TrainingError("服务关闭，数据下载已停止")
        if time.monotonic() > deadline:
            raise TrainingError("下载超时，请缩小日期范围或使用 CSV 导入")
        api.wait_update(deadline=time.time() + 1)

    try:
        api = TqApi(auth=TqAuth(user, password), debug=False)
        symbol = options["symbol"]
        info = api.query_symbol_info([symbol])
        if info.empty:
            raise TrainingError("未查到该历史合约的资料，请检查合约代码")
        row = info.iloc[0]
        if row.get("ins_class", "FUTURE") != "FUTURE":
            raise TrainingError("仅支持商品期货合约")
        meta = {"symbol": symbol, "name": str(row.get("instrument_name") or symbol),
                "priceTick": row.get("price_tick"), "multiplier": row.get("volume_multiple"),
                "source": "天勤一分钟历史；合约资料为下载时取得，费用和保证金由本局配置"}
        quote = None
        if options["mode"] == "recent":
            quote = api.get_quote(symbol)
            serial = api.get_kline_serial(symbol, 60, data_length=options["count"])
            while not api.is_serial_ready(serial):
                wait()
            # 最后一根可能仍在形成，统一剔除，不能把半根数据当作已收盘训练行情。
            rows = [{"time": int(item.datetime), "open": float(item.open), "high": float(item.high),
                     "low": float(item.low), "close": float(item.close), "volume": int(item.volume)}
                    for item in serial.iloc[:-1].itertuples() if item.datetime > 0]
            meta["priceTick"] = quote.price_tick
            meta["multiplier"] = quote.volume_multiple
            meta["name"] = quote.instrument_name or symbol
        else:
            root.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix="download-", suffix=".csv", dir=root)
            os.close(fd)
            task = DataDownloader(api, symbol_list=symbol, dur_sec=60,
                                  start_dt=date.fromisoformat(options["startDate"]),
                                  end_dt=date.fromisoformat(options["endDate"]), csv_file_name=temp_path)
            while not task.is_finished():
                wait()
                progress(progress=round(task.get_progress(), 1), message="正在下载历史数据（需要相应天勤权限）…")
            rows = parse_csv(Path(temp_path).read_text(encoding="utf-8-sig"), symbol)
        # 静态资料不一定包含历史时段。未知时段保持为空，训练在时间缺口处截断。
        sessions = getattr(quote, "trading_time", None) if quote is not None else row.get("trading_time")
        if isinstance(sessions, str):
            try:
                sessions = json.loads(sessions)
            except ValueError:
                sessions = None
        if not isinstance(sessions, Mapping):
            sessions = {key: row.get("trading_time_" + key) for key in ("day", "night")}
            sessions = {key: value if isinstance(value, (list, tuple)) else [] for key, value in sessions.items()}
        if isinstance(sessions, Mapping):
            meta["tradingTime"] = {key: [list(pair) for pair in sessions.get(key, [])] for key in ("day", "night")}
        progress(progress=99, message="正在校验一分钟行情…")
        return meta, rows
    finally:
        try:
            if api is not None:
                api.close()
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


def make_training_router(service, downloads):
    router = APIRouter(prefix="/api/training", tags=["training"])

    def respond(call, *args):
        try:
            return JSONResponse(call(*args), headers={"Cache-Control": "no-store"})
        except TrainingError as exc:
            raise HTTPException(409 if isinstance(exc, Conflict) else 400, str(exc),
                                headers={"Cache-Control": "no-store"}) from exc

    @router.get("/indicator-config")
    def indicator_config():
        return respond(lambda: CFG)

    @router.get("/datasets")
    def datasets():
        return respond(service.datasets.list)

    @router.post("/datasets/import")
    def import_dataset(body: dict = Body(...)):
        def run():
            rows = parse_csv(body.get("csv"), str(body.get("symbol") or ""))
            return service.datasets.save({**body, "source": "CSV 导入", "demo": False}, rows)
        return respond(run)

    @router.post("/datasets/demo")
    def demo():
        return respond(service.datasets.save, *demo_dataset())

    @router.post("/downloads")
    def download(body: dict = Body(...)):
        return respond(downloads.start, body)

    @router.get("/downloads/{identity}")
    def download_status(identity: str):
        return respond(downloads.get, identity)

    @router.get("/sessions")
    def sessions():
        return respond(service.list_sessions)

    @router.post("/sessions")
    def create(body: dict = Body(...)):
        return respond(service.create, body)

    @router.get("/sessions/{identity}")
    def session(identity: str, at: int | None = None):
        return respond(service.view, identity, at)

    @router.post("/sessions/{identity}/commands")
    def command(identity: str, body: dict = Body(...)):
        return respond(service.command, identity, body)

    return router
