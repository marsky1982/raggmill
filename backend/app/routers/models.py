"""模型配置预设：CRUD + 连通性自检。"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, field_validator

from .. import storage
from .. import crypto
from ..schemas import OkResult, PresetDefaultOut, PresetOut, SafeId, SelfCheckResult

router = APIRouter(prefix="/api/presets", tags=["presets"])


class PresetIn(BaseModel):
    kind: str  # llm | llm_testset | embedding
    name: str
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    extra: dict = {}

    @field_validator("name", "base_url", "model", mode="before")
    @classmethod
    def _strip(cls, value):
        """去首尾空格：带空格的名称会让同名判重失效、列表里看起来一样却是两条。"""
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name_required(cls, value: str) -> str:
        if not value:
            raise ValueError("预设名称不能为空")
        return value

    @field_validator("extra")
    @classmethod
    def _reasoning_effort(cls, value: dict) -> dict:
        effort = (value or {}).get("reasoning_effort")
        if effort in (None, ""):
            (value or {}).pop("reasoning_effort", None)
        elif effort not in storage.REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort 只能是 {'/'.join(storage.REASONING_EFFORTS)} 或留空")
        return value or {}

    @field_validator("extra")
    @classmethod
    def _enable_thinking(cls, value: dict) -> dict:
        """思考模式开关只接受布尔值；空/缺省表示「按端点默认」（DashScope 关、其它不发）。"""
        if not value or "enable_thinking" not in value:
            return value or {}
        flag = value["enable_thinking"]
        if flag is None or flag == "":
            value.pop("enable_thinking", None)
        elif not isinstance(flag, bool):
            raise ValueError("enable_thinking 只能是 true / false 或留空")
        return value


# 所有 LLM 类 kind（连通性自检按 ChatOpenAI 测；testset 生成用 llm_testset 单独存）
LLM_KINDS = storage.LLM_PRESET_KINDS
ALL_KINDS = storage.PRESET_KINDS


@router.get("", response_model=list[PresetOut], response_model_exclude_unset=True)
def list_all(kind: str | None = None):
    kinds = [kind] if kind in ALL_KINDS else ALL_KINDS
    return [p for k in kinds for p in storage.list_presets(k)]


@router.post("", response_model=PresetOut, response_model_exclude_unset=True)
def create(p: PresetIn):
    if p.kind not in ALL_KINDS:
        raise HTTPException(400, f"kind 必须是 {ALL_KINDS} 之一")
    # 同名同 kind 不允许新建副本（前端误走 POST 的兜底），改名或编辑既有预设。
    # 查重与写入在存储锁内一次完成，避免两步之间被别人插入同名。
    try:
        return storage.create_preset_if_absent(p.model_dump())
    except storage.PresetNameConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/{preset_id}", response_model=PresetOut, response_model_exclude_unset=True)
def update(preset_id: SafeId, p: PresetIn):
    if p.kind not in ALL_KINDS:
        raise HTTPException(400, f"kind 必须是 {ALL_KINDS} 之一")
    if not storage.get_preset(preset_id):
        raise HTTPException(404, "预设不存在")
    data = p.model_dump()
    data["id"] = preset_id
    return storage.save_preset(data)


@router.delete("/{preset_id}", response_model=OkResult)
def delete(preset_id: SafeId):
    if not storage.delete_preset(preset_id):
        raise HTTPException(404, "预设不存在")
    return {"ok": True}


@router.post("/{preset_id}/set-default", response_model=PresetDefaultOut,
             response_model_exclude_unset=True)
def set_default(preset_id: SafeId, kind: str = "llm"):
    if kind not in ALL_KINDS:
        raise HTTPException(400, f"kind 必须是 {ALL_KINDS} 之一")
    result = storage.set_default_preset(preset_id, kind)
    if not result:
        raise HTTPException(404, "预设不存在或 kind 不匹配")
    return {"ok": True, "preset": result}


@router.post("/{preset_id}/test", response_model=SelfCheckResult)
async def test_conn(preset_id: SafeId):
    preset = storage.get_preset(preset_id)
    if not preset:
        raise HTTPException(404, "预设不存在")
    t0 = time.perf_counter()
    kind = preset["kind"]
    api_key = storage.preset_plain_key(preset)
    base_url = (preset.get("base_url") or "").strip()
    model = preset.get("model") or ""
    try:
        if kind in LLM_KINDS:
            from langchain_openai import ChatOpenAI
            client = ChatOpenAI(
                base_url=base_url or None, api_key=api_key or "EMPTY",
                model=model, timeout=10, max_retries=0,
            )
            # 带超时，防止坏 endpoint 挂死
            await asyncio.wait_for(client.ainvoke([HumanMessage(content="ping")]), timeout=12)
        else:
            from langchain_openai import OpenAIEmbeddings
            client = OpenAIEmbeddings(
                base_url=base_url or None, api_key=api_key or "EMPTY",
                model=model, timeout=10, max_retries=0,
                check_embedding_ctx_length=False,
            )
            await asyncio.wait_for(client.aembed_query("ping"), timeout=12)
    except crypto.PresetKeyError as e:
        return {"ok": False, "error_code": e.error_code, "detail": str(e),
                "latency_ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error_code": "connect_failed", "detail": _short_err(e),
                "latency_ms": int((time.perf_counter() - t0) * 1000)}
    return {"ok": True, "latency_ms": int((time.perf_counter() - t0) * 1000)}


def _short_err(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    return s[:300]
