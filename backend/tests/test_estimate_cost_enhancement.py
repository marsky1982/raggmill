"""G1 / G2：成本预估要计入投影 KG 的生成前增强，以及抽象题遇文档级簇时的整篇展开。

预估口径：G1 预估完全不含增强（2000 chunk 的库光摘要就要约 2000 次调用）；
G2 文档级相似边让 ragas 把整篇文档塞进出题 prompt，实测 2 篇 × 12 chunk 时输入低估约 20 倍。
纯本地计算，0 token。
"""
from __future__ import annotations

import pytest
from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

ONLY_SINGLE = {"single_hop_specific": 1.0, "multi_hop_abstract": 0.0, "multi_hop_specific": 0.0}
ONLY_ABSTRACT = {"single_hop_specific": 0.0, "multi_hop_abstract": 1.0, "multi_hop_specific": 0.0}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _save(kg_id: str, kg: KnowledgeGraph, mode: str) -> None:
    from app import config, storage

    path = config.DATA_DIR / "kg" / f"{kg_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    kg.save(str(path))
    storage.kg_upsert({"id": kg_id, "name": kg_id, "kg_path": str(path), "mode": mode,
                       "created_at": storage.now_iso()})


def _projection(n_chunks: int) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    doc = Node(type=NodeType.DOCUMENT, properties={"page_content": "a.txt"})
    kg.add(doc)
    prev = None
    for i in range(n_chunks):
        c = Node(type=NodeType.CHUNK, properties={"page_content": "字" * 1030, "entities": [f"实体{i}"]})
        kg.add(c)
        kg.add(Relationship(type="child", source=doc, target=c))
        if prev is not None:
            kg.add(Relationship(type="entities_overlap", source=prev, target=c, bidirectional=True,
                                properties={"entities_overlap_score": 0.5, "overlapped_items": [["a", "a"]]}))
        prev = c
    return kg


def test_projection_estimate_includes_enhancement():
    from app.ragas_adapter.testset import estimate_generation_cost

    _save("kg-est-proj", _projection(50), "ragflow-projection")
    single = estimate_generation_cost("kg-est-proj", 5, ONLY_SINGLE, False)
    enh = single["enhancement"]
    assert enh["will_enhance"] is True
    assert enh["chunk_summaries"] == 50
    assert enh["theme_chunks"] == 0          # 只出单跳题不补 themes（与增强裁剪同口径）
    assert enh["embeddings"] == 51           # 50 chunk + 1 文档
    assert "生成前增强" in single["breakdown_tokens"]
    # 50 个 chunk 的增强远大于 5 条单跳题本身：这正是旧预估漏掉的大头
    assert single["breakdown_tokens"]["生成前增强"] > single["breakdown_tokens"]["样本·单跳出题"]
    assert single["llm_calls_estimate"] >= 50

    default = estimate_generation_cost("kg-est-proj", 5, None, True)
    assert default["enhancement"]["theme_chunks"] == 50   # 默认分布含抽象题 → 也补 themes


def test_step1_kg_has_no_enhancement():
    from app.ragas_adapter.testset import estimate_generation_cost

    _save("kg-est-step1", _projection(5), "ragas")
    est = estimate_generation_cost("kg-est-step1", 5, ONLY_SINGLE, False)
    assert est["enhancement"]["will_enhance"] is False
    assert "生成前增强" not in est["breakdown_tokens"]


def _two_docs_with_doc_level_edge(chunks_per_doc: int) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    docs = []
    for d in range(2):
        doc = Node(type=NodeType.DOCUMENT, properties={
            "page_content": f"d{d}", "summary": "摘要", "summary_embedding": [1.0, 0.0], "themes": ["t"]})
        kg.add(doc)
        docs.append(doc)
        for _ in range(chunks_per_doc):
            c = Node(type=NodeType.CHUNK, properties={
                "page_content": "字" * 1030, "summary": "s", "summary_embedding": [1.0, 0.0],
                "themes": ["t"], "entities": ["e"]})
            kg.add(c)
            kg.add(Relationship(type="child", source=doc, target=c))
    kg.add(Relationship(type="summary_similarity", source=docs[0], target=docs[1], bidirectional=True,
                        properties={"summary_similarity": 0.9}))
    return kg


