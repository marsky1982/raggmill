"""人格聚类必须与 ragas 原实现逐字对齐。

PR7 只把「逐个 persona 调 LLM」换成可取消的 JobExecutor，聚类（过滤 → 余弦 0.75 分组 →
每组取最长摘要 → 不足时 np.random.choice 随机补齐）必须与 ragas 完全一致，否则人格数量与
代表摘要会漂移，出题风格跟着变。这里用确定性随机数 + 打桩 run_async_batch 做逐字对齐。
"""
from __future__ import annotations

import pprint
import uuid

import numpy as np
import pytest

# 两个同一簇（余弦 1.0）+ 一个独立簇：代表摘要分别是「更长的同一簇摘要」与「另一个簇的摘要」，
# 代表数 2 < num_personas 3，因此必然走一次随机补齐分支。
_SPECS = (
    ("短摘要", [1.0, 0.0, 0.0]),
    ("更长的同一簇摘要", [1.0, 0.0, 0.0]),
    ("另一个簇的摘要", [0.0, 1.0, 0.0]),
)


def _build_kg():
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    nodes = [Node(id=str(uuid.uuid4()), type=NodeType.CHUNK,
                  properties={"summary": summary, "summary_embedding": embedding,
                              "page_content": summary})
             for summary, embedding in _SPECS]
    return KnowledgeGraph(nodes=nodes)


def test_select_persona_summaries_matches_ragas(monkeypatch):
    from ragas.testset import persona as ragas_persona

    from app.ragas_adapter.testset_persona import select_persona_summaries

    # 随机补齐必须确定性：两边都取前 N 个
    monkeypatch.setattr(np.random, "choice", lambda a, size: list(a)[:size])

    sent: list[list[str]] = []

    def fake_run_async_batch(**kwargs):
        sent.append([item["data"].text for item in kwargs["kwargs_list"]])
        return [ragas_persona.Persona(name=f"p{i}", role_description="r")
                for i, _ in enumerate(kwargs["kwargs_list"])]

    # 打桩 ragas 自己的批量入口，只捕获它真正送进 LLM 的 data 文本
    monkeypatch.setattr(ragas_persona, "run_async_batch", fake_run_async_batch)

    kg = _build_kg()
    ragas_persona.generate_personas_from_kg(llm=object(), kg=kg, num_personas=3, callbacks=[])
    ours = select_persona_summaries(kg, 3)

    assert pprint.pformat(sorted(sent[0])) == pprint.pformat(sorted(ours))
    assert sorted(ours) == sorted(["更长的同一簇摘要", "另一个簇的摘要", "更长的同一簇摘要"])


def test_empty_kg_error_message_matches_ragas(monkeypatch):
    from ragas.testset import persona as ragas_persona
    from ragas.testset.graph import KnowledgeGraph

    from app.ragas_adapter.testset_persona import select_persona_summaries

    monkeypatch.setattr(ragas_persona, "run_async_batch",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不该调 LLM")))
    with pytest.raises(ValueError) as ragas_exc:
        ragas_persona.generate_personas_from_kg(llm=object(), kg=KnowledgeGraph(),
                                                num_personas=3, callbacks=[])
    with pytest.raises(ValueError) as our_exc:
        select_persona_summaries(KnowledgeGraph(), 3)
    assert str(our_exc.value) == str(ragas_exc.value)
