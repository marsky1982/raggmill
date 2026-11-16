"""第三方兼容网关的请求约束，用本地仿网关复现并锁定：

1. Chat：请求体带 `n` 字段（n=1 也算）→ 400 ZodError；`stream=true` → 400（只发布非流式接口）；
2. Embedding：`input` 数组多于一个元素 → 400「supports one text per request」；
3. 推理强度：预设 extra.reasoning_effort 原样进请求体，留空不发送。

不连外部服务、0 token。测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import config, storage
    from app.main import app

    config.ensure_data_dirs()
    storage.init_storage()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def gateway():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, message: str):
            self._reply(400, {"error": {"type": "invalid_request_error", "code": "invalid_request",
                                        "message": message}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, **body})
            if self.path.endswith("/embeddings"):
                texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                if len(texts) > 1:
                    return self._error("This gateway text protocol supports one text per request; "
                                       "use a string or a singleton array.")
                return self._reply(200, {"object": "list", "model": body["model"],
                                         "data": [{"object": "embedding", "index": 0,
                                                   "embedding": [float(len(texts[0])), 1.0, 2.0]}],
                                         "usage": {"prompt_tokens": 3, "total_tokens": 3}})
            if "n" in body:
                return self._error("Invalid value for request.")
            if body.get("stream"):
                return self._error("Chat Completions streaming is not published by this unary-only API Profile.")
            return self._reply(200, {"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                                     "choices": [{"index": 0, "finish_reason": "stop",
                                                  "message": {"role": "assistant", "content": "好"}}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()


def _preset(base_url: str, kind: str = "llm", **extra) -> dict:
    return {"kind": kind, "base_url": base_url, "model": "fake", "api_key": "", "extra": extra}


def test_default_chat_request_passes_gateway_validation(gateway):
    """默认配置（非流式、不发 n）能通过网关校验；n>1 拆成多次调用也不带 n。"""
    from langchain_core.prompt_values import StringPromptValue

    from app.ragas_adapter.clients import build_llm

    url, seen = gateway
    seen.clear()
    llm = build_llm(_preset(url), timeout=5, sdk_max_retries=0)

    async def run(n):
        return await llm.agenerate_text(StringPromptValue(text="你好"), n=n)

    assert asyncio.run(run(1)).generations[0][0].text == "好"
    assert [g.text for g in asyncio.run(run(2)).generations[0]] == ["好", "好"]
    chats = [body for body in seen if body["path"].endswith("/chat/completions")]
    assert len(chats) == 3
    assert all("n" not in body and not body.get("stream") for body in chats)


def test_reasoning_effort_is_sent_only_when_configured(gateway):
    from langchain_core.prompt_values import StringPromptValue

    from app.ragas_adapter.clients import build_llm

    url, seen = gateway

    async def call(preset):
        llm = build_llm(preset, timeout=5, sdk_max_retries=0)
        await llm.agenerate_text(StringPromptValue(text="你好"))

    seen.clear()
    asyncio.run(call(_preset(url, reasoning_effort="minimal")))
    assert seen[-1]["reasoning_effort"] == "minimal"
    asyncio.run(call(_preset(url)))
    assert "reasoning_effort" not in seen[-1]
    asyncio.run(call(_preset(url, reasoning_effort="bogus")))  # 非法值不发送（接口层另有校验）
    assert "reasoning_effort" not in seen[-1]


def test_embedding_batch_falls_back_to_single_text_requests(gateway):
    """批量向量化被拒后自动拆成单条请求：结果按原顺序、用量逐条计入遥测，之后不再试批量。"""
    from app.ragas_adapter.clients import build_embeddings
    from app.ragas_adapter.telemetry import GenerationTelemetry

    url, seen = gateway
    seen.clear()
    tele = GenerationTelemetry(token_budget=0)
    emb = build_embeddings(_preset(url, kind="embedding"), timeout=5, telemetry=tele)
    texts = ["一", "二二", "三三三"]

    first = asyncio.run(emb.aembed_texts(texts))
    assert [v[0] for v in first] == [1.0, 2.0, 3.0]  # 按原顺序
    calls_after_first = len(seen)
    assert calls_after_first == 1 + len(texts)  # 1 次批量被拒 + 3 次单条

    second = asyncio.run(emb.aembed_texts(texts))
    assert [v[0] for v in second] == [1.0, 2.0, 3.0]
    assert len(seen) - calls_after_first == len(texts)  # 记住限制：不再先试批量
    assert tele.snapshot()["embedding_tokens"] == 3 * 3 * 2
    assert asyncio.run(emb.aembed_text("单条"))[0] == 2.0


def test_embedding_other_errors_are_not_swallowed(gateway):
    import openai

    from app.ragas_adapter.clients import SingleTextFallbackEndpoint

    async def boom(**kwargs):
        raise openai.BadRequestError("model not found", response=_fake_response(), body=None)

    endpoint = SingleTextFallbackEndpoint(boom)
    with pytest.raises(openai.BadRequestError, match="model not found"):
        asyncio.run(endpoint.create(input=["a", "b"], model="m"))
    assert endpoint.single_text_only is False


def _fake_response():
    import httpx

    return httpx.Response(400, request=httpx.Request("POST", "http://x/v1/embeddings"))


def test_reasoning_effort_options_match_frontend():
    """名字只定义一处：后端 storage.REASONING_EFFORTS 与配置页下拉选项一致。"""
    from app import storage

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "ConfigPage.tsx").read_text(
        encoding="utf-8")
    match = re.search(r"REASONING_EFFORT_OPTIONS = \[([^\]]*)\]", page)
    assert match, "ConfigPage.tsx 里找不到 REASONING_EFFORT_OPTIONS"
    assert tuple(re.findall(r'"([^"]+)"', match.group(1))) == storage.REASONING_EFFORTS


def test_preset_api_validates_reasoning_effort(client):
    from app import storage

    base = {"kind": "llm", "name": "推理强度校验", "base_url": "http://127.0.0.1:1/v1", "model": "m"}
    assert client.post("/api/presets", json=base | {"extra": {"reasoning_effort": "ultra"}}).status_code == 422
    created = client.post("/api/presets", json=base | {"extra": {"reasoning_effort": "minimal",
                                                                  "streaming": True}}).json()
    try:
        assert created["extra"] == {"reasoning_effort": "minimal", "streaming": True}
        updated = client.put(f"/api/presets/{created['id']}",
                             json=base | {"extra": {"reasoning_effort": "", "streaming": True}}).json()
        assert updated["extra"] == {"streaming": True}  # 留空 = 不发送，键被移除
    finally:
        storage.delete_preset(created["id"])
