"""路由冒烟测试：补齐此前测试里一次都没出现过的 25 个 HTTP 路径。

每个路径至少验证一条成功路径和一条错误码，长任务入口把真正干活的协程换成桩，
不连 LLM、不连 RAGFlow、0 token。覆盖统计口径：从 OpenAPI 列出全部路径，在测试源码里找字面路径。
"""
from __future__ import annotations

import time

import pytest
from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _wait(client, task_id: str, timeout: float = 10.0) -> dict:
    """轮询任务到终态（任务协程跑在 TestClient 的事件循环线程里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/tasks/{task_id}").json()
        if view["status"] in TERMINAL:
            return view
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束")


def _presets() -> tuple[str, str]:
    from app import storage

    llm = storage.save_preset({"kind": "llm", "name": "smoke-llm", "base_url": "http://x", "model": "m", "api_key": "k"})
    emb = storage.save_preset({"kind": "embedding", "name": "smoke-emb", "base_url": "http://x", "model": "e",
                               "api_key": "k"})
    return llm["id"], emb["id"]


def _save_kg(kg_id: str) -> None:
    """最小可用 KG：1 篇文档 + 3 个带实体与摘要向量的分块，块间有 entities_overlap 边。"""
    from app import config, storage

    kg = KnowledgeGraph()
    doc = Node(type=NodeType.DOCUMENT, properties={
        "page_content": "doc", "document_metadata": {"filename": "a.md"},
        "summary": "摘要", "summary_embedding": [0.1] * 8})
    kg.add(doc)
    prev = None
    for i in range(3):
        c = Node(type=NodeType.CHUNK, properties={"page_content": "字" * 300, "entities": [f"实体{i}", "共同"],
                                                    "themes": ["主题"]})
        kg.add(c)
        kg.add(Relationship(type="child", source=doc, target=c))
        if prev is not None:
            kg.add(Relationship(type="entities_overlap", source=prev, target=c, bidirectional=True,
                                properties={"entities_overlap_score": 0.5, "overlapped_items": [["共同", "共同"]]}))
        prev = c
    path = config.DATA_DIR / "kg" / f"{kg_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    kg.save(str(path))
    storage.kg_upsert({"id": kg_id, "name": kg_id, "kg_path": str(path), "mode": "ragas",
                       "created_at": storage.now_iso()})


def _save_testset(ts_id: str, kg_id: str) -> None:
    from app import storage

    data = {"id": ts_id, "name": ts_id, "kg_id": kg_id, "created_at": storage.now_iso(),
            "samples": [{"index": 0, "user_input": "问", "reference": "答", "reference_contexts": ["上下文"],
                         "synthesizer_name": "single_hop_specific_query_synthesizer", "adopted": False}]}
    path = storage.ts_write(ts_id, data)
    storage.ts_upsert({k: v for k, v in data.items() if k != "samples"} | {"path": path, "count": 1})


# ---------- /api/presets ----------

def test_preset_set_default(client):
    from app import storage

    a = storage.save_preset({"kind": "llm", "name": "smoke-def-a", "base_url": "http://x", "model": "m", "api_key": "k"})
    b = storage.save_preset({"kind": "llm", "name": "smoke-def-b", "base_url": "http://x", "model": "m", "api_key": "k"})
    bid = b["id"]
    resp = client.post(f"/api/presets/{bid}/set-default", params={"kind": "llm"})
    assert resp.status_code == 200 and resp.json()["preset"]["is_default"] is True
    assert storage.get_preset(a["id"])["is_default"] is False
    assert client.post(f"/api/presets/{bid}/set-default", params={"kind": "embedding"}).status_code == 404
    assert client.post(f"/api/presets/{bid}/set-default", params={"kind": "bogus"}).status_code == 400


def test_preset_connectivity_test(client, monkeypatch):
    import langchain_openai

    llm_id, emb_id = _presets()
    assert client.post("/api/presets/preset-nope/test").status_code == 404

    class FakeChat:
        def __init__(self, **kw):
            pass

        async def ainvoke(self, _messages):
            return "pong"

    class FailingEmb:
        def __init__(self, **kw):
            pass

        async def aembed_query(self, _text):
            raise RuntimeError("401 bad key")

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", FailingEmb)
    assert client.post(f"/api/presets/{llm_id}/test").json()["ok"] is True
    body = client.post(f"/api/presets/{emb_id}/test").json()
    assert body["ok"] is False and "401 bad key" in body["detail"]


# ---------- /api/documents ----------

def test_documents_estimate(client):
    from app import storage

    doc = storage.add_document("smoke-短文.md", "短", 3, overwrite=True)
    body = client.post("/api/documents/estimate", json={"document_ids": [doc["id"]]}).json()
    assert body["per_file"] == [{"name": "smoke-短文.md", "est_tokens": doc["est_tokens"]}]
    assert body["bin"] == "short" and "过短" in body["warning"]
    storage.remove_document(doc["id"])


def test_documents_folder_import(client, tmp_path):
    from app import storage

    (tmp_path / "a.md").write_text("# 甲\n" + "内容" * 80, encoding="utf-8")
    (tmp_path / "b.txt").write_text("乙" * 200, encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF")  # 不支持的类型被跳过
    resp = client.post("/api/documents/folder", json={"path": str(tmp_path), "glob": "*"})
    assert resp.status_code == 200
    names = sorted(d["name"] for d in resp.json())
    assert [n.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for n in names] == ["a.md", "b.txt"]
    # 再导一次：同名默认拒绝
    assert client.post("/api/documents/folder", json={"path": str(tmp_path), "glob": "*"}).status_code == 409
    assert client.post("/api/documents/folder", json={"path": str(tmp_path / "nope")}).status_code == 400
    for d in resp.json():
        storage.remove_document(d["id"])
    # folder 导入的 name 是用户原文件路径：删索引绝不能动原文件
    assert (tmp_path / "a.md").exists()


# ---------- /api/evaluations ----------

def test_evaluation_metrics(client):
    rows = client.get("/api/evaluations/metrics").json()
    keys = {r["metric_key"] for r in rows}
    assert {"faithfulness", "answer_relevancy", "context_precision", "context_recall", "answer_correctness"} <= keys
    assert all("label" in r and "required" in r for r in rows)


# ---------- /api/kg ----------

def test_kg_checkpoints_list_and_delete(client):
    from app import storage
    from app.tasks.manager import manager

    storage.ckpt_save("smoke-run", {"run_id": "smoke-run", "status": "failed", "stage_index": 1,
                                    "total_stages": 3, "updated_at": storage.now_iso()})
    assert "smoke-run" in {m["run_id"] for m in client.get("/api/kg/checkpoints").json()}
    busy = manager.create("build_kg", ("run:smoke-run",))  # 仍有任务占用该断点
    try:
        assert client.delete("/api/kg/checkpoints/smoke-run").status_code == 409
    finally:
        busy.status = "canceled"
    assert client.delete("/api/kg/checkpoints/smoke-run").json() == {"ok": True}
    assert "smoke-run" not in {m["run_id"] for m in client.get("/api/kg/checkpoints").json()}
    assert client.delete("/api/kg/checkpoints/smoke-run").status_code == 404


def test_kg_generate_validates_and_starts_task(client, monkeypatch):
    from app.routers import knowledge_graph

    llm_id, emb_id = _presets()
    seen: dict = {}

    async def fake_build_kg(task, document_ids, llm, emb, name, **kw):
        seen.update(document_ids=document_ids, llm=llm["id"], run_id=kw["run_id"])
        return {"kg_id": "kg-fake"}

    monkeypatch.setattr(knowledge_graph, "build_kg", fake_build_kg)
    base = {"document_ids": ["doc-1"], "llm_preset_id": llm_id, "embedding_preset_id": emb_id}
    assert client.post("/api/kg/generate", json=base | {"llm_preset_id": emb_id}).status_code == 400
    assert client.post("/api/kg/generate", json=base | {"run_id": "../escape"}).status_code == 400
    resp = client.post("/api/kg/generate", json=base | {"run_id": "kgrun-smoke"})
    assert resp.status_code == 200
    view = _wait(client, resp.json()["task_id"])
    assert view["status"] == "succeeded" and view["result"] == {"kg_id": "kg-fake"}
    assert seen == {"document_ids": ["doc-1"], "llm": llm_id, "run_id": "kgrun-smoke"}


# ---------- /api/tasks 及 kg / testset 别名：stream 与 cancel ----------

def _start_task(client, monkeypatch, coro_factory) -> str:
    from app.routers import knowledge_graph

    llm_id, emb_id = _presets()
    monkeypatch.setattr(knowledge_graph, "build_kg", coro_factory)
    return client.post("/api/kg/generate", json={"document_ids": [], "llm_preset_id": llm_id,
                                                 "embedding_preset_id": emb_id}).json()["task_id"]


@pytest.mark.parametrize("url", ["/api/tasks/{}/stream", "/api/kg/tasks/{}/stream", "/api/testset/tasks/{}/stream"])
def test_task_stream_emits_done_for_finished_task(client, monkeypatch, url):
    async def quick(*_a, **_k):
        return {"ok": True}

    task_id = _start_task(client, monkeypatch, quick)
    _wait(client, task_id)
    body = client.get(url.format(task_id)).text
    assert "event: done" in body and '"status": "succeeded"' in body


def test_task_stream_unknown_task_is_404(client):
    assert client.get("/api/tasks/task-nope/stream").status_code == 404


@pytest.mark.parametrize("url", ["/api/tasks/{}/cancel", "/api/kg/tasks/{}/cancel", "/api/testset/tasks/{}/cancel"])
def test_task_cancel(client, monkeypatch, url):
    import asyncio

    async def slow(*_a, **_k):
        await asyncio.sleep(30)

    task_id = _start_task(client, monkeypatch, slow)
    deadline = time.time() + 5
    while client.get(f"/api/tasks/{task_id}").json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    assert client.post(url.format(task_id)).json() == {"ok": True}
    assert _wait(client, task_id)["status"] == "canceled"
    # 已是终态：再取消返回 ok=False；不存在的任务 404
    assert client.post(url.format(task_id)).json() == {"ok": False}
    assert client.post(url.format("task-nope")).status_code == 404


# ---------- /api/ragflow ----------

def test_ragflow_probe_route(client, monkeypatch):
    from app import storage
    from app.routers import ragflow as ragflow_router
    from app.tasks.manager import manager

    assert client.post("/api/ragflow/connections/conn-nope/datasets/kb1/probe", json={}).status_code == 404
    storage.ragflow_save_connection({"id": "conn-smoke", "name": "冒烟", "base_url": "http://ragflow.example:9380",
                                     "api_key": "k"})
    seen: dict = {}

    async def fake_probe(task, connection_id, dataset_id, base_url, api_key, sample_docs=None):
        seen.update(connection_id=connection_id, dataset_id=dataset_id, sample_docs=sample_docs)
        return {"verdict": "ready"}

    monkeypatch.setattr(ragflow_router, "probe_ragflow", fake_probe)
    resp = client.post("/api/ragflow/connections/conn-smoke/datasets/kb1/probe", json={"sample_docs": 2})
    assert resp.status_code == 202
    assert resp.json()["source_id"] == storage.ragflow_source_id("conn-smoke", "kb1")
    assert _wait(client, resp.json()["task_id"])["result"] == {"verdict": "ready"}
    assert seen == {"connection_id": "conn-smoke", "dataset_id": "kb1", "sample_docs": 2}
    busy = manager.create("ragflow_probe", (resp.json()["source_id"],))
    try:
        again = client.post("/api/ragflow/connections/conn-smoke/datasets/kb1/probe", json={})
        assert again.status_code == 409 and again.json()["error_code"] == "probe_in_progress"
    finally:
        busy.status = "canceled"


def test_ragflow_legacy_config_and_snapshot(client):
    from app import storage

    assert client.get("/api/ragflow/datasets/kb-smoke/config").status_code == 404
    missing = client.get("/api/ragflow/datasets/kb-smoke/snapshot")
    assert missing.status_code == 404
    # 与新布局同一套错误码：前端只在 snapshot_not_found 时显示「还没同步过」，其它错误报读取失败
    assert missing.json()["error_code"] == "snapshot_not_found"
    storage.ragflow_save_config("kb-smoke", {"base_url": "http://ragflow.example:9380", "api_key": "secret"})
    cfg = client.get("/api/ragflow/datasets/kb-smoke/config").json()
    assert cfg["base_url"] == "http://ragflow.example:9380"
    assert "api_key" not in cfg and "api_key_enc" not in cfg  # 不把密钥下发给浏览器
    path = storage.ragflow_snapshot_write("kb-smoke", "snap-1", {
        "fetched_at": "2026-11-05T00:00:00", "source_hash": "h",
        "documents": {"d1": {}}, "chunks": {"c1": {}, "c2": {}}})
    storage.ragflow_snapshot_commit("kb-smoke", "snap-1", path, "kg-proj", "v1")
    snap = client.get("/api/ragflow/datasets/kb-smoke/snapshot").json()
    assert snap["snapshot_id"] == "snap-1" and snap["projection_kg_id"] == "kg-proj"
    assert snap["document_count"] == 1 and snap["chunk_count"] == 2


def test_ragflow_entity_settings_routes(client):
    """实体规范化 / LLM 消歧开关：连接式与旧布局两套入口的读、写、预估。"""
    from app import storage

    llm_id, _ = _presets()
    storage.ragflow_save_connection({"id": "conn-er", "name": "消歧", "base_url": "http://ragflow.example:9380",
                                     "api_key": "k"})
    settings_url = "/api/ragflow/connections/conn-er/datasets/kb-er/settings"

    view = client.get(settings_url)
    assert view.status_code == 200
    assert view.json()["entity_resolution"] == "disabled"
    assert view.json()["pending_rebuild"] is True  # 还没有投影

    # 非法模式 / 缺预设都拒绝
    assert client.put(settings_url, json={"entity_resolution": "oops"}).status_code == 400
    assert client.put(settings_url, json={"entity_resolution": "hybrid"}).status_code == 400
    saved = client.put(settings_url, json={"entity_resolution": "hybrid", "llm_preset_id": llm_id})
    assert saved.status_code == 200 and saved.json()["entity_resolution"] == "hybrid"
    assert saved.json()["config_fingerprint"].startswith("sha256:")

    estimate = client.post("/api/ragflow/connections/conn-er/datasets/kb-er/settings/estimate",
                           json={"entity_resolution": "hybrid"})
    assert estimate.status_code == 200
    assert estimate.json()["available"] is False  # 无快照：不为了预估去拉 RAGFlow

    legacy_url = "/api/ragflow/datasets/kb-er/settings"
    assert client.get(legacy_url).json()["entity_resolution"] == "disabled"
    assert client.put(legacy_url, json={"entity_resolution": "disabled"}).status_code == 200


def test_ragflow_legacy_sync_route(client, monkeypatch):
    from app.routers import ragflow as ragflow_router

    assert client.post("/api/ragflow/sync", json={"dataset_id": "kb-new",
                                                  "base_url": "http://ragflow.example:9380"}).status_code == 400
    assert client.post("/api/ragflow/sync", json={"dataset_id": "kb-new", "base_url": "ftp://x",
                                                  "api_key": "k"}).status_code == 400

    async def fake_sync(task, dataset_id, base_url, api_key, name, full_refresh, timeout, *, policy=None):
        return {"dataset_id": dataset_id, "full_refresh": full_refresh, "policy": policy.model_dump()}

    monkeypatch.setattr(ragflow_router, "sync_ragflow", fake_sync)
    resp = client.post("/api/ragflow/sync", json={
        "dataset_id": "kb-new", "base_url": "http://ragflow.example:9380", "api_key": "k",
        "full_refresh": True, "llm_timeout": 60, "max_retries": 2, "max_workers": 3,
    })
    assert resp.status_code == 200 and resp.json()["mode"] == "manual"
    result = _wait(client, resp.json()["task_id"])["result"]
    assert result["dataset_id"] == "kb-new" and result["full_refresh"] is True
    # 同步请求体继承 GenerationPolicy：执行参数能从请求传到任务
    assert (result["policy"]["llm_timeout"], result["policy"]["max_retries"],
            result["policy"]["max_workers"]) == (60, 2, 3)


# ---------- /api/testset ----------

def test_testset_languages(client):
    values = {o["value"] for o in client.get("/api/testset/languages").json()}
    assert "zh" in values and all(isinstance(v, str) for v in values)


def test_testset_capabilities_health_and_estimate(client):
    assert client.get("/api/testset/capabilities/kg-nope").status_code == 404
    assert client.get("/api/testset/health/kg-nope").status_code == 404
    assert client.post("/api/testset/estimate-cost", json={"kg_id": "kg-nope"}).status_code == 400
    _save_kg("kg-smoke")
    caps = client.get("/api/testset/capabilities/kg-smoke").json()
    assert set(caps["synthesizers"]) >= {"single_hop_specific", "multi_hop_abstract", "multi_hop_specific"}
    health = client.get("/api/testset/health/kg-smoke")
    assert health.status_code == 200 and health.json()
    est = client.post("/api/testset/estimate-cost", json={
        "kg_id": "kg-smoke", "testset_size": 4, "use_default_distribution": False,
        "query_distribution": {"single_hop_specific": 1.0, "multi_hop_abstract": 0.0, "multi_hop_specific": 0.0}})
    assert est.status_code == 200
    assert est.json()["breakdown_tokens"]


def test_testset_generate_external_rejects_kg_json_without_node_ids(client, monkeypatch):
    """外部 KG 的节点 id 是文档身份锚点：缺失/重复必须同步返回 400，而不是起一个必然失败的任务。"""
    from app import storage
    from app.routers import testset as testset_router

    llm_id, emb_id = _presets()
    base = {"mode": "kg_json", "llm_preset_id": llm_id, "embedding_preset_id": emb_id,
            "source_name": "外部图"}

    def _must_not_create(*args, **kwargs):
        raise AssertionError("校验失败时不应创建任务")

    # 方案 §4.2 PR1 验收：400 时不创建任务、不新增 KG 条目
    monkeypatch.setattr(testset_router.manager, "create", _must_not_create)
    kg_count_before = len(storage.kg_list())

    missing = base | {"kg_json": {"nodes": [{"type": "chunk", "properties": {"page_content": "x"}}]}}
    resp = client.post("/api/testset/generate-external", json=missing)
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "external_kg_invalid"
    assert "缺少 id" in resp.json()["detail"]

    duplicated = base | {"kg_json": {"nodes": [
        {"id": "n1", "type": "chunk", "properties": {"page_content": "x"}},
        {"id": "n1", "type": "chunk", "properties": {"page_content": "y"}},
    ]}}
    resp = client.post("/api/testset/generate-external", json=duplicated)
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "external_kg_invalid"
    assert "重复" in resp.json()["detail"]

    # 节点不是对象（字符串等）：与缺 id 同出口，必须是 400 而不是 500
    not_object = base | {"kg_json": {"nodes": [
        {"id": "n1", "type": "chunk", "properties": {"page_content": "x"}},
        "字符串节点",
    ]}}
    resp = client.post("/api/testset/generate-external", json=not_object)
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "external_kg_invalid"
    assert "不是对象" in resp.json()["detail"]

    assert len(storage.kg_list()) == kg_count_before


def test_testset_generate_external(client, monkeypatch):
    from app.routers import testset as testset_router

    llm_id, emb_id = _presets()
    base = {"mode": "documents", "documents": [{"name": "a.md", "text": "正文"}],
            "llm_preset_id": llm_id, "embedding_preset_id": emb_id}
    assert client.post("/api/testset/generate-external", json=base | {"mode": "pdf"}).status_code == 422
    assert client.post("/api/testset/generate-external", json=base | {"testset_size": 0}).status_code == 422
    assert client.post("/api/testset/generate-external", json=base | {"llm_preset_id": emb_id}).status_code == 400

    async def fake_external(task, mode, kg_json, chunks, documents, *args, **kwargs):
        return {"mode": mode, "documents": len(documents)}

    monkeypatch.setattr(testset_router, "generate_testset_external", fake_external)
    resp = client.post("/api/testset/generate-external", json=base)
    assert resp.status_code == 200
    assert _wait(client, resp.json()["task_id"])["result"] == {"mode": "documents", "documents": 1}


def test_testset_provenance_and_patch_sample(client):
    from app import storage

    assert client.get("/api/testset/ts-nope/provenance").status_code == 404
    assert client.patch("/api/testset/ts-nope/samples/0", json={"adopted": True}).status_code == 404
    _save_kg("kg-smoke-ts")
    _save_testset("ts-smoke", "kg-smoke-ts")
    prov = client.get("/api/testset/ts-smoke/provenance")
    assert prov.status_code == 200 and prov.json()
    resp = client.patch("/api/testset/ts-smoke/samples/0", json={"adopted": True})
    assert resp.json() == {"ok": True, "index": 0, "adopted": True}
    assert storage.ts_read("ts-smoke")["samples"][0]["adopted"] is True
    assert client.patch("/api/testset/ts-smoke/samples/9", json={"adopted": True}).status_code == 404


# ---------- 守门：每个 HTTP 路径都至少在一条测试里出现 ----------

def test_every_route_appears_in_some_test():
    """新增路由却不写测试时这里会失败。

    口径：从 OpenAPI 列出全部路径，`{参数}` 按任意片段匹配，在 tests/ 源码里找字面路径。
    这只保证「至少被请求过一次」，不代表契约已测全。
    """
    import re
    from pathlib import Path

    from app.main import app

    source = "\n".join(p.read_text(encoding="utf-8") for p in Path(__file__).parent.glob("test_*.py"))
    missing = []
    for path in sorted(app.openapi()["paths"]):
        # re.escape 把 {task_id} 变成 \{task_id\}；换成「任意一段非分隔字符」
        pattern = re.sub(r"\\\{[^}]+\\\}", r"[^\"'/ ]+", re.escape(path))
        if not re.search(pattern, source):
            missing.append(path)
    assert not missing, f"以下路由在 tests/ 里没有任何请求，请补冒烟测试：{missing}"


def test_kg_checkpoint_delete_while_canceling_is_deferred(client, monkeypatch):
    """取消后立刻删断点：任务还在收尾时返回 202「收尾后自动删除」，不再 409；收尾后断点被删掉。"""
    import time as _time

    from app import storage
    from app.routers import knowledge_graph
    from app.tasks.manager import manager

    monkeypatch.setattr(knowledge_graph, "_DEFERRED_DELETE_POLL", 0.05)
    storage.ckpt_save("smoke-cancel", {"run_id": "smoke-cancel", "status": "running", "stage_index": 1,
                                       "total_stages": 3, "updated_at": storage.now_iso()})
    busy = manager.create("build_kg", ("run:smoke-cancel",))
    busy.status = "running"
    try:
        # 没有取消、仍在正常运行：照旧 409
        assert client.delete("/api/kg/checkpoints/smoke-cancel").status_code == 409
        busy.cancel_event.set()  # 已发取消，但在跑的那一步还没结束
        resp = client.delete("/api/kg/checkpoints/smoke-cancel")
        assert resp.status_code == 202 and resp.json()["deferred"] is True
        assert client.delete("/api/kg/checkpoints/smoke-cancel").status_code == 202  # 重复请求幂等
        assert "smoke-cancel" in {m["run_id"] for m in client.get("/api/kg/checkpoints").json()}
    finally:
        busy.status = "canceled"  # 收尾完成
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline and storage.ckpt_read_meta("smoke-cancel") is not None:
        _time.sleep(0.05)
    assert storage.ckpt_read_meta("smoke-cancel") is None
    assert client.delete("/api/kg/checkpoints/smoke-cancel").status_code == 404


def test_preset_name_is_trimmed_and_blank_name_rejected(client):
    """名称 / 模型 / 地址去首尾空格；只有空格的名称 422；带空格的同名也算重复（409）。"""
    from app import storage

    base = {"kind": "llm", "base_url": " http://127.0.0.1:1/v1 ", "model": " m-trim ", "api_key": "sk-x"}
    assert client.post("/api/presets", json=base | {"name": "   "}).status_code == 422
    created = client.post("/api/presets", json=base | {"name": "  去空格预设  "}).json()
    try:
        assert created["name"] == "去空格预设"
        assert created["model"] == "m-trim" and created["base_url"] == "http://127.0.0.1:1/v1"
        assert client.post("/api/presets", json=base | {"name": "去空格预设 "}).status_code == 409
    finally:
        storage.delete_preset(created["id"])
