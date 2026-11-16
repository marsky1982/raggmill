"""Step 2 局部补生成：语料同步后，只把受影响的样本重出一遍，其余原样保留。

口径（对应「按出处指纹做样本级局部补生成」工单与设计文档 P3）：
- 判定对照**数据源的当前投影**（`current.json` 的 `projection_kg_id`），不是测试集生成时那张旧图；
  投影节点 id 是按上游 chunk id 算的确定性 uuid5，重建不漂移，所以旧样本引用的节点 id 可直接对照。
- `stale`（引用的分块内容变了）/ `orphan`（引用的分块已不存在）→ 重出；`valid` → 原样保留；
  `legacy`（无内容哈希的历史样本）→ 原样保留并提示人工决定，**不自动重烧**；
  `unknown`（当前投影读不到）→ 拒绝补生成。
- 结果另存为一个**新**测试集（保留的样本 + 新样本），旧测试集一个字节不改；不删样本、
  不把旧测试集指向新图、不做跨测试集批量。新测试集的账单只含这次补生成，便于与预估对账。

本模块不 import ragas；生成复用 `testset_generation.generate_testset`。
"""
from __future__ import annotations

from .. import storage
from ..policies import GenerationPolicy
from ..tasks.manager import Task, manager
from .provenance import (
    STATUS_LEGACY,
    STATUS_ORPHAN,
    STATUS_STALE,
    STATUS_UNKNOWN,
    STATUS_VALID,
    evaluate_samples_provenance,
)
from .testset_common import DIST_KEYS, SYNTHESIZER_MAP

REGENERATE_STATUSES = (STATUS_STALE, STATUS_ORPHAN)
_SYNTH_SUFFIX = "_query_synthesizer"


_KEY_BY_DISPLAY = {display: key for key, display in SYNTHESIZER_MAP.items()}


def _dist_key(synthesizer_name: str) -> str | None:
    """样本的 synthesizer_name → 题型比例键。

    两种写法都要认：真实生成是 ragas 合成器名（single_hop_specific_query_synthesizer），
    演示模式是 SYNTHESIZER_MAP 的显示名（SingleHopSpecificQuery）。
    """
    name = str(synthesizer_name or "")
    if name in _KEY_BY_DISPLAY:
        return _KEY_BY_DISPLAY[name]
    key = name[: -len(_SYNTH_SUFFIX)] if name.endswith(_SYNTH_SUFFIX) else name
    return key if key in DIST_KEYS else None


def current_projection_kg_id(ts_data: dict) -> str | None:
    """测试集所属 RAGFlow 数据源的当前投影；非 RAGFlow 测试集或指针缺失时返回 None。"""
    dataset_id = ts_data.get("source_dataset_id")
    if not dataset_id:
        return None
    connection_id = ts_data.get("source_connection_id")
    try:
        # 历史连接（旧 config.json 合成的 conn-legacy-*）的快照仍在旧布局 data/ragflow/{dataset_id}/，
        # 不在 sources/{connection__dataset}/ 下；普通连接不回退旧布局，免得认领别的实例的同名知识库
        if connection_id and connection_id != storage.legacy_connection_id(dataset_id):
            _, pointer = storage.ragflow_source_snapshot(storage.ragflow_source_id(connection_id, dataset_id))
        else:
            _, pointer = storage.ragflow_snapshot_current(dataset_id)
    except Exception:  # noqa: BLE001 —— 路径非法 / 读取失败一律当作「找不到当前投影」
        return None
    kg_id = (pointer or {}).get("projection_kg_id")
    return kg_id if kg_id and storage.kg_get(kg_id) else None


def resolve_judge_target(ts_data: dict, projection_kg_id: str | None = None) -> tuple[str | None, str]:
    """判定对照哪张图：显式指定 > 数据源当前投影 > 测试集自身的图。返回 (kg_id, 来源说明)。"""
    if projection_kg_id:
        return projection_kg_id, "explicit"
    current = current_projection_kg_id(ts_data)
    if current:
        return current, "current_projection"
    return ts_data.get("kg_id"), "own_kg"


