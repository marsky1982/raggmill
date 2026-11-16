"""Step 1 两个缺陷的回归：单分块建图崩溃、短文档被复制成两个 document 节点。

缺陷 1：`ChunkCosineSimilarityBuilder` 在分块 embedding 少于 2 条时，ragas 的
`_find_similar_embedding_pairs` 会拿 `np.array([]).shape == (0,)` 去解包两个值，抛
`ValueError: not enough values to unpack (expected 2, got 1)`。适配层应在候选分块
不足 2 个时跳过该阶段。

缺陷 2：`HeadlineSplitter.split` 在「切不出多个块」时 `return [node], []`，
`Splitter.generate_execution_plan` 又把这个**同一个**节点对象 append 进 `kg.nodes`，
导致同一篇文档出现两次（同 id）；随后文档级相似度还会生成 self-loop 边。

本文件不连外部服务、0 token：LLM / embedding 客户端全部打桩，短文档走不到它们。
"""
from __future__ import annotations

import asyncio

import pytest


def _chunk_node(text: str, embedding: list[float]):
    from ragas.testset.graph import Node, NodeType

    return Node(type=NodeType.CHUNK, properties={
        "page_content": text, "summary": f"摘要{text}", "summary_embedding": embedding,
    })


def _run_plan(coros):
    async def _drain():
        for coro in coros:
            await coro

    asyncio.run(_drain())


# ---------------- 缺陷 1：单分块（0 / 1 条分块向量）不得崩溃 ----------------

@pytest.mark.parametrize("chunk_count", [0, 1])
def test_chunk_cosine_builder_skips_when_fewer_than_two_chunks(chunk_count):
    from app.ragas_adapter.kg import _chunk_enhance_transforms
    from ragas.testset.graph import KnowledgeGraph

    builder = _chunk_enhance_transforms(llm=None, embedding_model=None)[2]
    assert type(builder).__name__ == "ChunkCosineSimilarityBuilder"

    nodes = [_chunk_node("片段", [0.1, 0.2, 0.3]) for _ in range(chunk_count)]
    kg = KnowledgeGraph(nodes=nodes)

    # 修复前：0 条时 np.array([]).shape 解包失败；1 条时虽不崩但也不该发空任务
    plan = builder.generate_execution_plan(kg)
    assert plan == []
    assert kg.relationships == []


def test_chunk_cosine_builder_still_builds_edge_with_two_chunks():
    """守卫不能把正常多分块场景一起关掉。"""
    from app.ragas_adapter.kg import _chunk_enhance_transforms
    from ragas.testset.graph import KnowledgeGraph

    builder = _chunk_enhance_transforms(llm=None, embedding_model=None)[2]
    kg = KnowledgeGraph(nodes=[
        _chunk_node("甲", [1.0, 0.0]),
        _chunk_node("乙", [0.99, 0.01]),
    ])
    _run_plan(builder.generate_execution_plan(kg))

    rels = [r for r in kg.relationships if r.type == "summary_similarity"]
    assert len(rels) == 1
    assert rels[0].source.id != rels[0].target.id


# ---------------- 缺陷 2：同篇文档只保留一个 document 节点 ----------------

def test_dedupe_same_object_appended_twice_and_drops_self_loop():
    from app.ragas_adapter.kg import _dedupe_documents
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    doc = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "短文档正文", "document_metadata": {"filename": "短文档.md"},
    })
    kg = KnowledgeGraph(
        nodes=[doc, doc],  # ragas 的 Splitter 就是这样把同一个对象又 append 了一次
        relationships=[Relationship(type="summary_similarity", source=doc, target=doc,
                                    bidirectional=True, properties={"summary_similarity": 1.0})],
    )

    assert _dedupe_documents(kg) == 1
    assert [n for n in kg.nodes if n.type == NodeType.DOCUMENT] == [doc]
    assert kg.relationships == []


def test_dedupe_remaps_child_edges_to_kept_node():
    """不同 id 但文件名 + 正文相同的副本：child 边必须重指到保留节点，不留孤儿边。"""
    from app.ragas_adapter.kg import _dedupe_documents
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    props = {"page_content": "同一篇正文", "document_metadata": {"filename": "重复.md"}}
    keep = Node(type=NodeType.DOCUMENT, properties=dict(props))
    dup = Node(type=NodeType.DOCUMENT, properties=dict(props))
    other = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "另一篇正文", "document_metadata": {"filename": "另一篇.md"},
    })
    child_a = Node(type=NodeType.CHUNK, properties={"page_content": "子块A"})
    child_b = Node(type=NodeType.CHUNK, properties={"page_content": "子块B"})
    kg = KnowledgeGraph(nodes=[keep, dup, other, child_a, child_b], relationships=[
        Relationship(type="child", source=keep, target=child_a),
        Relationship(type="child", source=dup, target=child_b),
    ])

    assert _dedupe_documents(kg) == 1
    docs = [n for n in kg.nodes if n.type == NodeType.DOCUMENT]
    assert {n.id for n in docs} == {keep.id, other.id}
    children = {r.target.id: r.source.id for r in kg.relationships if r.type == "child"}
    assert children == {child_a.id: keep.id, child_b.id: keep.id}
    # 不能留下指向已删除副本的边
    assert all(r.source.id in {n.id for n in kg.nodes} and r.target.id in {n.id for n in kg.nodes}
               for r in kg.relationships)


