"""第二批后端修复的回归。

G6（断点签名）在 test_reliability.py，（导出格式）在 test_export_roundtrip.py。
不连外部服务。
"""
from __future__ import annotations

import asyncio
import io

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _texts_dir():
    from app import storage

    return storage.DOC_INDEX.parent / "texts"


# ---------- S5：用户原文残留 ----------

def test_overwrite_and_delete_remove_old_text_artifacts():
    from app import storage

    doc = storage.add_document("s5-残留.md", "第一版原文", 10)
    first = {p.name for p in _texts_dir().glob(f"{doc['id']}-*.txt")}
    assert len(first) == 1
    storage.add_document("s5-残留.md", "第二版原文", 10, overwrite=True)
    second = {p.name for p in _texts_dir().glob(f"{doc['id']}-*.txt")}
    assert len(second) == 1 and second != first, "覆盖上传后旧原文必须被清掉"
    storage.remove_document(doc["id"])
    assert not list(_texts_dir().glob(f"{doc['id']}-*.txt")), "删除文档后原文必须被清掉"


def test_startup_cleans_historical_orphan_text_artifacts():
    from app import storage

    keep = storage.add_document("s5-保留.md", "仍在索引里的原文", 10)
    orphan = _texts_dir() / "doc-deadbeef-0000.txt"
    orphan.write_bytes("旧版本遗留的用户原文".encode("utf-8"))
    storage.init_storage()
    assert not orphan.exists()
    assert storage.DOCUMENTS[keep["id"]]["text"] == "仍在索引里的原文"
    storage.remove_document(keep["id"])


# ---------- G5：预算熔断时少记账 ----------

def test_canceled_inflight_call_marks_usage_incomplete():
    from app.ragas_adapter.telemetry import GenerationTelemetry

    tele = GenerationTelemetry(token_budget=100)
    tele.on_llm_start({}, ["x" * 250], run_id="a")
    tele.on_llm_start({}, ["y" * 500], run_id="b")
    tele.on_llm_error(asyncio.CancelledError(), run_id="a")
    snap = tele.snapshot()
    # a 被取消（已发出、结果未知），b 快照时仍在途：两者都可能已被计费
    assert snap["usage_complete"] is False
    assert snap["usage_unknown_calls"] == 2
    assert snap["est_unreported_input_tokens"] == 300  # (250 + 500) / 2.5


def test_rejected_call_is_not_counted_as_unknown_billing():
    from app.ragas_adapter.telemetry import GenerationTelemetry

    class AuthenticationError(Exception):
        pass

    tele = GenerationTelemetry()
    tele.on_llm_start({}, ["prompt"], run_id="a")
    tele.on_llm_error(AuthenticationError("401"), run_id="a")
    snap = tele.snapshot()
    assert snap["usage_complete"] is True and snap["usage_unknown_calls"] == 0
    tele.on_llm_start({}, ["prompt"], run_id="b")
    tele.on_llm_error(TimeoutError(), run_id="b")
    assert tele.snapshot()["usage_unknown_calls"] == 1


# ---------- 预设类型校验 / 同名文件 ----------

def _presets():
    from app import storage

    llm = storage.save_preset({"kind": "llm", "name": "b2-llm", "base_url": "http://x", "model": "m", "api_key": "k"})
    emb = storage.save_preset({"kind": "embedding", "name": "b2-emb", "base_url": "http://x", "model": "e",
                               "api_key": "k"})
    return llm["id"], emb["id"]


def test_generation_presets_rejects_swapped_kinds():
    from app import storage

    llm_id, emb_id = _presets()
    llm, emb = storage.generation_presets(llm_id, emb_id)
    assert llm["kind"] == "llm" and emb["kind"] == "embedding"
    with pytest.raises(ValueError, match="不是 LLM 预设"):
        storage.generation_presets(emb_id, emb_id)
    with pytest.raises(ValueError, match="不是 Embedding 预设"):
        storage.generation_presets(llm_id, llm_id)
    with pytest.raises(ValueError, match="不存在"):
        storage.generation_presets("preset-nope", emb_id)


