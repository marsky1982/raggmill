"""任务通用：状态查询 / SSE 流 / 取消。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from ..errors import ApiError, error_body
from ..schemas import OkResult, SafeId, TaskOut
from ..tasks.manager import manager

router = APIRouter(prefix="/api", tags=["tasks"])


def _task_missing() -> ApiError:
    return ApiError(404, "任务不存在", "task_not_found")


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks():
    return manager.all()


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: SafeId):
    t = manager.get(task_id)
    if not t:
        raise _task_missing()
    return t.view()


async def _sse_gen(task_id: str, after: int = 0):
    task = manager.get(task_id)
    if not task:
        # SSE 的错误事件也用同一套错误体（前端 api.ts 按 detail 收尾）
        payload = error_body("任务不存在", "task_not_found")
        yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return
    q = await manager.subscribe(task, after)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event, payload = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"id: {payload.get('seq', 0)}\nevent: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if event == "done":
                break
    finally:
        manager.unsubscribe(task, q)


@router.get("/tasks/{task_id}/stream")
def stream(task_id: SafeId, last_event_id: str | None = Header(default=None)):
    try:
        after = max(0, int(last_event_id or 0))
    except (ValueError, TypeError):
        after = 0
    if not manager.get(task_id):
        raise _task_missing()
    return StreamingResponse(_sse_gen(task_id, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# 别名（api-design.md 的路径）。断线续传与主路由同口径：透传 Last-Event-ID
@router.get("/kg/tasks/{task_id}/stream")
def kg_stream(task_id: SafeId, last_event_id: str | None = Header(default=None)):
    return stream(task_id, last_event_id)


@router.get("/testset/tasks/{task_id}/stream")
def ts_stream(task_id: SafeId, last_event_id: str | None = Header(default=None)):
    return stream(task_id, last_event_id)


async def _cancel(task_id: str):
    if not manager.get(task_id):
        raise _task_missing()
    ok = manager.cancel(task_id)
    return {"ok": ok}


@router.post("/tasks/{task_id}/cancel", response_model=OkResult)
async def cancel(task_id: SafeId):
    return await _cancel(task_id)


@router.post("/kg/tasks/{task_id}/cancel", response_model=OkResult)
async def kg_cancel(task_id: SafeId):
    return await _cancel(task_id)


@router.post("/testset/tasks/{task_id}/cancel", response_model=OkResult)
async def ts_cancel(task_id: SafeId):
    return await _cancel(task_id)
