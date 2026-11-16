"""S1：存活判断 = PID 存活且进程创建时间一致；S2：优雅关闭先落盘。

场景：`stop` 强杀后锁文件 / 资源租约 / 任务 owner 残留，重启后旧 PID 被别的进程复用。
只看 PID 时：后端拒绝启动、全局 409、旧任务恢复成取消不了的 foreign running。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest


def _parent_started() -> int:
    from app.single_instance import process_started

    started = process_started(os.getppid())
    if started is None:
        pytest.skip("当前平台拿不到进程创建时间")
    return started


def test_identity_alive_rejects_reused_pid():
    from app.single_instance import current_identity, identity_alive

    me = current_identity()
    assert identity_alive(me["pid"], me["started"]) is True
    assert identity_alive(me["pid"], (me["started"] or 0) + 12345) is False  # 同 PID、不同创建时间 = 被复用
    assert identity_alive(me["pid"], None) is True                          # 旧记录：只能按 PID 判


def test_lock_with_reused_pid_is_taken_over(data_dir: Path):
    from app.main import app, lifespan

    async def _startup() -> None:
        async with lifespan(app):
            pass

    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / ".backend.lock"
    # 父进程活着，但创建时间对不上 → 是「PID 被复用」，应当接管而不是拒绝启动
    lock.write_text(json.dumps({"pid": os.getppid(), "started": _parent_started() + 999}), encoding="utf-8")
    asyncio.run(_startup())
    assert not lock.exists()  # 正常启动 + 关闭后释放了自己的锁


def test_lock_with_live_identity_still_refuses(data_dir: Path):
    from app.main import app, lifespan

    async def _startup() -> None:
        async with lifespan(app):
            pass

    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / ".backend.lock"
    body = json.dumps({"pid": os.getppid(), "started": _parent_started()})
    lock.write_text(body, encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match="后端已在运行"):
            asyncio.run(_startup())
        assert lock.read_text(encoding="utf-8") == body
    finally:
        lock.unlink(missing_ok=True)


def _fresh_manager(path: Path):
    from app.tasks.manager import TaskManager

    manager = TaskManager()
    manager.initialize(path)
    return manager


def test_stale_lease_with_reused_pid_does_not_block(tmp_path: Path):
    from app.tasks.manager import ResourceBusyError

    journal = tmp_path / "tasks.sqlite3"
    _fresh_manager(journal)
    with sqlite3.connect(journal) as db:
        db.execute("INSERT INTO resource_leases (resource, task_id, owner, pid, started) VALUES (?, ?, ?, ?, ?)",
                   ("ragas-generation", "task-old", "owner-old", os.getppid(), _parent_started() + 999))
    manager = _fresh_manager(journal)
    task = manager.create("generate_testset", ("ragas-generation",))  # 不再 409
    assert task.resources == ("ragas-generation",)

    with sqlite3.connect(journal) as db:  # 同一进程身份仍在 → 照旧拒绝
        db.execute("UPDATE resource_leases SET owner='owner-other', pid=?, started=? WHERE resource=?",
                   (os.getppid(), _parent_started(), "ragas-generation"))
    other = _fresh_manager(journal)
    with pytest.raises(ResourceBusyError):
        other.create("generate_testset", ("ragas-generation",))


def test_restored_task_with_reused_owner_pid_is_interrupted(tmp_path: Path):
    journal = tmp_path / "tasks.sqlite3"
    _fresh_manager(journal)
    body = {"task_id": "task-ghost", "type": "generate_testset", "status": "running", "resources": ["kg:x"],
            "owner": "owner-dead", "owner_pid": os.getppid(), "owner_started": _parent_started() + 999}
    with sqlite3.connect(journal) as db:
        db.execute("INSERT INTO tasks VALUES (?, ?, ?)", ("task-ghost", json.dumps(body), 1.0))
    manager = _fresh_manager(journal)
    task = manager.tasks["task-ghost"]
    assert task._foreign is False
    assert task.status == "interrupted"  # 不再是「永远运行中、取消不了」的 foreign 任务


def test_shutdown_cancels_running_tasks_and_waits():
    from app.tasks.manager import TaskManager

    async def scenario():
        manager = TaskManager()
        task = manager.create("generate_testset", ("kg:shutdown",))
        saved = []

        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                saved.append("partial")  # 模拟「被取消时先落盘」
                raise

        manager.run(task, work())
        await asyncio.sleep(0.05)
        await manager.shutdown(timeout=5)
        return task, saved

    task, saved = asyncio.run(scenario())
    assert saved == ["partial"]
    assert task.status == "canceled"


def test_shutdown_endpoint_requires_managed_instance(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("RAGAS_STUDIO_WRITE_TOKEN", raising=False)
    with TestClient(app) as client:
        resp = client.post("/api/admin/shutdown")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "not_managed"