def test_testset_generate_route_rejects_embedding_as_llm():
    from fastapi.testclient import TestClient

    from app.main import app

    _, emb_id = _presets()
    with TestClient(app) as client:
        resp = client.post("/api/testset/generate", json={
            "kg_id": "kg-none", "llm_preset_id": emb_id, "embedding_preset_id": emb_id,
            "testset_size": 1, "name": "x"})
    assert resp.status_code == 400
    assert "不是 LLM 预设" in resp.json()["detail"]


def test_upload_rejects_same_name_in_batch_without_writing():
    from fastapi.testclient import TestClient

    from app import storage
    from app.config import UPLOADS_DIR
    from app.main import app

    files = [("files", ("a/同名.md", io.BytesIO("甲".encode("utf-8")), "text/markdown")),
             ("files", ("b/同名.md", io.BytesIO("乙".encode("utf-8")), "text/markdown"))]
    with TestClient(app) as client:
        resp = client.post("/api/documents/upload", files=files)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "a/同名.md" in detail and "b/同名.md" in detail
    assert storage.document_by_name("同名.md") is None
    assert not (UPLOADS_DIR / "同名.md").exists()


def test_upload_checks_all_existing_names_before_writing():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    storage.add_document("已存在.md", "旧", 3, overwrite=True)
    files = [("files", ("新文件.md", io.BytesIO(b"new"), "text/markdown")),
             ("files", ("已存在.md", io.BytesIO(b"dup"), "text/markdown"))]
    with TestClient(app) as client:
        resp = client.post("/api/documents/upload", files=files)
    assert resp.status_code == 409 and "已存在.md" in resp.json()["detail"]
    # 排在前面的新文件也没有被写入（整批校验后才写）
    assert storage.document_by_name("新文件.md") is None


# ----------  ⑤：图谱预览裁剪 ----------

def test_preview_strips_vectors_and_caps_edges():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    nodes = [{"id": f"n{i}", "type": "chunk",
              "properties": {"page_content": f"块{i}", "summary_embedding": [0.1] * 64, "entities": ["甲", "乙"]}}
             for i in range(6)]
    rels = ([{"id": f"s{i}", "type": "entities_overlap", "source": "n0", "target": f"n{i}",
              "properties": {"entities_overlap_score": 0.5}} for i in range(1, 6)]
            + [{"id": "c1", "type": "next", "source": "n0", "target": "n1", "properties": {}}])
    path = storage.kg_write_json("kg-b2preview", {"nodes": nodes, "relationships": rels})
    storage.kg_upsert({"id": "kg-b2preview", "name": "预览", "kg_path": path, "mode": "ragas",
                       "created_at": storage.now_iso()})
    with TestClient(app) as client:
        body = client.get("/api/kg/kg-b2preview/preview", params={"max_edges": 3}).json()
    assert body["nodes"][0]["properties"]["summary_embedding"] == "向量[64]（预览已省略）"
    assert body["nodes"][0]["properties"]["entities"] == ["甲", "乙"]
    assert len(body["relationships"]) == 3
    assert body["relationships"][0]["type"] == "next", "结构边优先保留"
    assert body["stats"]["total_relationships"] == 6


# ----------  ⑥：列表不下发服务器本地路径 ----------

def test_testset_list_hides_local_path():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    storage.ts_upsert({"id": "ts-b2path", "name": "路径", "count": 0, "created_at": storage.now_iso(),
                       "path": "C:/secret/data/testset/ts-b2path.json", "partial": True,
                       "stop_reason": "canceled", "testset_size": 5})
    with TestClient(app) as client:
        rows = client.get("/api/testset").json()
    row = next(r for r in rows if r["id"] == "ts-b2path")
    assert "path" not in row
    # 列表徽章依赖的终态字段照常返回
    assert row["partial"] is True and row["stop_reason"] == "canceled" and row["testset_size"] == 5
