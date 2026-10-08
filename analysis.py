# -*- coding: utf-8 -*-
"""AI 看图分析: 页面截图 + 同一段数值摘要 -> DeepSeek 看图模型 -> 流式回给页面, 结果存盘留档。

- 模型默认 deepseek-flash: DeepSeek 官方 API 里收图的是它(2026-10 查的文档; deepseek-chat 等旧名已退役)。
  走 OpenAI 兼容的 /chat/completions, 图片是 base64 data URL 的 image_url 块, 只能放在 user 消息里(放 system 回 400)。
- 为什么图和数一起送: 每张图会被缩到约 1300×1300 像素、最多 1024 token, 模型从图上看得出形态, 读不准价格和
  指标数值。所以价格、指标以页面传来的数值表为准, 图只用来看结构; 图怎么读(K 线配色、自研指标)写在
  SYSTEM_PROMPT 里 —— 这些指标模型本来不认识。数值表的列由页面算好(指标都在前端算), 列的含义由这里解释。
- 配置在 .env: DEEPSEEK_API_KEY(必填)、DEEPSEEK_MODEL、DEEPSEEK_BASE_URL、DEEPSEEK_REASONING_EFFORT。
  key 只在服务端, 不下发页面。
- 一次只跑一个分析(第二个请求 429)。上游调用在独立线程里跑, 页面断开(点停止、关浮层、关页面)就不再读上游,
  已经出来的部分照样存盘。
- 存盘: data/analysis/YYYYmmdd/HHMMSS-合约-周期.json, 截图同名加 -1.png / -2.png(时间按北京时间, 同图表),
  随 data/ 快照一起备份。有结论文字或正常结束才存; 一开始就失败(key 无效、余额不足)不留记录。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from indicator import TF_OPTIONS, ltf_options

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
# 思考强度(API 的 reasoning_effort): none 关掉思考, 其余开; API 默认 high
REASONING_EFFORTS = ("none", "low", "high", "max")
DEFAULT_EFFORT = "high"
MAX_TOKENS = 32768            # 思考 + 结论的总上限; 开思考时 API 默认 64K, 看一张图用不了那么多
READ_TIMEOUT_SEC = 120        # 连接与每次读的超时; 流式输出中途停顿超过它就算中断
HEARTBEAT_SEC = 10            # 等上游时隔这么久给页面发一次 SSE 注释, 页面断开能及时发现
USER_AGENT = "FlowScope/1.0"
CST = timezone(timedelta(hours=8))   # 记录时间按北京时间, 与图表时间轴同口径

MAX_CHARTS = 2
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 300
MAX_TEXT = 200
IMAGE_TYPES = {"image/png": (".png", b"\x89PNG\r\n\x1a\n"), "image/jpeg": (".jpg", b"\xff\xd8\xff"),
               "image/webp": (".webp", b"RIFF")}
DATA_URL_RE = re.compile(r"data:(image/[a-z]+);base64,(.+)", re.S)

# 数值表允许的列与含义(只把出现的列写进提示词)。列名由页面(static/ai-panel.js 的 COLUMNS)决定, 两边要一致
COLUMN_NOTES = {
    "time": "bar 起始时刻(北京时间)",
    "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
    "volume": "成交量",
    "buy": "主动买量(空 = 这根没有买卖量数据)", "sell": "主动卖量",
    "delta": "主动买量 - 主动卖量",
    "cvd": "累计 delta(CVD)",
    "rvol": "相对成交量 = 成交量 / 近 20 根均量, 大于 1 为放量",
    "ema21": "EMA21", "ema55": "EMA55", "ema100": "EMA100", "ema200": "EMA200",
    "fw": "FlowWave 主线 LSMA(0~100, 量价合成振荡; 图上没有单独窗格)",
    "fw_sig": "FlowWave 信号 wt2(大于 80 超买、小于 20 超卖; 主图 FlowWave带 按它染色)",
    "crv_slope": "CRVOL 斜率(带符号相对量累计的回归斜率, 正 = 量能偏多)",
    "band_up": "FlowWave带 上轨", "band_mid": "FlowWave带 中线(收盘价 21 根线性回归)",
    "band_dn": "FlowWave带 下轨",
}

MODE_LABELS = {"rvol": "Relative Volume(相对成交量柱)", "crvol": "CRVOL(带符号相对量的累计, 蜡烛)",
               "volume": "成交量柱", "bsv": "买卖量柱(买在 0 轴上方、卖在下方)", "delta": "Delta 柱",
               "cvd": "CVD(累计 delta, 蜡烛)"}

SYSTEM_PROMPT = """你是期货与加密永续合约的盘面分析助手。用户发来 FlowScope 的图表截图, 以及同一段行情的数值表。

