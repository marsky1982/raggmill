"""D1A 第一条离线评估链路：字段门禁和 Context Recall@5 口径。"""
import asyncio
import json
import sys
import types

import pytest


def test_capability_report_separates_missing_inputs_and_context_limit():
    from app.ragas_adapter.evaluation import capability_report
    rows = [
        {"id": "a", "user_input": "问题", "response": "回答", "retrieved_contexts": ["c1", "c2"], "reference": "标准答案"},
        {"id": "b", "user_input": "只有问题", "response": "", "retrieved_contexts": [], "reference": None},
    ]
    result = capability_report(rows, ["faithfulness", "context_recall", "answer_correctness"])
    by_key = {item["metric_key"]: item for item in result["metrics"]}
    assert by_key["faithfulness"]["ready_count"] == 1
    assert by_key["context_recall"]["context_limit"] == 5
    assert by_key["answer_correctness"]["missing_fields"]["reference"] == 1
    assert by_key["faithfulness"]["empty_fields"]["retrieved_contexts"] == 1


def test_normalize_preserves_missing_vs_explicit_empty_contexts():
    from app.ragas_adapter.evaluation import normalize_observations
    rows = normalize_observations([
        {"id": "missing", "user_input": "q"},
        {"id": "empty", "user_input": "q", "retrieved_contexts": []},
    ])
    assert "retrieved_contexts" not in rows[0]
    assert rows[1]["retrieved_contexts"] == []


def test_preview_endpoint_reports_five_default_metrics():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.post("/api/evaluations/preview", json={
            "observations": [{"user_input": "q", "response": "a", "retrieved_contexts": ["c"], "reference": "r"}],
        })
    assert response.status_code == 200
    assert [item["metric_key"] for item in response.json()["metrics"]] == [
        "faithfulness", "answer_relevancy", "context_precision", "context_recall", "answer_correctness"
    ]


def test_preview_rejects_unknown_metric():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.post("/api/evaluations/preview", json={
            "metric_keys": ["recall_at_5"],
            "observations": [{"user_input": "q"}],
        })
    assert response.status_code == 400


def test_import_creates_offline_observation_dataset():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.post("/api/evaluations/import", json={
            "name": "夹具观察",
            "metric_keys": ["context_recall"],
            "observations": [{"id": "q1", "user_input": "q", "response": "a",
                               "retrieved_contexts": ["c1", "c2"], "reference": "r"}],
        })
    assert response.status_code == 200
    body = response.json()
    assert body["dataset_id"].startswith("dataset-")
    assert body["capability"]["metrics"][0]["context_limit"] == 5


def test_score_accepts_imported_dataset_id(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import storage
    import app.routers.evaluation as evaluation_router
    storage.ensure_data_dirs()
    dataset_id = "dataset-fixture"
    storage.evaluation_write(dataset_id, {
        "id": dataset_id, "kind": "offline_observations", "metric_keys": ["context_recall"],
        "observations": [{"id": "q1", "user_input": "q", "response": "a",
                           "retrieved_contexts": ["c"], "reference": "r"}],
    })
    storage.PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "get_preset", lambda preset_id: {
        "id": preset_id, "kind": "embedding" if preset_id == "emb" else "llm"
    })
    async def fake_score(*args, **kwargs):
        return {"evaluation_id": "eval-fixture", "status": "succeeded", "sample_count": 1,
                "scores": [], "usage": {}}
    monkeypatch.setattr(evaluation_router, "score_offline", fake_score)
    with TestClient(app) as client:
        response = client.post("/api/evaluations/score", json={
            "dataset_id": dataset_id,
            "llm_preset_id": "llm",
            "embedding_preset_id": "emb",
            "metric_keys": ["context_recall"],
        })
    assert response.status_code == 200
    assert response.json()["task_id"].startswith("task-")


