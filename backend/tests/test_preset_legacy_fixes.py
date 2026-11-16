"""预设密钥、kind 迁移和同名文档保护。"""
from __future__ import annotations

import pytest


def test_preset_update_keeps_default_and_increments_revision():
    from app import storage

    preset = storage.save_preset({
        "kind": "llm",
        "name": "judge",
        "api_key": "sk-old-key",
        "is_default": True,
    })
    updated = storage.save_preset({
        "id": preset["id"],
        "kind": "llm",
        "name": "judge-v2",
        "api_key": "sk-new-key",
    })

    assert updated["is_default"] is True
    assert updated["revision"] == 2
    assert storage.preset_plain_key(storage.get_preset(preset["id"])) == "sk-new-key"


def test_preset_kind_migration_removes_old_file():
    from app import storage

    preset = storage.save_preset({
        "kind": "llm",
        "name": "movable",
        "api_key": "sk-key",
    })
    old_file = storage._preset_file("llm", preset["id"])
    migrated = storage.save_preset({
        "id": preset["id"],
        "kind": "embedding",
        "name": "movable",
    })

    assert migrated["kind"] == "embedding"
    assert not old_file.exists()
    assert storage.get_preset(preset["id"])["kind"] == "embedding"


def test_invalid_secret_key_has_stable_error_code(monkeypatch):
    from app import crypto, storage

    monkeypatch.setattr(crypto, "decrypt", lambda token: (_ for _ in ()).throw(crypto.PresetKeyError("坏密钥")))
    with pytest.raises(crypto.PresetKeyError) as exc_info:
        storage.preset_plain_key({"api_key_enc": "broken"})
    assert exc_info.value.error_code == "preset_key_invalid"


def test_same_name_upload_requires_explicit_overwrite():
    from fastapi.testclient import TestClient
    from app.main import app
    from app import storage

    with TestClient(app) as client:
        first = client.post(
            "/api/documents/upload",
            files={"files": ("same.txt", b"first", "text/plain")},
        )
        duplicate = client.post(
            "/api/documents/upload",
            files={"files": ("same.txt", b"second", "text/plain")},
        )
        overwrite = client.post(
            "/api/documents/upload?overwrite=true",
            files={"files": ("same.txt", b"second", "text/plain")},
        )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert overwrite.status_code == 200
    assert storage.document_by_name("same.txt")["text"] == "second"
