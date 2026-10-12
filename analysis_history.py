"""只读的 AI 分析档案: 兼容 analysis.py 已写出的 JSON 与截图, 不依赖模型或行情服务。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
DAY_RE = re.compile(r"\d{8}")
FILE_RE = re.compile(r"\d{6}-[A-Za-z0-9_.@+-]+\.json")
IMAGE_RE = re.compile(r"[A-Za-z0-9_.@+-]+\.(?:png|jpg|jpeg|webp)")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
STATUSES = {"done", "stopped", "error", "unknown"}


class InvalidRecord(ValueError):
    """JSON 损坏或必需字段无法识别; 列表跳过它, 单条查询给出明确提示。"""


def record_id(day: str, filename: str) -> str:
    return base64.urlsafe_b64encode(f"{day}/{filename}".encode("ascii")).decode("ascii").rstrip("=")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _reject_constant(value):
    raise ValueError(f"非法 JSON 数值: {value}")


class AnalysisHistory:
    def __init__(self, dir_provider):
        self._dir_provider = dir_provider

    def _root(self) -> Path:
        return Path(self._dir_provider()).resolve()

    def _path(self, identity: str) -> Path:
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", identity):
            raise ValueError("分析记录标识不合法")
        try:
            raw = base64.b64decode(identity + "=" * (-len(identity) % 4), altchars=b"-_", validate=True)
            day, filename = raw.decode("ascii").split("/")
            if not DAY_RE.fullmatch(day) or not FILE_RE.fullmatch(filename):
                raise ValueError
            datetime.strptime(day, "%Y%m%d")
            if record_id(day, filename) != identity:
                raise ValueError
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("分析记录标识不合法") from None
        root = self._root()
        path = (root / day / filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError("找不到这条历史分析")
        return path

    def _read(self, path: Path) -> dict:
        try:
            with path.open(encoding="utf-8-sig") as handle:
                record = json.load(handle, parse_constant=_reject_constant)
            if not isinstance(record, dict) or not _text(record.get("symbol")):
                raise ValueError
            # 原格式有带时区的 createdAt; 缺少它的早期文件可从文件名恢复北京时间。
            stamp = record.get("createdAt")
            created = (datetime.fromisoformat(stamp) if stamp else
                       datetime.strptime(path.parent.name + path.name[:6], "%Y%m%d%H%M%S"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=CST)
            record["createdAt"] = created.astimezone(CST).isoformat(timespec="seconds")
            record["charts"] = [chart for chart in record.get("charts", []) if isinstance(chart, dict)]
            return record
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise InvalidRecord("这条分析记录已损坏, 无法读取") from None

    @staticmethod
    def _summary(identity: str, record: dict) -> dict:
        periods = list(dict.fromkeys(chart["tf"] for chart in record["charts"]
                                    if type(chart.get("tf")) is int and chart["tf"] > 0))
        answer = _text(record.get("answer")).strip()
        conclusion = re.search(r"【结论】\s*(.*?)(?=【|$)", answer, re.S)
        excerpt = " ".join((conclusion.group(1) if conclusion else answer).split())[:180]
        status = _text(record.get("status"))
        return {"id": identity, "createdAt": record["createdAt"], "symbol": record["symbol"],
                "label": _text(record.get("label")) or record["symbol"], "periods": periods,
                "status": status if status in STATUSES else "unknown", "finish": _text(record.get("finish")),
                "excerpt": excerpt}

    def list(self, *, symbol="", q="", date_from="", date_to="", tf=0, status="", offset=0, limit=20):
        if not 1 <= limit <= 100 or offset < 0 or tf < 0 or status and status not in STATUSES:
            raise ValueError("历史查询参数不合法")
        if len(q) > 120 or len(symbol) > 128:
            raise ValueError("搜索内容过长")
        try:
            start = date.fromisoformat(date_from) if date_from else None
            end = date.fromisoformat(date_to) if date_to else None
        except ValueError:
            raise ValueError("日期格式应为 YYYY-MM-DD") from None
        if start and end and start > end:
            raise ValueError("开始日期不能晚于结束日期")
        root = self._root()
        if not root.exists():
            return {"items": [], "total": 0, "offset": offset, "hasMore": False, "periods": [], "skipped": 0}
        summaries, periods, skipped = [], set(), 0
        query = q.strip().casefold()
        for folder in root.iterdir():
            if not DAY_RE.fullmatch(folder.name) or not folder.is_dir():
                continue
            try:
                datetime.strptime(folder.name, "%Y%m%d")
            except ValueError:
                continue
            if not folder.resolve().is_relative_to(root):
                continue
            for path in folder.glob("*.json"):
                if not FILE_RE.fullmatch(path.name) or not path.resolve().is_relative_to(root):
                    continue
                try:
                    item = self._summary(record_id(folder.name, path.name), self._read(path))
                except (InvalidRecord, OSError):
                    skipped += 1
                    continue
                periods.update(item["periods"])
                day = date.fromisoformat(item["createdAt"][:10])
                if ((symbol and item["symbol"] != symbol)
                        or (query and query not in f"{item['symbol']} {item['label']}".casefold())
                        or (start and day < start) or (end and day > end)
                        or (tf and tf not in item["periods"]) or (status and item["status"] != status)):
                    continue
                summaries.append(item)
        summaries.sort(key=lambda item: (item["createdAt"], item["id"]), reverse=True)
        return {"items": summaries[offset:offset + limit], "total": len(summaries), "offset": offset,
                "hasMore": offset + limit < len(summaries), "periods": sorted(periods), "skipped": skipped}

    def _image(self, path: Path, chart: dict) -> Path:
        name = chart.get("image")
        if not isinstance(name, str) or not IMAGE_RE.fullmatch(name):
            raise FileNotFoundError("这张分析截图不存在")
        image = (path.parent / name).resolve()
        # 只提供此 JSON 声明、位于同一个日期目录的图片, 包括拒绝指向目录外的符号链接。
        if (image.parent != path.parent or not image.is_relative_to(self._root()) or not image.is_file()):
            raise FileNotFoundError("这张分析截图不存在")
        return image

    def detail(self, identity: str) -> dict:
        path = self._path(identity)
        record = self._read(path)
        charts = []
        for index, chart in enumerate(record["charts"]):
            try:
                self._image(path, chart)
                url = f"/api/analysis/history/{identity}/images/{index}"
            except OSError:
                url = None
            charts.append({"tf": chart.get("tf"), "imageUrl": url})
        return {**self._summary(identity, record), "answer": _text(record.get("answer")),
                "reasoning": _text(record.get("reasoning")), "model": _text(record.get("model")),
                "reasoningEffort": _text(record.get("reasoningEffort")), "usage": record.get("usage"),
                "elapsed": record.get("elapsed"), "error": _text(record.get("error")), "charts": charts}

    def image(self, identity: str, index: int) -> tuple[Path, str]:
        path = self._path(identity)
        record = self._read(path)
        if not 0 <= index < len(record["charts"]):
            raise FileNotFoundError("这张分析截图不存在")
        image = self._image(path, record["charts"][index])
        return image, IMAGE_TYPES[image.suffix.lower()]
