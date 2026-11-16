"""Step 2 能力判定与预检：按 KG 实际素材判定三类题型能否出题、缺什么、能否自动补齐。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

from .testset_common import DIST_KEYS, _has_content, _node_kind, default_distribution, normalize_distribution


CAPABILITY_LABELS = {
    "single_hop_specific": "单跳具体题（SingleHopSpecificQuery）",
    "multi_hop_abstract": "多跳抽象题（MultiHopAbstractQuery）",
    "multi_hop_specific": "多跳具体题（MultiHopSpecificQuery）",
}


# 只有 RAGFlow 投影 KG 走「生成前按缺口自动补齐」：投影只带原文/实体，
# 没有 Step 1 生成的摘要、摘要向量与 themes；普通 Step 1 KG 缺这些应回 Step 1 重建。
_ENHANCEMENT_MODES = {"ragflow-projection"}


def _similarity_gap_message(estimate: dict, overlap_edges: int) -> str:
    """摘要相似度确实够不到阈值时，给用户一条可执行的建议（而不是泛化报错）。"""
    peak = estimate.get("max_pairwise_similarity")
    peak_text = f"{peak:.3f}" if isinstance(peak, (int, float)) else "未知"
    advice = "建议把「多跳抽象题的连线依据」改为 entities_overlap_score"
    if overlap_edges > 0:
        advice += f"（图谱里已有 {overlap_edges} 条 entities_overlap 边）"
    else:
        advice += "（当前没有 entities_overlap 边，需先补齐实体重叠）"
    return (f"多跳抽象题的连线依据 summary_similarity 一条边都没有，且现有 "
            f"{estimate.get('embedded_nodes', 0)} 个分块摘要向量两两余弦最高只有 {peak_text}，"
            f"低于 {estimate.get('threshold', 0.7)} 阈值——自动补齐也建不出任何相似边。"
            f"{advice}，或把多跳抽象题比例设为 0，或补充同主题语料后重建知识图谱。")


def _kg_flags(kg, relation_property: str) -> dict:
    """统计各合成器需要的素材（预检 / 能力判定 / 质检报告共用，避免多套口径漂移）。

    每一项都对应 ragas 源码里一个会在人格生成（~6k token）之后才暴露的缺口：
    - 单跳具体题只读节点属性 entities（single_hop/specific.py:44），与 themes 无关；
    - 多跳抽象题读分块 themes 且按关系属性筛边（multi_hop/abstract.py:37,50）；
    - 多跳具体题按关系类型 entities_overlap 取两两节点（multi_hop/specific.py:35）。
    """
    nodes = kg.nodes
    # chunk -> 所属 document，用于判断一条边是否跨文档
    chunk_owner = {r.target.id: r.source.id for r in kg.relationships if r.type == "child"}
    sim_edges = sim_chunk_chunk = sim_doc_doc = 0
    overlap_edges = overlap_chunk_chunk = overlap_cross_doc = 0
    for r in kg.relationships:
        if r.type == "summary_similarity":
            sim_edges += 1
            if _node_kind(r.source) == "CHUNK" and _node_kind(r.target) == "CHUNK":
                sim_chunk_chunk += 1
            else:
                sim_doc_doc += 1
        elif r.type == "entities_overlap":
            overlap_edges += 1
            if _node_kind(r.source) == "CHUNK" and _node_kind(r.target) == "CHUNK":
                overlap_chunk_chunk += 1
                a, b = chunk_owner.get(r.source.id), chunk_owner.get(r.target.id)
                if a and b and a != b:
                    overlap_cross_doc += 1
    return {
        "document_count": sum(1 for n in nodes if _node_kind(n) == "DOCUMENT"),
        "chunk_count": sum(1 for n in nodes if _node_kind(n) == "CHUNK"),
        # 人格：persona.default_filter 要求 DOCUMENT/CHUNK 节点带 summary_embedding
        "summary_embedding_nodes": sum(
            1 for n in nodes
            if _node_kind(n) in ("DOCUMENT", "CHUNK") and n.properties.get("summary_embedding") is not None),
        # 单跳具体题：single_hop/specific.py 只取带 entities 属性的节点（chunk 优先），空列表不算素材
        "entity_nodes": sum(
            1 for n in nodes
            if _node_kind(n) in ("DOCUMENT", "CHUNK") and _has_content(n.properties.get("entities"))),
        "chunk_entity_nodes": sum(
            1 for n in nodes
            if _node_kind(n) == "CHUNK" and _has_content(n.properties.get("entities"))),
        # 带摘要向量的分块：按分块判断是否还有缺口（投影里新增 / 正文变化的分块没有继承到增强成果）
        "chunk_embedding_nodes": sum(
            1 for n in nodes
            if _node_kind(n) == "CHUNK" and n.properties.get("summary_embedding") is not None),
        "chunk_summary_nodes": sum(
            1 for n in nodes
            if _node_kind(n) == "CHUNK" and _has_content(n.properties.get("summary"))),
        # 多跳抽象题场景生成读 chunk.themes（缺了不报错但会 0 场景，见 T10 记录⑦）
        "theme_chunks": sum(
            1 for n in nodes
            if _node_kind(n) == "CHUNK" and _has_content(n.properties.get("themes"))),
        # 多跳抽象题按「关系上有没有这个属性」筛边（属性名，不是关系类型名）
        "relation_property_edges": sum(1 for r in kg.relationships if r.get_property(relation_property)),
        # 多跳具体题按关系类型 entities_overlap 取两两节点
        "overlap_edges": overlap_edges,
        # 语义相似边：文档级与分块级都算（ragas 原生只建文档级，分块级是本项目 A′ 增强），
        # 文档级的边会被展开成该文档的全部分块，多跳抽象题同样能用
        "similarity_edges": sim_edges,
        "similarity_edges_chunk_chunk": sim_chunk_chunk,
        "similarity_edges_doc_doc": sim_doc_doc,
        "overlap_edges_chunk_chunk": overlap_chunk_chunk,
        "overlap_edges_cross_doc": overlap_cross_doc,
        "available_relation_properties": sorted({k for r in kg.relationships for k in (r.properties or {})}),
    }


def _synth_material_ok(flags: dict, key: str) -> bool:
    """按 _kg_flags 的同一套口径判断某合成器的真实素材是否齐全（不含生成前自动增强）。"""
    if key == "single_hop_specific":
        # ragas 单跳具体题只读 entities，themes 与它无关（single_hop/specific.py:44）
        return flags["entity_nodes"] > 0
    if key == "multi_hop_abstract":
        # 可用的连线依据：文档级/分块级语义相似边，或实体重叠边
        return flags["similarity_edges"] > 0 or flags["overlap_edges"] > 0
    if key == "multi_hop_specific":
        return flags["overlap_edges"] > 0
    return False


def _recommend_relation_property(flags: dict) -> tuple[str, str, str]:
    """推荐多跳抽象题的连线依据，返回 (recommended_rp, label, reason)。"""
    if flags["similarity_edges"] > 0:
        return (
            "summary_similarity",
            "语义相似边（summary_similarity）",
            "图谱里已有语义相似边（文档级或分块级），"
            "多跳抽象题可以直接按「意思有多接近」把多个分块串起来出题，效果最好。"
            "这也是系统默认值，无需改动。",
        )
    if flags["overlap_edges"] > 0:
        return (
            "entities_overlap_score",
            "实体重叠边（entities_overlap_score）",
            "⚠ 需要改动：图谱里没有语义相似边，但有实体重叠边，只能退而求其次："
            "用「两段内容提到相同的人名或产品名」来近似判断意思接近。"
            "系统默认值是语义相似边，这份图谱下要手动改成实体重叠边，否则抽象题可能生成不出来。"
            "题目质量也会打折扣，想要更好的抽象题建议重建图谱。",
        )
    return (
        "（无可用）",
        "（无可用）",
        "图谱里既没有语义相似边也没有实体重叠边，多跳抽象题生成不出来，建议重建知识图谱。",
    )


def assess_kg_capabilities(kg_entry: dict, relation_property: str = "summary_similarity") -> dict:
    """按 KG 实际素材判断三类题型是否可用。

    返回结构同时供前端展示「缺什么」和路由层放行判定；缺失说明必须可读，
    不允许静默禁用。
    """
    from ragas.testset.graph import KnowledgeGraph

    try:
        kg = KnowledgeGraph.load(kg_entry["kg_path"])
    except (KeyError, OSError) as exc:
        raise ValueError(f"知识图谱文件缺失或不可读：{kg_entry.get('kg_path') or '（无路径）'}") from exc
    flags = _kg_flags(kg, relation_property)

    # 投影 KG 可在生成前按缺口自动补齐：这些缺失项不阻塞生成，但要如实告知
    auto_enhance = kg_entry.get("mode") in _ENHANCEMENT_MODES
    persona_missing = (
        [("摘要向量（summary_embedding）—— ragas 生成提问人格时必须有，三类题型都依赖", True)]
        if flags["summary_embedding_nodes"] == 0 else []
    )

    # 摘要相似边：能不能补出来是可以按现有向量预演出来的，不能承诺补不出的东西
    similarity_estimate = None
    similarity_probe = False
    similarity_hint = ""
    if (auto_enhance and relation_property == "summary_similarity"
            and flags["relation_property_edges"] == 0):
        from .kg import estimate_similarity_edges
        try:
            similarity_estimate = estimate_similarity_edges(kg)
        except Exception:  # noqa: BLE001 —— 纯预演失败不应阻断能力判定
            similarity_estimate = None
        if similarity_estimate and similarity_estimate["embedded_nodes"] >= 2:
            if similarity_estimate["reachable_edges"] == 0:
                similarity_hint = _similarity_gap_message(
                    similarity_estimate, flags["overlap_edges"])
            else:
                peak = similarity_estimate["max_pairwise_similarity"]
                similarity_hint = (f"现有摘要向量最高余弦 {peak:.3f}，自动补齐后预计可建出 "
                                   f"{similarity_estimate['reachable_edges']} 条 summary_similarity 边")
        else:
            similarity_probe = True
            similarity_hint = ("当前还没有可用的摘要向量，增强前无法预判能否达到 0.7；"
                               "生成时会先补齐向量并复核，若仍不足会在出题前停下并给出建议（不消耗出题 token）")

    abstract_missing: list[tuple[str, bool]] = []
    if flags["relation_property_edges"] == 0:
        available = "、".join(flags["available_relation_properties"]) or "（无）"
        hint = ""
        if relation_property == "entities_overlap":
            hint = "（注意：这里要填属性名 entities_overlap_score，而不是关系类型名 entities_overlap）"
        if (similarity_estimate and similarity_estimate["embedded_nodes"] >= 2
                and similarity_estimate["reachable_edges"] == 0):
            # 已证明补不出相似边：直接把「不可自动补齐 + 可操作建议」作为缺失原因
            abstract_missing.append((similarity_hint, False))
        else:
            text = (f"连线依据「{relation_property}」一条边都没有{hint}；"
                    f"图谱中实际存在的关系属性：{available}")
            if similarity_hint:
                text += f"（{similarity_hint}）"
            abstract_missing.append((text, relation_property == "summary_similarity"))
    if flags["theme_chunks"] == 0:
        abstract_missing.append(("分块主题标签（themes）—— 抽象题场景生成会读到 0 条", True))

    missing_by_key = {
        "single_hop_specific": (
            [] if _synth_material_ok(flags, "single_hop_specific") else [("实体标签（entities）", False)]),
        "multi_hop_abstract": abstract_missing,
        "multi_hop_specific": (
            [] if _synth_material_ok(flags, "multi_hop_specific") else [("实体重叠边（entities_overlap）", False)]),
    }
    synthesizers: dict[str, dict] = {}
    for key, own_missing in missing_by_key.items():
        items = persona_missing + own_missing
        missing = [text for text, _ in items]
        fillable = [text for text, can_fill in items if can_fill]
        auto_fillable = auto_enhance and bool(items) and all(can_fill for _, can_fill in items)
        # 「还没有向量、无法预判」这一种情况允许先跑增强再复核：不承诺自动补齐，
        # 但放行生成；真正补不出时由生成路径在出题前 0 token 停下并给出建议
        requires_probe = (auto_enhance and similarity_probe and key == "multi_hop_abstract"
                          and all(can_fill for _, can_fill in items))
        if requires_probe:
            auto_fillable = False  # 无法预判时不承诺「自动补齐」，只放行一次增强后复核
        effective = not missing or auto_fillable or requires_probe
        reason = "缺少 " + "；".join(missing) if missing else "素材齐全，可以生成"
        if auto_fillable:
            reason = ("缺少 " + "；".join(missing)
                      + "（生成前会自动补齐：每个缺项 chunk 各调用一次 LLM，大库开销可观，请先点「预估 token 消耗」）")
        elif requires_probe:
            reason = ("缺少 " + "；".join(missing)
                      + "（生成时会先补齐摘要向量并复核相似边；若仍达不到阈值会在出题前停下并给出建议，"
                        "不消耗出题 token）")
        synthesizers[key] = {
            "label": CAPABILITY_LABELS[key],
            "available": not missing,
            # 真实素材是否齐全；effective 表示算上「生成前自动补齐」后能否出题
            "effective_available": effective,
            "auto_enhanceable": auto_fillable,
            "requires_probe": requires_probe,
            "missing": missing,
            "fillable_missing": fillable,
            "reason": reason,
        }

    return {
        "kg_id": kg_entry.get("id", ""),
        "mode": kg_entry.get("mode", ""),
        "source_type": kg_entry.get("source_type", ""),
        "relation_property": relation_property,
        # 旧索引可能带 generation_ready：只作展示兼容，不再作为唯一开关
        "generation_ready": bool(kg_entry.get("generation_ready")),
        "synthesizers": synthesizers,
        # usable = 算上生成前自动增强后能否出题；material_complete = 现有素材是否已齐全
        "usable": all(item["effective_available"] for item in synthesizers.values()),
        "material_complete": all(item["available"] for item in synthesizers.values()),
        # 摘要相似边的可达性预演（仅当抽象题连线依据为 summary_similarity 且当前 0 边时计算）
        "similarity_estimate": similarity_estimate,
        "enhancement": {
            "supported": auto_enhance,
            "will_enhance": any(item["auto_enhanceable"] for item in synthesizers.values()),
            "missing": sorted({text for item in synthesizers.values()
                               for text in item["fillable_missing"]}),
        },
        "flags": flags,
    }


def check_generation_capabilities(
    kg_entry: dict,
    distribution: dict | None = None,
    use_default: bool = True,
    relation_property: str = "summary_similarity",
) -> dict:
    """按本次请求的题型组合检查能力；缺素材时抛可读 ValueError（0 token 预检失败）。

    单跳 + 多跳具体只需基本素材即可放行；多跳抽象额外要求连线依据边与 themes。
    """
    report = assess_kg_capabilities(kg_entry, relation_property)
    raw = default_distribution() if use_default else (distribution or default_distribution())
    probs, _ = normalize_distribution(raw)
    needed = [k for k, p in zip(DIST_KEYS, probs) if p > 0]
    blocked = [
        (k, report["synthesizers"][k]) for k in needed
        if not report["synthesizers"][k]["effective_available"]
    ]
    if blocked:
        details = "\n".join(f"· {item['label']}：{item['reason']}" for _, item in blocked)
        raise ValueError("当前知识图谱不能生成所选题型（未消耗任何 token）：\n" + details)
    return report


def _precheck_kg(kg, active_keys: set[str], relation_property: str) -> None:
    """提交前的素材检查：缺素材就在花任何 token 之前给出可读错误。"""
    flags = _kg_flags(kg, relation_property)

    if flags["summary_embedding_nodes"] == 0:
        raise ValueError("这份知识图谱没有带摘要向量（summary_embedding）的节点，ragas 无法生成提问人格。"
                         "请重建知识图谱（Step 1 会补上摘要与摘要向量）。")

    if "single_hop_specific" in active_keys and flags["entity_nodes"] == 0:
        raise ValueError("单跳具体题需要节点带实体标签（entities），这份图谱里没有。"
                         "请把单跳题比例设为 0，或重建知识图谱。")

    if "multi_hop_abstract" in active_keys:
        if flags["relation_property_edges"] == 0:
            available = "、".join(flags["available_relation_properties"]) or "（无）"
            hint = ""
            if relation_property == "entities_overlap":
                hint = "（注意：这里要填属性名 entities_overlap_score，而不是关系类型名 entities_overlap）"
            from .kg import estimate_similarity_edges
            estimate = estimate_similarity_edges(kg)
            if estimate["embedded_nodes"] >= 2 and estimate["reachable_edges"] == 0:
                # 已能证明「补齐也建不出边」：直接给可操作建议，而不是泛化的「一条边都没有」
                raise ValueError(_similarity_gap_message(estimate, flags["overlap_edges"]))
            raise ValueError(
                f"多跳抽象题的连线依据「{relation_property}」在这份图谱里一条边都没有{hint}。"
                f"图谱中实际存在的关系属性：{available}。"
                f"请改用其中之一（通常是 summary_similarity 或 entities_overlap_score），"
                f"或把多跳抽象题比例设为 0。")
        if flags["theme_chunks"] == 0:
            raise ValueError("多跳抽象题需要分块主题标签（themes），这份图谱的 chunk 里一个都没有，"
                             "场景生成会读到 0 条（不报错但出不了题）。"
                             "请把多跳抽象题比例设为 0，或重建知识图谱。")

    if "multi_hop_specific" in active_keys and flags["overlap_edges"] == 0:
        raise ValueError("多跳具体题需要实体重叠边（entities_overlap），这份图谱里一条都没有。"
                         "请把多跳具体题比例设为 0，或重建知识图谱。")


def _enhancement_needs(kg_entry: dict, flags: dict, active_keys: set[str],
                       relation_property: str) -> list[str]:
    """本次生成需要自动补齐的素材；空列表表示不需要增强（不会多发一次调用）。"""
    if kg_entry.get("mode") not in _ENHANCEMENT_MODES:
        return []
    needs: list[str] = []
    # 按分块判断缺口，而不是「整张图一个都没有」才补：同步后新增 / 正文变化的分块没有继承到增强成果，
    # 旧口径下只要图里还有别的分块带着摘要，它们就永远补不上，人格与抽象题会越来越看不到新内容。
    # enhance_existing_kg 只处理缺项节点，已增强的分块不会重算。
    chunk_count = flags.get("chunk_count")  # 手工构造的精简 flags 没有分块计数时，只按整体缺失判断
    missing_summary = (chunk_count - flags.get("chunk_summary_nodes", chunk_count)) if chunk_count else 0
    missing_vector = (chunk_count - flags.get("chunk_embedding_nodes", chunk_count)) if chunk_count else 0
    if flags["summary_embedding_nodes"] == 0:
        needs.append("摘要与摘要向量")
    elif missing_summary > 0 or missing_vector > 0:
        needs.append(f"新增或变化分块的摘要与向量（{max(missing_summary, missing_vector)} 个分块）")
    if "multi_hop_abstract" in active_keys:
        missing_themes = (chunk_count - flags["theme_chunks"]) if chunk_count else 0
        if flags["theme_chunks"] == 0:
            needs.append("分块主题")
        elif missing_themes > 0:
            needs.append(f"新增或变化分块的主题（{missing_themes} 个分块）")
        # 只自动补 summary_similarity 边；entities_overlap_score 属于投影阶段产物，不现场造
        if flags["relation_property_edges"] == 0 and relation_property == "summary_similarity":
            needs.append("分块语义相似边")
    return needs


def _unfillable_gaps(flags: dict, active_keys: set[str], relation_property: str) -> list[str]:
    """自动增强补不上的硬缺口（实体 / 重叠边 / 非 summary_similarity 连线依据）。"""
    gaps: list[str] = []
    if "single_hop_specific" in active_keys and flags["entity_nodes"] == 0:
        gaps.append("实体标签（entities）")
    if "multi_hop_specific" in active_keys and flags["overlap_edges"] == 0:
        gaps.append("实体重叠边（entities_overlap）")
    if ("multi_hop_abstract" in active_keys and relation_property != "summary_similarity"
            and flags["relation_property_edges"] == 0):
        gaps.append(f"连线依据「{relation_property}」")
    return gaps
