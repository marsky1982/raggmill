"""D1A 离线评估：观察导入、指标能力检查和评分任务。"""
from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import storage
from .. import crypto
from ..errors import ApiError
from ..policies import GenerationPolicy
from ..schemas import SafeId
from ..ragas_adapter.evaluation import DEFAULT_METRICS, METRICS, capability_report, normalize_observations, score_offline
from ..tasks.manager import manager

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


class ObservationIn(BaseModel):
    id: str | None = None
    user_input: str
    response: str | None = None
    retrieved_contexts: list[str] = Field(default_factory=list)
    reference: str | None = None
    metadata: dict = Field(default_factory=dict)


class PreviewIn(BaseModel):
    observations: list[ObservationIn]
    metric_keys: list[str] = list(DEFAULT_METRICS)


class ScoreIn(PreviewIn, GenerationPolicy):
    observations: list[ObservationIn] = Field(default_factory=list)
    dataset_id: str | None = None
    llm_preset_id: str
    embedding_preset_id: str | None = None
    name: str = ""


class ImportIn(PreviewIn):
    name: str = ""


@router.get("/metrics")
def metrics():
    return [{"metric_key": key, **value} for key, value in METRICS.items()]


@router.post("/preview")
def preview(inp: PreviewIn):
    try:
        rows = normalize_observations([row.model_dump() for row in inp.observations])
        return capability_report(rows, inp.metric_keys)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/import")
def import_observations(inp: ImportIn):
    try:
        rows = normalize_observations([row.model_dump() for row in inp.observations])
        capability = capability_report(rows, inp.metric_keys)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    dataset_id = storage.new_id("dataset")
    data = {"id": dataset_id, "kind": "offline_observations", "name": inp.name or f"离线观察-{storage.now_iso()}",
            "created_at": storage.now_iso(), "status": "draft", "metric_keys": inp.metric_keys,
            "sample_count": len(rows), "observations": rows, "capability": capability}
    storage.evaluation_write(dataset_id, data)
    return {"dataset_id": dataset_id, "status": data["status"], "sample_count": len(rows), "capability": capability}


@router.post("/score")
async def score(inp: ScoreIn):
    llm = storage.get_preset(inp.llm_preset_id)
    emb = storage.get_preset(inp.embedding_preset_id) if inp.embedding_preset_id else None
    if not llm or llm.get("kind") not in storage.LLM_PRESET_KINDS:
        raise HTTPException(400, "LLM 预设不存在或类型不匹配")
    if emb and emb.get("kind") not in storage.EMBEDDING_PRESET_KINDS:
        raise HTTPException(400, "Embedding 预设类型不匹配")
    try:
        storage.preset_plain_key(llm)
        if emb:
            storage.preset_plain_key(emb)
    except crypto.PresetKeyError as exc:
        raise ApiError(503, str(exc), exc.error_code) from exc
    rows = [row.model_dump() for row in inp.observations]
    if inp.dataset_id:
        dataset = storage.evaluation_read(inp.dataset_id)
        if not dataset or dataset.get("kind") != "offline_observations":
            raise HTTPException(400, "离线观察数据集不存在")
        rows = dataset.get("observations") or []
        if not inp.metric_keys or inp.metric_keys == list(DEFAULT_METRICS):
            inp.metric_keys = dataset.get("metric_keys") or list(DEFAULT_METRICS)
    if not inp.metric_keys:
        raise HTTPException(400, "至少选择一个指标")
    if any(METRICS[key]["needs_embedding"] for key in inp.metric_keys) and (not emb or emb.get("kind") != "embedding"):
        raise HTTPException(400, "所选指标需要 Embedding 预设，且类型必须为 embedding")
    if not rows:
        raise HTTPException(400, "observations 或 dataset_id 至少提供一个")
    try:
        capability_report(rows, inp.metric_keys)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    task = manager.create("offline_evaluation", ("evaluation",))
    manager.run(task, score_offline(task, rows, inp.metric_keys,
                                    llm, emb, GenerationPolicy(**inp.execution_kwargs()), inp.name))
    return {"task_id": task.id}


@router.get("")
def list_evaluations():
    return storage.evaluation_list()


@router.get("/{evaluation_id}")
def get_evaluation(evaluation_id: SafeId):
    data = storage.evaluation_read(evaluation_id)
    if not data:
        raise HTTPException(404, "评估不存在")
    return data


@router.delete("/{evaluation_id}")
def delete_evaluation(evaluation_id: SafeId):
    # 非法 id 由 storage.check_id 抛 UnsafePathError，main.py 统一转 400
    if not storage.evaluation_delete(evaluation_id):
        raise HTTPException(404, "评估不存在")
    return {"deleted": True, "evaluation_id": evaluation_id}


def _export_row(data: dict, observation: dict) -> dict:
    values = {
        metric.get("metric_key"): {
            item.get("observation_id"): item.get("value")
            for item in metric.get("scores") or []
        }
        for metric in data.get("scores") or []
    }
    row = {
        "id": observation.get("id"),
        "user_input": observation.get("user_input"),
        "response": observation.get("response"),
        "retrieved_contexts": observation.get("retrieved_contexts"),
        "reference": observation.get("reference"),
    }
    for key in data.get("metric_keys") or []:
        row[key] = values.get(key, {}).get(observation.get("id"))
    return row


def _disposition(filename: str, ascii_name: str | None = None) -> str:
    """下载头：中文文件名必须走 RFC 5987（Starlette 的响应头只能 latin-1 编码）。

    ascii_name 是不认 filename* 的客户端（脚本 / 命令行下载工具）的备用名；演示数据必须自带标记，
    否则从中文字名里摘掉中文后只剩一个孤零零的 `-`，看不出是模拟分数。
    """
    fallback = ascii_name or filename.encode("ascii", "ignore").decode() or "evaluation-export"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/{evaluation_id}/export")
def export_evaluation(evaluation_id: SafeId, format: str = "jsonl"):
    data = storage.evaluation_read(evaluation_id)
    if not data:
        raise HTTPException(404, "评估不存在")
    rows = [_export_row(data, item) for item in data.get("observations") or []]
    # 演示评分必须能和真实分数区分（与测试集导出同一规矩）：文件名后缀 + 每行 is_mock
    is_mock = bool(data.get("is_mock"))
    suffix = "-演示数据" if is_mock else ""
    ascii_stem = f"{evaluation_id}{'-demo' if is_mock else ''}"
    if format.lower() == "jsonl":
        if is_mock:
            for row in rows:
                row["is_mock"] = True
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        return Response(body, media_type="application/jsonl; charset=utf-8",
                        headers={"Content-Disposition": _disposition(
                            f"{evaluation_id}{suffix}.jsonl", f"{ascii_stem}.jsonl")})
    if format.lower() != "csv":
        raise HTTPException(400, "format 只支持 jsonl 或 csv")
    def safe_cell(value):
        if isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        text = str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@") else text
    output = io.StringIO(newline="")
    fields = list(rows[0]) if rows else ["id", "user_input", "response", "retrieved_contexts", "reference"] + list(data.get("metric_keys") or [])
    if is_mock:
        fields.append("is_mock")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cells = {key: safe_cell(row.get(key)) for key in fields}
        if is_mock:
            cells["is_mock"] = "true"
        writer.writerow(cells)
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": _disposition(
                        f"{evaluation_id}{suffix}.csv", f"{ascii_stem}.csv")})
