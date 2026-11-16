"""阶段 3 契约硬切：`doc_id` / `source_ref` / `source_type` / `doc_name` 四字段的回归。

不连真实 RAGFlow、不跑付费 LLM：建图走演示模式（`RAGAS_AVAILABLE=False`），
投影走纯函数，外部导入走纯函数 + conftest 指向的临时 DATA_DIR。
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _kind(node: dict) -> str:
    return str(node.get("type") or "").rsplit(".", 1)[-1].lower()


def _props(data: dict, kind: str) -> list[dict]:
    return [node.get("properties") or {} for node in data["nodes"] if _kind(node) == kind]


def _external_kg(doc_node_key: str = "doc-node", with_child: bool = True) -> dict:
    """外部整份 KG JSON：1 篇文档 + 2 个分块（关系里 source/target 是节点 id 字符串）。"""
    nodes = [
        {"id": doc_node_key, "type": "document",
         "properties": {"page_content": "一.txt", "document_metadata": {"filename": "一.txt"}}},
        {"id": "c1", "type": "chunk", "properties": {"page_content": "第一段"}},
        {"id": "c2", "type": "chunk", "properties": {"page_content": "第二段"}},
    ]
    relationships = []
    if with_child:
        relationships = [
            {"id": "e1", "type": "child", "source": doc_node_key, "target": "c1"},
            {"id": "e2", "type": "child", "source": doc_node_key, "target": "c2"},
        ]
    return {"nodes": nodes, "relationships": relationships}


# ---------- 身份构造函数 ----------

def test_identity_constructors_follow_prefix_contract():
    from app.ragas_adapter import doc_identity as di

    local = di.local_document_identity({"id": "doc-1234abcd", "name": "a.md"})
    assert local == {"doc_id": "doc-1234abcd", "source_ref": "",
                     "source_type": "local", "doc_name": "a.md"}

    rf1 = di.ragflow_document_identity("conn__ds", "up-1", "一.txt")
    rf2 = di.ragflow_document_identity("conn__ds", "up-1", "改名了.txt")
    rf3 = di.ragflow_document_identity("conn2__ds", "up-1", "一.txt")
    assert rf1["doc_id"] == rf2["doc_id"], "改文件名不该换身份"
    assert rf1["doc_id"] != rf3["doc_id"], "换连接/知识库就是另一个来源，身份必须不同"
    assert rf1["doc_id"].startswith("rf-") and len(rf1["doc_id"]) == len("rf-") + 12
    assert rf1["source_ref"] == "up-1" and rf1["source_type"] == "ragflow"

    ext = di.external_kg_document_identity("导入名", "node-1", "一.txt")
    assert ext["doc_id"].startswith("ext-") and len(ext["doc_id"]) == len("ext-") + 12
    assert ext["source_ref"] == "", "外部整份 KG 的上游 id 不可信，source_ref 留空"
    assert ext["doc_name"] == "一.txt" and ext["source_type"] == "external"
    assert ext["doc_id"] != di.external_kg_document_identity("导入名", "node-2", "一.txt")["doc_id"], \
        "同一份图里同名文档靠节点 id 区分，不能按文件名合并"


def test_local_identity_falls_back_to_stable_doc_id():
    from app import storage
    from app.ragas_adapter import doc_identity as di

    assert di.local_document_identity({"name": "同名.txt"})["doc_id"] \
        == storage.stable_doc_id("同名.txt")
    assert di.local_document_identity({"id": "doc-x", "name": "a"}, source_type="external")["source_type"] \
        == "external"


def test_external_chunks_identity_uses_content_fingerprint():
    from app.ragas_adapter import doc_identity as di

    base = di.external_chunks_identity("导入", ["甲", "乙"])
    assert base == di.external_chunks_identity("导入", ["乙", "甲"]), "内容顺序不应改变身份"
    assert base["doc_id"] != di.external_chunks_identity("导入", ["甲", "丙"])["doc_id"]
    assert base["doc_id"] != di.external_chunks_identity("另一个导入", ["甲", "乙"])["doc_id"]


def test_source_type_summary_is_single_value_or_mixed():
    from app.ragas_adapter import doc_identity as di

    assert di.summarize_source_types([]) == ""
    assert di.summarize_source_types(["local"]) == "local"
    assert di.summarize_source_types(["ragflow", "ragflow"]) == "ragflow"
    assert di.summarize_source_types(["local", "external"]) == "mixed"


# ---------- 外部整份 KG JSON ----------

def test_external_kg_json_colors_documents_and_children():
    from app import storage
    from app.ragas_adapter.testset_external import prepare_external_kg_from_json

    kg_json = _external_kg()
    kg_id = prepare_external_kg_from_json(kg_json, "导入图")
    data = storage.kg_read_json(kg_id)
    entry = storage.kg_get(kg_id)

    docs = _props(data, "document")
    chunks = _props(data, "chunk")
    assert len(docs) == 1 and len(chunks) == 2
    assert docs[0]["doc_id"].startswith("ext-")
    assert docs[0]["source_ref"] == ""
    assert docs[0]["source_type"] == "external"
    assert docs[0]["doc_name"] == "一.txt"
    assert {chunk["doc_id"] for chunk in chunks} == {docs[0]["doc_id"]}
    assert {chunk["doc_name"] for chunk in chunks} == {"一.txt"}
    assert entry["source_type"] == "external"


def test_external_kg_orphan_chunks_share_one_fingerprint_identity():
    from app import storage
    from app.ragas_adapter.testset_external import prepare_external_kg_from_json

    kg_id = prepare_external_kg_from_json(_external_kg(with_child=False), "游离图")
    chunks = _props(storage.kg_read_json(kg_id), "chunk")

    assert len(chunks) == 2
    assert {chunk["doc_id"] for chunk in chunks} == {chunks[0]["doc_id"]}
    assert chunks[0]["doc_id"].startswith("ext-")
    assert chunks[0]["source_ref"] == ""
    assert chunks[0]["doc_name"] == "游离图"


def test_reimporting_same_external_kg_keeps_doc_id_but_creates_new_kg():
    from app import storage
    from app.ragas_adapter.testset_external import prepare_external_kg_from_json

    first_json = _external_kg()
    first = prepare_external_kg_from_json(first_json, "幂等图")
    second = prepare_external_kg_from_json(copy.deepcopy(first_json), "幂等图")

    assert first != second, "重复导入仍会多出一张图（幂等边界只到 doc_id）"
    first_doc = _props(storage.kg_read_json(first), "document")[0]
    second_doc = _props(storage.kg_read_json(second), "document")[0]
    assert first_doc["doc_id"] == second_doc["doc_id"]


def test_validate_external_kg_rejects_missing_and_duplicate_ids():
    from app.ragas_adapter.doc_identity import validate_external_kg

    with pytest.raises(ValueError, match="缺少 id"):
        validate_external_kg({"nodes": [{"type": "chunk", "properties": {}}]})
    with pytest.raises(ValueError, match="重复的节点 id"):
        validate_external_kg({"nodes": [{"id": "x"}, {"id": "x"}]})
    with pytest.raises(ValueError, match="不是对象"):
        # 节点是字符串时读不到 id，必须和缺 id 一样是 ValueError（路由据此回 400），不能抛 AttributeError
        validate_external_kg({"nodes": [{"id": "x"}, "字符串节点"]})
    with pytest.raises(ValueError, match="nodes 数组"):
        validate_external_kg({"nodes": []})

    validate_external_kg(_external_kg())  # 合法图不应抛错


# ---------- 外部 chunks ----------

def test_external_chunks_import_colors_every_chunk():
    from app import storage
    from app.ragas_adapter.testset_external import prepare_external_kg_from_chunks

    kg_id, health = prepare_external_kg_from_chunks(
        [{"page_content": "第一段", "properties": {"themes": ["主题"]}},
         {"page_content": "第二段", "properties": {}}],
        "分块导入",
    )
    chunks = _props(storage.kg_read_json(kg_id), "chunk")

    assert health["node_count"] == 2
    assert {chunk["doc_id"] for chunk in chunks} == {chunks[0]["doc_id"]}
    assert chunks[0]["doc_id"].startswith("ext-")
    assert chunks[0]["source_type"] == "external"
    assert chunks[0]["doc_name"] == "分块导入"
    assert storage.kg_get(kg_id)["source_type"] == "external"


# ---------- RAGFlow 投影 ----------

def _snapshot(dataset: str = "ds-identity") -> dict:
    return {
        "dataset_id": dataset,
        "documents": {"d1": {"id": "d1", "name": "一.txt"}},
        "chunks": {
            "c1": {"id": "c1", "document_id": "d1", "position": 0,
                   "page_content": "第一段", "entities": ["国家电网"]},
        },
    }


def test_projection_carries_identity_and_is_scoped_by_source_id():
    from app.ragas_adapter.ragflow import build_ragas_projection

    first = build_ragas_projection(_snapshot(), source_id="connA__ds-identity")
    second = build_ragas_projection(_snapshot(), source_id="connB__ds-identity")

    chunk = _props(first, "chunk")[0]
    doc = _props(first, "document")[0]
    assert chunk["doc_id"] == doc["doc_id"]
    assert chunk["doc_id"].startswith("rf-")
    assert chunk["source_ref"] == "d1" and doc["source_ref"] == "d1"
    assert chunk["source_type"] == "ragflow" and doc["source_type"] == "ragflow"
    assert chunk["doc_name"] == "一.txt"
    assert _props(second, "chunk")[0]["doc_id"] != chunk["doc_id"], "不同来源的同名上游 id 不能撞"


def test_projection_requires_source_id():
    """`source_id` 必填：退化成数据集 id 会让不同连接下的同名上游 id 撞成同一个 doc_id。"""
    from app.ragas_adapter.ragflow import build_ragas_projection

    with pytest.raises(TypeError):
        build_ragas_projection(_snapshot())


def test_enhancement_inheritance_still_works_with_doc_id_grouping():
    """回归高风险点：分组键换成 doc_id 后，未改动的文档仍能继承旧投影的增强成果。"""
    from app.ragas_adapter.ragflow import build_ragas_projection, inherit_enhancement

    old = build_ragas_projection(_snapshot("ds-inherit-id"), source_id="conn__ds-inherit-id")
    for node in old["nodes"]:
        node["properties"]["summary"] = "旧摘要"
        node["properties"]["summary_embedding"] = [0.1, 0.2]
    snap = _snapshot("ds-inherit-id")
    snap["chunks"]["c2"] = {"id": "c2", "document_id": "d1", "position": 1,
                            "page_content": "新增的第二段", "entities": []}
    new = build_ragas_projection(snap, source_id="conn__ds-inherit-id")

    stats = inherit_enhancement(new, old)
    assert stats == {"chunks": 1, "documents": 0}, "分块没变可继承；文档多了分块，摘要要重算"

    # 完全没变的同步：文档摘要也要继承（旧的「按 dataset_id + doc_id 反推节点 id」写法在这里会静默失配）
    unchanged = build_ragas_projection(_snapshot("ds-inherit-id"), source_id="conn__ds-inherit-id")
    assert inherit_enhancement(unchanged, old) == {"chunks": 1, "documents": 1}


# ---------- 建图（演示模式，0 token） ----------

def test_build_kg_colors_every_chunk_and_records_source_type(monkeypatch):
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import Task

    monkeypatch.setattr(kg_mod, "RAGAS_AVAILABLE", False)
    doc = storage.add_document("染色演示.txt", "第一段。第二段。", 20)
    result = asyncio.run(kg_mod.build_kg(
        Task("build_kg"), [doc["id"]], {"id": "llm-demo"}, {"id": "emb-demo"}, "染色演示"))

    data = storage.kg_read_json(result["kg_id"])
    entry = storage.kg_get(result["kg_id"])
    chunks = _props(data, "chunk")
    docs = _props(data, "document")

    assert chunks and docs
    assert {chunk["doc_id"] for chunk in chunks} == {doc["id"]}
    assert {chunk["source_type"] for chunk in chunks} == {"local"}
    assert {chunk["doc_name"] for chunk in chunks} == {"染色演示.txt"}
    assert all(chunk["source_ref"] == "" for chunk in chunks)
    assert docs[0]["doc_id"] == doc["id"]
    assert entry["source_type"] == "local"


def test_color_doc_identity_recolors_a_resumed_graph():
    """续跑进来的图可能还是未染色的：同一个完成出口再染一次要能补齐（且不改其它属性）。"""
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    from app.ragas_adapter.kg import _color_doc_identity

    doc = Node(type=NodeType.DOCUMENT, properties={"document_metadata": {"filename": "续跑.txt"}})
    chunk = Node(type=NodeType.CHUNK, properties={"page_content": "正文", "summary": "已有摘要"})
    kg = KnowledgeGraph(nodes=[doc, chunk],
                        relationships=[Relationship(type="child", source=doc, target=chunk)])

    summary = _color_doc_identity(kg, [{"id": "doc-resume01", "name": "续跑.txt"}], "local")

    assert summary == "local"
    assert chunk.properties["doc_id"] == "doc-resume01"
    assert chunk.properties["source_type"] == "local"
    assert chunk.properties["doc_name"] == "续跑.txt"
    assert chunk.properties["summary"] == "已有摘要", "染色只增字段，不能覆盖已有属性"


def test_resume_reads_source_type_back_from_checkpoint(monkeypatch):
    """外部导入建的图中途失败后续跑，不能被染回 local：断点里的 source_type 必须读回。"""
    from app import storage
    from app.ragas_adapter import kg as kg_mod
    from app.tasks.manager import Task

    monkeypatch.setattr(kg_mod, "RAGAS_AVAILABLE", False)
    doc = storage.add_document("外部续跑.txt", "第一段。第二段。", 20)

    real_sleep = asyncio.sleep
    state = {"calls": 0}

    async def flaky_sleep(delay):
        # 只统计演示模式的阶段节奏 sleep（0.7s）；StagedJob 内部 drainer 的 0.2s 轮询不算
        if delay >= 0.5:
            state["calls"] += 1
            if state["calls"] >= 2:
                raise RuntimeError("模拟中途失败")
        await real_sleep(0)

    monkeypatch.setattr(kg_mod.asyncio, "sleep", flaky_sleep)
    with pytest.raises(RuntimeError, match="模拟中途失败"):
        asyncio.run(kg_mod.build_kg(
            Task("build_kg"), [doc["id"]], {"id": "llm-demo"}, {"id": "emb-demo"}, "外部续跑",
            run_id="kgrun-resume-external", source_type="external"))
    monkeypatch.setattr(kg_mod.asyncio, "sleep", real_sleep)

    meta, _ = storage.ckpt_load("kgrun-resume-external")
    assert meta["source_type"] == "external", "断点必须记住来源类型"

    # 续跑入口（/api/kg/generate）不知道 source_type，只能从断点读回
    result = asyncio.run(kg_mod.build_kg(
        Task("build_kg"), [doc["id"]], {"id": "llm-demo"}, {"id": "emb-demo"}, "外部续跑",
        run_id="kgrun-resume-external", resume=True))

    assert storage.kg_get(result["kg_id"])["source_type"] == "external"
    chunks = _props(storage.kg_read_json(result["kg_id"]), "chunk")
    assert {chunk["source_type"] for chunk in chunks} == {"external"}


# ---------- 守卫：盘上不允许残留旧口径 ----------

def test_guard_no_stale_identity_keys_on_disk(monkeypatch):
    """v2.6 §3.3-6：一次合成建图 + 一次打桩同步后扫落盘 JSON，确认没有旧口径残留。

    自带前置数据，不依赖本文件里其它用例先跑：单独执行本用例也必须通过。
    扫描范围 `data/kg/**/*.json` + `data/testset/*.json`；快照目录不在范围内。
    """
    from app import config, storage
    from app.ragas_adapter import kg as kg_mod
    from app.ragas_adapter import ragflow as rf
    from app.ragas_adapter.provenance import attach_provenance
    from app.tasks.manager import Task
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    stale_fragments = ("doc_by_chunk", "source_document_node_ids")
    allowed_types = {"local", "ragflow", "external", "mixed"}
    doc_id_prefixes = ("doc-", "rf-", "ext-")

    def _stale_keys(payload: dict) -> list[str]:
        # source_document_ids 是样本出处的合法字段（值是 doc_id）；旧的投影字段叫 source_document_id
        return [key for key in payload
                if key == "source_document_id" or any(f in key for f in stale_fragments)]

    def _scan_stale_keys(path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):  # 索引类文件是数组，没有节点
            return
        for node in (data.get("nodes") or []) + (data.get("relationships") or []):
            props = node.get("properties") or {}
            assert not _stale_keys(props), path.name

    # 1) 合成建图（演示模式，0 token）：本地产物
    monkeypatch.setattr(kg_mod, "RAGAS_AVAILABLE", False)
    doc = storage.add_document("守卫建图.txt", "第一段。第二段。", 20)
    local_kg_id = asyncio.run(kg_mod.build_kg(
        Task("build_kg"), [doc["id"]], {"id": "llm-guard"}, {"id": "emb-guard"}, "守卫建图"))["kg_id"]

    # 2) 打桩同步：走真实 sync_ragflow，落盘快照 + 投影（不碰网络）
    class Client:
        def list_documents(self, dataset_id):
            return [{"id": "up-guard-1", "name": "守卫同步.txt", "update_time": "1",
                     "status": "done", "chunk_count": 1}]

        def list_chunks(self, dataset_id, document_id):
            return [{"id": "up-guard-c1", "content": "同步分块正文"}]

        def kb_structure(self, dataset_id):
            return {"templates": [{"entities": []}]}

        def get_version(self):
            return "1.0.0-rc3"

        def alteration(self, dataset_id):
            return {}

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *a, **k: Client())
    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources("ds-guard", "conn-guard")  # 入口要求持有租约
    sync_report = asyncio.run(rf.sync_ragflow(
        task, "ds-guard", "http://127.0.0.1:9380", "key", connection_id="conn-guard"))
    ragflow_kg_id = sync_report["kg_id"]

    for kg_id in (local_kg_id, ragflow_kg_id):
        data = json.loads((config.DATA_DIR / "kg" / f"{kg_id}.json").read_text(encoding="utf-8"))
        for node in data["nodes"]:
            props = node.get("properties") or {}
            if _kind(node) != "chunk":
                continue
            assert props.get("doc_id"), f"{kg_id} 的分块缺 doc_id"
            assert props.get("source_type") in allowed_types, props.get("source_type")
            assert "doc_name" in props
            assert props["doc_id"].startswith(doc_id_prefixes), props["doc_id"]

    # data/kg/**（含断点快照）：只断言旧字段不出现。断点快照是阶段中间产物，
    # 染色统一在完成出口做，所以它们允许暂时没有 doc_id。
    for path in (config.DATA_DIR / "kg").rglob("*.json"):
        _scan_stale_keys(path)

    # 3) 测试集样本：出处里的文档 id 也必须是新口径（doc-/rf-/ext-），不能再是节点 id
    doc_node = Node(type=NodeType.DOCUMENT, properties={"doc_id": "doc-abc12345"})
    chunk_node = Node(type=NodeType.CHUNK, properties={
        "page_content": "正文", "doc_id": "doc-abc12345", "source_chunk_id": "rc-1"})
    kg = KnowledgeGraph(nodes=[doc_node, chunk_node],
                        relationships=[Relationship(type="child", source=doc_node, target=chunk_node)])
    sample = {"index": 0}
    attach_provenance([sample], {0: [str(chunk_node.id)]}, kg, {})

    ts_id = storage.new_id("ts")
    storage.ts_write(ts_id, {"id": ts_id, "kg_id": ragflow_kg_id, "samples": [sample]})
    storage.ts_upsert({"id": ts_id, "name": "守卫",
                       "path": str(config.DATA_DIR / "testset" / f"{ts_id}.json"),
                       "kg_id": ragflow_kg_id, "created_at": storage.now_iso()})

    for path in (config.DATA_DIR / "testset").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):  # 索引文件是数组，没有样本
            continue
        for row in payload.get("samples") or []:
            assert not _stale_keys(row)
            for doc_id in row.get("source_document_ids") or []:
                assert doc_id.startswith(doc_id_prefixes), doc_id
