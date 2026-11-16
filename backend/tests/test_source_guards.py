"""两条守卫的回归：KG 删除前检查数据源指针；同步遇到失效旧投影按首次处理。

不连真实 RAGFlow（同步路径用 duck-typed fake client），不碰 data/ 里的现有数据。
测试模块顶层不 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


class _StubClient:
    """最小 RAGFlow 客户端：只提供 fetch_snapshot 需要的方法。"""

    def __init__(self, text: str):
        self.text = text

    def list_documents(self, dataset_id: str) -> list[dict]:
        return [{"id": "d1", "name": "守卫文档.txt", "update_time": "2026-01-01",
                 "status": "done", "chunk_count": 1}]

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return [{"id": "c1", "content": self.text}]

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": []}

    def get_version(self) -> str:
        return "1.0.0-rc3"

    def alteration(self, dataset_id: str) -> dict:
        return {}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _wait_task(client, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body.get("status") in ("succeeded", "failed", "canceled", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在时限内结束")


def _write_projection(kg_id: str) -> None:
    from app import storage

    path = storage.kg_write_json(kg_id, {"nodes": [], "relationships": []})
    storage.kg_upsert({
        "id": kg_id, "name": "守卫投影", "kg_path": path, "created_at": storage.now_iso(),
        "mode": "ragflow-projection", "source_dataset_id": "ds-guard", "source_connection_id": "conn-guard",
        "projection_version": "v1", "node_count": 0, "relationship_count": 0,
    })


def _write_pointer(kg_id: str, *, dataset_id: str, connection_id: str | None = None) -> None:
    """把某个数据源的当前指针指向 kg_id（覆盖已有指针）。"""
    from app import storage

    source_dir = storage.ragflow_source_dir(connection_id, dataset_id) if connection_id else None
    snapshot_id = f"snap-{dataset_id}"
    path = storage.ragflow_snapshot_write(dataset_id, snapshot_id, {"source_hash": "h"}, source_dir=source_dir)
    storage.ragflow_snapshot_commit(
        dataset_id, snapshot_id, path, kg_id, "v1", source_dir=source_dir,
        connection_id=connection_id,
        source_id=storage.ragflow_source_id(connection_id, dataset_id) if connection_id else None,
    )


def test_kg_delete_rejected_while_source_pointer_references_it():
    from app import storage

    kg_id = "kg-guard-new-layout"
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id="kb-guard-new", connection_id="conn-guard-new")

    refs = storage.ragflow_pointers_referencing_kg(kg_id)
    assert refs and refs[0]["dataset_id"] == "kb-guard-new"
    assert refs[0]["source_id"] == "conn-guard-new__kb-guard-new"

    with pytest.raises(storage.KGReferencedError) as err:
        storage.kg_delete(kg_id)
    assert kg_id in str(err.value) and "kb-guard-new" in str(err.value)
    # 拒绝删除：索引与文件都原样保留
    assert storage.kg_get(kg_id) is not None
    assert storage.kg_read_json(kg_id) is not None


def test_kg_delete_rejected_for_legacy_layout_pointer():
    """旧布局 data/ragflow/{dataset_id}/current.json 同样受守卫保护。"""
    from app import storage

    kg_id = "kg-guard-legacy-layout"
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id="kb-guard-legacy")

    assert storage.ragflow_pointers_referencing_kg(kg_id)[0]["dataset_id"] == "kb-guard-legacy"
    with pytest.raises(storage.KGReferencedError):
        storage.kg_delete(kg_id)


def test_kg_delete_succeeds_after_pointer_moves_off_it():
    from app import storage

    kg_id, other_id = "kg-guard-moved-off", "kg-guard-current"
    _write_projection(kg_id)
    _write_projection(other_id)
    _write_pointer(kg_id, dataset_id="kb-guard-move", connection_id="conn-guard-move")
    _write_pointer(other_id, dataset_id="kb-guard-move", connection_id="conn-guard-move")

    assert storage.kg_delete(kg_id) is True
    assert storage.kg_get(kg_id) is None
    assert storage.kg_read_json(other_id) is not None


def test_delete_route_returns_409_kg_in_use():
    from app import storage

    kg_id = "kg-guard-http"
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id="kb-guard-http", connection_id="conn-guard-http")
    with _client() as client:
        resp = client.delete(f"/api/kg/{kg_id}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "kg_in_use"
    assert kg_id in body["detail"]
    assert body["extra"]["sources"][0]["dataset_id"] == "kb-guard-http"


def test_delete_route_409_includes_connection_name():
    """409 明细要带连接名，用户才能认出「这是哪个库」；没有连接记录时返回 null 由前端退回 id。"""
    from app import storage

    conn_id = "conn-guard-named"
    storage.ragflow_save_connection({
        "id": conn_id, "name": "守卫连接", "base_url": "http://127.0.0.1:9380", "api_key": "k",
    })
    kg_id = "kg-guard-named"
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id="kb-guard-named", connection_id=conn_id)
    with _client() as client:
        resp = client.delete(f"/api/kg/{kg_id}")
    assert resp.status_code == 409
    source = resp.json()["extra"]["sources"][0]
    assert source["connection_name"] == "守卫连接"
    assert source["dataset_id"] == "kb-guard-named"
    assert "守卫连接/kb-guard-named" in resp.json()["detail"]


def test_delete_missing_kg_returns_404():
    """不存在的 kg id：带不带确认参数都按 404 处理，不写指针、不报 500。"""
    from app import storage

    with _client() as client:
        plain = client.delete("/api/kg/kg-guard-does-not-exist")
        with_detach = client.delete("/api/kg/kg-guard-does-not-exist",
                                   params={"detach_sources": "true"})
    assert plain.status_code == 404
    assert with_detach.status_code == 404
    assert storage.kg_get("kg-guard-does-not-exist") is None


def _write_testset(kg_id: str, dataset_id: str, connection_id: str) -> str:
    from app import storage

    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "投影失联回归", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas",
        "source_dataset_id": dataset_id, "source_connection_id": connection_id,
        "samples": [{"index": 0, "user_input": "问题", "reference": "答案",
                     "synthesizer_name": "SingleHopSpecificQuerySynthesizer",
                     "reference_contexts": ["守卫正文"]}],
    }
    path = storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"},
                       "path": path, "count": 1})
    return ts_id


def test_detach_delete_marks_testset_projection_missing_until_resync():
    """确认删除后：测试集读时推导为「投影失联」（不写落盘），重新同步接上新投影后自动恢复。"""
    from app import storage

    conn_id, dataset_id = "conn-guard-proj", "kb-guard-proj"
    kg_id, new_kg = "kg-guard-proj", "kg-guard-proj-new"
    storage.ragflow_save_connection({
        "id": conn_id, "name": "投影失联连接", "base_url": "http://127.0.0.1:9380", "api_key": "k",
    })
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id=dataset_id, connection_id=conn_id)
    ts_id = _write_testset(kg_id, dataset_id, conn_id)

    with _client() as client:
        resp = client.delete(f"/api/kg/{kg_id}", params={"detach_sources": "true"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["detached_sources"][0]["dataset_id"] == dataset_id

        detail = client.get(f"/api/testset/{ts_id}").json()
        assert detail["projection_missing"] is True
        # 不是连接级失联：连接仍在，不能让用户看到「已停止跟踪 + 改绑」
        assert detail["source_unbound"] is False
        listed = [row for row in client.get("/api/testset").json() if row["id"] == ts_id]
        assert listed and listed[0]["projection_missing"] is True

        # 读时推导：落盘记录不新增字段
        assert "projection_missing" not in storage.ts_read(ts_id)

        # 模拟下一次同步重建投影：指针指向新图后状态自动消失
        _write_projection(new_kg)
        _write_pointer(new_kg, dataset_id=dataset_id, connection_id=conn_id)
        healed = client.get(f"/api/testset/{ts_id}").json()
        assert healed["projection_missing"] is False
        assert healed["source_unbound"] is False


def test_detach_pointer_write_failure_rolls_back(monkeypatch):
    """指针逐个写、第 2 个失败：已写的第 1 个回滚，图谱不删，500 体说明卡在哪个 source。"""
    from app import storage

    kg_id = "kg-guard-atomic"
    _write_projection(kg_id)
    datasets = ("kb-atomic-a", "kb-atomic-b")
    for ds in datasets:
        _write_pointer(kg_id, dataset_id=ds, connection_id="conn-atomic")

    with _client() as client:
        original_write = storage._write_json
        calls = {"n": 0}

        def flaky_write(path, data, **kwargs):
            if path.name == "current.json":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("模拟磁盘写入失败")
            return original_write(path, data, **kwargs)

        monkeypatch.setattr(storage.base, "_write_json", flaky_write)
        resp = client.delete(f"/api/kg/{kg_id}", params={"detach_sources": "true"})
        monkeypatch.undo()

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error_code"] == "kg_pointer_reset_failed"
    assert body["extra"]["failed_source"]["position"] == 2
    assert body["extra"]["failed_source"]["source_id"] == "conn-atomic__kb-atomic-b"
    # 图谱未删除；两个指针都恢复成原值（没有留下「半切断」的数据源）
    assert storage.kg_get(kg_id) is not None
    for ds in datasets:
        pointer = storage.ragflow_snapshot_current(
            ds, source_dir=storage.ragflow_source_dir("conn-atomic", ds))[1]
        assert pointer["projection_kg_id"] == kg_id


def test_kg_delete_with_detach_resets_all_referencing_pointers():
    """同一 KG 被多个数据源引用时，确认删除要把每个 current.json 的投影 id 都置空。"""
    from app import storage

    kg_id = "kg-guard-detach-multi"
    _write_projection(kg_id)
    datasets = ("kb-detach-a", "kb-detach-b")
    for ds in datasets:
        _write_pointer(kg_id, dataset_id=ds, connection_id="conn-detach-multi")
    assert len(storage.ragflow_pointers_referencing_kg(kg_id)) == 2

    assert storage.kg_delete(kg_id, detach_sources=True) is True
    assert storage.kg_get(kg_id) is None
    for ds in datasets:
        source_dir = storage.ragflow_source_dir("conn-detach-multi", ds)
        snapshot, pointer = storage.ragflow_snapshot_current(ds, source_dir=source_dir)
        assert pointer is not None and pointer["projection_kg_id"] is None
        # 快照与 source 目录原样保留（下次同步仍可做增量对比）
        assert snapshot is not None and pointer["path"] and Path(pointer["path"]).exists()


def test_delete_route_detach_param_reports_reset_sources():
    from app import storage

    kg_id = "kg-guard-http-detach"
    _write_projection(kg_id)
    _write_pointer(kg_id, dataset_id="kb-guard-detach-http", connection_id="conn-guard-detach-http")
    with _client() as client:
        resp = client.delete(f"/api/kg/{kg_id}", params={"detach_sources": "true"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["detached_sources"][0]["dataset_id"] == "kb-guard-detach-http"
    assert storage.kg_get(kg_id) is None
    pointer = storage.ragflow_snapshot_current(
        "kb-guard-detach-http", source_dir=storage.ragflow_source_dir("conn-guard-detach-http", "kb-guard-detach-http"),
    )[1]
    assert pointer["projection_kg_id"] is None


def test_sync_rebuilds_projection_after_detached_delete(monkeypatch):
    """确认删除后指针已空：下一次同步按首次同步口径重建投影（不复用、不继承）。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: _StubClient("守卫正文"))

    dataset = "kb-guard-detach-sync"
    with _client() as client:
        first = _wait_task(client, client.post("/api/ragflow/sync", json={
            "dataset_id": dataset, "base_url": "http://127.0.0.1:9380", "api_key": "k",
        }).json()["task_id"])
        assert first["status"] == "succeeded", first
        first_kg = first["result"]["kg_id"]

        assert storage.kg_delete(first_kg, detach_sources=True) is True

        second = _wait_task(client, client.post("/api/ragflow/sync", json={
            "dataset_id": dataset, "base_url": "http://127.0.0.1:9380", "api_key": "k",
        }).json()["task_id"])
        assert second["status"] == "succeeded", second
        assert second["result"]["kg_id"] != first_kg
        changes = second["result"]["report"]["changes"]
        assert changes["reused_projection"] is False
        assert changes["inherited_chunks"] == 0 and changes["inherited_documents"] == 0
        pointer = storage.ragflow_snapshot_current(dataset)[1]
        assert pointer["projection_kg_id"] == second["result"]["kg_id"]


