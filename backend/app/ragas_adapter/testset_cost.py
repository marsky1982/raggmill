"""Step 2 成本预估：按 KG 实际分块长度与实测标定系数给出下限 / 预期 / 最坏三档。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

from ..storage import kg_get
from .testset_capabilities import _enhancement_needs, _kg_flags
from .testset_common import (
    DIST_KEYS,
    _has_content,
    _node_kind,
    calculate_split_values,
    default_distribution,
    normalize_distribution,
)


# ---------- 生成前的 token 预估 ----------

# 实测标定表：每种 prompt 单次调用的 (输入 token, 输出 token) 均值。
# 来源：ts-d0e8c282（11 条样本 / 28 次调用 / 73,233 token，某推理模型，
#      KG kg-d8f42f1c 分块中位 1,030 字符）。
#
# ⚠ 关键教训：**输出 token 远大于输入**（实测出 47,248 / 入 25,985，约 1.8 倍）。
# 早先按"输出≈输入的 15%"估算是严重低估——某推理模型 这类带思维链的模型输出很长，
# 「主题×人格匹配」甚至是出 17,139 / 入 5,883（2.9 倍）。改参数前先看实测值。
_CALIB: dict[str, tuple[int, int]] = {
    "persona":         (435, 1935),   # 人格生成，3 次固定
    "concept_combo":   (614, 716),    # 概念组合，每个多跳 cluster 1 次
    "themes_personas": (535, 1558),   # 主题×人格匹配，每个 node/cluster 1 次
    "single_qa":       (1216, 1877),  # 单跳出题，每条单跳样本 1 次
    "multi_qa":        (1812, 2129),  # 多跳出题，每条多跳样本 1 次
}


# 标定时的分块中位长度（字符）。出题类 prompt 内联分块原文，
# 实际分块更长/更短时按比例缩放输入部分。
_CALIB_CHUNK_CHARS = 1030


# 生成前增强（投影 KG）：每个缺项 chunk 各 1 次 LLM。系数 = (固定输入, 每字符输入, 输出)。
# 2026-11-06 用 某推理模型 重标定：10 个分块（91–1839 字符）逐个单独增强，按调用拟合。
#   · 输入：摘要 ≈ 220 + 0.60×字符、主题 ≈ 236 + 0.60×字符（与旧系数接近）；
#   · 输出：摘要平均 721（222–1278）、主题平均 695（321–1749），与分块长短关系弱，取平均值。
#   旧系数（250/0.5/350、200/0.5/150）粗标定自 2026-11-04 qwen 联调，输出只有现在的 1/2～1/5，
#   rc3 实测预估 3,493、实际 5,411（偏低约 55%）；新系数回算同一次为 LLM 4,712 / 实际 4,705。
#   不同模型输出长度差异大，换模型后按 设计文档 的方法重标。
_CALIB_ENHANCE: dict[str, tuple[int, float, int]] = {
    "summary": (220, 0.6, 720),
    "themes": (240, 0.6, 700),
}


_CALIB_ENHANCE_MODEL = "某推理模型"


# 每个节点的摘要向量：按摘要长度计 embedding token（量级小，但大库不可忽略；2026-11-06 实测平均 192）
_CALIB_EMBED_TOKENS_PER_NODE = 200


def _estimate_enhancement(kg, kg_entry: dict, active_keys: set[str], relation_property: str) -> dict:
    """生成前增强的调用次数与 token（G1）：与 _enhancement_needs / enhance_existing_kg 同口径。

    enhance_existing_kg 一旦触发，会给**所有**缺摘要的 chunk 做摘要（ragas persona 过滤要求节点有
    summary_embedding，目前按全量补）；themes 只在出抽象题时补。已增强的节点不重算。
    向量只计「补完后会有摘要、但还没有向量」的节点：缺向量的分块 + 名下有分块且缺向量的文档
    （没有分块的文档拼不出摘要，增强也不会给它做向量，不能每次都算进去）。
    """
    flags = _kg_flags(kg, relation_property)
    needs = _enhancement_needs(kg_entry, flags, active_keys, relation_property)
    if not needs:
        return {"will_enhance": False, "needs": [], "chunk_summaries": 0, "theme_chunks": 0,
                "embeddings": 0, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                "embedding_tokens": 0, "total_tokens": 0}
    chunks = [n for n in kg.nodes if _node_kind(n) == "CHUNK"]
    missing_summary = [n for n in chunks if not _has_content(n.properties.get("summary"))]
    missing_themes = ([n for n in chunks if not _has_content(n.properties.get("themes"))]
                      if "multi_hop_abstract" in active_keys else [])

    def llm_cost(kind: str, nodes: list) -> tuple[float, float]:
        base, per_char, out = _CALIB_ENHANCE[kind]
        chars = sum(len(n.properties.get("page_content") or "") for n in nodes)
        return base * len(nodes) + per_char * chars, out * len(nodes)

    s_in, s_out = llm_cost("summary", missing_summary)
    t_in, t_out = llm_cost("themes", missing_themes)
    parents = {str(r.source.id) for r in kg.relationships if r.type == "child"}
    embeddings = (sum(1 for n in chunks if n.properties.get("summary_embedding") is None)
                  + sum(1 for n in kg.nodes if _node_kind(n) == "DOCUMENT" and str(n.id) in parents
                        and n.properties.get("summary_embedding") is None))
    emb_tokens = embeddings * _CALIB_EMBED_TOKENS_PER_NODE
    llm_in, llm_out = s_in + t_in, s_out + t_out
    return {
        "will_enhance": True, "needs": needs,
        "chunk_summaries": len(missing_summary), "theme_chunks": len(missing_themes),
        "embeddings": embeddings,
        "llm_calls": len(missing_summary) + len(missing_themes),
        "input_tokens": int(llm_in), "output_tokens": int(llm_out),
        "embedding_tokens": int(emb_tokens),
        "total_tokens": int(llm_in + llm_out + emb_tokens),
    }


def _abstract_context_expansion(kg, relation_property: str) -> dict:
    """多跳抽象题的上下文放大倍数（G2）。

    ragas `multi_hop/abstract.py` 对簇内每个节点：有 child 就整篇展开成全部子 chunk，否则用节点本身。
    文档级相似边组成的簇因此把整篇文档原文塞进出题 prompt。
    放大倍数 = 文档级边占比 × 平均每文档 chunk 数 + chunk 级边占比 × 1（标定基准是 chunk 级簇）。
    """
    child_count: dict[str, int] = {}
    for rel in kg.relationships:
        if rel.type == "child":
            key = str(rel.source.id)
            child_count[key] = child_count.get(key, 0) + 1
    doc_edges = chunk_edges = 0
    for rel in kg.relationships:
        if not rel.get_property(relation_property):
            continue
        if str(rel.source.id) in child_count or str(rel.target.id) in child_count:
            doc_edges += 1
        else:
            chunk_edges += 1
    total = doc_edges + chunk_edges
    avg_chunks = (sum(child_count.values()) / len(child_count)) if child_count else 1.0
    frac_doc = doc_edges / total if total else 0.0
    factor = max(1.0, frac_doc * avg_chunks + (1 - frac_doc))
    return {"factor": round(factor, 2), "doc_level_edges": doc_edges, "chunk_level_edges": chunk_edges,
            "avg_chunks_per_document": round(avg_chunks, 1)}


def estimate_generation_cost(
    kg_id: str,
    testset_size: int,
    distribution: dict | None,
    use_default: bool,
    max_retries: int = 3,
    num_personas: int = 3,
    abstract_relation_property: str = "summary_similarity",
) -> dict:
    """按 KG 实际分块长度估算本次生成的 token 量级（下限 / 预期 / 最坏）。

    估算模型（对齐 ragas 源码的调用次数）：
      · 生成前增强（仅投影 KG 且缺素材时）：每个缺摘要 chunk 1 次、出抽象题时每个缺 themes chunk 1 次，
        外加摘要向量（G1：此前完全不计，大库会严重低估）
      · 场景阶段 SingleHop：每个 node 一次「主题×人格匹配」，最多 n_single 次
      · 场景阶段 MultiHop ：每个 cluster 两次（概念组合 + 主题×人格匹配）
      · 样本阶段          ：每条样本一次「出题」，prompt 内联 scenario 全部分块原文；
        抽象题遇到文档级簇时按整篇展开放大（G2）
    """
    from ragas.testset.graph import KnowledgeGraph

    entry = kg_get(kg_id)
    if not entry:
        raise ValueError(f"知识图谱 {kg_id} 不存在")
    kg = KnowledgeGraph.load(entry["kg_path"])

    chunks = [n for n in kg.nodes if str(n.type).lower().endswith("chunk")]
    if not chunks:
        raise ValueError("该图谱没有分块节点，无法估算")
    lens = sorted(len(n.properties.get("page_content") or "") for n in chunks)
    median_chunk = lens[len(lens) // 2]

    dist = default_distribution() if use_default else (distribution or default_distribution())
    probs, _ = normalize_distribution(dist)  # 与生成时同口径：合计≠1 的比例先归一化
    splits = calculate_split_values(probs, testset_size)
    n_single, n_abs, n_spec = splits

    multi_clusters = n_abs + n_spec
    single_calls = min(n_single, len(chunks))
    active_keys = {k for k, n in zip(DIST_KEYS, splits) if n > 0}
    enhancement = _estimate_enhancement(kg, entry, active_keys, abstract_relation_property)
    expansion = (_abstract_context_expansion(kg, abstract_relation_property) if n_abs else
                 {"factor": 1.0, "doc_level_edges": 0, "chunk_level_edges": 0, "avg_chunks_per_document": 0})
    abs_factor = expansion["factor"]

    # 出题类 prompt 内联分块原文，输入随分块长度线性缩放；输出与分块长度基本无关
    scale = median_chunk / _CALIB_CHUNK_CHARS if _CALIB_CHUNK_CHARS else 1.0

    def cost(kind: str, calls: int, scale_input: float = 1.0) -> tuple[float, float]:
        ti, to = _CALIB[kind]
        return calls * ti * scale_input, calls * to

    # ---- 调用次数（对齐 ragas 源码）----
    #   人格生成       : num_personas 次（默认 3）
    #   主题×人格匹配  : 每个单跳 node 1 次 + 每个多跳 cluster 1 次
    #   概念组合       : 每个多跳 cluster 1 次
    #   出题           : 每条样本 1 次
    parts = {
        "人格生成": cost("persona", num_personas),
        # 标定值取自 num_personas=3；人格越多，这个 prompt 的输入输出都按比例增长
        "场景·主题匹配": tuple(  # type: ignore[dict-item]
            x * (num_personas / 3) for x in
            cost("themes_personas", single_calls + multi_clusters)),
        # 抽象题的概念组合 prompt 带上簇内（展开后）每个节点的 themes，同样随展开放大
        "场景·概念组合": tuple(a + b for a, b in zip(  # type: ignore[dict-item]
            cost("concept_combo", n_spec), cost("concept_combo", n_abs, abs_factor))),
        "样本·单跳出题": cost("single_qa", n_single, scale),
        "样本·多跳具体出题": cost("multi_qa", n_spec, scale),
        "样本·多跳抽象出题": cost("multi_qa", n_abs, scale * abs_factor),
    }
    if enhancement["will_enhance"]:
        # embedding token 并入输入侧，沿用「输入 / 输出」两栏展示
        parts["生成前增强"] = (enhancement["input_tokens"] + enhancement["embedding_tokens"],
                           enhancement["output_tokens"])

    floor_in = sum(v[0] for v in parts.values())
    floor_out = sum(v[1] for v in parts.values())
    floor = floor_in + floor_out

    # 预期：留 1.15 倍余量（偶发格式修复 + 输出长度波动）
    expected = floor * 1.15
    # 最坏：所有调用都打满 max_retries（每次重试重发整份 prompt，输出也重新生成）
    worst = floor * max_retries

    return {
        "kg_id": kg_id,
        "kg_name": entry.get("name", ""),
        "testset_size": testset_size,
        "splits": {"SingleHopSpecificQuery": n_single,
                   "MultiHopAbstractQuery": n_abs,
                   "MultiHopSpecificQuery": n_spec},
        "chunk_stats": {"count": len(chunks), "median_chars": median_chunk,
                        "min_chars": lens[0], "max_chars": lens[-1]},
        "estimate_tokens": {
            "floor": int(floor),
            "expected": int(expected),
            "worst_case": int(worst),
        },
        # 分项：每项是 该阶段的 输入+输出 合计
        "breakdown_tokens": {k: int(v[0] + v[1]) for k, v in parts.items()},
        "io_split": {"input": int(floor_in), "output": int(floor_out)},
        "llm_calls_estimate": int(num_personas + single_calls + multi_clusters * 2 + n_single + multi_clusters
                                  + enhancement["llm_calls"]),
        "enhancement": enhancement,
        "abstract_context": expansion,
        "assumptions": [
            *([f"投影 KG 缺素材，生成前会自动增强：{enhancement['chunk_summaries']} 个 chunk 摘要、"
               f"{enhancement['theme_chunks']} 个 chunk themes、{enhancement['embeddings']} 个摘要向量，"
               f"约 {enhancement['total_tokens']:,} token（成功后写回图谱，下次不再重复；系数按 "
               f"{_CALIB_ENHANCE_MODEL} 标定，模型输出越长实际越高）"]
              if enhancement["will_enhance"] else []),
            *([f"多跳抽象题有 {expansion['doc_level_edges']} 条文档级相似边：ragas 会把文档整篇展开"
               f"（平均 {expansion['avg_chunks_per_document']} 个 chunk/篇），抽象题上下文按 {abs_factor:.1f}× 放大"]
              if n_abs and abs_factor > 1.0 else []),
            f"分块中位长度 {median_chunk} 字符（标定基准 {_CALIB_CHUNK_CHARS}），"
            f"出题类 prompt 的输入按 {scale:.2f}× 缩放",
            "系数为实测标定（某推理模型），"
            f"**输出 token 约为输入的 {floor_out / floor_in:.1f} 倍** —— 带思维链的模型输出很长，"
            "这是本工具最大的单项开销",
            f"最坏值 = 下限 × max_retries({max_retries})，"
            f"对应「每次调用都失败并重试到上限」；ragas 默认 max_retries=10",
            "出题阶段不含 embedding 调用（Step 2 只在选节点时用到，量级可忽略）",
            "条数按归一化比例精确分配，合计恰为 testset_size；单条失败会被跳过，实际条数可能更少",
        ],
    }
