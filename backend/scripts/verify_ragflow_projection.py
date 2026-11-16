"""验证  的 RAGFlow 增量读取和 RAGAS 投影契约。

不访问真实 RAGFlow，也不写 data/；用于在接入真实部署前验证增量比较算法。
"""
from __future__ import annotations

from copy import deepcopy
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from app import storage
from app.ragas_adapter.ragflow import RAGFlowClient, RAGFlowError, annotate_samples, build_ragas_projection, fetch_snapshot, mark_testsets_stale
from app.ragas_adapter.testset import _ragflow_generation_fingerprint
from app.ragas_adapter.testset import default_distribution, generate_testset
from app.tasks.manager import Task


class FakeRAGFlow(RAGFlowClient):
    def __init__(self) -> None:
        self.documents = {
            "d1": {"id": "d1", "name": "文档一", "update_time": "1", "status": "1", "run": "3", "chunk_count": 2},
            "d2": {"id": "d2", "name": "文档二", "update_time": "1", "status": "1", "run": "3", "chunk_count": 1},
        }
        self.chunks = {
            "d1": [
                {"id": "c1", "content": "Alpha 的介绍"},
                {"id": "c2", "content": "文档一的补充"},
            ],
            "d2": [{"id": "c3", "content": "Alpha 的应用"}],
        }
        self.calls = {"documents": 0, "chunks": 0, "kb_structure": 0, "version": 0, "alteration": 0}
        self.fail_alteration = False

    def list_documents(self, dataset_id: str) -> list[dict]:
        self.calls["documents"] += 1
        return list(self.documents.values())

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        self.calls["chunks"] += 1
        return deepcopy(self.chunks[document_id])

    def kb_structure(self, dataset_id: str) -> dict:
        # 实体池走 KB 级端点（T3-3）：Alpha 跨文档语义归并后只有一行
        self.calls["kb_structure"] += 1
        return {"templates": [{"entities": [
            {"name": "Alpha", "source_chunk_ids": ["c1", "c3"]},
            {"name": "Beta", "source_chunk_ids": ["c2"]},
        ]}]}

    def get_version(self) -> str | None:
        self.calls["version"] += 1
        return "0.27.1"

    def alteration(self, dataset_id: str) -> dict:
        self.calls["alteration"] += 1
        if self.fail_alteration:
            raise RAGFlowError("simulated unavailable")
        return {"newly_uploaded_doc_ids": [], "removed_doc_ids": [], "involved_doc_ids": []}


