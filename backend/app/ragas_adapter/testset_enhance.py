"""Step 2 生成前增强（投影 KG）：补摘要 / 主题 / 摘要向量，部分成果落盘与账单。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from ..storage import kg_update, now_iso
from ..tasks.manager import Task, manager
from .clients import build_embeddings, build_llm, configure_run
from .kg import _kg_to_dict
from .testset_common import _has_content

# 增强的四个子项（与 enhance_existing_kg 的返回值键对应）：完成一项回调一次进度
_ENHANCE_ITEMS = (("chunk 摘要", "chunk_summaries"), ("文档摘要", "document_summaries"),
                  ("themes", "theme_chunks"), ("向量", "embeddings"))


def _enhancement_usage(before: dict, after: dict) -> dict:
    """增强阶段的 token 分项：用遥测前后快照相减，避免与出题账单混在一个总数里。"""
    def diff(key: str) -> int:
        return max(0, int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0))

    llm_input, llm_output = diff("input_tokens"), diff("output_tokens")
    embedding = diff("embedding_tokens")
    return {
        "input_tokens": llm_input,
        "output_tokens": llm_output,
        "llm_tokens": llm_input + llm_output,
        "embedding_tokens": embedding,
        "total_tokens": llm_input + llm_output + embedding,
        "llm_calls": diff("llm_calls"),
        "embedding_calls": diff("embedding_calls"),
    }


class EnhancementStopped(RuntimeError):
    """生成前增强没有正常完成（失败 / 超预算 / 取消）。

    与旧行为的区别：不再「整批作废」。ragas 会把同批 transform 跑完才抛错，已成功节点的
    摘要 / themes / 向量都是已付费成果，会先写回 KG（下次只补剩余缺口）；调用方据此把
    增强账单落盘到测试集，而不是让这笔 token 无处可查。
    """

    def __init__(self, stop_reason: str, message: str, *, saved_nodes: int = 0):
        super().__init__(message)
        self.stop_reason = stop_reason  # failed | budget_exceeded | canceled
        self.saved_nodes = saved_nodes


def _material_counts(kg) -> dict[str, int]:
    """增强会写的三类逐节点素材计数；前后对比即可知道这次实际补上了多少。"""
    counts = {"summary": 0, "themes": 0, "summary_embedding": 0}
    for node in kg.nodes:
        props = node.properties
        if _has_content(props.get("summary")):
            counts["summary"] += 1
        if _has_content(props.get("themes")):
            counts["themes"] += 1
        if props.get("summary_embedding") is not None:
            counts["summary_embedding"] += 1
    return counts


async def _enhance_projection_kg(
    task: Task,
    kg_entry: dict,
    kg,
    llm_preset: dict,
    emb_preset: dict,
    telemetry,
    on_progress: Callable[[str], None] | None = None,
    *,
    max_retries: int,
    llm_timeout: int,
    max_workers: int,
    include_themes: bool = True,
) -> dict:
    """生成前增强投影 KG。

    · 成功且确有改动 → 原子写回；
    · 失败 / 超预算 / 取消 → 先把已成功节点的成果写回（逐节点属性，幂等，下次只补剩余），
      再抛 EnhancementStopped，由调用方把账单落盘；一个节点都没补上时不写盘。
    · 增强线程登记进 task.worker_futures：取消后租约要等线程真正退出才释放，避免新任务与
      仍在跑的旧增强线程并发、重复花钱。
    · 写回前确认 KG 仍在索引里：生成期间 KG 被删除 / 被同步清理时不把它「复活」（S3 / R-1）。
    """
    from .kg import enhance_existing_kg

    llm = build_llm(llm_preset, timeout=llm_timeout, sdk_max_retries=0, callbacks=[telemetry])
    embedding_model = build_embeddings(
        emb_preset, timeout=llm_timeout, sdk_max_retries=0,
        telemetry=telemetry, max_retries=max_retries,
    )
    run_config = configure_run(llm, timeout=llm_timeout, retries=max_retries, workers=max_workers)
    before = _material_counts(kg)

    def _run():
        return enhance_existing_kg(kg, llm, embedding_model, run_config=run_config,
                                   include_themes=include_themes)

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, _run)
    task.worker_futures.append(future)

    def _finished(f):
        if not f.cancelled():
            f.exception()  # 取消后无人 await 的线程异常也要消费
        if task._manager:
            task._manager.worker_finished(task)

    future.add_done_callback(_finished)

    stats: dict | None = None
    failure: BaseException | None = None
    canceled = False
    try:
        stats = await asyncio.shield(future)
    except asyncio.CancelledError:
        canceled = True
        # 遥测的 should_stop 已挡住新调用，在途调用最多再等一个超时；等线程退出后再结算成果与账单
        deadline = time.time() + llm_timeout + 15
        while not future.done() and time.time() < deadline:
            try:
                await asyncio.wait({future}, timeout=1.0)
            except asyncio.CancelledError:
                pass
        if future.done() and not future.cancelled() and future.exception() is None:
            stats = future.result()
    except Exception as exc:  # noqa: BLE001 —— 转成可读错误，已付费成果照常保存
        failure = exc

    if task.cancel_event.is_set():
        canceled = True
    budget = bool(telemetry.budget_exceeded)
    after = _material_counts(kg) if future.done() else before
    gained = {k: max(0, after[k] - before[k]) for k in before}
    saved_nodes = max(gained.values()) if gained else 0
    complete = stats is not None and not canceled and not budget and failure is None

    if complete and stats.get("changed") or (not complete and saved_nodes and future.done()):
        # 锁内完成「写投影原文 + 刷新索引」：期间被删除则整体跳过，不会把已删图谱写回列表
        def _write_back(entry: dict) -> None:
            entry.update(
                node_count=len(kg.nodes),
                relationship_count=len(kg.relationships),
                enhanced_at=now_iso(),
                enhancement=(stats if complete else
                             {"partial": True, "gained": gained, "stop": "canceled" if canceled
                              else "budget_exceeded" if budget else "failed"}),
            )

        saved = await asyncio.to_thread(kg_update, kg_entry["id"], _write_back, data=_kg_to_dict(kg))
        if saved is None:
            manager.log(task, "⚠ 知识图谱已在生成期间被删除 / 清理，增强成果不再写回（避免把它重新加回列表）",
                        extra={"stage": "enhancing", "level": "warn"})

    if complete:
        if on_progress is not None:
            for label, key in _ENHANCE_ITEMS:
                if stats.get(key):
                    on_progress(label)
        return stats
    kept = (f"已保存 {saved_nodes} 个节点的已完成部分（摘要 +{gained['summary']} / "
            f"themes +{gained['themes']} / 向量 +{gained['summary_embedding']}），下次生成只补剩余缺口"
            if saved_nodes else "未保存任何改动")
    if canceled:
        # 骨架按 CancelledError 落 canceled 终态；给用户的解释走日志（这里在主协程，可安全落日志）
        manager.log(task, f"知识图谱自动增强已取消；{kept}",
                    extra={"stage": "enhancing", "level": "warn"})
        raise asyncio.CancelledError
    if budget:
        raise EnhancementStopped(
            "budget_exceeded",
            f"知识图谱自动增强已超出 token 预算；{kept}。请提高本次预算，或把多跳抽象题比例降低后重试。",
            saved_nodes=saved_nodes)
    raise EnhancementStopped(
        "failed",
        f"知识图谱自动增强失败（{kept}）：{type(failure).__name__}: {str(failure)[:300]}",
        saved_nodes=saved_nodes) from failure
