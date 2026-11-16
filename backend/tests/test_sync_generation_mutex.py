"""R-1 / S3 / N10①：同步、出题、删除 KG 之间的互斥。

- 出题任务对 RAGFlow 投影占用数据源键：生成期间同一数据源不能再起同步（否则切指针 + 清理旧投影会删掉
  正在出题的那份投影，测试集断链，增强写回还会把它「复活」）。
- 删除 KG 前检查 `kg:{id}` 是否被任务占用。
- 旧投影清理把「运行中任务正在用的 kg」也算作被引用（第二道保险）。
- legacy 数据源的旧 /sync 与连接式入口共用同一组键。
不连真实 RAGFlow、不跑 LLM。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


@contextmanager
def _holding(type_: str, resources: tuple[str, ...]):
    """占住一组资源键，模拟正在运行的任务；退出时按终态释放租约。"""
    from app.tasks.manager import manager

    task = manager.create(type_, resources)
    try:
        yield task
    finally:
        task.status = "canceled"
        manager._release_resources(task)
        manager.tasks.pop(task.id, None)


def _projection_entry(kg_id: str, dataset: str, connection_id: str | None) -> dict:
    from app import storage

    path = storage.kg_write_json(kg_id, {"nodes": [], "relationships": []})
    entry = {"id": kg_id, "name": kg_id, "kg_path": path, "mode": "ragflow-projection",
             "source_dataset_id": dataset, "created_at": storage.now_iso()}
    if connection_id:
        entry["source_connection_id"] = connection_id
    storage.kg_upsert(entry)
    return entry


def test_sync_resources_single_definition():
    from app import storage

    assert storage.ragflow_sync_resources("ds1", "conn-a") == ("conn-a__ds1",)
    legacy = storage.ragflow_sync_resources("ds1", None)
    assert legacy == ("ragflow:ds1", storage.ragflow_source_id(storage.legacy_connection_id("ds1"), "ds1"))
    entry = {"mode": "ragflow-projection", "source_dataset_id": "ds1", "source_connection_id": "conn-a"}
    assert storage.ragflow_projection_resources(entry) == ("conn-a__ds1",)
    assert storage.ragflow_projection_resources({"mode": "ragas"}) == ()


def test_connection_sync_blocked_while_generating_on_its_projection():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    storage.ragflow_save_connection({"id": "conn-mutex", "name": "互斥", "base_url": "http://127.0.0.1:9380",
                                     "api_key": "key"})
    entry = _projection_entry("kg-mutex-a", "ds-mutex", "conn-mutex")
    gen_resources = ("ragas-generation", "kg:kg-mutex-a", *storage.ragflow_projection_resources(entry))
    with TestClient(app) as client, _holding("generate_testset", gen_resources):
        resp = client.post("/api/ragflow/connections/conn-mutex/datasets/ds-mutex/sync", json={})
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "sync_in_progress"


def test_generate_route_blocked_while_source_is_syncing(monkeypatch):
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app
    from app.routers import testset as ts_router

    _projection_entry("kg-mutex-b", "ds-mutex-b", "conn-mutex-b")
    llm = storage.save_preset({"kind": "llm_testset", "name": "互斥 LLM", "base_url": "http://127.0.0.1:1",
                               "model": "m", "api_key": "k"})
    emb = storage.save_preset({"kind": "embedding", "name": "互斥 Emb", "base_url": "http://127.0.0.1:1",
                               "model": "m", "api_key": "k"})
    # 空投影过不了能力判定；这里只验证资源互斥，跳过判定
    monkeypatch.setattr(ts_router, "check_generation_capabilities", lambda *a, **k: None)
    with TestClient(app) as client, _holding("ragflow_sync",
                                             storage.ragflow_sync_resources("ds-mutex-b", "conn-mutex-b")):
        resp = client.post("/api/testset/generate", json={
            "kg_id": "kg-mutex-b", "llm_preset_id": llm["id"], "embedding_preset_id": emb["id"],
            "testset_size": 1})
    assert resp.status_code == 409, resp.text


def test_kg_delete_rejected_while_in_use():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    _projection_entry("kg-mutex-del", "ds-mutex-del", "conn-x")
    with TestClient(app) as client:
        with _holding("generate_testset", ("ragas-generation", "kg:kg-mutex-del")):
            busy = client.delete("/api/kg/kg-mutex-del")
        assert busy.status_code == 409
        assert storage.kg_get("kg-mutex-del") is not None
        ok = client.delete("/api/kg/kg-mutex-del")
    assert ok.status_code == 200


def test_cleanup_keeps_projection_used_by_running_task():
    from app import storage
    from app.ragas_adapter.ragflow import _cleanup_old_projections

    _projection_entry("kg-old-inuse", "ds-clean", "conn-clean")
    _projection_entry("kg-old-free", "ds-clean", "conn-clean")
    _projection_entry("kg-new", "ds-clean", "conn-clean")
    with _holding("generate_testset", ("kg:kg-old-inuse",)):
        result = _cleanup_old_projections("ds-clean", "kg-new", "conn-clean")
    assert result["removed"] == ["kg-old-free"]
    assert [r["kg_id"] for r in result["retained"]] == ["kg-old-inuse"]
    assert result["retained"][0]["in_use_by_task"] is True
    assert storage.kg_get("kg-old-inuse") is not None
