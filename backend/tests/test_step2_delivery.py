"""Step 2 修复回归（真跑暴露的四类问题）。

覆盖：
- 能力判定不再承诺「自动补齐一定能建出 summary_similarity 边」；
- persona 名称归一化 / 降级为 1 人格，不再整类丢题；
- 少出条数写入 usage + undelivered，并标记 partial；
- 生成前增强的 token 有独立分项。
"""
from __future__ import annotations

import asyncio
import math

import pytest


def _kg_entry(tmp_path, *, cosine: float, with_summary_edge: bool = False,
              with_embeddings: bool = True, mode: str = "ragflow-projection") -> dict:
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    doc_props = {"page_content": "文档", "document_metadata": {"filename": "a.txt"}}
    if with_embeddings:
        doc_props["summary_embedding"] = [1.0, 0.0]
    doc = Node(type=NodeType.DOCUMENT, properties=doc_props)

    def chunk(index: int, entities: list[str], vector: list[float]) -> Node:
        props = {"page_content": f"分块{index}", "entities": entities,
                 "themes": ["电网调度"]}
        if with_embeddings:
            props["summary_embedding"] = vector
        return Node(type=NodeType.CHUNK, properties=props)

    c1 = chunk(1, ["国家电网"], [1.0, 0.0])
    c2 = chunk(2, ["国网"], [cosine, math.sqrt(max(0.0, 1 - cosine ** 2))])
    kg = KnowledgeGraph()
    for node in (doc, c1, c2):
        kg.add(node)
    kg.add(Relationship(type="child", source=doc, target=c1))
    kg.add(Relationship(type="child", source=doc, target=c2))
    kg.add(Relationship(type="entities_overlap", source=c1, target=c2, bidirectional=True,
                        properties={"entities_overlap_score": 0.5,
                                    "overlapped_items": [["国家电网", "国网"]]}))
    if with_summary_edge:
        kg.add(Relationship(type="summary_similarity", source=c1, target=c2, bidirectional=True,
                            properties={"summary_similarity": cosine}))

    path = tmp_path / "kg.json"
    kg.save(str(path))
    return {"id": "kg-test", "kg_path": str(path), "mode": mode,
            "node_count": len(kg.nodes), "relationship_count": len(kg.relationships)}


def test_capability_does_not_promise_unreachable_similarity_edges(tmp_path):
    from app.ragas_adapter.testset import assess_kg_capabilities, check_generation_capabilities

    entry = _kg_entry(tmp_path, cosine=0.634)
    report = assess_kg_capabilities(entry)
    estimate = report["similarity_estimate"]
    assert estimate["embedded_nodes"] == 2
    assert estimate["reachable_edges"] == 0
    assert estimate["max_pairwise_similarity"] == pytest.approx(0.634, abs=1e-9)

    abstract = report["synthesizers"]["multi_hop_abstract"]
    assert abstract["available"] is False
    assert abstract["effective_available"] is False
    assert abstract["auto_enhanceable"] is False
    assert "0.634" in abstract["reason"]
    assert "entities_overlap_score" in abstract["reason"]

    # 默认分布含抽象题 -> 0 token 直接拒绝，并给出可操作建议
    with pytest.raises(ValueError) as excinfo:
        check_generation_capabilities(entry)
    assert "entities_overlap_score" in str(excinfo.value)

    # 把抽象题比例设为 0 时，其余两类照常放行
    check_generation_capabilities(
        entry,
        distribution={"single_hop_specific": 1.0, "multi_hop_abstract": 0.0,
                      "multi_hop_specific": 0.0},
        use_default=False,
    )


def test_capability_reports_reachable_edges_when_threshold_is_met(tmp_path):
    from app.ragas_adapter.testset import assess_kg_capabilities

    report = assess_kg_capabilities(_kg_entry(tmp_path, cosine=0.9))
    assert report["similarity_estimate"]["reachable_edges"] == 1
    abstract = report["synthesizers"]["multi_hop_abstract"]
    assert abstract["auto_enhanceable"] is True
    assert abstract["effective_available"] is True
    assert "预计可建出 1 条" in abstract["reason"]


def test_projection_without_vectors_allows_probe_but_no_promise(tmp_path):
    from app.ragas_adapter.testset import assess_kg_capabilities

    report = assess_kg_capabilities(_kg_entry(tmp_path, cosine=0.634, with_embeddings=False))
    abstract = report["synthesizers"]["multi_hop_abstract"]
    # 没有向量时无法预判：允许先增强再复核，但明确不承诺自动补齐
    assert abstract["effective_available"] is True
    assert abstract["auto_enhanceable"] is False
    assert abstract["requires_probe"] is True
    assert "复核" in abstract["reason"]


