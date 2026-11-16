"""增强阶段的「部分成果保存 + 账单落盘 + 按题型裁剪 + 不复活已删 KG」回归。

覆盖以下场景： ①（增强的取消 / 超预算 / 失败直接 raise，已花 token 不进任何账单）、
 ①（只出单跳题也给每个 chunk 跑 themes）、 ②（全有或全无，一个 chunk 失败整批作废）、
§2.4 R-1 / §7.1 S3（增强写回会把已删除的 KG 重新加回索引）。
全部用桩 LLM / embedding，0 token。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from test_kg_enhancement import FakeEmbedding, FakeLLM, _build_projection_kg

PRESET = {"base_url": "x", "model": "m", "api_key": "k"}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


class HalfFailingLLM(FakeLLM):
    """第二个 chunk 的摘要调用失败，其余正常；记录是否被要求抽 themes。"""

    themes_calls: int = 0

    async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        text = prompt.text if hasattr(prompt, "text") else str(prompt)
        if "themes and concepts" in text:
            self.themes_calls += 1
        if "国网调度中心" in text:
            raise RuntimeError("provider 500")
        return await super().agenerate_text(prompt, n, temperature, stop, callbacks)


def _register(kg_id: str, kg) -> dict:
    from app import storage
    from app.ragas_adapter.kg import _kg_to_dict

    path = storage.kg_write_json(kg_id, _kg_to_dict(kg))
    entry = {"id": kg_id, "name": "投影", "kg_path": path, "mode": "ragflow-projection",
             "created_at": storage.now_iso()}
    storage.kg_upsert(entry)
    return entry


def _run_enhance(monkeypatch, entry, kg, llm, **kw):
    from app.ragas_adapter import testset as ts
    from app.ragas_adapter import testset_enhance
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.tasks.manager import Task

    # 补丁打在真正调用的子模块上（testset.py 只是门面）
    monkeypatch.setattr(testset_enhance, "build_llm", lambda *a, **k: llm)
    monkeypatch.setattr(testset_enhance, "build_embeddings", lambda *a, **k: FakeEmbedding())
    return asyncio.run(ts._enhance_projection_kg(
        Task("testset"), entry, kg, PRESET, PRESET, GenerationTelemetry(),
        None, max_retries=1, llm_timeout=30, max_workers=2, **kw,
    ))


def _chunk_summaries(path: str) -> list:
    data = json.loads(open(path, encoding="utf-8").read())
    return [n["properties"].get("summary") for n in data["nodes"] if str(n["type"]).lower().endswith("chunk")]


def test_partial_enhancement_is_saved_not_discarded(monkeypatch):
    from app.ragas_adapter.testset import EnhancementStopped

    kg = _build_projection_kg()
    entry = _register("kg-partial-enh", kg)
    with pytest.raises(EnhancementStopped) as excinfo:
        _run_enhance(monkeypatch, entry, kg, HalfFailingLLM())
    stop = excinfo.value
    assert stop.stop_reason == "failed"
    assert stop.saved_nodes == 1
    assert "已保存 1 个节点" in str(stop)
    # 成功的那个 chunk 的摘要已写回磁盘：下次只需补剩余缺口，不必整批重付
    summaries = _chunk_summaries(entry["kg_path"])
    assert sum(1 for s in summaries if s) == 1


class CountingLLM(FakeLLM):
    themes_calls: int = 0

    async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        text = prompt.text if hasattr(prompt, "text") else str(prompt)
        if "themes and concepts" in text:
            self.themes_calls += 1
        return await super().agenerate_text(prompt, n, temperature, stop, callbacks)


def test_themes_skipped_when_abstract_not_requested(monkeypatch):
    kg = _build_projection_kg()
    entry = _register("kg-crop-enh", kg)
    llm = CountingLLM()
    stats = _run_enhance(monkeypatch, entry, kg, llm, include_themes=False)
    assert stats["theme_chunks"] == 0
    assert llm.themes_calls == 0
    assert stats["chunk_summaries"] == 2


def test_enhancement_does_not_resurrect_deleted_kg(monkeypatch):
    from app import storage
    from app.ragas_adapter.testset import EnhancementStopped

    kg = _build_projection_kg()
    entry = _register("kg-deleted-enh", kg)
    before = open(entry["kg_path"], "rb").read()
    storage.kg_delete("kg-deleted-enh")
    assert storage.kg_get("kg-deleted-enh") is None
    with pytest.raises(EnhancementStopped):
        _run_enhance(monkeypatch, entry, kg, HalfFailingLLM())
    assert storage.kg_get("kg-deleted-enh") is None  # 没有被 kg_upsert 加回索引
    import os
    if os.path.exists(entry["kg_path"]):
        assert open(entry["kg_path"], "rb").read() == before


def test_enhancement_failure_bill_is_persisted(monkeypatch):
    """增强失败时 generate_testset 要落一份带账单的测试集记录，并在错误里给出其 id。"""
    from app import storage
    from app.ragas_adapter import testset as ts
    from app.tasks.manager import Task

    kg = _build_projection_kg()
    entry = _register("kg-bill-enh", kg)

    async def fake_enhance(task, kg_entry, kg, llm_preset, emb_preset, tele, on_progress=None, **kw):
        with tele._lock:
            tele.calls_started += 3
            tele.input_tokens += 1200
            tele.output_tokens += 800
        raise ts.EnhancementStopped("failed", "知识图谱自动增强失败（未保存任何改动）：RuntimeError: 403")

    from app.ragas_adapter import testset_generation
    monkeypatch.setattr(testset_generation, "build_llm", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(testset_generation, "_enhance_projection_kg", fake_enhance)
    task = Task("testset")
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(ts.generate_testset(task, entry["id"], PRESET, PRESET, 4, None, True, "账单测试"))
    message = str(excinfo.value)
    assert "自动增强失败" in message and "已保存为测试集" in message
    saved = [x for x in storage.ts_list() if x.get("kg_id") == entry["id"]]
    assert len(saved) == 1
    data = storage.ts_read(saved[0]["id"])
    assert data["stop_reason"] == "failed"
    assert data["samples"] == []
    assert data["usage"]["enhancement"]["llm_calls"] == 3
    assert data["usage"]["enhancement"]["total_tokens"] == 2000


def test_detail_endpoint_exposes_stop_reason_and_usage():
    """/171：GET /api/testset/{id} 要带终态、少出条数与账单（含增强分项），界面才看得到。"""
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    path = storage.ts_write("ts-detail-usage", {
        "id": "ts-detail-usage", "name": "部分结果", "created_at": "t", "kg_id": "kg-x",
        "testset_size": 5, "stop_reason": "undelivered", "partial": True, "failed_samples": 1,
        "undelivered": {"requested": 5, "delivered": 4, "total_missing": 1}, "error": None,
        "usage": {"total_tokens": 100, "llm_calls": 3, "enhancement": {"total_tokens": 40}},
        "samples": [],
    })
    storage.ts_upsert({"id": "ts-detail-usage", "path": path, "name": "部分结果"})
    with TestClient(app) as client:
        body = client.get("/api/testset/ts-detail-usage").json()
    assert body["stop_reason"] == "undelivered" and body["partial"] is True
    assert body["testset_size"] == 5 and body["failed_samples"] == 1
    assert body["undelivered"]["total_missing"] == 1
    assert body["usage"]["enhancement"]["total_tokens"] == 40


def test_stop_labels_match_frontend():
    """终态中文在前后端各有一份（lib/testsetView.ts STOP_LABELS / testset._STOP_LABELS），锁住一致。"""
    import re
    from pathlib import Path

    from app.ragas_adapter.testset import _STOP_LABELS

    page = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "testsetView.ts")
    block = re.search(r"const STOP_LABELS[^{]*\{([^}]*)\}", page.read_text(encoding="utf-8")).group(1)
    front = dict(re.findall(r'(\w+):\s*"([^"]+)"', block))
    assert front == _STOP_LABELS
