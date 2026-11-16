"""Step 1：知识图谱生成 / 列表 / 详情 / 预览裁剪 / 删除。"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import storage
from ..errors import error_response
from ..policies import GenerationPolicy
from ..ragas_adapter.kg import build_kg
from ..schemas import (
    CheckpointDeletedOut, CheckpointOut, KgDeletedOut, KgDetailOut, KgEntryOut,
    KgPreviewOut, SafeId, TaskRef,
)
from ..tasks.manager import manager

router = APIRouter(prefix="/api/kg", tags=["knowledge_graph"])


class GenIn(GenerationPolicy):
    document_ids: list[str]
    llm_preset_id: str
    embedding_preset_id: str
    name: str = ""
    advanced: dict = {}
    run_id: str | None = None  # 续跑时传入的生成会话 ID
    resume: bool = False        # True = 从最近断点继续
    min_tokens: int = 500       # HeadlineSplitter 最小 chunk token 数
    max_tokens: int = 1000      # HeadlineSplitter 最大 chunk token 数


@router.post("/generate", response_model=TaskRef)
async def generate(inp: GenIn):
    if inp.run_id:
        storage.check_id(inp.run_id, "run_id")  # 非法则 UnsafePathError → 400，不让它进后台任务
    try:
        llm, emb = storage.generation_presets(inp.llm_preset_id, inp.embedding_preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    run_id = inp.run_id or storage.new_id("kgrun")
    task = manager.create("build_kg", ("ragas-generation", f"run:{run_id}"))
    manager.run(task, build_kg(
        task, inp.document_ids, llm, emb, inp.name,
        run_id=run_id, resume=inp.resume,
        min_tokens=inp.min_tokens, max_tokens=inp.max_tokens,
        policy=GenerationPolicy(**inp.execution_kwargs()),
    ))
    return {"task_id": task.id}


@router.get("/checkpoints", response_model=list[CheckpointOut])
def list_checkpoints():
    """列出可恢复的生成断点（运行中或失败且未到末步）。"""
    return storage.ckpt_list()


log = logging.getLogger(__name__)
# 取消后延迟删除断点：最长等待时间与轮询间隔。Step 1 取消时正在跑的那一个 transform 会跑完
# （线程无法中断），可能要几分钟，所以给足 30 分钟；超时就放弃，断点留在列表里让用户手动删。
_DEFERRED_DELETE_TIMEOUT = 1800.0
_DEFERRED_DELETE_POLL = 0.5
_DEFERRED_DELETES: dict[str, asyncio.Task] = {}  # run_id → 等待收尾的删除协程（持引用防止被回收）


def _busy_checkpoint_tasks(run_id: str) -> list:
    """仍占用该断点的任务：未到终态，或已到终态但工作线程还没收尾。"""
    from ..tasks.manager import TERMINAL
    return [t for t in list(manager.tasks.values())
            if f"run:{run_id}" in t.resources
            and (t.status not in TERMINAL or any(not f.done() for f in t.worker_futures))]


async def _delete_checkpoint_when_idle(run_id: str) -> None:
    deadline = time.monotonic() + _DEFERRED_DELETE_TIMEOUT
    try:
        while _busy_checkpoint_tasks(run_id):
            if time.monotonic() > deadline:
                log.warning("断点 %s 等待任务收尾超时，未删除（可在断点列表手动删除）", run_id)
                return
            await asyncio.sleep(_DEFERRED_DELETE_POLL)
        await asyncio.to_thread(storage.ckpt_delete, run_id)
    except Exception as exc:  # noqa: BLE001 —— 后台清理失败只记日志，断点留在列表里
        log.warning("断点 %s 延迟删除失败: %s", run_id, exc)
    finally:
        _DEFERRED_DELETES.pop(run_id, None)


@router.delete("/checkpoints/{run_id}", response_model=CheckpointDeletedOut,
               response_model_exclude_unset=True)
async def delete_checkpoint(run_id: SafeId):
    """删除断点。占用它的任务**已在取消中**时不再报 409：登记为「收尾后删除」并返回 202。

    取消是异步的（在跑的那一步要跑完），前端「取消并删除断点」紧接着发删除，
    以前会偶发 409、断点留在列表里。没有取消、仍在正常运行的任务照旧 409。
    """
    from ..tasks.manager import ResourceBusyError
    storage.check_id(run_id, "run_id")
    busy = _busy_checkpoint_tasks(run_id)
    if busy:
        if not all(t.cancel_event.is_set() for t in busy):
            raise ResourceBusyError("该断点仍有任务在运行或收尾，请等待结束后删除")
        if run_id not in _DEFERRED_DELETES:
            _DEFERRED_DELETES[run_id] = asyncio.create_task(_delete_checkpoint_when_idle(run_id))
        return JSONResponse(status_code=202, content={
            "ok": True, "deferred": True, "detail": "任务正在收尾，结束后自动删除断点"})
    if not storage.ckpt_delete(run_id):
        raise HTTPException(404, "断点不存在")
    return {"ok": True}


@router.get("", response_model=list[KgEntryOut], response_model_exclude_unset=True)
def list_kgs():
    items = storage.kg_list()
    for it in items:
        it.pop("kg_path", None)
    return items


@router.get("/{kg_id}", response_model=KgDetailOut, response_model_exclude_unset=True)
def get_kg(kg_id: SafeId):
    entry = storage.kg_get(kg_id)
    if not entry:
        raise HTTPException(404, "知识图谱不存在")
    data = storage.kg_read_json(kg_id)
    if data is None:
        raise HTTPException(404, "KG 文件缺失")
    return {"meta": entry, "kg": data}


def _preview_properties(props: dict) -> dict:
    """预览只给界面看：长向量换成说明文字。"""
    out = {}
    for k, v in (props or {}).items():
        if isinstance(v, list) and len(v) > 16 and all(isinstance(x, (int, float)) for x in v[:16]):
            out[k] = f"向量[{len(v)}]（预览已省略）"
        else:
            out[k] = v
    return out


# 预览里边的保留顺序：先结构边（归属、顺序），再按原顺序保留关联边
_PREVIEW_EDGE_PRIORITY = ("child", "next")


@router.get("/{kg_id}/preview", response_model=KgPreviewOut)
def preview(kg_id: SafeId, max_nodes: int = 200, max_edges: int = 2000):
    data = storage.kg_read_json(kg_id)
    if data is None:
        raise HTTPException(404, "知识图谱不存在")
    nodes = data.get("nodes", [])
    rels = data.get("relationships", [])
    if len(nodes) > max_nodes:
        # 优先保留 document 节点，chunk 按序截断
        docs = [n for n in nodes if n.get("type") == "document"]
        chunks = [n for n in nodes if n.get("type") != "document"][: max(0, max_nodes - len(docs))]
        kept = docs + chunks
        keep_ids = {n["id"] for n in kept}
        rels = [r for r in rels if r["source"] in keep_ids and r["target"] in keep_ids]
        nodes = kept
    if len(rels) > max_edges:
        ordered = ([r for r in rels if r.get("type") in _PREVIEW_EDGE_PRIORITY]
                   + [r for r in rels if r.get("type") not in _PREVIEW_EDGE_PRIORITY])
        rels = ordered[:max_edges]
    nodes = [dict(n, properties=_preview_properties(n.get("properties") or {})) for n in nodes]
    rels = [dict(r, properties=_preview_properties(r.get("properties") or {})) for r in rels]
    type_count: dict[str, int] = {}
    for n in nodes:
        type_count[n.get("type") or "unknown"] = type_count.get(n.get("type") or "unknown", 0) + 1
    return {"nodes": nodes, "relationships": rels,
            "stats": {"node_count": len(nodes), "relationship_count": len(rels), "by_type": type_count,
                      "total_nodes": len(data.get("nodes", [])), "total_relationships": len(data.get("relationships", []))}}


@router.delete("/{kg_id}", response_model=KgDeletedOut)
def delete(kg_id: SafeId, detach_sources: bool = False):
    from ..tasks.manager import ResourceBusyError
    # 出题 / 增强正在用这份图谱时不能删：否则任务结束时测试集断链，增强写回还会把它加回索引
    if f"kg:{kg_id}" in manager.busy_resources():
        raise ResourceBusyError("该知识图谱正被出题任务使用，请等任务结束（或取消）后再删除")
    # 用户显式确认（detach_sources=true）时要回报被重置的数据源，故先取一份引用明细
    refs = storage.ragflow_pointers_referencing_kg(kg_id) if detach_sources else []
    try:
        deleted = storage.kg_delete(kg_id, detach_sources=detach_sources)
    except storage.KGReferencedError as exc:
        # 仍是 RAGFlow 数据源的当前投影：删掉会让下一次同步拿不到旧投影（断链），拒绝删除
        return error_response(409, str(exc), "kg_in_use", {"sources": exc.sources})
    except storage.KGPointerResetError as exc:
        # 已确认要切断指针但改写中途失败：指针已回滚、图谱未删除，告诉前端是哪一步没做成
        return error_response(500, str(exc), "kg_pointer_reset_failed", {
            "failed_source": {"position": exc.position, "source_id": exc.source_id},
            "rollback_errors": exc.rollback_errors,
        })
    if not deleted:
        raise HTTPException(404, "知识图谱不存在")
    return {"ok": True, "detached_sources": refs}