【图怎么读】
每张图从上到下两个窗格: 主图(K 线)、FlowMeter(成交量类指标)。图片顶部一行写着合约、周期、视图和截止时刻。
主图:
- K 线涨为灰白色、跌为深灰色(不是红绿)。
- EMA21 黄、EMA55 蓝、EMA100 青绿、EMA200 紫。
- FlowWave带(若显示): 收盘价 21 根线性回归通道, 两条淡线是上下轨; 中线到上轨染红、上轨加粗 = FlowWave 超买(fw_sig 大于 80), 中线到下轨染绿、下轨加粗 = 超卖(fw_sig 小于 20)。
- 模拟交易(若有): 带「买N」字样的红色上箭头、「卖N」字样的绿色下箭头是成交, 水平实线是持仓均价, 虚线是挂单。
- 半透明矩形加读数框(若有)是用户手工测量的区间, 读数是涨跌、K 线根数和时长。
- 足迹图视图(顶部写「足迹图」时): 每根 bar 按价位分格, 左半格卖、右半格买, 暖色越深量越大; 白框是成交最多的价位(POC), 绿框 / 红框是买方 / 卖方对角失衡(3:1 以上); 紫色是判不出方向的量。
FlowMeter: 按数据说明里的模式画柱或蜡烛。灰色是普通量, 偏买 / 上涨方向用绿色系、偏卖 / 下跌方向用红色系, 颜色越深表示超过第 1/2/3 档阈值越多。窗格底部的细柱是 RVOL 脉冲(相对成交量), 亮绿 / 亮红是第 3 档放量。

【数据怎么用】
- 数值表是图中最右边一段 bar 的原始数据, 价格、指标以表为准, 不要从图上估读数字; 图用来看形态、结构和全局位置。
- 空值表示没有数据(指标预热期、买卖量缺失), 不是 0。
- 期货的买卖量是按 tick 快照估算的主动方向, 加密是交易所逐笔成交自带的主动方向; 覆盖不完整时 CVD 与 delta 要打折看待。

【输出】
用中文, 简洁、具体, 直接给结论, 不要寒暄。不用 Markdown 的 #、表格和加粗符号, 用下面的【】小标题分段, 段内用「- 」列点:
【结论】一句话: 偏多 / 偏空 / 震荡, 以及把握程度(高 / 中 / 低)。
【结构与趋势】EMA 排列、高低点结构、价格在 FlowWave带 里的位置。
【量价与资金流】CVD、delta 与价格是否同步, 放量出现在哪里, 有没有背离。
【关键价位】支撑、压力各一到三个, 写价格和依据。
【情景】多、空各一条: 触发条件、目标、失效价位。
【风险】数据缺失、周期太短、临近休市、信号互相矛盾等。
有多张图时先看大周期定方向, 再用小周期找位置。看不清或数据不足就直说, 不要编造。只做技术面分析, 不构成投资建议。"""


class NotConfigured(Exception):
    """没有配置 DEEPSEEK_API_KEY。"""


class Busy(Exception):
    """上一次分析还没结束。"""


class UpstreamError(Exception):
    """调 DeepSeek 失败; 消息直接显示在页面上。"""


def load_settings() -> dict:
    """从环境变量(.env 已在 ingest 导入时读进来)取 DeepSeek 配置; 每次分析现取, 改 .env 后重启服务生效。"""
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise NotConfigured("没有配置 DeepSeek: 在 .env 里加一行 DEEPSEEK_API_KEY=sk-...(在 platform.deepseek.com 创建), "
                            "然后重启服务")
    effort = (os.getenv("DEEPSEEK_REASONING_EFFORT") or DEFAULT_EFFORT).strip().lower()
    return {
        "api_key": key,
        "model": (os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip(),
        "base_url": (os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "reasoning_effort": effort if effort in REASONING_EFFORTS else DEFAULT_EFFORT,
    }


# ---------- 请求校验 ----------
# 页面传来的东西要原样进提示词和文件, 所以逐项限定类型与长度; 不认识的字段直接丢掉。

def _text(value, limit=MAX_TEXT) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _number(value):
    """有限的数(不收 bool); 其余返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _counts(value, keys) -> dict:
    value = value if isinstance(value, dict) else {}
    return {key: int(value[key]) for key in keys if _number(value.get(key)) is not None}


