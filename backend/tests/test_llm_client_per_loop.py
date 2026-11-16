"""LLM / Embedding 异步客户端跨事件循环复用（2026-11-06 rc3 出题实测发现）。

一次出题会依次经过多个事件循环（生成前增强的每个 apply_transforms、人格、场景、样本各自 asyncio.run）。
langchain-openai 默认用模块级共享的 httpx.AsyncClient，连接池绑在第一个循环上，第二个循环起即报
`Event loop is closed` → openai 包成 `APIConnectionError: Connection error`（0.0s，请求没发出去）。
本地起 HTTP 服务复现，不连外部服务、0 token。
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def local_url():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive：连接会留在池里，才能复现跨循环复用

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_plain_shared_async_client_breaks_on_second_loop(local_url):
    """对照组：证明问题真实存在（httpx 共享客户端跨循环复用会失败）。"""
    import httpx

    client = httpx.AsyncClient(timeout=5)
    assert asyncio.run(client.get(local_url)).status_code == 200
    with pytest.raises(RuntimeError, match="Event loop is closed"):
        asyncio.run(client.get(local_url))


def test_per_loop_client_survives_many_loops(local_url):
    from app.ragas_adapter.clients import per_loop_async_http_client

    client = per_loop_async_http_client(5)
    for _ in range(4):  # 增强 → 人格 → 场景 → 样本
        assert asyncio.run(client.get(local_url)).status_code == 200

    async def concurrent():
        rs = await asyncio.gather(*(client.get(local_url) for _ in range(5)))
        return [r.status_code for r in rs]

    assert asyncio.run(concurrent()) == [200] * 5


def test_build_llm_and_embeddings_use_per_loop_client():
    from app.ragas_adapter import clients

    if not clients.RAGAS_AVAILABLE:
        pytest.skip("需要 ragas")
    preset = {"base_url": "http://127.0.0.1:9/v1", "model": "m"}
    llm = clients.build_llm(preset, timeout=30, sdk_max_retries=0)
    raw = llm.langchain_llm
    assert raw.http_async_client is not None, "不能退回 langchain-openai 的模块级共享客户端"
    assert type(raw.http_async_client._transport).__name__ == "_PerLoopTransport"

    emb = clients.build_embeddings(preset, timeout=30)
    inner = emb.client.openai_client._client  # openai.AsyncOpenAI 内部的 httpx 客户端（外面包了单条拆分层）
    assert type(inner._transport).__name__ == "_PerLoopTransport"
