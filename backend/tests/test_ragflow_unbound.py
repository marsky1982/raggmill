"""连接删除后的「已停止跟踪」标记、存量推导与重新绑定。

背景：连接只是凭据实体，删连接不删已同步的图谱/测试集；但引用它的测试集从此无法再跟随
该数据源增量同步。这里覆盖三件事：删除时显式置位、读取时对存量记录推导、重新绑定改归属。

全部用合成投影 + 临时 DATA_DIR，不连真实 RAGFlow、不碰 data/ 里的现有测试集。
测试模块顶层不 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


class _StubClient:
    """只回答「新连接能看到哪些库」，rebind 的前置校验只需要这个能力。"""

    def __init__(self, visible: list[str]):
        self.visible = visible

    def list_datasets(self) -> list[dict]:
        return [{"id": item} for item in self.visible]


def _save_connection(conn_id: str, name: str) -> None:
    from app import storage

    storage.ragflow_save_connection({
        "id": conn_id, "name": name, "base_url": "http://127.0.0.1:9", "api_key": "stub-key",
    })


def _write_projection(dataset: str, kg_id: str, connection_id: str,
                      identity: dict | None = None) -> None:
    from app import storage

    props = {"page_content": "正文", "source_chunk_id": "c1"}
    props.update(identity or {})
    nodes = [{"id": "n1", "type": "chunk", "properties": props}]
    path = storage.kg_write_json(kg_id, {"nodes": nodes, "relationships": []})
    storage.kg_upsert({
        "id": kg_id, "name": "合成投影", "kg_path": path, "created_at": storage.now_iso(),
        "mode": "ragflow-projection", "source_dataset_id": dataset,
        "source_connection_id": connection_id,
        "source_id": storage.ragflow_source_id(connection_id, dataset),
        "node_count": 1, "relationship_count": 0, "projection_version": kg_id,
    })


def _write_source(dataset: str, connection_id: str, kg_id: str) -> Path:
    from app import storage

    source_dir = storage.ragflow_source_dir(connection_id, dataset, create=True)
    snap = storage.ragflow_snapshot_write(dataset, "snap1", {"rows": []}, source_dir=source_dir)
    storage.ragflow_snapshot_commit(
        dataset, "snap1", snap, kg_id, "v1", source_dir=source_dir,
        connection_id=connection_id, source_id=storage.ragflow_source_id(connection_id, dataset))
    return source_dir


def _write_testset(dataset: str, connection_id: str, kg_id: str,
                   document_ids: list[str] | None = None) -> str:
    from app import storage

    ts_id = storage.new_id("ts")
    sample = {"index": 0, "user_input": "问题", "reference": "答案",
              "source_connection_id": connection_id}
    if document_ids is not None:
        sample["source_document_ids"] = list(document_ids)
    data = {
        "id": ts_id, "name": "跟踪样本集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas", "is_mock": False, "language": "zh",
        "source_dataset_id": dataset, "source_connection_id": connection_id,
        "samples": [sample],
    }
    storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"},
                       "path": str(storage.TESTSET_DIR / f"{ts_id}.json"), "count": 1})
    return ts_id


def test_delete_marks_referencing_testsets_and_keeps_connection_evidence():
    from app import storage
    from app.ragas_adapter.ragflow import mark_testsets_unbound

    conn_id, dataset = "conn-unbound-del", "ds-unbound-del"
    kg_id = storage.new_id("kg")
    _save_connection(conn_id, "待删连接")
    _write_projection(dataset, kg_id, conn_id)
    ts_id = _write_testset(dataset, conn_id, kg_id)

    assert storage.ragflow_delete_connection(conn_id)
    marked = mark_testsets_unbound(conn_id)
    assert [row["testset_id"] for row in marked] == [ts_id]

    data = storage.ts_read(ts_id)
    assert data["source_unbound"] is True
    assert data["source_unbound_reason"] == "connection_deleted"
    assert data["source_connection_id"] == conn_id, "原连接 id 必须保留作为「曾绑定」的证据"
    entry = next(item for item in storage.ts_list() if item["id"] == ts_id)
    assert entry["source_unbound"] is True


def test_tracking_state_derives_missing_connection_for_existing_records():
    """存量记录（连接早已被删、从未写过标记）：读取时按连接是否存在推导。"""
    from app.ragas_adapter.ragflow import testset_tracking_state

    _save_connection("conn-live-state", "在用连接")
    assert testset_tracking_state({"source_connection_id": "conn-live-state"})["unbound"] is False
    assert testset_tracking_state({"source_connection_id": "conn-gone-state"})["unbound"] is True
    assert testset_tracking_state({"source_connection_id": "conn-gone-state"})["reason"] == "connection_missing"
    # 没有连接 id 的老记录（本地文档生成）不参与判断
    assert testset_tracking_state({"source_connection_id": None})["unbound"] is False
    # 显式标记优先，reason 保留写入时的语义
    state = testset_tracking_state({"source_connection_id": "conn-gone-state",
                                    "source_unbound": True,
                                    "source_unbound_reason": "connection_deleted"})
    assert state["reason"] == "connection_deleted"


def test_rebind_moves_source_and_rewrites_records():
    from app import storage
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rebind-move", "conn-rebind-old", "conn-rebind-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    _write_projection(dataset, kg_id, old)
    ts_id = _write_testset(dataset, old, kg_id)
    old_dir = _write_source(dataset, old, kg_id)

    result = rebind_source(new, dataset, old, _StubClient([dataset]))
    new_dir = storage.ragflow_source_dir(new, dataset)
    assert new_dir.exists() and not old_dir.exists()
    pointer = json.loads((new_dir / "current.json").read_text(encoding="utf-8"))
    assert pointer["connection_id"] == new
    assert pointer["source_id"] == result["source_id"]
    assert Path(pointer["path"]).is_relative_to(new_dir)
    assert Path(result["backup"]).exists(), "改绑前必须先备份旧目录"

    data = storage.ts_read(ts_id)
    assert data["source_connection_id"] == new
    assert "source_unbound" not in data
    assert data["samples"][0]["source_connection_id"] == new
    entry = next(item for item in storage.kg_list() if item["id"] == kg_id)
    assert entry["source_connection_id"] == new and entry["source_id"] == result["source_id"]


def test_rebind_rewrites_projection_and_sample_identity():
    """改绑换了 source_id：投影的 rf- 与样本 source_document_ids 一起改到新值，原文留备份。"""
    from app import storage
    from app.ragas_adapter.doc_identity import ragflow_document_identity
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rebind-identity", "conn-rb-id-old", "conn-rb-id-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    old_sid = storage.ragflow_source_id(old, dataset)
    new_sid = storage.ragflow_source_id(new, dataset)
    old_identity = ragflow_document_identity(old_sid, "up-doc-1", "说明.md")
    _write_projection(dataset, kg_id, old, identity={**old_identity, "summary": "已付费摘要"})
    ts_id = _write_testset(dataset, old, kg_id, document_ids=[old_identity["doc_id"]])
    _write_source(dataset, old, kg_id)

    result = rebind_source(new, dataset, old, _StubClient([dataset]))

    new_doc_id = ragflow_document_identity(new_sid, "up-doc-1", "说明.md")["doc_id"]
    assert new_doc_id != old_identity["doc_id"]
    props = storage.kg_read_json(kg_id)["nodes"][0]["properties"]
    assert props["doc_id"] == new_doc_id
    assert props["summary"] == "已付费摘要", "只改 doc_id，增强产物不能丢"
    assert props["source_ref"] == "up-doc-1"
    assert props["source_type"] == "ragflow"
    assert props["doc_name"] == "说明.md"
    assert storage.ts_read(ts_id)["samples"][0]["source_document_ids"] == [new_doc_id]
    assert result["identity_rewrite"] == {
        "documents": 1, "projection_kg_ids": [kg_id], "samples": 1}

    # 可还原：改绑前的投影原文落在备份目录
    original = json.loads(
        (Path(result["backup"]) / "kg-json" / f"{kg_id}.json").read_text(encoding="utf-8"))
    assert original["nodes"][0]["properties"]["doc_id"] == old_identity["doc_id"]


def test_rebind_rollback_restores_projection_identity(monkeypatch):
    """改绑中途失败：投影 doc_id 与样本 source_document_ids 都必须回到旧值。"""
    from app import storage
    from app.ragas_adapter.doc_identity import ragflow_document_identity
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rebind-id-rb", "conn-rb-idrb-old", "conn-rb-idrb-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    old_identity = ragflow_document_identity(
        storage.ragflow_source_id(old, dataset), "up-doc-1", "说明.md")
    _write_projection(dataset, kg_id, old, identity=old_identity)
    ts_id = _write_testset(dataset, old, kg_id, document_ids=[old_identity["doc_id"]])
    _write_source(dataset, old, kg_id)

    real_write = storage.ts_write
    calls = {"n": 0}

    def flaky_write(write_id, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟写测试集失败")
        return real_write(write_id, data)

    monkeypatch.setattr(storage, "ts_write", flaky_write)
    with pytest.raises(ValueError) as exc:
        rebind_source(new, dataset, old, _StubClient([dataset]))
    assert "已回滚" in str(exc.value)

    assert storage.kg_read_json(kg_id)["nodes"][0]["properties"]["doc_id"] == old_identity["doc_id"]
    assert storage.ts_read(ts_id)["samples"][0]["source_document_ids"] == [old_identity["doc_id"]]


def test_rebind_rejects_when_new_connection_cannot_see_dataset():
    from app import storage
    from app.ragas_adapter.ragflow import RAGFlowHTTPError, rebind_source

    dataset, old, new = "ds-rebind-invisible", "conn-rb-inv-old", "conn-rb-inv-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    _write_projection(dataset, kg_id, old)
    _write_source(dataset, old, kg_id)

    with pytest.raises(RAGFlowHTTPError) as exc:
        rebind_source(new, dataset, old, _StubClient(["ds-other"]))
    assert exc.value.error_code == "dataset_not_found"
    assert storage.ragflow_source_dir(old, dataset).exists(), "前置校验失败时不得移动目录"


def _enhanced_snapshot(dataset: str) -> dict:
    """两篇分块同属一篇文档的快照，用来验证文档级（按 doc_id 分组）的增强继承。"""
    return {
        "dataset_id": dataset,
        "documents": {"up-doc-1": {"name": "说明.md"}},
        "chunks": {
            "up-c1": {"id": "up-c1", "document_id": "up-doc-1", "page_content": "正文一",
                      "position": 0, "entities": ["实体A"]},
            "up-c2": {"id": "up-c2", "document_id": "up-doc-1", "page_content": "正文二",
                      "position": 1, "entities": ["实体B"]},
        },
    }


def test_rebind_keeps_document_enhancement_inheritable():
    """改绑重写 doc_id 后，下一次真重建的文档级增强仍能继承（不重新生成摘要与向量）。

    这是改绑身份重写的核心目的：改绑只换 source_id，若投影里的旧 doc_id 不跟着换，
    重建后按 doc_id 分组的文档级继承会 100% 落空、整库摘要与向量重付费。
    """
    from app import storage
    from app.ragas_adapter.ragflow import (
        build_ragas_projection,
        inherit_enhancement,
        rebind_source,
    )

    dataset, old, new = "ds-rebind-inherit", "conn-rb-inh-old", "conn-rb-inh-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    snapshot = _enhanced_snapshot(dataset)
    projection = build_ragas_projection(
        snapshot, source_id=storage.ragflow_source_id(old, dataset))
    # 增强产物已写回投影（出题前的增强步骤落在这几个属性上）
    for node in projection["nodes"]:
        props = node.setdefault("properties", {})
        props["summary"] = f"{node['type']} 摘要"
        props["themes"] = ["主题"]
        props["summary_embedding"] = [0.1, 0.2]
    path = storage.kg_write_json(kg_id, projection)
    storage.kg_upsert({
        "id": kg_id, "name": "增强过的投影", "kg_path": path, "created_at": storage.now_iso(),
        "mode": "ragflow-projection", "source_dataset_id": dataset,
        "source_connection_id": old,
        "source_id": storage.ragflow_source_id(old, dataset),
        "node_count": len(projection["nodes"]),
        "relationship_count": len(projection["relationships"]),
        "projection_version": kg_id,
    })
    _write_source(dataset, old, kg_id)

    result = rebind_source(new, dataset, old, _StubClient([dataset]))
    assert result["identity_rewrite"]["documents"] == 1

    rebuilt = build_ragas_projection(
        snapshot, source_id=storage.ragflow_source_id(new, dataset))
    inherited = inherit_enhancement(rebuilt, storage.kg_read_json(kg_id))
    assert inherited == {"chunks": 2, "documents": 1}
    document = next(node for node in rebuilt["nodes"] if node["type"] == "document")
    assert document["properties"]["summary"] == "document 摘要"
    assert document["properties"]["summary_embedding"] == [0.1, 0.2]

    # 反向验证：这份重建投影如果直接对比「未改写的旧投影」，文档级增强会全部落空
    original = json.loads(
        (Path(result["backup"]) / "kg-json" / f"{kg_id}.json").read_text(encoding="utf-8"))
    assert inherit_enhancement(
        build_ragas_projection(snapshot, source_id=storage.ragflow_source_id(new, dataset)),
        original)["documents"] == 0


def test_rebind_rolls_back_when_pointer_commit_fails(monkeypatch):
    from app import storage
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rebind-rollback", "conn-rb-rb-old", "conn-rb-rb-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    _write_projection(dataset, kg_id, old)
    ts_id = _write_testset(dataset, old, kg_id)
    old_dir = _write_source(dataset, old, kg_id)
    original_pointer = json.loads((old_dir / "current.json").read_text(encoding="utf-8"))

    def boom(*args, **kwargs):
        raise RuntimeError("模拟写指针失败")

    monkeypatch.setattr(storage, "ragflow_snapshot_commit", boom)
    with pytest.raises(ValueError) as exc:
        rebind_source(new, dataset, old, _StubClient([dataset]))
    assert "已回滚" in str(exc.value)
    assert old_dir.exists() and not storage.ragflow_source_dir(new, dataset).exists()
    pointer = json.loads((old_dir / "current.json").read_text(encoding="utf-8"))
    assert pointer["connection_id"] == old
    assert pointer["snapshot_id"] == original_pointer["snapshot_id"]
    assert storage.ts_read(ts_id)["source_connection_id"] == old
    assert next(e for e in storage.kg_list() if e["id"] == kg_id)["source_connection_id"] == old


def test_rebind_is_rejected_while_source_is_busy():
    """数据源上有出题 / 同步任务占着资源键时，重新绑定返回 409，目录与记录不动、也不做备份。"""
    from app import config, storage
    from app.ragas_adapter.ragflow import rebind_source
    from app.tasks.manager import ResourceBusyError, manager

    dataset, old, new = "ds-rebind-busy", "conn-rb-busy-old", "conn-rb-busy-new"
    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    _write_projection(dataset, kg_id, old)
    ts_id = _write_testset(dataset, old, kg_id)
    old_dir = _write_source(dataset, old, kg_id)
    backups_before = set((config.BACKUP_DIR / "ragflow-sources").glob(f"{old_dir.name}-*"))

    busy = manager.create("testset", storage.ragflow_sync_resources(dataset, old))  # 正在出题
    try:
        with pytest.raises(ResourceBusyError):
            rebind_source(new, dataset, old, _StubClient([dataset]))
    finally:
        manager.complete(busy, "canceled")

    assert old_dir.exists() and not storage.ragflow_source_dir(new, dataset).exists()
    assert storage.ts_read(ts_id)["source_connection_id"] == old
    assert set((config.BACKUP_DIR / "ragflow-sources").glob(f"{old_dir.name}-*")) == backups_before

    # 任务结束、锁释放后可以正常改绑；改绑自身也会放锁
    rebind_source(new, dataset, old, _StubClient([dataset]))
    assert storage.ragflow_source_dir(new, dataset).exists()
    assert not (set(storage.ragflow_sync_resources(dataset, new)) & manager.busy_resources())