def test_sync_treats_missing_previous_projection_as_first_sync(monkeypatch):
    """指针残留失效旧投影：按首次同步处理，不拿死 id 认领无关测试集、不误标 stale。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    holder = {"text": "第一版正文"}
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: _StubClient(holder["text"]))

    dataset = "kb-guard-dead-projection"
    with _client() as client:
        first = _wait_task(client, client.post("/api/ragflow/sync", json={
            "dataset_id": dataset, "base_url": "http://127.0.0.1:9380", "api_key": "k",
        }).json()["task_id"])
        assert first["status"] == "succeeded", first
        first_kg = first["result"]["kg_id"]
        entry = storage.kg_get(first_kg)
        assert entry is not None
        kg_path = Path(entry["kg_path"])

        # 模拟索引条目与 KG 文件同时丢失（手工清理 / 损坏）
        index = [x for x in storage.kg_list() if x["id"] != first_kg]
        storage.KG_INDEX.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        kg_path.unlink()

        # 历史测试集：kg_id 命中已失效的旧投影，但来源数据集与本库无关
        ts_id = storage.new_id("ts")
        data = {
            "id": ts_id, "name": "无关来源测试集", "created_at": storage.now_iso(),
            "kg_id": first_kg, "mode": "ragas", "source_dataset_id": "kb-other-source",
            "samples": [{"index": 0, "user_input": "问题", "reference": "答案",
                         "reference_contexts": ["第一版正文"]}],
        }
        path = storage.ts_write(ts_id, data)
        storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"},
                           "path": path, "count": 1})

        holder["text"] = "第二版正文"
        second = _wait_task(client, client.post("/api/ragflow/sync", json={
            "dataset_id": dataset, "base_url": "http://127.0.0.1:9380", "api_key": "k",
        }).json()["task_id"])
        assert second["status"] == "succeeded", second
        assert any("已失效" in line and "按首次同步处理" in line for line in second["logs"])
        assert second["result"]["delta"]["changed_chunk_ids"] == ["c1"]

        after = storage.ts_read(ts_id)
        assert after is not None
        assert not after.get("stale")
        assert all(not sample.get("stale") for sample in after["samples"])