def test_abstract_estimate_expands_document_level_clusters():
    from app.ragas_adapter.testset import estimate_generation_cost

    _save("kg-est-docedge", _two_docs_with_doc_level_edge(12), "ragas")
    est = estimate_generation_cost("kg-est-docedge", 4, ONLY_ABSTRACT, False)
    ctx = est["abstract_context"]
    assert ctx["doc_level_edges"] == 1 and ctx["chunk_level_edges"] == 0
    assert ctx["factor"] == pytest.approx(12.0)
    assert any("整篇展开" in line for line in est["assumptions"])

    # 同样条数、只有 chunk 级边时不放大：多跳抽象出题的输入相差约 12 倍
    from ragas.testset.graph import KnowledgeGraph as KG

    flat = KG()
    a = Node(type=NodeType.CHUNK, properties={"page_content": "字" * 1030, "summary": "s",
                                              "summary_embedding": [1.0, 0.0], "themes": ["t"]})
    b = Node(type=NodeType.CHUNK, properties={"page_content": "字" * 1030, "summary": "s",
                                              "summary_embedding": [1.0, 0.0], "themes": ["t"]})
    flat.add(a)
    flat.add(b)
    flat.add(Relationship(type="summary_similarity", source=a, target=b, bidirectional=True,
                          properties={"summary_similarity": 0.9}))
    _save("kg-est-chunkedge", flat, "ragas")
    base = estimate_generation_cost("kg-est-chunkedge", 4, ONLY_ABSTRACT, False)
    assert base["abstract_context"]["factor"] == 1.0
    ratio = est["breakdown_tokens"]["样本·多跳抽象出题"] / base["breakdown_tokens"]["样本·多跳抽象出题"]
    assert ratio > 5


def test_enhancement_estimate_matches_rc3_real_bill():
    """2026-11-06 重标定回归：rc3 实测 2 个分块（合计约 793 字符）摘要 + 主题，LLM 实际 4,705 token。

    旧系数预估 3,493（含向量 800，LLM 部分只有 2,693），偏低约 55%，主要是输出侧低估；新系数要落在实测 ±15% 内。
    """
    from app.ragas_adapter.testset import estimate_generation_cost

    kg = KnowledgeGraph()
    chunks = []
    for name, chars in (("一.txt", 397), ("二.txt", 396)):
        doc = Node(type=NodeType.DOCUMENT, properties={"page_content": name})
        chunk = Node(type=NodeType.CHUNK, properties={"page_content": "字" * chars, "entities": ["星河物流"]})
        kg.add(doc)
        kg.add(chunk)
        kg.add(Relationship(type="child", source=doc, target=chunk))
        chunks.append(chunk)
    kg.add(Relationship(type="entities_overlap", source=chunks[0], target=chunks[1], bidirectional=True,
                        properties={"entities_overlap_score": 0.5, "overlapped_items": [["星河物流", "星河物流"]]}))
    _save("kg-est-rc3", kg, "ragflow-projection")
    enh = estimate_generation_cost("kg-est-rc3", 3, None, True)["enhancement"]
    assert enh["chunk_summaries"] == 2 and enh["theme_chunks"] == 2 and enh["embeddings"] == 4
    llm_tokens = enh["input_tokens"] + enh["output_tokens"]
    assert abs(llm_tokens - 4705) / 4705 < 0.15, llm_tokens
    assert enh["output_tokens"] > enh["input_tokens"], "带思维链的模型增强输出长于输入"


def test_telemetry_labels_enhancement_prompts():
    """增强的两类 prompt 以前记成「未识别 prompt」，账单里看不出摘要与主题各花多少。"""
    from ragas.testset.transforms.extractors.llm_based import (
        SummaryExtractorPrompt,
        ThemesAndConceptsExtractorPrompt,
    )

    from app.ragas_adapter.telemetry import GenerationTelemetry

    tel = GenerationTelemetry()
    assert tel._label_of(SummaryExtractorPrompt.instruction + "\n正文……") == "增强·分块摘要"
    assert tel._label_of(ThemesAndConceptsExtractorPrompt.instruction + "\n正文……") == "增强·主题"
