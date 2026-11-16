"""RAGFlow 列表排序与读取完整性。

- A2：文档按 update_time 升序翻页，同步期间被更新的文档挪到末尾、后面整体前移 → 漏读一篇 →
  被当成「已删除」误标测试集。现按 create_time 翻页 + 比对上游 total，不全就重读，仍不全就中止。
- P1-1：分块顺序变化被当成正文变化。rc3 分块行没有任何排序依据（无 positions / 页码 / 时间），
  分块列表的 orderby 也被忽略，所以内容签名不再含顺序，分块集合与正文没变时沿用上次的顺序。
不连真实 RAGFlow、0 token。测试模块顶层不 import app。
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module", autouse=True)
def _data_dirs():
    from app import config, storage

    config.ensure_data_dirs()
    storage.init_storage()


def _client(monkeypatch, pages_by_round, page_size=2):
    """pages_by_round：每一轮整读对应一个 {page: (rows, total)}；记录每次请求参数。"""
    from app.ragas_adapter import ragflow as rf

    monkeypatch.setattr(rf, "_PAGE_SIZE", page_size)
    client = rf.RAGFlowClient("http://127.0.0.1:9380", "key")
    calls = []
    state = {"round": 0}

    def fake_get(path, params=None):
        params = dict(params or {})
        calls.append(params)
        page = params["page"]
        rounds = pages_by_round[min(state["round"], len(pages_by_round) - 1)]
        rows, total = rounds.get(page, ([], None))
        if len(rows) < page_size:  # 本轮最后一页
            state["round"] += 1
        return {"docs": [dict(r) for r in rows], "total": total}

    monkeypatch.setattr(client, "_get", fake_get)
    return client, calls


def _ids(*names):
    return [{"id": n} for n in names]


def test_documents_are_paged_by_create_time(monkeypatch):
    client, calls = _client(monkeypatch, [{1: (_ids("a"), 1)}])
    assert [d["id"] for d in client.list_documents("ds")] == ["a"]
    assert calls[0]["orderby"] == "create_time" and calls[0]["desc"] == "false"


def test_shifted_listing_is_reread_instead_of_dropping_a_document(monkeypatch):
    """第一轮读完第 1 页后 a 被删除、后面整体前移 → 漏读 c。

    读到 a,b,d,e 共 4 行、恰好等于新 total=4，只比行数发现不了；靠「各页 total 不一致」识别并重读。
    """
    client, calls = _client(monkeypatch, [
        {1: (_ids("a", "b"), 5), 2: (_ids("d", "e"), 4), 3: ([], 4)},   # 漏了 c，total=4
        {1: (_ids("b", "c"), 4), 2: (_ids("d", "e"), 4), 3: ([], 4)},
    ])
    docs = client.list_documents("ds")
    assert sorted(d["id"] for d in docs) == ["b", "c", "d", "e"]
    assert sum(1 for c in calls if c["page"] == 1) == 2, "应当整轮重读一次"


def test_count_shortfall_alone_also_triggers_reread(monkeypatch):
    client, calls = _client(monkeypatch, [
        {1: (_ids("a", "b"), 3), 2: ([], 3)},
        {1: (_ids("a", "b"), 3), 2: (_ids("c"), 3)},
    ])
    assert [d["id"] for d in client.list_documents("ds")] == ["a", "b", "c"]


def test_persistently_incomplete_listing_aborts_sync(monkeypatch):
    from app.ragas_adapter.ragflow import RAGFlowError

    client, _ = _client(monkeypatch, [{1: (_ids("a", "b"), 5), 2: (_ids("c"), 5)}])
    with pytest.raises(RAGFlowError, match="读取不完整：读到 3 行，上游报告 5 行"):
        client.list_documents("ds")


def test_rows_repeated_across_pages_are_deduplicated(monkeypatch):
    """列表在翻页期间后移时同一行会出现在两页：按 id 去重，不重复计数。"""
    client, _ = _client(monkeypatch, [{1: (_ids("a", "b"), 3), 2: (_ids("b", "c"), 3), 3: ([], 3)}])
    assert [d["id"] for d in client.list_documents("ds")] == ["a", "b", "c"]


def test_missing_total_keeps_old_behaviour(monkeypatch):
    client, calls = _client(monkeypatch, [{1: (_ids("a", "b"), None), 2: (_ids("c"), None)}])
    assert [d["id"] for d in client.list_documents("ds")] == ["a", "b", "c"]
    assert len(calls) == 2


def test_content_signature_ignores_chunk_order():
    from app.ragas_adapter.ragflow import _document_source, _source_content_signature

    doc = {"id": "d1", "name": "一.txt"}
    rows = [{"id": "c1", "content": "第一段"}, {"id": "c2", "content": "第二段"}]
    forward, backward = _document_source(doc, rows), _document_source(doc, list(reversed(rows)))
    assert _source_content_signature(forward) == _source_content_signature(backward)
    changed = _document_source(doc, [rows[0], {"id": "c2", "content": "第二段改过"}])
    assert _source_content_signature(changed) != _source_content_signature(forward)


@pytest.mark.parametrize("touch_update_time", [False, True])
def test_reordered_chunks_reuse_projection(monkeypatch, touch_update_time):
    """上游只是换了分块返回次序：不算正文变化、复用原投影、不标 stale。

    touch_update_time=False 走「元数据没变 → 内容指纹校验」路径；True 走「元数据变了 → 重新拉取」路径。
    """
    from app import storage
    from app.ragas_adapter import ragflow as rf
    from app.tasks.manager import Task

    docs = [{"id": "d1", "name": "一.txt", "update_time": "1", "status": "done", "chunk_count": 2}]
    chunks = [{"id": "c1", "content": "第一段"}, {"id": "c2", "content": "第二段"}]

    class Client:
        def list_documents(self, dataset_id):
            return [dict(r) for r in docs]

        def list_chunks(self, dataset_id, document_id):
            return [dict(r) for r in chunks]

        def kb_structure(self, dataset_id):
            return {"templates": [{"entities": []}]}

        def get_version(self):
            return "1.0.0-rc3"

        def alteration(self, dataset_id):
            return {}

    monkeypatch.setattr(rf, "RAGFlowClient", lambda *a, **k: Client())
    dataset = f"ds-reorder-{int(touch_update_time)}"

    def sync():
        task = Task("ragflow_sync")
        task.resources = storage.ragflow_sync_resources(dataset, None)  # 入口要求持有租约
        return asyncio.run(rf.sync_ragflow(task, dataset, "http://127.0.0.1:9380", "key"))

    first = sync()
    chunks.reverse()
    if touch_update_time:
        docs[0]["update_time"] = "2"
    second = sync()
    assert second["delta"]["content_changed_doc_ids"] == []
    assert second["delta"]["changed_chunk_ids"] == []
    assert second.get("reused") is True and second["kg_id"] == first["kg_id"]
    assert second["stale_testset_ids"] == []
