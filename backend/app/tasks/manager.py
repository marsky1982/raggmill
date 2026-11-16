"""TaskManager：内存任务表 + SSE 订阅 + 取消。"""
from __future__ import annotations

import asyncio
import logging
import json
import sqlite3
import time
import threading
import os
import uuid
from pathlib import Path
from collections import deque
from datetime import datetime, timezone

from ..jsonutil import sanitize_nonfinite
from ..storage import new_id
from ..single_instance import current_identity, identity_alive

log = logging.getLogger("tasks")
TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}


class ResourceBusyError(ValueError):
    pass


class Task:
    def __init__(self, type_: str):
        self.id = new_id("task")
        self.type = type_
        self.status = "pending"
        self.progress = 0.0
        self.stage = ""
        self.logs: deque[str] = deque(maxlen=200)
        self.result: dict | None = None
        self.error: str | None = None
        # 终态细分（目前只有建图超预算置为 "budget_exceeded"）。status 仍是标准四态，
        # 不新增状态值：前端 api.ts 只认四个终态值，多一个会让 SSE 订阅等不到终态。
        self.stop_reason: str | None = None
        # LLM 用量快照（Step 2 生成任务才会填），由 log() 从 extra 中回写
        self.usage: dict = {}
        self.cancel_event = asyncio.Event()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.subscribers: list[asyncio.Queue] = []
        # 真正持有 asyncio.Task 句柄，cancel() 才能中断 await
        self._async_task: asyncio.Task | None = None
        self.resources: tuple[str, ...] = ()
        self.seq = 0
        self.events: deque = deque(maxlen=500)
        self.finished_at: float | None = None
        self.worker_futures: list = []
        self.policy: dict = {}
        self._manager = None
        self._foreign = False

    def view(self) -> dict:
        # 出口统一清洗：存量任务里可能存着 ragas 的 NaN 哨兵值，直接交给 FastAPI 会整张列表 500。
        return sanitize_nonfinite({
            "task_id": self.id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "logs": list(self.logs)[-50:],
            "result": self.result,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "usage": self.usage,
            # Step 1 的断点 run_id 随任务透出：SSE 订阅晚于 init 事件时，前端仍能从快照里拿到，
            # 取消时才能提供「取消并删除断点」（默认 None，其他任务类型不受影响）。
            "run_id": getattr(self, "run_id", None),
            "resources": list(self.resources), "seq": self.seq,
            "workers_pending": sum(not f.done() for f in self.worker_futures),
            "policy": self.policy,
        })


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self._guard = threading.RLock()
        self._journal: Path | None = None
        self._owner = uuid.uuid4().hex
        self._started = current_identity()["started"]

    def initialize(self, path: Path) -> None:
        """由lifespan持单实例锁后调用；重启不自动重发任何付费任务。"""
        self._journal = path
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS resource_leases (resource TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner TEXT NOT NULL, pid INTEGER NOT NULL)")
            # S1：租约记录持有进程的创建时间，PID 被复用时不再误判为仍被占用（旧库就地加列）
            columns = {row[1] for row in db.execute("PRAGMA table_info(resource_leases)")}
            if "started" not in columns:
                db.execute("ALTER TABLE resource_leases ADD COLUMN started INTEGER")
            rows = db.execute("SELECT body FROM tasks ORDER BY updated DESC LIMIT 500").fetchall()
        for (raw,) in reversed(rows):
            data = json.loads(raw)
            task = self._restore(data)
            task._manager = self
            owner = data.get("owner")
            task._foreign = bool(owner and owner != self._owner
                                 and identity_alive(data.get("owner_pid", 0), data.get("owner_started")))
            self.tasks[task.id] = task
            if task.status not in TERMINAL and not task._foreign:
                task.status = "interrupted"
                task.error = "服务已重启，任务中断；远端请求结果可能未知，请核对后重试"
                task.finished_at = time.time()
                self.publish(task, "done", task.view())

    def _restore(self, data: dict) -> Task:
        task = Task(data["type"])
        task.id = data["task_id"]
        for key in ("status", "progress", "stage", "result", "error", "stop_reason", "created_at",
                    "usage", "seq", "finished_at", "policy", "run_id"):
            if key in data:
                setattr(task, key, data[key])
        task.resources = tuple(data.get("resources") or [])
        task.logs.extend(data.get("logs") or [])
        # 存量任务可能带着旧版本未清洗就落盘的 NaN 哨兵值，读入内存时就洗掉。
        task.result = sanitize_nonfinite(task.result)
        task.usage = sanitize_nonfinite(task.usage) or {}
        task.policy = sanitize_nonfinite(task.policy) or {}
        for event, payload in data.get("events") or []:
            task.events.append((event, sanitize_nonfinite(payload)))
        return task

    def _save(self, task: Task) -> None:
        if self._journal and not task._foreign:
            payload = self._payload(task)
            with sqlite3.connect(self._journal, timeout=10) as db:
                db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)",
                           (task.id, json.dumps(payload, ensure_ascii=False), time.time()))

    def _payload(self, task: Task) -> dict:
        return task.view() | {"events": list(task.events), "finished_at": task.finished_at,
                              "owner": self._owner, "owner_pid": os.getpid(), "owner_started": self._started}

    def _release_resources(self, task: Task) -> None:
        if task._foreign or task.status not in TERMINAL or any(not f.done() for f in task.worker_futures):
            return
        if self._journal:
            with sqlite3.connect(self._journal, timeout=10) as db:
                db.execute("DELETE FROM resource_leases WHERE task_id=? AND owner=?", (task.id, self._owner))

    def worker_finished(self, task: Task) -> None:
        # 在事件循环线程执行；取消后的迟到 usage 也写入终态，不能伪装成线程已停。
        telemetry = getattr(task, "generation_telemetry", None)
        if telemetry is not None:
            task.usage.update(telemetry.snapshot())
        self._save(task)
        self._release_resources(task)

    async def shutdown(self, timeout: float = 30.0) -> None:
        """优雅关闭：取消本进程所有在跑任务，等它们走完「先落盘再结束」的收尾。

        Step 2 被取消时会先保存已完成样本与账单；强杀进程则全部丢失。超时后照常退出，不无限等待。
        """
        with self._guard:
            running = [t for t in self.tasks.values()
                       if not t._foreign and t.status in ("pending", "running", "cancel_requested")]
        for task in running:
            self.cancel(task.id)
        pending = [t._async_task for t in running if t._async_task is not None and not t._async_task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def busy_resources(self) -> set[str]:
        """本进程仍在运行或线程仍在收尾的任务所占的资源键（口径与 create() 的冲突判定一致）。"""
        with self._guard:
            return {r for t in self.tasks.values()
                    if not t._foreign and (t.status not in TERMINAL or any(not f.done() for f in t.worker_futures))
                    for r in t.resources}

    def create(self, type_: str, resources: tuple[str, ...] = ()) -> Task:
        with self._guard:
            # 只回收内存；磁盘终态仍可按ID查询。
            for key, old in list(self.tasks.items()):
                if (old.finished_at and time.time() - old.finished_at > 86400 and not old.subscribers
                        and all(f.done() for f in old.worker_futures)):
                    self.tasks.pop(key)
            for old in self.tasks.values():
                if (not old._foreign and (old.status not in TERMINAL or any(not f.done() for f in old.worker_futures))
                        and set(resources) & set(old.resources)):
                    raise ResourceBusyError(f"资源已有任务 {old.id} 正在执行或收尾，请等待完成")
            t = Task(type_)
            t.resources = resources
            t._manager = self
            if self._journal:
                with sqlite3.connect(self._journal, timeout=10) as db:
                    db.execute("BEGIN IMMEDIATE")
                    for resource in set(resources):
                        lease = db.execute("SELECT task_id, owner, pid, started FROM resource_leases WHERE resource=?",
                                           (resource,)).fetchone()
                        if lease and lease[1] != self._owner and identity_alive(lease[2], lease[3]):
                            raise ResourceBusyError(f"资源已有任务 {lease[0]} 正在执行或收尾，请等待完成")
                        if lease:
                            db.execute("DELETE FROM resource_leases WHERE resource=?", (resource,))
                        db.execute("INSERT INTO resource_leases (resource, task_id, owner, pid, started) VALUES (?, ?, ?, ?, ?)",
                                   (resource, t.id, self._owner, os.getpid(), self._started))
                    db.execute("INSERT INTO tasks VALUES (?, ?, ?)",
                               (t.id, json.dumps(self._payload(t), ensure_ascii=False), time.time()))
            self.tasks[t.id] = t
            return t

    def get(self, task_id: str) -> Task | None:
        with self._guard:
            task = self.tasks.get(task_id)
            if (task and not task._foreign) or not self._journal:
                return task
            with sqlite3.connect(self._journal) as db:
                row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row:
                task = self._restore(json.loads(row[0]))
                task._foreign = True
                self.tasks[task.id] = task
            return task

    def all(self) -> list[dict]:
        with self._guard:
            return [t.view() for t in reversed(self.tasks.values())]

    # ---- 事件发布 / SSE 订阅 ----

    def publish(self, task: Task, event: str, data: dict) -> None:
        # SSE 用 json.dumps 直接拼 data；NaN 会写成非法字面量让前端 JSON.parse 失败。
        payload = sanitize_nonfinite(dict(data))
        payload.setdefault("status", task.status)
        payload.setdefault("progress", task.progress)
        payload.setdefault("stage", task.stage)
        task.seq += 1
        payload["seq"] = task.seq
        task.events.append((event, payload))
        self._save(task)  # 先提交权威状态，再通知订阅者
        for q in task.subscribers:
            try:
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                # 丢最旧通知；终态不丢，重连可由seq重放/快照收敛。
                q.get_nowait()
                q.put_nowait((event, payload))

    def log(self, task: Task, msg: str, event: str = "progress", extra: dict | None = None) -> None:
        task.logs.append(msg)
        data = {"log": msg}
        if extra:
            data.update(extra)
            # 把进度/阶段回写到任务对象本身，否则非 SSE 的轮询接口
            # （GET /api/tasks、/api/tasks/{id}）永远显示 progress=0、stage=""
            if isinstance(extra.get("progress"), (int, float)):
                task.progress = float(extra["progress"])
            if extra.get("stage"):
                task.stage = str(extra["stage"])
            # 生成类任务的用量快照，供轮询接口与前端刷新后恢复展示
            for k in ("llm_calls", "llm_calls_failed", "llm_retries", "llm_fix_calls",
                      "input_tokens", "output_tokens", "total_tokens", "embedding_tokens", "embedding_calls",
                      "embedding_calls_failed", "usage_missing_calls", "usage_complete", "budget_scope", "llm_total_tokens",
                      "usage_unknown_calls", "est_unreported_input_tokens"):
                if k in extra:
                    task.usage[k] = extra[k]
        self.publish(task, event, data)

    async def subscribe(self, task: Task, after: int = 0) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        if task.status in TERMINAL:
            q.put_nowait(("done", task.view()))
        elif after and task.events and task.events[0][1]["seq"] <= after + 1 <= task.seq + 1:
            for event, payload in task.events:
                if payload["seq"] > after:
                    q.put_nowait((event, payload))
        else:
            q.put_nowait(("progress", task.view() | {"log": f"[当前状态] {task.status} {task.stage}"}))
        task.subscribers.append(q)
        return q

    def unsubscribe(self, task: Task, q: asyncio.Queue) -> None:
        if q in task.subscribers:
            task.subscribers.remove(q)

    # ---- 执行 ----

    def complete(self, task: Task, status: str = "succeeded", error: str | None = None) -> None:
        """结束一个**同步**执行、不经 run() 的短任务：写终态并释放资源。

        用于只需要占用资源键、在请求线程里直接做完的动作（如重新绑定数据源）：先 create() 占锁，
        与同一数据源的同步 / 出题 / 探测任务互斥，做完调本方法放锁。
        """
        if status not in TERMINAL:
            raise ValueError(f"非终态：{status}")
        task.status = status
        task.error = error
        if status == "succeeded":
            task.progress = 1.0
        task.finished_at = time.time()
        self._save(task)
        self._release_resources(task)

    def run(self, task: Task, coro) -> None:
        # 在 run() 之前就被 cancel()：直接丢弃协程，不启动
        if task.status == "canceled":
            coro.close()
            return

        async def _wrap():
            try:
                if task.cancel_event.is_set():
                    coro.close()
                    raise asyncio.CancelledError
                task.status = "running"
                self._save(task)
                result = await coro
                # 取消信号在协程正常结束后才到达：丢弃结果
                task.result = sanitize_nonfinite(result)  # 入口清洗：新任务不再把 NaN 写进内存与日记
                task.status = "canceled" if task.cancel_event.is_set() else "succeeded"
                task.progress = 1.0
            except asyncio.CancelledError:
                task.status = "canceled"
            except Exception as e:  # noqa: BLE001
                log.exception("task %s failed", task.id)
                task.error = str(e)
                task.status = "failed"
            finally:
                telemetry = getattr(task, "generation_telemetry", None)
                if telemetry is not None:
                    task.usage.update(telemetry.snapshot())
                task.finished_at = time.time()
                self.publish(task, "done", task.view())
                self._release_resources(task)

        # 保存句柄：cancel() 才能真正中断 await 中的协程（含 to_thread 阻塞）
        task._async_task = asyncio.create_task(_wrap())

    def cancel(self, task_id: str) -> bool:
        t = self.get(task_id)
        if not t or t._foreign or t.status not in ("pending", "running"):
            return False
        was_running = t.status == "running"
        t.status = "cancel_requested"
        t.cancel_event.set()
        # 真正中断在跑的协程：await asyncio.to_thread(...) 会立即抛 CancelledError
        # （底层线程会跑完当前 transform，但结果被丢弃，后续步骤不再执行）
        if was_running and t._async_task is not None and not t._async_task.done():
            t._async_task.cancel()
        # 终态必须等协程落盘/清理；在途线程另占资源，不能提前启动重叠任务。
        self.publish(t, "progress", {"status": "cancel_requested", "log": "已请求取消，等待在途调用收尾"})
        return True


async def worker_call(task: Task, fn, *args, **kwargs):
    """取消等待不取消真实线程；保留future，让资源锁持续到线程实际结束。"""
    future = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    task.worker_futures.append(future)
    # 取消后无人await的线程异常也必须被消费。
    def finished(f):
        if not f.cancelled():
            f.exception()
        if task._manager:
            task._manager.worker_finished(task)
    future.add_done_callback(finished)
    return await asyncio.shield(future)


manager = TaskManager()
