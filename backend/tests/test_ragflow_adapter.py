"""RAGFlow 投影适配器回归测试（T8-4 版本锚定、T3-3 实体口径、增量幂等）。

不连真实 RAGFlow：用 duck-typed fake client 喂固定响应。
"""
from __future__ import annotations


class FakeClient:
    def __init__(
        self,
        docs: list[dict],
        chunks: dict[str, list[dict]],
        kb_rows: list[dict],
        version: str | None = "0.27.1",
    ):
        self.docs = docs
        self.chunks = chunks
        self.kb_rows = kb_rows
        self.version = version
        self.document_structure_calls: list[str] = []

    def list_documents(self, dataset_id: str) -> list[dict]:
        return self.docs

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return self.chunks.get(document_id, [])

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": [{"entities": self.kb_rows}]}

    def get_version(self) -> str | None:
        return self.version

    def alteration(self, dataset_id: str) -> dict:
        return {}

    def document_structure(self, dataset_id: str, document_id: str) -> dict:
        # 旧路径：文档级结构行。新实现不应再调用它
        self.document_structure_calls.append(document_id)
        return {"templates": [{"entities": []}]}


def _two_docs():
    docs = [
        {"id": "d1", "name": "文档一.txt", "update_time": "2026-01-01", "status": "done", "chunk_count": 1},
        {"id": "d2", "name": "文档二.txt", "update_time": "2026-01-02", "status": "done", "chunk_count": 1},
    ]
    chunks = {
        "d1": [{"id": "c1", "content": "国家电网发布了公告"}],
        "d2": [{"id": "c2", "content": "国网宣布新计划"}],
    }
    # KB 级：两个名字已被语义归并成一行（T3-3 的核心）
    kb_rows = [{"name": "国家电网有限公司", "source_chunk_ids": ["c1", "c2"]}]
    return docs, chunks, kb_rows


def test_snapshot_writes_ragflow_version():
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    snapshot, _ = fetch_snapshot(FakeClient(docs, chunks, kb_rows, version="0.27.1"), "ds-1")

    assert snapshot["ragflow_version"] == "0.27.1"


def test_client_reads_version_from_both_response_shapes(monkeypatch):
    """v0.27.1 的字符串 data 与 v1.0.0 的对象 data 都必须取到版本号。

    打桩要给 `_get` 的**返回值**（dict 形态的 data 已被展开一层），不是上游原始响应：
    旧用例按原始响应打桩，才让「二次解包 data」的 bug 漏了过去。
    """
    from app.ragas_adapter.ragflow import RAGFlowClient

    client = RAGFlowClient("http://127.0.0.1:9380", "key")

    # v0.27.1: data 是字符串，_get 原样返回整个 body
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"code": 0, "data": "v0.27.1"})
    assert client.get_version() == "0.27.1"

    # v1.0.0 起 data 改为对象：_get 已展开一层，payload 本身就是 {"version": ...}
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"version": "v1.0.0"})
    assert client.get_version() == "1.0.0"

    # 无 v 前缀、带空白也认
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"version": " 1.2.3 "})
    assert client.get_version() == "1.2.3"


def test_client_version_tolerates_unexpected_payloads(monkeypatch):
    """上游形状异常时一律返回 None 并记 warning，不抛错（调用方把 None 显式写进快照）。"""
    from app.ragas_adapter.ragflow import RAGFlowClient, RAGFlowError

    client = RAGFlowClient("http://127.0.0.1:9380", "key")

    for payload in (
        {},
        {"build": "x"},
        {"version": None},
        {"version": 100},
        {"version": "   "},
        {"code": 0},
        [],
        None,
    ):
        monkeypatch.setattr(client, "_get", lambda path, params=None, _p=payload: _p)
        assert client.get_version() is None

    def boom(path, params=None):
        raise RAGFlowError("down")

    monkeypatch.setattr(client, "_get", boom)
    assert client.get_version() is None


def test_snapshot_keeps_none_when_version_unavailable():
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    snapshot, _ = fetch_snapshot(FakeClient(docs, chunks, kb_rows, version=None), "ds-1")

    assert "ragflow_version" in snapshot
    assert snapshot["ragflow_version"] is None


def test_old_snapshot_without_version_is_readable(tmp_path):
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    snapshot, _ = fetch_snapshot(client, "ds-1")
    old_snapshot = {k: v for k, v in snapshot.items() if k != "ragflow_version"}

    # 旧快照（无该字段）作为 previous 不应报错，新快照仍补上该字段
    refreshed, _ = fetch_snapshot(client, "ds-1", previous=old_snapshot)
    assert refreshed["ragflow_version"] == "0.27.1"


def test_projection_uses_kb_level_entities_not_name_union():
    from app.ragas_adapter.ragflow import build_ragas_projection, fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    snapshot, _ = fetch_snapshot(client, "ds-1")

    # 供料路径不应再读单文档结构（否则会退回名字并集口径）
    assert client.document_structure_calls == []
    assert snapshot["document_sources"]["d1"]["chunks"]["c1"]["entities"] == ["国家电网有限公司"]
    assert snapshot["document_sources"]["d2"]["chunks"]["c2"]["entities"] == ["国家电网有限公司"]

    projection = build_ragas_projection(snapshot, source_id="conn__ds-1")
    chunk_nodes = [n for n in projection["nodes"] if n["type"] == "chunk"]
    assert {e for n in chunk_nodes for e in n["properties"]["entities"]} == {"国家电网有限公司"}
    # KB 级一行 -> 投影只有 1 个实体；旧口径会得到 2 个不同名字
    assert sum(len(n["properties"]["entities"]) for n in chunk_nodes) == 2

    overlap_edges = [r for r in projection["relationships"] if r["type"] == "entities_overlap"]
    assert len(overlap_edges) == 1
    assert overlap_edges[0]["properties"]["overlapped_items"] == [["国家电网有限公司", "国家电网有限公司"]]


def test_snapshot_records_entity_types_for_blacklist_policy():
    """快照要持久化实体类型，供建边黑名单对人名做豁免。"""
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    typed = [dict(kb_rows[0], type="PERSON")]
    snapshot, _ = fetch_snapshot(FakeClient(docs, chunks, typed), "ds-1")

    assert snapshot["entity_types"] == {"国家电网有限公司": "person"}


def test_entity_type_change_changes_projection_fingerprint():
    """只有实体类型变化（分块与实体列表都不变）时也要重建投影，否则旧边会被复用。"""
    from app.ragas_adapter.ragflow import _projection_fingerprint, fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    snapshot, _ = fetch_snapshot(FakeClient(docs, chunks, [dict(kb_rows[0], type="person")]), "ds-1")
    retyped = {**snapshot, "entity_types": {"国家电网有限公司": "org"}}

    assert _projection_fingerprint(snapshot) != _projection_fingerprint(retyped)


def test_snapshot_without_type_field_keeps_entity_types_empty():
    """老版本 KB 实体行没有 type 时，entity_types 为空（建边走小库兜底）。"""
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    snapshot, _ = fetch_snapshot(FakeClient(docs, chunks, kb_rows), "ds-1")

    assert snapshot["entity_types"] == {}


def test_incremental_sync_stays_idempotent():
    from app.ragas_adapter.ragflow import fetch_snapshot

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    first, _ = fetch_snapshot(client, "ds-1")
    second, delta = fetch_snapshot(client, "ds-1", previous=first)

    assert delta["changed"] is False
    assert second["source_hash"] == first["source_hash"]
