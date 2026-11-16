"""import 无副作用 + 启动初始化挪到 lifespan。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import BACKEND_DIR


def _import_in_subprocess(modules: list[str], data_dir: Path) -> subprocess.CompletedProcess:
    # 必须用子进程：本进程里 app.* 可能已被其他测试 import 过，模块缓存会掩盖 import 副作用
    env = {**os.environ, "RAGAS_STUDIO_DATA_DIR": str(data_dir), "PYTHONIOENCODING": "utf-8"}
    code = "; ".join(f"import {m}" for m in modules)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


def test_import_storage_touches_no_files(tmp_path: Path):
    data_dir = tmp_path / "data"
    r = _import_in_subprocess(["app.storage"], data_dir)
    assert r.returncode == 0, r.stderr
    assert not data_dir.exists(), f"import app.storage 创建了 {sorted(p.name for p in data_dir.rglob('*'))}"


def test_import_main_touches_no_files(tmp_path: Path):
    # 单实例锁、建目录、加载索引都应推迟到 lifespan，import 本身不碰文件
    data_dir = tmp_path / "data"
    r = _import_in_subprocess(["app.main"], data_dir)
    assert r.returncode == 0, r.stderr
    assert not data_dir.exists(), f"import app.main 创建了 {sorted(p.name for p in data_dir.rglob('*'))}"


def test_lifespan_initializes_data_dir(data_dir: Path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        # 前端靠这两项判断「后端是不是旧进程」；commit 拿不到时也要回原因
        assert health["started_at"] and isinstance(health["commit_note"], str)
        for sub in ("presets", "uploads", "kg", "testset", "documents"):
            assert (data_dir / sub).is_dir(), sub
        lock = data_dir / ".backend.lock"
        # 锁文件记录进程身份（PID + 创建时间，S1），不再是裸 PID
        import json
        recorded = json.loads(lock.read_text(encoding="utf-8"))
        assert recorded["pid"] == os.getpid()
        assert "started" in recorded
    # 正常关闭时释放锁
    assert not (data_dir / ".backend.lock").exists()


def test_lifespan_refuses_when_another_instance_holds_lock(data_dir: Path):
    import asyncio

    import pytest

    from app.main import app, lifespan

    async def _startup() -> None:
        async with lifespan(app):
            pass

    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / ".backend.lock"
    other_pid = os.getppid()  # 父进程必然存活，冒充「另一个后端实例」
    lock.write_text(str(other_pid), encoding="utf-8")
    try:
        with pytest.raises(SystemExit, match="后端已在运行"):
            asyncio.run(_startup())
        # 拒绝启动时不能删掉别人的锁
        assert lock.read_text(encoding="utf-8") == str(other_pid)
    finally:
        lock.unlink(missing_ok=True)
