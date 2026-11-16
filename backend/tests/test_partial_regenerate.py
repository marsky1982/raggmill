"""局部补生成（按出处指纹，只重出受影响的样本）。

链路：第一次同步 → 构造带出处的测试集 → 改一个分块正文、删一个分块 → 第二次同步 →
判定对照**当前投影**（不是测试集生成时的旧图）→ 补生成计划 → 演示模式执行补生成。
验收：只有引用了变化分块的样本判 stale、引用被删分块的判 orphan，其余 valid / legacy 原样保留；
补生成结果另存为新测试集，旧测试集一个字节不改。
不连真实 RAGFlow、0 token（演示模式出新样本）。测试模块顶层不 import app。
"""
from __future__ import annotations

import asyncio
import json

import pytest

DATASET = "ds-partial-regen"


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


class _Upstream:
    def __init__(self):
        self.docs = [{"id": "d1", "name": "一.txt", "update_time": "1", "run": "DONE", "chunk_count": 2},
                     {"id": "d2", "name": "二.txt", "update_time": "1", "run": "DONE", "chunk_count": 2}]
        self.chunks = {"d1": [{"id": "c1", "content": "星河物流调度平台的建设背景"},
                              {"id": "c2", "content": "路径规划引擎采用 OR-Tools"}],
                       "d2": [{"id": "c3", "content": "订单中心每天同步一万条运单"},
                              {"id": "c4", "content": "司机端应用支持离线派单"}]}

    def client(self):
        up = self

        class Client:
            def list_documents(self, dataset_id):
                return [dict(r) for r in up.docs]

            def list_chunks(self, dataset_id, document_id):
                return [dict(r) for r in up.chunks.get(document_id, [])]

            def kb_structure(self, dataset_id):
                return {"templates": [{"entities": []}]}

            def get_version(self):
                return "1.0.0-rc3"

            def alteration(self, dataset_id):
                return {}

        return Client()


def _sync(monkeypatch, up: _Upstream) -> dict:
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *a, **k: up.client())
    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources(DATASET, None)  # 入口要求持有租约
    return asyncio.run(rf.sync_ragflow(task, DATASET, "http://127.0.0.1:9380", "key"))


def _node_ids(kg_id: str) -> dict[str, tuple[str, str]]:
    """上游 chunk id -> (投影节点 id, 正文)"""
    from app import storage

    out = {}
    for node in storage.kg_read_json(kg_id)["nodes"]:
        props = node.get("properties") or {}
        if props.get("source_chunk_id"):
            out[props["source_chunk_id"]] = (str(node["id"]), props.get("page_content") or "")
    return out


def _sample(index: int, synth: str, cites: list[tuple[str, str]] | None) -> dict:
    from app.ragas_adapter.provenance import content_hash

    sample = {"index": index, "synthesizer_name": synth, "user_input": f"问题{index}", "reference": f"答案{index}",
              "reference_contexts": [f"上下文{index}"], "adopted": False}
    if cites is not None:
        sample["source_chunk_ids"] = [node_id for node_id, _ in cites]
        sample["source_content_hashes"] = [content_hash(text) for _, text in cites]
    return sample


@pytest.fixture(scope="module")
def scenario():
    """同步两次（中间改 c1 正文、删 c3），返回 (旧测试集 id, 第二次同步结果)。"""
    from app import storage

    mp = pytest.MonkeyPatch()
    up = _Upstream()
    first = _sync(mp, up)
    nodes = _node_ids(first["kg_id"])
    samples = [
        _sample(0, "single_hop_specific_query_synthesizer", [nodes["c1"]]),              # → stale
        _sample(1, "multi_hop_specific_query_synthesizer", [nodes["c1"], nodes["c2"]]),  # → stale
        _sample(2, "single_hop_specific_query_synthesizer", [nodes["c3"]]),              # → orphan
        _sample(3, "single_hop_specific_query_synthesizer", [nodes["c4"]]),              # → valid
        _sample(4, "multi_hop_abstract_query_synthesizer", [nodes["c2"], nodes["c4"]]),  # → valid
        _sample(5, "single_hop_specific_query_synthesizer", None),                       # → legacy
    ]
    ts_id = storage.new_id("ts")
    data = {"id": ts_id, "name": "补生成样本集", "created_at": storage.now_iso(), "kg_id": first["kg_id"],
            "testset_size": len(samples), "mode": "ragas", "is_mock": False, "language": "zh",
            "relation_property": "summary_similarity", "source_dataset_id": DATASET,
            "source_connection_id": None, "samples": samples}
    path = storage.ts_write(ts_id, data)
    storage.ts_upsert({**{k: v for k, v in data.items() if k != "samples"}, "path": path, "count": len(samples)})

    up.chunks["d1"][0]["content"] = "星河物流调度平台的建设背景（2026 年修订）"
    up.docs[0]["update_time"] = "2"
    up.chunks["d2"] = [c for c in up.chunks["d2"] if c["id"] != "c3"]
    up.docs[1]["update_time"] = "2"
    up.docs[1]["chunk_count"] = 1
    second = _sync(mp, up)
    mp.undo()
    assert second["kg_id"] != first["kg_id"], "内容变了应当重建投影"
    return ts_id, first, second


