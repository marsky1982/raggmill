""" S1 §5：两个连接同 dataset_id 的数据源隔离与三处身份贯穿。

不连真实 RAGFlow：用 duck-typed fake client。测试模块顶层不得 import app。
"""
from __future__ import annotations

import asyncio

import pytest


class FakeClient:
    """与 test_ragflow_sync_hardening.py 的桩等价（门禁文件不改，故此处独立定义）。"""

    def __init__(self, docs: list[dict], chunks: dict[str, list[dict]], kb_rows: list[dict] | None = None,
                 version: str | None = "0.27.1"):
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


def _doc(doc_id: str, name: str, update_time: str = "2026-01-01") -> dict:
    return {"id": doc_id, "name": name, "update_time": update_time, "status": "done", "chunk_count": 1}


def _install(monkeypatch, client: FakeClient) -> None:
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)


def _sync(connection_id: str, dataset_id: str) -> dict:
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task
    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources(dataset_id, connection_id)  # 入口要求持有租约
    return asyncio.run(rf.sync_ragflow(
        task, dataset_id, "http://placeholder:9380", "key", connection_id=connection_id,
    ))


def _make_connection(storage, name: str, base_url: str) -> dict:
    return storage.ragflow_save_connection({"name": name, "base_url": base_url, "api_key": "key"})


def _make_testset(storage, dataset_id: str, kg_id: str, connection_id: str, upstream_chunk_ids: list[str]) -> str:
    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "隔离用测试集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas",
        "source_dataset_id": dataset_id,
        "source_connection_id": connection_id,
        "stale": False, "stale_sample_count": 0,
        "samples": [{
            "index": 0, "user_input": "问题",
            "source_upstream_chunk_ids": upstream_chunk_ids,
            "stale": False,
        }],
    }
    path = storage.ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry.update(path=path, count=1)
    storage.ts_upsert(entry)
    return ts_id


def test_two_connections_same_dataset_have_independent_source_dirs(monkeypatch):
    from app import config, storage

    ds = "kb-shared-case-conn-dirs"
    conn_a = _make_connection(storage, "隔离A-MAR158", "http://a.local:9380")
    conn_b = _make_connection(storage, "隔离B-MAR158", "http://b.local:9380")

    _install(monkeypatch, FakeClient(
        [_doc("d1", "A实例文档.txt")], {"d1": [{"id": "c1", "content": "A 实例的正文"}]},
    ))
    result_a = _sync(conn_a["id"], ds)
    _install(monkeypatch, FakeClient(
        [_doc("d2", "B实例文档.txt")], {"d2": [{"id": "c2", "content": "B 实例的正文"}]},
    ))
    result_b = _sync(conn_b["id"], ds)

    dir_a = storage.ragflow_source_dir(conn_a["id"], ds)
    dir_b = storage.ragflow_source_dir(conn_b["id"], ds)
    assert dir_a != dir_b
    assert (dir_a / "current.json").exists() and (dir_b / "current.json").exists()

    snap_a, ptr_a = storage.ragflow_snapshot_current(ds, source_dir=dir_a)
    snap_b, ptr_b = storage.ragflow_snapshot_current(ds, source_dir=dir_b)
    assert ptr_a["projection_kg_id"] == result_a["kg_id"]
    assert ptr_b["projection_kg_id"] == result_b["kg_id"]
    assert ptr_a["connection_id"] == conn_a["id"]
    assert ptr_b["connection_id"] == conn_b["id"]
    assert set(snap_a["documents"]) == {"d1"}
    assert set(snap_b["documents"]) == {"d2"}

    # 新连接不写旧布局：旧目录连 config.json 都不该出现
    assert not (config.RAGFLOW_DIR / ds).exists()


