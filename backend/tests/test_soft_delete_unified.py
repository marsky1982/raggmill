"""删除三件套统一实现：每类实体 × 物理删除 / 沙箱降级软删 / 读路径过滤 / 启动清理。

用 monkeypatch 让指定目录下的 `Path.unlink` 抛 OSError，模拟沙箱拦截物理删除；
不拦截整个 DATA_DIR，否则 _write_json / ckpt_save 的临时文件清理也会被拦。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _block_unlink(monkeypatch, base: Path) -> None:
    """让 base 目录（含子目录）下的 unlink 全部抛 OSError。"""
    original = Path.unlink
    target = Path(base).resolve()

    def fake(self, *args, **kwargs):
        p = Path(self)
        try:
            inside = p.resolve().is_relative_to(target)
        except OSError:  # 文件已不存在时 resolve 仍可用，这里只兜底
            inside = False
        if inside:
            raise OSError("sandbox blocked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake)


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class _FakeKg:
    """ckpt_save 只有在传入带 save() 的图对象时才会写不可变快照。"""

    def save(self, path: Path) -> None:
        Path(path).write_text('{"nodes": [], "edges": []}', encoding="utf-8")


# ---------- 预设 ----------

def test_preset_physical_delete_then_soft_delete():
    from app import storage

    storage.init_storage()
    item = storage.save_preset({"kind": "llm", "name": "删除夹具-物理", "base_url": "", "model": "m"})
    assert storage.delete_preset(item["id"]) is True
    assert storage.get_preset(item["id"]) is None
    assert not (storage.PRESETS_DIR / "llm" / f"{item['id']}.json").exists()


def test_preset_soft_delete_when_unlink_blocked(monkeypatch):
    from app import storage

    storage.init_storage()
    item = storage.save_preset({"kind": "llm", "name": "删除夹具-软删", "base_url": "", "model": "m"})
    path = storage.PRESETS_DIR / "llm" / f"{item['id']}.json"
    _block_unlink(monkeypatch, storage.PRESETS_DIR)

    assert storage.delete_preset(item["id"]) is True
    assert path.exists()
    assert _read(path)["_deleted"] is True
    # 读路径统一过滤：get_preset / list_presets 都看不到
    assert storage.get_preset(item["id"]) is None
    assert all(row["id"] != item["id"] for row in storage.list_presets("llm"))


# ---------- 文档正文制品 / 上传文件 ----------

def test_document_text_artifact_cleared_when_unlink_blocked(monkeypatch):
    from app import storage

    storage.init_storage()
    doc = storage.add_document("删除夹具-正文.txt", "正文内容" * 20, 160)
    texts_dir = storage.DOC_INDEX.parent / "texts"
    artifacts = [p for p in texts_dir.glob(f"{doc['id']}-*.txt")]
    assert artifacts, "正文制品应已落盘"

    _block_unlink(monkeypatch, texts_dir)
    assert storage.remove_document(doc["id"]) is True
    # 索引里看不到
    assert doc["id"] not in storage.document_snapshot()
    # 制品仍在（删不掉），但内容已被清空，不再泄露原文
    for p in artifacts:
        assert p.exists() and p.stat().st_size == 0


def test_upload_file_soft_delete_records_trash(monkeypatch):
    from app import storage

    storage.init_storage()
    name = "删除夹具-上传.txt"
    upload = storage.UPLOADS_DIR / name
    upload.write_bytes("上传内容".encode("utf-8"))
    doc = storage.add_document(name, "上传内容", 12, source="disk")

    _block_unlink(monkeypatch, storage.UPLOADS_DIR)
    assert storage.remove_document(doc["id"]) is True
    assert upload.exists()
    assert name in json.loads((storage.UPLOADS_DIR / ".trashed.json").read_text(encoding="utf-8"))
    # 重启扫描不会把墓碑里的文件复活成文档
    storage.init_storage()
    assert name not in {d["name"] for d in storage.document_snapshot().values()}


# ---------- KG / 断点 / 测试集 ----------

def test_kg_soft_delete_and_read_filter(monkeypatch):
    from app import storage

    storage.init_storage()
    kg_id = storage.new_id("kg")
    entry = {"id": kg_id, "name": "删除夹具-KG", "mode": "ragas", "node_count": 0, "edge_count": 0,
             "created_at": storage.now_iso()}
    entry["kg_path"] = storage.kg_write_json(kg_id, {"nodes": [], "edges": []})
    storage.kg_upsert(entry)
    path = Path(entry["kg_path"])

    _block_unlink(monkeypatch, storage.KG_DIR)
    assert storage.kg_delete(kg_id) is True
    assert _read(path)["_deleted"] is True
    assert storage.kg_get(kg_id) is None
    assert storage.kg_read_json(kg_id) is None


def test_checkpoint_soft_delete_keeps_snapshot_for_purge(monkeypatch):
    from app import storage

    storage.init_storage()
    run_id = "ckpt-delete-fixture"
    storage.ckpt_save(run_id, {"run_id": run_id, "status": "failed", "stage_index": 1, "total_stages": 3,
                               "doc_ids": [], "updated_at": storage.now_iso()}, _FakeKg())
    snapshot = list(storage.KG_CKPT_DIR.glob("*.snapshot-*.json"))
    assert snapshot, "断点应带不可变快照文件"

    _block_unlink(monkeypatch, storage.KG_CKPT_DIR)
    assert storage.ckpt_delete(run_id) is True
    assert storage.ckpt_read_meta(run_id) is None
    assert storage.ckpt_meta_path(run_id).exists()
    assert _read(storage.ckpt_meta_path(run_id))["_deleted"] is True


def test_testset_soft_delete_and_read_filter(monkeypatch):
    from app import storage

    storage.init_storage()
    ts_id = storage.new_id("ts")
    path = Path(storage.ts_write(ts_id, {"id": ts_id, "name": "删除夹具-测试集", "samples": []}))
    storage.ts_upsert({"id": ts_id, "name": "删除夹具-测试集", "path": str(path),
                       "created_at": storage.now_iso()})

    _block_unlink(monkeypatch, storage.TESTSET_DIR)
    assert storage.ts_delete(ts_id) is True
    assert _read(path)["_deleted"] is True
    assert storage.ts_get(ts_id) is None
    assert storage.ts_read(ts_id) is None


# ---------- 评估 ----------

def test_evaluation_soft_delete_and_read_filter(monkeypatch):
    from app import storage

    storage.init_storage()
    storage.evaluation_write("dataset-delete-fixture", {"id": "dataset-delete-fixture", "name": "删除夹具-评估",
                                                        "status": "draft", "metric_keys": [], "sample_count": 0})
    _block_unlink(monkeypatch, storage.EVALUATION_DIR)

    assert storage.evaluation_delete("dataset-delete-fixture") is True
    path = storage.EVALUATION_DIR / "dataset-delete-fixture.json"
    assert _read(path)["_deleted"] is True
    assert storage.evaluation_read("dataset-delete-fixture") is None
    assert all(row["id"] != "dataset-delete-fixture" for row in storage.evaluation_list())


# ---------- RAGFlow 连接 / 数据源 ----------

def test_connection_tombstone_has_no_secret(monkeypatch):
    from app import storage

    storage.init_storage()
    conn = storage.ragflow_save_connection({"name": "删除夹具-连接", "base_url": "http://127.0.0.1:1",
                                            "api_key": "ragflow-fixture-key"})
    path = storage.ragflow_connection_path(conn["id"])
    assert "api_key_enc" in _read(path)

    _block_unlink(monkeypatch, storage.RAGFLOW_CONNECTIONS_DIR)
    assert storage.ragflow_delete_connection(conn["id"]) is True
    tomb = _read(path)
    assert tomb["_deleted"] is True
    assert "api_key_enc" not in tomb and "api_key_mask" not in tomb
    assert storage.ragflow_get_connection(conn["id"]) is None
    assert all(item["id"] != conn["id"] for item in storage.ragflow_list_connections())
    # 连接删了但数据源/投影保留
    assert storage.ragflow_delete_connection(conn["id"]) is False


def test_legacy_connection_keeps_tombstone():
    from app import storage

    storage.init_storage()
    conn = storage.ragflow_save_connection({"name": "删除夹具-legacy", "base_url": "http://127.0.0.1:1",
                                            "api_key": "ragflow-legacy-key", "legacy": True,
                                            "dataset_id": "legacy-delete-fixture"})
    path = storage.ragflow_connection_path(conn["id"])
    assert storage.ragflow_delete_connection(conn["id"]) is True
    # legacy 连接不物理删：留墓碑，否则下次启动会从旧 config.json 重新合成
    assert path.exists()
    tomb = _read(path)
    assert tomb["_deleted"] is True and tomb["legacy"] is True
    assert "api_key_enc" not in tomb


def test_source_pointer_soft_delete_hidden_from_read(monkeypatch):
    from app import storage

    storage.init_storage()
    root = storage.ragflow_source_dir("conn-delete-fixture", "ds-delete-fixture", create=True)
    storage.ragflow_snapshot_pointer_write("ds-delete-fixture",
                                           {"dataset_id": "ds-delete-fixture", "snapshot_id": "snap-1"},
                                           source_dir=root)
    pointer = root / "current.json"
    source_id = storage.ragflow_source_id("conn-delete-fixture", "ds-delete-fixture")
    assert storage.ragflow_source_snapshot(source_id)[1] is not None

    _block_unlink(monkeypatch, root)
    storage._soft_delete(pointer)
    assert _read(pointer)["_deleted"] is True
    assert storage.ragflow_source_snapshot(source_id) == (None, None)


# ---------- 启动清理 ----------

def test_init_storage_purges_all_soft_deleted_entities():
    from app import storage

    storage.init_storage()
    # 预设 / KG / 断点（含快照）/ 测试集 / 评估 / 非 legacy 连接 / 数据源指针
    preset = storage.save_preset({"kind": "llm", "name": "清理夹具", "base_url": "", "model": "m"})
    preset_path = storage.PRESETS_DIR / "llm" / f"{preset['id']}.json"
    data = _read(preset_path)
    data["_deleted"] = True
    preset_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    kg_id = "kg-purge-fixture"
    kg_path = Path(storage.kg_write_json(kg_id, {"_deleted": True, "nodes": [], "edges": []}))

    run_id = "ckpt-purge-fixture"
    storage.ckpt_save(run_id, {"run_id": run_id, "status": "failed", "stage_index": 1, "total_stages": 3,
                               "doc_ids": [], "updated_at": storage.now_iso()}, _FakeKg())
    meta_path = storage.ckpt_meta_path(run_id)
    meta = _read(meta_path)
    meta["_deleted"] = True
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    snapshots = list(storage.KG_CKPT_DIR.glob("*.snapshot-*.json"))
    assert snapshots

    ts_id = "ts-purge-fixture"
    ts_path = Path(storage.ts_write(ts_id, {"_deleted": True, "id": ts_id, "samples": []}))

    storage.evaluation_write("dataset-purge-fixture", {"_deleted": True, "id": "dataset-purge-fixture"})
    eval_path = storage.EVALUATION_DIR / "dataset-purge-fixture.json"

    conn = storage.ragflow_save_connection({"name": "清理夹具-连接", "base_url": "http://127.0.0.1:1",
                                            "api_key": "ragflow-purge-key"})
    conn_path = storage.ragflow_connection_path(conn["id"])
    tomb = _read(conn_path)
    tomb["_deleted"] = True
    conn_path.write_text(json.dumps(tomb, ensure_ascii=False), encoding="utf-8")

    legacy = storage.ragflow_save_connection({"name": "清理夹具-legacy", "base_url": "http://127.0.0.1:1",
                                              "api_key": "ragflow-legacy-key", "legacy": True,
                                              "dataset_id": "legacy-purge-fixture"})
    legacy_path = storage.ragflow_connection_path(legacy["id"])
    legacy_tomb = _read(legacy_path)
    legacy_tomb["_deleted"] = True
    legacy_path.write_text(json.dumps(legacy_tomb, ensure_ascii=False), encoding="utf-8")

    root = storage.ragflow_source_dir("conn-purge-fixture", "ds-purge-fixture", create=True)
    storage.ragflow_snapshot_pointer_write("ds-purge-fixture",
                                           {"_deleted": True, "dataset_id": "ds-purge-fixture"},
                                           source_dir=root)
    pointer = root / "current.json"

    storage.init_storage()

    assert not preset_path.exists()
    assert not kg_path.exists()
    assert not meta_path.exists()
    assert all(not p.exists() for p in snapshots)
    assert not ts_path.exists()
    assert not eval_path.exists()
    assert not conn_path.exists()
    assert not pointer.exists()
    # legacy 连接的墓碑必须保留（否则下次启动会从旧 config.json 重新合成）
    assert legacy_path.exists()


# ---------- 评估删除接口 ----------

def test_delete_evaluation_endpoint():
    from fastapi.testclient import TestClient
    from app import storage
    from app.main import app

    storage.init_storage()
    storage.evaluation_write("dataset-route-fixture", {"id": "dataset-route-fixture", "name": "接口夹具",
                                                      "status": "draft", "metric_keys": [], "sample_count": 0})
    with TestClient(app) as client:
        ok = client.delete("/api/evaluations/dataset-route-fixture")
        again = client.delete("/api/evaluations/dataset-route-fixture")
        illegal = client.delete("/api/evaluations/bad$id")
        missing = client.delete("/api/evaluations/dataset-missing-fixture")
    assert ok.status_code == 200 and ok.json()["deleted"] is True
    assert again.status_code == 404
    # 非法 id 在路径参数校验阶段就被拦下（422 统一错误体），不再进业务代码
    assert illegal.status_code == 422
    assert illegal.json()["error_code"] == "validation_error"
    assert illegal.json()["extra"]["errors"]
    assert missing.status_code == 404
