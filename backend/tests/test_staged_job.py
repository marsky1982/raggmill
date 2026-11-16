"""统一阶段化任务骨架：终态落盘、取消、预算熔断、线程安全日志与执行器。

这些用例覆盖 ``tasks/staged.py`` 的冻结 API——PR6/PR7/PR8 只消费它，
骨架一旦破坏，这些断言应当先红。

注意：conftest 要求测试模块不得在顶层 import ``app``，否则 DATA_DIR 会在
会话夹具设置临时目录之前被固定。所有 app 导入都放在用例内部。
"""
import asyncio
import threading
import time

import pytest


class FakeTelemetry:
    def __init__(self, snapshot=None):
        self.budget_exceeded = False
        self._on_budget_exceeded = None
        self._snapshot = snapshot or {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0}

    def snapshot(self):
        return dict(self._snapshot)


def test_usage_ledger_keeps_full_snapshot_and_stage_deltas():
    from app.tasks.staged import UsageLedger

    ledger = UsageLedger()
    ledger.update({"total_tokens": 10, "input_tokens": 6, "budget_exceeded": False})
    ledger.begin_stage("a")
    ledger.update({"total_tokens": 25, "input_tokens": 15, "budget_exceeded": False})
    ledger.begin_stage("b")
    ledger.update({"total_tokens": 40, "input_tokens": 20, "budget_exceeded": False})

    assert ledger.snapshot()["total_tokens"] == 40
    assert ledger.total_tokens == 40
    by_stage = ledger.by_stage()
    assert by_stage["a"]["total_tokens"] == 15
    assert by_stage["b"]["total_tokens"] == 15


def test_completed_job_persists_before_returning():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_COMPLETED, Stage, StagedJob

    events = []

    async def scenario():
        task = Task("staged_test")

        async def first(ctx):
            events.append("stage:first")
            return {"value": 1}

        def second(ctx):
            events.append("stage:second")
            return {"value": 2}

        def persist(ctx):
            events.append(f"persist:{ctx.stop_reason}")

        job = StagedJob(task, [Stage("first", first), Stage("second", second)],
                        policy=GenerationPolicy(), persist=persist)
        result = await job.run()
        events.append("returned")
        return result, job.ctx

    result, ctx = asyncio.run(scenario())
    assert result == {"value": 2}
    assert ctx.stop_reason == STOP_COMPLETED
    assert events.index(f"persist:{STOP_COMPLETED}") < events.index("returned")


def test_checkpoint_runs_after_stage_and_resume_skips_it():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    calls = []

    async def scenario():
        def run(ctx):
            calls.append("run")
            return "fresh"

        def checkpoint(ctx, value):
            calls.append(f"checkpoint:{value}")

        async def persist(ctx):
            calls.append(f"persist:{ctx.result}")

        first = StagedJob(Task("staged_test"), [Stage("build", run, checkpoint=checkpoint)],
                          policy=GenerationPolicy(), persist=persist)
        await first.run()

        def resume(ctx):
            calls.append("resume")
            return "saved"

        second = StagedJob(Task("staged_test"), [Stage("build", run, resume=resume)],
                           policy=GenerationPolicy(), persist=persist)
        await second.run()

    asyncio.run(scenario())
    assert calls == ["run", "checkpoint:fresh", "persist:fresh",
                     "resume", "persist:saved"]


def test_resume_sentinel_means_run_stage():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import NO_RESUME, Stage, StagedJob

    calls = []

    async def scenario():
        async def persist(ctx):
            calls.append(ctx.result)

        job = StagedJob(Task("staged_test"), [Stage("build", lambda ctx: "ran", resume=lambda ctx: NO_RESUME)],
                        policy=GenerationPolicy(), persist=persist)
        await job.run()

    asyncio.run(scenario())
    assert calls == ["ran"]


