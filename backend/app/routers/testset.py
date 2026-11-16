"""Step 2：测试集生成 / 分类预览 / 采纳标记 / 导出。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, BeforeValidator, Field

from .. import storage
from ..policies import GenerationPolicy
from ..ragas_adapter.doc_identity import validate_external_kg
from ..ragas_adapter.ragflow import testset_projection_state, testset_tracking_state
from ..schemas import (
    LanguageOut, OkResult, PatchSampleOut, RegenerateStartedOut, SafeId, TaskRef,
    TestsetDetailOut, TestsetEntryOut,
)
from ..ragas_adapter.testset import (
    LANGUAGE_OPTIONS, assess_kg_capabilities, check_generation_capabilities,
    estimate_generation_cost, export_testset, generate_testset,
    generate_testset_external, kg_health_report,
)
from ..tasks.manager import manager

router = APIRouter(prefix="/api/testset", tags=["testset"])

# 测试集名称长度上限：主生成与局部补生成共用这一个值，前端
# frontend/src/lib/testsetView.ts 的 MAX_TESTSET_NAME_LEN 必须与它一致。
MAX_TESTSET_NAME_LEN = 80


def _validate_testset_name(value: Any) -> Any:
    """名称校验放在 BeforeValidator：先去掉首尾空白，超限时给出中文可读错误。"""
    if isinstance(value, str):
        value = value.strip()
        if len(value) > MAX_TESTSET_NAME_LEN:
            raise ValueError(f"测试集名称最多 {MAX_TESTSET_NAME_LEN} 个字符")
    return value


# Annotated 复用：GenIn / RegenIn / ExternalGenIn 的 name 都声明成 TestsetName，避免上限各写各的
TestsetName = Annotated[
    str, BeforeValidator(_validate_testset_name), Field(max_length=MAX_TESTSET_NAME_LEN)
]


def _testset_not_found(exc: Exception) -> JSONResponse:
    """业务 404：带 error_code，便于前端区分「接口不存在」与「测试集不存在」。"""
    return JSONResponse(status_code=404, content={"detail": str(exc), "error_code": "testset_not_found"})


class GenIn(GenerationPolicy):
    kg_id: str
    llm_preset_id: str
    embedding_preset_id: str
    testset_size: int = Field(default=10, ge=1, le=500)
    query_distribution: dict | None = None
    use_default_distribution: bool = True
    name: TestsetName = ""
    abstract_relation_property: str = "summary_similarity"



@router.get("/capabilities/{kg_id}")
def capabilities(kg_id: SafeId, relation_property: str = "summary_similarity"):
    """按 KG 实际素材返回三类题型可用性与缺失原因（前端据此禁用按钮并展示原因）。"""
    entry = storage.kg_get(kg_id)
    if not entry:
        raise HTTPException(404, "知识图谱不存在")
    try:
        return assess_kg_capabilities(entry, relation_property)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/generate", response_model=TaskRef)
async def generate(inp: GenIn):
    try:
        llm, emb = storage.generation_presets(inp.llm_preset_id, inp.embedding_preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    kg_entry = storage.kg_get(inp.kg_id)
    if not kg_entry:
        raise HTTPException(400, "知识图谱不存在")
    # generation_ready 全仓库无人写入，改为按 KG 实际素材放行。
    # 旧索引里的 generation_ready 字段仍被读取（assess_kg_capabilities 会回显），但不再是开关。
    try:
        check_generation_capabilities(
            kg_entry, inp.query_distribution, inp.use_default_distribution,
            inp.abstract_relation_property)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 执行策略（重试 / 超时 / 并发 / 预算 / 人格数 / 语言）的范围校验统一由 pydantic 的
    # GenerationPolicy 完成（超范围在此前的请求解析阶段就是 422），路由层不再重复判断
    policy = GenerationPolicy.model_validate(
        {k: getattr(inp, k) for k in GenerationPolicy.model_fields})
    # RAGFlow 投影额外占用数据源键：出题期间同步不能切指针 / 清理这份投影
    task = manager.create("generate_testset", ("ragas-generation", f"kg:{inp.kg_id}",
                                               *storage.ragflow_projection_resources(storage.kg_get(inp.kg_id))))
    manager.run(task, generate_testset(task, inp.kg_id, llm, emb, inp.testset_size,
                                       inp.query_distribution, inp.use_default_distribution,
                                       inp.name, inp.abstract_relation_property,
                                       policy=policy))
    return {"task_id": task.id}


class EstimateIn(BaseModel):
    kg_id: str
    testset_size: int = Field(default=10, ge=1, le=500)
    query_distribution: dict | None = None
    use_default_distribution: bool = True
    max_retries: int = Field(default=3, ge=1, le=10)
    num_personas: int = Field(default=3, ge=1, le=10)
    abstract_relation_property: str = "summary_similarity"


@router.post("/estimate-cost")
def estimate_cost(inp: EstimateIn):
    """生成前的 token 预估：按 KG 实际分块长度 + ragas 各 prompt 的固定开销折算。

    目的是让用户在点「生成」之前就看到量级，而不是事后被账单吓到。
    """
    if not storage.kg_get(inp.kg_id):
        raise HTTPException(400, "知识图谱不存在")
    try:
        return estimate_generation_cost(
            inp.kg_id, inp.testset_size, inp.query_distribution,
            inp.use_default_distribution, inp.max_retries, inp.num_personas,
            inp.abstract_relation_property)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ExternalGenIn(GenerationPolicy):
    """外部导入：跳过 Step1 UI，直接传入 KG/chunks/documents 生成测试集。"""
    # 输入模式（三选一）；越界取值由请求解析直接返回 422
    mode: Literal["kg_json", "chunks", "documents"]
    kg_json: dict | None = None        # mode=kg_json 时传入完整 KG 快照
    chunks: list[dict] | None = None   # mode=chunks 时传入预切分块
    documents: list[dict] | None = None  # mode=documents 时传入原文档 [{"name","text"}]
    source_name: str = ""              # KG 显示名

    # 生成参数
    llm_preset_id: str
    embedding_preset_id: str
    testset_size: int = Field(default=10, ge=1, le=500)
    query_distribution: dict | None = None
    use_default_distribution: bool = True
    name: TestsetName = ""  # 与普通生成 / 补生成同一上限
    abstract_relation_property: str = "summary_similarity"

    # documents 模式下的切分参数
    min_tokens: int = 500
    max_tokens: int = 1000


@router.post("/generate-external", response_model=TaskRef)
async def generate_external(inp: ExternalGenIn):
    try:
        llm, emb = storage.generation_presets(inp.llm_preset_id, inp.embedding_preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if inp.mode == "kg_json":
        # 节点 id 是外部 KG 文档身份的唯一锚点：缺 id / id 重复必须在起任务前就拒绝，
        # 否则异常只会变成任务 failed，HTTP 仍 200，前端看不出是输入问题。
        try:
            validate_external_kg(inp.kg_json)
        except ValueError as e:
            return JSONResponse(status_code=400,
                                content={"detail": str(e), "error_code": "external_kg_invalid"})
    policy = GenerationPolicy.model_validate(
        {k: getattr(inp, k) for k in GenerationPolicy.model_fields})
    task = manager.create("generate_testset_external", ("ragas-generation",))
    manager.run(task, generate_testset_external(
        task, inp.mode, inp.kg_json, inp.chunks, inp.documents,
        llm, emb, inp.testset_size, inp.query_distribution,
        inp.use_default_distribution, inp.name,
        inp.abstract_relation_property, inp.source_name,
        inp.min_tokens, inp.max_tokens,
        policy=policy,
    ))
    return {"task_id": task.id}


@router.get("/languages", response_model=list[LanguageOut])
def languages():
    """出题语言选项（前端下拉框数据源，避免前后端各写一份）。"""
    return [{"value": k, "label": v[0]} for k, v in LANGUAGE_OPTIONS.items()]


@router.get("", response_model=list[TestsetEntryOut], response_model_exclude_unset=True)
def list_testsets():
    # 不把服务器本地绝对路径下发给浏览器；前端只用 id 取详情。
    # 连接删除后仍挂旧 id 的测试集要显示为「已停止跟踪」：按连接当前是否存在推导（存量不迁移），
    # 已显式标记的条目原样返回。
    conn_ids = {str(c.get("id")) for c in storage.ragflow_list_connections()}
    out = []
    for entry in storage.ts_list():
        item = {k: v for k, v in entry.items() if k != "path"}
        state = testset_tracking_state(item, conn_ids)
        if state["unbound"] and not item.get("source_unbound"):
            item["source_unbound"] = True
            item["source_unbound_reason"] = state["reason"]
        # 投影失联只在响应里推导：连接还在、重新同步后自动恢复，不写落盘
        item["projection_missing"] = testset_projection_state(item)["missing"]
        out.append(item)
    return out


@router.get("/health/{kg_id}")
def health(kg_id: SafeId):
    """KG 质检报告：节点/边/属性覆盖率 + 各合成器可用性 + 推荐 relation_property。"""
    try:
        return kg_health_report(kg_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/{testset_id}", response_model=TestsetDetailOut, response_model_exclude_unset=True)
def get_testset(testset_id: SafeId):
    data = storage.ts_read(testset_id)
    if data is None:
        raise HTTPException(404, "测试集不存在")
    # 读取时标注 legacy（只在响应里，不改落盘）：无内容哈希的历史样本来源不可信、不能自动判有效
    from ..ragas_adapter.provenance import annotate_legacy_flag

    annotate_legacy_flag(data["samples"])
    # 数据源跟踪状态：连接被删/已失效时显式回显「已停止跟踪」，前端据此给出重新绑定入口
    tracking = testset_tracking_state(data)
    groups: dict[str, list] = {}
    stats: dict[str, int] = {}
    for s in data["samples"]:
        groups.setdefault(s["synthesizer_name"], []).append(s)
        stats[s["synthesizer_name"]] = stats.get(s["synthesizer_name"], 0) + 1
    return {"id": data["id"], "name": data["name"], "created_at": data["created_at"],
            "kg_id": data["kg_id"], "total": len(data["samples"]),
            # 模型元信息：只在详情里展示，导出样本字段不受影响
            "llm_preset_id": data.get("llm_preset_id"), "llm_model": data.get("llm_model"),
            "embedding_preset_id": data.get("embedding_preset_id"),
            "embedding_model": data.get("embedding_model"),
            "relation_property": data.get("relation_property"),
            "groups": groups, "stats": stats, "mode": data.get("mode"),
            "is_mock": data.get("is_mock", False), "stale": data.get("stale", False),
            "stale_sample_count": data.get("stale_sample_count", 0),
            "source_dataset_id": data.get("source_dataset_id"),
            "source_connection_id": data.get("source_connection_id"),
            "source_unbound": tracking["unbound"],
            "source_unbound_reason": tracking["reason"],
            "projection_missing": testset_projection_state(data)["missing"],
            "source_projection_version": data.get("source_projection_version"),
            # 生成时的实体口径（关闭/规则/LLM 混合）；存量测试集没有该字段时为 None（界面显示「未记录」）
            "source_projection_config": data.get("source_projection_config"),
            # /171：终态、少出条数与账单（含增强分项）要在界面上看得到，不只落在文件里
            "testset_size": data.get("testset_size"),
            "stop_reason": data.get("stop_reason"), "partial": data.get("partial", False),
            "failed_samples": data.get("failed_samples", 0), "undelivered": data.get("undelivered"),
            "error": data.get("error"), "usage": data.get("usage") or {},
            # 局部补生成的来源与替换范围（普通测试集为 None）
            "regeneration": data.get("regeneration")}


@router.get("/{testset_id}/provenance")
def testset_provenance(testset_id: SafeId, projection_kg_id: str | None = None):
    """样本出处判定（valid / stale / orphan / legacy / unknown），只读。

    对照哪张图：显式 `projection_kg_id` > 测试集所属 RAGFlow 数据源的**当前投影** > 测试集自身的图。
    （此前默认用测试集自身的图——那是生成时的旧投影，同步后永远判 valid。）
    返回里的 `judged_against` 说明本次对照的来源。路由层不 import ragas。
    """
    from ..ragas_adapter.provenance import evaluate_testset_provenance
    from ..ragas_adapter.testset_regenerate import resolve_judge_target

    data = storage.ts_read(testset_id)
    if data is None:
        raise HTTPException(404, f"测试集 {testset_id} 不存在")
    target, basis = resolve_judge_target(data, projection_kg_id)
    try:
        result = evaluate_testset_provenance(testset_id, target)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    result["judged_against"] = basis
    return result


@router.get("/{testset_id}/regenerate-plan")
def regenerate_plan(testset_id: SafeId,
                    num_personas: Annotated[int, Query(ge=1, le=10)] = 3,
                    max_retries: Annotated[int, Query(ge=1, le=10)] = 3):
    """局部补生成计划（只读、0 token）：哪些样本要重出、按题型多少条、预估 token。

    num_personas / max_retries 传本次补生成打算用的值，预估才对得上（人格开销随人格数线性增长）。
    越界取值由请求解析直接返回 422。
    """
    from ..ragas_adapter.testset_regenerate import plan_regeneration

    try:
        return plan_regeneration(testset_id, num_personas=num_personas, max_retries=max_retries)
    except ValueError as exc:
        return _testset_not_found(exc)


class RegenIn(GenerationPolicy):
    llm_preset_id: str
    embedding_preset_id: str
    name: TestsetName = ""
    # 不传则沿用原测试集的出题语言
    language: Literal["", "zh", "en"] | None = None


@router.post("/{testset_id}/regenerate", response_model=RegenerateStartedOut)
async def regenerate(testset_id: SafeId, inp: RegenIn):
    """局部补生成：只重出内容变化 / 来源被删的样本，结果另存为新测试集，原测试集不改。"""
    from ..ragas_adapter.testset_regenerate import plan_regeneration, regenerate_testset

    try:
        llm, emb = storage.generation_presets(inp.llm_preset_id, inp.embedding_preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        plan = plan_regeneration(testset_id, num_personas=inp.num_personas, max_retries=inp.max_retries)
    except ValueError as exc:
        return _testset_not_found(exc)
    if plan["blocked"]:
        raise HTTPException(400, plan["blocked"])
    kg_id = plan["judged_against_kg_id"]
    task = manager.create("generate_testset", ("ragas-generation", f"kg:{kg_id}",
                                               *storage.ragflow_projection_resources(storage.kg_get(kg_id))))
    # RegenIn.language 允许 None（= 沿用原测试集语言），不能进 pydantic 校验，单独回填
    policy = GenerationPolicy.model_validate(
        {k: getattr(inp, k) for k in GenerationPolicy.model_fields if k != "language"})
    policy = policy.model_copy(update={"language": inp.language})
    manager.run(task, regenerate_testset(
        task, testset_id, llm, emb, policy=policy, name=inp.name or None))
    return {"task_id": task.id, "plan": {k: plan[k] for k in ("regenerate", "regenerate_by_type", "keep",
                                                               "keep_legacy", "estimate")}}


class PatchIn(BaseModel):
    adopted: bool


@router.patch("/{testset_id}/samples/{index}", response_model=PatchSampleOut)
def patch_sample(testset_id: SafeId, index: int, p: PatchIn):
    if not storage.ts_patch_sample(testset_id, index, p.adopted):
        raise HTTPException(404, "测试集或样本不存在")
    return {"ok": True, "index": index, "adopted": p.adopted}


@router.delete("/{testset_id}", response_model=OkResult)
def delete(testset_id: SafeId):
    if not storage.ts_delete(testset_id):
        raise HTTPException(404, "测试集不存在")
    return {"ok": True}


class ExportIn(BaseModel):
    format: str  # csv | jsonl
    scope: str = "all"  # all | adopted | by_type
    types: list[str] | None = None


@router.post("/{testset_id}/export")
def export(testset_id: SafeId, inp: ExportIn):
    data = storage.ts_read(testset_id)
    if data is None:
        raise HTTPException(404, "测试集不存在")
    try:
        filename, content = export_testset(data, inp.format, inp.scope, inp.types)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "ignore").decode() or f"export.{inp.format}"
    return Response(
        content=content,
        media_type="text/csv" if inp.format == "csv" else "application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        },
    )
