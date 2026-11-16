""" A1~A4 与  增量复用的回归测试。

不连真实 RAGFlow：分页用例用桩 `_get`，同步用例用 duck-typed fake client。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest


class FakeClient:
    """记录调用次数，便于断言“只重拉变化文档”。"""

    def __init__(self, docs: list[dict], chunks: dict[str, list[dict]], kb_rows: list[dict],
                 version: str | None = "0.27.1"):
        self.docs = docs
        self.chunks = chunks
        self.kb_rows = kb_rows
        self.version = version
        self.document_calls = 0
        self.chunk_calls: list[str] = []

    def list_documents(self, dataset_id: str) -> list[dict]:
        self.document_calls += 1
        return [dict(row) for row in self.docs]

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        self.chunk_calls.append(document_id)
        return [dict(row) for row in self.chunks.get(document_id, [])]

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": [{"entities": [dict(row) for row in self.kb_rows]}]}

    def get_version(self) -> str | None:
        return self.version

    def alteration(self, dataset_id: str) -> dict:
        return {}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage
    config.ensure_data_dirs()
    storage.init_storage()


def _two_docs():
    docs = [
        {"id": "d1", "name": "文档一.txt", "update_time": "2026-01-01", "status": "done", "chunk_count": 1},
        {"id": "d2", "name": "文档二.txt", "update_time": "2026-01-02", "status": "done", "chunk_count": 1},
    ]
    chunks = {
        "d1": [{"id": "c1", "content": "国家电网发布了公告"}],
        "d2": [{"id": "c2", "content": "国网宣布新计划"}],
    }
    kb_rows = [{"name": "国家电网有限公司", "source_chunk_ids": ["c1", "c2"]}]
    return docs, chunks, kb_rows


def _install_client(monkeypatch, client: FakeClient):
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)


def _sync(dataset_id: str, name: str = ""):
    result, _ = _sync_with_logs(dataset_id, name)
    return result


def _sync_with_logs(dataset_id: str, name: str = "") -> tuple[dict, list[str]]:
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task
    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources(dataset_id, None)  # 入口要求持有租约
    result = asyncio.run(rf.sync_ragflow(task, dataset_id, "http://127.0.0.1:9380", "key", name=name))
    return result, list(task.logs)


def _make_testset(dataset_id: str, kg_id: str, projection_version: str = "") -> str:
    """落一条引用指定投影的测试集，用于验证「被引用不删」。"""
    from app import storage

    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "引用旧投影的测试集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas", "source_dataset_id": dataset_id,
        "stale": False, "stale_sample_count": 0,
        "samples": [{"index": 0, "user_input": "问题", "source_chunk_ids": ["c1"], "stale": False}],
    }
    if projection_version:
        data["source_projection_version"] = projection_version
    path = storage.ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry.update(path=path, count=1)
    storage.ts_upsert(entry)
    return ts_id


# ---------- A1：分页上限 / 重复页检测 ----------

def _full_page(prefix: str) -> list[dict]:
    return [{"id": f"{prefix}{index}"} for index in range(100)]


def test_document_pagination_detects_repeated_page(monkeypatch, caplog):
    from app.ragas_adapter.ragflow import RAGFlowClient, RAGFlowError

    caplog.set_level(logging.WARNING)
    client = RAGFlowClient("http://127.0.0.1:9380", "key")
    # 服务端忽略 page 参数：每次返回完全相同的一整页
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"docs": _full_page("d")})
    with pytest.raises(RAGFlowError, match="重复页"):
        client.list_documents("ds")
    assert "重复" in caplog.text


def test_chunk_pagination_detects_repeated_page(monkeypatch):
    from app.ragas_adapter.ragflow import RAGFlowClient, RAGFlowError

    client = RAGFlowClient("http://127.0.0.1:9380", "key")
    monkeypatch.setattr(client, "_get", lambda path, params=None: {"chunks": _full_page("c")})
    with pytest.raises(RAGFlowError, match="重复页"):
        client.list_chunks("ds", "d1")


def test_pagination_stops_at_page_limit(monkeypatch, caplog):
    from app.ragas_adapter import ragflow as rf

    caplog.set_level(logging.WARNING)
    client = rf.RAGFlowClient("http://127.0.0.1:9380", "key")
    monkeypatch.setattr(rf, "_MAX_PAGES", 3)

    def fake_get(path, params=None):
        page = (params or {}).get("page", 1)
        return {"docs": [{"id": f"d{page}-{index}"} for index in range(100)]}

    monkeypatch.setattr(client, "_get", fake_get)
    with pytest.raises(rf.RAGFlowError, match="超过上限 3 页"):
        client.list_documents("ds")
    assert "上限" in caplog.text


def test_kb_entity_rows_have_upper_bound(monkeypatch):
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_MAX_ENTITY_ROWS", 2)
    with pytest.raises(rf.RAGFlowError, match="实体行数"):
        rf._flatten_templates({"templates": [{"entities": [
            {"name": "a"}, {"name": "b"}, {"name": "c"},
        ]}]})


# ---------- A2：指针提交成功后才标记 stale ----------

def test_pointer_commit_failure_keeps_samples_fresh(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-a2-commit"

    first = _sync(dataset)

    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "历史测试集", "created_at": storage.now_iso(),
        "kg_id": first["kg_id"], "mode": "ragas", "source_dataset_id": dataset,
        "stale": False, "stale_sample_count": 0,
        "samples": [{"index": 0, "user_input": "问题", "source_chunk_ids": ["c1"], "stale": False}],
    }
    path = storage.ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry.update(path=path, count=1)
    storage.ts_upsert(entry)

    docs[0]["update_time"] = "2026-01-03"
    chunks["d1"][0]["content"] = "国家电网发布了新公告"

    def commit_boom(*args, **kwargs):
        raise RuntimeError("模拟指针提交失败")

    monkeypatch.setattr(storage, "ragflow_snapshot_commit", commit_boom)
    with pytest.raises(RuntimeError, match="指针提交失败"):
        _sync(dataset)

    sample = storage.ts_read(ts_id)["samples"][0]
    assert sample["stale"] is False
    # 指针仍指向旧投影，新投影已回滚
    assert storage.ragflow_snapshot_current(dataset)[1]["projection_kg_id"] == first["kg_id"]
    projections = [
        row for row in storage.kg_list()
        if row.get("mode") == "ragflow-projection" and row.get("source_dataset_id") == dataset
    ]
    assert [row["id"] for row in projections] == [first["kg_id"]]


# ---------- A3：旧投影在指针切换后清理 ----------

def test_old_projection_is_removed_after_new_snapshot(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-a3-cleanup"

    first = _sync(dataset)
    old_id = first["kg_id"]
    old_path = Path(storage.kg_get(old_id)["kg_path"])

    docs[0]["update_time"] = "2026-01-04"
    chunks["d1"][0]["content"] = "国家电网发布了更新公告"
    second = _sync(dataset)

    assert second["kg_id"] != old_id
    assert second["removed_kg_ids"] == [old_id]
    assert storage.kg_get(old_id) is None
    assert storage.kg_read_json(old_id) is None
    assert storage.kg_get(second["kg_id"]) is not None
    assert not old_path.exists() or json.loads(old_path.read_text(encoding="utf-8")).get("_deleted") is True


def test_soft_deleted_old_projection_is_unreadable(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-a3-soft"

    first = _sync(dataset)
    old_id = first["kg_id"]
    old_path = Path(storage.kg_get(old_id)["kg_path"])

    docs[0]["update_time"] = "2026-01-05"
    chunks["d1"][0]["content"] = "国家电网发布了第二次公告"

    def deny_unlink(self, missing_ok=False):
        raise OSError("沙箱拦截 unlink")

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    second = _sync(dataset)

    # 物理删除被拦截时降级为 _deleted 标记，索引条目同时移除
    assert storage.kg_read_json(old_id) is None
    assert json.loads(old_path.read_text(encoding="utf-8"))["_deleted"] is True
    assert storage.kg_get(second["kg_id"]) is not None


# ---------- A4：同一 dataset 并发同步被任务租约拒绝 ----------

def test_concurrent_sync_same_dataset_is_rejected():
    """互斥已从模块内集合迁到任务租约（`storage.ragflow_sync_resources`）。

    直接调用 `sync_ragflow`（脚本 / 测试）不再有进程内互斥；只有经过
    `manager.create(type, resources)` 的入口才会互斥，资源键唯一定义处是
    `storage.ragflow_sync_resources(dataset_id, connection_id)`。
    """
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import ResourceBusyError, manager

    assert not hasattr(rf, "_sync_inflight") and not hasattr(rf, "_acquire_sync")

    dataset = "ds-a4-lock"
    resources = storage.ragflow_sync_resources(dataset, None)
    held = manager.create("ragflow_sync", resources)
    other = None
    try:
        with pytest.raises(ResourceBusyError, match="资源已有任务"):
            manager.create("ragflow_sync", storage.ragflow_sync_resources(dataset, None))
        # 另一个数据源不受影响
        other = manager.create("ragflow_sync", storage.ragflow_sync_resources("ds-a4-lock-other", None))
    finally:
        for task in (held, other):
            if task is None:
                continue
            task.status = "canceled"
            manager._release_resources(task)
            manager.tasks.pop(task.id, None)


# ---------- 增量复用与幂等 ----------

def test_second_sync_without_changes_reuses_projection(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-mar47-unchanged"

    first = _sync(dataset)
    kg_path = Path(storage.kg_get(first["kg_id"])["kg_path"])
    before = kg_path.read_bytes()

    second = _sync(dataset)

    assert second["reused"] is True
    assert second["kg_id"] == first["kg_id"]
    assert second["delta"]["changed"] is False
    assert kg_path.read_bytes() == before  # 投影字节级一致
    #  P1：元数据没变也要拉一次 chunk 列表做内容指纹校验（不重建投影）
    assert client.chunk_calls.count("d1") == 2
    assert client.chunk_calls.count("d2") == 2
    assert client.document_calls == 2


def test_metadata_only_change_does_not_rebuild_projection(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-mar47-metadata"

    first = _sync(dataset)
    kg_path = Path(storage.kg_get(first["kg_id"])["kg_path"])
    before = kg_path.read_bytes()

    # 只改 RAGFlow 的展示元数据，内容与实体都没变
    docs[0]["update_time"] = "2026-01-09"
    second = _sync(dataset)

    assert second["reused"] is True
    assert second["delta"]["changed"] is False
    assert kg_path.read_bytes() == before
    # 元数据变化的文档会重拉一次；两篇文档还会各为内容指纹校验重拉一次 chunk 列表，
    # 但都不会再生成一份新投影
    assert client.chunk_calls.count("d1") == 2
    assert client.chunk_calls.count("d2") == 2


def test_partial_change_refetches_only_changed_document(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-mar47-partial"

    first = _sync(dataset)
    docs[0]["update_time"] = "2026-01-06"
    chunks["d1"][0]["content"] = "国家电网发布了更新公告"
    second = _sync(dataset)

    assert second.get("reused") is None
    assert second["delta"]["changed_chunk_ids"] == ["c1"]
    assert second["delta"]["changed_doc_ids"] == ["d1"]
    assert client.chunk_calls.count("d1") == 2
    # d2 会为内容指纹校验重拉一次 chunk 列表，但内容签名一致、不重建投影
    assert client.chunk_calls.count("d2") == 2

    kg = storage.kg_read_json(second["kg_id"])
    chunk_nodes = {node["properties"]["source_chunk_id"]: node for node in kg["nodes"]
                   if node["type"] == "chunk"}
    assert chunk_nodes["c1"]["properties"]["page_content"] == "国家电网发布了更新公告"
    assert chunk_nodes["c2"]["properties"]["page_content"] == "国网宣布新计划"
    assert storage.kg_get(second["kg_id"]) is not None


def test_deleted_document_is_removed_from_projection(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-mar47-deleted"

    first = _sync(dataset)
    docs.pop(1)  # 删除文档二
    second = _sync(dataset)

    assert second["delta"]["deleted_doc_ids"] == ["d2"]
    assert second["delta"]["deleted_chunk_ids"] == ["c2"]
    kg = storage.kg_read_json(second["kg_id"])
    document_nodes = [node for node in kg["nodes"] if node["type"] == "document"]
    chunk_nodes = [node for node in kg["nodes"] if node["type"] == "chunk"]
    assert len(document_nodes) == 1
    # 阶段 3 契约：分块文档归属读 doc_id / source_ref，不再用已计划删除的 child 反查口径
    assert all(node["properties"].get("source_ref") != "d2" for node in chunk_nodes)
    assert all(str(node["properties"].get("doc_id", "")).startswith("rf-") for node in chunk_nodes)
    assert all(node["properties"].get("source_type") == "ragflow" for node in chunk_nodes)
    assert all(node["type"] != "child" or node["source"] in {n["id"] for n in document_nodes}
               for node in kg["relationships"])
    assert storage.kg_read_json(first["kg_id"]) is None  # 旧投影已清理


# ---------- 旧投影引用保护（止血，2026-11-04） ----------

def test_referenced_projection_is_retained_after_new_snapshot(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-retain-referenced"

    first, _ = _sync_with_logs(dataset)
    ts_id = _make_testset(dataset, first["kg_id"], first["snapshot_id"])

    docs[0]["update_time"] = "2026-01-07"
    chunks["d1"][0]["content"] = "国家电网发布了新公告（一期）"
    second, logs = _sync_with_logs(dataset)

    assert second["kg_id"] != first["kg_id"]
    assert second["retained_kg_ids"] == [first["kg_id"]]
    assert second["removed_kg_ids"] == []
    assert storage.kg_get(first["kg_id"]) is not None
    assert storage.kg_read_json(second["kg_id"]) is not None
    assert storage.ragflow_snapshot_current(dataset)[1]["projection_kg_id"] == second["kg_id"]
    done = [line for line in logs if "投影已保存" in line]
    assert done and "保留 1 个仍被测试集引用的旧投影" in done[-1]
    assert first["kg_id"] in done[-1] and ts_id in done[-1]


def test_unreferenced_projection_is_still_removed(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-retain-unreferenced"

    first = _sync(dataset)
    old_path = Path(storage.kg_get(first["kg_id"])["kg_path"])
    # 引用其它 dataset 投影的测试集，不应阻止本条 dataset 的清理
    _make_testset("ds-other", "kg-other-projection")

    docs[0]["update_time"] = "2026-01-08"
    chunks["d1"][0]["content"] = "国家电网发布了新公告（二期）"

    def deny_unlink(self, missing_ok=False):
        raise OSError("沙箱拦截 unlink")

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    second = _sync(dataset)

    assert second["removed_kg_ids"] == [first["kg_id"]]
    assert second["retained_kg_ids"] == []
    assert storage.kg_get(first["kg_id"]) is None
    # 软删兜底路径：物理删除被拦截时落 `_deleted` 标记，读路径同样取不到
    assert json.loads(old_path.read_text(encoding="utf-8"))["_deleted"] is True
    assert storage.kg_get(second["kg_id"]) is not None


def test_projection_is_removed_after_reference_is_dropped(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-retain-released"

    first = _sync(dataset)
    ts_id = _make_testset(dataset, first["kg_id"], first["snapshot_id"])
    docs[0]["update_time"] = "2026-01-09"
    chunks["d1"][0]["content"] = "国家电网发布了新公告（三期）"
    second = _sync(dataset)
    assert first["kg_id"] in second["retained_kg_ids"]

    storage.ts_delete(ts_id)  # 引用解除，不是永久豁免
    docs[0]["update_time"] = "2026-01-10"
    chunks["d1"][0]["content"] = "国家电网发布了新公告（四期）"
    third = _sync(dataset)

    assert set(third["removed_kg_ids"]) == {first["kg_id"], second["kg_id"]}
    assert third["retained_kg_ids"] == []
    assert storage.kg_get(first["kg_id"]) is None
    assert storage.kg_get(third["kg_id"]) is not None


def test_testset_scan_failure_keeps_old_projections(monkeypatch, caplog):
    from app import storage
    from app.ragas_adapter import ragflow as rf

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-retain-scan-fail"
    caplog.set_level(logging.WARNING)

    first = _sync(dataset)

    def boom(projections):
        raise RuntimeError("模拟测试集索引损坏")

    monkeypatch.setattr(rf, "_referenced_projection_ids", boom)
    docs[0]["update_time"] = "2026-01-11"
    chunks["d1"][0]["content"] = "国家电网发布了新公告（五期）"
    second, logs = _sync_with_logs(dataset)

    # 扫描失败不得让同步整体失败：新投影照常提交，旧投影保守保留
    assert second["removed_kg_ids"] == []
    assert second["retained_kg_ids"] == []
    assert storage.kg_get(first["kg_id"]) is not None
    assert storage.kg_get(second["kg_id"]) is not None
    assert "扫描测试集引用失败" in caplog.text
    assert any("旧投影清理已跳过" in line for line in logs)


# ----------  P1：chunk 正文变更（元数据未变）必须被发现 ----------

def test_chunk_content_change_without_metadata_bump_is_detected(monkeypatch):
    from app import storage

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-case-prov-content"

    first = _sync(dataset)
    assert client.chunk_calls.count("d1") == 1

    # 真实复现路径：只改 chunk 正文，文档 update_time / chunk_count 一律不动
    chunks["d1"][0]["content"] = "国家电网发布了新的公告正文"
    second, logs = _sync_with_logs(dataset)

    assert client.chunk_calls.count("d1") == 2  # 元数据未变也做了一次内容指纹校验
    assert "d1" in second["delta"]["content_changed_doc_ids"]
    assert second["delta"]["changed"] is True
    assert second.get("reused") is None  # 确实重建，而不是「复用旧投影」
    assert second["kg_id"] != first["kg_id"]
    assert storage.kg_get(second["kg_id"]) is not None
    # 返回结构里没有 reused 标记时，日志必须显式说明「为什么重建」
    done = [line for line in logs if "投影已保存" in line]
    assert done and "chunk 内容指纹校验发现 1 个文档正文变化" in done[-1]


def test_chunk_verify_budget_skip_is_explicit(monkeypatch, caplog):
    from app import storage
    from app.ragas_adapter import ragflow as rf

    caplog.set_level(logging.WARNING)
    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-case-prov-budget"

    first = _sync(dataset)
    monkeypatch.setattr(rf, "_CHUNK_VERIFY_MAX_CHUNKS", 0)

    chunks["d1"][0]["content"] = "国家电网发布了新的公告正文（预算跳过）"
    second = _sync(dataset)

    # 超预算时不额外拉 chunk，但必须在 delta 与日志里显式说明，不能静默漏检
    assert client.chunk_calls.count("d1") == 1
    assert second["delta"]["chunk_verify_skipped"]
    assert second["delta"]["content_changed_doc_ids"] == []
    assert second["kg_id"] == first["kg_id"]  # 复用旧投影（已知取舍）
    assert "内容指纹校验已跳过" in caplog.text


def test_chunk_verify_fetch_failure_aborts_sync(monkeypatch):
    from app import storage
    from app.ragas_adapter import ragflow as rf

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-case-prov-fetch-fail"

    first = _sync(dataset)

    def boom(dataset_id, document_id):
        raise rf.RAGFlowError("模拟 chunk 列表拉取失败")

    monkeypatch.setattr(client, "list_chunks", boom)
    with pytest.raises(rf.RAGFlowError, match="无法确认"):
        _sync(dataset)

    # 指针不动：不能把拉取失败当成「无变化」静默通过
    assert storage.ragflow_snapshot_current(dataset)[1]["projection_kg_id"] == first["kg_id"]


# ---------- projection_config 纳入复用条件 ----------
# 注意：ragflow.py 的阈值常量在 import 时由环境变量求值（`_OVERLAP_MIN_SCORE = _env_float(...)`），
# 所以运行时改 env 不会生效；这里 monkeypatch 模块属性，等效于「进程以该参数启动」。
# 用例结束由 pytest 自动还原。

def test_default_overlap_params_keep_legacy_config_and_reuse(monkeypatch):
    """参数等于存量投影的隐含参数时：projection_config 只有 entity_resolution，连续同步直接复用。"""
    from app import storage

    _use_legacy_overlap_params(monkeypatch)

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-case-src-default"

    first = _sync(dataset)
    entry = storage.kg_get(first["kg_id"])
    # 默认参数不写 overlap 字段 —— 否则一次性所有存量投影都会被判「配置不一致」
    assert entry["projection_config"] == {"entity_resolution": "disabled"}
    kg_path = Path(entry["kg_path"])
    before = kg_path.read_bytes()

    second = _sync(dataset)

    assert second["reused"] is True
    assert second["kg_id"] == first["kg_id"]
    assert kg_path.read_bytes() == before


@pytest.mark.parametrize("attr, value", [
    ("_OVERLAP_BLACKLIST_MIN_DF", 1),
    ("_OVERLAP_BLACKLIST_DF_RATIO", 0.10),
    ("_OVERLAP_MIN_SCORE", 0.5),
])
def test_overlap_param_drift_rebuilds_projection(monkeypatch, attr, value):
    """任一重叠参数偏离默认 → 复用条件不成立，必须重建投影并写入 overlap 明细。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = f"ds-case-src-drift-{attr}"
    _use_legacy_overlap_params(monkeypatch)

    first = _sync(dataset)
    old_id = first["kg_id"]
    assert storage.kg_get(old_id)["projection_config"] == {"entity_resolution": "disabled"}

    # 第二次同步前调参（等价于重启进程时换环境变量）
    monkeypatch.setattr(rf, attr, value)

    second = _sync(dataset)
    assert second.get("reused") is None  # 没有走复用分支
    assert second["kg_id"] != old_id

    expected_config = {
        "entity_resolution": "disabled",
        "overlap": {
            "min_score": rf._OVERLAP_MIN_SCORE,
            "blacklist_df_ratio": rf._OVERLAP_BLACKLIST_DF_RATIO,
            "blacklist_min_df": rf._OVERLAP_BLACKLIST_MIN_DF,
        },
    }
    entry = storage.kg_get(second["kg_id"])
    assert entry["projection_config"] == expected_config
    assert storage.kg_read_json(second["kg_id"])["projection_config"] == expected_config

    # 旧投影无测试集引用 → 按既有清理策略删除（有引用才保留，见 ）
    assert second["removed_kg_ids"] == [old_id]
    assert second["retained_kg_ids"] == []
    assert storage.kg_get(old_id) is None