def test_canceled_job_persists_then_raises():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_CANCELED, Stage, StagedJob

    persisted = {}

    async def scenario():
        async def stage(ctx):
            raise asyncio.CancelledError

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(Task("staged_test"), [Stage("work", stage)],
                        policy=GenerationPolicy(), persist=persist)
        with pytest.raises(asyncio.CancelledError):
            await job.run()

    asyncio.run(scenario())
    assert persisted["reason"] == STOP_CANCELED


def test_budget_exceeded_at_stage_boundary_stops_before_next_stage():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_BUDGET_EXCEEDED, Stage, StagedJob

    ran = []
    persisted = {}

    async def scenario():
        task = Task("staged_test")
        tele = FakeTelemetry()

        async def first(ctx):
            ran.append("first")
            tele.budget_exceeded = True
            ctx.ledger.update({"total_tokens": 100, "budget_exceeded": True})
            return "partial"

        async def second(ctx):
            ran.append("second")
            return "never"

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(task, [Stage("first", first), Stage("second", second)],
                        policy=GenerationPolicy(), persist=persist)
        job.ctx.bind_telemetry(tele)
        return await job.run()

    assert asyncio.run(scenario()) == "partial"
    assert ran == ["first"]
    assert persisted["reason"] == STOP_BUDGET_EXCEEDED


def test_budget_exceeded_mid_stage_is_terminal_not_failure():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_BUDGET_EXCEEDED, Stage, StagedJob

    persisted = {}

    async def scenario():
        task = Task("staged_test")
        tele = FakeTelemetry()

        async def stage(ctx):
            tele.budget_exceeded = True
            raise RuntimeError("token 预算已用尽")

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason
            persisted["error"] = ctx.error

        job = StagedJob(task, [Stage("work", stage)], policy=GenerationPolicy(), persist=persist)
        job.ctx.bind_telemetry(tele)
        return await job.run()

    assert asyncio.run(scenario()) is None
    assert persisted["reason"] == STOP_BUDGET_EXCEEDED
    assert "预算" in persisted["error"]


def test_failed_job_persists_then_raises_original_error():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_FAILED, Stage, StagedJob

    persisted = {}

    async def scenario():
        def stage(ctx):
            raise ValueError("坏了")

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason
            persisted["error"] = ctx.error

        job = StagedJob(Task("staged_test"), [Stage("work", stage)],
                        policy=GenerationPolicy(), persist=persist)
        with pytest.raises(ValueError, match="坏了"):
            await job.run()

    asyncio.run(scenario())
    assert persisted == {"reason": STOP_FAILED, "error": "坏了"}


def test_worker_thread_logs_are_drained_on_main_thread(monkeypatch):
    from app.policies import GenerationPolicy
    from app.tasks import staged
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    calls = []
    worker_threads = []
    main_thread = threading.current_thread()

    def fake_log(task, msg, event="progress", extra=None):
        calls.append((threading.current_thread(), msg))

    monkeypatch.setattr(staged.manager, "log", fake_log)
    monkeypatch.setattr(staged.manager, "publish", lambda task, event, data: None)

    async def scenario():
        def stage(ctx):
            worker_threads.append(threading.current_thread())
            ctx.log("来自工作线程")
            ctx.advance()
            return "ok"

        def persist(ctx):
            return None

        job = StagedJob(Task("staged_test"), [Stage("worker", stage)],
                        policy=GenerationPolicy(), persist=persist)
        await job.run()

    asyncio.run(scenario())
    assert worker_threads and worker_threads[0] is not main_thread
    assert any(msg == "来自工作线程" for _, msg in calls)
    assert all(thread is main_thread for thread, _ in calls)


def test_job_executor_counts_completion():
    from app.tasks.staged import JobExecutor

    seen = []

    async def scenario():
        async def job(index):
            await asyncio.sleep(0.01)
            return index

        executor = JobExecutor(max_workers=2,
                               on_complete=lambda done, total, value: seen.append((done, total, value)))
        return await executor.run([lambda i=i: job(i) for i in range(4)])

    result = asyncio.run(scenario())
    assert sorted(result) == [0, 1, 2, 3]
    assert seen[-1][0] == 4 and seen[-1][1] == 4


