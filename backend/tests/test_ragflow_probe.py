""" S2 §9.1：知识库探测（verdict / 7 阶段 / 请求上限 / 无副作用 / 可取消）。

全部打桩，不连真实 RAGFlow、不调 LLM、不写快照或投影。
"""
from __future__ import annotations

import asyncio

import pytest

DATASET = "ds-1"


def _entity(with_source: bool) -> dict:
    row = {"name": "实体"}
    if with_source:
        row["source_chunk_ids"] = ["chunk-1"]
    return row


class FakeProbeClient:
    def __init__(self, docs, chunks, kb_data, graph_data):
        self.docs = docs
        self.chunks = chunks
        self.kb_data = kb_data
        self.graph_data = graph_data
        self.last_status: int | None = 200
        self.calls: list[str] = []

    def list_datasets_page(self, page, page_size):
        self.calls.append("datasets")
        return {"items": [{"id": DATASET, "name": "库"}], "total": 1,
                "has_more": False, "shape": "array", "upstream_status": 200}

    def list_documents_page(self, dataset_id, page, page_size):
        self.calls.append("documents")
        return {"items": self.docs, "total": len(self.docs),
                "has_more": False, "shape": "array", "upstream_status": 200}

    def list_chunks_page(self, dataset_id, document_id, page, page_size):
        self.calls.append(f"chunks:{document_id}")
        rows = self.chunks.get(document_id, [])
        return {"items": rows, "total": len(rows), "has_more": False,
                "shape": "array", "upstream_status": 200}

    def kb_structure(self, dataset_id):
        self.calls.append("kb_structure")
        return self.kb_data

    def document_structure(self, dataset_id, document_id):
        self.calls.append(f"graph:{document_id}")
        return self.graph_data.get(document_id, {"templates": []})


def _run_probe(monkeypatch, client, sample_docs=3):
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)
    task = Task("ragflow_probe")
    result = asyncio.run(rf.probe_ragflow(
        task, "conn-probe-1", DATASET, "http://127.0.0.1:9380", "key", sample_docs=sample_docs,
    ))
    return result, task


def _assert_stages(result: dict) -> None:
    from app.ragas_adapter import ragflow as rf
    stages = result["stages"]
    assert [stage["key"] for stage in stages] == list(rf._STAGE_KEYS)
    assert len(stages) == 7
    for stage in stages:
        assert stage["status"] in ("ok", "fail", "skipped")


def _docs(count: int, run: str = "DONE") -> list[dict]:
    return [{"id": f"doc{i}", "run": run, "progress": 1.0} for i in range(count)]


def _chunks_for(docs: list[dict]) -> dict[str, list[dict]]:
    return {doc["id"]: [{"id": f"{doc['id']}-c1", "content": "正文"}] for doc in docs}


def _kb(with_source: bool = True) -> dict:
    return {"templates": [{"entities": [_entity(with_source) for _ in range(4)],
                           "relationships": [{"source": "a", "target": "b"}]}]}


def _graphs(docs: list[dict], with_source: bool = True) -> dict[str, dict]:
    return {
        doc["id"]: {"templates": [{"entities": [_entity(with_source) for _ in range(2)]}]}
        for doc in docs
    }


def test_empty_library_is_graph_empty(monkeypatch):
    client = FakeProbeClient(docs=[], chunks={}, kb_data={"templates": []}, graph_data={})
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "graph_empty"
    assert result["stages"][3]["status"] == "fail"
    _assert_stages(result)
    assert len(client.calls) == 2  # 库列表 + 文档首页


def test_documents_without_graph_is_graph_empty(monkeypatch):
    docs = _docs(2, run="DONE")
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data={"templates": []}, graph_data={doc["id"]: {"templates": []} for doc in docs},
    )
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "graph_empty"
    assert result["stages"][5]["status"] == "fail"
    assert result["stages"][6]["status"] == "fail"
    _assert_stages(result)


def test_unfinished_documents_is_partial(monkeypatch):
    docs = _docs(2, run="DONE") + [{"id": "doc-run", "run": "RUNNING", "progress": 0.3}]
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data=_kb(), graph_data=_graphs(docs),
    )
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "partial"
    assert result["stages"][3]["status"] == "ok"
    # N6：未解析完的篇数要露出来，前端据此给「等解析结束」的建议
    assert result["counts"]["unfinished_documents"] == 1
    assert "其中 1 篇还没解析完" in result["stages"][3]["detail"]
    _assert_stages(result)


