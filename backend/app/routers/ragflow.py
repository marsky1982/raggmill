"""RAGFlow 手动同步 / 连接发现 API。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import storage
from ..errors import error_response
from ..policies import GenerationPolicy
from ..ragas_adapter import entity_resolution
from ..schemas import (
    RagflowCheckResult, RagflowConnectionDeletedOut, RagflowConnectionOut,
    RagflowDatasetListOut, RagflowEstimateOut, RagflowId, RagflowManualSyncStartedOut,
    RagflowSettingsOut, RagflowSnapshotOut, RagflowSourceId, RagflowSyncStartedOut,
)
from ..ragas_adapter.ragflow import (
    RAGFlowClient,
    RAGFlowError,
    RAGFlowHTTPError,
    current_overlap_config,
    entity_exclusion_keys,
    mark_testsets_unbound,
    normalize_base_url,
    probe_ragflow,
    rebind_source,
    run_connection_test,
    sync_ragflow,
)
from ..tasks.manager import ResourceBusyError, manager

router = APIRouter(prefix="/api/ragflow", tags=["ragflow"])
log = logging.getLogger(__name__)


class SyncIn(GenerationPolicy):
    dataset_id: str
    base_url: str = ""
    api_key: str | None = None
    name: str = ""
    full_refresh: bool = False
    timeout: int = 30


class TestIn(BaseModel):
    timeout: int = 10


class ProbeIn(BaseModel):
    sample_docs: int | None = None


class ConnectionIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    declared_version: str | None = None


class ConnectionPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ConnectionSyncIn(GenerationPolicy):
    name: str = ""
    full_refresh: bool = False
    timeout: int = 30


def _policy_of(inp: GenerationPolicy) -> GenerationPolicy:
    """同步请求体继承 GenerationPolicy：只取执行参数，忽略路由专用字段。

    `GenerationPolicy` 是 `extra="forbid"`，不能直接 `GenerationPolicy(**inp.model_dump())`。
    """
    return GenerationPolicy(**{key: getattr(inp, key) for key in GenerationPolicy.model_fields})


@router.post("/sync", response_model=RagflowManualSyncStartedOut)
async def sync(inp: SyncIn):
    saved = storage.ragflow_get_config(inp.dataset_id) or {}
    try:
        base_url = normalize_base_url(inp.base_url or saved.get("base_url") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    saved_base = (saved.get("base_url") or "").strip().rstrip("/")
    if inp.api_key:
        api_key = inp.api_key
    elif saved_base and base_url != saved_base:
        # 已存 key 只能发往保存它时的地址，换地址必须重新提供 key，防止被导向任意服务器
        raise HTTPException(400, "RAGFlow base_url 已变更，请重新填写 API key（已保存的 key 不会发往新地址）")
    else:
        api_key = inp.api_key if inp.api_key is not None else saved.get("api_key", "")
    if not api_key:
        raise HTTPException(400, "RAGFlow API key 不能为空")
    try:
        resources = storage.ragflow_sync_resources(inp.dataset_id, None)
    except storage.UnsafePathError as exc:
        raise HTTPException(400, str(exc))
    task = manager.create("ragflow_sync", resources)
    manager.run(task, sync_ragflow(
        task, inp.dataset_id, base_url, api_key, inp.name,
        inp.full_refresh, inp.timeout, policy=_policy_of(inp),
    ))
    return {"task_id": task.id, "mode": "manual"}


def _error_json(status_code: int, detail: str, error_code: str,
                upstream_status: int | None = None) -> JSONResponse:
    """错误体：detail（中文）+ error_code；502 把上游状态码放进 extra.upstream_status。"""
    extra = {"upstream_status": upstream_status} if status_code == 502 else None
    return error_response(status_code, detail, error_code, extra)


def _upstream_json(exc: RAGFlowHTTPError) -> JSONResponse:
    return _error_json(502, exc.detail, exc.error_code, exc.upstream_status)


def _connection_or_error(connection_id: str):
    try:
        conn = storage.ragflow_get_connection(connection_id, reveal_key=True)
    except storage.UnsafePathError as exc:
        return None, _error_json(400, str(exc), "bad_request")
    if not conn:
        return None, _error_json(404, "RAGFlow 连接不存在或已删除", "connection_not_found")
    return conn, None


def _client_or_error(conn: dict, timeout: int):
    try:
        base_url = normalize_base_url(conn.get("base_url") or "")
    except ValueError as exc:
        return None, _error_json(400, str(exc), "bad_request")
    api_key = conn.get("api_key") or ""
    if not api_key:
        return None, _error_json(400, "该连接缺少 API key，请先补填", "bad_request")
    return RAGFlowClient(base_url, api_key, timeout=timeout), None


def _int_field(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _first_int(row: dict, keys: tuple[str, ...]):
    for key in keys:
        parsed = _int_field(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _dataset_view(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        # 不同 RAGFlow 版本字段名不一致：老版 doc_num/chunk_num，新版 document_count/chunk_count
        "doc_count": _first_int(row, ("doc_count", "document_count", "doc_num")),
        "chunk_count": _first_int(row, ("chunk_count", "chunk_num")),
        "status": row.get("status"),
    }


def _connection_view(conn: dict) -> dict:
    """对外视图：只回掩码，永不回明文/密文；legacy 缺省 False（旧记录可能没这个字段）。"""
    view = {k: v for k, v in conn.items() if k not in ("api_key", "api_key_enc")}
    view["api_key_mask"] = view.get("api_key_mask") or ""
    view["legacy"] = bool(view.get("legacy"))
    return view


@router.get("/connections", response_model=list[RagflowConnectionOut],
            response_model_exclude_unset=True)
def list_connections():
    return [_connection_view(conn) for conn in storage.ragflow_list_connections()]


@router.post("/connections", response_model=RagflowConnectionOut,
             response_model_exclude_unset=True)
def create_connection(inp: ConnectionIn):
    name = (inp.name or "").strip()
    if not name:
        return _error_json(400, "连接名称不能为空", "bad_request")
    api_key = (inp.api_key or "").strip()
    if not api_key:
        return _error_json(400, "API key 不能为空", "bad_request")
    try:
        base_url = normalize_base_url(inp.base_url)
    except ValueError as exc:
        return _error_json(400, str(exc), "bad_request")
    if storage.ragflow_find_connection_by_name(name):
        return _error_json(409, "已存在同名连接，请换一个名称", "name_conflict")
    saved = storage.ragflow_save_connection({
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "declared_version": inp.declared_version,
    })
    return _connection_view(saved)


@router.get("/connections/{connection_id}", response_model=RagflowConnectionOut,
            response_model_exclude_unset=True)
def get_connection(connection_id: RagflowId):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    return _connection_view(conn)


@router.patch("/connections/{connection_id}", response_model=RagflowConnectionOut,
              response_model_exclude_unset=True)
def update_connection(connection_id: RagflowId, inp: ConnectionPatch):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    updates: dict = {"id": connection_id}
    if inp.name is not None:
        name = inp.name.strip()
        if not name:
            return _error_json(400, "连接名称不能为空", "bad_request")
        if storage.ragflow_find_connection_by_name(name, exclude_id=connection_id):
            return _error_json(409, "已存在同名连接，请换一个名称", "name_conflict")
        updates["name"] = name
    if inp.base_url is not None:
        try:
            new_base = normalize_base_url(inp.base_url)
        except ValueError as exc:
            return _error_json(400, str(exc), "bad_request")
        if new_base != (conn.get("base_url") or "").rstrip("/"):
            # legacy 连接的 source 固定指向旧目录，改地址只会让凭据发给陌生实例
            if conn.get("legacy"):
                return _error_json(400, "历史配置来自旧目录，服务地址不可修改", "bad_request")
            if not (inp.api_key or "").strip():
                return _error_json(
                    400,
                    "服务地址已变更，请重新填写 API Key（已保存的 key 不会发往新地址）",
                    "credential_rebind_required",
                )
            updates["base_url"] = new_base
    if inp.api_key is not None and inp.api_key.strip():
        updates["api_key"] = inp.api_key.strip()
    if "base_url" in updates or "api_key" in updates:
        # N18：上次测试结果对应的是旧地址 / 旧 key，改了就作废，避免旧徽章继续显示「可用」
        updates["last_test"] = None
        updates["probed_version"] = None
    saved = storage.ragflow_save_connection(updates)
    return _connection_view(saved)


@router.delete("/connections/{connection_id}", response_model=RagflowConnectionDeletedOut)
def delete_connection(connection_id: RagflowId):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    # 删除连接只删凭据；sources/、投影、测试集一律保留。但引用它的测试集从此会停止跟随
    # 该数据源 —— 这个断裂必须显式落下标记（source_connection_id 原值保留作证据），
    # 之后可用「重新绑定」把它改绑到另一条连接。
    if not storage.ragflow_delete_connection(connection_id):
        return _error_json(404, "连接不存在或已删除", "connection_not_found")
    unbound = mark_testsets_unbound(connection_id)
    return {"deleted": True, "retained": True,
            "unbound_testset_ids": [row["testset_id"] for row in unbound]}


class RebindIn(BaseModel):
    from_connection_id: str
    timeout: int = 30


@router.post("/connections/{connection_id}/datasets/{dataset_id}/rebind")
def rebind_connection_dataset(connection_id: RagflowId, dataset_id: RagflowId, inp: RebindIn):
    """把某个知识库的已同步数据源从旧连接改绑到当前连接（同 dataset_id）。"""
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    client, err = _client_or_error(conn, inp.timeout)
    if err:
        return err
    try:
        return rebind_source(connection_id, dataset_id, inp.from_connection_id, client)
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    except ResourceBusyError as exc:
        return _error_json(409, str(exc), "resource_busy")
    except RAGFlowHTTPError as exc:
        return _upstream_json(exc)
    except RAGFlowError as exc:
        return _error_json(502, str(exc), "upstream_error")
    except ValueError as exc:
        return _error_json(400, str(exc), "rebind_failed")


@router.get("/connections/{connection_id}/datasets", response_model=RagflowDatasetListOut)
def list_connection_datasets(connection_id: RagflowId, page: int = 1, page_size: int = 20):
    if page < 1 or page_size < 1 or page_size > 100:
        return _error_json(400, "分页参数非法：page ≥ 1，page_size 在 1~100 之间", "bad_request")
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    client, err = _client_or_error(conn, 30)
    if err:
        return err
    try:
        data = client.list_datasets_page(page, page_size)
    except RAGFlowHTTPError as exc:
        return _upstream_json(exc)
    except RAGFlowError as exc:
        # 上游返回了 200 但形状不符：仍按 502 + upstream_status 返回，供前端展示
        return _error_json(502, str(exc), "upstream_error", client.last_status)
    return {
        "items": [_dataset_view(row) for row in data["items"]],
        "total": data["total"],
        "page": page,
        "page_size": page_size,
        "has_more": data["has_more"],
    }


@router.post("/connections/{connection_id}/test", response_model=RagflowCheckResult)
async def test_connection(connection_id: RagflowId, inp: TestIn | None = None):
    timeout = max(5, min(int((inp.timeout if inp else 10)), 180))
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    client, err = _client_or_error(conn, timeout)
    if err:
        return err
    result = await asyncio.to_thread(run_connection_test, client, timeout)
    # 快照最近一次探测结果；测试期间连接被删除时不回写（否则会「复活」一条无名连接）
    try:
        storage.ragflow_record_connection_test(connection_id, result.get("probed_version"), {
            "at": storage.now_iso(),
            "verdict": result["verdict"],
            "stages": result["stages"],
        })
    except Exception:  # 落盘失败不应让探测结果丢失
        log.warning("RAGFlow 连接测试结果落盘失败: %s", connection_id, exc_info=True)
    return result


@router.post("/connections/{connection_id}/datasets/{dataset_id}/probe",
             response_model=RagflowSyncStartedOut, status_code=202)
async def probe_connection_dataset(connection_id: RagflowId, dataset_id: RagflowId,
                                   inp: ProbeIn | None = None):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
        source_id = storage.ragflow_source_id(connection_id, dataset)
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    try:
        base_url = normalize_base_url(conn.get("base_url") or "")
    except ValueError as exc:
        return _error_json(400, str(exc), "bad_request")
    api_key = conn.get("api_key") or ""
    if not api_key:
        return _error_json(400, "该连接缺少 API key，请先补填", "bad_request")
    try:
        task = manager.create("ragflow_probe", (source_id,))
    except ResourceBusyError as exc:
        return _error_json(409, str(exc), "probe_in_progress")
    sample_docs = inp.sample_docs if inp else None
    manager.run(task, probe_ragflow(
        task, connection_id, dataset, base_url, api_key, sample_docs=sample_docs,
    ))
    return JSONResponse(status_code=202, content={"task_id": task.id, "source_id": source_id})


@router.post("/connections/{connection_id}/datasets/{dataset_id}/sync",
             response_model=RagflowSyncStartedOut, status_code=202)
async def sync_connection_dataset(connection_id: RagflowId, dataset_id: RagflowId,
                                  inp: ConnectionSyncIn | None = None):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
        # legacy 连接只能同步它原来的那个库：对别的库走旧路径会覆盖那个库旧目录里的
        # config.json（换成本连接的地址与 key），并以别人的快照为基线算 delta —— 跨实例互毁
        if conn.get("legacy") and conn.get("dataset_id") and dataset != conn["dataset_id"]:
            return _error_json(400, f"历史连接只能同步它原来的知识库（{conn['dataset_id']}）；"
                                    "同步其他知识库请新建连接", "bad_request")
        # legacy 连接沿用旧目录布局（§2.2）：等效 connection_id 就是它合成时的那条 id
        effective_conn = storage.legacy_connection_id(dataset) if conn.get("legacy") else connection_id
        source_id = storage.ragflow_source_id(effective_conn, dataset)
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    try:
        base_url = normalize_base_url(conn.get("base_url") or "")
    except ValueError as exc:
        return _error_json(400, str(exc), "bad_request")
    api_key = conn.get("api_key") or ""
    if not api_key:
        return _error_json(400, "该连接缺少 API key，请先补填", "bad_request")
    body = inp or ConnectionSyncIn()
    timeout = max(5, min(int(body.timeout), 180))
    try:
        task = manager.create("ragflow_sync", storage.ragflow_sync_resources(
            dataset, None if conn.get("legacy") else connection_id))
    except ResourceBusyError:
        return _error_json(409, "该知识库已有同步、探测或出题任务在执行，请等它结束", "sync_in_progress")
    manager.run(task, sync_ragflow(
        task, dataset, base_url, api_key, body.name, body.full_refresh, timeout,
        connection_id=None if conn.get("legacy") else connection_id,
        policy=_policy_of(body),
    ))
    return JSONResponse(status_code=202, content={"task_id": task.id, "source_id": source_id})


@router.get("/sources/{source_id}/snapshot", response_model=RagflowSnapshotOut,
            response_model_exclude_unset=True)
def get_source_snapshot(source_id: RagflowSourceId):
    try:
        snapshot, pointer = storage.ragflow_source_snapshot(source_id)
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    if not snapshot:
        return _error_json(404, "RAGFlow 源快照不存在", "snapshot_not_found")
    pointer = pointer or {}
    return {
        "source_id": source_id,
        "dataset_id": pointer.get("dataset_id"),
        "connection_id": pointer.get("connection_id"),
        "snapshot_id": pointer.get("snapshot_id"),
        "projection_kg_id": pointer.get("projection_kg_id"),
        "projection_version": pointer.get("projection_version"),
        "fetched_at": snapshot.get("fetched_at"),
        "source_hash": snapshot.get("source_hash"),
        "document_count": len(snapshot.get("documents") or {}),
        "chunk_count": len(snapshot.get("chunks") or {}),
        "ragflow_version": snapshot.get("ragflow_version"),
    }


@router.get("/datasets/{dataset_id}/config")
def get_config(dataset_id: RagflowId):
    data = storage.ragflow_get_config(dataset_id)
    if not data:
        raise HTTPException(404, "RAGFlow 数据集尚未配置")
    data.pop("api_key", None)
    return data


@router.get("/datasets/{dataset_id}/snapshot", response_model=RagflowSnapshotOut,
            response_model_exclude_unset=True)
def get_snapshot(dataset_id: RagflowId):
    snapshot, pointer = storage.ragflow_snapshot_current(dataset_id)
    if not snapshot:
        # 与新布局的 /sources/{id}/snapshot 同一套错误码：前端只在 snapshot_not_found
        # 时显示「还没同步过」，其它错误要报读取失败
        return _error_json(404, "RAGFlow 源快照不存在", "snapshot_not_found")
    return {
        "dataset_id": dataset_id,
        "snapshot_id": (pointer or {}).get("snapshot_id"),
        "projection_kg_id": (pointer or {}).get("projection_kg_id"),
        "projection_version": (pointer or {}).get("projection_version"),
        "fetched_at": snapshot.get("fetched_at"),
        "source_hash": snapshot.get("source_hash"),
        "document_count": len(snapshot.get("documents") or {}),
        "chunk_count": len(snapshot.get("chunks") or {}),
        "ragflow_version": snapshot.get("ragflow_version"),
    }


# ---------- 实体规范化 / LLM 消歧开关（按数据源） ----------

class EntitySettingsIn(BaseModel):
    entity_resolution: str = "disabled"
    llm_preset_id: str | None = None
    # 人工别名表本期只接受 API 写入（无编辑 UI）；不传则保留原值
    aliases: dict[str, list[str]] | None = None


class EntityEstimateIn(BaseModel):
    entity_resolution: str | None = None
    llm_preset_id: str | None = None


def _applied_projection_config(dataset_id: str, source_dir) -> dict | None:
    """当前指针指向投影的 projection_config；没有投影时 None。"""
    _, pointer = storage.ragflow_snapshot_current(dataset_id, source_dir=source_dir)
    kg_id = (pointer or {}).get("projection_kg_id")
    if not kg_id:
        return None
    entry = storage.kg_get(kg_id)
    if not entry:
        return None
    return entry.get("projection_config") or {}


def _applied_entity_mode(applied: dict | None) -> str:
    """已生效投影的实体口径。缺字段 = 该投影从未做过消歧，等价于 `disabled`。"""
    raw = (applied or {}).get("entity_resolution")
    if not raw:
        return "disabled"
    try:
        return entity_resolution.normalize_mode(raw)
    except ValueError:
        return "disabled"


def _entity_settings_view(dataset_id: str, source_dir, settings: dict) -> dict:
    """开关状态 + 生效状态。

    `pending_rebuild` 比四件事，逐条都有明确含义：
    1. 还没有投影 → 下次同步才生效；
    2. 已生效口径与当前模式不同 → 重建（含「同一 source 曾开过 hybrid 又关掉」）；
    3. 启用态才比口径指纹（模型 / alias / 阈值变化）；
    4. 建边参数（高频词门槛等）与已生效投影不同 → 重建（与同步的复用条件同一口径）。
    关闭态不比指纹：关闭态同步本来就不写 `config_fingerprint`，
    拿「算出来的指纹」比 null、或比一个遗留指纹，都会得出无意义的重建结论。
    """
    try:
        mode = entity_resolution.normalize_mode(settings.get("entity_resolution") or "disabled")
    except ValueError:
        mode = "disabled"
    preset_id = settings.get("llm_preset_id")
    preset = storage.get_preset(preset_id) if preset_id else None
    fingerprint = entity_resolution.compute_config_fingerprint(
        mode, preset_id, (preset or {}).get("model"), settings.get("aliases"))
    applied = _applied_projection_config(dataset_id, source_dir)
    applied_mode = _applied_entity_mode(applied)
    applied_fingerprint = (applied or {}).get("config_fingerprint")
    if applied is None:
        pending = True
    elif applied_mode != mode:
        pending = True
    elif mode == "disabled":
        pending = False
    else:
        pending = applied_fingerprint != fingerprint
    if applied is not None and (applied or {}).get("overlap") != current_overlap_config().get("overlap"):
        pending = True
    return {
        "entity_resolution": mode,
        "llm_preset_id": preset_id,
        "config_fingerprint": fingerprint,
        "applied_fingerprint": applied_fingerprint,
        "pending_rebuild": pending,
    }


def _write_entity_settings(dataset_id: str, source_dir, inp: EntitySettingsIn):
    try:
        mode = entity_resolution.normalize_mode(inp.entity_resolution)
    except ValueError as exc:
        return None, _error_json(400, str(exc), "bad_request")
    preset_id = (inp.llm_preset_id or "").strip() or None
    if mode in entity_resolution.LLM_MODES:
        # 界面之外的调用也必须校验：没有可用的测试集生成预设时不允许开开关
        preset = storage.get_preset(preset_id) if preset_id else None
        if not preset or preset.get("kind") not in storage.LLM_PRESET_KINDS:
            return None, _error_json(
                400, "开启实体消歧需要选择存在且可用的测试集生成用 LLM 预设", "preset_unavailable",
            )
    existing = storage.ragflow_settings_read(dataset_id, source_dir=source_dir)
    aliases = inp.aliases if inp.aliases is not None else existing.get("aliases")
    data = {
        "schema_version": entity_resolution.SETTINGS_SCHEMA_VERSION,
        "entity_resolution": mode,
        "llm_preset_id": preset_id,
        "prompt_version": entity_resolution.PROMPT_VERSION,
        "updated_at": storage.now_iso(),
    }
    if aliases:
        data["aliases"] = aliases
    storage.ragflow_settings_write(dataset_id, data, source_dir=source_dir)
    return _entity_settings_view(dataset_id, source_dir, data), None


def _estimate_view(dataset_id: str, source_dir, inp: EntityEstimateIn | None):
    settings = storage.ragflow_settings_read(dataset_id, source_dir=source_dir)
    mode_value = (inp.entity_resolution if inp and inp.entity_resolution else None) \
        or settings.get("entity_resolution") or "disabled"
    try:
        mode = entity_resolution.normalize_mode(mode_value)
    except ValueError as exc:
        return _error_json(400, str(exc), "bad_request")
    preset_id = (inp.llm_preset_id if inp else None) or settings.get("llm_preset_id")
    preset = storage.get_preset(preset_id) if preset_id else None
    snapshot, _ = storage.ragflow_snapshot_current(dataset_id, source_dir=source_dir)
    if not snapshot:
        # 不为预估去拉 RAGFlow：只读本地快照
        return {"available": False, "mode": mode, "hint": "先同步一次再预估"}
    estimate = entity_resolution.estimate_resolution(
        snapshot,
        mode=mode,
        aliases=settings.get("aliases"),
        dataset_id=dataset_id,
        source_dir=source_dir,
        exclude_keys=entity_exclusion_keys(snapshot),
        model=(preset or {}).get("model"),
    )
    return {"available": True, **estimate}


@router.get("/connections/{connection_id}/datasets/{dataset_id}/settings",
            response_model=RagflowSettingsOut)
def get_connection_dataset_settings(connection_id: RagflowId, dataset_id: RagflowId):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    source_dir = None if conn.get("legacy") else storage.ragflow_source_dir(connection_id, dataset)
    return _entity_settings_view(dataset, source_dir,
                                storage.ragflow_settings_read(dataset, source_dir=source_dir))


@router.put("/connections/{connection_id}/datasets/{dataset_id}/settings",
            response_model=RagflowSettingsOut)
def put_connection_dataset_settings(connection_id: RagflowId, dataset_id: RagflowId,
                                    inp: EntitySettingsIn):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    if conn.get("legacy") and conn.get("dataset_id") and dataset != conn["dataset_id"]:
        return _error_json(400, f"历史连接只能设置它原来的知识库（{conn['dataset_id']}）", "bad_request")
    source_dir = None if conn.get("legacy") else storage.ragflow_source_dir(connection_id, dataset)
    view, err = _write_entity_settings(dataset, source_dir, inp)
    return err or view


@router.post("/connections/{connection_id}/datasets/{dataset_id}/settings/estimate",
             response_model=RagflowEstimateOut, response_model_exclude_unset=True)
def estimate_connection_dataset_settings(connection_id: RagflowId, dataset_id: RagflowId,
                                         inp: EntityEstimateIn | None = None):
    conn, err = _connection_or_error(connection_id)
    if err:
        return err
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    source_dir = None if conn.get("legacy") else storage.ragflow_source_dir(connection_id, dataset)
    return _estimate_view(dataset, source_dir, inp)


@router.get("/datasets/{dataset_id}/settings", response_model=RagflowSettingsOut)
def get_dataset_settings(dataset_id: RagflowId):
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    return _entity_settings_view(dataset, None, storage.ragflow_settings_read(dataset))


@router.put("/datasets/{dataset_id}/settings", response_model=RagflowSettingsOut)
def put_dataset_settings(dataset_id: RagflowId, inp: EntitySettingsIn):
    try:
        dataset = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    except storage.UnsafePathError as exc:
        return _error_json(400, str(exc), "bad_request")
    view, err = _write_entity_settings(dataset, None, inp)
    return err or view