def test_provenance_is_judged_against_current_projection(scenario):
    """只有引用了变化分块的样本判 stale、引用被删分块的判 orphan，其余 valid / legacy。"""
    from fastapi.testclient import TestClient

    from app.main import app

    ts_id, first, second = scenario
    with TestClient(app) as client:
        body = client.get(f"/api/testset/{ts_id}/provenance").json()
        own = client.get(f"/api/testset/{ts_id}/provenance", params={"projection_kg_id": first["kg_id"]}).json()
    assert body["judged_against"] == "current_projection" and body["judged_against_kg_id"] == second["kg_id"]
    status = {row["index"]: row["status"] for row in body["samples"]}
    assert status == {0: "stale", 1: "stale", 2: "orphan", 3: "valid", 4: "valid", 5: "legacy"}
    assert own["judged_against"] == "explicit" and own["counts"]["valid"] == 5, "对照旧图时全部有效（旧默认口径的盲区）"


def test_regenerate_plan_counts_only_affected_samples(scenario):
    from fastapi.testclient import TestClient

    from app.main import app

    ts_id, _, second = scenario
    with TestClient(app) as client:
        plan = client.get(f"/api/testset/{ts_id}/regenerate-plan").json()
    assert plan["blocked"] is None
    assert plan["regenerate"] == 3 and plan["keep"] == 3 and plan["keep_legacy"] == 1
    assert plan["regenerate_by_type"] == {"single_hop_specific": 2, "multi_hop_specific": 1}
    assert {row["index"] for row in plan["affected"]} == {0, 1, 2}
    assert plan["estimate"]["splits"] == {"SingleHopSpecificQuery": 2, "MultiHopAbstractQuery": 0,
                                          "MultiHopSpecificQuery": 1}
    assert plan["legacy_note"]
    # 预估要用本次补生成的人格数：人格开销随人格数线性增长（rc3 实测按默认 3 个估、实际 1 个跑，高估约 2.8 倍）
    with TestClient(app) as client:
        one = client.get(f"/api/testset/{ts_id}/regenerate-plan", params={"num_personas": 1}).json()
        bad = client.get(f"/api/testset/{ts_id}/regenerate-plan", params={"num_personas": 0})
    assert one["estimate"]["breakdown_tokens"]["人格生成"] * 3 == plan["estimate"]["breakdown_tokens"]["人格生成"]
    assert bad.status_code == 422


def test_regenerate_keeps_unaffected_samples_and_leaves_old_testset_untouched(scenario, monkeypatch):
    from app import storage
    from app.ragas_adapter import testset_generation
    from app.ragas_adapter.testset_regenerate import _dist_key, regenerate_testset
    from app.tasks.manager import Task

    ts_id, _, second = scenario
    before = json.dumps(storage.ts_read(ts_id), ensure_ascii=False, sort_keys=True)
    old_samples = {s["index"]: s for s in storage.ts_read(ts_id)["samples"]}
    monkeypatch.setattr(testset_generation, "RAGAS_AVAILABLE", False)  # 演示模式出新样本，0 token
    task = Task("testset")

    result = asyncio.run(regenerate_testset(task, ts_id, {"id": "p-llm"}, {"id": "p-emb"}))
    assert json.dumps(storage.ts_read(ts_id), ensure_ascii=False, sort_keys=True) == before, "旧测试集不改"
    # 补生成传的是「占比重算后的条数」：计划里的 regenerate_by_type 是条数，直接当比例传会
    # 触发「题型比例合计为 2（应为 1）」的告警。这里断言告警不再出现（口径归一化已完成）。
    assert not any("题型比例合计" in line for line in task.logs)

    new = storage.ts_read(result["testset_id"])
    assert new["kg_id"] == second["kg_id"]
    assert new["regeneration"]["from_testset_id"] == ts_id
    assert new["regeneration"]["replaced_indices"] == [0, 1, 2]
    assert new["regeneration"]["replaced_by_status"] == {"stale": 2, "orphan": 1}
    kept = [s for s in new["samples"] if not s.get("regenerated")]
    fresh = [s for s in new["samples"] if s.get("regenerated")]
    assert [s["index"] for s in kept] == [3, 4, 5]
    assert all(s == old_samples[s["index"]] for s in kept), "未受影响的样本一个字段都不改"
    assert len(fresh) == 3 and [s["index"] for s in fresh] == [6, 7, 8]
    # 演示模式用显示名、真实模式用合成器名，统一换成题型键比较
    assert sorted(_dist_key(s["synthesizer_name"]) for s in fresh) == [
        "multi_hop_specific", "single_hop_specific", "single_hop_specific"]
    assert new["testset_size"] == 6 and new["generation_fingerprint"] is None
    # 历史连接同步的投影会给新测试集记 conn-legacy-* 连接 id：判定仍要找到数据源当前投影（不能退回自身的图）
    from app.ragas_adapter.testset_regenerate import resolve_judge_target
    assert new["source_connection_id"] == storage.legacy_connection_id(DATASET)
    assert resolve_judge_target(new) == (second["kg_id"], "current_projection")


