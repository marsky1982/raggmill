"""预设编辑语义与同名冲突。

覆盖两件事：
1. `PUT /api/presets/{id}` 的 api_key 为空（None 或空串）时沿用原密钥，不清空；
2. 同 kind 下新建同名预设被 409 拦下（前端误走 POST 的兜底），跨 kind 同名允许。
"""
from __future__ import annotations


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_update_preset_with_empty_api_key_keeps_existing_secret():
    from app import storage

    with _client() as client:
        created = client.post("/api/presets", json={
            "kind": "llm", "name": "case-preset-keep-key", "model": "m1", "api_key": "sk-original",
        })
        assert created.status_code == 200
        preset_id = created.json()["id"]

        for blank in (None, ""):
            updated = client.put(f"/api/presets/{preset_id}", json={
                "kind": "llm", "name": "case-preset-keep-key", "model": "m2", "api_key": blank,
            })
            assert updated.status_code == 200
            assert updated.json()["model"] == "m2"
            assert "api_key_enc" not in updated.json()

    # 密钥密文保留，明文字段仍可解密出原值
    assert storage.preset_plain_key(storage.get_preset(preset_id)) == "sk-original"


def test_duplicate_name_same_kind_create_is_rejected():
    with _client() as client:
        first = client.post("/api/presets", json={
            "kind": "llm", "name": "case-preset-dup", "model": "m1", "api_key": "sk-a",
        })
        assert first.status_code == 200

        duplicate = client.post("/api/presets", json={
            "kind": "llm", "name": "  case-preset-dup  ", "model": "m2", "api_key": "sk-b",
        })
        assert duplicate.status_code == 409
        assert "同名预设" in duplicate.json()["detail"]

        renamed = client.post("/api/presets", json={
            "kind": "llm", "name": "case-preset-dup-renamed", "model": "m2", "api_key": "sk-b",
        })
        assert renamed.status_code == 200

        # 跨 kind 同名不在拦截范围
        other_kind = client.post("/api/presets", json={
            "kind": "embedding", "name": "case-preset-dup", "model": "e1", "api_key": "sk-c",
        })
        assert other_kind.status_code == 200


def test_update_preset_to_duplicate_name_is_allowed_for_legacy_copies():
    """PUT 不做查重：历史遗留的同名副本仍可正常编辑（不能把用户卡死在 409）。"""
    with _client() as client:
        left = client.post("/api/presets", json={
            "kind": "llm_testset", "name": "case-preset-legacy", "model": "m1", "api_key": "sk-a",
        })
        right = client.post("/api/presets", json={
            "kind": "llm_testset", "name": "case-preset-legacy-other", "model": "m2", "api_key": "sk-b",
        })
        assert left.status_code == 200 and right.status_code == 200

        renamed = client.put(f"/api/presets/{right.json()['id']}", json={
            "kind": "llm_testset", "name": "case-preset-legacy", "model": "m3",
        })
        assert renamed.status_code == 200