def test_export_returns_jsonl_and_csv(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import storage
    evaluation_id = "eval-export-fixture"
    storage.evaluation_write(evaluation_id, {
        "id": evaluation_id, "metric_keys": ["context_recall", "answer_correctness"],
        "observations": [{"id": "q1", "user_input": "q", "response": "a",
                           "retrieved_contexts": ["c"], "reference": "r"}],
        "scores": [
            {"metric_key": "context_recall", "scores": [{"observation_id": "q1", "value": 0.8}]},
            {"metric_key": "answer_correctness", "scores": [{"observation_id": "q1", "value": 0.7}]},
        ],
    })
    with TestClient(app) as client:
        jsonl = client.get(f"/api/evaluations/{evaluation_id}/export?format=jsonl")
        csv = client.get(f"/api/evaluations/{evaluation_id}/export?format=csv")
    assert jsonl.status_code == 200
    assert jsonl.json()["context_recall"] == 0.8
    assert jsonl.json()["answer_correctness"] == 0.7
    assert "is_mock" not in jsonl.json()
    assert csv.status_code == 200
    assert "context_recall" in csv.text and "answer_correctness" in csv.text
    assert "is_mock" not in csv.text
    # 真实评分两个文件名都不带演示标记（含 ASCII 备用名）
    assert "-demo" not in jsonl.headers["content-disposition"]
    assert "-demo" not in csv.headers["content-disposition"]
    assert "演示数据" not in jsonl.headers["content-disposition"]


def test_export_marks_demo_scores():
    """演示评分导出必须能和真实分数区分：文件名后缀 + 每行 is_mock（与测试集导出同一规矩）。"""
    from urllib.parse import quote
    from fastapi.testclient import TestClient
    from app.main import app
    from app import storage
    evaluation_id = "eval-demo-export"
    storage.evaluation_write(evaluation_id, {
        "id": evaluation_id, "is_mock": True, "metric_keys": ["faithfulness"],
        "observations": [{"id": "q1", "user_input": "q", "response": "a",
                          "retrieved_contexts": ["c"], "reference": "r"}],
        "scores": [{"metric_key": "faithfulness", "scores": [{"observation_id": "q1", "value": 0.8}]}],
    })
    with TestClient(app) as client:
        jsonl = client.get(f"/api/evaluations/{evaluation_id}/export?format=jsonl")
        csv = client.get(f"/api/evaluations/{evaluation_id}/export?format=csv")

    assert jsonl.json()["is_mock"] is True
    assert quote("eval-demo-export-演示数据.jsonl", safe="") in jsonl.headers["content-disposition"]
    assert quote("eval-demo-export-演示数据.csv", safe="") in csv.headers["content-disposition"]
    # ASCII 备用文件名也要带标记：不认 filename* 的脚本 / 命令行下载工具据此仍能区分演示数据
    assert 'filename="eval-demo-export-demo.jsonl"' in jsonl.headers["content-disposition"]
    assert 'filename="eval-demo-export-demo.csv"' in csv.headers["content-disposition"]
    assert csv.text.splitlines()[0].endswith("is_mock")
    assert csv.text.splitlines()[-1].endswith("true")


def test_score_task_persists_policy_and_result_with_stubbed_adapter(monkeypatch):
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task
    from app import storage

    seen = {}
    async def fake_score(task, rows, keys, llm, emb, policy, name):
        seen["policy"] = policy
        return {"evaluation_id": "eval-test", "status": "succeeded", "sample_count": len(rows), "scores": [], "usage": {}}
    monkeypatch.setattr(adapter, "score_offline", fake_score)
    task = Task("offline_evaluation")
    asyncio.run(fake_score(task, [{"user_input": "q"}], ["faithfulness"], {}, {},
                           type("P", (), {"token_budget": 3})(), "x"))
    assert seen["policy"].token_budget == 3


def test_score_offline_controlled_five_metrics_mapping_and_recall_limit(monkeypatch):
    """不联网验证 D1A 编排，不把 HTTP 200 当成模型评分成功。"""
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    calls = []
    embedding_builds = []

    class FakeDataset:
        def __init__(self, rows):
            self.rows = rows

        @classmethod
        def from_list(cls, rows):
            return cls(rows)

    native_keys = {
        "faithfulness": "faithfulness",
        "answer_relevancy": "answer_relevancy",
        "context_precision": "llm_context_precision_with_reference",
        "context_recall": "context_recall",
        "answer_correctness": "answer_correctness",
    }

    def fake_evaluate(dataset, metrics, **kwargs):
        metric = metrics[0]
        calls.append((metric.key, dataset.rows))
        return types.SimpleNamespace(scores=[{native_keys[metric.key]: 0.8} for _ in dataset.rows])

    fake_ragas = types.ModuleType("ragas")
    fake_ragas.evaluate = fake_evaluate
    fake_schema = types.ModuleType("ragas.dataset_schema")
    fake_schema.EvaluationDataset = FakeDataset
    monkeypatch.setitem(sys.modules, "ragas", fake_ragas)
    monkeypatch.setitem(sys.modules, "ragas.dataset_schema", fake_schema)
    monkeypatch.setattr(adapter, "build_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "configure_run", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "build_embeddings", lambda *args, **kwargs: embedding_builds.append(True) or object())
    monkeypatch.setattr(adapter, "_metric_instance", lambda key, llm, embeddings, sync_embedding=None: types.SimpleNamespace(key=key))

    rows = [{"id": "q1", "user_input": "q", "response": "a",
             "retrieved_contexts": [f"c{i}" for i in range(6)], "reference": "r"}]
    task = Task("offline_evaluation")
    result = asyncio.run(adapter.score_offline(
        task, rows, list(adapter.DEFAULT_METRICS), {"kind": "llm"}, {"kind": "embedding"},
        GenerationPolicy(), "受控五指标"))

    assert result["status"] == "succeeded"
    assert [key for key, _ in calls] == list(adapter.DEFAULT_METRICS)
    recall_rows = next(rows for key, rows in calls if key == "context_recall")
    assert len(recall_rows[0]["retrieved_contexts"]) == 5
    assert len(embedding_builds) == 1
    saved = storage.evaluation_read(result["evaluation_id"])
    assert saved["status"] == "succeeded"
    assert all(item["value"] == 0.8 for metric in saved["scores"] for item in metric["scores"])


def test_score_offline_all_no_scores_is_failed_and_json_safe(monkeypatch):
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    class FakeDataset:
        @classmethod
        def from_list(cls, rows):
            return cls()

    def fake_evaluate(*args, **kwargs):
        return types.SimpleNamespace(scores=[{"faithfulness": float("nan")}])

    fake_ragas = types.ModuleType("ragas")
    fake_ragas.evaluate = fake_evaluate
    fake_schema = types.ModuleType("ragas.dataset_schema")
    fake_schema.EvaluationDataset = FakeDataset
    monkeypatch.setitem(sys.modules, "ragas", fake_ragas)
    monkeypatch.setitem(sys.modules, "ragas.dataset_schema", fake_schema)
    monkeypatch.setattr(adapter, "build_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "configure_run", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "_metric_instance", lambda *args, **kwargs: object())

    result = asyncio.run(adapter.score_offline(
        Task("offline_evaluation"),
        [{"id": "q1", "user_input": "q", "response": "a", "retrieved_contexts": ["c"]}],
        ["faithfulness"],
        {"kind": "llm"},
        None,
        GenerationPolicy(),
        "无分数回归",
    ))

    assert result["status"] == "failed"
    saved = storage.evaluation_read(result["evaluation_id"])
    assert saved["scores"][0]["status"] == "no_score"
    assert saved["scores"][0]["scores"][0]["raw"]["faithfulness"] is None
    assert saved["error"] == "所有可评估指标均未返回有效分数"


def _install_offline_stubs(monkeypatch, adapter, fake_evaluate):
    class FakeDataset:
        def __init__(self, rows):
            self.rows = rows

        @classmethod
        def from_list(cls, rows):
            return cls(rows)

    fake_ragas = types.ModuleType("ragas")
    fake_ragas.evaluate = fake_evaluate
    fake_schema = types.ModuleType("ragas.dataset_schema")
    fake_schema.EvaluationDataset = FakeDataset
    monkeypatch.setitem(sys.modules, "ragas", fake_ragas)
    monkeypatch.setitem(sys.modules, "ragas.dataset_schema", fake_schema)
    monkeypatch.setattr(adapter, "build_llm", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "configure_run", lambda *args, **kwargs: object())
    monkeypatch.setattr(adapter, "_metric_instance",
                        lambda key, llm, embeddings, sync_embedding=None: types.SimpleNamespace(key=key))


def _offline_row():
    return {"id": "q1", "user_input": "q", "response": "a",
            "retrieved_contexts": ["c"], "reference": "r"}


def _find_evaluation(storage, name):
    for path in sorted(storage.EVALUATION_DIR.glob("eval-*.json")):
        data = storage.evaluation_read(path.stem)
        if data and data.get("name") == name:
            return data
    raise AssertionError(f"未找到评估：{name}")


def test_score_offline_budget_exceeded_persists_terminal(monkeypatch):
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    task = Task("offline_evaluation")
    calls = []

    def fake_evaluate(dataset, metrics, **kwargs):
        key = metrics[0].key
        calls.append(key)
        task.generation_telemetry.budget_exceeded = True
        return types.SimpleNamespace(scores=[{adapter.RESULT_KEYS[key][0]: 0.9} for _ in dataset.rows])

    _install_offline_stubs(monkeypatch, adapter, fake_evaluate)
    result = asyncio.run(adapter.score_offline(
        task, [_offline_row()], ["faithfulness", "context_precision"],
        {"kind": "llm"}, None, GenerationPolicy(), "超预算终态"))

    assert calls == ["faithfulness"]
    assert result["status"] == "succeeded"
    saved = storage.evaluation_read(result["evaluation_id"])
    assert saved["status"] == "succeeded"
    assert saved["stop_reason"] == "budget_exceeded"
    assert saved["partial"] is True
    assert [item["metric_key"] for item in saved["scores"]] == ["faithfulness"]


def test_score_offline_cancel_persists_partial_then_raises(monkeypatch):
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    task = Task("offline_evaluation")
    calls = []

    def fake_evaluate(dataset, metrics, **kwargs):
        key = metrics[0].key
        calls.append(key)
        task.cancel_event.set()
        return types.SimpleNamespace(scores=[{adapter.RESULT_KEYS[key][0]: 0.8} for _ in dataset.rows])

    _install_offline_stubs(monkeypatch, adapter, fake_evaluate)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.score_offline(
            task, [_offline_row()], ["faithfulness", "context_precision"],
            {"kind": "llm"}, None, GenerationPolicy(), "取消终态"))

    assert calls == ["faithfulness"]
    saved = _find_evaluation(storage, "取消终态")
    assert saved["status"] == "canceled"
    assert saved["stop_reason"] == "canceled"
    assert saved["partial"] is True


def test_score_offline_failure_persists_then_raises(monkeypatch):
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    def fake_evaluate(*args, **kwargs):
        raise RuntimeError("模型炸了")

    _install_offline_stubs(monkeypatch, adapter, fake_evaluate)
    with pytest.raises(RuntimeError, match="模型炸了"):
        asyncio.run(adapter.score_offline(
            Task("offline_evaluation"), [_offline_row()], ["faithfulness"],
            {"kind": "llm"}, None, GenerationPolicy(), "失败终态"))

    saved = _find_evaluation(storage, "失败终态")
    assert saved["status"] == "failed"
    assert saved["stop_reason"] == "failed"
    assert saved["error"] == "模型炸了"


def test_score_offline_demo_mode_uses_deterministic_mock_scores(monkeypatch):
    """演示模式（RAGAS_AVAILABLE=False）：不接真 ragas，评分走确定性模拟并打 is_mock 标。"""
    from app import storage
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    monkeypatch.setattr(adapter, "RAGAS_AVAILABLE", False)
    monkeypatch.setattr(adapter, "build_llm", lambda *args, **kwargs: {"__demo__": True})

    result = asyncio.run(adapter.score_offline(
        Task("offline_evaluation"), [_offline_row()], ["faithfulness", "answer_relevancy"],
        {"kind": "llm"}, {"kind": "embedding"}, GenerationPolicy(), "演示模式"))

    assert result["status"] == "succeeded"
    saved = storage.evaluation_read(result["evaluation_id"])
    assert saved["is_mock"] is True
    assert all(item["status"] == "scored" for metric in saved["scores"] for item in metric["scores"])
    first = adapter._demo_scores("faithfulness", ["q1"])[0]["value"]
    assert saved["scores"][0]["scores"][0]["value"] == first
    assert 0.55 <= first < 0.96


def test_score_offline_demo_mode_duplicate_id_still_fails(monkeypatch):
    """演示模式下 observation id 重复仍走真实失败路径（stage 抛错 → 终态 failed）。"""
    from app.policies import GenerationPolicy
    from app.ragas_adapter import evaluation as adapter
    from app.tasks.manager import Task

    monkeypatch.setattr(adapter, "RAGAS_AVAILABLE", False)
    monkeypatch.setattr(adapter, "build_llm", lambda *args, **kwargs: {"__demo__": True})
    row = _offline_row()
    with pytest.raises(ValueError, match="observation id 重复"):
        asyncio.run(adapter.score_offline(
            Task("offline_evaluation"), [row, dict(row)], ["faithfulness"],
            {"kind": "llm"}, None, GenerationPolicy(), "演示重复 id"))