def test_overlap_param_drift_keeps_referenced_old_projection(monkeypatch):
    """调参重建时，被测试集引用的旧投影仍按  保留。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-case-src-drift-retain"

    first = _sync(dataset)
    _make_testset(dataset, first["kg_id"], first["snapshot_id"])
    monkeypatch.setattr(rf, "_OVERLAP_MIN_SCORE", 0.5)

    second = _sync(dataset)

    assert second["kg_id"] != first["kg_id"]
    assert second["retained_kg_ids"] == [first["kg_id"]]
    assert second["removed_kg_ids"] == []
    assert storage.kg_get(first["kg_id"]) is not None


def _use_legacy_overlap_params(monkeypatch):
    """把建边参数设回存量投影的隐含参数：projection_config 不带 overlap 明细，与历史投影一致。

    默认最小频次已从 3 改为 5（新默认会写 overlap 明细、触发存量投影重建一次）；
    本模块测的是别的行为，固定成存量口径，避免两件事互相干扰。
    """
    from app.ragas_adapter import ragflow as rf

    for key, attr in (("min_score", "_OVERLAP_MIN_SCORE"), ("blacklist_df_ratio", "_OVERLAP_BLACKLIST_DF_RATIO"),
                      ("blacklist_min_df", "_OVERLAP_BLACKLIST_MIN_DF")):
        monkeypatch.setattr(rf, attr, rf._OVERLAP_LEGACY_PARAMS[key])


def test_new_default_min_df_rebuilds_legacy_projection_once_then_reuses(monkeypatch):
    """建边最小频次默认 3 → 5：存量投影（无 overlap 明细）下次同步重建一次，之后带明细、正常复用。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.routers.ragflow import _entity_settings_view

    assert rf._OVERLAP_BLACKLIST_MIN_DF == 5 and rf._OVERLAP_LEGACY_PARAMS["blacklist_min_df"] == 3
    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-min-df-upgrade"

    with monkeypatch.context() as m:  # 模拟旧版本建的存量投影
        m.setattr(rf, "_OVERLAP_BLACKLIST_MIN_DF", 3)
        legacy = _sync(dataset)
        assert storage.kg_get(legacy["kg_id"])["projection_config"] == {"entity_resolution": "disabled"}
    # 界面提前告诉用户「下次同步会重建」
    assert _entity_settings_view(dataset, None, {"entity_resolution": "disabled"})["pending_rebuild"] is True

    upgraded = _sync(dataset)
    assert upgraded.get("reused") is None and upgraded["kg_id"] != legacy["kg_id"]
    assert storage.kg_get(upgraded["kg_id"])["projection_config"]["overlap"]["blacklist_min_df"] == 5
    assert _entity_settings_view(dataset, None, {"entity_resolution": "disabled"})["pending_rebuild"] is False

    again = _sync(dataset)
    assert again["reused"] is True and again["kg_id"] == upgraded["kg_id"]


