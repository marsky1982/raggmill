"""N16 / N18：同步报告、改地址或换 key 后作废旧测试结果（N14 的后端部分见 test_ragflow_probe）。

不连真实 RAGFlow、0 token。测试模块顶层不 import app。
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _connection(name: str) -> str:
    from app import storage

    saved = storage.ragflow_save_connection({"name": name, "base_url": "http://127.0.0.1:9380", "api_key": "old-key"})
    assert storage.ragflow_record_connection_test(saved["id"], "1.0.0-rc3", {"verdict": "ready", "at": "t0"})
    return saved["id"]


@pytest.mark.parametrize("payload", [
    {"api_key": "new-key"},
    {"base_url": "http://127.0.0.1:9380", "api_key": "new-key"},
])
def test_changing_address_or_key_clears_last_test(payload):
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    cid = _connection(f"n18-{len(payload)}")
    with TestClient(app) as client:
        resp = client.patch(f"/api/ragflow/connections/{cid}", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["last_test"] is None and body["probed_version"] is None
    assert storage.ragflow_get_connection(cid)["last_test"] is None


def test_renaming_keeps_last_test():
    from fastapi.testclient import TestClient

    from app.main import app

    cid = _connection("n18-rename")
    with TestClient(app) as client:
        resp = client.patch(f"/api/ragflow/connections/{cid}", json={"name": "n18-renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_test"]["verdict"] == "ready", "只改名不影响连通性，测试结果保留"


def test_sync_result_carries_report(monkeypatch):
    """N16：同步结果分开报告读取情况、来源质量与本次变化；第二次同步（内容未变）标明复用。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    docs = [{"id": "d1", "name": "一.txt", "update_time": "1", "run": "DONE", "chunk_count": 2},
            {"id": "d2", "name": "二.txt", "update_time": "1", "run": "RUNNING", "chunk_count": 0}]
    chunks = {"d1": [{"id": "c1", "content": "星河物流的调度平台"}, {"id": "c2", "content": "第二段"}]}

    class Client:
        def list_documents(self, dataset_id):
            return [dict(r) for r in docs]

        def list_chunks(self, dataset_id, document_id):
            return [dict(r) for r in chunks.get(document_id, [])]

        def kb_structure(self, dataset_id):
            return {"templates": [{"entities": [{"name": "星河物流", "source_chunk_ids": ["c1"]}]}]}

        def get_version(self):
            return "1.0.0-rc3"

        def alteration(self, dataset_id):
            return {}

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *a, **k: Client())

    def sync():
        task = Task("ragflow_sync")
        task.resources = storage.ragflow_sync_resources("ds-n16-report", None)  # 入口要求持有租约
        return asyncio.run(rf.sync_ragflow(task, "ds-n16-report", "http://127.0.0.1:9380", "key"))

    report = sync()["report"]
    assert report["read"] == {"documents": 2, "chunks": 2, "unfinished_documents": 1}
    assert report["source_quality"] == {"chunks": 2, "chunks_with_entities": 1}
    assert report["changes"]["added_chunks"] == 2 and report["changes"]["reused_projection"] is False

    again = sync()
    assert again["reused"] is True
    assert again["report"]["changes"]["reused_projection"] is True
    assert again["report"]["changes"]["added_chunks"] == 0