def test_a_sync_does_not_mark_b_testsets_or_delete_b_projection(monkeypatch):
    from app import storage

    ds = "kb-shared-case-conn-stale"
    conn_a = _make_connection(storage, "隔离A-标记", "http://a.local:9380")
    conn_b = _make_connection(storage, "隔离B-标记", "http://b.local:9380")

    _install(monkeypatch, FakeClient([_doc("d1", "A文档.txt")], {"d1": [{"id": "c1", "content": "A 正文一"}]}))
    a1 = _sync(conn_a["id"], ds)
    _install(monkeypatch, FakeClient([_doc("d2", "B文档.txt")], {"d2": [{"id": "c2", "content": "B 正文一"}]}))
    b1 = _sync(conn_b["id"], ds)

    ts_a = _make_testset(storage, ds, a1["kg_id"], conn_a["id"], ["c1"])
    ts_b = _make_testset(storage, ds, b1["kg_id"], conn_b["id"], ["c2"])

    # A 改了正文 → A 的同步只能标 A 的测试集
    _install(monkeypatch, FakeClient([_doc("d1", "A文档.txt")], {"d1": [{"id": "c1", "content": "A 正文二"}]}))
    a2 = _sync(conn_a["id"], ds)
    assert ts_a in a2["stale_testset_ids"]
    assert ts_b not in a2["stale_testset_ids"]
    assert storage.ts_read(ts_b)["stale"] is False
    # B 的投影不是本次数据源的候选，A 的清理不得删它
    assert storage.kg_get(b1["kg_id"]) is not None
    assert b1["kg_id"] not in a2["removed_kg_ids"]
    # A 的旧投影仍被 ts_a 引用 → 保留（止血规则）
    assert storage.kg_get(a1["kg_id"]) is not None

    # 反向：B 改正文只标 B 的测试集，最新 A 投影保持不动
    ts_a2 = _make_testset(storage, ds, a2["kg_id"], conn_a["id"], ["c9"])
    _install(monkeypatch, FakeClient([_doc("d2", "B文档.txt")], {"d2": [{"id": "c2", "content": "B 正文二"}]}))
    b2 = _sync(conn_b["id"], ds)
    assert ts_b in b2["stale_testset_ids"]
    assert ts_a2 not in b2["stale_testset_ids"]
    assert storage.ts_read(ts_a2)["stale"] is False
    assert storage.kg_get(a2["kg_id"]) is not None


def test_concurrent_same_source_is_rejected_but_different_sources_allowed(monkeypatch):
    """互斥改由任务租约提供（模块内 `_sync_inflight` 已随阶段 2 删除）。

    同 source（同连接同库）只能被一个任务占用；不同 source（另一连接同 dataset_id）
    可以同时占用，两次同步的投影互不影响。
    """
    from app import storage
    from app.tasks.manager import ResourceBusyError, manager

    ds = "kb-shared-case-conn-lock"
    conn_a = _make_connection(storage, "隔离A-并发", "http://a.local:9380")
    conn_b = _make_connection(storage, "隔离B-并发", "http://b.local:9380")

    held = manager.create("ragflow_sync", storage.ragflow_sync_resources(ds, conn_a["id"]))
    other = None
    try:
        with pytest.raises(ResourceBusyError, match="资源已有任务"):
            manager.create("ragflow_sync", storage.ragflow_sync_resources(ds, conn_a["id"]))
        other = manager.create("ragflow_sync", storage.ragflow_sync_resources(ds, conn_b["id"]))
    finally:
        for task in (held, other):
            if task is None:
                continue
            task.status = "canceled"
            manager._release_resources(task)
            manager.tasks.pop(task.id, None)

    _install(monkeypatch, FakeClient([_doc("d1", "A文档.txt")], {"d1": [{"id": "c1", "content": "A 正文"}]}))
    result_a = _sync(conn_a["id"], ds)
    _install(monkeypatch, FakeClient([_doc("d2", "B文档.txt")], {"d2": [{"id": "c2", "content": "B 正文"}]}))
    result_b = _sync(conn_b["id"], ds)

    assert result_a["kg_id"]
    assert result_b["kg_id"]
    assert result_a["kg_id"] != result_b["kg_id"]