def test_dedupe_keeps_same_filename_with_different_content():
    """同文件名不同正文（如改版）不是重复，不能误合。"""
    from app.ragas_adapter.kg import _dedupe_documents
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    a = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "第一版", "document_metadata": {"filename": "同名词.md"},
    })
    b = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "第二版", "document_metadata": {"filename": "同名词.md"},
    })
    kg = KnowledgeGraph(nodes=[a, b])

    assert _dedupe_documents(kg) == 0
    assert len(kg.nodes) == 2


# ---------------- 端到端：单篇短文档跑完 build_kg ----------------

def test_build_kg_single_short_document(monkeypatch, tmp_path):
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks import manager as manager_mod
    from ragas.run_config import RunConfig
    from ragas.testset.graph import NodeType
    from ragas.testset.transforms.base import Extractor
    from ragas.testset.transforms.splitters import HeadlineSplitter

    storage.init_storage()
    text = "这是一篇很短的中文文档，只有一句话，切不出多个分块。"
    doc = storage.add_document("step1-短文档.md", text, len(text))

    class _Headlines(Extractor):
        async def extract(self, node):
            return "headlines", []

    def fake_default_transforms(documents, llm, embedding_model):
        # 只保留「提取标题 + 切片」两步：短文档在 HeadlineSplitter 里会被原样返回
        return [_Headlines(), HeadlineSplitter(min_tokens=500, max_tokens=1000)]

    monkeypatch.setattr("ragas.testset.transforms.default_transforms", fake_default_transforms)
    monkeypatch.setattr(kg_mod, "_build_step1_clients",
                        lambda *a, **kw: (object(), object(), RunConfig()))

    async def _run():
        task = manager_mod.manager.create("build_kg", ("ragas-generation", "run:test-step1-short"))
        manager_mod.manager.run(task, kg_mod.build_kg(
            task, [doc["id"]], {"id": "p-llm"}, {"id": "p-emb"}, "单短文档测试",
            run_id="run-test-step1-short",
        ))
        await task._async_task
        return task

    task = asyncio.run(_run())

    assert task.status == "succeeded", task.error
    assert task.result and task.result["kg_id"]
    data = storage.kg_read_json(task.result["kg_id"])
    docs = [n for n in data["nodes"] if n["type"] == NodeType.DOCUMENT.value]
    assert len(docs) == 1, f"短文档被复制成了 {len(docs)} 个 document 节点"
    assert not [r for r in data["relationships"] if r["source"] == r["target"]]
    # 断点在成功后应已清除（只看本次 run_id，不依赖会话级共享目录里别的残留）
    assert storage.ckpt_read_meta("run-test-step1-short") is None


# ---------------- 集成：恰好 1 个分块走真实增强流程 ----------------

class _FakeSummaryLLM:
    """只回固定摘要 JSON，让 SummaryExtractor 走真实的 PydanticPrompt 解析链路。"""

    is_async = True

    async def generate(self, prompt, n=1, temperature=None, stop=None, callbacks=None):
        from ragas.llms.base import Generation, LLMResult

        return LLMResult(generations=[[Generation(text='{"text": "自动摘要"}') for _ in range(n)]])


class _FakeEmbeddings:
    is_async = True

    async def aembed_text(self, text):
        return [0.1, 0.2, 0.3]


def test_single_chunk_enhance_runs_through_real_transforms():
    """锁定的真实结论（这里固化成可回归的集成用例）：

    1. ragas 的 HeadlineSplitter 在只剩 1 段时 `return [node], []`，不会产出 chunk 节点，
       所以真实 build_kg 流水线本身走不出「恰好 1 个分块」；
    2. 单分块图谱实际来自 RAGFlow 投影等外部来源，进入 Step 1 的 A′ 增强
       （摘要 → 向量 → 相似边）时必须正常跑完，不能因 `_find_similar_embedding_pairs`
       解包失败而整单崩溃；
    3. 结果应为「分块补齐 summary/summary_embedding、summary_similarity 边为 0」。

    外部依赖全部打桩（0 token），但 apply_transforms 与三个真实 transform 都不打桩。
    """
    from app.ragas_adapter.kg import _chunk_enhance_transforms
    from ragas.run_config import RunConfig
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType
    from ragas.testset.transforms import apply_transforms

    chunk = Node(type=NodeType.CHUNK, properties={"page_content": "唯一的分块正文"})
    kg = KnowledgeGraph(nodes=[chunk])
    transforms = _chunk_enhance_transforms(llm=_FakeSummaryLLM(), embedding_model=_FakeEmbeddings())

    # apply_transforms 是同步入口，内部自己起事件循环
    apply_transforms(kg, transforms, run_config=RunConfig(max_workers=1))

    chunks = [n for n in kg.nodes if n.type == NodeType.CHUNK]
    assert len(chunks) == 1
    assert chunks[0].get_property("summary") == "自动摘要"
    assert chunks[0].get_property("summary_embedding") == [0.1, 0.2, 0.3]
    assert [r for r in kg.relationships if r.type == "summary_similarity"] == []