def plan_regeneration(ts_id: str, *, num_personas: int = 3, max_retries: int = 3) -> dict:
    """补生成计划（只读、0 token）：判定每条样本，统计要重出的条数与题型，并给 token 预估。

    预估必须用**本次补生成的** num_personas / max_retries：人格生成与主题匹配都随人格数线性增长，
    rc3 实测用默认 3 个人格估、实际 1 个人格跑，预估高出约 2.8 倍。
    """
    data = storage.ts_read(ts_id)
    if data is None:
        raise ValueError(f"测试集 {ts_id} 不存在")
    target, basis = resolve_judge_target(data)
    projection = storage.kg_read_json(target) if target else None
    judged = evaluate_samples_provenance(data, projection)
    by_index = {row["index"]: row for row in judged["samples"]}

    affected: list[dict] = []
    kept: list[dict] = []
    by_type: dict[str, int] = {key: 0 for key in DIST_KEYS}
    unsupported: list[int] = []
    for sample in data.get("samples") or []:
        row = by_index.get(sample.get("index")) or {}
        status = row.get("status")
        if status in REGENERATE_STATUSES:
            key = _dist_key(sample.get("synthesizer_name"))
            if key is None:
                unsupported.append(sample.get("index"))
                kept.append(sample)
                continue
            by_type[key] += 1
            affected.append({"index": sample.get("index"), "status": status, "type": key,
                             "reason": row.get("reason"),
                             "changed_chunk_ids": row.get("changed_chunk_ids"),
                             "missing_chunk_ids": row.get("missing_chunk_ids")})
        else:
            kept.append(sample)

    counts = judged["counts"]
    blocked = None
    if counts.get(STATUS_UNKNOWN):
        blocked = "读不到对照用的图谱，无法判定哪些样本受影响；请先在知识图谱页重新同步该数据源"
    elif not affected:
        blocked = "没有内容变化或来源被删除的样本，不需要补生成"

    # 下游（成本预估 / generate_testset 的 query 分布）要的是**占比**，不是条数：
    # 传条数会被 normalize_distribution 当成「合计 2 的比例」，既触发「题型比例合计为 2（应为 1）」
    # 的告警，也把比例口径搞混。这里一次性换算成占比，条数只留在计划里给界面看。
    by_type_counts = {k: v for k, v in by_type.items() if v}
    by_type_ratio = {k: v / len(affected) for k, v in by_type_counts.items()} if affected else {}

    estimate = None
    if affected and not blocked and target:
        from .testset_cost import estimate_generation_cost
        try:
            estimate = estimate_generation_cost(
                target, len(affected), by_type_ratio, False,
                max_retries=max_retries, num_personas=num_personas,
                abstract_relation_property=data.get("relation_property") or "summary_similarity")
        except Exception as exc:  # noqa: BLE001 —— 预估失败不阻断计划，只是不给数字
            estimate = {"error": str(exc)}

    return {
        "testset_id": ts_id,
        "testset_name": data.get("name"),
        "judged_against_kg_id": target,
        "judged_against": basis,
        "counts": counts,
        "total": judged["total"],
        "regenerate": len(affected),
        "regenerate_by_type": by_type_counts,
        "keep": len(kept),
        "keep_legacy": counts.get(STATUS_LEGACY, 0),
        "keep_valid": counts.get(STATUS_VALID, 0),
        "unsupported_indices": unsupported,
        # 对照当前投影复核仍一致的样本序号：补生成时撤掉它们身上过期的 stale 标记
        "valid_indices": sorted(index for index, row in by_index.items() if row.get("status") == STATUS_VALID),
        "affected": affected,
        "blocked": blocked,
        "estimate": estimate,
        "legacy_note": ("有 {n} 条历史样本没有内容指纹，无法判断是否受影响，会原样保留；"
                        "如需更新请人工复核或整批重新生成").format(n=counts.get(STATUS_LEGACY, 0))
        if counts.get(STATUS_LEGACY) else None,
    }


async def regenerate_testset(task: Task, ts_id: str, llm_preset: dict, emb_preset: dict, *,
                             policy: GenerationPolicy | None = None,
                             name: str | None = None) -> dict:
    """局部补生成：只重出 stale / orphan 的样本，结果另存为新测试集（旧测试集不改）。"""
    from .testset_generation import generate_testset

    policy = policy or GenerationPolicy()
    plan = plan_regeneration(ts_id, num_personas=policy.num_personas, max_retries=policy.max_retries)
    if plan["blocked"]:
        raise ValueError(plan["blocked"])
    data = storage.ts_read(ts_id) or {}
    # 不传语言（None）时沿用原测试集的出题语言
    if policy.language is None:
        policy = policy.model_copy(update={"language": data.get("language") or ""})
    replaced = {row["index"] for row in plan["affected"]}
    # 原样保留的样本：经复核仍一致（valid）的撤掉同步时留下的过期 stale 标记，其余字段不动；
    # 无内容指纹的历史样本、题型不支持重出的样本无法确认，保留原标记。
    valid = set(plan.get("valid_indices") or [])
    keep_samples = []
    for sample in data.get("samples") or []:
        if sample.get("index") in replaced:
            continue
        sample = dict(sample)
        if sample.get("index") in valid:
            sample.pop("stale", None)
            sample.pop("stale_reason", None)
        keep_samples.append(sample)
    by_status: dict[str, int] = {}
    for row in plan["affected"]:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    manager.log(task, f"局部补生成：对照图谱 {plan['judged_against_kg_id']}，重出 {plan['regenerate']} 条"
                      f"（{'、'.join(f'{k} {v}' for k, v in plan['regenerate_by_type'].items())}），"
                      f"原样保留 {len(keep_samples)} 条"
                      + (f"（其中历史样本 {plan['keep_legacy']} 条无内容指纹，未判定）" if plan["keep_legacy"] else ""),
                extra={"stage": "plan", "progress": 0.02, "regenerate": plan["regenerate"]})
    # generate_testset 的 query 分布要占比（合计 1）；计划里的 regenerate_by_type 是条数。
    total = plan["regenerate"] or 1
    by_type_ratio = {k: v / total for k, v in plan["regenerate_by_type"].items()}
    result = await generate_testset(
        task, plan["judged_against_kg_id"], llm_preset, emb_preset,
        plan["regenerate"], by_type_ratio, False,
        name or f"{data.get('name') or ts_id}·局部补生成",
        data.get("relation_property") or "summary_similarity",
        policy=policy,
        regeneration={
            "from_testset_id": ts_id,
            "from_testset_name": data.get("name"),
            "keep_samples": keep_samples,
            "replaced_indices": sorted(replaced),
            "replaced_by_status": by_status,
        },
    )
    result["regeneration_plan"] = {k: plan[k] for k in
                                   ("regenerate", "regenerate_by_type", "keep", "keep_legacy", "estimate")}
    return result