# ---------- stale 统计口径与并发删除 ----------

def test_still_stale_ids_keeps_partially_reconciled_testsets(monkeypatch):
    """部分回收（还剩 stale 样本）的测试集仍算「仍过期」；只有全部回收的才剔除。"""
    from app.ragas_adapter import ragflow as rf

    _use_legacy_overlap_params(monkeypatch)
    docs, chunks, kb_rows = _two_docs()
    client = FakeClient(docs, chunks, kb_rows)
    _install_client(monkeypatch, client)
    dataset = "ds-still-stale-partial"
    _sync(dataset)

    monkeypatch.setattr(rf, "mark_testsets_stale", lambda *a, **k: ["ts-full", "ts-partial"])
    monkeypatch.setattr(rf, "reconcile_testsets_stale", lambda *a, **k: [
        {"testset_id": "ts-full", "cleared": 1, "remaining": 0},
        {"testset_id": "ts-partial", "cleared": 1, "remaining": 2},
    ])
    client.chunks["d1"] = [{"id": "c1", "content": "国家电网发布了新公告"}]
    client.docs[0]["update_time"] = "2026-02-01"
    result = _sync(dataset)

    assert result.get("reused") is None
    assert result["stale_testset_ids"] == ["ts-full", "ts-partial"]
    assert result["still_stale_ids"] == ["ts-partial"]


