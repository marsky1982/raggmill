"""Step 2 质检报告：KG 素材体检与面向非技术用户的解释。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

from ..storage import kg_get
from .testset_capabilities import _kg_flags, _recommend_relation_property, _synth_material_ok


# ---------- 质检报告 ----------

# 质检报告术语表：面向非技术用户，格式为「中文（英文原名）」
_PROP_LABELS = {
    "page_content": "分块原文（page_content）",
    "themes": "主题标签（themes）",
    "entities": "实体标签（entities）",
    "summary": "分块摘要（summary）",
    "summary_embedding": "摘要向量（summary_embedding）",
    "embedding": "通用向量（embedding）",
    "keyphrases": "关键词（keyphrases）",
    "document_metadata": "文档元数据（document_metadata）",
}


_EDGE_LABELS = {
    "child": "上下级（child）",
    "next": "顺序相邻（next）",
    "summary_similarity": "语义相似（summary_similarity）",
    "entities_overlap": "实体重叠（entities_overlap）",
}


def kg_health_report(kg_id: str) -> dict:
    """生成 KG 质检报告：节点/边/属性覆盖率 + 各合成器可用性推断 + 推荐 relation_property。

    返回结构供前端模态框渲染：每项指标含 value + explain 文本，便于用户理解。
    """
    from collections import Counter
    from ragas.testset.graph import KnowledgeGraph, NodeType

    entry = kg_get(kg_id)
    if not entry:
        raise ValueError(f"知识图谱 {kg_id} 不存在")

    kg = KnowledgeGraph.load(entry["kg_path"])
    # 素材统计统一走 _kg_flags（与生成前预检、能力判定同一套口径，避免第二份判断逻辑漂移）
    flags = _kg_flags(kg, "summary_similarity")

    def is_chunk(node) -> bool:
        return node.type == NodeType.CHUNK or str(node.type).lower() == "chunk"

    def is_doc(node) -> bool:
        return node.type == NodeType.DOCUMENT or str(node.type).lower() == "document"

    # 节点统计
    doc_count = sum(1 for n in kg.nodes if is_doc(n))
    chunk_count = sum(1 for n in kg.nodes if is_chunk(n))
    other_count = len(kg.nodes) - doc_count - chunk_count

    # chunk 属性覆盖率
    chunk_props_cov: Counter = Counter()
    for n in kg.nodes:
        if is_chunk(n):
            for k in n.properties.keys():
                chunk_props_cov[k] += 1

    # 边统计 + 区分 chunk×chunk vs document×document（拆分口径来自 _kg_flags）
    edge_types: Counter = Counter(r.type for r in kg.relationships)
    sim_chunk_chunk = flags["similarity_edges_chunk_chunk"]
    sim_doc_doc = flags["similarity_edges_doc_doc"]
    overlap_chunk_chunk = flags["overlap_edges_chunk_chunk"]
    overlap_cross_doc = flags["overlap_edges_cross_doc"]

    chunk_has_entities = chunk_props_cov.get("entities", 0)

    # 推荐「多跳连线依据」（技术字段：relation_property）
    recommended_rp, recommended_label, rp_reason = _recommend_relation_property(flags)

    # 语义相似边文档级与分块级都算：ragas 原生只建文档级，文档级边会被展开成该文档的全部分块
    needs_rebuild = flags["similarity_edges"] == 0 and flags["chunk_summary_nodes"] == 0

    # 各题型（合成器）可用性推断
    single_ok = _synth_material_ok(flags, "single_hop_specific")
    abstract_ok = _synth_material_ok(flags, "multi_hop_abstract")
    specific_ok = _synth_material_ok(flags, "multi_hop_specific")
    synth_avail = {
        "SingleHopSpecificQuery": {
            "label": "单跳具体题（SingleHopSpecificQuery）",
            "available": single_ok,
            "status": "可用" if single_ok else "不可用",
            "reason": (
                f"需要节点带实体标签（entities）—— ragas 的单跳具体题直接读它出题，"
                f"不看主题标签（themes）。当前 {chunk_count} 个分块中 {chunk_has_entities} 个有实体标签，"
                f"全图带实体标签的节点共 {flags['entity_nodes']} 个。"
            ),
            "desc": "从单个分块里提取关键信息，出一道答案明确的具体问题。",
        },
        "MultiHopAbstractQuery": {
            "label": "多跳抽象题（MultiHopAbstractQuery）",
            "available": abstract_ok,
            "status": "可用" if abstract_ok else "不可用",
            "reason": (
                f"需要节点之间有可用的连线（语义相似边或实体重叠边）。"
                f"当前语义相似边 {flags['similarity_edges']} 条"
                f"（分块之间 {sim_chunk_chunk} 条、文档之间 {sim_doc_doc} 条），"
                f"实体重叠边 {flags['overlap_edges']} 条。"
                f"文档级的语义相似边会被展开成该文档的全部分块，同样能出抽象题。"
            ),
            "relation_property": recommended_rp,
            "relation_property_label": recommended_label,
            "desc": "跨多个分块归纳总结，出一道需要综合理解的概括性问题。",
        },
        "MultiHopSpecificQuery": {
            "label": "多跳具体题（MultiHopSpecificQuery）",
            "available": specific_ok,
            "status": "可用" if specific_ok else "不可用",
            "reason": (
                f"需要实体重叠边（entities_overlap），当前 {flags['overlap_edges']} 条"
                f"（分块之间 {overlap_chunk_chunk} 条）。"
            ),
            "desc": "找到两个提到相同实体的分块，出一道需要同时参考两处内容的具体问题。",
        },
    }

    # 总评：给非技术用户的一句话结论（演示模式的样本带 is_mock，别按真实生成展示）
    demo = entry.get("mode") == "demo" or bool(entry.get("is_mock"))
    mode_label = "示例" if demo else "真实生成"
    if needs_rebuild:
        verdict = "需重建"
        headline = "这份知识图谱缺少关键素材，建议重建后再生成测试集"
        rebuild_reason = (
            "文本分块（chunk）里没有摘要，整份图谱也没有任何语义相似边"
            "（summary_similarity，文档级与分块级都算），"
            "多跳抽象题会生成不出来。重建图谱后，新流水线会自动补上"
            "分块摘要（summary）、摘要向量（summary_embedding）与语义相似边（summary_similarity）。"
        )
    else:
        verdict = "可用"
        headline = "这份知识图谱素材齐全，可以直接生成测试集"
        rebuild_reason = "图谱里已有语义相似边（文档级或分块级）或分块摘要，关键素材不再缺失。"

    node_explain = (
        f"共 {len(kg.nodes)} 个节点：{doc_count} 篇文档（document）、"
        f"{chunk_count} 个文本分块（chunk）"
    )
    if other_count:
        node_explain += f"、{other_count} 个其他类型节点"

    return {
        "kg_id": kg_id,
        "name": entry.get("name", ""),
        "mode": entry.get("mode", ""),
        "mode_label": mode_label,
        "created_at": entry.get("created_at", ""),
        "source_files": entry.get("source_files") or entry.get("doc_names") or [],
        "summary": {
            "verdict": verdict,
            "headline": headline,
            "detail": rebuild_reason,
        },
        "node_stats": {
            "total": len(kg.nodes),
            "document": doc_count,
            "chunk": chunk_count,
            "other": other_count,
            "explain": node_explain + "。",
        },
        "edge_stats": {
            "total": len(kg.relationships),
            "by_type": dict(edge_types),
            "by_type_labeled": [
                {"key": t, "label": _EDGE_LABELS.get(t, t), "count": c}
                for t, c in edge_types.most_common()
            ],
            "summary_similarity_chunk_chunk": sim_chunk_chunk,
            "summary_similarity_doc_doc": sim_doc_doc,
            "entities_overlap": flags["overlap_edges"],
            "entities_overlap_cross_doc": overlap_cross_doc,
            "explain": (
                f"共 {len(kg.relationships)} 条关联。"
                f"语义相似边（summary_similarity）：分块之间 {sim_chunk_chunk} 条、"
                f"文档之间 {sim_doc_doc} 条 —— 两者多跳抽象题都能用"
                f"（文档级的边会被自动展开成该文档的全部分块再出题）；"
                f"实体重叠边（entities_overlap）{flags['overlap_edges']} 条"
                f"（多跳具体题用，其中 {overlap_chunk_chunk} 条在分块之间，"
                f"{overlap_cross_doc} 条跨文档"
                f"{f'，占 {overlap_cross_doc / overlap_chunk_chunk:.0%}' if overlap_chunk_chunk else ''}）。"
            ),
            "explain_cross_doc": (
                f"跨文档出题能力主要来自实体重叠边：{overlap_cross_doc} 条跨文档"
                f"（共 {overlap_chunk_chunk} 条）。文档之间的语义相似边只有 {sim_doc_doc} 条，"
                f"但它不限制跨文档题目的数量 —— 每条边一次展开就能覆盖两个文档的全部分块。"
                if overlap_chunk_chunk or sim_doc_doc else
                "没有跨文档关联，无法出跨文档题目。"
            ),
        },
        "chunk_property_coverage": {
            k: {
                "label": _PROP_LABELS.get(k, k),
                "value": f"{v}/{chunk_count}",
                "explain": _prop_explain(k, v, chunk_count),
            }
            for k, v in chunk_props_cov.items()
        },
        "synthesizer_availability": synth_avail,
        "recommended_relation_property": recommended_rp,
        "recommended_label": recommended_label,
        "recommended_reason": rp_reason,
        # 系统默认值 = GenIn.abstract_relation_property 的默认；两者一致时用户什么都不用做
        "default_relation_property": "summary_similarity",
        "relation_property_needs_change": recommended_rp not in ("summary_similarity", "（无可用）"),
        "needs_rebuild": needs_rebuild,
        "rebuild_reason": rebuild_reason,
    }


def _prop_explain(prop: str, count: int, total: int) -> str:
    """单属性的解释文本（面向非技术用户）。"""
    explains = {
        "page_content": "分块里的原始文本。每个分块都该有，缺失说明图谱结构损坏。",
        "themes": "这段内容讲什么的概括。多跳抽象题靠它出题，缺失就出不了概括题。",
        "entities": "这段内容里出现的人名、产品名等关键名词。单跳具体题直接拿它出题；多跳具体题也靠它把两段内容连起来。",
        "summary": "这段内容的摘要。有了它才能判断两段内容在意思上有多接近。",
        "summary_embedding": "摘要转成的向量，用来计算分块之间的语义相似度。",
        "embedding": "另一种命名的向量字段，作用同上。",
        "keyphrases": "关键词。当前流水线不生成这一项，显示 0 是正常的。",
        "document_metadata": "文档本身的信息（如文件名），通常只挂在文档节点上。",
    }
    base = explains.get(prop, "未知属性")
    pct = f"{count}/{total}（{round(count / total * 100) if total else 0}%）"
    return f"{pct} — {base}"
