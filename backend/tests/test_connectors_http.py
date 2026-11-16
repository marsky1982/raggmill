"""连接器层 HTTP 基座与 RAGFlow 客户端接入的回归（架构演进阶段 4-③）。

覆盖验收口径：协议白名单、只跟随同源重定向（跨源不带认证头）、有限重试
（只对幂等 GET 的连接错误/超时/5xx/429 生效；4xx 与业务错误码不重试）、超时透传。
不连真实网络：用 `httpx.MockTransport` 注入，退避用空 `sleep`。
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

import httpx
import pytest


def _transport(handler, **kwargs):
    from app.connectors.http import HttpTransport

    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpTransport(transport=httpx.MockTransport(handler), **kwargs)


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


def _request(method: str = "GET", url: str = "http://127.0.0.1:9380/api/v1/datasets") -> Request:
    return Request(
        url,
        headers={"Accept": "application/json", "Authorization": "Bearer secret"},
        method=method,
    )


def _client(monkeypatch, handler, **kwargs):
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_OPENER", _transport(handler, **kwargs))
    return rf.RAGFlowClient("http://127.0.0.1:9380", "secret", timeout=5)


# --- 重试口径 ---


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_get_retries_transient_status_then_succeeds(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(status)
        return _json_response({"code": 0, "data": []})

    response = _transport(handler, max_attempts=3).open(_request(), timeout=5)

    assert response.status == 200
    assert json.loads(response.read())["code"] == 0
    assert len(calls) == 3


def test_get_retry_exhausted_raises_last_status() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(HTTPError) as excinfo:
        _transport(handler, max_attempts=3).open(_request(), timeout=5)

    assert excinfo.value.code == 503
    assert len(calls) == 3


def test_get_retries_timeout_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return _json_response({"code": 0})

    response = _transport(handler, max_attempts=2).open(_request(), timeout=5)

    assert response.status == 200
    assert len(calls) == 2


def test_connection_error_exhausted_maps_to_url_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(URLError):
        _transport(handler, max_attempts=2).open(_request(), timeout=5)

    assert len(calls) == 2


def test_client_error_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    with pytest.raises(HTTPError) as excinfo:
        _transport(handler, max_attempts=3).open(_request(), timeout=5)

    assert excinfo.value.code == 404
    assert len(calls) == 1


def test_post_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(HTTPError):
        _transport(handler, max_attempts=3).open(_request(method="POST"), timeout=5)

    assert len(calls) == 1


# --- 安全：协议白名单与同源重定向 ---


def test_cross_origin_redirect_rejected_without_leaking_authorization() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://evil.example/steal"})

    with pytest.raises(HTTPError) as excinfo:
        _transport(handler).open(_request(), timeout=5)

    assert excinfo.value.code == 302
    assert "拒绝跨域重定向" in excinfo.value.reason
    assert [str(c.url) for c in calls] == ["http://127.0.0.1:9380/api/v1/datasets"]
    assert calls[0].headers["authorization"] == "Bearer secret"


def test_same_origin_redirect_keeps_authorization() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "/api/v1/other"})
        return _json_response({"code": 0})

    response = _transport(handler).open(_request(), timeout=5)

    assert response.status == 200
    assert str(calls[1].url) == "http://127.0.0.1:9380/api/v1/other"
    assert calls[1].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/x", "file:///C:/secret.txt"])
def test_non_http_scheme_rejected(url: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(ValueError):
        _transport(handler).open(_request(url=url), timeout=5)

    assert calls == []


def test_timeout_is_passed_through() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions.get("timeout", {}))
        return _json_response({"code": 0})

    _transport(handler).open(_request(), timeout=7)

    assert seen["read"] == 7.0


# --- RAGFlow 客户端接入连接器 ---


def test_ragflow_client_uses_connector_retry(monkeypatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(502)
        return _json_response({"code": 0, "data": {"version": "v1.0.0-rc3"}})

    client = _client(monkeypatch, handler, max_attempts=3)

    body = client._request_json("system/version")

    assert body["code"] == 0
    assert len(calls) == 2


def test_ragflow_business_error_code_not_retried(monkeypatch) -> None:
    from app.ragas_adapter import ragflow as rf

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _json_response({"code": 109, "message": "Authentication error"})

    client = _client(monkeypatch, handler, max_attempts=3)

    with pytest.raises(rf.RAGFlowHTTPError) as excinfo:
        client._request_json("system/version")

    assert excinfo.value.error_code == "upstream_auth_failed"
    assert len(calls) == 1


def test_connection_test_timeout_sends_single_request(monkeypatch) -> None:
    """test 是手动点的：网络黑洞时只发 1 次请求，不把 10s 超时放大成 ~31.5s。"""
    from app.ragas_adapter import ragflow as rf

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    # 连接器默认 3 次尝试；只有 run_connection_test 这一层会把它压到 1 次。
    client = _client(monkeypatch, handler, max_attempts=3)

    result = rf.run_connection_test(client)

    assert result["verdict"] == "unreachable"
    assert result["request_count"] == 1
    assert len(calls) == 1
    # 覆盖只对本轮生效，不污染同一个 client 后续的后台同步调用。
    assert client.attempts is None


def test_sync_path_keeps_default_retries(monkeypatch) -> None:
    """后台同步不经过 run_connection_test，仍按连接器默认次数重试。"""
    from app.ragas_adapter import ragflow as rf

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _client(monkeypatch, handler, max_attempts=3)

    with pytest.raises(rf.RAGFlowHTTPError) as excinfo:
        client._request_json("datasets")

    assert excinfo.value.error_code == "upstream_unreachable"
    assert len(calls) == 3


def test_normalize_base_url_still_rejects_non_http() -> None:
    from app.ragas_adapter import ragflow as rf

    assert rf.normalize_base_url(" http://127.0.0.1:9380/ ") == "http://127.0.0.1:9380"
    with pytest.raises(ValueError):
        rf.normalize_base_url("ftp://127.0.0.1:9380")
