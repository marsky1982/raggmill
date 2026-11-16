"""stale 标记的回收：置位 → 清除双向，以及 legacy 不被误清。

背景：`mark_testsets_stale` 只按「受影响的 chunk 集合」置位（把实体重算也当成正文变化），
且从不回收。同步末尾的 `reconcile_testsets_stale` 复用 P2 出处判定（只比正文哈希）：
有指纹且复核 valid → 清；legacy（无内容指纹）无法判定 → 保持原标记。

全部用合成投影 + 临时 DATA_DIR，不连真实 RAGFlow、不碰 data/ 里的现有测试集。
测试模块顶层不 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _write_projection(dataset: str, kg_id: str, chunk_text: dict[str, str]) -> None:
    from app import storage

    nodes = [{"id": node_id, "type": "chunk",
              "properties": {"page_content": text, "source_chunk_id": node_id.replace("n", "c")}}
             for node_id, text in chunk_text.items()]
    path = storage.kg_write_json(kg_id, {"nodes": nodes, "relationships": []})
    storage.kg_upsert({
        "id": kg_id, "name": "合成投影", "kg_path": path, "created_at": storage.now_iso(),
        "mode": "ragflow-projection", "source_dataset_id": dataset, "node_count": len(nodes),
        "relationship_count": 0, "projection_version": kg_id,
    })


def _write_testset(dataset: str, kg_id: str, samples: list[dict]) -> str:
    from app import storage

    ts_id = storage.new_id("ts")
    stale_count = sum(1 for sample in samples if sample.get("stale"))
    data = {
        "id": ts_id, "name": "stale 回收样本集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas", "is_mock": False, "language": "zh",
        "source_dataset_id": dataset, "source_connection_id": None,
        "stale": bool(stale_count), "stale_sample_count": stale_count,
        "testset_size": len(samples), "samples": samples,
    }
    path = storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"},
                       "path": path, "count": len(samples)})
    return ts_id


def _fingerprinted(index: int, text: str, stale: bool = False) -> dict:
    from app.ragas_adapter.provenance import content_hash

    sample = {
        "index": index, "synthesizer_name": "single_hop_specific_query_synthesizer",
        "user_input": f"问题{index}", "reference": f"答案{index}",
        "reference_contexts": [text], "adopted": False,
        "source_chunk_ids": ["n1"], "source_content_hashes": [content_hash(text)],
        "source_upstream_chunk_ids": ["c1"],
    }
    if stale:
        sample["stale"] = True
        sample["stale_reason"] = "source_projection_changed"
    return sample


def _legacy(index: int, stale: bool = True) -> dict:
    sample = {
        "index": index, "synthesizer_name": "single_hop_specific_query_synthesizer",
        "user_input": f"旧问题{index}", "reference": f"旧答案{index}",
        "reference_contexts": ["旧上下文"], "adopted": False,
    }
    if stale:
        sample["stale"] = True
        sample["stale_reason"] = "source_projection_changed_without_provenance"
    return sample


def _mark(dataset: str, kg_id: str, delta: dict) -> list[str]:
    from app.ragas_adapter.ragflow import mark_testsets_stale

    return mark_testsets_stale(dataset, kg_id, delta, legacy_source=True)


def _reconcile(dataset: str, kg_id: str) -> list[dict]:
    from app.ragas_adapter.ragflow import reconcile_testsets_stale

    return reconcile_testsets_stale(dataset, kg_id, legacy_source=True)


def _read(ts_id: str) -> dict:
    from app import storage

    return storage.ts_read(ts_id) or {}


def test_mark_sets_then_reconcile_clears_when_content_recovers():
    """置位 → 清除双向：正文改回被引用时的样子，有指纹样本解除 stale，legacy 保持。"""
    from app import storage

    dataset = "ds-stale-reconcile-roundtrip"
    kg_id = storage.new_id("kg")
    _write_projection(dataset, kg_id, {"n1": "原始正文"})
    ts_id = _write_testset(dataset, kg_id, [_fingerprinted(0, "原始正文"), _legacy(1)])

    # ① 置位：本次同步 delta 里 c1 被记为变化（旧口径：只要 chunk 集合受影响就标 stale）
    assert ts_id in _mark(dataset, kg_id, {"changed_chunk_ids": ["c1"]})
    data = _read(ts_id)
    assert data["stale"] is True and data["stale_sample_count"] == 2
    assert all(sample.get("stale") for sample in data["samples"])

    # ② 清除：投影正文与样本指纹重新一致 → 只清有指纹的那条
    result = _reconcile(dataset, kg_id)
    row = next(item for item in result if item["testset_id"] == ts_id)
    assert row["cleared"] == 1
    assert row["undetermined_legacy"] == 1
    data = _read(ts_id)
    assert data["samples"][0].get("stale") is None, "复核有效的样本应当解除 stale"
    assert "stale_reason" not in data["samples"][0]
    assert data["samples"][1]["stale"] is True, "legacy 无内容指纹，无法判定，不得误清"
    assert data["stale"] is True
    assert data["stale_sample_count"] == 1


def test_content_still_differs_keeps_stale():
    """正文确实变了：复核仍是 stale，标记与计数都不动。"""
    from app import storage

    dataset = "ds-stale-reconcile-kept"
    kg_id = storage.new_id("kg")
    _write_projection(dataset, kg_id, {"n1": "修改后的正文"})
    ts_id = _write_testset(dataset, kg_id, [_fingerprinted(0, "原始正文", stale=True)])

    assert _reconcile(dataset, kg_id) == []
    data = _read(ts_id)
    assert data["stale"] is True and data["stale_sample_count"] == 1
    assert data["samples"][0]["stale"] is True


def test_full_recovery_drops_testset_level_stale_flag():
    """全部样本都复核有效：测试集 stale 置 false、计数撤掉，列表索引同步更新。"""
    from app import storage

    dataset = "ds-stale-reconcile-full"
    kg_id = storage.new_id("kg")
    _write_projection(dataset, kg_id, {"n1": "原始正文"})
    ts_id = _write_testset(dataset, kg_id, [
        _fingerprinted(0, "原始正文", stale=True),
        _fingerprinted(1, "原始正文", stale=True),
    ])

    result = _reconcile(dataset, kg_id)
    row = next(item for item in result if item["testset_id"] == ts_id)
    assert row["cleared"] == 2 and row["remaining"] == 0
    data = _read(ts_id)
    assert data["stale"] is False
    assert "stale_sample_count" not in data
    entry = next(item for item in storage.ts_list() if item["id"] == ts_id)
    assert entry.get("stale") is False


def test_legacy_only_testset_is_never_cleared():
    """只有 legacy 样本的测试集：全部无法判定，一条都不清、也不写回文件。"""
    from app import storage

    dataset = "ds-stale-reconcile-legacy"
    kg_id = storage.new_id("kg")
    _write_projection(dataset, kg_id, {"n1": "原始正文"})
    ts_id = _write_testset(dataset, kg_id, [_legacy(0), _legacy(1)])

    assert _reconcile(dataset, kg_id) == []
    data = _read(ts_id)
    assert data["stale"] is True and data["stale_sample_count"] == 2
    assert all(sample["stale"] for sample in data["samples"])


def test_reconcile_note_matches_actual_counts():
    """两条同步路径共用的日志文案：条数/测试集数必须与实际回收结果一致，空结果不打字。"""
    from app.ragas_adapter.ragflow import _reconcile_note

    assert _reconcile_note([]) == ""
    reconciled = [
        {"testset_id": "ts-a", "cleared": 2, "remaining": 1, "undetermined_legacy": 0},
        {"testset_id": "ts-b", "cleared": 3, "remaining": 0, "undetermined_legacy": 0},
    ]
    note = _reconcile_note(reconciled)
    assert "2 个测试集" in note and "5 条样本" in note


def test_annotate_confirms_absent_key_as_normal():
    """落盘形态统一为「缺键 = 正常」：标注不再写入 stale:false，已有的 stale:true 保持。"""
    from app.ragas_adapter.ragflow import annotate_samples

    fresh = {"index": 0, "user_input": "问题0", "reference": "答案0", "reference_contexts": ["上下文0"]}
    explicit_false = {**fresh, "index": 1, "stale": False}
    already_stale = {**fresh, "index": 2, "stale": True, "stale_reason": "source_projection_changed"}
    annotate_samples([fresh, explicit_false, already_stale], {"nodes": []}, "v1", "ds-shape")

    assert "stale" not in fresh, "正常样本不应再落 stale:false"
    assert "stale" not in explicit_false, "存量显式 false 收敛为缺键"
    assert already_stale["stale"] is True and already_stale["stale_reason"] == "source_projection_changed"


def test_old_false_shape_reads_as_not_stale():
    """存量两种形态（无键 / stale:false）读路径等价：都不算 stale，也不触发回收。"""
    from app.ragas_adapter.provenance import clear_recovered_stale, judge_sample

    chunks = {"n1": "原始正文"}
    explicit_false = {**_fingerprinted(0, "原始正文"), "stale": False}
    absent = _fingerprinted(1, "原始正文")

    assert judge_sample(explicit_false, chunks, True)["status"] == "valid"
    outcome = clear_recovered_stale([explicit_false, absent], chunks)
    assert outcome == {"cleared": 0, "remaining": 0, "undetermined_legacy": 0}
