"""投影 KG 生成前增强（摘要 / 摘要向量 / themes / summary_similarity 边）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from ragas.llms.base import BaseRagasLLM
from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship


class FakeLLM(BaseRagasLLM):
    """只实现 ragas 需要的异步文本生成；按 prompt 内容返回摘要或 themes 的合法 JSON。"""

    calls: int = 0

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def generate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        raise NotImplementedError

    async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
        self.calls += 1
        text = prompt.text if hasattr(prompt, "text") else str(prompt)
        if "themes and concepts" in text:
            payload = {"output": ["电网调度", "电力系统"]}
        else:
            payload = {"text": "摘要：" + text.splitlines()[-1][:60]}
        from ragas.llms.base import Generation, LLMResult
        return LLMResult(generations=[[Generation(text=json.dumps(payload, ensure_ascii=False))]])

    def is_finished(self, response):
        return True


class FakeEmbedding:
    is_async = True

    def __init__(self) -> None:
        self.calls = 0

    async def aembed_text(self, text):
        self.calls += 1
        return [1.0, 0.5, float(len(text) % 5) / 5.0, 0.25]


def _build_projection_kg() -> KnowledgeGraph:
    """模拟 RAGFlow 投影：只有原文/实体/实体重叠边，没有 summary/embedding/themes。"""
    kg = KnowledgeGraph()
    doc = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "调度规范.txt",
        "document_metadata": {"filename": "调度规范.txt"},
    })
    chunks = [
        Node(type=NodeType.CHUNK, properties={"page_content": "国家电网负责跨省调度。", "entities": ["国家电网"]}),
        Node(type=NodeType.CHUNK, properties={"page_content": "国网调度中心承担实时平衡。", "entities": ["国网"]}),
    ]
    for node in (doc, *chunks):
        kg.add(node)
    for chunk in chunks:
        kg.add(Relationship(type="child", source=doc, target=chunk))
    kg.add(Relationship(
        type="entities_overlap", source=chunks[0], target=chunks[1], bidirectional=True,
        properties={"entities_overlap_score": 0.5, "overlapped_items": [["国家电网", "国网"]]},
    ))
    return kg


def test_enhanced_projection_kg_makes_all_three_types_available(tmp_path):
    from ragas.run_config import RunConfig
    from app.ragas_adapter.kg import enhance_existing_kg
    from app.ragas_adapter.testset import assess_kg_capabilities, check_generation_capabilities

    kg = _build_projection_kg()
    stats = enhance_existing_kg(kg, FakeLLM(), FakeEmbedding(), run_config=RunConfig())
    assert stats["changed"] is True
    assert stats["chunk_summaries"] == 2
    assert stats["document_summaries"] == 1
    assert stats["theme_chunks"] == 2
    assert stats["embeddings"] == 3
    assert stats["similarity_edges"] >= 1

    path = tmp_path / "kg-enhanced.json"
    kg.save(str(path))
    entry = {"id": "kg-enhanced", "kg_path": str(path), "mode": "ragflow-projection",
             "node_count": len(kg.nodes), "relationship_count": len(kg.relationships)}

    report = assess_kg_capabilities(entry)
    for key in ("single_hop_specific", "multi_hop_abstract", "multi_hop_specific"):
        assert report["synthesizers"][key]["available"] is True, report["synthesizers"][key]
    assert report["enhancement"]["will_enhance"] is False
    check_generation_capabilities(entry)


def test_enhancement_is_idempotent(tmp_path):
    from ragas.run_config import RunConfig
    from app.ragas_adapter.kg import enhance_existing_kg

    kg = _build_projection_kg()
    llm, emb = FakeLLM(), FakeEmbedding()
    enhance_existing_kg(kg, llm, emb, run_config=RunConfig())
    first_calls, first_edges = llm.calls, len(kg.relationships)

    stats = enhance_existing_kg(kg, llm, emb, run_config=RunConfig())
    assert stats["changed"] is False
    assert stats["chunk_summaries"] == 0 and stats["theme_chunks"] == 0
    assert stats["embeddings"] == 0
    assert llm.calls == first_calls          # 第二次不再发任何 LLM 调用
    assert emb.calls == 3                    # 也不再请求 embedding
    assert len(kg.relationships) == first_edges  # 不重复建 summary_similarity 边


def test_no_enhancement_calls_when_material_is_complete():
    from app.ragas_adapter.testset import _enhancement_needs

    complete_flags = {"summary_embedding_nodes": 10, "theme_chunks": 5,
                      "relation_property_edges": 3, "entity_nodes": 4, "overlap_edges": 2}
    active = {"single_hop_specific", "multi_hop_abstract", "multi_hop_specific"}
    assert _enhancement_needs({"mode": "ragflow-projection"}, complete_flags, active,
                              "summary_similarity") == []

    missing_flags = dict(complete_flags, summary_embedding_nodes=0)
    # 非投影 KG 不走自动增强（缺摘要向量应回 Step 1 重建）
    assert _enhancement_needs({"mode": "step1"}, missing_flags, active, "summary_similarity") == []


def test_enhancement_failure_raises_and_does_not_persist(tmp_path, monkeypatch):
    from app.ragas_adapter import testset as ts
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.tasks.manager import Task

    kg = _build_projection_kg()
    path = tmp_path / "kg-untouched.json"
    kg.save(str(path))
    entry = {"id": "kg-untouched", "kg_path": str(path), "mode": "ragflow-projection"}
    before = path.read_bytes()

    class FailingLLM(FakeLLM):
        async def agenerate_text(self, prompt, n=1, temperature=0.01, stop=None, callbacks=None):
            raise RuntimeError("provider 403")

    from app.ragas_adapter import testset_enhance  # 补丁打在真正调用的子模块上
    monkeypatch.setattr(testset_enhance, "build_llm", lambda *a, **k: FailingLLM())
    monkeypatch.setattr(testset_enhance, "build_embeddings", lambda *a, **k: FakeEmbedding())

    task = Task("testset")
    tele = GenerationTelemetry()
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(ts._enhance_projection_kg(
            task, entry, kg, {"base_url": "x", "model": "m", "api_key": "k"},
            {"base_url": "x", "model": "m", "api_key": "k"}, tele,
            None, max_retries=1, llm_timeout=30, max_workers=2,
        ))
    assert "自动增强失败" in str(excinfo.value)
    assert "未保存任何改动" in str(excinfo.value)
    assert path.read_bytes() == before


def test_budget_exceeded_raises_and_does_not_persist(tmp_path, monkeypatch):
    from app.ragas_adapter import kg as kg_adapter
    from app.ragas_adapter import testset as ts
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.tasks.manager import Task

    kg = _build_projection_kg()
    path = tmp_path / "kg-budget.json"
    kg.save(str(path))
    entry = {"id": "kg-budget", "kg_path": str(path), "mode": "ragflow-projection"}
    before = path.read_bytes()

    from app.ragas_adapter import testset_enhance  # 补丁打在真正调用的子模块上
    monkeypatch.setattr(testset_enhance, "build_llm", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(testset_enhance, "build_embeddings", lambda *a, **k: FakeEmbedding())
    monkeypatch.setattr(kg_adapter, "enhance_existing_kg", lambda *a, **k: {
        "version": 1, "chunk_summaries": 1, "document_summaries": 0,
        "theme_chunks": 0, "embeddings": 0, "similarity_edges": 0, "changed": True,
    })
    tele = GenerationTelemetry()
    tele.budget_exceeded = True

    task = Task("testset")
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(ts._enhance_projection_kg(
            task, entry, kg, {"base_url": "x", "model": "m", "api_key": "k"},
            {"base_url": "x", "model": "m", "api_key": "k"}, tele,
            None, max_retries=1, llm_timeout=30, max_workers=2,
        ))
    assert "超出 token 预算" in str(excinfo.value)
    assert path.read_bytes() == before


def _projection_with_one_changed_chunk() -> KnowledgeGraph:
    """模拟同步后的投影：两篇文档，doc1 的分块正文变了（没继承到增强），其余节点都已增强。"""
    from ragas.run_config import RunConfig

    from app.ragas_adapter.kg import enhance_existing_kg

    kg = _build_projection_kg()
    enhance_existing_kg(kg, FakeLLM(), FakeEmbedding(), run_config=RunConfig())
    doc2 = Node(type=NodeType.DOCUMENT, properties={"page_content": "变更.txt",
                                                    "document_metadata": {"filename": "变更.txt"}})
    changed = Node(type=NodeType.CHUNK, properties={"page_content": "国网调度中心新增夜间降级预案。",
                                                    "entities": ["国网"]})
    kg.add(doc2)
    kg.add(changed)
    kg.add(Relationship(type="child", source=doc2, target=changed))
    return kg


def test_enhancement_triggers_for_individual_chunks_missing_material():
    """rc3 对账发现：只要图里还有别的分块带摘要，新增 / 正文变化的分块就永远补不上。"""
    from app.ragas_adapter.testset import _enhancement_needs, _kg_flags

    kg = _projection_with_one_changed_chunk()
    entry = {"mode": "ragflow-projection"}
    flags = _kg_flags(kg, "summary_similarity")
    single = _enhancement_needs(entry, flags, {"single_hop_specific"}, "summary_similarity")
    assert single == ["新增或变化分块的摘要与向量（1 个分块）"]
    abstract = _enhancement_needs(entry, flags, {"multi_hop_abstract"}, "summary_similarity")
    assert "新增或变化分块的主题（1 个分块）" in abstract
    # Step 1 图谱不走自动增强
    assert _enhancement_needs({"mode": "ragas"}, flags, {"single_hop_specific"}, "summary_similarity") == []


def test_per_chunk_enhancement_only_fills_the_gap_and_estimate_matches():
    from ragas.run_config import RunConfig

    from app.ragas_adapter.kg import enhance_existing_kg
    from app.ragas_adapter.testset_cost import _estimate_enhancement

    kg = _projection_with_one_changed_chunk()
    est = _estimate_enhancement(kg, {"mode": "ragflow-projection"}, {"multi_hop_abstract"}, "summary_similarity")
    assert est["chunk_summaries"] == 1 and est["theme_chunks"] == 1
    assert est["embeddings"] == 2, "缺向量的分块 1 个 + 名下有分块且缺向量的文档 1 篇"
    llm, emb = FakeLLM(), FakeEmbedding()
    enhance_existing_kg(kg, llm, emb, run_config=RunConfig())
    assert llm.calls == est["llm_calls"] == 2, "只补缺的那个分块（摘要 1 + 主题 1），已增强的不重算"
    assert emb.calls == est["embeddings"]
    assert all(n.properties.get("summary_embedding") is not None for n in kg.nodes if n.type == NodeType.CHUNK)


def test_document_without_chunks_is_not_counted_as_pending_embedding():
    from app.ragas_adapter.testset_cost import _estimate_enhancement

    kg = _projection_with_one_changed_chunk()
    kg.add(Node(type=NodeType.DOCUMENT, properties={"page_content": "未解析.txt",
                                                    "document_metadata": {"filename": "未解析.txt"}}))
    est = _estimate_enhancement(kg, {"mode": "ragflow-projection"}, {"single_hop_specific"}, "summary_similarity")
    assert est["embeddings"] == 2, "没有分块的文档拼不出摘要，增强也不会给它做向量"


def test_enhancement_keeps_doc_identity_fields(tmp_path):
    """增强只往节点上补属性：阶段 3 的四字段必须在增强与落盘往返后仍在。"""
    from ragas.run_config import RunConfig

    from app.ragas_adapter.doc_identity import IDENTITY_KEYS
    from app.ragas_adapter.kg import _color_doc_identity, enhance_existing_kg

    kg = _build_projection_kg()
    _color_doc_identity(kg, [{"id": "doc-abc12345", "name": "调度规范.txt"}], "local")
    stats = enhance_existing_kg(kg, FakeLLM(), FakeEmbedding(), run_config=RunConfig())
    assert stats["changed"] is True

    path = tmp_path / "kg-identity.json"
    kg.save(str(path))
    loaded = KnowledgeGraph.load(str(path))
    for node in loaded.nodes:
        props = node.properties
        assert all(key in props for key in IDENTITY_KEYS), (node.type, props)
        assert props["doc_id"] == "doc-abc12345"
        assert props["source_type"] == "local"
