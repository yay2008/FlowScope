"""AI 看图分析的离线回归: 请求校验、提示词、DeepSeek 流式接口、分析线程与存盘、HTTP 入口。不连 DeepSeek。"""
import asyncio
import base64
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import analysis
import app as server
from analysis import CST, AnalysisService, Busy, NotConfigured, UpstreamError

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
NOW = datetime(2026, 10, 8, 15, 30, 12, tzinfo=CST)
T0 = 1791446400          # 10-08 08:00:00(北京时间当 UTC 存)
SETTINGS = {"api_key": "sk-test", "model": "deepseek-flash", "base_url": "https://api.example.com",
            "reasoning_effort": "high"}


def data_url(raw=PNG, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def chart(tf=30, ltf=0, **overrides):
    fields = {
        "tf": tf, "image": data_url(),
        "columns": ["time", "open", "close", "buy", "ema21"],
        "rows": [[T0, 3000, 3001.0, None, 3000.25], [T0 + tf, 3001, 2999.5, 12, 3000.21]],
        "meta": {"view": "candle", "ltf": ltf, "shown": {"ema": True, "band": False},
                 "range": {"from": T0, "to": T0 + tf}, "loaded": {"count": 800, "from": T0 - 3600,
                                                                  "high": 3050, "low": 2950},
                 "coverage": {"complete": 1, "missing": 1}},
    }
    fields.update(overrides)
    return fields


def body(*charts, **overrides):
    fields = {"symbol": "KQ.m@SHFE.fu", "label": "燃油2611", "charts": list(charts) or [chart()],
              "settings": {"mode": "cvd", "threshtype": "Z-SCORE", "bandK": 2},
              "paper": [{"title": "空2 均价", "price": 3000}]}
    fields.update(overrides)
    return fields


def chunk(reasoning=None, content=None, finish=None, usage=None):
    delta = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    return {"choices": [{"delta": delta, "finish_reason": finish}], "usage": usage}


USAGE = {"prompt_tokens": 1200, "completion_tokens": 30, "prompt_cache_hit_tokens": 0}


def scripted(*chunks):
    """假的 DeepSeek: 按顺序吐出 chunk; 元素是异常时就地抛出。"""
    def transport(settings, payload, cancelled):
        transport.payload = payload
        for item in chunks:
            if isinstance(item, Exception):
                raise item
            yield item
    return transport


def collect(service, request=None):
    """在一个事件循环里启动分析并收完整个 SSE 流, 返回 [(event, data)]。"""
    async def run():
        job = service.start(request or body(), asyncio.get_running_loop())
        return [text async for text in service.events(job)]
    return [parse(text) for text in asyncio.run(run()) if not text.startswith(":")]


def parse(text):
    head, data = text.strip().split("\n")
    return head[len("event: "):], json.loads(data[len("data: "):])


def wait_idle(service):
    assert service._busy.acquire(timeout=5), "分析线程没有结束"
    service._busy.release()


class RequestTest(unittest.TestCase):
    def test_parse_valid_request(self):
        parsed = analysis.parse_request(body(chart(300), chart(30)))
        self.assertEqual(parsed["symbol"], "KQ.m@SHFE.fu")
        self.assertEqual([c["tf"] for c in parsed["charts"]], [300, 30])
        first = parsed["charts"][0]
        self.assertEqual(first["image"], PNG)
        self.assertEqual(first["mime"], "image/png")
        self.assertEqual(first["rows"][0], [T0, 3000, 3001.0, None, 3000.25])
        self.assertEqual(first["meta"]["coverage"], {"complete": 1, "missing": 1})
        self.assertEqual(parsed["paper"], [{"title": "空2 均价", "price": 3000}])
        # 不认识的模式回落到默认, 标签缺省时用代码
        loose = analysis.parse_request(body(label="", settings={"mode": "?"}))
        self.assertEqual(loose["label"], "KQ.m@SHFE.fu")
        self.assertEqual(loose["settings"]["mode"], "cvd")

    def test_reject_bad_requests(self):
        cases = {
            "一次分析 1~2 张图": body(chart(), chart(), chart()),
            "周期不对": body(chart(tf=20)),
            "截图格式不对": body(chart(image=data_url(mime="image/gif"))),
            "与声明的 image/png 不符": body(chart(image=data_url(b"\xff\xd8\xffjpeg"))),
            "不是合法的 base64": body(chart(image="data:image/png;base64,***")),
            "列名不对": body(chart(columns=["time", "close", "secret"], rows=[[T0, 1, 2]])),
            "行长度与列数不符": body(chart(rows=[[T0, 1]])),
            "只能是数字或空值": body(chart(rows=[[T0, 1, "x", None, 2]])),
            "买卖量口径不对": body(chart(tf=10, ltf=15)),   # 15s 拆不了 10s 的 bar
        }
        for message, request in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(ValueError, message):
                    analysis.parse_request(request)
        with self.assertRaisesRegex(ValueError, "周期不对"):
            analysis.parse_request(body(chart(tf=True)))
        for ltf in (None, True, 10.5, -1):
            with self.subTest(ltf=ltf), self.assertRaisesRegex(ValueError, "买卖量口径不对"):
                analysis.parse_request(body(chart(ltf=ltf)))
        with self.assertRaisesRegex(ValueError, "缺少合约代码"):
            analysis.parse_request(body(symbol=""))

    def test_messages_keep_images_in_user_message(self):
        request = analysis.parse_request(body(chart(300), chart(30)))
        system, user = analysis.build_messages(request, NOW)
        self.assertEqual(system["role"], "system")
        self.assertIsInstance(system["content"], str, "图片放进 system 会被 DeepSeek 拒绝(400)")
        kinds = [part["type"] for part in user["content"]]
        self.assertEqual(kinds, ["text", "text", "image_url", "text", "image_url"])
        self.assertEqual(user["content"][1]["text"], "图 1: 5m 周期截图")
        self.assertEqual(user["content"][2]["image_url"]["url"], data_url())

        text = user["content"][0]["text"]
        self.assertIn("合约: 燃油2611(KQ.m@SHFE.fu)", text)
        self.assertIn("分析时刻: 2026-10-08 15:30:12 北京时间", text)
        self.assertIn("模拟交易: 空2 均价 3000", text)
        self.assertIn("ema21 = EMA21", text)
        self.assertNotIn("band_up", text, "没发的列不解释")
        self.assertIn("周期从大到小", text)
        # 数值表: 时间按周期决定是否带秒, 整数值不带 .0, 空值留空
        self.assertIn("=== 图 1: 5m 周期 ===", text)
        self.assertIn("10-08 08:00,3000,3001,,3000.25", text)
        self.assertIn("10-08 08:00:00,3000,3001,,3000.25", text)
        self.assertIn("数值表里买卖量的覆盖: 完整 1 根、缺失 1 根", text)
        self.assertIn("主图叠加: EMA 开、FlowWave带 关\n", text)
        self.assertIn("FlowWave带 宽度: ±2σ", text)
        self.assertNotIn("WaveTrend", text + analysis.SYSTEM_PROMPT, "WaveTrend 已删除, 提示词里不能再提")
        self.assertIn("买卖量: tick 口径", text)

    def test_split_source_is_per_chart(self):
        # 工具栏选 K 线口径 15s 时, 30s 图实际用 15s, 10s 图回落到 10s: 每张图各写各的, 不能共用一句
        request = analysis.parse_request(body(chart(30, ltf=15), chart(10, ltf=10)))
        self.assertEqual([c["meta"]["ltf"] for c in request["charts"]], [15, 10])
        text = analysis.prompt_text(request, NOW)
        first, second = text.split("=== 图 2: 10s 周期 ===")
        self.assertIn("买卖量: TV K线口径(按 15s 小周期", first.split("=== 图 1: 30s 周期 ===")[1])
        self.assertIn("买卖量: TV K线口径(按 10s 小周期", second)
        self.assertNotIn("按 15s", second)
        self.assertNotIn("小周期", first.split("=== 图 1")[0], "开头的工具栏说明里不能有统一的拆分粒度")


class SettingsTest(unittest.TestCase):
    def test_load_settings(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(NotConfigured, "DEEPSEEK_API_KEY"):
                analysis.load_settings()
        env = {"DEEPSEEK_API_KEY": " sk-1 ", "DEEPSEEK_MODEL": "", "DEEPSEEK_BASE_URL": "https://x.test/",
               "DEEPSEEK_REASONING_EFFORT": "turbo"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(analysis.load_settings(), {"api_key": "sk-1", "model": "deepseek-flash",
                                                        "base_url": "https://x.test", "reasoning_effort": "high"})


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TransportTest(unittest.TestCase):
    def test_stream_parses_sse_lines(self):
        lines = (b": keep-alive\n\n"
                 b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x83\xb3"}}]}\n\n'
                 b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                 b"data: [DONE]\n\n"
                 b'data: {"after":"done"}\n\n')
        seen = {}

        def urlopen(request, timeout):
            seen["request"], seen["timeout"] = request, timeout
            return FakeResponse(lines)

        with patch.object(analysis.urllib.request, "urlopen", urlopen):
            chunks = list(analysis.deepseek_stream(SETTINGS, {"model": "m"}, threading.Event()))
        self.assertEqual([c["choices"][0]["delta"] for c in chunks], [{"reasoning_content": "想"}, {"content": "ok"}])
        request = seen["request"]
        self.assertEqual(request.full_url, "https://api.example.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test")
        self.assertEqual(json.loads(request.data), {"model": "m"})
        self.assertEqual(seen["timeout"], analysis.READ_TIMEOUT_SEC)

    def test_stream_stops_when_cancelled(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(analysis.urllib.request, "urlopen",
                          lambda request, timeout: FakeResponse(b'data: {"x":1}\n\n')):
            self.assertEqual(list(analysis.deepseek_stream(SETTINGS, {}, cancelled)), [])

    def test_http_errors_are_explained(self):
        def fail(code, payload):
            def urlopen(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(payload))
            return urlopen

        cases = [(401, b'{"error":{"message":"Authentication Fails"}}', "API key 无效.*Authentication Fails"),
                 (402, b'{"error":{"message":"Insufficient Balance"}}', "余额不足"),
                 (400, b"plain text", "拒绝了请求.*HTTP 400: plain text")]
        for code, payload, pattern in cases:
            with self.subTest(code=code), patch.object(analysis.urllib.request, "urlopen", fail(code, payload)):
                with self.assertRaisesRegex(UpstreamError, pattern):
                    list(analysis.deepseek_stream(SETTINGS, {}, threading.Event()))

        def offline(request, timeout):
            raise urllib.error.URLError("Name or service not known")

        with patch.object(analysis.urllib.request, "urlopen", offline):
            with self.assertRaisesRegex(UpstreamError, "连不上 DeepSeek.*Name or service not known"):
                list(analysis.deepseek_stream(SETTINGS, {}, threading.Event()))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "analysis")

    def service(self, transport):
        return AnalysisService(lambda: self.dir, transport=transport, settings=lambda: SETTINGS, clock=lambda: NOW)

    def record(self, saved):
        with open(os.path.join(self.tmp.name, saved), encoding="utf-8") as handle:
            return json.load(handle)

    def test_stream_and_save(self):
        transport = scripted(chunk(reasoning="先看"), chunk(reasoning="大周期"), chunk(content="【结论】"),
                             chunk(content="偏多", finish="stop"), chunk(usage=USAGE))
        events = collect(self.service(transport), body(chart(300), chart(30)))
        self.assertEqual([kind for kind, _ in events], ["meta", "reasoning", "reasoning", "delta", "delta", "done"])
        self.assertEqual(events[0][1], {"model": "deepseek-flash", "reasoningEffort": "high", "images": 2})
        done = events[-1][1]
        self.assertEqual((done["status"], done["finish"], done["usage"]), ("done", "stop", USAGE))
        self.assertEqual(done["saved"], "analysis/20261008/153012-KQ.m@SHFE.fu-5m+30s.json")

        payload = transport.payload
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["reasoning_effort"], "high")

        record = self.record(done["saved"])
        self.assertEqual((record["status"], record["answer"], record["reasoning"]), ("done", "【结论】偏多", "先看大周期"))
        self.assertEqual(record["createdAt"], "2026-10-08T15:30:12+08:00")
        self.assertEqual([c["image"] for c in record["charts"]],
                         ["153012-KQ.m@SHFE.fu-5m+30s-1.png", "153012-KQ.m@SHFE.fu-5m+30s-2.png"])
        self.assertIn("10-08 08:00,3000,3001,,3000.25", record["prompt"])
        with open(os.path.join(self.dir, "20261008", "153012-KQ.m@SHFE.fu-5m+30s-1.png"), "rb") as handle:
            self.assertEqual(handle.read(), PNG)

        # 同一秒再分析一次不覆盖上一份
        again = collect(self.service(scripted(chunk(content="又一次", finish="stop"))))
        self.assertEqual(again[-1][1]["saved"], "analysis/20261008/153012-KQ.m@SHFE.fu-30s.json")
        third = collect(self.service(scripted(chunk(content="第三次", finish="stop"))))
        self.assertEqual(third[-1][1]["saved"], "analysis/20261008/153012-KQ.m@SHFE.fu-30s-2.json")

    def test_upstream_error_before_output_is_not_saved(self):
        events = collect(self.service(scripted(UpstreamError("DeepSeek 账户余额不足, 请先充值(HTTP 402)"))))
        self.assertEqual([kind for kind, _ in events], ["meta", "error"])
        self.assertEqual(events[-1][1]["message"], "DeepSeek 账户余额不足, 请先充值(HTTP 402)")
        self.assertIsNone(events[-1][1]["saved"])
        self.assertFalse(os.path.exists(self.dir))

    def test_error_after_output_keeps_partial_answer(self):
        events = collect(self.service(scripted(chunk(content="半句"), UpstreamError("DeepSeek 输出中断: timed out"))))
        error = events[-1][1]
        self.assertEqual((events[-1][0], error["status"]), ("error", "error"))
        record = self.record(error["saved"])
        self.assertEqual((record["status"], record["answer"], record["error"]),
                         ("error", "半句", "DeepSeek 输出中断: timed out"))

    def test_unexpected_exception_still_reaches_page(self):
        events = collect(self.service(scripted(ValueError("bad json"))))
        self.assertEqual(events[-1], ("error", {"message": "分析出错: ValueError: bad json", "status": "error",
                                                "finish": None, "usage": None, "elapsed": events[-1][1]["elapsed"],
                                                "saved": None}))

    def test_one_analysis_at_a_time(self):
        gate = threading.Event()

        def slow(settings, payload, cancelled):
            gate.wait(5)
            yield chunk(content="好了", finish="stop")

        service = self.service(slow)

        async def run():
            loop = asyncio.get_running_loop()
            job = service.start(body(), loop)
            with self.assertRaisesRegex(Busy, "上一次分析还没结束"):
                service.start(body(), loop)
            gate.set()
            return [text async for text in service.events(job)]

        asyncio.run(run())
        wait_idle(service)
        self.assertEqual(collect(service)[-1][0], "done", "结束后可以再分析")

    def test_page_disconnect_stops_and_saves_partial(self):
        more = threading.Event()

        def endless(settings, payload, cancelled):
            yield chunk(content="第一段")
            more.wait(5)       # 页面断开后才产出下一段, 分析线程看到 cancelled 就收手
            for _ in range(1000):
                yield chunk(content="……")

        service = self.service(endless)

        async def run():
            job = service.start(body(), asyncio.get_running_loop())
            stream = service.events(job)
            async for text in stream:
                if text.startswith("event: delta"):
                    break
            await stream.aclose()     # 页面点了停止 / 关了浮层: 生成器被关掉
            more.set()

        asyncio.run(run())
        wait_idle(service)
        saved = os.listdir(os.path.join(self.dir, "20261008"))
        name = next(name for name in saved if name.endswith(".json"))
        record = self.record(os.path.join("analysis", "20261008", name))
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["answer"].startswith("第一段"))
        self.assertLess(len(record["answer"]), 3 + 2 * 1000)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = server.analysis
        self.addCleanup(setattr, server, "analysis", self.original)

    def use(self, transport, settings=lambda: SETTINGS):
        server.analysis = AnalysisService(lambda: os.path.join(self.tmp.name, "analysis"), transport=transport,
                                          settings=settings, clock=lambda: NOW)

    def call(self, request):
        async def run():
            response = await server.analyze(request)
            self.assertIsInstance(response, StreamingResponse)
            self.assertEqual(response.media_type, "text/event-stream")
            return [text async for text in response.body_iterator]
        return asyncio.run(run())

    def test_streams_events(self):
        self.use(scripted(chunk(content="偏空", finish="stop"), chunk(usage=USAGE)))
        texts = self.call(body())
        self.assertEqual([parse(text)[0] for text in texts], ["meta", "delta", "done"])

    def test_error_statuses(self):
        self.use(scripted())
        with self.assertRaises(HTTPException) as caught:
            self.call(body(symbol="../etc"))
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            self.call(body(charts=[]))
        self.assertEqual(caught.exception.status_code, 400)

        def missing():
            raise NotConfigured("没有配置 DeepSeek")

        self.use(scripted(), settings=missing)
        with self.assertRaises(HTTPException) as caught:
            self.call(body())
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (503, "没有配置 DeepSeek"))

        self.use(scripted())
        server.analysis._busy.acquire()
        try:
            with self.assertRaises(HTTPException) as caught:
                self.call(body())
            self.assertEqual(caught.exception.status_code, 429)
        finally:
            server.analysis._busy.release()


if __name__ == "__main__":
    unittest.main()
