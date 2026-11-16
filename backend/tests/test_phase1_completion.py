"""第一阶段收尾：资源租约、正文制品、Embedding 计量和失败账单。"""
import asyncio
import json
from types import SimpleNamespace

import pytest


def test_persistent_lease_is_atomic_across_managers(tmp_path):
    from app.tasks.manager import TaskManager, ResourceBusyError
    first = TaskManager()
    first.initialize(tmp_path / 'tasks.sqlite3')
    job = first.create('generation', ('shared',))
    second = TaskManager()
    second.initialize(tmp_path / 'tasks.sqlite3')
    assert second.get(job.id).status == 'pending'
    with pytest.raises(ResourceBusyError):
        second.create('duplicate', ('shared', 'other'))
    # 部分键不能在事务失败后泄漏。
    second.create('independent', ('other',))
    job.status = 'succeeded'
    first.publish(job, 'done', job.view())
    first._release_resources(job)
    second.create('after-completion', ('shared',))


def test_document_text_is_separate_and_legacy_readable(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base, 'DOC_INDEX', tmp_path / 'index.json')
    monkeypatch.setattr(storage.base, 'DOCUMENTS', {})
    text = '中文正文\n' * 1000
    doc = storage.add_document('large.md', text, len(text))
    index = json.loads(storage.base.DOC_INDEX.read_text(encoding='utf-8'))
    assert 'text' not in index[doc['id']]
    assert storage.base.DOC_INDEX.stat().st_size < 2000
    storage.base.DOCUMENTS.clear()
    storage._load_persisted_docs()
    assert storage.document_snapshot()[doc['id']]['text'] == text
    storage.base.DOC_INDEX.write_text(json.dumps({'legacy': {'text': '旧格式'}}), encoding='utf-8')
    storage._load_persisted_docs()
    assert storage.document_snapshot()['legacy']['text'] == '旧格式'


def test_missing_document_text_blocks_writes(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base, 'DOC_INDEX', tmp_path / 'index.json')
    monkeypatch.setattr(storage.base, 'DOCUMENTS', {})
    monkeypatch.setattr(storage.base, '_CORRUPT', {})
    doc = storage.add_document('a.md', '证据', 2)
    index = json.loads(storage.base.DOC_INDEX.read_text(encoding='utf-8'))
    (tmp_path / 'texts' / index[doc['id']]['text_file']).unlink()
    with pytest.raises(storage.StorageCorruptionError):
        storage._load_persisted_docs()
    with pytest.raises(storage.StorageCorruptionError):
        storage.add_document('b.md', '不能覆盖坏索引', 5)
    assert json.loads(storage.base.DOC_INDEX.read_text(encoding='utf-8')) == index


def test_embedding_usage_shares_budget_and_blocks_next_attempt():
    from app.ragas_adapter.clients import MeteredEmbeddingEndpoint
    from app.ragas_adapter.telemetry import GenerationTelemetry
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(total_tokens=7), data=[])
    async def run():
        tele = GenerationTelemetry(token_budget=10)
        tele.on_llm_start({}, ['test'], run_id='llm')
        tele.on_llm_end(SimpleNamespace(llm_output={'token_usage': {'prompt_tokens': 2, 'completion_tokens': 1}}), run_id='llm')
        endpoint = MeteredEmbeddingEndpoint(create, tele, timeout=2, max_retries=1)
        await endpoint.create(input='chunk', model='embedding')
        assert tele.snapshot()['total_tokens'] == 10
        assert tele.snapshot()['embedding_tokens'] == 7
        with pytest.raises(RuntimeError):
            await endpoint.create(input='blocked', model='embedding')
        assert len(calls) == 1
    asyncio.run(run())


def test_missing_embedding_usage_is_explicit():
    from app.ragas_adapter.telemetry import GenerationTelemetry
    tele = GenerationTelemetry()
    tele.begin_embedding()
    tele.end_embedding(None)
    assert tele.snapshot()['usage_complete'] is False
    assert tele.snapshot()['usage_missing_calls'] == 1


def test_failed_prestage_keeps_usage_in_journal(tmp_path):
    from app.ragas_adapter.telemetry import GenerationTelemetry
    from app.tasks.manager import TaskManager
    async def run():
        manager = TaskManager()
        manager.initialize(tmp_path / 'tasks.sqlite3')
        task = manager.create('generate_testset_external')
        async def body():
            task.generation_telemetry = GenerationTelemetry()
            task.generation_telemetry.begin_embedding()
            task.generation_telemetry.end_embedding({'total_tokens': 19})
            raise ValueError('建图失败')
        manager.run(task, body())
        await task._async_task
        return task.id
    task_id = asyncio.run(run())
    import sqlite3
    with sqlite3.connect(tmp_path / 'tasks.sqlite3') as db:
        data = json.loads(db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
    assert data['status'] == 'failed'
    assert data['usage']['embedding_tokens'] == 19