def test_marking_stale_does_not_resurrect_concurrently_deleted_testset(monkeypatch):
    """置位在存储锁内「重读 → 判定 → 写回」：期间用户删除的测试集不会被写回来。

    桩在归属判定时另起线程删除该测试集。修复前判定与写回之间不持锁，删除先完成、
    随后的写回把文件与索引都重新写出；修复后删除线程要等写回结束，最终仍是已删除。
    """
    from app import storage
    from app.ragas_adapter import ragflow as rf

    ts_id = _make_testset("ds-race-delete", "kg-race-delete")
    original = rf._testset_belongs_to_source
    deleter: list[threading.Thread] = []

    def belongs_then_delete(data, *args, **kwargs):
        if data.get("id") == ts_id and not deleter:
            thread = threading.Thread(target=storage.ts_delete, args=(ts_id,))
            deleter.append(thread)
            thread.start()
            thread.join(timeout=0.3)  # 修复后删除被锁挡住，这里超时返回
        return original(data, *args, **kwargs)

    monkeypatch.setattr(rf, "_testset_belongs_to_source", belongs_then_delete)
    rf.mark_testsets_stale("ds-race-delete", "kg-race-delete", {"changed_chunk_ids": ["c1"]})
    deleter[0].join(timeout=5)

    assert storage.ts_get(ts_id) is None
    assert storage.ts_read(ts_id) is None
    assert not (storage.TESTSET_DIR / f"{ts_id}.json").exists()


def test_ts_update_skips_missing_and_falsy(monkeypatch):
    from app import storage

    assert storage.ts_update("ts-not-exist", lambda data: True) is None
    assert not (storage.TESTSET_DIR / "ts-not-exist.json").exists()

    ts_id = _make_testset("ds-ts-update", "kg-ts-update")
    assert storage.ts_update(ts_id, lambda data: data.update(name="不会写入")) is None  # 返回假值 → 不写
    assert storage.ts_read(ts_id)["name"] == "引用旧投影的测试集"
    assert storage.ts_update(ts_id, lambda data: data.update(name="已写入") or "ok") == "ok"
    assert storage.ts_read(ts_id)["name"] == "已写入"
    assert storage.ts_get(ts_id)["name"] == "已写入"