def test_rc3_unfinished_document_is_partial(monkeypatch):
    """rc3 真实形状：解析中的文档 status 仍是 "1"（启用），状态在 ingestion_status。"""
    docs = [dict(d, ingestion_status="COMPLETED", status="1") for d in _docs(1, run=None)]
    docs.append({"id": "doc-rc3-running", "ingestion_status": "RUNNING", "status": "1", "progress": 0.5})
    client = FakeProbeClient(docs=docs, chunks=_chunks_for(docs), kb_data=_kb(), graph_data=_graphs(docs))
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "partial" and result["counts"]["unfinished_documents"] == 1


def test_low_source_ratio_is_partial(monkeypatch):
    docs = _docs(2, run="DONE")
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data=_kb(), graph_data=_graphs(docs, with_source=False),
    )
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "partial"
    assert result["counts"]["entity_sources"] == 0
    _assert_stages(result)


def test_all_ok_is_ready(monkeypatch):
    docs = _docs(2, run="DONE")
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data=_kb(), graph_data=_graphs(docs),
    )
    result, _ = _run_probe(monkeypatch, client)
    assert result["verdict"] == "ready"
    # N14：结果自带连接 + 知识库，前端据此认领，刷新后恢复的任务不会挂到别的库下
    assert result["connection_id"] == "conn-probe-1" and result["dataset_id"] == DATASET
    assert all(stage["status"] == "ok" for stage in result["stages"])
    _assert_stages(result)


def test_request_budget_is_capped(monkeypatch):
    docs = _docs(30, run="DONE")
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data=_kb(), graph_data=_graphs(docs),
    )
    result, _ = _run_probe(monkeypatch, client, sample_docs=50)  # 上限 10 篇
    sample = 10
    assert len(client.calls) <= 3 + 2 * sample
    assert result["request_count"] == 3 + 2 * sample
    assert result["verdict"] == "ready"


def test_probe_has_no_snapshot_projection_or_llm_side_effects(monkeypatch):
    from app import storage

    docs = _docs(2, run="DONE")
    client = FakeProbeClient(
        docs=docs, chunks=_chunks_for(docs),
        kb_data=_kb(), graph_data=_graphs(docs),
    )
    source_dir = storage.ragflow_source_dir("conn-probe-1", DATASET)
    before_snapshot = storage.ragflow_snapshot_current(DATASET, source_dir=source_dir)
    before_kgs = len(storage.kg_list())
    result, _ = _run_probe(monkeypatch, client)
    after_snapshot = storage.ragflow_snapshot_current(DATASET, source_dir=source_dir)
    assert before_snapshot == after_snapshot == (None, None)
    assert len(storage.kg_list()) == before_kgs
    assert all(call in ("datasets", "documents", "kb_structure")
               or call.startswith(("chunks:", "graph:")) for call in client.calls)


def test_probe_is_cancellable(monkeypatch):
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    client = FakeProbeClient(
        docs=_docs(2, run="DONE"), chunks=_chunks_for(_docs(2, run="DONE")),
        kb_data=_kb(), graph_data=_graphs(_docs(2, run="DONE")),
    )
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)
    task = Task("ragflow_probe")
    task.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rf.probe_ragflow(
            task, "conn-probe-1", DATASET, "http://127.0.0.1:9380", "key", sample_docs=1,
        ))


@pytest.mark.parametrize("row,done", [
    ({"ingestion_status": "UNSTART", "status": "1", "progress": 0}, False),   # rc3：status=1 只是「启用」
    ({"ingestion_status": "RUNNING", "status": "1", "progress": 0.4}, False),
    ({"ingestion_status": "COMPLETED", "status": "1", "progress": 1}, True),   # rc3 实测终态
    ({"ingestion_status": "SCHEDULED", "status": "1", "progress": 0}, False),
    ({"ingestion_status": "FAILED", "status": "1"}, False),
    ({"ingestion_status": "STOPPED", "status": "1"}, False),
    ({"run": "DONE", "status": "1"}, True),                                    # v0.27.1 形状不回归
    ({"run": "UNSTART", "status": "1"}, False),
])
def test_doc_done_reads_rc3_ingestion_status(row, done):
    """rc3 把解析状态字段 run 改名为 ingestion_status；只看 status 会把未解析文档当成已完成。"""
    from app.ragas_adapter import ragflow as rf
    assert rf._doc_done(row) is done