def parse_image(value, index) -> tuple[str, bytes]:
    """data URL -> (MIME 类型, 图片字节); 格式按文件头核对, 存盘的扩展名才可信。"""
    match = DATA_URL_RE.fullmatch(value) if isinstance(value, str) else None
    if not match or match.group(1) not in IMAGE_TYPES:
        raise ValueError(f"图 {index} 的截图格式不对(要 PNG / JPEG / WebP 的 data URL)")
    if len(match.group(2)) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ValueError(f"图 {index} 的截图超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"图 {index} 的截图不是合法的 base64") from None
    mime = match.group(1)
    if not data.startswith(IMAGE_TYPES[mime][1]):
        raise ValueError(f"图 {index} 的截图内容与声明的 {mime} 不符")
    return mime, data


def parse_chart(chart, index) -> dict:
    if not isinstance(chart, dict):
        raise ValueError(f"图 {index} 的数据格式不对")
    tf = chart.get("tf")
    if isinstance(tf, bool) or not isinstance(tf, int) or tf not in TF_OPTIONS:
        raise ValueError(f"图 {index} 的周期不对")
    mime, image = parse_image(chart.get("image"), index)
    columns = chart.get("columns")
    if (not isinstance(columns, list) or not columns or columns[0] != "time"
            or len(set(columns)) != len(columns) or any(column not in COLUMN_NOTES for column in columns)):
        raise ValueError(f"图 {index} 的数值表列名不对")
    rows = chart.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise ValueError(f"图 {index} 的数值表要有 1~{MAX_ROWS} 行")
    clean_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError(f"图 {index} 的数值表行长度与列数不符")
        values = [_number(value) for value in row]
        if values[0] is None or any(value is None and raw is not None for value, raw in zip(values, row)):
            raise ValueError(f"图 {index} 的数值表只能是数字或空值")
        clean_rows.append(values)
    meta = chart.get("meta") if isinstance(chart.get("meta"), dict) else {}
    ltf = meta.get("ltf")   # 本图买卖量实际的拆分粒度(各图可能不同): 0 = tick 口径, 正数 = 小周期 K 线秒数
    if isinstance(ltf, bool) or not isinstance(ltf, int) or ltf not in ltf_options(tf):
        raise ValueError(f"图 {index} 的买卖量口径不对")
    shown = meta.get("shown") if isinstance(meta.get("shown"), dict) else {}
    span = meta.get("range") if isinstance(meta.get("range"), dict) else {}
    loaded = meta.get("loaded") if isinstance(meta.get("loaded"), dict) else {}
    return {
        "tf": tf, "mime": mime, "image": image, "columns": list(columns), "rows": clean_rows,
        "meta": {
            "view": "footprint" if meta.get("view") == "footprint" else "candle",
            "ltf": ltf,
            "shown": {key: shown[key] for key in ("ema", "band") if isinstance(shown.get(key), bool)},
            "range": {key: _number(span.get(key)) for key in ("from", "to")},
            "loaded": {key: _number(loaded.get(key)) for key in ("count", "from", "high", "low")},
            "coverage": _counts(meta.get("coverage"), ("complete", "partial", "missing", "legacy")),
        },
    }


def parse_request(body) -> dict:
    """页面的请求 -> 校验过的分析请求; 不合法抛 ValueError(消息给页面看)。symbol 由调用方另行按订阅规则校验。"""
    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    symbol = _text(body.get("symbol"), 64)
    if not symbol:
        raise ValueError("缺少合约代码")
    charts = body.get("charts")
    if not isinstance(charts, list) or not 1 <= len(charts) <= MAX_CHARTS:
        raise ValueError(f"一次分析 1~{MAX_CHARTS} 张图")
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    paper = body.get("paper") if isinstance(body.get("paper"), list) else []
    return {
        "symbol": symbol,
        "label": _text(body.get("label"), 64) or symbol,
        "charts": [parse_chart(chart, index) for index, chart in enumerate(charts, 1)],
        "settings": {
            "mode": settings.get("mode") if settings.get("mode") in MODE_LABELS else "cvd",
            "threshtype": _text(settings.get("threshtype"), 16),
            "bandK": _number(settings.get("bandK")),
        },
        "paper": [{"title": _text(line.get("title"), 40), "price": _number(line.get("price"))}
                  for line in paper[:10] if isinstance(line, dict)],
    }


# ---------- 提示词 ----------

def tf_label(tf: int) -> str:
    if tf % 3600 == 0:
        return f"{tf // 3600}h"
    if tf % 60 == 0:
        return f"{tf // 60}m"
    return f"{tf}s"


def bar_time(seconds, with_seconds=True) -> str:
    """图表时间戳(北京时间当 UTC 存, 见 indicator.TZ_SHIFT_S) -> 「MM-DD HH:MM[:SS]」。"""
    if seconds is None:
        return "-"
    stamp = datetime.fromtimestamp(seconds, timezone.utc)
    return stamp.strftime("%m-%d %H:%M:%S" if with_seconds else "%m-%d %H:%M")


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def split_text(ltf: int) -> str:
    """买卖量口径的说明; ltf 同 /api/history: 0 = tick, 正数 = 按这么多秒的小周期 K 线归类。"""
    if ltf:
        return f"TV K线口径(按 {ltf}s 小周期 K 线涨跌归类成交量)"
    return "tick 口径(期货: TqSdk 快照估算主动方向; 加密: 交易所逐笔主动方向)"


def chart_text(chart, index) -> str:
    meta, tf = chart["meta"], chart["tf"]
    with_seconds = tf < 60
    on_off = lambda key: {True: "开", False: "关"}.get(meta["shown"].get(key), "-")
    lines = [f"=== 图 {index}: {tf_label(tf)} 周期 ===",
             f"视图: {'足迹图' if meta['view'] == 'footprint' else 'K 线'}; 主图叠加: EMA {on_off('ema')}、"
             f"FlowWave带 {on_off('band')}",
             f"买卖量: {split_text(meta['ltf'])}",
             f"截图可视范围: {bar_time(meta['range']['from'], with_seconds)} → "
             f"{bar_time(meta['range']['to'], with_seconds)}"]
    loaded = meta["loaded"]
    if loaded["count"]:
        lines.append(f"页面已加载 {cell(loaded['count'])} 根, 从 {bar_time(loaded['from'], with_seconds)} 起, "
                     f"其间最高 {cell(loaded['high'])}、最低 {cell(loaded['low'])}")
    coverage = meta["coverage"]
    if coverage:
        names = {"complete": "完整", "partial": "部分", "missing": "缺失", "legacy": "旧历史"}
        lines.append("数值表里买卖量的覆盖: " + "、".join(f"{names[key]} {count} 根" for key, count in coverage.items()))
    columns = chart["columns"]
    lines.append(f"数值表(最右 {len(chart['rows'])} 根, 空 = 无数据):")
    lines.append(",".join(columns))
    for row in chart["rows"]:
        lines.append(",".join(bar_time(value, with_seconds) if column == "time" else cell(value)
                              for column, value in zip(columns, row)))
    return "\n".join(lines)


def prompt_text(request, now: datetime) -> str:
    """user 消息里的文字部分: 合约与工具栏状态、列含义, 再逐张图给出买卖量口径、范围和数值表。"""
    settings = request["settings"]
    paper = "; ".join(f"{line['title']} {cell(line['price'])}" for line in request["paper"] if line["title"])
    columns = list(dict.fromkeys(column for chart in request["charts"] for column in chart["columns"]))
    charts = request["charts"]
    lines = [
        f"合约: {request['label']}({request['symbol']})",
        f"分析时刻: {now.strftime('%Y-%m-%d %H:%M:%S')} 北京时间",
        f"FlowMeter 模式: {MODE_LABELS[settings['mode']]}; 阈值算法: {settings['threshtype'] or '-'}; "
        "买卖量口径见各图",
        f"FlowWave带 宽度: ±{cell(settings['bandK'])}σ",
        f"模拟交易: {paper or '当前合约没有持仓和挂单'}",
        "",
        "数值表各列: " + "; ".join(f"{column} = {COLUMN_NOTES[column]}" for column in columns),
        "",
        f"共 {len(charts)} 张图" + (", 周期从大到小" if len(charts) > 1 else "") + ", 截图按同样顺序附在后面。",
    ]
    for index, chart in enumerate(charts, 1):
        lines += ["", chart_text(chart, index)]
    return "\n".join(lines)


def build_messages(request, now: datetime) -> list[dict]:
    """system 只放文字(放图会 400); user 先放文字和数值表, 再逐张放截图, 每张前面标一句是哪张图。"""
    content = [{"type": "text", "text": prompt_text(request, now)}]
    for index, chart in enumerate(request["charts"], 1):
        content.append({"type": "text", "text": f"图 {index}: {tf_label(chart['tf'])} 周期截图"})
        url = f"data:{chart['mime']};base64,{base64.b64encode(chart['image']).decode('ascii')}"
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]


