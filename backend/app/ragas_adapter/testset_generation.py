"""Step 2 生成主流程：generate_testset 入口、真实 ragas 分阶段生成、终态落盘与交付差额。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from .. import storage
from ..config import RAGAS_AVAILABLE
from ..policies import GenerationPolicy
from ..storage import kg_get, new_id, now_iso, ts_upsert, ts_write
from ..tasks.manager import Task, manager
from ..tasks.staged import Stage, StagedJob
from .clients import build_llm, configure_run
from .provenance import attach_provenance
from .ragflow import annotate_samples
from .testset_capabilities import (
    _enhancement_needs,
    _kg_flags,
    _precheck_kg,
    _similarity_gap_message,
    _unfillable_gaps,
)
from .testset_common import (
    DIST_KEYS,
    LANGUAGE_OPTIONS,
    SYNTHESIZER_MAP,
    _language_directive,
    calculate_split_values,
    default_distribution,
    normalize_distribution,
)
from .testset_demo import _generate_demo_samples
from .testset_enhance import _enhance_projection_kg, _enhancement_usage
from .testset_synth import _PhasedGeneration, _build_persona_safe_synthesizer


def _model_meta(llm_preset: dict, emb_preset: dict, relation_property: str) -> dict:
    """测试集落盘时记录本次实际使用的模型与连线依据。

    此前产物只有 kg_id / distribution / usage，事后无法反查"这批题是哪个模型出的"
    （预设被删除或改名后尤其难查）。这里只落测试集详情，不进导出样本字段，
    避免破坏 ragas 的 CSV/JSONL 兼容性。
    """
    return {
        "llm_preset_id": llm_preset.get("id"),
        "llm_model": llm_preset.get("model"),
        "embedding_preset_id": emb_preset.get("id"),
        "embedding_model": emb_preset.get("model"),
        "relation_property": relation_property,
    }


def _ragflow_generation_fingerprint(kg_entry: dict, llm_preset: dict, emb_preset: dict, params: dict) -> str:
    """同一 source 内容和出题配置共享结果，不把 API key 放入缓存键。"""
    def model_identity(preset: dict) -> dict:
        return {key: preset.get(key) for key in ("id", "provider", "base_url", "model", "extra", "updated_at")}

    payload = {
        "source_hash": kg_entry.get("source_hash"),
        "projection_config": kg_entry.get("projection_config") or {},
        "llm": model_identity(llm_preset),
        "embedding": model_identity(emb_preset),
        "params": params,
        "is_mock": not RAGAS_AVAILABLE,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def generate_testset(
    task: Task,
    kg_id: str,
    llm_preset: dict,
    emb_preset: dict,
    testset_size: int,
    distribution: dict | None,
    use_default: bool,
    name: str,
    abstract_relation_property: str = "summary_similarity",
    *,
    policy: GenerationPolicy | None = None,
    regeneration: dict | None = None,
) -> dict:
    """生成测试集。

    执行策略（重试 / 超时 / 并发 / 预算 / 人格数 / 语言）只从 `policy` 进来，默认值见
    `policies.GenerationPolicy`；路由层不再重复做范围校验。

    `regeneration`（局部补生成，见 testset_regenerate.py）：只出受影响的那几条，落盘时与原样保留的
    样本合并成一个**新**测试集；不复用既有测试集、不写生成指纹，账单只记这次补生成的花费。
    键：from_testset_id / from_testset_name / keep_samples / replaced_indices / replaced_by_status。
    """
    policy = policy or GenerationPolicy()
    task.policy = policy.execution_kwargs()
    raw_dist = default_distribution() if use_default else (distribution or default_distribution())
    keys = DIST_KEYS
    probs, raw_total = normalize_distribution(raw_dist)
    dist = dict(zip(keys, probs))  # 落盘与指纹都用归一化后的比例
    if abs(raw_total - 1.0) > 1e-6:
        manager.log(task, f"⚠ 题型比例合计为 {raw_total:g}（应为 1），已按比例归一化为 "
                          + "、".join(f"{SYNTHESIZER_MAP[k]} {p:.0%}" for k, p in zip(keys, probs)),
                    extra={"stage": "scenarios", "progress": 0.04, "level": "warn"})
    splits = calculate_split_values(probs, testset_size)
    manager.log(task, f"条数分配: {dict(zip((SYNTHESIZER_MAP[k] for k in keys), splits))}",
                extra={"stage": "scenarios", "progress": 0.05})

    kg_entry = kg_get(kg_id)
    if not kg_entry:
        raise ValueError(f"知识图谱 {kg_id} 不存在")

    generation_fingerprint = None
    if not regeneration and kg_entry.get("mode") == "ragflow-projection" and kg_entry.get("source_hash"):
        generation_fingerprint = _ragflow_generation_fingerprint(
            kg_entry, llm_preset, emb_preset,
            {"testset_size": testset_size, "distribution": dist,
             "abstract_relation_property": abstract_relation_property,
             **policy.execution_kwargs()},
        )
        for prior in storage.ts_list():
            if (prior.get("generation_fingerprint") != generation_fingerprint or prior.get("stale")
                    or prior.get("partial")):
                continue
            existing = storage.ts_read(prior["id"])
            if existing and not any(sample.get("stale") for sample in existing.get("samples") or []):
                manager.log(task, f"复用既有测试集 {prior['id']}，无需再次调用 LLM",
                            extra={"stage": "done", "progress": 1.0, "reused": True})
                return {"testset_id": prior["id"], "count": len(existing.get("samples") or []),
                        "by_type": prior.get("by_type") or {}, "is_mock": existing.get("is_mock", False),
                        "usage": {"total_tokens": 0, "llm_calls": 0}, "reused": True}

    # ============ 演示模式（RAGAS_AVAILABLE=False）：模板占位 ============
    usage: dict = {}
    outcome: dict = {"stop_reason": "completed", "error": None, "failed_samples": 0}
    if not RAGAS_AVAILABLE:
        samples = await _generate_demo_samples(task, kg_entry, splits, keys)
    # ============ 真实 ragas 模式 ============
    else:
        # 进入生成后，取消 / 超预算 / 失败都会正常返回（带 outcome），先落盘再定终态
        samples, usage, outcome = await _generate_ragas_samples(
            task, kg_entry, llm_preset, emb_preset, testset_size,
            probs, abstract_relation_property, policy,
        )

    stop_reason = outcome["stop_reason"]
    partial = stop_reason != "completed"
    if not samples and not (usage.get("llm_calls") or usage.get("embedding_calls")
                            or usage.get("total_tokens")):
        # 一次调用都没发出（含增强阶段的 embedding）、也没有产出：没有账单可记，不落空测试集
        if stop_reason == "canceled":
            raise asyncio.CancelledError
        raise RuntimeError(outcome.get("error") or "没有生成任何样本")

    # ============ 共享：落盘（成功 / 部分完成 / 取消 / 失败 都落，账单可审计） ============
    source_dataset_id = None
    source_connection_id = None
    source_projection_version = None
    source_projection_config = None
    if samples and kg_entry.get("mode") == "ragflow-projection":
        kg_data = storage.kg_read_json(kg_id) or {}
        source_dataset_id = kg_entry.get("source_dataset_id")
        # 数据源身份升级为 connection_id + dataset_id；历史投影没有该字段（保持 None）
        source_connection_id = kg_entry.get("source_connection_id")
        source_projection_version = kg_entry.get("projection_version")
        # 实体口径随测试集留档：历史测试集没有该字段时界面显示「未记录」
        source_projection_config = kg_entry.get("projection_config")
        annotate_samples(
            samples, kg_data, source_projection_version or "", source_dataset_id or "",
            connection_id=source_connection_id or "",
        )

    regeneration_meta = None
    if regeneration:
        # 局部补生成：新样本只做本次出处标注；原样保留的样本由 regenerate_testset 准备好
        # （复核仍一致的已撤掉过期 stale 标记，其余字段不动），序号接在后面
        kept = list(regeneration.get("keep_samples") or [])
        next_index = max([int(s.get("index", -1)) for s in kept] + [-1]) + 1
        for offset, sample in enumerate(samples):
            sample["index"] = next_index + offset
            sample["regenerated"] = True
        regeneration_meta = {
            "from_testset_id": regeneration.get("from_testset_id"),
            "from_testset_name": regeneration.get("from_testset_name"),
            "replaced_indices": list(regeneration.get("replaced_indices") or []),
            "replaced_by_status": dict(regeneration.get("replaced_by_status") or {}),
            "kept": len(kept),
            "requested": testset_size,
            "regenerated": len(samples),
        }
        samples = kept + samples
        testset_size = len(kept) + testset_size

    is_mock = (not RAGAS_AVAILABLE) or any(s.get("is_mock") for s in samples)
    stale_sample_count = sum(1 for s in samples if s.get("stale"))
    ts_id = new_id("ts")
    display_name = name or f"测试集-{now_iso()}"
    if partial:
        display_name += f"【{_STOP_LABELS.get(stop_reason, stop_reason)}·部分结果】"
    data = {"id": ts_id, "name": display_name, "created_at": now_iso(),
            "kg_id": kg_id, "testset_size": testset_size, "distribution": dist,
            # 本次实际使用的模型预设与连线依据（任何终态都写，便于事后反查；不进导出）
            **_model_meta(llm_preset, emb_preset, abstract_relation_property),
            "mode": "demo" if not RAGAS_AVAILABLE else "ragas",
            "is_mock": is_mock,
            "language": policy.language,
            # token 账单随测试集落盘：事后可审计"这批题花了多少钱"（取消/失败也落）
            "usage": usage,
            # 终态：completed / canceled / budget_exceeded / failed / undelivered；非 completed 即 partial
            "stop_reason": stop_reason,
            "partial": partial,
            "failed_samples": outcome.get("failed_samples", 0),
            "undelivered": outcome.get("undelivered"),
            "error": outcome.get("error"),
            "source_dataset_id": source_dataset_id,
            "source_connection_id": source_connection_id,
            "source_projection_version": source_projection_version,
            "source_projection_config": source_projection_config,
            # 部分结果 / 有失败条目的不参与复用：下次同配置应重新生成，而不是复用残缺结果
            "generation_fingerprint": (None if partial or outcome.get("failed_samples")
                                       else generation_fingerprint),
            # 局部补生成的来源与替换范围（普通生成为 None）；账单 usage 只含这次补生成
            "regeneration": regeneration_meta,
            # 普通生成恒为 0；局部补生成时，无法复核或无法重出的保留样本可能仍带 stale
            "stale": stale_sample_count > 0,
            **({"stale_sample_count": stale_sample_count} if stale_sample_count else {}),
            "samples": samples}
    path = ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry["path"] = path
    entry["count"] = len(samples)
    by_type: dict[str, int] = {}
    for s in samples:
        by_type[s["synthesizer_name"]] = by_type.get(s["synthesizer_name"], 0) + 1
    entry["by_type"] = by_type
    ts_upsert(entry)
    if is_mock:
        manager.log(
            task,
            f"⚠ 测试集已保存（MOCK 数据）: {entry['name']}（{len(samples)} 条）\n"
            f"  此测试集含 mock 样本，不可用于 RAG 评测。请安装 ragas 后重新生成。",
            extra={"stage": "done", "progress": 1.0, "level": "warn", "is_mock": True},
        )
    else:
        cost_note = ""
        if usage.get("total_tokens"):
            per = usage["total_tokens"] / max(1, len(samples))
            cost_note = (f"\n  token 合计 {usage['total_tokens']:,}"
                         f"（均摊 {per:,.0f}/条，LLM 调用 {usage.get('llm_calls', 0)} 次）")
            enh = usage.get("enhancement") or {}
            if enh.get("total_tokens"):
                cost_note += (f"\n  其中生成前增强 {enh['total_tokens']:,} token"
                              f"（LLM {enh['llm_tokens']:,} + embedding {enh['embedding_tokens']:,}）")
        fail_note = ""
        if outcome.get("failed_samples"):
            fail_note = (f"\n  ⚠ {outcome['failed_samples']}/{outcome.get('planned_samples', '?')} 条生成失败已跳过，"
                         f"其余照常保存（失败原因见上方 ✗ 日志）")
        gap = outcome.get("undelivered") or {}
        if gap.get("total_missing"):
            fail_note += (f"\n  ⚠ 请求 {gap['requested']} 条，实际产出 {gap['delivered']} 条，"
                          f"少出 {gap['total_missing']} 条（已标记 partial，差额见 usage.undelivered_samples）")
        manager.log(task, f"测试集已保存: {entry['name']}（{len(samples)} 条）{cost_note}{fail_note}",
                    extra={"stage": "done", "progress": 1.0,
                           **({"level": "warn"} if (partial or fail_note) else {}), **usage})

    # ---- 终态：先落盘，再按 stop_reason 决定任务以什么状态结束 ----
    if stop_reason == "canceled":
        # manager.cancel() 已把任务置为 canceled 并发过 done，这里只需把取消传播出去
        raise asyncio.CancelledError
    if stop_reason == "failed" or not samples:
        reason = outcome.get("error") or "没有生成任何样本"
        if stop_reason == "budget_exceeded":
            reason = f"token 超预算（{policy.token_budget:,}）自动中止，尚未产出任何样本"
        raise RuntimeError(f"{reason}。已完成 {len(samples)} 条与 token 账单"
                           f"（{usage.get('total_tokens', 0):,} token）已保存为测试集「{display_name}」（{ts_id}）")
    # 超预算但已有样本：以 succeeded 结束（前端会打开这份部分结果），result 里带 partial 标记
    return {"testset_id": ts_id, "count": len(samples), "by_type": by_type,
            "is_mock": is_mock, "usage": usage,
            "partial": partial, "stop_reason": stop_reason,
            "undelivered": outcome.get("undelivered"),
            "failed_samples": outcome.get("failed_samples", 0)}


def _token_usage_parser(result):
    """CostCallbackHandler 的用量解析：非流式读 llm_output，流式读消息 usage_metadata（与遥测同一口径）。

    ragas 自带的 get_token_usage_for_openai 只认 llm_output，流式调用下恒为 0，账单会少记。
    """
    from ragas.cost import TokenUsage

    from .telemetry import llm_result_token_usage

    ti, to, _ = llm_result_token_usage(result)
    model = ((getattr(result, "llm_output", None) or {}).get("model_name") or "")
    return TokenUsage(input_tokens=ti, output_tokens=to, model=model)


_STOP_LABELS = {"completed": "已完成", "canceled": "已取消", "budget_exceeded": "超预算中止",
                "failed": "生成失败", "undelivered": "条数不足"}


def _delivery_gap(requested: int, planned: int, delivered: int) -> dict:
    """请求条数 / 计划条数 / 实际产出条数三者的差额（供 usage 与终态提示使用）。"""
    return {
        "requested": requested,
        "planned": planned,
        "delivered": delivered,
        "not_planned": max(0, requested - planned),
        "not_delivered": max(0, planned - delivered),
        "total_missing": max(0, requested - delivered),
    }


async def _generate_ragas_samples(
    task: Task,
    kg_entry: dict,
    llm_preset: dict,
    emb_preset: dict,
    testset_size: int,
    probs: list[float],
    abstract_relation_property: str,
    policy: GenerationPolicy,
) -> tuple[list[dict], dict, dict]:
    """真实 ragas 模式：加载 KG →（投影 KG 按缺口增强）→ 预检 → 分阶段驱动 ragas 出题。

    编排交给阶段化任务框架（`app.tasks.staged`）：四个 Stage 依次是 enhance（生成前增强）/
    personas / scenarios / samples，进度按**完成条数**自建计数（不再折算 ragas 两个 tqdm）。
    返回 (samples, usage, outcome)。usage 是 token/调用账单（见 telemetry.GenerationTelemetry）；
    outcome = {stop_reason: completed|canceled|budget_exceeded|failed, error, failed_samples, ...}。
    **进入生成后的任何终态都正常返回**（含取消 / 失败，persist 已结算账单），由 generate_testset
    统一落盘账单与已完成样本，再决定任务终态。硬缺口（实体缺失等）在花任何 token 之前直接抛
    ValueError；投影 KG 可补齐的缺口（摘要/向量/themes/summary_similarity 边）会先增强并原子写回。
    emb_preset 目前只用于投影 KG 的生成前增强，出题阶段 ragas 不调 embedding。
    """
    from ragas.cost import CostCallbackHandler
    from ragas.testset.graph import KnowledgeGraph
    from ragas.testset.synthesizers.testset_schema import Testset, TestsetSample

    from .telemetry import GenerationTelemetry

    kg = KnowledgeGraph.load(kg_entry["kg_path"])
    # SDK 层重试关掉（sdk_max_retries=0），重试全部交给下面的 RunConfig，避免两层相乘
    llm = build_llm(llm_preset, timeout=policy.llm_timeout, sdk_max_retries=0)

    sim_edges = sum(1 for r in kg.relationships if r.type == "summary_similarity")
    overlap_edges = sum(1 for r in kg.relationships if r.type == "entities_overlap")
    manager.log(task, f"KG 已加载：{len(kg.nodes)} 节点 / {len(kg.relationships)} 关系"
                      f"（summary_similarity {sim_edges} / entities_overlap {overlap_edges}）",
                extra={"stage": "kg_loaded", "progress": 0.1,
                       "node_count": len(kg.nodes), "relationship_count": len(kg.relationships),
                       "similarity_edges": sim_edges, "overlap_edges": overlap_edges})

    # 只提交分到 >0 条的合成器：n=0 的多跳抽象题会让 find_n_indirect_clusters(0) 抛错拖垮整批
    keys = ["single_hop_specific", "multi_hop_abstract", "multi_hop_specific"]
    splits = calculate_split_values(probs, testset_size)
    active_keys = {k for k, n in zip(keys, splits) if n > 0}

    # 投影 KG 只有原文/实体，缺 ragas 需要的摘要、摘要向量、themes 与相似边。
    # 在花出题 token 之前按缺口补齐；素材齐全时 needs 为空 → 0 次额外调用。
    tele = getattr(task, "generation_telemetry", None) or GenerationTelemetry(token_budget=policy.token_budget)
    prior_usage = tele.snapshot()
    tele.should_stop = task.cancel_event.is_set
    task.generation_telemetry = tele
    enh_usage: dict = _enhancement_usage({}, {})
    flags = _kg_flags(kg, abstract_relation_property)
    # 摘要相似边一条都没有时，先按现有向量预演：已能证明「补齐也建不出边」就 0 token 直接报错，
    # 不再花增强 token 去撞同一堵墙（T10 真跑暴露的能力过度承诺）
    if ("multi_hop_abstract" in active_keys and abstract_relation_property == "summary_similarity"
            and flags["relation_property_edges"] == 0):
        from .kg import estimate_similarity_edges
        estimate = estimate_similarity_edges(kg)
        if estimate["embedded_nodes"] >= 2 and estimate["reachable_edges"] == 0:
            raise ValueError(_similarity_gap_message(estimate, flags["overlap_edges"]))
    # 自动增强补不上的硬缺口先按 0 token 预检报错，避免白白花掉增强 token 后再失败
    if _unfillable_gaps(flags, active_keys, abstract_relation_property):
        _precheck_kg(kg, active_keys, abstract_relation_property)
    needs = _enhancement_needs(kg_entry, flags, active_keys, abstract_relation_property)

    async def enhance_stage(ctx) -> None:
        """生成前增强（阶段 0）：缺口为 0 时 0 调用；期间已花的 token 在 finally 里结算。"""
        nonlocal enh_usage
        if not needs:
            ctx.set_stage("enhancing", 0)
            # 无需增强也要按同一口径预检（硬缺口已在建 job 之前拦过，这里兜住组合缺口）
            _precheck_kg(kg, active_keys, abstract_relation_property)
            return
        ctx.set_stage("enhancing", 4)
        ctx.log(f"投影 KG 缺少 {'、'.join(needs)}，生成前自动补齐缺失项（已具备的不重复调用）")
        before_snap = tele.snapshot()

        def on_progress(label: str) -> None:
            ctx.log(f"· 增强完成：{label}")
            ctx.advance(1)

        try:
            stats = await _enhance_projection_kg(
                task, kg_entry, kg, llm_preset, emb_preset, tele, on_progress,
                max_retries=policy.max_retries, llm_timeout=policy.llm_timeout,
                max_workers=policy.max_workers,
                include_themes="multi_hop_abstract" in active_keys,
            )
        finally:
            # 增强没跑完也是一种终态：已花的 token 必须进账单（persist 统一结算）
            enh_usage = _enhancement_usage(before_snap, tele.snapshot())
            for line in tele.drain():
                ctx.log(line)
        ctx.log(f"增强完成：chunk 摘要 {stats['chunk_summaries']}、文档摘要 {stats['document_summaries']}、"
                f"themes {stats['theme_chunks']}、向量 {stats['embeddings']}、"
                f"相似边 {stats['similarity_edges']}")
        # 预检放在增强之后：刚补齐的素材要参与判定，否则会误报「缺摘要向量」
        _precheck_kg(kg, active_keys, abstract_relation_property)

    # 语言锁定：ragas 的出题 prompt 全是英文（instruction + few-shot 示例都是英文），
    # 且没有任何一句约束输出语言，喂中文语料时会中英摇摆、甚至"英文问题+中文答案"。
    # `llm_context` 是 ragas 官方留的自由文本钩子，会作为 QueryConditions.llm_context
    # 进入出题 prompt（见 single_hop/prompts.py 的 instruction 第 3 条），用它钉住语言。
    # 注意：TestsetGenerator 自身的 llm_context 只在 query_distribution 为 None 时才生效，
    # 必须逐个合成器设 llm_context。
    llm_ctx = _language_directive(policy.language)
    factories = {
        key: (lambda k=key: _build_persona_safe_synthesizer(
            k, llm, llm_ctx, abstract_relation_property))
        for key in keys
    }
    active = [(factories[k](), n) for k, n in zip(keys, splits) if n > 0]
    skipped = [SYNTHESIZER_MAP[k] for k, n in zip(keys, splits) if n == 0]
    if skipped:
        manager.log(task, f"以下题型分到 0 条，本次不提交：{', '.join(skipped)}",
                    extra={"stage": "kg_loaded", "progress": 0.12})

    # RunConfig 是重试的唯一上限：ragas 给 ChatOpenAI 设 run_config 时会把 exception_types
    # 改成只重试 RateLimitError；SDK 层重试已关，这里把超时/连接/5xx 这类瞬时错误也纳入，
    # 保证「失败重试上限 N」= 每次调用最多发 N 次，不再被 SDK 层放大
    run_config = configure_run(llm, timeout=policy.llm_timeout, retries=policy.max_retries,
                              workers=policy.max_workers)

    if task.cancel_event.is_set():
        raise asyncio.CancelledError

    manager.log(task, f"▶ 开始生成（预算/容错：单次调用超时 {policy.llm_timeout}s、"
                      f"每次调用最多发送 {policy.max_retries} 次、并发 {policy.max_workers}、"
                      f"人格数 {policy.num_personas}"
                      f"、出题语言 {LANGUAGE_OPTIONS.get(policy.language or '', ('跟随语料',))[0]}"
                      + (f"、token 预算 {policy.token_budget:,}" if policy.token_budget else "、token 预算不限")
                      + "）",
                extra={"stage": "generating", "progress": 0.15})

    cost_cb = CostCallbackHandler(token_usage_parser=_token_usage_parser)
    runner = _PhasedGeneration(llm, kg, active, policy.num_personas, run_config,
                               callbacks=[tele, cost_cb], telemetry=tele)
    # persist 在**任何终态**都会执行（含取消 / 失败），落盘所需的三元组先存进箱子里
    box: dict = {"samples": [], "usage": {}, "outcome": {"stop_reason": "failed", "error": None}}

    def drain_notes(ctx, _value=None) -> None:
        """阶段结束后把编排器缓冲（✗ / ⚠ 明细）转进日志缓冲（工作线程侧只写缓冲）。"""
        for line in runner.drain_notes():
            ctx.log(line)

    async def persist(ctx) -> tuple[list[dict], dict, dict]:
        """终态结算：账单校正 + 样本转换 + 出处标注 + 交付差额，结果放进 box 供入口落盘。"""
        usage = tele.snapshot()
        # ragas 自带的 CostCallbackHandler 是权威口径，优先用它校正遥测的累计值
        try:
            tt = cost_cb.total_tokens()
            if isinstance(tt, list):
                usage["input_tokens"] = sum(u.input_tokens for u in tt)
                usage["output_tokens"] = sum(u.output_tokens for u in tt)
            else:
                usage["input_tokens"] = tt.input_tokens
                usage["output_tokens"] = tt.output_tokens
            usage["input_tokens"] += prior_usage["input_tokens"] + enh_usage.get("input_tokens", 0)
            usage["output_tokens"] += prior_usage["output_tokens"] + enh_usage.get("output_tokens", 0)
            usage["llm_total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
            usage["total_tokens"] = usage["llm_total_tokens"] + usage.get("embedding_tokens", 0)
            usage["usage_source"] = "ragas_cost_cb"
        except Exception:
            usage["usage_source"] = "telemetry"  # 上游没回传 usage 时退回遥测累计
        # 增强分项：LLM / embedding / 合计；没有增强时也给零值，保证字段口径稳定
        usage["enhancement"] = enh_usage or _enhancement_usage({}, {})

        # 编排器缓冲只能在这里落日志（工作线程不碰 manager），取消时也不会丢
        for line in runner.drain_notes():
            manager.log(task, line, extra={"stage": "accounting", **usage})
        for line in tele.summary_lines():
            manager.log(task, line, extra={"stage": "accounting", "progress": 0.95, **usage})

        ordered, planned, sample_failures, scenario_failures, nodes_by_pos = runner.snapshot_results()
        error = ctx.error or runner.error
        stop_reason = ctx.stop_reason or "completed"
        if stop_reason == "completed":
            if not error and not ordered:
                error = f"全部 {planned} 条样本生成失败" if planned else "没有生成任何样本"
                first = (sample_failures or scenario_failures or [""])[0]
                if first:
                    error += f"（首个错误：{first}）"
            if error:
                stop_reason = "failed"
        if error and stop_reason not in ("canceled", "budget_exceeded"):
            manager.log(task, f"✗ {error}", extra={"stage": stop_reason, "level": "warn", **usage})
        usage["planned_samples"] = planned
        usage["failed_samples"] = len(sample_failures)

        # ragas Testset.to_list() → 项目样本格式
        testset = Testset(samples=[TestsetSample(eval_sample=s, synthesizer_name=name)
                                   for name, s in ordered])
        samples: list[dict] = []
        for idx, s in enumerate(testset.to_list()):
            samples.append({
                "index": idx,
                "synthesizer_name": s.get("synthesizer_name", ""),
                "user_input": s.get("user_input", ""),
                "reference": s.get("reference", ""),
                "reference_contexts": s.get("reference_contexts") or [],
                "retrieved_contexts": s.get("retrieved_contexts") or [],
                "response": s.get("response") or "",
                # ragas 给每条样本随机指定的"提问风格/长度/人格"，是题目读起来像病句的直接原因：
                # QueryStyle 有 4 挡，其中 MISSPELLED（故意错别字）、POOR_GRAMMAR（故意语法错）、
                # WEB_SEARCH_LIKE（关键词堆叠，不成句）占 3 挡，只有 PERFECT_GRAMMAR 是通顺的。
                # 不保留这三个字段就无法解释单条样本为何长这样，故一并落盘并在前端展示。
                "query_style": s.get("query_style") or "",
                "query_length": s.get("query_length") or "",
                "persona_name": s.get("persona_name") or "",
                "adopted": True,
            })
        #  P2：样本出处（chunk 节点 id / 内容哈希 / 文档节点 id / 投影版本）。
        # 来源是前面的 scenario.nodes，不是文本反查；解析不到 chunk 的样本保持无出处（读取时判 legacy）。
        attach_provenance(samples, nodes_by_pos, kg, kg_entry)

        delivered = len(samples)
        gap = _delivery_gap(testset_size, planned, delivered)
        usage["requested_samples"] = testset_size
        usage["delivered_samples"] = delivered
        usage["undelivered_samples"] = gap["total_missing"]
        if gap["total_missing"] > 0:
            # 少出的条数必须是「可见的部分结果」，不能伪装成 completed
            if stop_reason == "completed":
                stop_reason = "undelivered"
            manager.log(
                task,
                f"⚠ 请求 {gap['requested']} 条，实际产出 {delivered} 条，少出 {gap['total_missing']} 条"
                f"（场景不足 {gap['not_planned']} 条、样本失败 {gap['not_delivered']} 条），"
                f"已标记为部分结果（partial）",
                extra={"stage": "done", "progress": 0.98, "level": "warn"},
            )

        outcome = {
            "stop_reason": stop_reason,
            "error": error,
            "phase": runner.phase,
            "planned_samples": planned,
            "failed_samples": len(sample_failures),
            "requested_samples": testset_size,
            "delivered_samples": delivered,
            "undelivered": gap,
            "sample_failures": sample_failures[:50],
            "scenario_failures": scenario_failures,
        }
        box["samples"], box["usage"], box["outcome"] = samples, usage, outcome
        return samples, usage, outcome

    job = StagedJob(
        task,
        [
            Stage("enhance", enhance_stage),
            Stage("personas", runner.run_personas, checkpoint=drain_notes),
            Stage("scenarios", runner.run_scenarios, checkpoint=drain_notes),
            Stage("samples", runner.run_samples, checkpoint=drain_notes),
        ],
        policy=policy,
        persist=persist,
        name="testset",
    )
    # 遥测绑定后：预算熔断只置停止原因，由框架在阶段边界收口（不再手工轮询）
    job.ctx.bind_telemetry(tele)
    try:
        await job.run()
    except asyncio.CancelledError:
        pass  # 取消也返回：generate_testset 落盘已完成样本与账单后，再统一重抛取消
    except Exception:
        # 阶段异常在 persist 里已经结算过账单 → 按「部分结果」返回，由入口保存后决定终态；
        # 连账单都没结算出来（persist 本身失败）才是真异常，原样上抛
        if not box["usage"]:
            raise
    finally:
        # 取消时工作线程可能还没把解释性日志（如「人格生成阶段」）写进缓冲，
        # 而 drainer 已经停了：给一个很短的窗口把在途线程收掉，再把剩余缓冲刷进日志
        pending = [f for f in list(getattr(task, "worker_futures", [])) if not f.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=1.0)
            except Exception:  # noqa: BLE001 - 收尾失败不影响已结算的结果
                pass
        for message, extra in job.ctx.drain():
            if message:
                manager.log(task, message, extra=extra)
    return box["samples"], box["usage"], box["outcome"]
