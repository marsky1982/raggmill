"""N5 / N7：历史（legacy）连接与删除连接的加固。

- N5：legacy 连接只能同步它原来的知识库（否则覆盖别的库旧目录的 config.json，跨实例互毁）；
- N7①：legacy 连接删除后不会在下次启动被重新合成，旧 config.json 里的密钥一并抹掉；
- N7②：删除被降级为软删时，墓碑里不留密文；
- N7③：连接测试回写结果时，若连接已被删除则不写（不再「复活」一条无名连接）。
不连真实 RAGFlow。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _legacy(dataset: str) -> str:
    from app import storage

    storage.ragflow_save_config(dataset, {"base_url": "http://instance-a:9380", "api_key": "legacy-key"})
    storage._synthesize_legacy_connections()
    cid = storage.legacy_connection_id(dataset)
    assert storage.ragflow_get_connection(cid)["legacy"] is True
    return cid


def test_legacy_connection_cannot_sync_other_dataset():
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    cid = _legacy("kb-legacy-own")
    storage.ragflow_save_config("kb-other", {"base_url": "http://instance-b:9380", "api_key": "b-key"})
    with TestClient(app) as client:
        resp = client.post(f"/api/ragflow/connections/{cid}/datasets/kb-other/sync", json={})
    assert resp.status_code == 400
    assert "只能同步它原来的知识库" in resp.json()["detail"]
    # 别的库旧目录的凭据没有被改写
    assert storage.ragflow_get_config("kb-other")["base_url"] == "http://instance-b:9380"


def test_deleted_legacy_connection_stays_deleted_and_key_is_removed():
    from app import storage

    cid = _legacy("kb-legacy-del")
    assert storage.ragflow_delete_connection(cid) is True
    assert storage.ragflow_get_connection(cid) is None
    assert cid not in {c["id"] for c in storage.ragflow_list_connections()}
    # 模拟重启：再合成一次也不会把它带回来
    storage._synthesize_legacy_connections()
    assert storage.ragflow_get_connection(cid) is None
    # 旧 config.json 里的同一份密钥也被抹掉，旧 /sync 无法再直接用它
    assert storage.ragflow_get_config("kb-legacy-del")["api_key"] == ""
    tomb = json.loads(storage.ragflow_connection_path(cid).read_text(encoding="utf-8"))
    assert tomb["_deleted"] is True and "api_key_enc" not in tomb and "api_key_mask" not in tomb


def test_soft_deleted_connection_keeps_no_ciphertext(monkeypatch):
    from app import storage

    storage.ragflow_save_connection({"id": "conn-softdel", "name": "软删", "base_url": "http://x:9380",
                                     "api_key": "secret-key"})
    monkeypatch.setattr(storage.base, "_soft_delete", lambda _p: None)  # 模拟沙箱拦截物理删除
    assert storage.ragflow_delete_connection("conn-softdel") is True
    left = json.loads(storage.ragflow_connection_path("conn-softdel").read_text(encoding="utf-8"))
    assert left["_deleted"] is True
    assert "api_key_enc" not in left and "api_key_mask" not in left
    assert storage.ragflow_get_connection("conn-softdel") is None


def test_test_result_not_written_back_after_delete():
    from app import storage

    storage.ragflow_save_connection({"id": "conn-testdel", "name": "测完删", "base_url": "http://x:9380",
                                     "api_key": "k"})
    assert storage.ragflow_record_connection_test("conn-testdel", "0.27.1", {"verdict": "ready"}) is True
    assert storage.ragflow_get_connection("conn-testdel")["last_test"]["verdict"] == "ready"
    storage.ragflow_delete_connection("conn-testdel")
    assert storage.ragflow_record_connection_test("conn-testdel", "0.27.1", {"verdict": "ready"}) is False
    assert storage.ragflow_get_connection("conn-testdel") is None
    assert "conn-testdel" not in {c["id"] for c in storage.ragflow_list_connections()}
