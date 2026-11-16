"""仓储层读改写原语：并发不丢更新、改绑失败回滚、损坏文件不再静默。

对应架构演进阶段 1a-①：断点 meta / KG 条目 / 预设新建三处「锁内重读 → 改写 → 写回」，
以及数据源改绑的「跨文件按序写 + 逆序回滚」。全部用临时 DATA_DIR 与合成投影，
不连真实 RAGFlow、不碰仓库 data/；测试模块顶层不 import app（conftest 要先设 DATA_DIR）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


class _StubClient:
    """改绑的前置校验只问「新连接能看到哪些库」。"""

    def __init__(self, visible: list[str]):
        self.visible = visible

    def list_datasets(self) -> list[dict]:
        return [{"id": item} for item in self.visible]


def _run_threads(target, count: int) -> None:
    threads = [threading.Thread(target=target) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


# ---------- 断点 meta ----------

def test_ckpt_update_keeps_all_concurrent_updates():
    """多线程同时更新同一断点 meta：锁内重读写，最终计数不丢更新。"""
    from app import storage

    run_id = storage.new_id("run")
    storage.ckpt_save(run_id, {"run_id": run_id, "counter": 0})

    def bump(meta):
        meta["counter"] = int(meta.get("counter") or 0) + 1
        time.sleep(0.001)  # 拉大旧写法「读完再写」的窗口

    def worker():
        for _ in range(20):
            storage.ckpt_update(run_id, bump)

    _run_threads(worker, 8)
    assert storage.ckpt_read_meta(run_id)["counter"] == 160


def test_ckpt_update_does_not_resurrect_abandoned_checkpoint():
    """断点已被放弃（_deleted）时，更新不得把它写活。"""
    from app import storage

    run_id = storage.new_id("run")
    storage.ckpt_write_meta(run_id, {"run_id": run_id, "status": "running", "_deleted": True})
    called: list[int] = []

    def mutate(meta):
        called.append(1)
        meta["status"] = "running"

    assert storage.ckpt_update(run_id, mutate) is None
    assert called == []
    assert json.loads(storage.ckpt_meta_path(run_id).read_text(encoding="utf-8"))["_deleted"] is True


# ---------- KG 条目 ----------

def test_kg_update_keeps_all_concurrent_updates():
    from app import storage

    kg_id = storage.new_id("kg")
    path = storage.kg_write_json(kg_id, {"nodes": [], "relationships": []})
    storage.kg_upsert({"id": kg_id, "name": "并发条目", "kg_path": path,
                       "created_at": storage.now_iso(), "seq": 0})

    def bump(entry):
        entry["seq"] = int(entry.get("seq") or 0) + 1
        time.sleep(0.001)

    def worker():
        for _ in range(20):
            storage.kg_update(kg_id, bump)

    _run_threads(worker, 8)
    assert storage.kg_get(kg_id)["seq"] == 160


def test_kg_update_writes_projection_and_entry_under_one_lock():
    from app import storage

    kg_id = storage.new_id("kg")
    path = storage.kg_write_json(kg_id, {"nodes": [], "relationships": []})
    storage.kg_upsert({"id": kg_id, "name": "增强写回", "kg_path": path,
                       "created_at": storage.now_iso(), "node_count": 0})

    saved = storage.kg_update(
        kg_id,
        lambda entry: entry.update(node_count=2, enhanced_at="2026-11-12T00:00:00"),
        data={"nodes": [{"id": "a"}, {"id": "b"}], "relationships": []},
    )
    assert saved["node_count"] == 2
    assert storage.kg_get(kg_id)["enhanced_at"] == "2026-11-12T00:00:00"
    assert len(storage.kg_read_json(kg_id)["nodes"]) == 2


def test_kg_update_on_missing_entry_writes_nothing():
    """条目已被删除时不写盘、也不复活（增强结果写回的保护）。"""
    from app import storage

    kg_id = storage.new_id("kg")
    assert storage.kg_update(kg_id, lambda entry: entry.update(node_count=1),
                             data={"nodes": [1], "relationships": []}) is None
    assert storage.kg_get(kg_id) is None
    assert not (storage.KG_DIR / f"{kg_id}.json").exists()


# ---------- 预设 ----------

def test_create_preset_if_absent_only_one_wins():
    """同名并发新建：只有第一个成功，其余全部 PresetNameConflict（路由转 409）。"""
    from app import storage

    kind, name = "llm_testset", "并发同名预设"
    created: list[dict] = []
    conflicts: list[Exception] = []

    def worker():
        try:
            created.append(storage.create_preset_if_absent(
                {"kind": kind, "name": name, "model": "stub", "base_url": ""}))
        except storage.PresetNameConflict as exc:
            conflicts.append(exc)

    try:
        _run_threads(worker, 6)
        assert len(created) == 1
        assert len(conflicts) == 5
        assert "并发同名预设" in str(conflicts[0])
        assert [p["name"] for p in storage.list_presets(kind)].count(name) == 1
    finally:
        for preset in created:
            storage.delete_preset(preset["id"])


def test_set_default_preset_reports_corrupt_file():
    """同 kind 下存在坏文件时按损坏检测报错，不再静默跳过。"""
    from app import storage

    good = storage.save_preset({"kind": "llm_testset", "name": "正常预设", "model": "stub"})
    try:
        (storage._preset_dir("llm_testset") / "preset-llm_testset-broken1.json").write_bytes(
            b"{ not a json")
        with pytest.raises(storage.StorageCorruptionError):
            storage.set_default_preset(good["id"], "llm_testset")
    finally:
        storage.delete_preset(good["id"])


def test_ensure_has_default_reports_corrupt_file():
    from app import storage

    preset_id = "preset-llm_testset-broken2"
    (storage._preset_dir("llm_testset") / f"{preset_id}.json").write_bytes(b"{ broken")
    with pytest.raises(storage.StorageCorruptionError):
        storage._ensure_has_default("llm_testset", [{"id": preset_id, "kind": "llm_testset"}])


# ---------- 数据源改绑：按序写 + 逆序回滚 ----------

def _save_connection(conn_id: str, name: str) -> None:
    from app import storage

    storage.ragflow_save_connection({
        "id": conn_id, "name": name, "base_url": "http://127.0.0.1:9", "api_key": "stub-key",
    })


def _write_projection(dataset: str, kg_id: str, connection_id: str) -> str:
    """写一份带 `rf-` 身份的真实形状投影；返回改绑前该文档的 doc_id（供回滚断言用）。"""
    from app import storage
    from app.ragas_adapter.doc_identity import ragflow_document_identity

    source_id = storage.ragflow_source_id(connection_id, dataset)
    identity = ragflow_document_identity(source_id, "up-doc-1", "文档一.md")
    path = storage.kg_write_json(kg_id, {
        "nodes": [{"id": "n1", "type": "chunk",
                   "properties": {"page_content": "正文", "source_chunk_id": "c1", **identity}}],
        "relationships": [],
    })
    storage.kg_upsert({
        "id": kg_id, "name": "合成投影", "kg_path": path, "created_at": storage.now_iso(),
        "mode": "ragflow-projection", "source_dataset_id": dataset,
        "source_connection_id": connection_id,
        "source_id": source_id,
        "node_count": 1, "relationship_count": 0, "projection_version": kg_id,
    })
    return identity["doc_id"]


def _write_source(dataset: str, connection_id: str, kg_id: str) -> Path:
    from app import storage

    source_dir = storage.ragflow_source_dir(connection_id, dataset, create=True)
    snap = storage.ragflow_snapshot_write(dataset, "snap1", {"rows": []}, source_dir=source_dir)
    storage.ragflow_snapshot_commit(
        dataset, "snap1", snap, kg_id, "v1", source_dir=source_dir,
        connection_id=connection_id, source_id=storage.ragflow_source_id(connection_id, dataset))
    return source_dir


def _write_testset(dataset: str, connection_id: str, kg_id: str) -> str:
    from app import storage

    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "跟踪样本集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas", "is_mock": False, "language": "zh",
        "source_dataset_id": dataset, "source_connection_id": connection_id,
        "samples": [{"index": 0, "user_input": "问题", "reference": "答案",
                     "source_connection_id": connection_id}],
    }
    storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"},
                       "path": str(storage.TESTSET_DIR / f"{ts_id}.json"), "count": 1})
    return ts_id


def _prepare_rebind(dataset: str, old: str, new: str):
    from app import storage

    kg_id = storage.new_id("kg")
    _save_connection(new, "新连接")
    old_doc_id = _write_projection(dataset, kg_id, old)
    ts_id = _write_testset(dataset, old, kg_id)
    old_dir = _write_source(dataset, old, kg_id)
    return kg_id, ts_id, old_dir, old_doc_id


def test_rebind_rolls_back_when_kg_index_write_fails(monkeypatch):
    """改写投影这一步失败：目录、指针、KG 条目、测试集全部回到改绑前。"""
    from app import storage
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rw-kgfail", "conn-rw-kgfail-old", "conn-rw-kgfail-new"
    kg_id, ts_id, old_dir, old_doc_id = _prepare_rebind(dataset, old, new)
    original_pointer = json.loads((old_dir / "current.json").read_text(encoding="utf-8"))

    real_upsert = storage.kg_upsert
    calls = {"n": 0}

    def flaky_upsert(entry):
        calls["n"] += 1
        if calls["n"] == 1:  # 只让「改写投影」那一步失败，回滚时的写索引正常
            raise RuntimeError("模拟写索引失败")
        return real_upsert(entry)

    monkeypatch.setattr(storage, "kg_upsert", flaky_upsert)
    with pytest.raises(ValueError) as exc:
        rebind_source(new, dataset, old, _StubClient([dataset]))
    assert "已回滚" in str(exc.value)

    assert old_dir.exists()
    assert not storage.ragflow_source_dir(new, dataset).exists()
    pointer = json.loads((old_dir / "current.json").read_text(encoding="utf-8"))
    assert pointer["connection_id"] == old
    assert pointer["snapshot_id"] == original_pointer["snapshot_id"]
    assert storage.kg_get(kg_id)["source_connection_id"] == old
    assert storage.ts_read(ts_id)["source_connection_id"] == old
    # 投影 JSON 的 doc_id 也要回到改绑前的 rf- 身份（不是「JSON 已改写、索引已回滚」的半状态）
    restored_doc_id = storage.kg_read_json(kg_id)["nodes"][0]["properties"]["doc_id"]
    assert restored_doc_id == old_doc_id
    assert restored_doc_id != storage.ragflow_source_id(new, dataset)  # 不是新作用域下的 id


def test_rebind_reports_backup_when_rollback_itself_fails(monkeypatch):
    """回滚也失败时，错误信息必须带备份位置，供人工恢复。"""
    from app import storage
    from app.ragas_adapter.ragflow import rebind_source

    dataset, old, new = "ds-rw-rbfail", "conn-rw-rbfail-old", "conn-rw-rbfail-new"
    kg_id, _ts_id, old_dir, _old_doc_id = _prepare_rebind(dataset, old, new)

    def always_fail(entry):
        raise RuntimeError("索引不可写")

    monkeypatch.setattr(storage, "kg_upsert", always_fail)
    with pytest.raises(ValueError) as exc:
        rebind_source(new, dataset, old, _StubClient([dataset]))
    message = str(exc.value)
    assert "回滚未完成" in message
    assert "ragflow-sources" in message  # 备份目录在 .backups/ragflow-sources/ 下
    assert "备份恢复" in message
    assert "kg-json" in message  # 有 doc_id 改写时才会提示投影原文另存位置
    # 目录仍被搬回旧位置：回滚是逆序的，目录那一步最后执行
    assert old_dir.exists()
    assert kg_id  # 保留引用，便于失败时定位
