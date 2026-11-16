"""统一阶段化任务骨架（架构演进阶段 2 的基座）。

**已解冻（2026-11-13，PR8 合并后）**：PR5 起的「阶段 2 内公开名字不变」冻结到此为止。
理由：PR6（建图）、PR7（出题与增强）、PR8（RAGFlow 同步）三个消费方都已按原接口接入，
期间没有为迁就某个任务而改骨架，冻结的目的（防止边迁边改）已经达成；再冻下去，
下一个改动（PR9 的心跳日志）只能在消费侧绕开骨架另写一套，反而违背「一份骨架」的初衷。
解冻后的改动范围（只允许这些，超出仍按新 PR 评审）：

- **允许**：心跳 / 阶段耗时 / 进度上报这类**横切能力**加在骨架内（PR9 心跳日志落地处是
  ``JobExecutor`` 与 ``StagedJob._run_stage`` / ``drain``：长阶段跑在
  ``asyncio.to_thread`` 或 ragas 工作线程里，只有骨架同时看得到阶段边界与工作线程存活）；
  可新增可选参数与新的公开名字，但**不得改变现有名字的既有语义**。
  PR9 已落地的心跳：``StagedJob._drain_loop`` 在「连续 ``HEARTBEAT_INTERVAL_SECONDS`` 秒
  没有任何日志或进度事件」时发一条 ``heartbeat`` 进度事件（``manager.publish``，与普通事件
  共用 ``seq``），payload 为 ``{heartbeat: true, stage, progress, stage_elapsed_s, done, total}``；
  中文文案由前端按阶段名映射表渲染，骨架不维护译名。
- **不允许**：把终态语义（四种终态、一律先落盘再结束）、线程铁律（工作线程只写缓冲、
  主协程 ``drain()`` 落日志）、资源租约与取消链路改成依赖具体任务的实现。
- **必须**：改动同步更新本 docstring 的表格与 ``交接文档`` 阶段 2 段落，
  并保证 ``backend/tests/test_staged_job.py`` 等骨架回归随 CI 跑绿。

原冻结口径（历史记录，供查证）：PR6/PR7/PR8 只**消费**本模块，不改它；
``manager.py`` 的 ``Task.stop_reason`` 字段（建图超预算在任务级可见）是当时唯一例外。
各名字的用途与使用方：

| 名字 | 用途 | 使用方 |
| --- | --- | --- |
| ``Stage`` | 一个阶段：``name`` / ``run(ctx)`` / 可选 ``checkpoint`` / ``resume`` | 评估、PR6、PR7、PR8 |
| ``JobContext`` | 取消令牌（``task.cancel_event``）、``policy``、``ledger``、进度计数、日志缓冲 | 评估、PR6、PR7、PR8 |
| ``UsageLedger`` | 分阶段账单；所有终态由 ``StagedJob`` 统一落盘 | 评估、PR6、PR7、PR8 |
| ``JobExecutor`` | 框架持有的并发执行器（可取消、可 close 未启动协程） | PR7、PR9（心跳） |
| ``StagedJob`` | 驱动阶段、四种终态（``completed`` / ``canceled`` / ``budget_exceeded`` / ``failed``）、先落盘再结束 | 评估、PR6、PR7、PR8、PR9 |
| ``ctx.attach_executor()`` | 把外部执行器（如 ragas ``Executor``）交给框架持有，取消时由框架收尾 | PR7 |
| ``NO_RESUME`` | ``Stage.resume`` 的哨兵：没有可用断点，照常执行本阶段 | PR6 |
| ``ctx.progress_state()`` | 当前阶段 / 进度 / 阶段耗时的只读快照（心跳与后续进度上报用） | PR9 |
| ``HEARTBEAT_INTERVAL_SECONDS`` | 心跳间隔常量（默认 15 秒；``StagedJob(heartbeat_interval=)`` 可覆盖，≤0 关闭） | PR9 |

线程铁律（与 ``GenerationTelemetry`` / ``_TqdmProgressCapture`` 同一套路）
--------------------------------------------------------------------
阶段可能跑在 ragas 工作线程里，而 ``manager.log`` 最终 ``put_nowait`` 到 asyncio 队列——
跨线程操作事件循环队列不安全。所以 ``JobContext.log()`` / ``advance()`` **只加锁写缓冲**，
由 ``StagedJob`` 在主协程里 ``drain()`` 后再落日志/进度。工作线程不得直接调 ``manager.log``。

终态语义（沿用 ``设计文档``）
----------------------------------------------------------
``completed`` / ``canceled`` / ``budget_exceeded`` / ``failed`` **一律先落盘再结束**：
``StagedJob`` 在返回或抛出前先 await ``persist(ctx)``，由各任务把 ``ctx.stop_reason``、
``ctx.error``、``ctx.ledger.snapshot()`` 与自己的产物写进落盘文件。
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..policies import GenerationPolicy
from .manager import Task, manager, worker_call

STOP_COMPLETED = "completed"
STOP_CANCELED = "canceled"
STOP_BUDGET_EXCEEDED = "budget_exceeded"
STOP_FAILED = "failed"
STOP_REASONS = (STOP_COMPLETED, STOP_CANCELED, STOP_BUDGET_EXCEEDED, STOP_FAILED)

# Stage.resume 的哨兵：没有可用断点。除它之外的任何返回值（含 None）都视为阶段结果。
NO_RESUME = object()

# 心跳间隔：连续这么久没有日志或进度事件时补一条「仍在进行」事件。
# 只在这里定义一次；StagedJob(heartbeat_interval=) 可覆盖（测试用小值），≤0 关闭。
HEARTBEAT_INTERVAL_SECONDS = 15.0

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "NO_RESUME",
    "STOP_BUDGET_EXCEEDED",
    "STOP_CANCELED",
    "STOP_COMPLETED",
    "STOP_FAILED",
    "STOP_REASONS",
    "JobContext",
    "JobExecutor",
    "Stage",
    "StagedJob",
    "UsageLedger",
]


@dataclass(frozen=True)
class Stage:
    """一个可执行阶段。

    ``run(ctx)``：同步函数会在工作线程执行（``worker_call``，资源租约随线程持到真正结束）；
    协程函数在主协程执行。返回值作为 ``ctx.result`` 传给下一阶段与 ``persist``。

    ``checkpoint(ctx, result)``：阶段成功后的断点写入（同步或协程），失败/取消不写。
    ``resume(ctx)``：续跑钩子；返回 ``NO_RESUME`` 表示没有断点、正常执行，
    返回其他值则跳过 ``run``，直接把该值当阶段结果。
    """

    name: str
    run: Callable[[JobContext], Any]
    checkpoint: Callable[[JobContext, Any], Any] | None = None
    resume: Callable[[JobContext], Any] | None = None


def _numeric_delta(before: dict, after: dict) -> dict:
    """两次累计快照的数值差（布尔与非数值字段不参与分阶段账单）。"""
    delta = {}
    for key, value in after.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        base = before.get(key)
        base_value = base if isinstance(base, (int, float)) and not isinstance(base, bool) else 0
        delta[key] = value - base_value
    return delta


class UsageLedger:
    """分阶段账单：保留完整 usage 快照，另按阶段记录增量。

    ``update(snapshot)`` 接收**累计**快照（如 ``GenerationTelemetry.snapshot()``）；
    ``snapshot()`` 原样返回最近一次的累计快照，保证落盘 ``usage`` 键与既有格式一致；
    ``by_stage()`` 给出各阶段的 token 增量，用于日志与后续 PR 的分项展示。
    """

    def __init__(self, stage: str = "init") -> None:
        self._lock = threading.Lock()
        self._stage = stage or "init"
        self._snapshot: dict = {}
        self._baseline: dict = {}
        self._by_stage: dict[str, dict] = {}

    def begin_stage(self, name: str) -> None:
        """切换阶段：把上一阶段从基线到当前快照的增量结算掉，并把当前快照设为新基线。"""
        with self._lock:
            if self._snapshot:
                self._by_stage[self._stage] = _numeric_delta(self._baseline, self._snapshot)
            self._stage = name or self._stage
            self._baseline = dict(self._snapshot)

    def update(self, snapshot: dict | None) -> None:
        if not snapshot:
            return
        with self._lock:
            self._snapshot = dict(snapshot)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def by_stage(self) -> dict[str, dict]:
        with self._lock:
            result = {name: dict(value) for name, value in self._by_stage.items()}
            if self._snapshot:
                result[self._stage] = _numeric_delta(self._baseline, self._snapshot)
            return result

    @property
    def total_tokens(self) -> int:
        with self._lock:
            value = self._snapshot.get("total_tokens")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            return int(self._snapshot.get("input_tokens") or 0) + int(self._snapshot.get("output_tokens") or 0)


class JobContext:
    """阶段上下文：取消令牌、策略、账单、进度计数与线程安全日志缓冲。"""

    def __init__(self, task: Task, policy: GenerationPolicy, *,
                 ledger: UsageLedger | None = None, stage_count: int = 1, job_name: str = "") -> None:
        self.task = task
        self.policy = policy
        self.ledger = ledger or UsageLedger()
        self.job_name = job_name
        self.result: Any = None
        self.stop_reason: str | None = None
        self.error: str | None = None
        # 框架持有的执行器：JobExecutor（内部）或 ragas Executor 之类的外部执行器
        self.executor: JobExecutor | None = None
        self._external_executor: Any = None
        self._telemetry: Any = None
        self._lock = threading.Lock()
        self._messages: list[tuple[str, dict]] = []
        self._stage_count = max(1, int(stage_count))
        self._stage_index = 0
        self._stage = ""
        self._done = 0
        self._total = 0
        self._last_progress = -1.0
        self._stage_started = time.monotonic()

    # ---- 只读状态 ----

    @property
    def cancel_event(self) -> asyncio.Event:
        """统一取消令牌：只认 ``task.cancel_event``。"""
        return self.task.cancel_event

    @property
    def cancelled(self) -> bool:
        return self.task.cancel_event.is_set()

    @property
    def budget_exceeded(self) -> bool:
        if self._telemetry is not None and bool(getattr(self._telemetry, "budget_exceeded", False)):
            return True
        return bool(self.ledger.snapshot().get("budget_exceeded"))

    # ---- 绑定 ----

    def bind_telemetry(self, telemetry: Any) -> None:
        """绑定 LLM 遥测：预算熔断即时回调 + 终态账单来源。"""
        self._telemetry = telemetry
        if telemetry is not None and getattr(telemetry, "_on_budget_exceeded", None) is None:
            # 回调在 ragas 工作线程触发：这里只置停止原因，不碰事件循环
            telemetry._on_budget_exceeded = lambda: self.request_stop(STOP_BUDGET_EXCEEDED)

    def attach_executor(self, executor: Any) -> None:
        """把外部执行器（如 ragas ``Executor``）交给框架持有；取消/终态由框架调用 cancel/close。"""
        self._external_executor = executor

    def close_executor(self) -> None:
        """取消并收尾所有执行器。可重复调用；任何异常只吞掉，不能盖住终态落盘。"""
        for executor in (self.executor, self._external_executor):
            if executor is None:
                continue
            for method in ("cancel", "close"):
                callback = getattr(executor, method, None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:  # noqa: BLE001 - 收尾失败不改变终态
                        pass
        self.executor = None
        self._external_executor = None

    # ---- 停止与终态 ----

    def request_stop(self, reason: str) -> None:
        """可从任意线程调用；只认第一次停止原因（用户取消优先于预算熔断由调用方决定）。"""
        if reason not in (STOP_CANCELED, STOP_BUDGET_EXCEEDED):
            raise ValueError(f"不是停止原因：{reason}")
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason

    def finish(self, reason: str) -> None:
        """由 StagedJob 在终态调用：写最终原因并刷新账单。"""
        if reason not in STOP_REASONS:
            raise ValueError(f"未知终态：{reason}")
        with self._lock:
            self.stop_reason = reason
        self.refresh_usage()

    def refresh_usage(self) -> None:
        """把遥测的最新累计快照并入账单（主协程调用）。"""
        if self._telemetry is not None:
            self.ledger.update(self._telemetry.snapshot())

    # ---- 线程安全缓冲 ----

    def log(self, msg: str, **extra: Any) -> None:
        """任意线程可调：只写缓冲，由主协程 drain 后落日志。"""
        with self._lock:
            self._messages.append((str(msg), dict(extra)))

    def set_stage(self, name: str, total: int | None = None) -> None:
        """阶段内切换子阶段标签；``total`` 给出时重置完成计数。"""
        with self._lock:
            self._stage = str(name)
            self._stage_started = time.monotonic()
            if total is not None:
                self._total = max(0, int(total))
                self._done = 0

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = max(0, int(total))

    def advance(self, n: int = 1) -> None:
        """完成数 +n（PR7 的样本/场景计数用它，不照抄 ragas 两个 tqdm 的 total）。"""
        with self._lock:
            self._done += max(0, int(n))

    def progress_value(self) -> float:
        with self._lock:
            return self._progress_locked()

    def progress_state(self) -> dict:
        """当前阶段 / 进度 / 阶段耗时 / 完成计数的只读快照（心跳用，任意线程可读）。"""
        with self._lock:
            return {
                "stage": self._stage,
                "progress": self._progress_locked(),
                "stage_elapsed_s": int(max(0.0, time.monotonic() - self._stage_started)),
                "done": self._done,
                "total": self._total,
            }

    def _begin_stage(self, name: str, index: int) -> None:
        self.ledger.begin_stage(name)
        with self._lock:
            self._stage = str(name)
            self._stage_index = max(0, int(index))
            self._stage_started = time.monotonic()
            self._done = 0
            self._total = 0

    def drain(self) -> list[tuple[str | None, dict]]:
        """主协程调用：取走日志缓冲，并在有进展时补一条进度事件（``msg=None``）。"""
        self.refresh_usage()
        usage = self.ledger.snapshot()
        with self._lock:
            messages, self._messages = self._messages, []
            progress = self._progress_locked()
            stage = self._stage
            changed = progress > self._last_progress + 1e-9
            self._last_progress = progress
        base = {"stage": stage, "progress": progress, **usage}
        events: list[tuple[str | None, dict]] = [(msg, {**base, **extra}) for msg, extra in messages]
        if not events and changed:
            events.append((None, base))
        return events

    def _progress_locked(self) -> float:
        fraction = (self._done / self._total) if self._total else 0.0
        value = (self._stage_index + min(max(fraction, 0.0), 1.0)) / self._stage_count
        return round(min(max(value, 0.0), 0.99), 4)


class JobExecutor:
    """框架持有的并发执行器。

    PR7 把 ragas 的每个 job 包成零参协程工厂交给 ``run()``：按 ``max_workers`` 放行，
    每完成一个回调 ``on_complete(completed, total, value)``（可在工作线程里只写缓冲），
    ``cancel()`` 可从任意线程调用——停发排队 job、中断在途协程，并 ``close()`` 从未 await 的协程
    （避免 ``RuntimeWarning: coroutine ... was never awaited``）。
    """

    def __init__(self, max_workers: int = 8,
                 on_complete: Callable[[int, int, Any], None] | None = None,
                 raise_exceptions: bool = False) -> None:
        self.max_workers = max(1, int(max_workers))
        self.on_complete = on_complete
        self.raise_exceptions = raise_exceptions
        self.cancelled = False
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []

    async def run(self, jobs: list[Callable[[], Awaitable[Any]]]) -> list[Any]:
        total = len(jobs)
        self._loop = asyncio.get_running_loop()
        self._main = asyncio.current_task()
        coroutines = [job() for job in jobs]
        if self._stop.is_set():
            for coro in coroutines:
                coro.close()
            self._main = None
            self._loop = None
            return []
        started = [False] * total
        semaphore = asyncio.Semaphore(self.max_workers)

        async def _one(index: int, coro: Awaitable[Any]) -> Any:
            async with semaphore:
                started[index] = True
                return await coro

        self._tasks = [asyncio.create_task(_one(i, coro)) for i, coro in enumerate(coroutines)]
        results: list[Any] = []
        completed = 0
        try:
            iterator = asyncio.as_completed(self._tasks)
            # 取消检查必须在「取下一个 future」之前：as_completed 的 plain iterator 每次 next()
            # 都会新建一个 _wait_for_one 协程，先 next 再 break 会留下未 await 的协程。
            while not self._stop.is_set():
                try:
                    future = next(iterator)
                except StopIteration:
                    break
                try:
                    value = await future
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - raise_exceptions=False 时逐条记录
                    self.errors.append(exc)
                    if self.raise_exceptions:
                        raise
                    continue
                results.append(value)
                completed += 1
                if self.on_complete is not None:
                    try:
                        self.on_complete(completed, total, value)
                    except Exception:  # noqa: BLE001 - 进度回调不能拖垮任务
                        pass
        finally:
            self._stop.set()
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            for index, coro in enumerate(coroutines):
                if not started[index]:
                    coro.close()
            # run 结束后清掉调用方引用：再调 cancel() 只置标志，不能给已完成的任务补发取消
            self._tasks = []
            self._main = None
            self._loop = None
        return results

    def cancel(self) -> None:
        """任意线程可调：停发排队 job，并中断在途协程。"""
        self.cancelled = True
        self._stop.set()
        loop, main = self._loop, self._main
        if loop is not None and main is not None:
            try:
                loop.call_soon_threadsafe(main.cancel)
            except RuntimeError:  # 事件循环已关闭
                pass

    async def aclose(self) -> None:
        self.cancel()
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class StagedJob:
    """按顺序驱动 ``Stage``，统一处理取消 / 预算 / 失败，并在所有终态**先落盘再结束**。"""

    def __init__(self, task: Task, stages: list[Stage], *, policy: GenerationPolicy,
                 persist: Callable[[JobContext], Any], ledger: UsageLedger | None = None,
                 name: str = "", heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
        self.task = task
        self.stages = list(stages)
        self.policy = policy
        self.persist = persist
        self.name = name
        self.heartbeat_interval = float(heartbeat_interval)
        self.ctx = JobContext(task, policy, ledger=ledger, stage_count=len(self.stages), job_name=name)
        self._last_activity = time.monotonic()
        self._heartbeat_enabled = True

    async def run(self) -> Any:
        ctx = self.ctx
        self._last_activity = time.monotonic()
        drainer = asyncio.create_task(self._drain_loop())
        reason = STOP_COMPLETED
        error: BaseException | None = None
        try:
            for index, stage in enumerate(self.stages):
                if ctx.cancelled or ctx.stop_reason == STOP_CANCELED:
                    raise asyncio.CancelledError
                if ctx.stop_reason == STOP_BUDGET_EXCEEDED or ctx.budget_exceeded:
                    reason = STOP_BUDGET_EXCEEDED
                    break
                ctx._begin_stage(stage.name, index)
                self._drain()
                if stage.resume is not None:
                    resumed = await self._call(stage.resume, ctx)
                    if resumed is not NO_RESUME:
                        ctx.result = resumed
                        ctx.log(f"↺ 命中断点，跳过阶段「{stage.name}」")
                        continue
                ctx.log(f"▶ 阶段「{stage.name}」开始")
                value = await self._run_stage(stage)
                ctx.result = value
                if stage.checkpoint is not None:
                    await self._call(stage.checkpoint, ctx, value)
                ctx.advance()
                ctx.log(f"✓ 阶段「{stage.name}」完成")
                if ctx.budget_exceeded:
                    reason = STOP_BUDGET_EXCEEDED
                    break
        except asyncio.CancelledError:
            reason = STOP_CANCELED
        except Exception as exc:  # noqa: BLE001 - 统一转终态，先落盘再抛
            if ctx.budget_exceeded:
                # 预算熔断会让遥测拒绝新调用并抛出 RuntimeError：这是正常终态，不按失败处理
                reason = STOP_BUDGET_EXCEEDED
                ctx.error = str(exc)
            else:
                reason = STOP_FAILED
                ctx.error = str(exc) or type(exc).__name__
                error = exc
        finally:
            # 进入收尾后停心跳：终态落盘阶段不能再补「仍在进行」事件
            self._heartbeat_enabled = False
            ctx.finish(reason)
            ctx.close_executor()
            self._drain()
            # 先落盘，再做 drainer 收尾：避免收尾阶段的取消影响终态写入
            try:
                await self._call(self.persist, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 落盘失败本身就是失败终态
                manager.log(self.task, f"✗ 终态落盘失败：{exc}",
                            extra={"level": "error", "stage": ctx.stop_reason or ""})
                if error is None and reason != STOP_CANCELED:
                    reason = STOP_FAILED
                    ctx.stop_reason = STOP_FAILED
                    ctx.error = f"终态落盘失败：{exc}"
                    error = exc
            finally:
                drainer.cancel()
                # 只吸收 drainer 自己的取消；本任务收到的外部取消由 gather 继续抛出
                await asyncio.gather(drainer, return_exceptions=True)
        if reason == STOP_CANCELED:
            raise asyncio.CancelledError
        if reason == STOP_FAILED:
            raise error if error is not None else RuntimeError(ctx.error or "任务失败")
        return ctx.result

    async def _run_stage(self, stage: Stage) -> Any:
        if inspect.iscoroutinefunction(stage.run):
            return await stage.run(self.ctx)
        # 同步阶段在工作线程执行：登记 worker future，资源租约一直持到线程真正结束
        return await worker_call(self.task, stage.run, self.ctx)

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        return await asyncio.to_thread(fn, *args)

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            emitted = self._drain()
            now = time.monotonic()
            if emitted:
                self._last_activity = now
            elif (self._heartbeat_enabled and self.heartbeat_interval > 0
                  and now - self._last_activity >= self.heartbeat_interval):
                # 长阶段跑在 to_thread / ragas 工作线程里，期间没有任何日志或进度：
                # 补一条心跳，避免前端看起来像卡死。中文文案由前端按阶段映射表渲染。
                self._emit_heartbeat()
                self._last_activity = now

    def _emit_heartbeat(self) -> None:
        # 只发事件，不改 task.progress / task.stage：骨架之外的日志可能已推送更大的进度，
        # 覆盖会让断线重连读到的快照进度回跳。
        state = self.ctx.progress_state()
        manager.publish(self.task, "progress", {**state, "heartbeat": True})

    def _drain(self) -> bool:
        """主协程专用：把上下文缓冲转发到 manager；返回本次是否发了事件。"""
        events = self.ctx.drain()
        for msg, extra in events:
            if msg is None:
                # 纯进度：不写日志行，只更新任务字段并推送一次性进度事件
                self.task.progress = float(extra.get("progress") or 0.0)
                self.task.stage = str(extra.get("stage") or "")
                manager.publish(self.task, "progress", dict(extra))
            else:
                manager.log(self.task, msg, extra=extra)
        return bool(events)
