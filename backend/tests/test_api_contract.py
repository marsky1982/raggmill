"""PR10：稳定端点 2xx 响应模型契约。

三条护栏：
1. 稳定端点必须声明 response_model（OpenAPI 里有实体 schema），否则前端生成类型会退化；
2. 模型不得静默丢掉真实返回的键（数据驱动的条目开 extra="allow"）；
3. 含凭据的模型（预设 / RAGFlow 连接）永远不出现 api_key / api_key_enc。

注意：本模块**不在顶层 import app**（conftest 要先设 DATA_DIR），所有 app 相关导入放函数内；
也不把 `TestsetDetailOut` 直接绑到模块命名空间，避免被 pytest 当成测试类收集。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# 阶段 1 挂上 response_model 的端点（方法, 路径）。新增稳定端点时同步加到这里。
STABLE_ENDPOINTS: list[tuple[str, str]] = [
    ("get", "/api/presets"),
    ("post", "/api/presets"),
    ("put", "/api/presets/{preset_id}"),
    ("delete", "/api/presets/{preset_id}"),
    ("post", "/api/presets/{preset_id}/set-default"),
    ("post", "/api/presets/{preset_id}/test"),
    ("get", "/api/documents"),
    ("post", "/api/documents/upload"),
    ("post", "/api/documents/folder"),
    ("post", "/api/documents/estimate"),
    ("delete", "/api/documents/{doc_id}"),
    ("post", "/api/kg/generate"),
    ("get", "/api/kg"),
    ("get", "/api/kg/{kg_id}"),
    ("get", "/api/kg/{kg_id}/preview"),
    ("delete", "/api/kg/{kg_id}"),
    ("get", "/api/kg/checkpoints"),
    ("delete", "/api/kg/checkpoints/{run_id}"),
    ("post", "/api/testset/generate"),
    ("post", "/api/testset/generate-external"),
    ("get", "/api/testset"),
    ("get", "/api/testset/{testset_id}"),
    ("patch", "/api/testset/{testset_id}/samples/{index}"),
    ("post", "/api/testset/{testset_id}/regenerate"),
    ("delete", "/api/testset/{testset_id}"),
    ("get", "/api/testset/languages"),
    ("get", "/api/tasks"),
    ("get", "/api/tasks/{task_id}"),
    ("post", "/api/tasks/{task_id}/cancel"),
    ("get", "/api/ragflow/connections"),
    ("post", "/api/ragflow/connections"),
    ("get", "/api/ragflow/connections/{connection_id}"),
    ("patch", "/api/ragflow/connections/{connection_id}"),
    ("delete", "/api/ragflow/connections/{connection_id}"),
    ("get", "/api/ragflow/connections/{connection_id}/datasets"),
    ("post", "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/probe"),
    ("post", "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/sync"),
    ("get", "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/settings"),
    ("put", "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/settings"),
    ("post", "/api/ragflow/connections/{connection_id}/datasets/{dataset_id}/settings/estimate"),
    ("get", "/api/ragflow/sources/{source_id}/snapshot"),
    ("post", "/api/ragflow/sync"),
]


def _ok_schema(op: dict) -> dict | None:
    for code, resp in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
        if schema:
            return schema
    return None


def test_stable_endpoints_declare_response_model():
    from app.main import app

    spec = app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for method, path in STABLE_ENDPOINTS
        if not _ok_schema(spec["paths"][path][method])
    ]
    assert not missing, "以下稳定端点缺 response_model: " + ", ".join(missing)


def test_preset_model_never_carries_secrets():
    from app import schemas

    payload = {
        "id": "preset-llm-1", "kind": "llm", "name": "主模型",
        "provider": "openai_compatible", "base_url": "https://x/v1", "model": "m",
        "extra": {}, "created_at": "2026-11-13T00:00:00Z", "revision": 2,
        "is_default": True, "api_key_mask": "sk-****abcd",
        # 真实存储层会 pop 掉这两项；模型层再兜一次，防止将来漏 pop 就透传
        "api_key_enc": "gAAAA-secret", "api_key": "sk-live-secret",
    }
    out = schemas.PresetOut.model_validate(payload).model_dump(exclude_unset=True)
    assert out["api_key_mask"] == "sk-****abcd"
    assert "api_key_enc" not in out and "api_key" not in out


def test_connection_model_never_carries_secrets():
    from app import schemas

    payload = {
        "id": "conn-1", "name": "本地 RAGFlow", "base_url": "http://127.0.0.1:9380",
        "api_key_mask": "ragflow-****", "declared_version": "1.0.0",
        "probed_version": "1.0.0", "last_test": None, "legacy": False,
        "created_at": "2026-11-13T00:00:00Z", "updated_at": "2026-11-13T00:00:00Z",
        "api_key_enc": "gAAAA-secret", "api_key": "ragflow-secret",
    }
    out = schemas.RagflowConnectionOut.model_validate(payload).model_dump(exclude_unset=True)
    assert "api_key_enc" not in out and "api_key" not in out


def test_kg_entry_keeps_source_specific_fields():
    """投影 / 外部导入条目带本地 KG 没有的字段，模型必须原样保留。"""
    from app import schemas

    payload = {
        "id": "kg-1", "name": "投影", "created_at": "t", "source_files": [],
        "node_count": 9, "relationship_count": 23, "mode": "ragflow-projection",
        "source_id": "conn__ds", "source_dataset_id": "ds", "source_connection_id": "conn",
        "projection_config": {"entity_resolution": "rules"},
        "changed_chunk_ids": ["c1"],
    }
    out = schemas.KgEntryOut.model_validate(payload).model_dump(exclude_unset=True)
    assert out["source_id"] == "conn__ds"
    assert out["projection_config"] == {"entity_resolution": "rules"}
    assert out["changed_chunk_ids"] == ["c1"]


def test_sample_keeps_ragas_and_provenance_fields():
    from app import schemas

    sample = {
        "user_input": "q", "reference": "a", "reference_contexts": ["c"],
        "synthesizer_name": "single_hop_specific", "response": "", "adopted": True,
        "source_chunk_ids": ["n1"], "source_content_hashes": ["h1"],
        "source_document_ids": ["doc-1"], "kg_revision": "r3",
        "provenance_version": 2, "query_style": "MISSPELLED",
    }
    out = schemas.SampleOut.model_validate(sample).model_dump(exclude_unset=True)
    assert out["source_content_hashes"] == ["h1"]
    assert out["provenance_version"] == 2


def test_testset_detail_keeps_all_builder_keys():
    """路由构造的详情字典键必须全部出现在模型输出里（少一个就是静默丢字段）。"""
    from app import schemas

    detail = {
        "id": "ts-1", "name": "n", "created_at": "t", "kg_id": "kg-1", "total": 1,
        "groups": {"single_hop_specific": [{"user_input": "q", "reference": "a",
                                            "reference_contexts": ["c"], "response": "",
                                            "synthesizer_name": "s", "adopted": True,
                                            "extra_provenance": "keep-me"}]},
        "stats": {"single_hop_specific": 1},
        "mode": "ragas", "is_mock": False, "stale": False, "stale_sample_count": 0,
        "llm_preset_id": "p1", "llm_model": "m", "embedding_preset_id": "p2",
        "embedding_model": "e", "relation_property": None, "testset_size": 1,
        "stop_reason": "completed", "partial": False, "failed_samples": 0,
        "undelivered": None, "error": None, "usage": {"total_tokens": 10},
        "regeneration": None, "source_dataset_id": "ds", "source_connection_id": "conn",
        "source_unbound": False, "source_unbound_reason": None,
        "projection_missing": False, "source_projection_version": "v1",
        "source_projection_config": {"entity_resolution": "rules"},
    }
    out = schemas.TestsetDetailOut.model_validate(detail).model_dump(exclude_unset=True)
    assert set(out) == set(detail)
    sample_out = out["groups"]["single_hop_specific"][0]
    assert sample_out["extra_provenance"] == "keep-me"


def test_task_view_keeps_every_snapshot_key():
    from app import schemas

    view = {
        "task_id": "t1", "type": "kg_generate", "status": "running", "progress": 0.5,
        "stage": "extract", "logs": ["x"], "result": None, "error": None,
        "stop_reason": None, "created_at": "t", "usage": {"tokens": 1}, "run_id": "r1",
        "resources": ["kg:1"], "seq": 7, "workers_pending": 2, "policy": {"a": 1},
        "future_field": "keep-me",
    }
    out = schemas.TaskOut.model_validate(view).model_dump(exclude_unset=True)
    assert set(out) == set(view)


def test_testset_detail_serves_regeneration_over_http():
    """带补生成元数据的测试集详情：HTTP 200 且 7 个键原样返回。

    `RegenerationOut.from_testset_id` 是必填字段，落盘形状一旦对不上就会在响应校验处 500，
    所以这一条必须走真实 HTTP（`storage.ts_read` 级断言覆盖不到响应模型）。
    """
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    storage.init_storage()
    ts_id = storage.new_id("ts")
    regeneration = {
        "from_testset_id": "ts-from-old", "from_testset_name": "旧测试集",
        "replaced_indices": [0], "replaced_by_status": {"stale": 1},
        "kept": 0, "requested": 1, "regenerated": 1,
    }
    data = {
        "id": ts_id, "name": "局部补生成", "created_at": storage.now_iso(), "kg_id": "kg-1",
        "testset_size": 1, "mode": "ragas", "samples": [
            {"index": 0, "synthesizer_name": "single_hop_specific", "user_input": "q",
             "reference": "a", "reference_contexts": ["c"], "response": "", "adopted": False},
        ],
        "regeneration": regeneration,
        # 未声明的用量键必须保留（extra="allow"）
        "usage": {"total_tokens": 5, "budget_exceeded": True},
    }
    path = storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"}, "path": path, "count": 1})

    with TestClient(app) as client:
        resp = client.get(f"/api/testset/{ts_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["regeneration"] == regeneration
    assert set(body["regeneration"]) == set(regeneration)
    assert body["usage"]["budget_exceeded"] is True


@pytest.fixture(scope="module")
def export_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_export_is_deterministic(export_script, tmp_path):
    out_a, out_b = tmp_path / "a.json", tmp_path / "b.json"
    assert export_script.main(["--out", str(out_a)]) == 0
    assert export_script.main(["--out", str(out_b)]) == 0
    text_a = out_a.read_text(encoding="utf-8")
    assert text_a == out_b.read_text(encoding="utf-8")
    assert text_a.endswith("\n")
    spec = json.loads(text_a)
    assert "paths" in spec and spec["info"]["title"]


def test_openapi_export_reports_real_paths(export_script, tmp_path, capsys):
    out = tmp_path / "spec.json"
    assert export_script.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "OpenAPI 已写入" in printed and "paths=" in printed
