""" B 节 C3：实体重叠策略（高频词黑名单 / 可配置阈值 / 上限可配置）回归测试。

不连真实 RAGFlow：直接构造 snapshot 调 `build_ragas_projection`，分页用例用桩 `_get`。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import pytest


def _snapshot(chunks: dict[str, list[str]], entity_types: dict[str, str] | None = None) -> dict:
    """按「chunk_id -> 实体列表」构造最小 snapshot；所有 chunk 属于同一文档。

    `entity_types` 不传时模拟老快照（没有实体类型字段），传 `{}` 表示有该字段但类型缺失。
    """
    snapshot = {
        "dataset_id": "ds-test",
        "documents": {"d1": {"id": "d1", "name": "doc.txt", "update_time": "2026-01-01", "status": "done"}},
        "document_sources": {"d1": {"document": {"id": "d1", "name": "doc.txt"},
                                    "chunks": {cid: {"id": cid, "document_id": "d1",
                                                     "position": i, "page_content": cid}
                                               for i, cid in enumerate(chunks)}}},
        "chunks": {cid: {"id": cid, "document_id": "d1", "position": i,
                         "page_content": cid, "content_hash": f"h-{cid}", "entities": entities}
                   for i, (cid, entities) in enumerate(chunks.items())},
    }
    if entity_types is not None:
        snapshot["entity_types"] = dict(entity_types)
    return snapshot


def _overlap_edges(projection: dict) -> list[dict]:
    return [rel for rel in projection["relationships"] if rel["type"] == "entities_overlap"]


def test_default_params_keep_normal_overlap_edges():
    """原有正常边仍能建：默认参数下两个 chunk 共享一个实体就有一条边。"""
    from app.ragas_adapter import ragflow as rf

    projection = rf.build_ragas_projection(_snapshot({"c1": ["国网"], "c2": ["国网"]}),
                                           source_id="conn__ds-test")
    edges = _overlap_edges(projection)
    assert len(edges) == 1
    assert edges[0]["properties"]["entities_overlap_score"] == 1.0
    assert edges[0]["properties"]["overlapped_items"] == [["国网", "国网"]]


def test_high_frequency_entity_is_blacklisted(monkeypatch):
    """高频词被黑名单剔除后不建边，低频实体照常建边。"""
    from app.ragas_adapter import ragflow as rf

    # 「电力」出现在 6/6 个 chunk（df=6），「输配电价」只出现在 c1/c2（df=2）
    chunks = {f"c{i}": ["电力"] for i in range(1, 7)}
    chunks["c1"] = ["电力", "输配电价"]
    chunks["c2"] = ["电力", "输配电价"]
    monkeypatch.setattr(rf, "_OVERLAP_BLACKLIST_DF_RATIO", 0.5)
    monkeypatch.setattr(rf, "_OVERLAP_BLACKLIST_MIN_DF", 3)

    projection = rf.build_ragas_projection(_snapshot(chunks), source_id="conn__ds-test")
    edges = _overlap_edges(projection)
    # 只剩 c1-c2 那一条（由低频实体贡献），由高频词贡献的边全部消失
    assert len(edges) == 1
    expected = rf._stable_id("edge", "ds-test", "entities_overlap:c1:c2")
    assert edges[0]["id"] == expected
    assert edges[0]["properties"]["overlapped_items"] == [["输配电价", "输配电价"]]
    # 分母只算未拉黑实体 → 分数为 1.0 而不是被高频词稀释的 1/2
    assert edges[0]["properties"]["entities_overlap_score"] == 1.0


def test_blacklist_can_be_disabled(monkeypatch):
    """比例设为 0 即关闭黑名单，高频词重新参与建边。"""
    from app.ragas_adapter import ragflow as rf

    chunks = {f"c{i}": ["电力"] for i in range(1, 7)}
    monkeypatch.setattr(rf, "_OVERLAP_BLACKLIST_DF_RATIO", 0.0)
    projection = rf.build_ragas_projection(_snapshot(chunks), source_id="conn__ds-test")
    assert len(_overlap_edges(projection)) == 15  # 6 个 chunk 全对全


def test_person_entities_are_exempt_from_edge_blacklist():
    """人名 df 高说明是主线，建边黑名单不拉黑 person；其他高频词照旧拉黑。"""
    from app.ragas_adapter import ragflow as rf

    # 9 块库里两个实体都出现在全部 9 块（旧口径 df>=3 会把它们全拉黑 → 0 条边）
    chunks = {f"c{i}": ["严世蕃", "内阁"] for i in range(1, 10)}
    snapshot = _snapshot(chunks, entity_types={"严世蕃": "PERSON", "内阁": "org"})

    projection = rf.build_ragas_projection(snapshot, source_id="conn__ds-test")
    edges = _overlap_edges(projection)
    assert len(edges) == 36  # 9 块全对全，人名贡献的边全部保留
    items = [item for edge in edges for item in edge["properties"]["overlapped_items"]]
    assert all(name == "严世蕃" for pair in items for name in pair)
    # 分母只算未拉黑的实体（只剩人名）→ 分数 1.0
    assert edges[0]["properties"]["entities_overlap_score"] == 1.0


def test_legacy_snapshot_small_library_only_blacklists_ubiquitous():
    """ 老快照兜底：9 块且没有实体类型时门槛 = 分块数，不再按 30% 误伤主线实体。"""
    from app.ragas_adapter import ragflow as rf

    chunks = {f"c{i}": ["严世蕃"] for i in range(1, 10)}  # df=9（每块都有）
    for cid in ("c1", "c2", "c3", "c4", "c5", "c6"):
        chunks[cid].append("电力")  # df=6：旧口径（门槛 3）会拉黑，新口径保留

    projection = rf.build_ragas_projection(_snapshot(chunks), source_id="conn__ds-test")
    edges = _overlap_edges(projection)
    assert len(edges) == 15  # 只由「电力」贡献的 6 块全对全
    items = [item for edge in edges for item in edge["properties"]["overlapped_items"]]
    assert all(name == "电力" for pair in items for name in pair)


def test_legacy_snapshot_large_library_keeps_ratio_rule():
    """老快照兜底只在 ≤12 块生效；超过后仍按 30% 比例口径。"""
    from app.ragas_adapter import ragflow as rf

    chunks = {f"c{i}": [] for i in range(1, 14)}  # 13 块 → 门槛 max(5, ceil(3.9)) = 5
    for cid in ("c1", "c2", "c3", "c4", "c5"):
        chunks[cid].append("甲")  # df=5 → 拉黑
    for cid in ("c1", "c2", "c3", "c4"):
        chunks[cid].append("乙")  # df=4 → 保留

    projection = rf.build_ragas_projection(_snapshot(chunks), source_id="conn__ds-test")
    edges = _overlap_edges(projection)
    assert len(edges) == 6  # 只由「乙」贡献的 c1~c4 全对全
    items = [item for edge in edges for item in edge["properties"]["overlapped_items"]]
    assert all(name == "乙" for pair in items for name in pair)


def test_disambiguation_exclusion_ignores_person_exemption():
    """两套口径互不影响：消歧排除继续按纯 df 拉黑高频词（含人名）。"""
    from app.ragas_adapter import ragflow as rf

    chunks = {f"c{i}": ["严世蕃", "内阁"] for i in range(1, 10)}
    snapshot = _snapshot(chunks, entity_types={"严世蕃": "person", "内阁": "org"})
    excluded = rf.entity_exclusion_keys(snapshot)
    assert excluded == {rf.normalize_entity("严世蕃"), rf.normalize_entity("内阁")}

    # 同一份快照在投影里人名参与建边（只有「内阁」被拉黑）
    projection = rf.build_ragas_projection(snapshot, source_id="conn__ds-test")
    assert len(_overlap_edges(projection)) == 36

    # 老快照：建边走小库兜底（df=6 的实体保留），消歧仍按门槛 3 排除
    legacy_chunks = {f"c{i}": ["电力"] for i in range(1, 10)}
    legacy_snapshot = _snapshot(legacy_chunks)
    assert rf.entity_exclusion_keys(legacy_snapshot) == {rf.normalize_entity("电力")}
    assert "电力" not in rf._overlap_blacklist(
        legacy_snapshot["chunks"], {"c1": {"电力"}}, {})[0]


def test_overlap_min_score_is_configurable(monkeypatch):
    """阈值可配置生效：c1/c2 各两个实体、只共享一个 → 分数 0.5。"""
    from app.ragas_adapter import ragflow as rf

    snapshot = _snapshot({"c1": ["国网", "甲"], "c2": ["国网", "乙"]})

    monkeypatch.setattr(rf, "_OVERLAP_MIN_SCORE", 0.0)
    edges = _overlap_edges(rf.build_ragas_projection(snapshot, source_id="conn__ds-test"))
    assert len(edges) == 1
    assert edges[0]["properties"]["entities_overlap_score"] == 0.5

    monkeypatch.setattr(rf, "_OVERLAP_MIN_SCORE", 0.4)
    assert len(_overlap_edges(rf.build_ragas_projection(snapshot, source_id="conn__ds-test"))) == 1

    monkeypatch.setattr(rf, "_OVERLAP_MIN_SCORE", 0.6)
    assert _overlap_edges(rf.build_ragas_projection(snapshot, source_id="conn__ds-test")) == []


def test_page_limit_error_is_actionable(monkeypatch):
    """分页超限的报错要可读并给出环境变量建议。"""
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_MAX_PAGES", 1)
    monkeypatch.setattr(rf, "_PAGE_SIZE", 2)
    client = rf.RAGFlowClient("http://127.0.0.1:9380", "key")
    full_page = {"docs": [{"id": f"d{i}"} for i in range(2)]}
    monkeypatch.setattr(client, "_get", lambda path, params=None: dict(full_page))
    with pytest.raises(rf.RAGFlowError, match="RAGFLOW_MAX_PAGES"):
        client.list_documents("ds")


def test_entity_row_limit_error_is_actionable(monkeypatch):
    """实体行数超限的报错要可读并给出环境变量建议。"""
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_MAX_ENTITY_ROWS", 2)
    with pytest.raises(rf.RAGFlowError, match="RAGFLOW_MAX_ENTITY_ROWS"):
        rf._flatten_templates({"templates": [{"entities": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}]})


def test_sampling_cap_triggers_warning(monkeypatch, caplog):
    """实体行接近 RAGFlow 服务端采样阈值时给 warning（不报错）。"""
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_GRAPH_TOP_ENTITIES_CAP", 2)
    with caplog.at_level("WARNING"):
        rows = rf._flatten_templates({"templates": [{"entities": [{"name": "a"}, {"name": "b"}]}]})
    assert len(rows) == 2
    assert "采样阈值" in caplog.text


def test_typed_small_business_library_keeps_specific_entities():
    """rc3 / 1.0.0 真实  语料（9 块、全部带类型、没有人名）：只拦几乎每块都有的泛化词。

    旧门槛 3 时，出现在 3~4 块的具体实体（温控传感器、嘉兴冷链仓、电子回单）也被拉黑，
    实测只剩 5 条边；人名豁免对没有人名的业务文档不起作用。
    """
    from app.ragas_adapter import ragflow as rf

    df = {"星河物流": 7, "星河物流调度平台": 7, "司机端应用": 5, "温控传感器": 4, "嘉兴冷链仓": 4,
          "电子回单": 3, "上海港洋山码头": 2}
    types = {"星河物流": "org", "星河物流调度平台": "product", "司机端应用": "product", "温控传感器": "product",
             "嘉兴冷链仓": "location", "电子回单": "product", "上海港洋山码头": "location"}
    keys = {f"c{i}": {e for e, n in df.items() if i < n} for i in range(9)}
    chunks = {cid: {} for cid in keys}
    blacklist, cutoff = rf._overlap_blacklist(chunks, keys, types)
    assert cutoff == 5
    assert blacklist == {"星河物流", "星河物流调度平台", "司机端应用"}


def test_disambiguation_exclusion_keeps_its_own_threshold():
    """建边门槛调到 5 不影响消歧排除（仍为 3）：两者共用时，调高建边门槛会让送 LLM 的候选对变多。"""
    from app.ragas_adapter import ragflow as rf

    keys = {f"c{i}": {"甲"} if i < 3 else set() for i in range(9)}  # 甲 df=3
    chunks = {cid: {} for cid in keys}
    blacklist, cutoff = rf._disambiguation_blacklist(chunks, keys)
    assert cutoff == 3 and blacklist == {"甲"}
    overlap_blacklist, overlap_cutoff = rf._overlap_blacklist(chunks, keys, {"甲": "product"})
    assert overlap_cutoff == 5 and overlap_blacklist == set()
