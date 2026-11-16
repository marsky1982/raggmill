"""RAGFlow 手动增量同步迁入 StagedJob 后的行为回归（架构演进阶段 2）。

覆盖：五阶段顺序、失败不落盘、取消、任务租约接管、资源占用 409、兼容别名 SSE 断线续传。
不连真实 RAGFlow：同步链路用 duck-typed fake client，HTTP 只用 TestClient。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest


class FakeClient:
    """只提供同步链路真正用到的方法。"""

    def __init__(self, docs: list[dict], chunks: dict[str, list[dict]]):
        self.docs = docs
        self.chunks = chunks

    def list_documents(self, dataset_id: str) -> list[dict]:
        return [dict(row) for row in self.docs]

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return [dict(row) for row in self.chunks.get(document_id, [])]

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": [{"entities": []}]}

    def get_version(self) -> str | None:
        return "1.0.0"

    def alteration(self, dataset_id: str) -> dict:
        return {}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage
    config.ensure_data_dirs()
    storage.init_storage()


def _doc(doc_id: str, name: str) -> dict:
    return {"id": doc_id, "name": name, "update_time": "2026-01-01", "status": "done", "chunk_count": 1}


def _install(monkeypatch, client: FakeClient) -> None:
    from app.ragas_adapter import ragflow as rf
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)


def _release(task) -> None:
    from app.tasks.manager import manager

    task.status = "canceled"
    manager._release_resources(task)
    manager.tasks.pop(task.id, None)


def _lease_task(dataset_id: str, connection_id: str | None = None):
    """构造已持有同步租约的任务：等价于 `manager.create` 的产物，但不动真实任务表。"""
    from app import storage
    from app.tasks.manager import Task

    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources(dataset_id, connection_id)
    return task


def test_sync_rejects_caller_without_lease(monkeypatch):
    """绕过任务直接调用：没有该数据源的租约键，入口直接报错，不静默并发。"""
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    _install(monkeypatch, FakeClient([_doc("d1", "无租约文档.txt")], {"d1": [{"id": "c1", "content": "正文"}]}))
    with pytest.raises(rf.SyncLeaseError, match="缺少数据源任务租约"):
        asyncio.run(rf.sync_ragflow(Task("ragflow_sync"), "kb-staged-no-lease", "http://127.0.0.1:9380", "key"))
    # 别的数据源的租约不算数
    wrong = _lease_task("kb-staged-other")
    with pytest.raises(rf.SyncLeaseError):
        asyncio.run(rf.sync_ragflow(wrong, "kb-staged-no-lease", "http://127.0.0.1:9380", "key"))


def test_rebind_rejects_caller_without_lease(monkeypatch):
    """改绑入口走同一检查：任务没占资源键时直接报错。"""
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    monkeypatch.setattr(rf.manager, "create", lambda *a, **k: Task("ragflow_rebind"))
    with pytest.raises(rf.SyncLeaseError, match="改绑缺少数据源任务租约"):
        rf.rebind_source("conn-new", "kb-staged-rebind-lease", "conn-old", client=None)


def test_sync_and_rebind_lease_check_passes_for_created_task():
    """正常路径：manager.create 占好租约后，同一份键通过检查（同步与改绑共用一个入口）。"""
    from app import storage
    from app.ragas_adapter.ragflow import _require_sync_lease
    from app.tasks.manager import manager

    keys = storage.ragflow_sync_resources("kb-staged-lease-ok", None)
    task = manager.create("ragflow_sync", keys)
    try:
        _require_sync_lease(task, keys)
        # 改绑另占一个数据源（同一份键还在上面的任务手里，不能重复创建）
        rebind_keys = storage.ragflow_sync_resources("kb-staged-lease-rebind", "conn-new")
        rebind_task = manager.create("ragflow_rebind", rebind_keys)
        try:
            _require_sync_lease(rebind_task, rebind_keys, operation="改绑")
        finally:
            _release(rebind_task)
    finally:
        _release(task)


def test_staged_sync_runs_five_stages_in_order(monkeypatch):
    from app.ragas_adapter import ragflow as rf

    _install(monkeypatch, FakeClient([_doc("d1", "顺序文档.txt")], {"d1": [{"id": "c1", "content": "正文"}]}))
    task = _lease_task("kb-staged-order")
    result = asyncio.run(rf.sync_ragflow(task, "kb-staged-order", "http://127.0.0.1:9380", "key"))

    assert result["kg_id"]
    starts = [line for line in task.logs if line.startswith("▶ 阶段「")]
    assert starts == [
        "▶ 阶段「fetching」开始",
        "▶ 阶段「entity_resolution」开始",
        "▶ 阶段「projection」开始",
        "▶ 阶段「inherit」开始",
        "▶ 阶段「commit」开始",
    ]
    # 五阶段均分进度：进入第 5 阶段即 4/5，阶段标签停在最后一阶段
    assert "✓ 阶段「commit」完成" in task.logs
    assert task.stage == "commit"
    assert task.progress == pytest.approx(0.8)


def test_projection_reuse_ignores_policy(monkeypatch):
    """执行参数（超时/重试/并发）不进投影指纹：换 policy 再同步仍走复用，不重建。"""
    from app.policies import GenerationPolicy
    from app.ragas_adapter import ragflow as rf

    _install(monkeypatch, FakeClient([_doc("d1", "复用文档.txt")], {"d1": [{"id": "c1", "content": "正文"}]}))
    dataset = "kb-staged-reuse"
    first = asyncio.run(rf.sync_ragflow(
        _lease_task(dataset), dataset, "http://127.0.0.1:9380", "key",
        policy=GenerationPolicy(max_retries=1, llm_timeout=30, max_workers=2)))
    second = asyncio.run(rf.sync_ragflow(
        _lease_task(dataset), dataset, "http://127.0.0.1:9380", "key",
        policy=GenerationPolicy(max_retries=9, llm_timeout=900, max_workers=16)))

    assert second.get("reused") is True
    assert second["kg_id"] == first["kg_id"]


def test_sync_request_models_inherit_generation_policy():
    """两个同步请求体都继承 GenerationPolicy：执行参数可从请求调，默认值不变。"""
    from app.policies import GenerationPolicy
    from app.routers.ragflow import ConnectionSyncIn, SyncIn, _policy_of

    assert issubclass(SyncIn, GenerationPolicy) and issubclass(ConnectionSyncIn, GenerationPolicy)
    assert _policy_of(SyncIn(dataset_id="ds")) == GenerationPolicy()
    assert _policy_of(ConnectionSyncIn()) == GenerationPolicy()
    tuned = _policy_of(ConnectionSyncIn(max_retries=1, llm_timeout=30, max_workers=2))
    assert (tuned.max_retries, tuned.llm_timeout, tuned.max_workers) == (1, 30, 2)


def test_projection_failure_leaves_no_snapshot_or_kg(monkeypatch):
    """指针提交前的任何阶段失败都不写快照 / 投影 / 指针。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    conn = storage.ragflow_save_connection(
        {"name": "失败用连接", "base_url": "http://fail.local:9380", "api_key": "key"})
    dataset = "kb-staged-fail"
    _install(monkeypatch, FakeClient([_doc("d1", "失败文档.txt")], {"d1": [{"id": "c1", "content": "正文"}]}))

    def boom(*args, **kwargs):
        raise RuntimeError("模拟投影失败")

    monkeypatch.setattr(rf, "build_ragas_projection", boom)

    task = _lease_task(dataset, conn["id"])
    with pytest.raises(RuntimeError, match="模拟投影失败"):
        asyncio.run(rf.sync_ragflow(
            task, dataset, "http://placeholder:9380", "key", connection_id=conn["id"]))

    source_dir = storage.ragflow_source_dir(conn["id"], dataset)
    assert not (source_dir / "current.json").exists()
    snapshot, pointer = storage.ragflow_snapshot_current(dataset, source_dir=source_dir)
    assert snapshot is None and not pointer
    assert not [row for row in storage.kg_list() if row.get("source_dataset_id") == dataset]


