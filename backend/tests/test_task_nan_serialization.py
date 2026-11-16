"""非有限浮点（ragas 的 NaN 哨兵）不能把 /api/tasks 打成 500。

ragas 指标在「无法判定」时返回 np.nan（faithfulness 生成 0 条陈述、context_recall 分母为 0 等），
旧版本把这类值原样写进任务日记；FastAPI 拒绝序列化 NaN，整张列表就 500。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path


def _legacy_task_body(task_id: str) -> dict:
    """模拟旧版本落盘的脏任务：result.scores[].raw.* 与 usage 里都带 NaN。"""
    return {
        "task_id": task_id,
        "type": "offline_evaluation",
        "status": "succeeded",
        "progress": 1.0,
        "stage": "完成",
        "logs": [],
        "result": {
            "evaluation_id": "eval-legacy",
            "status": "succeeded",
            "scores": [{
                "metric_key": "faithfulness",
                "status": "no_score",
                "scores": [{"observation_id": "row-1", "value": None, "raw": {"faithfulness": float("nan")}}],
            }],
        },
        "usage": {"total_tokens": float("nan")},
        "error": None,
        "created_at": "2026-11-02T05:04:28+00:00",
        "resources": [],
        "seq": 1,
        "policy": {},
        "events": [["done", {"status": "succeeded", "seq": 1,
                             "result": {"scores": [{"raw": {"faithfulness": float("nan")}}]}}]],
        "finished_at": 1.0,
    }


def test_legacy_nan_task_is_readable_via_api():
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    from app.tasks.manager import TaskManager

    journal = config.DATA_DIR / "tasks.sqlite3"
    journal.parent.mkdir(parents=True, exist_ok=True)
    TaskManager().initialize(journal)  # 先建表，模拟真实库里已有的历史行
    body = _legacy_task_body("task-legacy-nan")
    with sqlite3.connect(journal) as db:
        # allow_nan=True 才能复现旧版本的落盘方式（NaN 字面量，不是合法 JSON）
        db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)",
                   (body["task_id"], json.dumps(body, ensure_ascii=False, allow_nan=True), 1.0))

    with TestClient(app) as client:
        response = client.get("/api/tasks")
        assert response.status_code == 200
        item = next(row for row in response.json() if row["task_id"] == "task-legacy-nan")
        assert item["result"]["scores"][0]["scores"][0]["raw"]["faithfulness"] is None
        assert item["usage"]["total_tokens"] is None

        detail = client.get("/api/tasks/task-legacy-nan")
        assert detail.status_code == 200
        assert detail.json()["result"]["scores"][0]["scores"][0]["raw"]["faithfulness"] is None


def test_new_task_result_never_writes_nan(tmp_path: Path):
    from app.tasks.manager import TaskManager

    journal = tmp_path / "tasks.sqlite3"
    manager = TaskManager()
    manager.initialize(journal)

    async def scenario():
        task = manager.create("offline_evaluation")

        async def work():
            return {
                "scores": [{"value": float("nan"), "raw": {"faithfulness": float("nan"), "ok": 0.5}}],
                "usage": {"total_tokens": float("inf")},
            }

        manager.run(task, work())
        await task._async_task
        return task

    task = asyncio.run(scenario())
    view = task.view()
    assert view["result"]["scores"][0]["value"] is None
    assert view["result"]["scores"][0]["raw"] == {"faithfulness": None, "ok": 0.5}
    assert view["result"]["usage"]["total_tokens"] is None
    json.dumps(view, ensure_ascii=False, allow_nan=False)  # 输出必须能被严格 JSON 序列化

    with sqlite3.connect(journal) as db:
        raw = db.execute("SELECT body FROM tasks WHERE id=?", (task.id,)).fetchone()[0]
    assert "NaN" not in raw and "Infinity" not in raw
    saved = json.loads(raw)
    assert saved["result"]["scores"][0]["raw"]["faithfulness"] is None
    assert saved["events"][-1][1]["status"] == "succeeded"


def test_legacy_evaluation_artifact_nan_is_sanitized_on_read():
    """同一根因的存量评估制品：读时兜底，接口不再 500。"""
    from app import storage

    evaluation_id = "eval-legacy-nan"
    storage.evaluation_write(evaluation_id, {
        "id": evaluation_id,
        "status": "succeeded",
        "scores": [{"metric_key": "faithfulness", "scores": [{"raw": {"faithfulness": float("nan")}}]}],
    })
    data = storage.evaluation_read(evaluation_id)
    assert data["scores"][0]["scores"][0]["raw"]["faithfulness"] is None