def test_precheck_uses_actionable_similarity_message(tmp_path):
    from ragas.testset.graph import KnowledgeGraph

    from app.ragas_adapter.testset import _precheck_kg

    entry = _kg_entry(tmp_path, cosine=0.634)
    kg = KnowledgeGraph.load(entry["kg_path"])
    with pytest.raises(ValueError) as excinfo:
        _precheck_kg(kg, {"multi_hop_abstract"}, "summary_similarity")
    message = str(excinfo.value)
    assert "0.634" in message and "entities_overlap_score" in message


def test_persona_name_key_normalizes_case_spaces_and_role_parenthetical():
    from app.ragas_adapter.testset import _persona_name_key

    assert _persona_name_key("Chen Wei (State Grid provincial investment planning analyst)") == "chen wei"
    assert _persona_name_key("  CHEN   WEI  ") == "chen wei"
    assert _persona_name_key("Chen Wei — 电网规划岗") == "chen wei"


def test_normalize_persona_mapping_falls_back_instead_of_dropping_category():
    from ragas.testset.persona import Persona

    from app.ragas_adapter.testset import _normalize_persona_mapping

    personas = [Persona(name="Chen Wei", role_description="规划"),
                Persona(name="Li Na", role_description="调度")]
    mapping = {
        "Chen Wei (State Grid provincial investment planning analyst)": ["电网规划"],
        "li na": ["调度运行"],
        "王五": ["无法匹配的条目"],
    }
    normalized, remapped = _normalize_persona_mapping(mapping, personas)
    assert set(normalized) == {"Chen Wei", "Li Na"}
    assert set(normalized["Chen Wei"]) == {"电网规划", "无法匹配的条目"}
    assert normalized["Li Na"] == ["调度运行"]
    assert remapped == 3


def test_scenario_job_downgrades_to_single_persona_instead_of_skipping_type():
    from ragas.testset.persona import Persona

    from app.ragas_adapter.testset import _PhasedGeneration

    class FakeSynth:
        name = "fake"

        def __init__(self):
            self.calls = []

        async def generate_scenarios(self, n, knowledge_graph, persona_list, callbacks=None):
            self.calls.append(list(persona_list))
            if len(persona_list) > 1:
                raise KeyError("No persona found with name 'Chen Wei (角色描述)'")
            return [f"scenario-{i}" for i in range(n)]

    runner = _PhasedGeneration(None, None, [], 1, None, [])
    runner.personas = [Persona(name="Chen Wei", role_description="规划"),
                       Persona(name="Li Na", role_description="调度")]
    synth = FakeSynth()
    sink: dict = {}
    job = runner._scenario_job(0, synth, sink)

    asyncio.run(job(n=2, knowledge_graph=None))

    assert len(synth.calls) == 2              # 第一次全量人格失败，第二次降级重试
    assert len(synth.calls[1]) == 1
    assert sink[0] == ["scenario-0", "scenario-1"]
    assert runner.scenario_failures == []
    assert any("降级为 1 个人格" in note for note in runner.drain_notes())


@pytest.mark.parametrize("requested,planned,delivered,missing", [
    (5, 4, 4, 1),   # 场景阶段少出 1 条
    (5, 5, 4, 1),   # 场景够但样本失败 1 条
    (5, 5, 5, 0),
])
def test_delivery_gap_reports_each_shortfall(requested, planned, delivered, missing):
    from app.ragas_adapter.testset import _delivery_gap

    gap = _delivery_gap(requested, planned, delivered)
    assert gap["total_missing"] == missing
    assert gap["not_planned"] == max(0, requested - planned)
    assert gap["not_delivered"] == max(0, planned - delivered)


def test_enhancement_usage_is_split_from_generation_billing():
    from app.ragas_adapter.testset import _enhancement_usage

    before = {"input_tokens": 100, "output_tokens": 50, "embedding_tokens": 20,
              "llm_calls": 2, "embedding_calls": 3}
    after = {"input_tokens": 900, "output_tokens": 700, "embedding_tokens": 320,
             "llm_calls": 14, "embedding_calls": 9}
    usage = _enhancement_usage(before, after)
    assert usage["input_tokens"] == 800
    assert usage["output_tokens"] == 650
    assert usage["llm_tokens"] == 1450
    assert usage["embedding_tokens"] == 300
    assert usage["total_tokens"] == 1750
    assert usage["llm_calls"] == 12
    assert usage["embedding_calls"] == 6

    empty = _enhancement_usage({}, {})
    assert empty["total_tokens"] == 0 and empty["llm_calls"] == 0
