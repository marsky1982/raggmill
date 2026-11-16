"""重建投影时继承内容未变节点的增强成果。

知识库里只要一篇文档变了，同步就整张重建投影；旧投影里已付费的摘要 / 主题 / 摘要向量全部丢失，
下次出题前按全库重新增强。这里验证：只有内容真没变的节点才继承，变了的、新增的一律不继承。
不连真实 RAGFlow、0 token。测试模块顶层不 import app。
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _snapshot(chunks: dict[str, tuple[str, str]], dataset: str = "ds-inherit") -> dict:
    """chunks: {chunk_id: (document_id, 正文)}"""
    docs = {d: {"id": d, "name": f"{d}.txt"} for d, _ in chunks.values()}
    return {"dataset_id": dataset, "documents": docs,
            "chunks": {cid: {"id": cid, "document_id": d, "position": i, "page_content": text, "entities": []}
                       for i, (cid, (d, text)) in enumerate(chunks.items())}}


def _enhance(projection: dict, ragas_style_type: bool = False) -> dict:
    """模拟生成前增强写回：分块补摘要 / 主题 / 向量，文档补摘要 / 向量；可选模拟 ragas 序列化后的 type 写法。"""
    for node in projection["nodes"]:
        props = node["properties"]
        kind = node["type"]
        props["summary"] = f"摘要:{props.get('source_chunk_id') or props.get('page_content')}"
        props["summary_embedding"] = [0.1, 0.2, 0.3]
        if kind == "chunk":
            props["themes"] = ["主题"]
        if ragas_style_type:
            node["type"] = "NodeType." + kind.upper()
    return projection


def _props(projection: dict, kind: str, key: str) -> dict:
    out = {}
    for node in projection["nodes"]:
        if node["type"] != kind:
            continue
        props = node["properties"]
        ident = props.get("source_chunk_id") if kind == "chunk" else props["document_metadata"]["filename"]
        out[ident] = props.get(key)
    return out


def test_unchanged_nodes_inherit_and_changed_ones_do_not():
    from app.ragas_adapter.ragflow import build_ragas_projection, inherit_enhancement

    old = _enhance(build_ragas_projection(_snapshot({
        "a1": ("A", "甲文档第一段"), "a2": ("A", "甲文档第二段"), "b1": ("B", "乙文档原文")}),
        source_id="conn__ds-inherit"), ragas_style_type=True)
    new = build_ragas_projection(_snapshot({
        "a1": ("A", "甲文档第一段"), "a2": ("A", "甲文档第二段"), "b1": ("B", "乙文档改过的原文")}),
        source_id="conn__ds-inherit")
    stats = inherit_enhancement(new, old)

    assert stats == {"chunks": 2, "documents": 1}
    summaries = _props(new, "chunk", "summary")
    assert summaries["a1"] == "摘要:a1" and summaries["a2"] == "摘要:a2"
    assert summaries["b1"] is None, "正文变了的分块不能继承旧摘要"
    assert _props(new, "chunk", "themes")["a1"] == ["主题"]
    assert _props(new, "chunk", "summary_embedding")["b1"] is None
    docs = _props(new, "document", "summary_embedding")
    assert docs["A.txt"] == [0.1, 0.2, 0.3]
    assert docs["B.txt"] is None, "子分块变了，文档摘要要重算"


def test_document_with_new_chunk_is_not_inherited_but_its_old_chunks_are():
    from app.ragas_adapter.ragflow import build_ragas_projection, inherit_enhancement

    old = _enhance(build_ragas_projection(_snapshot({"a1": ("A", "第一段")}), source_id="conn__ds-inherit"))
    new = build_ragas_projection(_snapshot({"a1": ("A", "第一段"), "a2": ("A", "新增的第二段")}),
                                 source_id="conn__ds-inherit")
    stats = inherit_enhancement(new, old)
    assert stats == {"chunks": 1, "documents": 0}
    assert _props(new, "chunk", "summary") == {"a1": "摘要:a1", "a2": None}
    assert _props(new, "document", "summary") == {"A.txt": None}


def test_no_previous_projection_is_a_noop():
    from app.ragas_adapter.ragflow import build_ragas_projection, inherit_enhancement

    new = build_ragas_projection(_snapshot({"a1": ("A", "第一段")}), source_id="conn__ds-inherit")
    assert inherit_enhancement(new, None) == {"chunks": 0, "documents": 0}
    assert _props(new, "chunk", "summary") == {"a1": None}


def test_second_sync_after_content_change_keeps_enhancement_of_unchanged_document(monkeypatch):
    """集成：第一次同步 → 模拟出题前增强写回 → 改一篇文档的正文 → 第二次同步，没变的文档沿用增强成果。"""
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    docs = [{"id": "d1", "name": "一.txt", "update_time": "1", "status": "done", "chunk_count": 1},
            {"id": "d2", "name": "二.txt", "update_time": "1", "status": "done", "chunk_count": 1}]
    chunks = {"d1": [{"id": "c1", "content": "第一篇原文"}], "d2": [{"id": "c2", "content": "第二篇原文"}]}

    class Client:
        def list_documents(self, dataset_id):
            return [dict(r) for r in docs]

        def list_chunks(self, dataset_id, document_id):
            return [dict(r) for r in chunks.get(document_id, [])]

        def kb_structure(self, dataset_id):
            return {"templates": [{"entities": []}]}

        def get_version(self):
            return "1.0.0-rc3"

        def alteration(self, dataset_id):
            return {}

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *a, **k: Client())
    dataset = "ds-inherit-sync"

    def sync():
        task = Task("ragflow_sync")
        task.resources = storage.ragflow_sync_resources(dataset, None)  # 入口要求持有租约
        return asyncio.run(rf.sync_ragflow(task, dataset, "http://127.0.0.1:9380", "key")), list(task.logs)

    first, _ = sync()
    projection = storage.kg_read_json(first["kg_id"])
    storage.kg_write_json(first["kg_id"], _enhance(projection, ragas_style_type=True))  # 出题前增强写回

    chunks["d2"][0]["content"] = "第二篇改过的原文"
    second, logs = sync()
    assert second["kg_id"] != first["kg_id"], "内容变了应当重建投影"
    rebuilt = storage.kg_read_json(second["kg_id"])
    assert _props(rebuilt, "chunk", "summary") == {"c1": "摘要:c1", "c2": None}
    assert _props(rebuilt, "document", "summary_embedding") == {"一.txt": [0.1, 0.2, 0.3], "二.txt": None}
    assert any("沿用旧投影的增强成果：1 个分块、1 篇文档" in line for line in logs)
