"""KG 生成能力判定回归测试。

用真实 ragas KnowledgeGraph 构造最小图谱，验证：
- 素材齐全的投影 KG 能进入生成；
- 缺 summary_embedding / 抽象题连线依据时给出可读拒绝原因；
- 旧 generation_ready 字段仍可读，但不再作为唯一开关。
"""
from __future__ import annotations

import pytest


def _write_kg(
    tmp_path,
    *,
    with_embedding: bool = True,
    with_entities: bool = True,
    with_themes: bool = True,
    with_summary_edge: bool = True,
    with_overlap_edge: bool = True,
    legacy_ready: bool | None = None,
    mode: str = "ragflow-projection",
) -> dict:
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    doc_props = {"page_content": "文档", "document_metadata": {"filename": "a.txt"}}
    if with_embedding:
        doc_props["summary_embedding"] = [0.1, 0.2]
    doc = Node(type=NodeType.DOCUMENT, properties=doc_props)

    def chunk(index: int) -> Node:
        props = {"page_content": f"分块{index}", "source_chunk_id": f"c{index}"}
        if with_embedding:
            props["summary_embedding"] = [float(index), 0.2]
        if with_entities:
            props["entities"] = ["国家电网"] if index == 1 else ["国网"]
        if with_themes:
            props["themes"] = ["电网调度"]
        return Node(type=NodeType.CHUNK, properties=props)

    c1, c2 = chunk(1), chunk(2)
    kg = KnowledgeGraph()
    for node in (doc, c1, c2):
        kg.add(node)
    if with_summary_edge:
        kg.add(Relationship(type="summary_similarity", source=c1, target=c2, bidirectional=True,
                            properties={"summary_similarity": 0.9}))
    if with_overlap_edge:
        kg.add(Relationship(type="entities_overlap", source=c1, target=c2, bidirectional=True,
                            properties={"entities_overlap_score": 0.5,
                                        "overlapped_items": [["国家电网", "国网"]]}))

    path = tmp_path / "kg.json"
    kg.save(path)
    entry = {"id": "kg-test", "kg_path": str(path), "mode": mode,
             "node_count": len(kg.nodes), "relationship_count": len(kg.relationships)}
    if legacy_ready is not None:
        entry["generation_ready"] = legacy_ready
    return entry


def test_capable_projection_kg_can_enter_generation(tmp_path):
    from app.ragas_adapter.testset import check_generation_capabilities

    entry = _write_kg(tmp_path)
    report = check_generation_capabilities(entry)  # 默认分布：三类全开

    assert report["usable"] is True
    assert report["synthesizers"]["single_hop_specific"]["available"] is True
    assert report["synthesizers"]["multi_hop_abstract"]["available"] is True
    assert report["synthesizers"]["multi_hop_specific"]["available"] is True


def test_missing_summary_embedding_is_rejected_with_readable_reason(tmp_path):
    from app.ragas_adapter.testset import check_generation_capabilities

    # 非投影 KG 缺摘要向量属于 Step 1 产物缺失，不能被生成前增强掩盖，必须明确拒绝
    entry = _write_kg(tmp_path, with_embedding=False, mode="step1")
    with pytest.raises(ValueError) as excinfo:
        check_generation_capabilities(entry)

    message = str(excinfo.value)
    assert "summary_embedding" in message
    assert "多跳抽象题" in message
    assert "未消耗任何 token" in message


def test_projection_kg_with_fillable_gaps_is_allowed_with_hint(tmp_path):
    """投影 KG 缺的素材可在生成前自动补齐，能力判定不再硬拦。"""
    from app.ragas_adapter.testset import assess_kg_capabilities, check_generation_capabilities

    entry = _write_kg(tmp_path, with_embedding=False)
    report = assess_kg_capabilities(entry)
    assert report["enhancement"]["supported"] is True
    assert report["enhancement"]["will_enhance"] is True
    for item in report["synthesizers"].values():
        assert item["available"] is False
        assert item["effective_available"] is True
        assert item["auto_enhanceable"] is True
        assert "生成前会自动补齐" in item["reason"]

    # 三类题型全部放行（实际增强在生成动作里做）
    check_generation_capabilities(entry)


def test_abstract_blocked_while_single_and_specific_still_allowed(tmp_path):
    from app.ragas_adapter.testset import assess_kg_capabilities, check_generation_capabilities

    # 有 persona/实体/重叠边，但缺 abstract 需要的连线依据与 themes
    entry = _write_kg(tmp_path, with_themes=False, with_summary_edge=False, mode="step1")
    report = assess_kg_capabilities(entry)
    abstract = report["synthesizers"]["multi_hop_abstract"]
    assert abstract["available"] is False
    assert "连线依据" in abstract["reason"] and "summary_similarity" in abstract["reason"]
    assert report["synthesizers"]["single_hop_specific"]["available"] is True
    assert report["synthesizers"]["multi_hop_specific"]["available"] is True

    # 分布里不含抽象题 -> 放行
    check_generation_capabilities(
        entry,
        distribution={"single_hop_specific": 1.0, "multi_hop_abstract": 0.0, "multi_hop_specific": 0.0},
        use_default=False,
    )
    # 一旦要抽象题 -> 明确拒绝
    with pytest.raises(ValueError) as excinfo:
        check_generation_capabilities(
            entry,
            distribution={"single_hop_specific": 0.0, "multi_hop_abstract": 1.0, "multi_hop_specific": 0.0},
            use_default=False,
        )
    assert "多跳抽象题" in str(excinfo.value)


def test_legacy_generation_ready_is_read_but_not_a_gate(tmp_path):
    from app.ragas_adapter.testset import assess_kg_capabilities, check_generation_capabilities

    entry = _write_kg(tmp_path, with_embedding=False, legacy_ready=True, mode="step1")
    report = assess_kg_capabilities(entry)
    assert report["generation_ready"] is True  # 旧字段仍被读取回显

    # 但能力不足照样拒绝：generation_ready 不再作为唯一开关
    with pytest.raises(ValueError):
        check_generation_capabilities(entry)
