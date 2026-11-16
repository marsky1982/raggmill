"""实体规范化与 LLM 消歧开关的回归测试（全部打桩，0 真实 token）。

覆盖设计文档 §10 的后端用例 1~8，外加 settings 接口的校验与生效状态。
测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

import asyncio
import shutil

import pytest


def _use_legacy_overlap_params(monkeypatch):
    """把建边参数设回存量投影的隐含参数：projection_config 不带 overlap 明细，与历史投影一致。

    默认最小频次已从 3 改为 5（新默认会写 overlap 明细、触发存量投影重建一次）；
    本模块测的是别的行为，固定成存量口径，避免两件事互相干扰。
    """
    from app.ragas_adapter import ragflow as rf

    for key, attr in (("min_score", "_OVERLAP_MIN_SCORE"), ("blacklist_df_ratio", "_OVERLAP_BLACKLIST_DF_RATIO"),
                      ("blacklist_min_df", "_OVERLAP_BLACKLIST_MIN_DF")):
        monkeypatch.setattr(rf, attr, rf._OVERLAP_LEGACY_PARAMS[key])


@pytest.fixture(autouse=True)
def _legacy_overlap_params(monkeypatch):
    _use_legacy_overlap_params(monkeypatch)


class FakeClient:
    """duck-typed RAGFlow 客户端；只提供同步链路真正用到的方法。"""

    def __init__(self, docs: list[dict], chunks: dict[str, list[dict]], kb_rows: list[dict],
                 version: str | None = "0.27.1"):
        self.docs = docs
        self.chunks = chunks
        self.kb_rows = kb_rows
        self.version = version
        self.document_calls = 0

    def list_documents(self, dataset_id: str) -> list[dict]:
        self.document_calls += 1
        return [dict(row) for row in self.docs]

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return [dict(row) for row in self.chunks.get(document_id, [])]

    def kb_structure(self, dataset_id: str) -> dict:
        return {"templates": [{"entities": [dict(row) for row in self.kb_rows]}]}

    def get_version(self) -> str | None:
        return self.version

    def alteration(self, dataset_id: str) -> dict:
        return {}


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


@pytest.fixture
def ds():
    """每个用例独占一个数据源目录，避免快照/投影在用例之间串味。"""
    from app import storage

    dataset_id = storage.new_id("ds")
    yield dataset_id
    shutil.rmtree(storage.ragflow_legacy_source_dir(dataset_id), ignore_errors=True)


def _preset(model: str = "fake-model") -> dict:
    from app import storage

    return storage.save_preset({
        "kind": "llm_testset",
        "name": f"消歧桩预设-{model}",
        "base_url": "http://127.0.0.1:1/v1",
        "model": model,
        "api_key": "sk-fake",
        "is_default": False,
    })


def _docs_and_chunks(entity_names: list[str]) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """单个文档单个分块，实体全部挂在这个分块上（保证进入共现候选）。"""
    docs = [{"id": "d1", "name": "文档一.txt", "update_time": "2026-01-01",
             "status": "done", "chunk_count": 1}]
    chunks = {"d1": [{"id": "c1", "content": "RAGFlow 与 ragflow 是同一套系统"}]}
    kb_rows = [{"name": name, "source_chunk_ids": ["c1"]} for name in entity_names]
    return docs, chunks, kb_rows


def _install(monkeypatch, docs, chunks, kb_rows) -> FakeClient:
    from app.ragas_adapter import ragflow as rf

    client = FakeClient(docs, chunks, kb_rows)
    monkeypatch.setattr(rf, "RAGFlowClient", lambda *args, **kwargs: client)
    return client


def _sync(dataset_id: str) -> dict:
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    task = Task("ragflow_sync")
    task.resources = storage.ragflow_sync_resources(dataset_id, None)  # 入口要求持有租约
    return asyncio.run(rf.sync_ragflow(task, dataset_id, "http://127.0.0.1:9380", "key"))


def _write_settings(dataset_id: str, mode: str, preset_id: str | None = None, **extra):
    from app import storage

    data = {
        "schema_version": 1,
        "entity_resolution": mode,
        "llm_preset_id": preset_id,
        "prompt_version": "entity-resolve-v1",
        "updated_at": storage.now_iso(),
    }
    data.update(extra)
    storage.ragflow_settings_write(dataset_id, data)


def _projection_entities(kg_id: str) -> list[str]:
    from app import storage

    projection = storage.kg_read_json(kg_id) or {}
    entities: set[str] = set()
    for node in projection.get("nodes") or []:
        if str(node.get("type")) != "chunk":
            continue
        entities.update((node.get("properties") or {}).get("entities") or [])
    return sorted(entities)


def _llm_calls(monkeypatch, verdicts: dict[frozenset, str] | None = None,
               raises: Exception | None = None) -> list[list[dict]]:
    """替换 entity_resolution 的 LLM 接缝：记录送出的候选对，按规约返回三态。"""
    from app.ragas_adapter import entity_resolution as er

    calls: list[list[dict]] = []

    async def fake(items, preset, task, telemetry, on_result, *, policy=None):
        calls.append(list(items))
        if raises is not None:
            raise raises
        out = {}
        for item in items:
            verdict = (verdicts or {}).get(frozenset((item["a"], item["b"])), "unsure")
            out[(item["a"], item["b"])] = verdict
            on_result((item["a"], item["b"]), verdict)
        return out

    monkeypatch.setattr(er, "_classify_pairs", fake)
    return calls


def _sent_pairs(calls: list[list[dict]]) -> set[frozenset]:
    return {frozenset((item["a"], item["b"])) for batch in calls for item in batch}


def _reused(result: dict) -> bool:
    """同步结果里的复用判定（沿用既有字段，不新增顶层 reused）。"""
    return bool((result.get("report") or {}).get("changes", {}).get("reused_projection"))


def _make_testset(dataset_id: str, kg_id: str) -> str:
    """落一条引用指定投影的测试集：验证「被引用的旧投影不删」。"""
    from app import storage

    ts_id = storage.new_id("ts")
    data = {
        "id": ts_id, "name": "引用旧投影的测试集", "created_at": storage.now_iso(),
        "kg_id": kg_id, "mode": "ragas", "source_dataset_id": dataset_id,
        "stale": False, "stale_sample_count": 0,
        "samples": [{"index": 0, "user_input": "问题", "source_chunk_ids": ["c1"], "stale": False}],
    }
    path = storage.ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry.update(path=path, count=1)
    storage.ts_upsert(entry)
    return ts_id


# ---------- 1. disabled：行为与现状一致，0 调用 ----------

def test_disabled_keeps_current_behaviour(monkeypatch, ds):
    from app import storage

    # 「RAGFlow」与「RAG Flow」规范化后不同键，关闭态应各自保留
    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    calls = _llm_calls(monkeypatch)

    result = _sync(ds)

    assert calls == []
    assert result["entity_resolution"] == {"mode": "disabled"}
    entry = storage.kg_get(result["kg_id"])
    # 关闭态配置必须恰好是存量形态（不含任何新字段），否则会触发一次性重建
    assert entry["projection_config"] == {"entity_resolution": "disabled"}
    assert _projection_entities(result["kg_id"]) == ["RAG Flow", "RAGFlow"]
    assert "entity_resolution" not in (storage.ragflow_snapshot_current(ds)[0] or {})


def test_disabled_reuses_existing_projection(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)

    first = _sync(ds)
    second = _sync(ds)

    assert _reused(first) is False
    assert _reused(second) is True
    assert second["kg_id"] == first["kg_id"]


# ---------- 2. rules：同义写法合并，0 调用 ----------

def test_rules_merge_synonyms_without_llm(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴"])
    _install(monkeypatch, docs, chunks, kb_rows)
    calls = _llm_calls(monkeypatch)
    _write_settings(ds, "rules")

    result = _sync(ds)

    assert calls == []
    assert _projection_entities(result["kg_id"]) == ["阿里巴巴"]
    assert result["entity_resolution"]["mode"] == "rules"
    assert result["entity_resolution"]["rule_hits"] >= 1
    assert result["entity_resolution"]["llm_calls"] == 0


# ---------- 3. hybrid：只对规则未定的候选调 LLM ----------

def test_hybrid_calls_llm_only_for_undecided_pairs(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴", "阿里"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch, {
        frozenset(("阿里巴巴", "阿里")): "same",
        frozenset(("阿里巴巴(alibaba", "阿里巴巴")): "same",
        frozenset(("阿里巴巴(alibaba", "阿里")): "same",
    })
    _write_settings(ds, "hybrid", preset["id"])

    result = _sync(ds)

    sent = _sent_pairs(calls)
    # 规则已定的对不送 LLM（括号全称 ↔ 简称）
    assert frozenset(("阿里巴巴(alibaba", "阿里巴巴")) not in sent
    assert frozenset(("阿里巴巴", "阿里")) in sent
    # 同频取字典序最小：「阿里」比「阿里巴巴」小，组的展示名就是它
    assert _projection_entities(result["kg_id"]) == ["阿里"]
    assert result["entity_resolution"]["rule_hits"] >= 1
    assert result["entity_resolution"]["llm_calls"] >= 1


def test_hybrid_unsure_is_not_merged(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["星河物流", "星和物流"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _llm_calls(monkeypatch)  # 默认全部 unsure
    _write_settings(ds, "hybrid", preset["id"])

    result = _sync(ds)

    assert _projection_entities(result["kg_id"]) == ["星和物流", "星河物流"]
    assert result["entity_resolution"]["unsure"] >= 1


# ---------- 4. 缓存：同输入二次 0 调用；改口径不命中 ----------

def test_cache_hit_makes_second_sync_free(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["星河物流", "星和物流"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch, {frozenset(("星和物流", "星河物流")): "same"})
    _write_settings(ds, "hybrid", preset["id"])

    first = _sync(ds)
    assert sum(len(batch) for batch in calls) >= 1
    assert first["entity_resolution"]["llm_calls"] >= 1

    calls.clear()
    second = _sync(ds)
    assert calls == []  # 全部命中缓存，0 调用
    assert second["entity_resolution"]["cache_hits"] >= 1
    assert _reused(second) is True
    # 同频取字典序最小：「星和物流」胜出，两次同步口径一致（幂等）
    assert _projection_entities(second["kg_id"]) == ["星和物流"]


def test_cache_key_changes_with_model(monkeypatch, ds):
    docs, chunks, kb_rows = _docs_and_chunks(["星河物流", "星和物流"])
    _install(monkeypatch, docs, chunks, kb_rows)
    calls = _llm_calls(monkeypatch, {frozenset(("星和物流", "星河物流")): "same"})
    _write_settings(ds, "hybrid", _preset("model-a")["id"])
    _sync(ds)

    calls.clear()
    _write_settings(ds, "hybrid", _preset("model-b")["id"])
    _sync(ds)
    # 换模型换缓存键：必须重新调用
    assert sum(len(batch) for batch in calls) >= 1


# ---------- 5. 切换：重建投影，旧投影保留 ----------

def test_switching_mode_rebuilds_and_keeps_old_projection(monkeypatch, ds):
    from app import storage

    docs, chunks, kb_rows = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _llm_calls(monkeypatch, {frozenset(("阿里巴巴(alibaba", "阿里巴巴")): "same"})

    disabled = _sync(ds)
    ts_id = _make_testset(ds, disabled["kg_id"])
    _write_settings(ds, "hybrid", preset["id"])
    enabled = _sync(ds)

    assert _reused(enabled) is False
    assert enabled["kg_id"] != disabled["kg_id"]
    old_entry = storage.kg_get(disabled["kg_id"])
    assert old_entry is not None  # 被测试集引用的旧投影保留
    assert disabled["kg_id"] in enabled["retained_kg_ids"]
    assert old_entry["projection_config"] == {"entity_resolution": "disabled"}
    assert _projection_entities(enabled["kg_id"]) == ["阿里巴巴"]
    assert storage.ts_read(ts_id)["kg_id"] == disabled["kg_id"]  # 历史测试集不改写
    assert storage.ragflow_snapshot_current(ds)[0]["entity_resolution"]["mode"] == "hybrid"


def test_switch_is_pending_until_sync(monkeypatch, ds):
    from app import storage
    from app.routers.ragflow import _entity_settings_view

    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _sync(ds)

    settings = {
        "entity_resolution": "hybrid",
        "llm_preset_id": preset["id"],
        "prompt_version": "entity-resolve-v1",
    }
    assert _entity_settings_view(ds, None, settings)["pending_rebuild"] is True

    _llm_calls(monkeypatch, {frozenset(("ragflow", "rag flow")): "unsure"})
    _write_settings(ds, "hybrid", preset["id"])
    _sync(ds)
    assert _entity_settings_view(ds, None, storage.ragflow_settings_read(ds))["pending_rebuild"] is False


# ---------- 6. 失败：不写快照/投影/指针，缓存不污染 ----------

def test_llm_failure_aborts_sync_without_writing(monkeypatch, ds):
    from app import storage
    from app.ragas_adapter.entity_resolution import EntityResolutionError

    docs, chunks, kb_rows = _docs_and_chunks(["星河物流", "星和物流"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _llm_calls(monkeypatch, raises=EntityResolutionError("桩失败"))
    _write_settings(ds, "hybrid", preset["id"])

    before_snapshot, before_pointer = storage.ragflow_snapshot_current(ds)
    with pytest.raises(EntityResolutionError):
        _sync(ds)
    after_snapshot, after_pointer = storage.ragflow_snapshot_current(ds)

    assert before_pointer == after_pointer
    assert before_snapshot == after_snapshot
    assert storage.ragflow_entity_cache_read(ds) == {}


def test_missing_preset_aborts_before_fetch(monkeypatch, ds):
    """开关开着但预设不可用：同步直接失败，且一个请求都不发。"""
    from app.ragas_adapter.entity_resolution import EntityResolutionError

    docs, chunks, kb_rows = _docs_and_chunks(["星河物流", "星和物流"])
    client = _install(monkeypatch, docs, chunks, kb_rows)
    _write_settings(ds, "hybrid", "preset-not-exist")

    with pytest.raises(EntityResolutionError):
        _sync(ds)
    assert client.document_calls == 0


# ---------- 7. 预估：0 请求 0 token ----------

def test_estimate_uses_local_snapshot_only(monkeypatch, ds):
    from app.routers.ragflow import EntityEstimateIn, _estimate_view

    docs, chunks, kb_rows = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴", "阿里"])
    client = _install(monkeypatch, docs, chunks, kb_rows)
    _sync(ds)
    before_calls = client.document_calls

    view = _estimate_view(ds, None, EntityEstimateIn(entity_resolution="hybrid"))

    assert view["available"] is True
    assert view["basis"] == "snapshot"
    assert view["candidate_pairs"] >= 1
    assert view["rule_hits"] >= 1
    assert 0 <= view["estimated"]["low"] <= view["estimated"]["expected"] <= view["estimated"]["high"]
    assert client.document_calls == before_calls  # 没有为了预估去拉 RAGFlow


def test_estimate_without_snapshot_reports_unavailable(ds):
    from app.routers.ragflow import EntityEstimateIn, _estimate_view

    view = _estimate_view(ds, None, EntityEstimateIn(entity_resolution="hybrid"))

    assert view["available"] is False
    assert "先同步" in view["hint"]


# ---------- 8. 多 source：各自独立 ----------

def test_two_sources_are_independent(monkeypatch):
    from app import storage

    defects = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴"])
    _install(monkeypatch, *defects)
    _llm_calls(monkeypatch, {frozenset(("阿里巴巴(alibaba", "阿里巴巴")): "same"})
    a_id, b_id = storage.new_id("ds"), storage.new_id("ds")
    try:
        _write_settings(a_id, "rules")
        _write_settings(b_id, "disabled")
        a = _sync(a_id)
        b = _sync(b_id)

        assert a["entity_resolution"]["mode"] == "rules"
        assert b["entity_resolution"] == {"mode": "disabled"}
        assert _projection_entities(a["kg_id"]) == ["阿里巴巴"]
        assert _projection_entities(b["kg_id"]) == ["阿里巴巴", "阿里巴巴（Alibaba）"]
        assert storage.kg_get(b["kg_id"])["projection_config"] == {"entity_resolution": "disabled"}
    finally:
        for dataset in (a_id, b_id):
            shutil.rmtree(storage.ragflow_legacy_source_dir(dataset), ignore_errors=True)


# ---------- settings 接口校验 ----------

def test_settings_write_rejects_bad_mode(ds):
    from app.routers.ragflow import EntitySettingsIn, _write_entity_settings

    view, err = _write_entity_settings(ds, None, EntitySettingsIn(entity_resolution="oops"))

    assert view is None
    assert err.status_code == 400


def test_settings_write_requires_valid_preset(ds):
    from app import storage
    from app.routers.ragflow import EntitySettingsIn, _write_entity_settings

    view, err = _write_entity_settings(ds, None, EntitySettingsIn(entity_resolution="hybrid"))
    assert err.status_code == 400

    # kind 不合法（embedding 预设不能当消歧模型）
    embedding = storage.save_preset({
        "kind": "embedding", "name": "embedding-桩", "base_url": "http://127.0.0.1:1/v1",
        "model": "emb", "api_key": "sk-fake", "is_default": False,
    })
    view, err = _write_entity_settings(
        ds, None, EntitySettingsIn(entity_resolution="hybrid", llm_preset_id=embedding["id"]))
    assert err.status_code == 400


def test_settings_write_accepts_disabled_without_preset(ds):
    from app.routers.ragflow import EntitySettingsIn, _write_entity_settings

    view, err = _write_entity_settings(ds, None, EntitySettingsIn(entity_resolution="disabled"))

    assert err is None
    assert view["entity_resolution"] == "disabled"
    assert view["pending_rebuild"] is True  # 还没有任何投影


def test_aliases_are_preserved_when_omitted(ds):
    from app import storage
    from app.routers.ragflow import EntitySettingsIn, _write_entity_settings

    preset = _preset()
    _write_entity_settings(ds, None, EntitySettingsIn(
        entity_resolution="hybrid", llm_preset_id=preset["id"],
        aliases={"阿里巴巴": ["阿里"]},
    ))
    assert storage.ragflow_settings_read(ds)["aliases"] == {"阿里巴巴": ["阿里"]}

    _write_entity_settings(ds, None, EntitySettingsIn(
        entity_resolution="hybrid", llm_preset_id=preset["id"]))
    assert storage.ragflow_settings_read(ds)["aliases"] == {"阿里巴巴": ["阿里"]}


# ---------- 9. blocking 边界：difflib 口径（0 token，与设计 §5.2 一致） ----------

def _candidate_pairs(*names: str, chunk_keys: dict | None = None) -> set[frozenset]:
    """手工造 pool，直接看候选对是否生成（不进规则、不调 LLM）。"""
    from app.ragas_adapter import entity_resolution as er

    keys = [er._normalize(name) for name in names]
    pool = {
        "counts": {key: 1 for key in keys},
        "display": {key: name for key, name in zip(keys, names)},
        "chunk_keys": chunk_keys or {},
        "context": {},
    }
    return {frozenset(pair) for pair in er.build_candidates(pool)}


def test_blocking_similarity_boundary_is_ratio_0_7():
    # 等长 10 字符、7 个字符相同 → ratio = 14/20 = 0.7，恰好落在门槛上（≥ 通过）。
    # 这两串的 2-gram Jaccard 只有 5/13 ≈ 0.38，能进候选说明走的是 difflib 这一路。
    assert frozenset(("abcdefghij", "abcdxxxhij")) in _candidate_pairs("abcdefghij", "abcdXXXhij")
    # 只相同 6 个字符 → 0.6，低于门槛；Jaccard 也不足 → 不进候选。
    assert frozenset(("abcdefghij", "abcdxxxxij")) not in _candidate_pairs("abcdefghij", "abcdXXXXij")


def test_blocking_short_names_need_cooccurrence():
    # 3 字及以下不走模糊匹配：'abc'/'ab' 的 ratio 是 0.8 也不会进候选（长度门槛）。
    assert frozenset(("abc", "ab")) not in _candidate_pairs("abc", "ab")
    # 同分块共现仍然能带进来：真实语料里同义写法基本同块出现。
    assert frozenset(("abc", "ab")) in _candidate_pairs("abc", "ab", chunk_keys={"c1": {"abc", "ab"}})


def test_blocking_digits_and_cjk_boundaries():
    """数字差异与短 CJK 前缀扩展是已知边界：会/不会进候选各锁一条。"""
    from app.ragas_adapter import entity_resolution as er

    # 数字只差一位（gpt4/gpt5）：进候选（blocking 宽一点只多花 token），
    # 但规则不合并：rules 模式保持分开，hybrid 交给 LLM 判。
    assert frozenset(("gpt4", "gpt5")) in _candidate_pairs("gpt4", "gpt5")
    assert er._rule_same("gpt4", "gpt5", {}) is False

    # 短 CJK 前缀扩展（「阿里云」/「阿里云计算」）：长度门槛 + 2-gram Jaccard 都不足，
    # 只有在同一分块出现时才会进候选 —— 这是设计里写明的边界。
    assert frozenset(("阿里云", "阿里云计算")) not in _candidate_pairs("阿里云", "阿里云计算")
    assert frozenset(("阿里云", "阿里云计算")) in _candidate_pairs(
        "阿里云", "阿里云计算", chunk_keys={"c1": {"阿里云", "阿里云计算"}})


# ---------- 10. pending_rebuild 的三类边界 ----------

def _rewrite_projection_config(kg_id: str, config: dict | None) -> None:
    """改写投影与其索引条目里的 projection_config（模拟存量投影 / 人工写入）。"""
    from app import storage

    entry = storage.kg_get(kg_id)
    projection = storage.kg_read_json(kg_id) or {}
    for target in (entry, projection):
        if config is None:
            target.pop("projection_config", None)
        else:
            target["projection_config"] = dict(config)
    storage.kg_upsert(entry)
    storage.kg_write_json(kg_id, projection)


def test_legacy_projection_without_fields_is_not_pending(monkeypatch, ds):
    """旧版投影没有任何消歧字段：关着的库既不提示重建，同步也必须复用。"""
    from app.routers.ragflow import _entity_settings_view

    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    first = _sync(ds)
    _rewrite_projection_config(first["kg_id"], None)

    view = _entity_settings_view(ds, None, {"entity_resolution": "disabled"})
    assert view["pending_rebuild"] is False

    again = _sync(ds)
    assert _reused(again) is True
    assert again["kg_id"] == first["kg_id"]


def test_disabled_mode_ignores_leftover_fingerprint(monkeypatch, ds):
    """关闭态但投影里留着指纹字段：不判重建，同步也不重建（与 UI 结论保持一致）。"""
    from app.routers.ragflow import _entity_settings_view

    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    first = _sync(ds)
    _rewrite_projection_config(first["kg_id"], {
        "entity_resolution": "disabled",
        "config_fingerprint": "sha256:legacy",
        "prompt_version": "entity-resolve-v0",
        "llm_model": "old-model",
    })

    view = _entity_settings_view(ds, None, {"entity_resolution": "disabled"})
    assert view["pending_rebuild"] is False
    assert _reused(_sync(ds)) is True


def test_turning_off_hybrid_is_pending_until_resync(monkeypatch, ds):
    """同一 source 开过 hybrid 又关掉：必须提示重建，同步真的重建。"""
    from app import storage
    from app.routers.ragflow import _entity_settings_view

    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _llm_calls(monkeypatch, {frozenset(("rag flow", "ragflow")): "same"})
    first = _sync(ds)
    _write_settings(ds, "hybrid", preset["id"])
    enabled = _sync(ds)
    assert enabled["kg_id"] != first["kg_id"]

    _write_settings(ds, "disabled")
    assert _entity_settings_view(ds, None, {"entity_resolution": "disabled"})["pending_rebuild"] is True

    third = _sync(ds)
    assert _reused(third) is False
    assert storage.kg_get(third["kg_id"])["projection_config"] == {"entity_resolution": "disabled"}
    assert _entity_settings_view(ds, None, {"entity_resolution": "disabled"})["pending_rebuild"] is False


# ---------- 11. 实体重写不影响历史测试集的 stale 判定 ----------

def test_entity_rewrite_does_not_mark_history_stale(monkeypatch, ds):
    """实体合并只改 entities、不改正文：历史测试集不能被误打 stale。

    这里刻意用**没有内容指纹的历史样本**（legacy 样本无法被出处复核回收），
    所以只要 mark_testsets_stale 把它标上，用例就会失败 —— 这是最严的口径。
    """
    from app import storage

    docs, chunks, kb_rows = _docs_and_chunks(["RAGFlow", "RAG Flow"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    _llm_calls(monkeypatch, {frozenset(("rag flow", "ragflow")): "same"})
    first = _sync(ds)
    ts_id = _make_testset(ds, first["kg_id"])

    _write_settings(ds, "hybrid", preset["id"])
    enabled = _sync(ds)

    assert enabled["kg_id"] != first["kg_id"]          # 口径变了 → 投影重建
    assert len(_projection_entities(enabled["kg_id"])) == 1   # 实体确实被合并
    assert enabled["report"]["changes"]["changed_chunks"] == 0
    entry = storage.ts_read(ts_id)
    assert entry.get("stale") in (None, False)
    assert entry.get("stale_sample_count") == 0
    assert ts_id not in (enabled.get("still_stale_ids") or [])

    # 第二次 hybrid 同步：实体已固化，既不重建、也不该有任何变化
    second = _sync(ds)
    assert _reused(second) is True
    assert second["report"]["changes"]["changed_chunks"] == 0


# ---------- 12. 截断正确性：规则前移 / 打分排序 / 三档统计 / max_pairs=0 ----------

def test_rule_hits_are_not_lost_by_truncation(monkeypatch, ds):
    """别名对排在字典序末尾、max_pairs 只留 1 对时，规则命中仍必须生效。

    旧实现先按字典序截断再跑规则，('zeta','zzz') 会被切掉、rule_hits=0；
    新实现先跑全量规则、再截断剩余未决对。
    """
    from app.ragas_adapter import entity_resolution as er

    monkeypatch.setattr(er, "_MAX_PAIRS", 1)
    docs, chunks, kb_rows = _docs_and_chunks(["zeta", "yyy", "zzz"])
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch)
    _write_settings(ds, "hybrid", preset["id"], aliases={"zzz": ["zeta"]})

    result = _sync(ds)

    stats = result["entity_resolution"]
    assert stats["rule_hits"] == 1
    # 规则已定的对绝不送 LLM（0 token 的召回不能被付费上限切断）
    assert frozenset(("zeta", "zzz")) not in _sent_pairs(calls)
    # 合并真的落地：zzz 归到字典序最小的 zeta
    entities = _projection_entities(result["kg_id"])
    assert "zzz" not in entities
    assert entities == ["yyy", "zeta"]


def test_ranking_prefers_cooccurrence_then_similarity():
    """打分排序：同词形下共现多的优先；同共现下词形更相似的优先。"""
    from app.ragas_adapter import entity_resolution as er

    pool = {
        "counts": {key: 1 for key in ("abcd", "abce", "abcf", "abcy", "abxy")},
        "display": {},
        "chunk_keys": {"c1": {"abcd", "abce", "abcf", "abxy"}, "c2": {"abcd", "abcf"}},
        "context": {},
    }
    ranked = er.rank_candidate_pairs(pool, [
        ("abcd", "abce"),   # 词形相近 + 共现 1 块
        ("abcd", "abcf"),   # 词形相近 + 共现 2 块（应排最前）
        ("abcd", "abcy"),   # 词形相近、无共现（应排在同词形的 1 块共现之后）
        ("abcd", "abxy"),   # 相似度更低 + 共现 1 块（应排在 abcd/abce 之后）
    ])

    assert ranked[0] == ("abcd", "abcf")
    assert ranked.index(("abcd", "abce")) < ranked.index(("abcd", "abcy"))
    assert ranked.index(("abcd", "abce")) < ranked.index(("abcd", "abxy"))


def test_truncation_takes_top_k_by_score_not_alphabetical(monkeypatch, ds):
    """max_pairs=1 时必须留下最像同义的那对，而不是码点最小的 ascii 对。"""
    from app.ragas_adapter import entity_resolution as er

    monkeypatch.setattr(er, "_MAX_PAIRS", 1)
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
             "阿里巴巴", "阿里巴巴集团"]
    docs, chunks, kb_rows = _docs_and_chunks(names)
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch)  # 默认全部 unsure
    _write_settings(ds, "hybrid", preset["id"])

    result = _sync(ds)

    sent = _sent_pairs(calls)
    assert sent == {frozenset(("阿里巴巴", "阿里巴巴集团"))}
    assert result["entity_resolution"]["pairs_truncated"] == 44
    assert result["entity_resolution"]["pairs_observed"] == 45
    assert result["entity_resolution"]["pairs_sent"] == 1


def test_stats_report_observed_truncated_and_sent(monkeypatch, ds):
    """三档统计：8 实体 28 对候选、max_pairs=20 → 截断 8、送 20；预估接口同口径。"""
    from app.ragas_adapter import entity_resolution as er
    from app.routers.ragflow import EntityEstimateIn, _estimate_view

    monkeypatch.setattr(er, "_MAX_PAIRS", 20)
    names = [f"e{i}" for i in range(8)]
    docs, chunks, kb_rows = _docs_and_chunks(names)
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch)

    # 先以关闭态同步一次拿到快照（此时还没有任何消歧缓存），保证预估就是「首次 hybrid」
    _write_settings(ds, "disabled")
    _sync(ds)
    _write_settings(ds, "hybrid", preset["id"])
    view = _estimate_view(ds, None, EntityEstimateIn(entity_resolution="hybrid"))
    assert view["candidate_pairs"] == 28
    assert view["llm_pairs"] == 20
    assert view["pairs_truncated"] == 8

    result = _sync(ds)

    stats = result["entity_resolution"]
    assert stats["pairs_observed"] == 28
    assert stats["pairs_truncated"] == 8
    assert stats["pairs_sent"] == 20
    assert sum(len(batch) for batch in calls) == 20


def test_max_pairs_zero_means_unlimited(monkeypatch, ds):
    """max_pairs=0 保持旧行为：不截断，全部未决对送 LLM。"""
    from app.ragas_adapter import entity_resolution as er

    monkeypatch.setattr(er, "_MAX_PAIRS", 0)
    names = [f"e{i}" for i in range(8)]
    docs, chunks, kb_rows = _docs_and_chunks(names)
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch)
    _write_settings(ds, "hybrid", preset["id"])

    result = _sync(ds)

    stats = result["entity_resolution"]
    assert stats["pairs_observed"] == 28
    assert stats["pairs_truncated"] == 0
    assert stats["pairs_sent"] == 28
    assert sum(len(batch) for batch in calls) == 28



def test_turning_resolution_off_does_not_mark_history_stale(monkeypatch, ds):
    """开关从开切到关：上次快照是合并后的实体名，本次是原始写法，正文没变。

    关闭前只有开启时才按正文指纹重算变化集，关掉后所有带实体的分块被判成变化，
    历史测试集被误打 stale。legacy 样本（无内容指纹）无法被回收，最能暴露这个问题。
    """
    from app import storage

    docs, chunks, kb_rows = _docs_and_chunks(["阿里巴巴（Alibaba）", "阿里巴巴"])
    _install(monkeypatch, docs, chunks, kb_rows)
    _write_settings(ds, "rules")
    enabled = _sync(ds)
    assert _projection_entities(enabled["kg_id"]) == ["阿里巴巴"]
    ts_id = _make_testset(ds, enabled["kg_id"])

    _write_settings(ds, "disabled")
    disabled = _sync(ds)

    assert disabled["kg_id"] != enabled["kg_id"]                 # 口径变了 → 投影重建
    assert len(_projection_entities(disabled["kg_id"])) == 2     # 实体恢复原始写法
    assert disabled["report"]["changes"]["changed_chunks"] == 0
    assert disabled["stale_testset_ids"] == []
    entry = storage.ts_read(ts_id)
    assert entry.get("stale") in (None, False)

    # 再同步一次：关闭态稳定复用
    again = _sync(ds)
    assert _reused(again) is True


def test_truncated_pairs_converge_when_candidates_exceed_cache_limit(monkeypatch, ds):
    """候选对多于缓存上限时，按批送出的结论不能被淘汰，几次同步后收敛到 0 次 LLM 调用。

    8 实体 28 对候选、每次最多送 10 对、缓存上限设成 12：修复前缓存只留 12 条，
    第 3 次同步起刚付过钱的结论被挤掉，每次都要重新送；修复后 3 次送完，第 4 次 0 调用。
    """
    from app.ragas_adapter import entity_resolution as er

    monkeypatch.setattr(er, "_MAX_PAIRS", 10)
    monkeypatch.setattr(er, "_CACHE_LIMIT", 12)
    names = [f"e{i}" for i in range(8)]
    docs, chunks, kb_rows = _docs_and_chunks(names)
    _install(monkeypatch, docs, chunks, kb_rows)
    preset = _preset()
    calls = _llm_calls(monkeypatch)
    _write_settings(ds, "hybrid", preset["id"])

    sent_per_sync = []
    for _ in range(5):
        before = sum(len(batch) for batch in calls)
        result = _sync(ds)
        sent_per_sync.append(sum(len(batch) for batch in calls) - before)
        assert result["entity_resolution"]["pairs_observed"] == 28

    assert sent_per_sync == [10, 10, 8, 0, 0]
    assert len(_sent_pairs(calls)) == 28  # 每对只付一次钱


# ---------- 执行参数统一走 GenerationPolicy ----------

def test_llm_execution_uses_generation_policy(monkeypatch):
    """超时 / 重试 / 并发不再读环境变量：`build_llm` 与重试次数都取自 GenerationPolicy。"""
    from app.policies import GenerationPolicy
    from app.ragas_adapter import clients
    from app.ragas_adapter import entity_resolution as er

    built: list[dict] = []

    class BoomLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def agenerate_text(self, _prompt):
            self.calls += 1
            raise RuntimeError("网关超时")

    llm = BoomLLM()

    def fake_build_llm(preset, **kwargs):
        built.append(kwargs)
        return llm

    monkeypatch.setattr(clients, "build_llm", fake_build_llm)
    items = [{"a": "a", "b": "b", "name_a": "A", "name_b": "B", "context_a": "", "context_b": ""}]
    policy = GenerationPolicy(max_retries=2, llm_timeout=42, max_workers=1)

    async def run():
        return await er._classify_pairs(
            items, {"id": "p1", "model": "m"}, None, None, lambda *args: None, policy=policy)

    with pytest.raises(er.EntityResolutionError, match="已尝试 2 次"):
        asyncio.run(run())
    assert built and built[0]["timeout"] == 42 and built[0]["sdk_max_retries"] == 0
    assert llm.calls == 2
