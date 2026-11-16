"""阶段 2-②：建图迁入 `StagedJob` 后的断点 / 取消 / 账单 / 染色语义回归。

不连真实 LLM：ragas 路径把 `default_transforms` / `apply_transforms` 换成假实现，
演示模式路径不依赖任何外部服务。断言口径与迁移前一致。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _kind(node: dict) -> str:
    return str(node.get("type") or "").rsplit(".", 1)[-1].lower()


class _FirstStage:
    """假 transform：类名与配置会进续跑签名（两次运行必须一致）。"""


class _SecondStage:
    pass


def _fake_transforms(*_args, **_kwargs):
    return [_FirstStage(), _SecondStage()]


def _patch_ragas(monkeypatch, apply):
    from ragas.run_config import RunConfig

    from app.ragas_adapter import kg as kg_mod

    monkeypatch.setattr(kg_mod, "RAGAS_AVAILABLE", True)
    monkeypatch.setattr(kg_mod, "_build_step1_clients",
                        lambda *args, **kwargs: (object(), object(), RunConfig()))
    monkeypatch.setattr("ragas.testset.transforms.default_transforms", _fake_transforms)
    monkeypatch.setattr("ragas.testset.transforms.apply_transforms", apply)


def _append_chunk(kg) -> None:
    """模拟一个「切出分块」的阶段：文档 → child → chunk。"""
    from ragas.testset.graph import Node, Relationship

    source = kg.nodes[0]
    chunk = Node(type="chunk", properties={"page_content": "第一段正文"})
    kg.nodes.append(chunk)
    kg.relationships.append(Relationship(type="child", source=source, target=chunk,
                                         bidirectional=False))


def test_failure_keeps_last_checkpoint_and_resume_colors_chunks(monkeypatch):
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import Task

    doc = storage.add_document("staged-失败续跑.md", "# 标题\n\n正文内容。", 12)
    calls: list[str] = []
    failed = {"done": False}

    def fake_apply(kg, transforms, run_config=None):
        name = type(transforms[0]).__name__
        calls.append(name)
        if name == "_FirstStage":
            _append_chunk(kg)
            return
        if not failed["done"]:
            failed["done"] = True
            raise RuntimeError("模拟第二个阶段失败")

    _patch_ragas(monkeypatch, fake_apply)

    async def run(resume: bool = False):
        return await kg_mod.build_kg(
            Task("build_kg"), [doc["id"]], {"id": "p-llm"}, {"id": "p-emb"},
            "失败续跑", run_id="run-staged-fail", resume=resume, enhance_chunks=False,
        )

    # 失败：保留第一个阶段的断点，不覆盖快照
    with pytest.raises(RuntimeError, match="模拟第二个阶段失败"):
        asyncio.run(run())

    meta, snapshot_path = storage.ckpt_load("run-staged-fail")
    assert meta["stage_index"] == 1
    assert meta["status"] == "failed"
    assert meta["stop_reason"] == "failed"
    assert snapshot_path and Path(snapshot_path).exists()
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    chunks = [n for n in snapshot["nodes"] if _kind(n) == "chunk"]
    assert chunks and "doc_id" not in chunks[0]["properties"]  # 染色只在完成出口做

    # 续跑：跳过已完成的阶段，完成出口统一染色
    result = asyncio.run(run(resume=True))
    assert calls == ["_FirstStage", "_SecondStage", "_SecondStage"]
    data = json.loads(Path(result["kg_path"]).read_text(encoding="utf-8"))
    chunks = [n for n in data["nodes"] if _kind(n) == "chunk"]
    assert chunks and all(c["properties"]["doc_id"] == doc["id"] for c in chunks)
    assert storage.ckpt_load("run-staged-fail")[0] is None  # 成功后断点已清除


def test_budget_exceeded_is_visible_on_task_and_checkpoint(monkeypatch):
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import manager

    doc = storage.add_document("staged-超预算.md", "# 标题\n\n正文内容。", 12)
    holder: dict = {}
    state = {"armed": True}

    def fake_apply(kg, transforms, run_config=None):
        _append_chunk(kg)
        if state["armed"]:
            state["armed"] = False
            # 模拟第一次 LLM 调用即触顶：预算熔断后当前阶段不得写断点
            holder["task"].generation_telemetry.budget_exceeded = True

    _patch_ragas(monkeypatch, fake_apply)

    async def run_once(resume: bool = False):
        task = manager.create("build_kg")   # 不占资源键，避免与前一任务收尾互相阻塞
        holder["task"] = task
        manager.run(task, kg_mod.build_kg(
            task, [doc["id"]], {"id": "p-llm"}, {"id": "p-emb"},
            "超预算", run_id="run-staged-budget", resume=resume, enhance_chunks=False,
        ))
        await task._async_task
        return task

    first = asyncio.run(run_once())

    # 任务级：status 仍是标准失败态，另带 stop_reason 供界面显示「超预算（可续跑）」
    assert first.status == "failed"
    assert first.stop_reason == "budget_exceeded"
    assert "token 预算" in (first.error or "")
    # SSE 终态事件同样带 stop_reason（前端订阅靠这一条收敛）
    event, payload = first.events[-1]
    assert event == "done"
    assert payload["status"] == "failed"
    assert payload["stop_reason"] == "budget_exceeded"

    # 断点：失败断点 + stop_reason，stage_index 停在未完成阶段，快照保留可续跑
    meta, snapshot_path = storage.ckpt_load("run-staged-budget")
    assert meta["stage_index"] == 0
    assert meta["status"] == "failed"
    assert meta["stop_reason"] == "budget_exceeded"
    assert snapshot_path and Path(snapshot_path).exists()

    resumed = asyncio.run(run_once(resume=True))
    assert resumed.status == "succeeded"
    data = json.loads(Path(resumed.result["kg_path"]).read_text(encoding="utf-8"))
    chunks = [n for n in data["nodes"] if _kind(n) == "chunk"]
    assert chunks and all(c["properties"]["doc_id"] == doc["id"] for c in chunks)
    assert storage.ckpt_load("run-staged-budget")[0] is None


def test_cancel_stops_before_next_stage_and_keeps_checkpoint(monkeypatch):
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import manager

    doc = storage.add_document("staged-取消.md", "# 标题\n\n正文内容。", 12)
    before = len(storage.kg_list())
    monkeypatch.setattr(kg_mod, "RAGAS_AVAILABLE", False)
    monkeypatch.setattr(kg_mod, "_DEMO_PAUSE_SECONDS", 0.3)

    async def scenario():
        task = manager.create("build_kg", ("kg-test", "run:run-staged-cancel"))
        manager.run(task, kg_mod.build_kg(
            task, [doc["id"]], {"id": "p-llm"}, {"id": "p-emb"},
            "取消测试", run_id="run-staged-cancel",
        ))
        for _ in range(600):
            meta, _path = storage.ckpt_load("run-staged-cancel")
            if meta and meta.get("stage_index", 0) >= 1:
                break
            await asyncio.sleep(0.01)
        manager.cancel(task.id)
        await task._async_task
        return task

    task = asyncio.run(scenario())

    assert task.status == "canceled"
    meta, snapshot_path = storage.ckpt_load("run-staged-cancel")
    assert meta["stage_index"] == 1  # 取消后不再推进到下一阶段
    assert snapshot_path and Path(snapshot_path).exists()  # 断点保留，可续跑
    assert len(storage.kg_list()) == before  # 取消不落图


def test_cancel_does_not_start_next_stage(monkeypatch):
    """ragas 路径：取消落在第一阶段在途时，第二阶段不得启动（即不再发新的 LLM 调用）。"""
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import manager

    doc = storage.add_document("staged-取消-ragas.md", "# 标题\n\n正文内容。", 12)
    calls: list[str] = []

    def slow_apply(kg, transforms, run_config=None):
        calls.append(type(transforms[0]).__name__)
        time.sleep(0.8)   # 留出取消窗口：等测试线程在第一次调用后取消

    _patch_ragas(monkeypatch, slow_apply)

    async def scenario():
        task = manager.create("build_kg")
        manager.run(task, kg_mod.build_kg(
            task, [doc["id"]], {"id": "p-llm"}, {"id": "p-emb"},
            "取消不发新调用", run_id="run-staged-cancel-ragas", enhance_chunks=False,
        ))
        for _ in range(800):
            if calls:
                break
            await asyncio.sleep(0.01)
        manager.cancel(task.id)
        await task._async_task
        await asyncio.sleep(0.9)   # 等在途线程收尾，确认没有进入下一阶段
        return task

    task = asyncio.run(scenario())

    assert task.status == "canceled"
    assert calls == ["_FirstStage"]
