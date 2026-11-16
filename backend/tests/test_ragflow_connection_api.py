""" S2.5：连接 CRUD、连接式同步与 source 快照的 HTTP 契约。

不连真实 RAGFlow：同步路径用 duck-typed fake client。测试模块顶层不得 import app
（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import time


class FakeClient:
    """与 test_ragflow_source_isolation.py 的桩等价（门禁文件不改，故独立定义）。"""

    def __init__(self, docs: list[dict], chunks: dict[str, list[dict]],
                 kb_rows: list[dict] | None = None, version: str | None = "0.27.1"):
        self.docs = docs
        self.chunks = chunks
        self.kb_rows = kb_rows or []
        self.version = version

    def list_documents(self, dataset_id: str) -> list[dict]:
        return [dict(row) for row in self.docs]

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return [dict(row) for row in self.chunks.get(document_id, [])]

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": [{"entities": [dict(row) for row in self.kb_rows]}]}

    def get_version(self) -> str | None:
        return self.version

    def alteration(self, dataset_id: str) -> dict:
        return {}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _create(client, *, name: str, base_url: str = "http://127.0.0.1:9380",
            api_key: str = "sk-s25-secret-7890", **extra):
    return client.post("/api/ragflow/connections", json={
        "name": name, "base_url": base_url, "api_key": api_key, **extra,
    })


def _wait_task(client, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body.get("status") in ("succeeded", "failed", "canceled", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在时限内结束")


def _doc(doc_id: str, name: str, update_time: str = "2026-01-01") -> dict:
    return {"id": doc_id, "name": name, "update_time": update_time, "status": "done", "chunk_count": 1}


def test_connection_crud_full_flow_never_exposes_key():
    from app import storage

    with _client() as client:
        created = _create(client, name="S25-CRUD-连接")
        assert created.status_code == 200
        body = created.json()
        cid = body["id"]
        assert cid.startswith("conn-")
        assert body["base_url"] == "http://127.0.0.1:9380"  # 规范化去尾斜杠
        assert body["api_key_mask"].endswith("7890")
        assert body["legacy"] is False
        assert "api_key" not in body and "api_key_enc" not in body

        raw = storage.ragflow_connection_path(cid).read_text(encoding="utf-8")
        assert "sk-s25-secret-7890" not in raw  # 落盘无明文
        assert "api_key_enc" in raw

        listed = client.get("/api/ragflow/connections").json()
        assert [c["id"] for c in listed if c["id"] == cid] == [cid]
        assert all("api_key" not in c for c in listed)

        single = client.get(f"/api/ragflow/connections/{cid}")
        assert single.status_code == 200
        assert "api_key" not in single.json()

        # 只改名：允许，且保留旧密文
        renamed = client.patch(f"/api/ragflow/connections/{cid}", json={"name": "S25-CRUD-改名"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "S25-CRUD-改名"
        assert renamed.json()["api_key_mask"].endswith("7890")

        missing = client.get("/api/ragflow/connections/conn-s25-missing")
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "connection_not_found"


def test_create_validation_and_name_conflict():
    with _client() as client:
        assert _create(client, name="S25-校验-连接").status_code == 200

        empty_key = _create(client, name="S25-空key", api_key="   ")
        assert empty_key.status_code == 400
        assert empty_key.json()["error_code"] == "bad_request"

        bad_url = _create(client, name="S25-坏地址", base_url="ftp://x.local")
        assert bad_url.status_code == 400
        assert bad_url.json()["error_code"] == "bad_request"

        conflict = _create(client, name="S25-校验-连接")
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "name_conflict"


def test_patch_base_url_requires_key_and_rejects_name_conflict():
    with _client() as client:
        first = _create(client, name="S25-改址-A", api_key="sk-old-1111").json()
        _create(client, name="S25-改址-B")
        cid = first["id"]

        rebind = client.patch(f"/api/ragflow/connections/{cid}", json={"base_url": "http://127.0.0.1:9381"})
        assert rebind.status_code == 400
        assert rebind.json()["error_code"] == "credential_rebind_required"
        assert "重新填写" in rebind.json()["detail"]

        # 换地址必须同时给 key
        ok = client.patch(f"/api/ragflow/connections/{cid}", json={
            "base_url": "http://127.0.0.1:9381", "api_key": "sk-new-2222",
        })
        assert ok.status_code == 200
        assert ok.json()["base_url"] == "http://127.0.0.1:9381"
        assert ok.json()["api_key_mask"].endswith("2222")

        dup = client.patch(f"/api/ragflow/connections/{cid}", json={"name": "S25-改址-B"})
        assert dup.status_code == 409
        assert dup.json()["error_code"] == "name_conflict"


def test_legacy_connection_cannot_change_base_url():
    from app import storage

    legacy_id = storage.legacy_connection_id("kb-s25-legacy")
    storage.ragflow_save_connection({
        "id": legacy_id, "name": "历史配置-kb-s25-legacy",
        "base_url": "http://old.local:9380", "api_key": "k", "legacy": True,
        "dataset_id": "kb-s25-legacy",
    })
    with _client() as client:
        resp = client.patch(f"/api/ragflow/connections/{legacy_id}", json={
            "base_url": "http://new.local:9380", "api_key": "k2",
        })
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "bad_request"
        assert "历史配置" in resp.json()["detail"]


def test_source_id_with_invalid_chars_is_rejected():
    with _client() as client:
        cid = _create(client, name="S25-非法ID").json()["id"]
        resp = client.post(f"/api/ragflow/connections/{cid}/datasets/中文知识库/sync", json={})
        # 非法路径参数在解析阶段被拦下（422 统一错误体），不再进业务代码
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "validation_error"
        assert "dataset_id" in resp.json()["detail"]


def test_connection_sync_returns_source_id_and_snapshot(monkeypatch):
    from app import storage
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: FakeClient(
        [_doc("d1", "S25同步文档.txt")], {"d1": [{"id": "c1", "content": "S25 正文"}]},
    ))
    with _client() as client:
        cid = _create(client, name="S25-同步").json()["id"]
        resp = client.post(f"/api/ragflow/connections/{cid}/datasets/kb-s25-sync/sync", json={})
        assert resp.status_code == 202
        body = resp.json()
        assert body["source_id"] == f"{cid}__kb-s25-sync"
        task = _wait_task(client, body["task_id"])
        assert task["status"] == "succeeded", task

        snapshot = client.get(f"/api/ragflow/sources/{body['source_id']}/snapshot")
        assert snapshot.status_code == 200
        snap = snapshot.json()
        assert snap["connection_id"] == cid
        assert snap["dataset_id"] == "kb-s25-sync"
        assert snap["document_count"] == 1
        assert snap["chunk_count"] == 1
        assert snap["projection_kg_id"] == task["result"]["kg_id"]
        # 前端「已同步数据源」的版本行直接读这个字段，必须与快照落盘值一致
        assert snap["ragflow_version"] == "0.27.1"

        assert storage.ragflow_source_dir(cid, "kb-s25-sync").is_dir()


def test_duplicate_sync_is_rejected_with_409():
    from app.tasks.manager import manager

    with _client() as client:
        cid = _create(client, name="S25-并发").json()["id"]
        source_id = f"{cid}__kb-s25-busy"
        busy = manager.create("ragflow_sync", (source_id,))  # 占用同一数据源
        try:
            resp = client.post(f"/api/ragflow/connections/{cid}/datasets/kb-s25-busy/sync", json={})
            assert resp.status_code == 409
            assert resp.json()["error_code"] == "sync_in_progress"
        finally:
            busy.status = "succeeded"
            busy.finished_at = time.time()


def test_delete_only_removes_credential_and_keeps_history(monkeypatch):
    from app import storage
    from app.ragas_adapter import ragflow as rf

    dataset = "kb-s25-retain"
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: FakeClient(
        [_doc("d1", "保留文档.txt")], {"d1": [{"id": "c1", "content": "保留正文"}]},
    ))
    with _client() as client:
        cid = _create(client, name="S25-删除").json()["id"]
        task = _wait_task(client, client.post(
            f"/api/ragflow/connections/{cid}/datasets/{dataset}/sync", json={},
        ).json()["task_id"])
        kg_id = task["result"]["kg_id"]

        ts_id = storage.new_id("ts")
        ts_path = storage.ts_write(ts_id, {
            "id": ts_id, "name": "S25 历史测试集", "created_at": storage.now_iso(),
            "kg_id": kg_id, "mode": "ragas", "samples": [{"index": 0, "user_input": "问题"}],
        })
        storage.ts_upsert({"id": ts_id, "name": "S25 历史测试集", "kg_id": kg_id,
                           "count": 1, "path": ts_path})

        removed = client.delete(f"/api/ragflow/connections/{cid}")
        assert removed.status_code == 200
        assert removed.json() == {"deleted": True, "retained": True, "unbound_testset_ids": []}

        # 凭据消失：列表与单条都查不到（读路径过滤 _deleted）
        assert [c["id"] for c in client.get("/api/ragflow/connections").json() if c["id"] == cid] == []
        assert client.get(f"/api/ragflow/connections/{cid}").status_code == 404

        # 历史仍可读：源目录、快照、投影、测试集都不受影响
        assert (storage.ragflow_source_dir(cid, dataset) / "current.json").exists()
        assert client.get(f"/api/ragflow/sources/{cid}__{dataset}/snapshot").status_code == 200
        assert storage.kg_read_json(kg_id) is not None
        assert storage.ts_read(ts_id) is not None

        again = client.delete(f"/api/ragflow/connections/{cid}")
        assert again.status_code == 404
        assert again.json()["error_code"] == "connection_not_found"


def test_source_snapshot_404_and_invalid_source_id():
    with _client() as client:
        missing = client.get("/api/ragflow/sources/conn-s25-x__kb-s25-y/snapshot")
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "snapshot_not_found"

        invalid = client.get("/api/ragflow/sources/bad%20id/snapshot")
        assert invalid.status_code == 422
        assert invalid.json()["error_code"] == "validation_error"


def test_rebind_rejects_when_old_source_dir_missing():
    """重新绑定的前置校验：旧连接下没有该库的同步目录时明确拒绝（不做任何改动）。"""
    with _client() as client:
        new_cid = _create(client, name="S25-改绑-新").json()["id"]
        old_cid = _create(client, name="S25-改绑-旧").json()["id"]
        resp = client.post(
            f"/api/ragflow/connections/{new_cid}/datasets/kb-s25-rebind-missing/rebind",
            json={"from_connection_id": old_cid},
        )
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "rebind_failed"
        assert "找不到旧连接" in resp.json()["detail"]