def main() -> None:
    class PagingClient(RAGFlowClient):
        def __init__(self) -> None:
            self.pages = []

        def _get(self, path: str, params: dict | None = None) -> dict:
            self.pages.append(params)
            assert params and params["page_size"] == 100
            if path.endswith("/chunks"):
                return {"chunks": [{"id": f"c-{i}"} for i in range(100)]} if params["page"] == 1 else {"chunks": [{"id": "c-last"}]}
            return {"docs": [{"id": f"d-{i}"} for i in range(100)]} if params["page"] == 1 else {"docs": [{"id": "d-last"}]}

    paging = PagingClient()
    assert len(paging.list_documents("ds")) == 101
    assert len(paging.list_chunks("ds", "d")) == 101

    client = FakeRAGFlow()
    first, first_delta = fetch_snapshot(client, "ds", None)
    assert set(first_delta["added_chunk_ids"]) == {"c1", "c2", "c3"}
    assert client.calls["chunks"] == 2 and client.calls["kb_structure"] == 1
    assert first["ragflow_version"] == "0.27.1"

    before_reuse = dict(client.calls)
    second, second_delta = fetch_snapshot(client, "ds", first)
    assert not second_delta["changed"]
    assert client.calls["chunks"] == before_reuse["chunks"]
    # KB 级结构每次同步都读一次（实体口径的唯一来源），但未变化文档的 chunk 不重读
    assert client.calls["kb_structure"] == before_reuse["kb_structure"] + 1

    client.documents["d1"]["update_time"] = "2"
    client.chunks["d1"][1]["content"] = "文档一的更新补充"
    third, third_delta = fetch_snapshot(client, "ds", second)
    assert third_delta["changed_doc_ids"] == ["d1"]
    assert third_delta["changed_chunk_ids"] == ["c2"]
    assert client.calls["chunks"] == before_reuse["chunks"] + 1

    del client.documents["d2"]
    del client.chunks["d2"]
    fourth, fourth_delta = fetch_snapshot(client, "ds", third)
    assert fourth_delta["deleted_doc_ids"] == ["d2"]
    assert fourth_delta["deleted_chunk_ids"] == ["c3"]

    client.fail_alteration = True
    refreshed, refreshed_delta = fetch_snapshot(client, "ds", fourth, full_refresh=True)
    assert refreshed_delta["changed"] is False
    projection = build_ragas_projection(first, source_id="probe__ds")
    assert all("template_id" not in (node.get("properties") or {}) for node in projection["nodes"])
    assert any(edge["type"] == "entities_overlap" for edge in projection["relationships"])
    assert all(edge["type"] != "owns" for edge in projection["relationships"])
    samples = [
        {"reference_contexts": ["Alpha 的介绍"]},
        {"reference_contexts": ["<1-hop>\n\nAlpha 的介绍", "<2-hop>\n\nAlpha 的应用"]},
    ]
    annotate_samples(samples, projection, "version-1", "ds")
    assert samples[0]["source_chunk_ids"] == ["c1"]
    assert samples[1]["source_chunk_ids"] == ["c1", "c3"]
    source = {"source_hash": first["source_hash"], "projection_config": {"entity_resolution": "disabled"}}
    preset = {"id": "llm-1", "model": "test-model", "base_url": "https://example.invalid", "api_key": "secret"}
    fingerprint = _ragflow_generation_fingerprint(source, preset, preset, {"testset_size": 10})
    assert fingerprint == _ragflow_generation_fingerprint(source, {**preset, "api_key": "rotated"}, preset, {"testset_size": 10})
    assert fingerprint != _ragflow_generation_fingerprint(source, preset, preset, {"testset_size": 11})
    from ragas.testset.graph import KnowledgeGraph

    with TemporaryDirectory() as directory:
        path = Path(directory) / "projection.json"
        import json
        path.write_text(json.dumps(projection, ensure_ascii=False), encoding="utf-8")
        loaded = KnowledgeGraph.load(path)
        assert len(loaded.nodes) == len(projection["nodes"])
        assert len(loaded.relationships) == len(projection["relationships"])

        original_dir, original_index, original_kg_index = storage.TESTSET_DIR, storage.TS_INDEX, storage.KG_INDEX
        try:
            storage.TESTSET_DIR = Path(directory)
            storage.TS_INDEX = Path(directory) / "index.json"
            storage.KG_INDEX = Path(directory) / "kg-index.json"
            testset = {
                "id": "ts-test", "kg_id": "kg-first", "source_dataset_id": "ds", "stale": False,
                "samples": [
                    {"source_chunk_ids": ["c1"], "stale": False},
                    {"source_chunk_ids": ["c2"], "stale": False},
                    {"source_chunk_ids": ["c3"], "stale": False},
                ],
            }
            testset_path = storage.ts_write("ts-test", testset)
            storage.ts_upsert({"id": "ts-test", "path": testset_path})
            assert mark_testsets_stale("ds", "kg-first", third_delta) == ["ts-test"]
            updated = storage.ts_read("ts-test")
            assert [sample["stale"] for sample in updated["samples"]] == [False, True, False]
            assert mark_testsets_stale("ds", "kg-first", fourth_delta) == ["ts-test"]
            updated = storage.ts_read("ts-test")
            assert [sample["stale"] for sample in updated["samples"]] == [False, True, True]
            assert updated["stale_sample_count"] == 2

            generation_kg = {"id": "kg-cache", "mode": "ragflow-projection", "source_hash": first["source_hash"],
                             "projection_config": {"entity_resolution": "disabled"}}
            storage.kg_upsert(generation_kg)
            generation_params = {"testset_size": 10, "distribution": default_distribution(),
                                 "abstract_relation_property": "summary_similarity", "max_retries": 3,
                                 "llm_timeout": 180, "max_workers": 8, "token_budget": 0,
                                 "num_personas": 3, "language": ""}
            cached_fingerprint = _ragflow_generation_fingerprint(generation_kg, preset, preset, generation_params)
            cached = {"id": "ts-cached", "kg_id": "kg-first", "generation_fingerprint": cached_fingerprint,
                      "stale": False, "samples": [{"index": 0, "stale": False}], "is_mock": False}
            cached_path = storage.ts_write("ts-cached", cached)
            storage.ts_upsert({"id": "ts-cached", "path": cached_path,
                               "generation_fingerprint": cached_fingerprint, "stale": False})
            result = asyncio.run(generate_testset(Task("test"), "kg-cache", preset, preset, 10,
                                                  None, True, ""))
            assert result["reused"] is True and result["testset_id"] == "ts-cached"
            assert result["usage"]["llm_calls"] == 0
        finally:
            storage.TESTSET_DIR, storage.TS_INDEX, storage.KG_INDEX = original_dir, original_index, original_kg_index
    print(" fake RAGFlow incremental verification: PASS")
    print({"first": first_delta, "update": third_delta, "delete": fourth_delta, "fallback": refreshed_delta})


if __name__ == "__main__":
    main()
