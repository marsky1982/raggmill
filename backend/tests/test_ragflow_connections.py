""" S1：RAGFlow 连接实体、密钥落盘与路径校验。

不连真实 RAGFlow；只验证 `storage` 层。测试模块顶层不得 import app
（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import json

import pytest


def _storage():
    from app import storage
    return storage


def test_connection_crud_never_returns_or_stores_plaintext_key():
    storage = _storage()
    conn = storage.ragflow_save_connection({
        "name": "实例A-连接",
        "base_url": "http://127.0.0.1:9380/",
        "api_key": "sk-secret-case-conn-3456",
        "declared_version": "0.27.1",
    })
    assert conn["id"].startswith("conn-")
    assert conn["base_url"] == "http://127.0.0.1:9380"  # 去尾斜杠
    assert conn["legacy"] is False
    # 响应只允许出现掩码，不得出现明文或密文
    assert "api_key" not in conn and "api_key_enc" not in conn
    assert conn["api_key_mask"].endswith("3456")

    raw = storage.ragflow_connection_path(conn["id"]).read_text(encoding="utf-8")
    assert "sk-secret-case-conn-3456" not in raw  # 落盘文件无明文 key
    assert json.loads(raw)["api_key_enc"]

    listed_ids = [c["id"] for c in storage.ragflow_list_connections()]
    assert conn["id"] in listed_ids
    assert storage.ragflow_get_connection(conn["id"])["name"] == "实例A-连接"

    # 更新未传 key：保留旧密文（与预设同一规则）
    updated = storage.ragflow_save_connection({
        "id": conn["id"], "name": "改名", "base_url": "http://127.0.0.1:9380",
    })
    assert updated["name"] == "改名"
    assert updated["api_key_mask"] == conn["api_key_mask"]

    assert storage.ragflow_delete_connection(conn["id"]) is True
    assert storage.ragflow_get_connection(conn["id"]) is None
    assert conn["id"] not in [c["id"] for c in storage.ragflow_list_connections()]


def test_find_connection_by_name_is_casefold_and_excludes_self():
    storage = _storage()
    conn = storage.ragflow_save_connection({
        "name": "生产 RAGFlow", "base_url": "http://prod.local:9380", "api_key": "k1",
    })
    assert storage.ragflow_find_connection_by_name("生产 ragflow")["id"] == conn["id"]
    assert storage.ragflow_find_connection_by_name("生产 RAGFLOW", exclude_id=conn["id"]) is None
    assert storage.ragflow_find_connection_by_name("") is None
    storage.ragflow_delete_connection(conn["id"])


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "a/b",
    "a b",
    "",
    ".",
    "..",
    "x" * 65,
    "知识库",
    "..\\windows",
])
def test_invalid_segments_are_rejected_everywhere(bad):
    """§3.3 的 9 个负向用例：dataset_id 与 connection_id 共用同一套白名单。"""
    storage = _storage()
    with pytest.raises(storage.UnsafePathError):
        storage.validate_ragflow_name(bad, "RAGFlow 知识库 ID")
    with pytest.raises(storage.UnsafePathError):
        storage.ragflow_source_id("conn-1a2b3c4d", bad)


def test_source_id_total_length_limit():
    storage = _storage()
    with pytest.raises(storage.UnsafePathError):
        # 64 + 2 + 64 = 130 > 120
        storage.ragflow_source_id("c" * 64, "d" * 64)
    assert storage.ragflow_source_id("conn-1a2b3c4d", "kb-abc123") == "conn-1a2b3c4d__kb-abc123"


def test_source_dir_is_not_created_until_requested():
    storage = _storage()
    d = storage.ragflow_source_dir("conn-1a2b3c4d", "kb-abc123")
    assert d.name == "conn-1a2b3c4d__kb-abc123"
    assert not d.exists()  # 读路径不留空目录
    created = storage.ragflow_source_dir("conn-1a2b3c4d", "kb-abc123", create=True)
    assert (created / "snapshots").is_dir()