# ---------- 调 DeepSeek ----------

def explain_http_error(code: int, body: bytes) -> str:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = body.decode("utf-8", "replace").strip()
    hint = {401: "API key 无效, 检查 .env 里的 DEEPSEEK_API_KEY", 402: "DeepSeek 账户余额不足, 请先充值",
            429: "请求太频繁, 稍后再试", 500: "DeepSeek 服务出错, 稍后再试",
            503: "DeepSeek 服务繁忙, 稍后再试"}.get(code, "DeepSeek 拒绝了请求")
    message = str(message)[:300]
    return f"{hint}(HTTP {code}{': ' + message if message else ''})"


def deepseek_stream(settings: dict, payload: dict, cancelled: threading.Event):
    """POST /chat/completions(stream), 逐个产出解析好的 chunk; 失败抛 UpstreamError。

    标准库 urllib(认 HTTPS_PROXY 等环境变量), 只在分析线程里用。cancelled 置位后读到下一行就收手。
    """
    request = urllib.request.Request(
        settings["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {settings['api_key']}"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=READ_TIMEOUT_SEC)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(explain_http_error(exc.code, exc.read()[:2000])) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UpstreamError(f"连不上 DeepSeek({settings['base_url']}): {reason}") from None
    with response:
        try:
            for raw in response:
                if cancelled.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):   # 空行、": keep-alive" 之类的注释
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                yield json.loads(data)
        except (TimeoutError, OSError) as exc:
            raise UpstreamError(f"DeepSeek 输出中断: {exc}") from None


