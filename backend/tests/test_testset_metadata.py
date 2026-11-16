"""测试集产物必须记录本次实际使用的模型与连线依据。

只落测试集详情（data/testset/{id}.json），不进 CSV/JSONL 样本字段——
导出格式要保持 ragas Testset.from_list() 能原样读回。
"""
from __future__ import annotations

import asyncio
import json

import pytest

LLM_PRESET = {"id": "preset-llm_testset-test", "kind": "llm_testset",
              "name": "test_llm", "model": "某推理模型"}
EMB_PRESET = {"id": "preset-embedding-test", "kind": "embedding",
              "name": "test_emb", "model": "某 embedding 模型"}

META_KEYS = {"llm_preset_id", "llm_model", "embedding_preset_id", "embedding_model",
             "relation_property"}


def _generate_demo(monkeypatch, size: int = 1, relation_property: str = "entities_overlap_score"):
    """演示模式生成一份测试集（不发任何 LLM / embedding 请求）。"""
    from app import storage
    from app.ragas_adapter import testset as tsmod
    from app.ragas_adapter import testset_generation  # 补丁打在真正调用的子模块上
    from app.tasks.manager import Task

    monkeypatch.setattr(testset_generation, "RAGAS_AVAILABLE", False)
    monkeypatch.setattr(testset_generation, "kg_get",
                        lambda kg_id: {"id": kg_id, "name": "测试KG", "mode": "ragas"})
    result = asyncio.run(tsmod.generate_testset(
        Task("test-metadata"), "kg-test", LLM_PRESET, EMB_PRESET, size, None, True,
        "元信息测试集", relation_property))
    return result, storage.ts_read(result["testset_id"])


def test_testset_json_records_model_and_relation_property(monkeypatch):
    result, data = _generate_demo(monkeypatch)
    assert data is not None

    assert data["llm_preset_id"] == LLM_PRESET["id"]
    assert data["llm_model"] == LLM_PRESET["model"]
    assert data["embedding_preset_id"] == EMB_PRESET["id"]
    assert data["embedding_model"] == EMB_PRESET["model"]
    assert data["relation_property"] == "entities_overlap_score"
    # 终态路径与完成路径共用同一个落盘 dict，元信息不会只出现在成功分支
    assert data["stop_reason"] == "completed"
    assert data["partial"] is False

    # 索引条目同样带元信息，列表页无需再单独查详情
    from app import storage
    entry = next(x for x in storage.ts_list() if x["id"] == result["testset_id"])
    assert entry["llm_model"] == LLM_PRESET["model"]

    for key in META_KEYS:
        assert key in data


SAMPLE = {
    "user_input": "问题",
    "reference": "答案",
    "reference_contexts": ["上下文"],
    "synthesizer_name": "single_hop_specific_query_synthesizer",
}

EXPECTED_SAMPLE_KEYS = {"user_input", "reference", "reference_contexts", "retrieved_contexts",
                        "response", "synthesizer_name", "query_style", "query_length",
                        "persona_name"}
EXPECTED_CSV_HEADER = ["user_input", "reference", "reference_contexts", "retrieved_contexts",
                       "response", "synthesizer_name", "query_style", "query_length",
                       "persona_name"]


@pytest.mark.parametrize("fmt", ["jsonl", "csv"])
def test_export_sample_fields_unchanged_by_metadata(fmt):
    from app.ragas_adapter.testset import export_testset

    ts_data = {"id": "ts-test", "name": "测试集", "samples": [dict(SAMPLE)],
               "llm_preset_id": LLM_PRESET["id"], "llm_model": LLM_PRESET["model"],
               "embedding_preset_id": EMB_PRESET["id"],
               "embedding_model": EMB_PRESET["model"],
               "relation_property": "summary_similarity"}
    _, content = export_testset(ts_data, fmt, "all", None)

    if fmt == "jsonl":
        rows = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        # 空的 response / retrieved_contexts 在 JSONL 里省略（CSV 表头仍保留）
        assert set(rows[0]) == EXPECTED_SAMPLE_KEYS - {"response", "retrieved_contexts"}
        assert not (META_KEYS & set(rows[0]))
    else:
        header = content.decode("utf-8-sig").splitlines()[0].split(",")
        assert header == EXPECTED_CSV_HEADER
        assert not (META_KEYS & set(header))