def test_job_executor_cancel_stops_dispatch_and_closes_unstarted_coroutines():
    import gc
    import warnings

    from app.tasks.staged import JobExecutor

    started = []

    async def scenario():
        gate = asyncio.Event()

        async def first():
            started.append("first")
            await gate.wait()
            return 1

        async def second():
            started.append("second")
            return 2

        executor = JobExecutor(max_workers=1)
        runner = asyncio.create_task(executor.run([first, second]))
        await asyncio.sleep(0.05)
        executor.cancel()
        result = await asyncio.gather(runner, return_exceptions=True)
        await executor.aclose()
        return result

    # 只置标志的取消：as_completed 必须按「先判取消、再取下一个 future」推进，
    # 否则每次取消都会留下一个 never-awaited 的 _wait_for_one 协程（只看 GC 警告才发现）
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = asyncio.run(scenario())
        gc.collect()
    leaked = [str(w.message) for w in caught
              if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)]

    assert started == ["first"]
    assert isinstance(result[0], asyncio.CancelledError)
    assert not leaked, f"取消后留下 {len(leaked)} 个未 await 的协程，例如：{leaked[:3]}"


def test_job_executor_does_not_cancel_caller_after_completion():
    """正常完成后 close_executor 不能再给 StagedJob 自己的任务补发取消。"""
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import JobExecutor, Stage, StagedJob

    persisted = {}

    async def scenario():
        async def work(index):
            await asyncio.sleep(0)
            return index

        async def stage(ctx):
            executor = JobExecutor(max_workers=2)
            ctx.executor = executor
            return await executor.run([lambda i=i: work(i) for i in range(3)])

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(Task("staged_test"), [Stage("work", stage)],
                        policy=GenerationPolicy(), persist=persist)
        result = await job.run()
        return result, asyncio.current_task().cancelling()

    result, cancelling = asyncio.run(scenario())
    assert sorted(result) == [0, 1, 2]
    assert persisted["reason"] == "completed"
    assert cancelling == 0


def test_attach_executor_is_canceled_and_closed_on_terminal():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_CANCELED, Stage, StagedJob

    class ExternalExecutor:
        def __init__(self):
            self.calls = []

        def cancel(self):
            self.calls.append("cancel")

        def close(self):
            self.calls.append("close")

    persisted = {}

    async def scenario():
        external = ExternalExecutor()

        async def stage(ctx):
            ctx.attach_executor(external)
            raise asyncio.CancelledError

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(Task("staged_test"), [Stage("work", stage)],
                        policy=GenerationPolicy(), persist=persist)
        with pytest.raises(asyncio.CancelledError):
            await job.run()
        return external

    external = asyncio.run(scenario())
    assert external.calls == ["cancel", "close"]
    assert persisted["reason"] == STOP_CANCELED


def test_close_executor_swallows_executor_errors_and_still_persists():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    class BrokenExecutor:
        def cancel(self):
            raise RuntimeError("cancel 坏了")

        def close(self):
            raise RuntimeError("close 坏了")

    persisted = {}

    async def scenario():
        async def stage(ctx):
            ctx.attach_executor(BrokenExecutor())
            return "ok"

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(Task("staged_test"), [Stage("work", stage)],
                        policy=GenerationPolicy(), persist=persist)
        return await job.run()

    assert asyncio.run(scenario()) == "ok"
    assert persisted["reason"] == "completed"


def test_request_stop_from_worker_thread_ends_as_budget_at_next_boundary():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import STOP_BUDGET_EXCEEDED, Stage, StagedJob

    ran = []
    persisted = {}

    async def scenario():
        def first(ctx):
            ctx.request_stop(STOP_BUDGET_EXCEEDED)
            ran.append("first")

        async def second(ctx):
            ran.append("second")

        def persist(ctx):
            persisted["reason"] = ctx.stop_reason

        job = StagedJob(Task("staged_test"), [Stage("first", first), Stage("second", second)],
                        policy=GenerationPolicy(), persist=persist)
        return await job.run()

    asyncio.run(scenario())
    assert ran == ["first"]
    assert persisted["reason"] == STOP_BUDGET_EXCEEDED