# ---------- 分析任务 ----------

def sse(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Job:
    """一次分析: 分析线程往 queue 里放事件(经事件循环线程安全地放), HTTP 响应从里面取。"""

    def __init__(self, request: dict, settings: dict, loop: asyncio.AbstractEventLoop, now: datetime):
        self.request = request
        self.settings = settings
        self.loop = loop
        self.created = now
        self.queue: asyncio.Queue = asyncio.Queue()
        self.cancelled = threading.Event()

    def emit(self, kind: str, data=None):
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (kind, data))
        except RuntimeError:   # 事件循环已经关了(服务在退出): 没人收了
            pass


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", text)


class AnalysisService:
    """HTTP 层只跟这个类打交道: start() 校验并启动分析线程, events() 把事件转成 SSE。

    transport / settings 可替换: 测试与离线预览不连 DeepSeek。
    """

    def __init__(self, dir_provider, transport=deepseek_stream, settings=load_settings, clock=None):
        self._dir_provider = dir_provider
        self.transport = transport
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(CST))
        self._busy = threading.Lock()

    def start(self, body, loop: asyncio.AbstractEventLoop) -> Job:
        request = parse_request(body)
        settings = self.settings()
        if not self._busy.acquire(blocking=False):
            raise Busy("上一次分析还没结束")
        try:
            job = Job(request, settings, loop, self._clock())
            threading.Thread(target=self._run, args=(job,), name="analysis", daemon=True).start()
        except BaseException:
            self._busy.release()
            raise
        return job

    async def events(self, job: Job):
        """SSE: meta → reasoning / delta …… → done 或 error。生成器被关掉(页面断开)时通知分析线程收手。"""
        try:
            while True:
                try:
                    kind, data = await asyncio.wait_for(job.queue.get(), HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "end":
                    return
                yield sse(kind, data)
        finally:
            job.cancelled.set()

    def _run(self, job: Job):
        started = time.monotonic()
        answer, reasoning = [], []
        usage = finish = error = None
        try:
            settings = job.settings
            payload = {"model": settings["model"], "messages": build_messages(job.request, job.created),
                       "stream": True, "stream_options": {"include_usage": True}, "max_tokens": MAX_TOKENS,
                       "reasoning_effort": settings["reasoning_effort"]}
            job.emit("meta", {"model": settings["model"], "reasoningEffort": settings["reasoning_effort"],
                              "images": len(job.request["charts"])})
            for chunk in self.transport(settings, payload, job.cancelled):
                if job.cancelled.is_set():
                    break
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                        job.emit("reasoning", {"text": delta["reasoning_content"]})
                    if delta.get("content"):
                        answer.append(delta["content"])
                        job.emit("delta", {"text": delta["content"]})
                    finish = choice.get("finish_reason") or finish
        except UpstreamError as exc:
            error = str(exc)
        except Exception as exc:   # 解析失败之类的意外也要告诉页面, 不能让它干等
            error = f"分析出错: {type(exc).__name__}: {exc}"
        try:
            status = "stopped" if job.cancelled.is_set() else "error" if error else "done"
            result = {"status": status, "finish": finish, "usage": usage,
                      "elapsed": round(time.monotonic() - started, 1), "saved": None}
            if answer or status == "done":
                try:
                    result["saved"] = self._save(job, result, "".join(answer), "".join(reasoning), error)
                except OSError as exc:
                    print(f"[analysis] 分析结果存盘失败: {exc}", flush=True)
                    result["saveError"] = str(exc)
            if error:
                job.emit("error", {"message": error, **result})
            else:
                job.emit("done", result)
            job.emit("end")
        finally:
            self._busy.release()

    def _save(self, job: Job, result: dict, answer: str, reasoning: str, error) -> str:
        """一次分析一个 JSON(连同发出去的提示词) + 每张截图一个文件; 返回相对数据目录的路径, 页面上显示。"""
        request, now = job.request, job.created
        day = now.strftime("%Y%m%d")
        folder = os.path.join(self._dir_provider(), day)
        os.makedirs(folder, exist_ok=True)
        tfs = "+".join(tf_label(chart["tf"]) for chart in request["charts"])
        base = f"{now.strftime('%H%M%S')}-{safe_name(request['symbol'])}-{tfs}"
        stem, serial = base, 1
        while os.path.exists(os.path.join(folder, stem + ".json")):   # 同一秒又分析了一次
            serial += 1
            stem = f"{base}-{serial}"
        charts = []
        for index, chart in enumerate(request["charts"], 1):
            name = f"{stem}-{index}{IMAGE_TYPES[chart['mime']][0]}"
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(chart["image"])
            charts.append({"tf": chart["tf"], "image": name, "meta": chart["meta"]})
        record = {
            "createdAt": now.isoformat(timespec="seconds"),
            "symbol": request["symbol"], "label": request["label"],
            "model": job.settings["model"], "reasoningEffort": job.settings["reasoning_effort"],
            **result, "error": error,
            "settings": request["settings"], "paper": request["paper"], "charts": charts,
            "system": SYSTEM_PROMPT, "prompt": prompt_text(request, now),
            "reasoning": reasoning, "answer": answer,
        }
        record.pop("saved", None)
        path = os.path.join(folder, stem + ".json")
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
        return f"analysis/{day}/{stem}.json"
