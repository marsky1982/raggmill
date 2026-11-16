"""进度必须按「本任务自己的完成数」计算。

PR7 去掉了进程全局 tqdm 捕获（那套按 ragas 两个 tqdm 的 total 折算百分比，且多个任务共用同一份
stderr，会互相串改进度）。现在每个 JobContext 自持 done/total，本用例锁住两个并发任务互不影响。
"""
from __future__ import annotations

import asyncio


def _make_job(totals: int, delays: list[float], seen: dict):
    from app.policies import GenerationPolicy
    from app.tasks.manager import Task
    from app.tasks.staged import JobExecutor, StagedJob, Stage

    async def _stage(ctx):
        ctx.set_stage("samples", totals)
        seen["total"] = totals
        seen["progress"] = []

        def _on_complete(completed, total, _value):
            # 完成一条计一次：本任务的 total 只能是自己的，进度只随自己的完成数增长
            assert total == totals
            ctx.advance(1)
            seen["progress"].append(ctx.progress_value())

        executor = JobExecutor(max_workers=len(delays), on_complete=_on_complete)
        await executor.run([lambda d=delay: _sleep(d) for delay in delays])

    return StagedJob(Task("testset"), [Stage("samples", _stage)],
                     policy=GenerationPolicy(), persist=lambda ctx: None, name="progress")


async def _sleep(delay: float):
    await asyncio.sleep(delay)
    return delay


def test_concurrent_jobs_keep_progress_independent():
    seen_a: dict = {}
    seen_b: dict = {}
    job_a = _make_job(2, [0.05, 0.10], seen_a)   # 总是最后完成
    job_b = _make_job(4, [0.01, 0.02, 0.03, 0.04], seen_b)

    async def _run():
        await asyncio.gather(job_a.run(), job_b.run())

    asyncio.run(_run())

    # 单阶段任务：progress = done / total，满进度封顶 0.99
    assert seen_a["total"] == 2 and seen_a["progress"] == [0.5, 0.99]
    assert seen_b["total"] == 4 and seen_b["progress"] == [0.25, 0.5, 0.75, 0.99]
    # 两个任务的上下文、完成计数完全隔离
    assert job_a.ctx is not job_b.ctx
    assert job_a.ctx.progress_value() == job_b.ctx.progress_value()
