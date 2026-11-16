"""多跳合成器的概念对齐（2026-11-06 rc3 小图出题实测）。

ragas 多跳合成器筛节点 / 人格用的是逐字相等：LLM 给出的概念组合只要和节点 themes / entities
差一个字（「与」写成「和」、多一个空格），整条组合就没有可用节点，小图只有 1 个簇时该题型直接 0 场景。
这里验证：概念先对齐到簇内节点真实存在的词，再交给 ragas 原实现；对不上的保留原词。
不调 LLM、0 token。测试模块顶层不 import app。
"""
from __future__ import annotations


def _chunk(themes: list[str] | None = None, entities: list[str] | None = None):
    from ragas.testset.graph import Node, NodeType

    props = {"page_content": "分块正文"}
    if themes is not None:
        props["themes"] = themes
    if entities is not None:
        props["entities"] = entities
    return Node(type=NodeType.CHUNK, properties=props)


def test_term_key_treats_connectors_spacing_and_width_as_same():
    from app.ragas_adapter.testset import _term_key

    assert _term_key("OR-Tools和阿里云部署") == _term_key("OR-Tools与阿里云部署")
    assert _term_key("ＯＲ－Ｔｏｏｌｓ 及 阿里云部署") == _term_key("or-tools与阿里云部署")
    assert _term_key("Route Planning and Scheduling") == _term_key("route planning & scheduling")
    assert _term_key("《调度平台》") == _term_key("调度平台")


def test_canonical_term_order_exact_normalized_contained_similar_else_none():
    from app.ragas_adapter.testset import _canonical_term

    vocab = ["物流调度平台建设", "OR-Tools与阿里云部署", "司机排班"]
    assert _canonical_term("司机排班", vocab) == "司机排班"
    assert _canonical_term("OR-Tools 和 阿里云部署", vocab) == "OR-Tools与阿里云部署"
    assert _canonical_term("调度平台建设", vocab) == "物流调度平台建设"  # 互相包含
    assert _canonical_term("物流调度平台的建设", vocab) == "物流调度平台建设"  # 相似度 ≥ 0.8
    assert _canonical_term("财务报销流程", vocab) is None
    assert _canonical_term("  ", vocab) is None


def test_align_multihop_concepts_rewrites_combinations_and_persona_lists():
    from app.ragas_adapter.testset import _align_multihop_concepts

    nodes = [_chunk(themes=["物流调度平台建设"]), _chunk(themes=["OR-Tools与阿里云部署"])]
    combos = [["物流调度平台 建设", "OR-Tools和阿里云部署"], ["财务报销流程"]]
    mapping = {"张工": ["OR-Tools和阿里云部署"], "李经理": ["物流调度平台建设"]}
    new_combos, new_mapping, aligned = _align_multihop_concepts(nodes, combos, mapping, "themes")
    assert new_combos == [["物流调度平台建设", "OR-Tools与阿里云部署"], ["财务报销流程"]]
    assert new_mapping == {"张工": ["OR-Tools与阿里云部署"], "李经理": ["物流调度平台建设"]}
    assert aligned == 3


def test_align_is_noop_without_vocabulary():
    from app.ragas_adapter.testset import _align_multihop_concepts

    combos, mapping = [["甲", "乙"]], {"张工": ["甲"]}
    assert _align_multihop_concepts([_chunk()], combos, mapping, "themes") == (combos, mapping, 0)


def _synth(key: str):
    from app.ragas_adapter.testset import _build_persona_safe_synthesizer

    return _build_persona_safe_synthesizer(key, llm=None, llm_ctx=None,
                                           abstract_relation_property="summary_similarity")


def test_abstract_synthesizer_keeps_paraphrased_combination_usable():
    """改写过的组合在 ragas 原实现里会 0 节点 0 人格；对齐后两者都在。"""
    from ragas.testset.persona import Persona
    from ragas.testset.synthesizers.multi_hop.base import MultiHopQuerySynthesizer

    nodes = [_chunk(themes=["物流调度平台建设"]), _chunk(themes=["OR-Tools与阿里云部署"])]
    personas = [Persona(name="张工", role_description="运维")]
    combos = [["物流调度平台建设 ", "OR-Tools和阿里云部署"]]
    mapping = {"张工": ["OR-Tools 及阿里云部署"]}  # 人格匹配那一步 LLM 又换了一种写法

    raw = MultiHopQuerySynthesizer.prepare_combinations(
        _synth("multi_hop_abstract"), nodes, [["物流调度平台建设 ", "OR-Tools和阿里云部署"]],
        personas, {"张工": ["OR-Tools 及阿里云部署"]}, "themes")
    assert raw[0]["nodes"] == [] and raw[0]["personas"] == [], "前提：ragas 原实现逐字匹配会丢掉这条组合"

    synth = _synth("multi_hop_abstract")
    out = synth.prepare_combinations(nodes, combos, personas, mapping, "themes")
    assert len(out[0]["nodes"]) == 2
    assert [p.name for p in out[0]["personas"]] == ["张工"]
    assert synth._concept_aligns == 3


def test_specific_synthesizer_aligns_against_entities():
    from ragas.testset.persona import Persona

    nodes = [_chunk(entities=["星河物流", "OR-Tools"]), _chunk(entities=["阿里云 ECS"])]
    personas = [Persona(name="张工", role_description="运维")]
    synth = _synth("multi_hop_specific")
    out = synth.prepare_combinations(nodes, [["星河 物流", "阿里云ECS"]], personas,
                                     {"张工": ["星河物流"]}, "entities")
    assert len(out[0]["nodes"]) == 2
    assert [p.name for p in out[0]["personas"]] == ["张工"]
    assert synth._concept_aligns == 2
