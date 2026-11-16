""" S1：旧 config.json → legacy 连接合成（不物理迁移、旧文件零改动）。"""
from __future__ import annotations

import hashlib
import json


def _storage():
    from app import storage
    return storage


def test_init_storage_synthesizes_legacy_connection_without_touching_old_files():
    from app import config, crypto, storage

    dataset_id = "kb-legacy-case-conn"
    old_dir = config.RAGFLOW_DIR / dataset_id
    old_dir.mkdir(parents=True, exist_ok=True)
    old_config = {
        "dataset_id": dataset_id,
        "base_url": "http://10.0.0.8:9380",
        "api_key_enc": crypto.encrypt("legacy-secret"),
        "api_key_mask": crypto.mask("legacy-secret"),
        "updated_at": "2026-10-01T10:00:00",
    }
    cfg_path = old_dir / "config.json"
    cfg_path.write_bytes(json.dumps(old_config, ensure_ascii=False).encode("utf-8"))
    before_bytes = cfg_path.read_bytes()
    before_mtime = cfg_path.stat().st_mtime_ns
    before_names = sorted(p.name for p in old_dir.iterdir())

    storage.init_storage()

    # 旧文件内容与 mtime 一个字节都不许变
    assert cfg_path.read_bytes() == before_bytes
    assert cfg_path.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in old_dir.iterdir()) == before_names  # 旧目录无新增文件

    cid = "conn-legacy-" + hashlib.md5(dataset_id.encode("utf-8")).hexdigest()[:8]
    conn = storage.ragflow_get_connection(cid)
    assert conn is not None
    assert conn["legacy"] is True
    assert conn["dataset_id"] == dataset_id
    assert conn["base_url"] == "http://10.0.0.8:9380"
    assert conn["api_key_mask"] == old_config["api_key_mask"]
    assert "api_key_enc" not in conn

    # 幂等：再启动一次不会重复合成，也不会复活已软删的记录
    storage.init_storage()
    legacy_rows = [c for c in storage.ragflow_list_connections() if c["id"] == cid]
    assert len(legacy_rows) == 1


def test_legacy_connection_is_stable_and_does_not_touch_unrelated_configs():
    from app import config, storage

    dataset_id = "kb-legacy-second"
    old_dir = config.RAGFLOW_DIR / dataset_id
    old_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = old_dir / "config.json"
    cfg_path.write_bytes(b'{"dataset_id": "kb-legacy-second", "base_url": "http://10.0.0.9:9380"}')
    before = cfg_path.read_bytes()

    storage.init_storage()
    cid = storage.legacy_connection_id(dataset_id)
    assert storage.ragflow_get_connection(cid)["legacy"] is True
    # 无 api_key_enc 的旧配置也允许合成（mask 为空，不伪造凭据）
    assert "api_key_mask" not in storage.ragflow_get_connection(cid)
    assert cfg_path.read_bytes() == before