def test_dist_key_accepts_both_naming_styles():
    from app.ragas_adapter.testset_regenerate import _dist_key

    assert _dist_key("single_hop_specific_query_synthesizer") == "single_hop_specific"
    assert _dist_key("MultiHopAbstractQuery") == "multi_hop_abstract"
    assert _dist_key("某种自定义合成器") is None


def test_regenerate_route_and_blocked_when_nothing_affected(scenario):
    """路由：有受影响样本时起任务；新生成的测试集对照当前投影全部有效 → 400「不需要补生成」。"""
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app

    llm = storage.save_preset({"kind": "llm_testset", "name": "regen-llm", "base_url": "http://x", "model": "m",
                               "api_key": "k"})
    emb = storage.save_preset({"kind": "embedding", "name": "regen-emb", "base_url": "http://x", "model": "e",
                               "api_key": "k"})
    ts_id, _, second = scenario
    clean_id = storage.new_id("ts")
    path = storage.ts_write(clean_id, {"id": clean_id, "name": "干净", "created_at": storage.now_iso(),
                                       "kg_id": second["kg_id"], "samples": [], "source_dataset_id": DATASET})
    storage.ts_upsert({"id": clean_id, "name": "干净", "path": path, "kg_id": second["kg_id"], "count": 0})
    body = {"llm_preset_id": llm["id"], "embedding_preset_id": emb["id"]}
    with TestClient(app) as client:
        blocked = client.post(f"/api/testset/{clean_id}/regenerate", json=body)
        missing = client.get("/api/testset/ts-not-exist/regenerate-plan")
        missing_post = client.post("/api/testset/ts-not-exist/regenerate", json=body)
    assert blocked.status_code == 400 and "不需要补生成" in blocked.json()["detail"]
    # 业务 404 必须带 error_code，前端才能与「接口不存在」的裸 {"detail":"Not Found"} 区分
    for resp in (missing, missing_post):
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "testset_not_found"
        assert "ts-not-exist" in resp.json()["detail"]


def test_regenerate_clears_stale_flags_on_recovered_kept_samples(scenario, monkeypatch):
    """保留样本里经复核仍一致（valid）的撤掉过期 stale 标记；无内容指纹的历史样本无法确认，保留标记。

    修复前保留样本原样带着同步时打的 stale，新测试集顶层却写死 stale=False，两层说法对不上。
    """
    from app import storage
    from app.ragas_adapter import testset_generation
    from app.ragas_adapter.testset_regenerate import regenerate_testset
    from app.tasks.manager import Task

    ts_id, _, _ = scenario
    data = storage.ts_read(ts_id)
    flagged = dict(data, id=storage.new_id("ts"), name="带过期标记的样本集")
    flagged["samples"] = [dict(s) for s in data["samples"]]
    for sample in flagged["samples"]:
        if sample["index"] in (3, 5):  # 3 = valid（实体变化留下的旧标记）、5 = legacy（无法复核）
            sample.update(stale=True, stale_reason="source_projection_changed")
    flagged.update(stale=True, stale_sample_count=2)
    path = storage.ts_write(flagged["id"], flagged)
    storage.ts_upsert({**{k: v for k, v in flagged.items() if k != "samples"}, "path": path,
                       "count": len(flagged["samples"])})
    monkeypatch.setattr(testset_generation, "RAGAS_AVAILABLE", False)

    result = asyncio.run(regenerate_testset(Task("testset"), flagged["id"], {"id": "p-llm"}, {"id": "p-emb"}))

    new = storage.ts_read(result["testset_id"])
    kept = {s["index"]: s for s in new["samples"] if not s.get("regenerated")}
    assert "stale" not in kept[3] and "stale_reason" not in kept[3]
    assert kept[5]["stale"] is True
    assert new["stale"] is True and new["stale_sample_count"] == 1
    assert storage.ts_read(flagged["id"])["samples"][3]["stale"] is True, "旧测试集不改"