def test_cancel_during_fetch_stops_before_commit(monkeypatch):
    """取消在拉取阶段到达：本阶段线程跑完后不再进入后续阶段，指针不提交。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf

    conn = storage.ragflow_save_connection(
        {"name": "取消用连接", "base_url": "http://cancel.local:9380", "api_key": "key"})
    dataset = "kb-staged-cancel"
    _install(monkeypatch, FakeClient([_doc("d1", "取消文档.txt")], {"d1": [{"id": "c1", "content": "正文"}]}))

    real_fetch = rf.fetch_snapshot
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(client_, dataset_id, previous=None, full_refresh=False):
        entered.set()
        assert release.wait(10), "测试未释放慢速拉取"
        return real_fetch(client_, dataset_id, previous, full_refresh)

    monkeypatch.setattr(rf, "fetch_snapshot", slow_fetch)

    async def scenario():
        task = _lease_task(dataset, conn["id"])
        coro = asyncio.create_task(rf.sync_ragflow(
            task, dataset, "http://placeholder:9380", "key", connection_id=conn["id"]))
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel_event.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await coro
        return task

    task = asyncio.run(scenario())

    logs = list(task.logs)
    assert any(line.startswith("▶ 阶段「fetching」") for line in logs)
    assert not any(line.startswith("▶ 阶段「commit」") for line in logs)
    source_dir = storage.ragflow_source_dir(conn["id"], dataset)
    assert not (source_dir / "current.json").exists()


def test_dead_owner_lease_is_taken_over(tmp_path):
    """租约持有进程已死（PID 不存在）时可被新任务接管，不用手工清理 journal。"""
    from app.tasks.manager import TaskManager

    manager = TaskManager()
    journal = tmp_path / "tasks.sqlite3"
    manager.initialize(journal)
    resource = "conn-dead__kb-dead-owner"
    with sqlite3.connect(journal) as db:
        db.execute(
            "INSERT OR REPLACE INTO resource_leases (resource, task_id, owner, pid, started) VALUES (?,?,?,?,?)",
            (resource, "task-dead", "other-owner", 2147483640, 1),
        )

    task = manager.create("ragflow_sync", (resource,))
    assert task.resources == (resource,)
    task.status = "canceled"
    manager._release_resources(task)


def test_busy_lease_returns_409_on_legacy_sync():
    from fastapi.testclient import TestClient
    from app import storage
    from app.main import app
    from app.tasks.manager import manager

    dataset = "kb-staged-409"
    with TestClient(app) as client:
        held = manager.create("ragflow_sync", storage.ragflow_sync_resources(dataset, None))
        try:
            resp = client.post("/api/ragflow/sync", json={
                "dataset_id": dataset,
                "base_url": "http://127.0.0.1:9380",
                "api_key": "key",
            })
        finally:
            _release(held)
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "resource_busy"


def test_busy_lease_returns_409_on_rebind():
    from fastapi.testclient import TestClient
    from app import storage
    from app.main import app
    from app.tasks.manager import manager

    dataset = "kb-staged-rebind"
    with TestClient(app) as client:
        old_conn = storage.ragflow_save_connection(
            {"name": "改绑旧连接", "base_url": "http://old.local:9380", "api_key": "key"})
        new_conn = storage.ragflow_save_connection(
            {"name": "改绑新连接", "base_url": "http://new.local:9380", "api_key": "key"})
        # 改绑要占新旧两个资源键：占住旧数据源即可让改绑返回 409
        held = manager.create("ragflow_sync", storage.ragflow_sync_resources(dataset, old_conn["id"]))
        try:
            resp = client.post(
                f"/api/ragflow/connections/{new_conn['id']}/datasets/{dataset}/rebind",
                json={"from_connection_id": old_conn["id"]},
            )
        finally:
            _release(held)
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "resource_busy"


@pytest.mark.parametrize("prefix", ["/api/testset", "/api/kg"])
def test_alias_stream_resumes_after_last_event_id(prefix):
    """兼容别名路由与主路由同口径：透传 Last-Event-ID 后只补发后续事件。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.tasks.manager import Task, manager

    with TestClient(app) as client:
        task = Task("ragflow_sync")
        task.status = "running"
        manager.tasks[task.id] = task
        try:
            for message in ("第一行", "第二行", "第三行"):
                task.seq += 1
                task.events.append(("progress", {"log": message, "seq": task.seq, "status": "running"}))
            task.seq += 1
            task.events.append(("done", {"seq": task.seq, "status": "running"}))
            resp = client.get(f"{prefix}/tasks/{task.id}/stream", headers={"Last-Event-ID": "1"})
        finally:
            manager.tasks.pop(task.id, None)

    assert resp.status_code == 200
    body = resp.text
    assert "第一行" not in body
    assert "第二行" in body and "第三行" in body
