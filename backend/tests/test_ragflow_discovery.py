""" S2 §9.1：RAGFlow 发现接口（两形状兼容 / 分页护栏 / test 判定）。

不连真实 RAGFlow：HTTP 层用 FakeOpener 打桩，分页形状用 `_get` 打桩。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest


class FakeResponse:
    def __init__(self, body: str, status: int = 200, content_type: str = "application/json"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, response=None, exc: Exception | None = None):
        self.response = response
        self.exc = exc

    def open(self, request, timeout=None):
        if self.exc:
            raise self.exc
        return self.response


class RoutingOpener:
    """按 URL path 返回不同响应，并记录请求路径。"""

    def __init__(self, routes: dict[str, object], calls: list[str]):
        self.routes = routes
        self.calls = calls

    def open(self, request, timeout=None):
        path = urlsplit(request.full_url).path
        self.calls.append(path)
        for prefix, response in self.routes.items():
            if path.endswith(prefix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response()
                return response
        raise AssertionError(f"未打桩的 RAGFlow 请求: {path}")


def _client():
    from app.ragas_adapter import ragflow as rf
    return rf.RAGFlowClient("http://127.0.0.1:9380", "key")


def _assert_stages(stages: list[dict]) -> None:
    from app.ragas_adapter import ragflow as rf
    assert [stage["key"] for stage in stages] == list(rf._STAGE_KEYS)
    assert len(stages) == 7
    for stage in stages:
        assert stage["status"] in ("ok", "fail", "skipped")


def test_list_datasets_supports_array_shape(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    body = json.dumps({"code": 0, "data": [{"id": "d1"}, {"id": "d2"}]})
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(body)))
    assert [row["id"] for row in _client().list_datasets()] == ["d1", "d2"]


def test_list_datasets_supports_paged_dict_shape(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    body = json.dumps({"code": 0, "data": {"datasets": [{"id": "d1"}, {"id": "d2"}], "total": 2}})
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(body)))
    assert [row["id"] for row in _client().list_datasets()] == ["d1", "d2"]


def test_list_datasets_page_normalizes_both_shapes(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    array_body = json.dumps({"code": 0, "data": [{"id": f"d{i}"} for i in range(5)]})
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(array_body)))
    page = _client().list_datasets_page(page=1, page_size=2)
    assert [row["id"] for row in page["items"]] == ["d0", "d1"]
    assert page["total"] == 5 and page["has_more"] is True

    paged_body = json.dumps({"code": 0, "data": {"datasets": [{"id": "a"}, {"id": "b"}], "total": 5}})
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(paged_body)))
    page = _client().list_datasets_page(page=1, page_size=2)
    assert page["total"] == 5 and page["has_more"] is True
    assert len(page["items"]) == 2


def test_list_datasets_detects_repeated_page(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    client = _client()
    rows = [{"id": f"d{i}"} for i in range(100)]
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"datasets": rows})
    with pytest.raises(rf.RAGFlowError, match="重复页"):
        client.list_datasets()


def test_list_datasets_respects_page_limit(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    client = _client()
    monkeypatch.setattr(rf, "_MAX_PAGES", 3)

    def fake_get(path, params=None):
        page = int((params or {}).get("page", 1))
        return {"datasets": [{"id": f"p{page}-{i}"} for i in range(100)]}

    monkeypatch.setattr(client, "_get", fake_get)
    with pytest.raises(rf.RAGFlowError, match="超过上限 3 页"):
        client.list_datasets()


def test_connection_test_401_and_403_map_auth_failed(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    for status in (401, 403):
        monkeypatch.setattr(
            rf, "_OPENER",
            FakeOpener(exc=HTTPError("http://x/api/v1/datasets", status, "auth", {}, None)),
        )
        result = rf.run_connection_test(_client())
        assert result["verdict"] == "auth_failed"
        assert result["request_count"] == 1
        _assert_stages(result["stages"])
        assert result["stages"][0]["status"] == "ok"
        assert result["stages"][1]["status"] == "fail"


def test_connection_test_html_maps_incompatible(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse("<html>login</html>", content_type="text/html")))
    result = rf.run_connection_test(_client())
    assert result["verdict"] == "incompatible"
    assert result["stages"][0]["status"] == "fail"
    _assert_stages(result["stages"])


def test_connection_test_timeout_maps_unreachable(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(exc=TimeoutError("timed out")))
    result = rf.run_connection_test(_client())
    assert result["verdict"] == "unreachable"
    assert result["stages"][0]["status"] == "fail"
    detail = result["stages"][0]["detail"]
    assert detail.startswith("连不上 RAGFlow 服务（")
    assert "datasets:" not in detail, "失败明细不应重复端点前缀"
    _assert_stages(result["stages"])


def test_connection_test_nonzero_code_is_not_success(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    body = json.dumps({"code": 1, "message": "boom", "data": {"datasets": []}})
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(body)))
    result = rf.run_connection_test(_client())
    assert result["verdict"] != "ready"
    assert result["verdict"] == "incompatible"
    assert result["stages"][2]["status"] == "fail"
    _assert_stages(result["stages"])


def test_connection_test_zero_datasets_maps_graph_empty(monkeypatch):
    """0 个可见知识库 = 没有可出题素材：判 graph_empty，后半段保持 skipped（§4.3）。"""
    from app.ragas_adapter import ragflow as rf
    calls: list[str] = []
    routes = {
        "/api/v1/system/version": FakeResponse(json.dumps({"code": 0, "data": "0.27.1"})),
        "/api/v1/datasets": FakeResponse(json.dumps({"code": 0, "data": [], "total_datasets": 0})),
    }
    monkeypatch.setattr(rf, "_OPENER", RoutingOpener(routes, calls))
    result = rf.run_connection_test(_client())
    assert result["verdict"] == "graph_empty"
    assert result["detail"] == "该账号在此实例下没有可见知识库"
    assert result["request_count"] == 2
    assert not any("/documents" in path for path in calls), "0 个库时不该再打文档接口"
    _assert_stages(result["stages"])
    assert result["stages"][0]["status"] == "ok"
    assert result["stages"][1]["status"] == "ok"
    assert result["stages"][2]["status"] == "ok"
    for index in (3, 4, 5, 6):
        assert result["stages"][index]["status"] == "skipped"


def test_connection_test_first_dataset_without_documents_is_still_ready(monkeypatch):
    """N9②：test 只判「连接能否使用」。列表里第一个库恰好为空，不能把整条连接判成 graph_empty
    （其他库可能正常）；首库文档数只作参考、标 skipped，目标库能否出题由 probe 回答。"""
    from app.ragas_adapter import ragflow as rf
    calls: list[str] = []
    routes = {
        "/api/v1/system/version": FakeResponse(json.dumps({"code": 0, "data": "0.27.1"})),
        "/api/v1/datasets/d1/documents": FakeResponse(
            json.dumps({"code": 0, "data": {"docs": [], "total": 0}})
        ),
        "/api/v1/datasets": FakeResponse(json.dumps({"code": 0, "data": [{"id": "d1", "name": "空库"}],
                                                    "total_datasets": 2})),
    }
    monkeypatch.setattr(rf, "_OPENER", RoutingOpener(routes, calls))
    result = rf.run_connection_test(_client())
    assert result["verdict"] == "ready"
    assert result["stages"][2]["detail"] == "可见 2 个知识库"
    assert result["stages"][3]["status"] == "skipped"
    assert "还没有文档" in result["stages"][3]["detail"] and "检查图谱" in result["stages"][3]["detail"]
    _assert_stages(result["stages"])


def test_connection_test_unreadable_first_dataset_is_partial(monkeypatch):
    """能列库但读不了首库的文档（不是「没有文档」）：仍判 partial，不掩盖真失败。"""
    from app.ragas_adapter import ragflow as rf
    routes = {
        "/api/v1/system/version": FakeResponse(json.dumps({"code": 0, "data": "1.0.0-rc3"})),
        "/api/v1/datasets/d1/documents": FakeResponse(json.dumps({"code": 500, "message": "boom"})),
        "/api/v1/datasets": FakeResponse(json.dumps({"code": 0, "data": [{"id": "d1"}], "total_datasets": 1})),
    }
    monkeypatch.setattr(rf, "_OPENER", RoutingOpener(routes, []))
    result = rf.run_connection_test(_client())
    assert result["verdict"] == "partial" and result["stages"][3]["status"] == "fail"


# ---------- N3：知识库列表是服务端分页，总数在顶层 total_datasets（v0.27.1 / rc3 同形状） ----------

def _paged_datasets_opener(total: int, calls: list):
    """模拟真实上游：按 page / page_size 返回当前页，顶层带 total_datasets。"""
    from urllib.parse import parse_qs

    class Opener:
        def open(self, request, timeout=None):
            q = parse_qs(urlsplit(request.full_url).query)
            page, size = int(q["page"][0]), int(q["page_size"][0])
            calls.append((page, size))
            start = (page - 1) * size
            rows = [{"id": f"d{i}"} for i in range(start, min(start + size, total))]
            return FakeResponse(json.dumps({"code": 0, "data": rows, "message": "success", "total_datasets": total}))
    return Opener()


def test_list_datasets_page_uses_server_pagination_and_total_datasets(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    calls: list = []
    monkeypatch.setattr(rf, "_OPENER", _paged_datasets_opener(25, calls))
    first = _client().list_datasets_page(page=1, page_size=20)
    assert first["total"] == 25 and first["has_more"] is True and len(first["items"]) == 20
    second = _client().list_datasets_page(page=2, page_size=20)
    assert [r["id"] for r in second["items"]] == [f"d{i}" for i in range(20, 25)]  # 旧实现第 2 页是 0 条
    assert second["has_more"] is False


def test_list_datasets_reads_every_server_page(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    calls: list = []
    monkeypatch.setattr(rf, "_OPENER", _paged_datasets_opener(rf._PAGE_SIZE + 7, calls))
    rows = _client().list_datasets()
    assert len(rows) == rf._PAGE_SIZE + 7 and len(calls) == 2  # 旧实现数组形状取一页就返回


def test_connection_test_counts_all_visible_datasets(monkeypatch):
    """连接测试只取 page_size=1，但计数要用 total_datasets（旧实现恒显示「可见 1 个知识库」）。"""
    from app.ragas_adapter import ragflow as rf
    calls: list = []
    monkeypatch.setattr(rf, "_OPENER", _paged_datasets_opener(25, calls))
    page = _client().list_datasets_page(1, 1)
    assert page["total"] == 25 and page["has_more"] is True


# ---------- N4：库不可见 ≠ 接口不兼容；业务码里的鉴权失败 ----------

@pytest.mark.parametrize("code,message", [
    (102, "You don't own the dataset nope."),    # v0.27.1
    (404, "resource not found"),                 # rc3 CodeNotFound（无权限资源也会被隐藏成 404）
    (403, "permission denied"),                  # rc3 CodeForbidden
    (108, "permission error"),                   # rc3 CodePermissionError
])
def test_not_visible_dataset_maps_dataset_not_found(monkeypatch, code, message):
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(json.dumps({"code": code, "data": None,
                                                                           "message": message}))))
    with pytest.raises(rf.RAGFlowHTTPError) as info:
        _client().list_documents_page("nope", 1, 1)
    assert info.value.error_code == "dataset_not_found"
    assert rf._classify_upstream(info.value) == "auth_failed"


def test_business_code_auth_failure_maps_auth_failed(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    for code in sorted(rf._AUTH_FAILED_CODES):
        monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(json.dumps({"code": code, "message": "bad token"}))))
        with pytest.raises(rf.RAGFlowHTTPError) as info:
            _client().list_datasets_page(1, 1)
        assert info.value.error_code == "upstream_auth_failed"


def test_non_dataset_business_error_stays_upstream_error(monkeypatch):
    """库列表本身（不是某个库下的资源）回 102：不能误报成「看不到这个知识库」。"""
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse(json.dumps({"code": 102, "message": "bad arg"}))))
    with pytest.raises(rf.RAGFlowHTTPError) as info:
        _client().list_datasets_page(1, 1)
    assert info.value.error_code == "upstream_error"


def _save_connection(connection_id: str = "conn-disc-1") -> dict:
    from app import storage
    return storage.ragflow_save_connection({
        "id": connection_id,
        "name": "发现测试",
        "base_url": "http://127.0.0.1:9380",
        "api_key": "key",
    })


def test_datasets_endpoint_404_and_502_error_bodies(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.ragas_adapter import ragflow as rf

    with TestClient(app) as client:
        missing = client.get("/api/ragflow/connections/conn-missing/datasets")
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "connection_not_found"

        _save_connection()
        monkeypatch.setattr(rf, "_OPENER", FakeOpener(FakeResponse("<html></html>", content_type="text/html")))
        bad = client.get("/api/ragflow/connections/conn-disc-1/datasets")
        assert bad.status_code == 502
        body = bad.json()
        assert body["error_code"] == "upstream_not_json"
        assert body["extra"]["upstream_status"] == 200
        assert isinstance(body["detail"], str) and body["detail"]

        invalid = client.get("/api/ragflow/connections/conn-disc-1/datasets?page=0")
        assert invalid.status_code == 400
        assert invalid.json()["error_code"] == "bad_request"


def test_test_endpoint_returns_seven_stages(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.ragas_adapter import ragflow as rf

    calls: list[str] = []
    routes = {
        "/api/v1/system/version": FakeResponse(json.dumps({"code": 0, "data": "0.27.1"})),
        "/api/v1/datasets/d1/documents": FakeResponse(
            json.dumps({"code": 0, "data": {"docs": [{"id": "doc1"}], "total": 1}})
        ),
        "/api/v1/datasets": FakeResponse(json.dumps({"code": 0, "data": [{"id": "d1"}]})),
    }
    monkeypatch.setattr(rf, "_OPENER", RoutingOpener(routes, calls))
    _save_connection()
    with TestClient(app) as client:
        resp = client.post("/api/ragflow/connections/conn-disc-1/test", json={"timeout": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["stages"]) == 7
    assert body["probed_version"] == "0.27.1"
    assert len(calls) <= 3


def test_connection_test_uses_at_most_three_requests_and_keeps_seven_stages(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    calls: list[str] = []
    routes = {
        "/api/v1/system/version": FakeResponse(json.dumps({"code": 0, "data": "0.27.1"})),
        "/api/v1/datasets/d1/documents": FakeResponse(
            json.dumps({"code": 0, "data": {"docs": [{"id": "doc1"}], "total": 1}})
        ),
        "/api/v1/datasets": FakeResponse(json.dumps({"code": 0, "data": [{"id": "d1", "name": "库1"}]})),
    }
    monkeypatch.setattr(rf, "_OPENER", RoutingOpener(routes, calls))
    result = rf.run_connection_test(_client())
    assert result["request_count"] == 3
    assert len(calls) <= 3
    assert result["probed_version"] == "0.27.1"
    assert result["verdict"] == "ready"
    _assert_stages(result["stages"])
    assert result["stages"][3]["status"] == "ok"
    assert result["stages"][4]["status"] == "skipped"
    assert result["stages"][5]["status"] == "skipped"
    assert result["stages"][6]["status"] == "skipped"
