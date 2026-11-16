"""思考模式开关（`enable_thinking`）的回归，2026-11-14：

DashScope 的 `某推理模型` 默认开思考（实测 41/100 token 里 86 是思考 token、1.9 秒），
关掉后 15/13、0.9 秒；出题时输出被放大到输入的近 3 倍。约定：

1. 只有 DashScope 端点发送 `enable_thinking`，默认 `false`（关），可用 `extra.enable_thinking` 开回来；
2. 非 DashScope（兼容网关等）**一律不发**该字段——发了会 400，包含用户显式配了 true 的情况；
3. 接口层只接受布尔值（`true` / `false`），非法值 400。

不连外部服务、0 token。测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
def echo_server():
    """本地回环假端点：记录收到的请求体，按 OpenAI Chat Completions 形状回一条。"""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, **body})
            payload = json.dumps({
                "id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "好"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()


def _preset(base_url: str, **extra) -> dict:
    return {"kind": "llm", "base_url": base_url, "model": "fake", "api_key": "", "extra": extra}


def _call(preset: dict) -> None:
    from langchain_core.prompt_values import StringPromptValue

    from app.ragas_adapter.clients import build_llm

    async def go():
        llm = build_llm(preset, timeout=5, sdk_max_retries=0)
        await llm.agenerate_text(StringPromptValue(text="你好"))

    asyncio.run(go())


def test_is_dashscope_endpoint_matches_only_dashscope_hosts():
    from app.ragas_adapter.clients import is_dashscope_endpoint

    assert is_dashscope_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert is_dashscope_endpoint("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    # 预设里允许省略协议头
    assert is_dashscope_endpoint("dashscope.aliyuncs.com/compatible-mode/v1")
    # 同理只认完整子域，别把相似域名当前缀
    assert not is_dashscope_endpoint("https://notdashscope.aliyuncs.com/v1")
    assert not is_dashscope_endpoint("https://gw.example.com/v1")
    assert not is_dashscope_endpoint("")
    assert not is_dashscope_endpoint(None)


def test_resolve_enable_thinking_three_states():
    from app.ragas_adapter.clients import resolve_enable_thinking

    dashscope = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert resolve_enable_thinking(_preset(dashscope)) is False  # 默认关
    assert resolve_enable_thinking(_preset(dashscope, enable_thinking=True)) is True
    assert resolve_enable_thinking(_preset(dashscope, enable_thinking=False)) is False
    assert resolve_enable_thinking(_preset(dashscope, enable_thinking="false")) is False  # 非布尔按默认
    # 非 DashScope：即使显式要求开，也不发送（None）
    assert resolve_enable_thinking(_preset("https://gw.example.com/v1", enable_thinking=True)) is None


def test_dashscope_request_body_carries_enable_thinking(monkeypatch, echo_server):
    """DashScope 端点：默认 false，显式 true 则 true。用本地回环端点 + 打桩主机判定，避免连真实服务。"""
    from app.ragas_adapter import clients

    url, seen = echo_server
    monkeypatch.setattr(clients, "is_dashscope_endpoint", lambda base_url: True)

    seen.clear()
    _call(_preset(url))
    assert seen[-1]["enable_thinking"] is False

    _call(_preset(url, enable_thinking=True))
    assert seen[-1]["enable_thinking"] is True

    _call(_preset(url, enable_thinking=False))
    assert seen[-1]["enable_thinking"] is False


def test_non_dashscope_request_body_never_carries_enable_thinking(echo_server):
    """兼容网关 等非 DashScope 端点：请求体里不能出现该字段（发了会 400）。"""
    url, seen = echo_server

    seen.clear()
    _call(_preset(url))
    _call(_preset(url, enable_thinking=True))
    assert len(seen) == 2
    assert all("enable_thinking" not in body for body in seen)


def test_preset_route_rejects_non_boolean_enable_thinking(client):
    base = {"kind": "llm", "provider": "openai_compatible", "base_url": "https://gw.example.com/v1", "model": "m"}

    r = client.post("/api/presets", json={**base, "name": "思考模式-非法值", "extra": {"enable_thinking": "yes"}})
    assert r.status_code == 422  # pydantic 校验失败由 FastAPI 统一返回 422
    assert "enable_thinking" in r.text

    r = client.post("/api/presets", json={**base, "name": "思考模式-布尔值", "extra": {"enable_thinking": False}})
    assert r.status_code == 200
    assert r.json()["extra"]["enable_thinking"] is False