def test_progress_counts_with_set_stage_and_set_total():
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import JobContext

    async def scenario():
        ctx = JobContext(Task("staged_test"), GenerationPolicy(), stage_count=1)
        ctx.set_stage("a", total=4)
        ctx.advance(2)
        assert ctx.progress_value() == 0.5
        ctx.set_total(8)
        assert ctx.progress_value() == 0.25
        ctx.advance(2)
        assert ctx.progress_value() == 0.5

    asyncio.run(scenario())


def test_heartbeat_emitted_when_worker_stage_stalls(monkeypatch):
    """长阶段在 to_thread 里跑、期间没有日志/进度时，骨架要补「仍在进行」事件。"""
    from app.policies import GenerationPolicy
    from app.tasks import staged
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    published = []
    monkeypatch.setattr(staged.manager, "publish",
                        lambda task, event, data: published.append((event, dict(data))))
    monkeypatch.setattr(staged.manager, "log", lambda *args, **kwargs: None)

    async def scenario():
        def slow(ctx):
            ctx.set_stage("fetching", total=5)
            ctx.advance(2)
            time.sleep(1.0)
            return "ok"

        job = StagedJob(Task("staged_test"), [Stage("fetching", slow)],
                        policy=GenerationPolicy(), persist=lambda ctx: None,
                        heartbeat_interval=0.05)
        return await job.run()

    assert asyncio.run(scenario()) == "ok"
    beats = [data for event, data in published if data.get("heartbeat")]
    assert beats
    assert all(event == "progress" for event, data in published if data.get("heartbeat"))
    assert beats[0]["stage"] == "fetching"
    assert beats[-1]["progress"] == pytest.approx(0.4)
    assert beats[-1]["done"] == 2 and beats[-1]["total"] == 5
    assert beats[0]["stage_elapsed_s"] >= 0


def test_no_heartbeat_while_progress_keeps_flowing(monkeypatch):
    """持续有进度事件时不应额外补心跳（否则日志会被刷屏）。"""
    from app.policies import GenerationPolicy
    from app.tasks import staged
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    published = []
    monkeypatch.setattr(staged.manager, "publish",
                        lambda task, event, data: published.append((event, dict(data))))
    monkeypatch.setattr(staged.manager, "log", lambda *args, **kwargs: None)

    async def scenario():
        async def steady(ctx):
            ctx.set_stage("samples", total=20)
            for _ in range(20):
                ctx.advance(1)
                await asyncio.sleep(0.05)
            return "done"

        job = StagedJob(Task("staged_test"), [Stage("samples", steady)],
                        policy=GenerationPolicy(), persist=lambda ctx: None,
                        heartbeat_interval=0.05)
        return await job.run()

    assert asyncio.run(scenario()) == "done"
    assert published
    assert not any(data.get("heartbeat") for _, data in published)


def test_heartbeat_goes_through_publish_with_monotonic_seq(monkeypatch):
    """心跳与其他事件共用 manager.publish 的 seq，重连去重/续传逻辑不需要特殊分支。"""
    from app.policies import GenerationPolicy
    from app.tasks import staged
    from app.tasks.manager import Task
    from app.tasks.staged import Stage, StagedJob

    monkeypatch.setattr(staged.manager, "log", lambda *args, **kwargs: None)
    task = Task("staged_test")

    async def scenario():
        def slow(ctx):
            time.sleep(0.6)

        job = StagedJob(task, [Stage("projection", slow)],
                        policy=GenerationPolicy(), persist=lambda ctx: None,
                        heartbeat_interval=0.05)
        await job.run()

    asyncio.run(scenario())
    heartbeats = [payload for _, payload in task.events if payload.get("heartbeat")]
    assert heartbeats
    seqs = [payload["seq"] for _, payload in task.events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    assert all(payload["stage"] == "projection" for payload in heartbeats)

