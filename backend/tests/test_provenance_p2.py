""" P2：样本出处（provenance）落盘与只读判定的回归测试。

不连真实 RAGFlow、不跑付费 LLM：投影用 `build_ragas_projection` 纯函数构造，
数据落盘走 conftest 指向的临时 DATA_DIR。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

EXPORT_KEYS = {"user_input", "reference", "reference_contexts", "retrieved_contexts",
               "response", "synthesizer_name", "query_style", "query_length", "persona_name"}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _ragas_kg():
    """最小 ragas KG：1 个 document + 2 个 chunk；文档归属写在分块的 `doc_id` 上。"""
    from ragas.testset.graph import Node, NodeType, Relationship

    doc = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "文档摘要", "doc_id": "doc-abc12345", "source_type": "local"})
    c1 = Node(type=NodeType.CHUNK, properties={
        "page_content": "第一段正文", "source_chunk_id": "rc-1", "doc_id": "doc-abc12345"})
    c2 = Node(type=NodeType.CHUNK, properties={
        "page_content": "第二段正文", "source_chunk_id": "rc-2", "doc_id": "doc-abc12345"})
    rels = [Relationship(type="child", source=doc, target=c1),
            Relationship(type="child", source=doc, target=c2)]
    return SimpleNamespace(nodes=[doc, c1, c2], relationships=rels), doc, c1, c2


def test_attach_provenance_writes_four_source_fields():
    from app.ragas_adapter.provenance import PROVENANCE_VERSION, attach_provenance, content_hash

    kg, doc, c1, c2 = _ragas_kg()
    sample = {"index": 0, "reference_contexts": ["第一段正文", "第二段正文"]}
    attach_provenance([sample], {0: [str(c1.id), str(c2.id)]}, kg, {"projection_version": "snap-1"})

    assert sample["source_chunk_ids"] == [str(c1.id), str(c2.id)]
    assert sample["source_content_hashes"] == [content_hash("第一段正文"), content_hash("第二段正文")]
    assert sample["source_document_ids"] == ["doc-abc12345"]   # 直接读分块的 doc_id，不再沿 child 边反查
    assert sample["kg_revision"] == "snap-1"                  # 取 KG 索引里的投影版本字段
    assert sample["provenance_version"] == PROVENANCE_VERSION
    assert sample["source_upstream_chunk_ids"] == ["rc-1", "rc-2"]


def test_attach_provenance_keeps_sample_without_chunk_nodes():
    from app.ragas_adapter.provenance import attach_provenance

    kg, doc, _, _ = _ragas_kg()
    sample = {"index": 0}
    # 只拿到 document 节点：解析不出 chunk，样本应保持「无出处」（读取时判 legacy）
    attach_provenance([sample], {0: [str(doc.id)]}, kg, {})
    assert "source_content_hashes" not in sample
    assert "source_chunk_ids" not in sample


def test_sample_job_captures_scenario_nodes_not_text():
    """出处在生成时取自 scenario.nodes；即使 reference_contexts 是改写过的文本也能定位。"""
    from app.ragas_adapter.testset import _PhasedGeneration

    class FakeSynth:
        name = "fake"

        async def generate_sample(self, scenario=None, **kwargs):
            return {"user_input": "改写后的题干", "reference_contexts": ["与原文不一致的文本"]}

    _, _, c1, c2 = _ragas_kg()
    runner = _PhasedGeneration(llm=None, kg=None, active=[], num_personas=0,
                               run_config=None, callbacks=[])
    scenario = SimpleNamespace(nodes=[c1, c2])
    asyncio.run(runner._sample_job(0, FakeSynth())(scenario=scenario))

    assert runner.snapshot_sample_nodes() == {0: [str(c1.id), str(c2.id)]}


def _run_jobs_with_failures(order: list[int], fail: set[int]):
    """3 个样本 job 分别引用 c0/c1/c2；按 order 的顺序完成，fail 里的提交序号抛错。"""
    from ragas.testset.graph import Node, NodeType

    from app.ragas_adapter.testset import _PhasedGeneration

    chunks = [Node(type=NodeType.CHUNK, properties={"page_content": f"正文{i}", "source_chunk_id": f"rc-{i}"})
              for i in range(3)]

    class FakeSynth:
        name = "fake"

        async def generate_sample(self, scenario=None, **kwargs):
            idx = scenario.idx
            if idx in fail:
                raise RuntimeError(f"第 {idx} 条失败")
            return {"user_input": f"基于正文{idx}的题", "reference_contexts": [f"正文{idx}"]}

    runner = _PhasedGeneration(llm=None, kg=None, active=[], num_personas=0, run_config=None, callbacks=[])
    for idx in order:
        scenario = SimpleNamespace(nodes=[chunks[idx]], idx=idx)
        runner.sample_synth[idx] = "fake"
        try:
            asyncio.run(runner._sample_job(idx, FakeSynth())(scenario=scenario))
        except RuntimeError:
            pass
    return runner, chunks


@pytest.mark.parametrize("order, fail", [
    ([0, 1, 2], {0}),        # 第 0 条失败：旧实现把 rc-1 挂到「基于正文 2」上
    ([0, 1, 2], {1}),        # 中间失败
    ([2, 0, 1], set()),      # 乱序完成
    ([2, 1, 0], {1}),        # 乱序 + 失败
])
def test_provenance_aligned_with_compacted_samples(order, fail):
    """P2-1 回归：出处必须与压实后的样本同序（任意一条失败 / 乱序完成都不能错位）。"""
    from app.ragas_adapter.provenance import attach_provenance

    runner, chunks = _run_jobs_with_failures(order, fail)
    ordered, _planned, failures, _sf, nodes_by_pos = runner.snapshot_results()
    assert len(failures) == len(fail)
    samples = [{"index": pos, "user_input": s["user_input"]} for pos, (_n, s) in enumerate(ordered)]
    kg = SimpleNamespace(nodes=chunks, relationships=[])
    attach_provenance(samples, nodes_by_pos, kg, {"projection_version": "snap-1"})
    for sample in samples:
        idx = int(sample["user_input"].removeprefix("基于正文").removesuffix("的题"))
        assert sample["source_upstream_chunk_ids"] == [f"rc-{idx}"], sample
    assert runner.snapshot_sample_nodes() == nodes_by_pos


def _snapshot() -> dict:
    return {
        "dataset_id": "ds-prov",
        "documents": {"rd-1": {"name": "文档一.txt"}},
        "chunks": {
            "rc-1": {"id": "rc-1", "document_id": "rd-1", "position": 0,
                     "page_content": "国家电网发布了公告", "entities": ["国家电网"]},
            "rc-2": {"id": "rc-2", "document_id": "rd-1", "position": 1,
                     "page_content": "国网宣布新计划", "entities": ["国网"]},
        },
    }


def test_judge_four_statuses_with_temp_storage():
    from app import storage
    from app.ragas_adapter.provenance import (STATUS_LEGACY, STATUS_ORPHAN, STATUS_STALE,
                                              STATUS_VALID, content_hash,
                                              evaluate_testset_provenance)
    from app.ragas_adapter.ragflow import _stable_id, build_ragas_projection

    projection = build_ragas_projection(_snapshot(), source_id="conn__ds-prov")
    chunks = {node["id"]: node["properties"]["page_content"]
              for node in projection["nodes"] if node["type"] == "chunk"}
    rc1 = _stable_id("chunk", "ds-prov", "rc-1")
    rc2 = _stable_id("chunk", "ds-prov", "rc-2")

    samples = [
        # valid：两个 chunk 都在且哈希一致
        {"index": 0, "source_chunk_ids": [rc1, rc2],
         "source_content_hashes": [content_hash(chunks[rc1]), content_hash(chunks[rc2])]},
        # stale：chunk 还在，但记录的哈希与当前投影不一致
        {"index": 1, "source_chunk_ids": [rc1], "source_content_hashes": ["0" * 64]},
        # orphan：引用的 chunk 在投影里不存在
        {"index": 2, "source_chunk_ids": ["chunk-已删除"], "source_content_hashes": ["0" * 64]},
        # legacy：历史样本只有文本反查出来的上游 id，没有内容哈希
        {"index": 3, "source_chunk_ids": ["rc-1"]},
    ]

    kg_id, ts_id = "kg-provtest", "ts-provtest"
    kg_path = storage.kg_write_json(kg_id, projection)
    storage.kg_upsert({"id": kg_id, "name": "投影", "created_at": storage.now_iso(),
                       "mode": "ragflow-projection", "kg_path": kg_path,
                       "projection_version": "snap-1"})
    ts_path = storage.ts_write(ts_id, {"id": ts_id, "name": "测试集", "kg_id": kg_id, "samples": samples})
    storage.ts_upsert({"id": ts_id, "path": ts_path, "name": "测试集"})

    result = evaluate_testset_provenance(ts_id)

    statuses = [row["status"] for row in result["samples"]]
    assert statuses == [STATUS_VALID, STATUS_STALE, STATUS_ORPHAN, STATUS_LEGACY]
    assert result["counts"][STATUS_VALID] == 1
    assert result["samples"][1]["changed_chunk_ids"] == [rc1]
    assert result["samples"][2]["missing_chunk_ids"] == ["chunk-已删除"]
    assert result["samples"][3]["legacy"] is True
    # 只判定：样本落盘内容不得被改动
    assert storage.ts_read(ts_id)["samples"] == samples


def test_legacy_flag_marked_on_read():
    from app.ragas_adapter.provenance import annotate_legacy_flag

    samples = [
        {"index": 0, "source_chunk_ids": ["rc-1"]},  # 历史：文本反查得到 id 但没有哈希
        {"index": 1, "source_chunk_ids": ["chunk-a"], "source_content_hashes": ["0" * 64]},
    ]
    annotate_legacy_flag(samples)
    assert [s["legacy"] for s in samples] == [True, False]


def test_judge_unknown_when_projection_missing():
    from app.ragas_adapter.provenance import STATUS_UNKNOWN, evaluate_samples_provenance

    judged = evaluate_samples_provenance(
        {"id": "ts-x", "kg_id": "kg-missing",
         "samples": [{"index": 0, "source_chunk_ids": ["chunk-a"], "source_content_hashes": ["0" * 64]}]},
        None)
    assert judged["samples"][0]["status"] == STATUS_UNKNOWN
    assert judged["samples"][0]["legacy"] is False


def test_export_keys_unchanged_with_provenance_fields():
    from app.ragas_adapter.testset import export_testset

    sample = {
        "user_input": "问题", "reference": "答案", "reference_contexts": ["上下文"],
        "retrieved_contexts": [], "response": "", "synthesizer_name": "single_hop_specific",
        "query_style": "PERFECT_GRAMMAR", "query_length": "SHORT", "persona_name": "测试",
        # P2 新增字段：样本里可以有，但导出键集合必须保持 ragas 原生 9 个
        "source_chunk_ids": ["chunk-a"], "source_content_hashes": ["0" * 64],
        "source_document_ids": ["doc-a"], "source_upstream_chunk_ids": ["rc-a"],
        "kg_revision": "snap-1", "provenance_version": 1, "adopted": True,
    }
    ts_data = {"id": "ts-export", "name": "测试集", "samples": [sample]}

    _, jsonl = export_testset(ts_data, "jsonl", "all", None)
    row = json.loads(jsonl.decode("utf-8").splitlines()[0])
    # 空的 response / retrieved_contexts 在 JSONL 里省略（CSV 表头仍保留）
    assert set(row) == EXPORT_KEYS - {"response", "retrieved_contexts"}

    _, csv_bytes = export_testset(ts_data, "csv", "all", None)
    header = csv_bytes.decode("utf-8-sig").splitlines()[0].split(",")
    assert set(header) == EXPORT_KEYS
