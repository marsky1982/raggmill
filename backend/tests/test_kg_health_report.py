"""质检报告判断口径回归。

两处被修的口径：
- 单跳具体题 ragas 读节点的 entities（single_hop/specific.py:44），与 themes 无关；
- needs_rebuild 必须把文档级 summary_similarity 边算进来（ragas 原生只建文档级）。

测试全部用合成 KG，不连后端、不调真实 LLM。
"""
from __future__ import annotations

import pytest


def _save_and_register(tmp_path, kg_id: str, kg) -> dict:
    """把合成 KG 落盘并写进索引，供 kg_health_report(kg_id) 读取。"""
    from app import config, storage

    config.ensure_data_dirs()
    path = tmp_path / f"{kg_id}.json"
    kg.save(str(path))
    entry = {
        "id": kg_id, "name": kg_id, "created_at": "", "mode": "step1",
        "kg_path": str(path), "node_count": len(kg.nodes),
        "relationship_count": len(kg.relationships), "doc_ids": [], "doc_names": [],
        "llm_preset_id": None, "embedding_preset_id": None,
    }
    storage.kg_upsert(entry)
    return entry


def test_single_hop_specific_uses_entities_not_themes(tmp_path):
    """有 entities、没 themes 的分块：单跳具体题应判为可用，且报告不再要求 themes。"""
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    from app.ragas_adapter.testset import assess_kg_capabilities, kg_health_report

    kg = KnowledgeGraph()
    kg.add(Node(type=NodeType.CHUNK, properties={
        "page_content": "分块正文",
        "entities": ["国家电网"],
        "summary": "分块摘要",
        "summary_embedding": [0.1, 0.2],
    }))
    entry = _save_and_register(tmp_path, "kg-case-health-entities-only", kg)

    report = kg_health_report("kg-case-health-entities-only")
    single = report["synthesizer_availability"]["SingleHopSpecificQuery"]
    assert single["available"] is True
    assert single["status"] == "可用"
    assert "实体标签（entities）" in single["reason"]
    # 旧文案「需要文本分块上有主题标签（themes）」不能再出现
    assert "需要文本分块（chunk）上有主题标签" not in single["reason"]

    # 与生成前能力判定同一套口径
    caps = assess_kg_capabilities(entry)
    assert caps["synthesizers"]["single_hop_specific"]["available"] is True


def test_empty_entities_list_is_not_material(tmp_path):
    """entities 为空列表等同于没有素材，不能被算作可用。"""
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    from app.ragas_adapter.testset import kg_health_report

    kg = KnowledgeGraph()
    kg.add(Node(type=NodeType.CHUNK, properties={
        "page_content": "分块正文",
        "entities": [],
        "themes": ["电网调度"],
    }))
    _save_and_register(tmp_path, "kg-case-health-empty-entities", kg)

    report = kg_health_report("kg-case-health-empty-entities")
    assert report["synthesizer_availability"]["SingleHopSpecificQuery"]["available"] is False


def test_needs_rebuild_counts_document_level_similarity_edges(tmp_path):
    """只有文档↔文档 summary_similarity 边（chunk 级没有）时，不应误判需要重建。"""
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    from app.ragas_adapter.testset import kg_health_report

    d1 = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "文档一", "summary": "摘要一", "summary_embedding": [1.0, 0.0],
    })
    d2 = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "文档二", "summary": "摘要二", "summary_embedding": [1.0, 0.0],
    })
    c1 = Node(type=NodeType.CHUNK, properties={"page_content": "分块一", "entities": ["国家电网"]})
    c2 = Node(type=NodeType.CHUNK, properties={"page_content": "分块二", "entities": ["国网"]})

    kg = KnowledgeGraph()
    for node in (d1, d2, c1, c2):
        kg.add(node)
    kg.add(Relationship(type="child", source=d1, target=c1))
    kg.add(Relationship(type="child", source=d2, target=c2))
    kg.add(Relationship(type="summary_similarity", source=d1, target=d2, bidirectional=True,
                        properties={"summary_similarity": 0.9}))
    _save_and_register(tmp_path, "kg-case-health-doc-sim-only", kg)

    report = kg_health_report("kg-case-health-doc-sim-only")
    assert report["needs_rebuild"] is False
    assert report["summary"]["verdict"] == "可用"
    assert report["edge_stats"]["summary_similarity_chunk_chunk"] == 0
    assert report["edge_stats"]["summary_similarity_doc_doc"] == 1
    # 文档级语义相似边同样可被多跳抽象题使用，推荐值应保持默认的 summary_similarity
    assert report["recommended_relation_property"] == "summary_similarity"
    assert report["synthesizer_availability"]["MultiHopAbstractQuery"]["available"] is True


def test_truly_empty_kg_still_reports_missing_material(tmp_path):
    """三者都缺的空 KG 仍须正确报缺失（防止回归测试变成永真）。"""
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    from app.ragas_adapter.testset import kg_health_report

    kg = KnowledgeGraph()
    kg.add(Node(type=NodeType.DOCUMENT, properties={"page_content": "文档"}))
    kg.add(Node(type=NodeType.CHUNK, properties={"page_content": "分块一"}))
    kg.add(Node(type=NodeType.CHUNK, properties={"page_content": "分块二"}))
    _save_and_register(tmp_path, "kg-case-health-empty", kg)

    report = kg_health_report("kg-case-health-empty")
    assert report["needs_rebuild"] is True
    assert report["summary"]["verdict"] == "需重建"
    assert report["recommended_relation_property"] == "（无可用）"
    for item in report["synthesizer_availability"].values():
        assert item["available"] is False
