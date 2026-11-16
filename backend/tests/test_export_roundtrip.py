"""导出的 JSONL 必须能被 ragas Testset 读回。

注意：ragas 的 Testset.from_jsonl() 用 open(path, "r") 不指定编码，
在中文 Windows 上按 cp936 解码会把 UTF-8 中文读坏，所以这里自行以 utf-8 打开再 from_list()。
"""
from __future__ import annotations

import json
from pathlib import Path

SAMPLES = [
    {
        "user_input": "ragas 的知识图谱里有哪些节点类型？",
        "reference": "只有 document 与 chunk 两种。",
        "reference_contexts": ["节点类型只有两种：document、chunk。"],
        "synthesizer_name": "single_hop_specific_query_synthesizer",
        "query_style": "PERFECT_GRAMMAR",
        "query_length": "SHORT",
        "persona_name": "测试工程师",
        "adopted": True,
    },
    {
        "user_input": "跨文档的多跳题依赖哪种关系边？",
        "reference": "主要依赖 entities_overlap，其次是 summary_similarity。",
        "reference_contexts": ["entities_overlap 门槛极低……", "summary_similarity 余弦 ≥0.7……"],
        "synthesizer_name": "multi_hop_specific_query_synthesizer",
        "adopted": False,
    },
]


def _ts_data() -> dict:
    return {"id": "ts-test", "name": "测试集", "samples": [dict(s) for s in SAMPLES]}


def test_jsonl_export_reads_back_into_ragas_testset(tmp_path: Path):
    from ragas.testset.synthesizers.testset_schema import Testset

    from app.ragas_adapter.testset import export_testset

    filename, content = export_testset(_ts_data(), "jsonl", "all", None)
    assert filename.endswith(".jsonl")
    path = tmp_path / filename
    path.write_bytes(content)

    with open(path, "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    ts = Testset.from_list(rows)

    assert len(ts) == len(SAMPLES)
    for sample, src in zip(ts.samples, SAMPLES):
        assert sample.synthesizer_name == src["synthesizer_name"]
        assert sample.eval_sample.user_input == src["user_input"]
        assert sample.eval_sample.reference == src["reference"]
        assert sample.eval_sample.reference_contexts == src["reference_contexts"]


def test_jsonl_export_scope_adopted(tmp_path: Path):
    from app.ragas_adapter.testset import export_testset

    _, content = export_testset(_ts_data(), "jsonl", "adopted", None)
    rows = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    assert [r["user_input"] for r in rows] == [SAMPLES[0]["user_input"]]


# ---------- 导出格式细节 ----------

def test_jsonl_omits_empty_response_and_retrieved_contexts():
    """空的 response / retrieved_contexts 不写，否则 ragas 评测会误以为这两列已存在。"""
    from app.ragas_adapter.testset import export_testset

    _, content = export_testset(_ts_data(), "jsonl", "all", None)
    for line in content.decode("utf-8").splitlines():
        row = json.loads(line)
        assert "response" not in row and "retrieved_contexts" not in row
        assert "is_mock" not in row


def test_csv_contexts_round_trip_and_formula_escaped():
    import csv
    import io

    from app.ragas_adapter.testset import export_testset

    data = _ts_data()
    data["samples"][0]["reference_contexts"] = ["含 || 分隔符的原文", "第二段"]
    data["samples"][0]["user_input"] = "=HYPERLINK(\"http://evil\")"
    data["samples"][1]["reference"] = "-1 是负数"
    _, content = export_testset(data, "csv", "all", None)
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert json.loads(rows[0]["reference_contexts"]) == ["含 || 分隔符的原文", "第二段"]
    assert rows[0]["user_input"].startswith("'=")
    assert rows[1]["reference"] == "'-1 是负数"
    # 正常文本不加前缀
    assert rows[1]["user_input"] == SAMPLES[1]["user_input"]


def test_type_filter_applies_with_all_and_adopted_scope():
    """ ②：界面在「全部 / 已采纳」下选题型时传 scope=all|adopted + types，也必须过滤。"""
    from app.ragas_adapter.testset import export_testset

    multi = "multi_hop_specific_query_synthesizer"
    _, content = export_testset(_ts_data(), "jsonl", "all", [multi])
    rows = [json.loads(line) for line in content.decode("utf-8").splitlines()]
    assert [r["synthesizer_name"] for r in rows] == [multi]
    import pytest
    with pytest.raises(ValueError):  # 已采纳的只有单跳题
        export_testset(_ts_data(), "jsonl", "adopted", [multi])


def test_mock_samples_are_marked_in_export():
    from app.ragas_adapter.testset import export_testset

    data = _ts_data()
    data["is_mock"] = True
    name, content = export_testset(data, "jsonl", "all", None)
    assert "演示数据" in name
    assert all(json.loads(line)["is_mock"] is True for line in content.decode("utf-8").splitlines())
    name, content = export_testset(data, "csv", "all", None)
    assert "演示数据" in name and content.decode("utf-8-sig").splitlines()[0].endswith(",is_mock")
