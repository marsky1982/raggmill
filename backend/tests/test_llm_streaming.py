"""LLM 流式（SSE）为可选项：默认关闭（兼容网关只发布非流式接口），开启后链路与用量正确。

本地起一个仿 OpenAI 的 SSE 服务（先慢慢吐内容分片，最后一个分片带 usage），
经 ragas 包装层 → langchain-openai → 遥测回调走完整链路；不连外部服务、0 token。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PIECES = ["流式", "输出", "完成"]


@pytest.fixture(scope="module")
def sse_server():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "fake"}
            if not body.get("stream"):
                payload = json.dumps({**base, "object": "chat.completion", "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": "".join(PIECES)},
                    "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send(obj):
                data = f"data: {obj if isinstance(obj, str) else json.dumps(obj)}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            for i, piece in enumerate(PIECES):
                send({**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
                time.sleep(0.05)
            send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                send({**base, "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                                        "total_tokens": 18}})
            send("[DONE]")
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()


def _preset(base_url: str, **extra) -> dict:
    return {"kind": "llm", "base_url": base_url, "model": "fake", "api_key": "", "extra": extra}


def _call(llm, telemetry, n: int = 1):
    from langchain_core.prompt_values import StringPromptValue

    async def run():
        return await llm.agenerate_text(StringPromptValue(text="你好"), n=n, callbacks=[telemetry])

    return asyncio.run(run())


def test_streaming_is_off_by_default_and_preset_can_turn_it_on():
    """默认非流式：兼容网关 对 stream=true 一律 400（unary-only API Profile）。"""
    from app.ragas_adapter.clients import build_llm, llm_streaming_enabled

    off = build_llm(_preset("http://127.0.0.1:1/v1"))
    assert llm_streaming_enabled({}) is False
    assert off.langchain_llm.streaming is False
    assert off.bypass_n is True  # 恒不发送 n 字段（兼容网关 不接受，n>1 拆成多次调用）

    on = build_llm(_preset("http://127.0.0.1:1/v1", streaming=True))
    assert on.langchain_llm.streaming is True and on.langchain_llm.stream_usage is True
    assert on.bypass_n is True


def test_streaming_call_returns_text_and_usage_through_full_chain(sse_server):
    """完整链路：请求带 stream=true + include_usage；拼出完整文本；遥测与账单解析都拿到用量。"""
    from app.ragas_adapter.clients import build_llm
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.ragas_adapter.testset_generation import _token_usage_parser

    url, seen = sse_server
    seen.clear()
    tele = GenerationTelemetry(token_budget=0)
    llm = build_llm(_preset(url, streaming=True), timeout=5, sdk_max_retries=0)

    result = _call(llm, tele)

    assert seen[-1]["stream"] is True
    assert seen[-1]["stream_options"] == {"include_usage": True}
    assert result.generations[0][0].text == "".join(PIECES)
    snap = tele.snapshot()
    assert (snap["input_tokens"], snap["output_tokens"]) == (11, 7)
    assert snap.get("usage_missing_calls", 0) == 0
    usage = _token_usage_parser(result)
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)


def test_non_streaming_preset_still_reports_usage(sse_server):
    from app.ragas_adapter.clients import build_llm
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.ragas_adapter.testset_generation import _token_usage_parser

    url, seen = sse_server
    seen.clear()
    tele = GenerationTelemetry(token_budget=0)
    result = _call(build_llm(_preset(url), timeout=5, sdk_max_retries=0), tele)

    assert not seen[-1].get("stream") and "n" not in seen[-1]
    assert result.generations[0][0].text == "".join(PIECES)
    snap = tele.snapshot()
    assert (snap["input_tokens"], snap["output_tokens"]) == (11, 7)
    usage = _token_usage_parser(result)
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)


def test_streaming_with_n_greater_than_one_keeps_candidates_separate(sse_server):
    """评估指标会用 n>1：流式下拆成 n 次调用，每个候选文本完整、互不拼接，用量逐次累加。"""
    from app.ragas_adapter.clients import build_llm
    from app.ragas_adapter.telemetry import GenerationTelemetry

    url, seen = sse_server
    seen.clear()
    tele = GenerationTelemetry(token_budget=0)
    result = _call(build_llm(_preset(url, streaming=True), timeout=5, sdk_max_retries=0), tele, n=3)

    assert len(seen) == 3 and all(body.get("n") in (None, 1) for body in seen)
    assert [g.text for g in result.generations[0]] == ["".join(PIECES)] * 3
    snap = tele.snapshot()
    assert (snap["input_tokens"], snap["output_tokens"]) == (33, 21)
