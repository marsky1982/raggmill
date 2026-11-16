"""Step 1 加固回归：（超时/重试不生效）与 （续跑重复 chunk/边）。

两者都不真连模型：LLM 用桩对象，Step 1 的断点链路用演示模式驱动。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# 注意：不要在模块顶层 import app.*（conftest 需要先设置临时 DATA_DIR）


# ---------------- Step 1 客户端必须按 policy 生效 ----------------

class _StubLLM:
    def __init__(self):
        self.config = None

    def set_run_config(self, config):
        self.config = config


def test_configure_run_applies_retry_budget_and_retriable_types():
    """RunConfig 的重试上限必须等于传入值，且异常类型覆盖超时/连接/5xx。"""
    import openai

    from app.ragas_adapter.clients import configure_run

    llm = _StubLLM()
    config = configure_run(llm, timeout=12, retries=4, workers=2)

    assert llm.config is config
    assert config.timeout == 12
    assert config.max_retries == 4          # 「重试上限 4」= 每次调用最多发 4 次
    assert config.max_workers == 2
    # openai 的 APITimeoutError 是 APIConnectionError 的子类，故超时已被覆盖
    assert issubclass(openai.APITimeoutError, openai.APIConnectionError)
    assert config.exception_types == (openai.RateLimitError, openai.APIConnectionError,
                                      openai.InternalServerError)


def test_step1_policy_reaches_both_llm_and_embeddings():
    """policy 的 timeout / 重试 / 并发必须一路传到 LLM 与 Embedding，不能有硬编码。"""
    from app.policies import GenerationPolicy
    from app.ragas_adapter import kg

    calls = {}

    def fake_llm(*args, **kwargs):
        calls["llm"] = kwargs
        return object()

    def fake_embeddings(*args, **kwargs):
        calls["embedding"] = kwargs
        return object()

    def fake_configure(llm, **kwargs):
        calls["run_config"] = kwargs
        return object()

    monkeypatch_targets = {"build_llm": fake_llm, "build_embeddings": fake_embeddings,
                           "configure_run": fake_configure}
    originals = {name: getattr(kg, name) for name in monkeypatch_targets}
    try:
        for name, fake in monkeypatch_targets.items():
            setattr(kg, name, fake)
        policy = GenerationPolicy(max_retries=2, llm_timeout=45, max_workers=4)
        kg._build_step1_clients({"id": "llm"}, {"id": "emb"}, policy, object())
    finally:
        for name, original in originals.items():
            setattr(kg, name, original)

    assert calls["llm"]["timeout"] == 45
    assert calls["llm"]["sdk_max_retries"] == 0      # SDK 自带重试必须关掉，避免与 tenacity 相乘
    assert calls["run_config"] == {"timeout": 45, "retries": 2, "workers": 4}
    assert calls["embedding"]["timeout"] == 45
    assert calls["embedding"]["sdk_max_retries"] == 0
    assert calls["embedding"]["max_retries"] == 2


# ---------------- 断点只记录完整成功的阶段 ----------------

class _FakeGraph:
    """只实现 save（写节点/边计数），用来验证快照指针语义。"""

    def __init__(self, node_count: int, relationship_count: int):
        self.node_count = node_count
        self.relationship_count = relationship_count

    def save(self, path):
        Path(path).write_bytes(json.dumps({"node_count": self.node_count,
                                           "relationship_count": self.relationship_count}).encode("utf-8"))


def _snapshot_counts(path: str) -> tuple[int, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["node_count"], data["relationship_count"]


def test_failed_stage_keeps_last_successful_snapshot_and_counts():
    """中途失败只标记阶段，不覆盖上一个成功快照；连续两次 resume 读到同一份图。"""
    from app import storage
    from app.ragas_adapter.kg import _failed_checkpoint_meta
    meta = {"run_id": "run-134", "status": "running", "stage_index": 2,
            "total_stages": 5, "node_count": 7, "relationship_count": 5}
    storage.ckpt_save("run-134", meta, _FakeGraph(7, 5))
    success_meta, success_path = storage.ckpt_load("run-134")

    # 第 3 个阶段（0-based index=2）抛错：只写 meta，不传 kg
    failed_meta = _failed_checkpoint_meta(success_meta, 2)
    storage.ckpt_save("run-134", failed_meta, None)

    for _ in range(2):  # 连续两次 resume
        reloaded_meta, reloaded_path = storage.ckpt_load("run-134")
        assert reloaded_path == success_path
        assert reloaded_meta["snapshot_hash"] == success_meta["snapshot_hash"]
        assert reloaded_meta["stage_index"] == 2          # 仍从失败阶段重跑，不回退也不前进
        assert reloaded_meta["node_count"] == 7
        assert reloaded_meta["relationship_count"] == 5
        assert _snapshot_counts(reloaded_path) == (7, 5)  # 半成品没有混进快照


def test_build_kg_failure_never_checkpoints_partial_stage(monkeypatch):
    """演示模式跑 Step 1 循环：第 2 阶段失败后，断点只停在已完成的第 1 阶段。"""
    from app import storage
    from app.ragas_adapter import kg as kgmod
    from app.tasks.manager import Task

    docs = {"doc-1": {"id": "doc-1", "name": "a.md",
                      "text": "# 标题\n\n正文内容，用于切块。\n\n## 小节\n\n更多内容。"}}
    monkeypatch.setattr(kgmod, "RAGAS_AVAILABLE", False)
    monkeypatch.setattr(kgmod, "document_snapshot", lambda: docs)

    real_sleep = asyncio.sleep
    state = {"calls": 0}

    async def flaky_sleep(delay):
        # 只统计演示模式的阶段节奏 sleep（0.7s）；StagedJob 内部 drainer 的 0.2s 轮询不算
        if delay >= 0.5:
            state["calls"] += 1
            if state["calls"] >= 2:
                raise RuntimeError("模拟中途失败")
        await real_sleep(0)

    monkeypatch.setattr(kgmod.asyncio, "sleep", flaky_sleep)

    with pytest.raises(RuntimeError, match="模拟中途失败"):
        asyncio.run(kgmod.build_kg(Task("test"), ["doc-1"], {"id": "llm", "model": "m"},
                                   {"id": "emb", "model": "e"}, "断点测试", run_id="run-145"))

    meta, snapshot_path = storage.ckpt_load("run-145")
    assert meta["stage_index"] == 1          # 只有第 1 阶段被记为完成
    # 失败即写失败断点（不分 ragas / 演示模式），但快照仍只到第 1 阶段
    assert meta["status"] == "failed"
    assert meta["stop_reason"] == "failed"
    assert snapshot_path is not None
