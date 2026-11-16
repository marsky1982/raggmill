"""F1 回归：前端所有写请求必须经 api.ts 的统一出口（带写令牌）。

背景：导出曾用裸 fetch 发 POST，没带 X-RAGAS-Write-Token；用 start.ps1 启动（启动器总会设置令牌）
时界面 CSV/JSONL 导出全部 403。这里做两件事：
1. 静态扫描 frontend/src：除 api.ts 外不允许出现 fetch(（读请求请用 get，写请求用 post/patch/put/del/postForBlob）；
2. 契约：设置令牌后，导出不带令牌 403、带令牌 200。
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_only_api_ts_calls_fetch():
    offenders = []
    for path in FRONTEND_SRC.rglob("*.ts*"):
        if path.name == "api.ts":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?<![\w.])fetch\(", line):
                offenders.append(f"{path.relative_to(FRONTEND_SRC)}:{lineno}")
    assert not offenders, f"请改用 api.ts 的出口（get/post/postForBlob…）：{offenders}"


def test_api_ts_write_header_matches_backend():
    """令牌头名字在前后端各写一份，这里锁住两者一致。"""
    api_ts = (FRONTEND_SRC / "api.ts").read_text(encoding="utf-8")
    main_py = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    front = re.search(r'"(X-RAGAS-Write-Token)"', api_ts)
    back = re.search(r'headers\.get\("([^"]+)"', main_py[main_py.index("_local_write_guard"):])
    assert front and back
    assert front.group(1).lower() == back.group(1).lower()


def test_export_requires_write_token(monkeypatch):
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    monkeypatch.setenv("RAGAS_STUDIO_WRITE_TOKEN", "test-token")
    with TestClient(app) as client:
        ts_path = storage.ts_write("ts-f1export", {
            "id": "ts-f1export", "name": "导出回归", "kg_id": "kg-x",
            "samples": [{"index": 0, "user_input": "问", "reference": "答",
                         "reference_contexts": ["上下文"], "synthesizer_name": "SingleHopSpecificQuery"}],
        })
        storage.ts_upsert({"id": "ts-f1export", "path": ts_path, "name": "导出回归"})
        body = {"format": "jsonl", "scope": "all"}
        missing = client.post("/api/testset/ts-f1export/export", json=body,
                              headers={"origin": "http://127.0.0.1:5173"})
        ok = client.post("/api/testset/ts-f1export/export", json=body,
                         headers={"origin": "http://127.0.0.1:5173", "x-ragas-write-token": "test-token"})
    assert missing.status_code == 403
    assert ok.status_code == 200, ok.text
    assert "问" in ok.content.decode("utf-8")
