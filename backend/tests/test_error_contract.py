"""统一错误模型契约（架构演进 PR9）。

非 2xx 一律 `{detail: str, error_code: str, extra?: object}`；2xx 的「检查结论为失败」是业务结果，
用带类型的 2xx 结果（模型自检、RAGFlow test / probe），不是错误体。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_UNIFIED_KEYS = {"detail", "error_code", "extra"}


@pytest.fixture()
def client():
    from app import storage
    from app.main import app

    storage.init_storage()
    with TestClient(app) as c:
        yield c


def _assert_error_body(resp, status: int, error_code: str | None = None):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) <= _UNIFIED_KEYS, f"错误体出现顶层多余字段: {sorted(set(body) - _UNIFIED_KEYS)}"
    assert isinstance(body["detail"], str) and body["detail"].strip(), body
    assert isinstance(body["error_code"], str) and body["error_code"].strip(), body
    if error_code:
        assert body["error_code"] == error_code
    return body


def test_not_found_uses_unified_body(client):
    _assert_error_body(client.get("/api/tasks/task-not-exist"), 404, "task_not_found")


def test_bad_request_uses_unified_body(client):
    _assert_error_body(client.post("/api/presets", json={"kind": "nope", "name": "x"}), 400, "bad_request")


def test_conflict_uses_unified_body(client):
    payload = {"kind": "llm", "name": "契约-重名", "model": "m"}
    assert client.post("/api/presets", json=payload).status_code == 200
    _assert_error_body(client.post("/api/presets", json=payload), 409, "conflict")


def test_service_unavailable_uses_unified_body(client, monkeypatch):
    from app import storage

    def boom(_kind):
        raise storage.StorageCorruptionError("index.json 损坏，已阻止写入")

    monkeypatch.setattr(storage, "list_presets", boom)
    body = _assert_error_body(client.get("/api/presets"), 503, "storage_corrupt")
    assert "损坏" in body["detail"]


def test_forbidden_uses_unified_body(client, monkeypatch):
    monkeypatch.delenv("RAGAS_STUDIO_WRITE_TOKEN", raising=False)
    _assert_error_body(client.post("/api/admin/shutdown"), 403, "not_managed")


def test_validation_422_is_unified_and_chinese(client):
    """越界参数也走统一错误体：detail 是中文句子，字段级明细在 extra.errors。"""
    body = _assert_error_body(
        client.post("/api/testset/generate", json={"kg_id": "kg-x", "max_retries": 99}), 422,
        "validation_error",
    )
    assert "max_retries" in body["detail"] and "不能大于" in body["detail"]
    errors = body["extra"]["errors"]
    assert errors and errors[0]["loc"] == ["body", "max_retries"]
    assert errors[0]["type"] == "less_than_equal"


def test_validation_422_keeps_custom_chinese_message(client):
    """自定义校验器抛出的中文提示原样保留（不能被 pydantic 的英文前缀吃掉）。"""
    body = _assert_error_body(client.post("/api/testset/generate", json={
        "kg_id": "kg-x", "llm_preset_id": "p-llm", "embedding_preset_id": "p-emb",
        "name": "名" * 81,
    }), 422, "validation_error")
    assert "测试集名称最多 80 个字符" in body["detail"]
    assert "Value error" not in body["detail"]


def test_sse_error_event_uses_unified_body():
    """任务不存在的 SSE 错误事件也用同一套错误体（前端 api.ts 按 detail 收尾）。"""
    from app.routers.tasks import _sse_gen

    async def collect():
        return [chunk async for chunk in _sse_gen("task-not-exist")]

    chunks = asyncio.run(collect())
    assert chunks[0].startswith("event: error")
    payload = json.loads(chunks[0].split("data: ", 1)[1])
    assert payload["error_code"] == "task_not_found"
    assert isinstance(payload["detail"], str) and payload["detail"]


def test_self_check_result_is_typed(client, monkeypatch):
    """连通失败是 2xx 业务结果：{ok, error_code?, detail?, latency_ms}，没有旧字段 error。"""
    import langchain_openai

    class _Boom:
        def __init__(self, **_kwargs):
            pass

        async def ainvoke(self, *_args, **_kwargs):
            raise RuntimeError("connect refused")

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _Boom)
    created = client.post("/api/presets", json={
        "kind": "llm", "name": "契约-自检", "base_url": "http://127.0.0.1:9", "model": "m",
    }).json()
    resp = client.post(f"/api/presets/{created['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "error_code", "detail", "latency_ms"}
    assert body["ok"] is False and body["error_code"] == "connect_failed"
    assert "connect refused" in body["detail"]
    assert isinstance(body["latency_ms"], int)


def test_ragflow_check_routes_declare_response_model():
    """test / probe 的 2xx 结果有显式 response_model。

    `test` 同步返回探测结论（RagflowCheckResult）；`probe` 起后台任务，2xx 只是 202 + task_id，
    真正的结论走任务 result，所以 202 声明 RagflowSyncStartedOut（此前误挂在 200 上，
    与真实响应不一致，PR10 补 response_model 时一并纠正）。
    """
    from app.main import app

    paths = app.openapi()["paths"]
    test_ref = json.dumps(
        paths["/api/ragflow/connections/{connection_id}/test"]["post"]["responses"]["200"],
        ensure_ascii=False)
    assert "RagflowCheckResult" in test_ref, "连接自检未声明 RagflowCheckResult"
    probe_responses = paths[
        "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/probe"]["post"]["responses"]
    probe_ref = json.dumps(probe_responses["202"], ensure_ascii=False)
    assert "RagflowSyncStartedOut" in probe_ref, "知识库探测未声明 202 回执模型"


def test_invalid_path_id_returns_422_chinese(client):
    """路径参数不匹配时 detail 是中文，不把正则原文抛给前端。"""
    body = _assert_error_body(client.get("/api/ragflow/sources/bad%20id/snapshot"), 422, "validation_error")
    assert "source_id" in body["detail"]
    assert "格式不合法" in body["detail"]
    assert "pattern" not in body["detail"]
    errors = body["extra"]["errors"]
    assert errors[0]["type"] == "string_pattern_mismatch"
    assert errors[0]["loc"] == ["path", "source_id"]


@pytest.mark.parametrize("path", [
    "/api/tasks/task-not-exist/stream",
    "/api/kg/tasks/task-not-exist/stream",
    "/api/testset/tasks/task-not-exist/stream",
])
def test_task_stream_routes_404_with_unified_body(client, path):
    """主路由与两条兼容别名的 SSE 入口同口径（断线续传的 Last-Event-ID 不影响 404）。"""
    _assert_error_body(client.get(path, headers={"Last-Event-ID": "0"}), 404, "task_not_found")


def test_unhandled_exception_uses_unified_body(monkeypatch):
    """未捕获异常也返回统一错误体（不是 text/plain 的 Internal Server Error），堆栈只进日志。"""
    from app import storage
    from app.main import app

    def boom(_kind):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(storage, "list_presets", boom)
    storage.init_storage()
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/presets")
    assert resp.headers["content-type"].startswith("application/json")
    body = _assert_error_body(resp, 500, "internal_error")
    assert "服务器内部错误" in body["detail"]
    assert "disk on fire" not in body["detail"]


def test_ragflow_id_patterns_match_validator():
    """路由用的 Path(pattern=...) 与 validate_ragflow_name 同一套规则，长度上限只定义一次。"""
    import re

    from app import storage

    cases = [
        ("abc", True), ("a" * 64, True), ("a" * 65, False), ("0-b_X", True),
        (".hidden", False), ("a.b", True), ("a b", False), ("中文", False), ("", False),
        ("a__b", True), ("conn-1__dataset.2", True),
    ]
    for value, ok in cases:
        matched = re.fullmatch(storage.RAGFLOW_ID_PATTERN, value) is not None
        try:
            storage.validate_ragflow_name(value)
            accepted = True
        except storage.UnsafePathError:
            accepted = False
        assert matched is ok and accepted is ok, f"{value!r}: pattern={matched} validator={accepted}"

    long_source = "a" * 120
    assert re.fullmatch(storage.RAGFLOW_SOURCE_ID_PATTERN, long_source) is not None
    assert storage.validate_ragflow_name(long_source, max_len=120) == long_source
    too_long = "a" * 121
    assert re.fullmatch(storage.RAGFLOW_SOURCE_ID_PATTERN, too_long) is None
    with pytest.raises(storage.UnsafePathError):
        storage.validate_ragflow_name(too_long, max_len=120)


def test_no_legacy_reason_code_in_sources():
    """改名做干净：backend/app 与 frontend/src 里不再出现 reason_code。"""
    root = Path(__file__).resolve().parents[2]
    targets = [root / "backend" / "app", root / "frontend" / "src"]
    hits = []
    for base in targets:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            if "reason_code" in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path.relative_to(root)))
    assert not hits, f"仍有 reason_code: {hits}"
