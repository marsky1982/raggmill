"""Step 2 人格生成：聚类照抄 ragas，逐个 persona 的 LLM 调用交给框架 JobExecutor。

为什么不直接调 ragas 的 `generate_personas_from_kg`：它用同步的 `run_async_batch` 跑人格，
外层拿不到句柄，取消后已发出的调用只能全部跑完。这里把「聚类选代表摘要」原样照抄
（含 `np.random.choice` 随机补齐的兜底），只把「逐个代表摘要调 LLM」换成可取消的 JobExecutor。

聚类口径必须与 ragas 完全一致（`select_persona_summaries` 有对齐测试），改这里等于改出题行为。
"""
from __future__ import annotations

import asyncio

from ..tasks.staged import JobExecutor

# 与 ragas testset/persona.py 相同：摘要向量余弦相似度超过它就算同一簇
_THRESHOLD = 0.75
# 空图报错文案与 ragas 逐字一致（含它的拼写错误），调用方据此判断「没有可用人格素材」
_EMPTY_KG_MESSAGE = "No nodes that satisfied the given filer. Try changing the filter."


def _default_filter(node) -> bool:
    """与 ragas `default_filter` 一致：文档 / 分块节点，且带摘要向量。"""
    return (node.type.name in ("DOCUMENT", "CHUNK")
            and node.properties.get("summary_embedding") is not None)


def select_persona_summaries(kg, num_personas: int) -> list[str]:
    """选出要送给 LLM 的代表摘要（顺序与条数与 ragas 一致）。

    步骤与 ragas 逐行对应：过滤节点 → 取摘要文本 → 截断人格数 → 摘要向量余弦相似度
    按 0.75 单遍分组（每组把后续所有相似节点一次性收进来）→ 每组取最长摘要作代表 →
    代表数不足 `num_personas` 时用 `np.random.choice` 有放回随机补齐。
    """
    import numpy as np

    nodes = [node for node in kg.nodes if _default_filter(node)]
    if len(nodes) == 0:
        raise ValueError(_EMPTY_KG_MESSAGE)

    summaries = [node.properties.get("summary") for node in nodes]
    summaries = [summary for summary in summaries if isinstance(summary, str)]
    num_personas = min(num_personas, len(summaries))

    embeddings = np.array([node.properties.get("summary_embedding") for node in nodes])
    cosine_similarities = np.dot(embeddings, embeddings.T)

    groups: list[list[int]] = []
    visited: set[int] = set()
    for i, _ in enumerate(summaries):
        if i in visited:
            continue
        group = [i]
        visited.add(i)
        for j in range(i + 1, len(summaries)):
            if cosine_similarities[i, j] > _THRESHOLD:
                group.append(j)
                visited.add(j)
        groups.append(group)

    top_summaries = [max([summaries[i] for i in group], key=len) for group in groups]
    if len(top_summaries) <= num_personas:
        top_summaries.extend(np.random.choice(top_summaries, num_personas - len(top_summaries)))
    return [str(summary) for summary in top_summaries[:num_personas]]


def generate_personas(kg, llm, *, num_personas: int, callbacks: list, ctx,
                      max_workers: int | None = None) -> list:
    """聚类在本线程完成，LLM 调用由框架 JobExecutor 并发，可被 `ctx` 取消。

    阶段内被取消时把 `CancelledError` 原样上抛（骨架据此落 canceled 终态），
    并留一条「人格生成阶段」日志给用户解释为什么之后不再有调用。

    `max_workers` 跟随用户在「成本与容错」里设置的并发上限（人格数最多 10，但不能无视该上限：
    用户调小并发通常是为了避开网关限流）；不传时按人格数全量放行。
    """
    from ragas.prompt import StringIO
    from ragas.testset.persona import Persona, PersonaGenerationPrompt

    summaries = select_persona_summaries(kg, num_personas)
    prompt = PersonaGenerationPrompt()
    jobs = [
        (lambda p=prompt, s=summary: p.generate(
            llm=llm, data=StringIO(text=s), callbacks=callbacks, temperature=1.0))
        for summary in summaries
    ]

    ctx.set_stage("personas", len(jobs))
    workers = min(len(jobs), max_workers) if max_workers else len(jobs)
    executor = JobExecutor(max_workers=workers, raise_exceptions=True,
                           on_complete=lambda completed, total, value: ctx.advance(1))
    # 交给框架持有：终态时框架调 cancel()，中断在途的人格调用（不再跑完那 ≤num_personas 次）
    ctx.attach_executor(executor)
    if ctx.cancelled:
        raise asyncio.CancelledError
    try:
        persona_list = asyncio.run(executor.run(jobs))
    except asyncio.CancelledError:
        ctx.log("停止信号（用户取消）：当前处于人格生成阶段，已中断在途调用，"
                "之后不再发起任何新调用。已完成的样本与 token 账单会照常落盘。")
        raise
    return [Persona(name=p.name, role_description=p.role_description) for p in persona_list]
