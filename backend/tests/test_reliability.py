"""第一批保护的回归测试；全部临时数据、无模型/网络请求。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path
import pytest


def test_replace_failure_keeps_old_file(tmp_path, monkeypatch):
    from app import storage
    path = tmp_path/'index.json'
    path.write_text('{"old":true}', encoding='utf-8')
    def fail(*_): raise OSError('injected')
    monkeypatch.setattr(storage.base.os, 'replace', fail)
    with pytest.raises(OSError): storage._write_json(path, {'new': True})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_corrupt_index_is_not_silently_overwritten(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base, '_CORRUPT', {})
    path=tmp_path/'index.json'; path.write_bytes(b'{broken')
    with pytest.raises(storage.StorageCorruptionError): storage._read_json(path,{})
    with pytest.raises(storage.StorageCorruptionError): storage._write_json(path,{'new':True})
    assert path.read_bytes()==b'{broken'
    assert len(list(tmp_path.glob('*.corrupt-*')))==1
    path.write_text('{}')  # 显式恢复后可再次读写
    storage._write_json(path,{'restored':True})
    assert not storage.storage_health()['degraded']


def test_document_index_failure_rolls_back_memory(tmp_path, monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base,'DOC_INDEX',tmp_path/'docs.json')
    monkeypatch.setattr(storage.base,'DOCUMENTS',{})
    first=storage.add_document('safe.md','old',3)
    def fail(*_): raise OSError('injected')
    monkeypatch.setattr(storage.documents,'_persist_docs',fail)
    with pytest.raises(OSError): storage.add_document('safe.md','new',3)
    assert storage.document_snapshot()[first['id']]['text']=='old'
    with pytest.raises(OSError): storage.remove_document(first['id'])
    assert first['id'] in storage.document_snapshot()


def test_concurrent_index_and_sample_edits_keep_all_changes(tmp_path,monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base,'KG_INDEX',tmp_path/'kg-index.json')
    monkeypatch.setattr(storage.base,'TESTSET_INDEX',tmp_path/'testset-index.json', raising=False)
    # KG读改写必须完整加锁。
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda i: storage.kg_upsert({'id':str(i)}),range(30)))
    assert len(storage.kg_list())==30
    data={'id':'ts-concurrent','samples':[{'index':i,'adopted':False} for i in range(20)]}
    path=storage.ts_write(data['id'],data)
    storage.ts_upsert({'id':data['id'],'path':path})
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert all(pool.map(lambda i: storage.ts_patch_sample(data['id'],i,True),range(20)))
    assert all(s['adopted'] for s in storage.ts_read(data['id'])['samples'])


def test_checkpoint_manifest_failure_keeps_previous_graph(tmp_path,monkeypatch):
    from app import storage
    monkeypatch.setattr(storage.base,'KG_CKPT_DIR',tmp_path)
    class Graph:
        def __init__(self, version): self.version=version
        def save(self,path): Path(path).write_text(json.dumps({'version':self.version}))
    storage.ckpt_save('run',{'stage_index':1},Graph('old'))
    old_meta,old_path=storage.ckpt_load('run')
    original=storage.base._write_json
    def fail(path,*args,**kw):
        if path.name.endswith('.meta.json'): raise OSError('injected manifest failure')
        return original(path,*args,**kw)
    monkeypatch.setattr(storage.base,'_write_json',fail)
    with pytest.raises(OSError): storage.ckpt_save('run',{'stage_index':2},Graph('new'))
    meta,path=storage.ckpt_load('run')
    assert meta==old_meta and path==old_path
    assert json.loads(path.read_text())['version']=='old'


def test_checkpoint_signature_rejects_changed_inputs_and_policy():
    from app.ragas_adapter.kg import _run_signature, _validate_resume_meta

    class Transform:
        min_tokens = 10
        max_tokens = 20

    documents = {"doc-1": {"text_sha256": "hash-a"}}
    base = _run_signature(
        documents,
        ["doc-1"],
        [Transform()],
        llm_id="llm-a",
        emb_id="emb-a",
        min_tokens=10,
        max_tokens=20,
        enhance_chunks=True,
        mode="ragas",
        policy={"llm_timeout": 60, "language": "zh"},
    )
    changed_doc = _run_signature(
        {"doc-1": {"text_sha256": "hash-b"}},
        ["doc-1"],
        [Transform()],
        llm_id="llm-a",
        emb_id="emb-a",
        min_tokens=10,
        max_tokens=20,
        enhance_chunks=True,
        mode="ragas",
        policy={"llm_timeout": 60, "language": "zh"},
    )
    changed_policy = _run_signature(
        documents,
        ["doc-1"],
        [Transform()],
        llm_id="llm-a",
        emb_id="emb-a",
        min_tokens=10,
        max_tokens=20,
        enhance_chunks=True,
        mode="ragas",
        policy={"llm_timeout": 60, "language": "en"},
    )
    assert base != changed_doc
    assert base != changed_policy
    meta = {"signature_version": 1, "run_signature": base}
    _validate_resume_meta(meta, base, 1, 3)
    with pytest.raises(ValueError, match="配置已变化"):
        _validate_resume_meta(meta, changed_policy, 1, 3)
    with pytest.raises(ValueError, match="缺少运行指纹"):
        _validate_resume_meta({}, base, 1, 3)
    with pytest.raises(ValueError, match="阶段索引无效"):
        _validate_resume_meta(meta, base, 4, 3)


def test_failed_checkpoint_keeps_last_successful_counts():
    from app.ragas_adapter.kg import _failed_checkpoint_meta

    meta = {"status": "running", "stage_index": 2, "node_count": 10, "relationship_count": 9}
    failed = _failed_checkpoint_meta(meta, 2)
    assert failed["status"] == "failed"
    assert failed["stage_index"] == 2
    assert failed["node_count"] == 10
    assert failed["relationship_count"] == 9


def test_step1_client_policy_disables_sdk_retry_and_sets_run_config(monkeypatch):
    from app.policies import GenerationPolicy
    from app.ragas_adapter import kg

    calls = {}

    def fake_llm(*args, **kwargs):
        calls["llm"] = kwargs
        return object()

    def fake_configure(llm, **kwargs):
        calls["run_config"] = kwargs
        return object()

    def fake_embeddings(*args, **kwargs):
        calls["embedding"] = kwargs
        return object()

    monkeypatch.setattr(kg, "build_llm", fake_llm)
    monkeypatch.setattr(kg, "configure_run", fake_configure)
    monkeypatch.setattr(kg, "build_embeddings", fake_embeddings)
    policy = GenerationPolicy(max_retries=4, llm_timeout=12, max_workers=2)
    telemetry = object()
    llm, embeddings, run_config = kg._build_step1_clients({"id": "llm"}, {"id": "emb"}, policy, telemetry)

    assert llm is not None and embeddings is not None and run_config is not None
    assert calls["llm"]["timeout"] == 12
    assert calls["llm"]["sdk_max_retries"] == 0
    assert calls["run_config"] == {"timeout": 12, "retries": 4, "workers": 2}
    assert calls["embedding"]["timeout"] == 12
    assert calls["embedding"]["sdk_max_retries"] == 0
    assert calls["embedding"]["max_retries"] == 4


def test_task_late_subscribe_full_queue_and_replay(tmp_path):
    from app.tasks.manager import TaskManager
    async def run():
        m=TaskManager();m.initialize(tmp_path/'jobs.sqlite3')
        t=m.create('test');q=await m.subscribe(t)
        for i in range(600): m.publish(t,'progress',{'number':i})
        t.status='failed';t.error='expected';m.publish(t,'done',t.view())
        assert any(q.get_nowait()[0]=='done' for _ in range(q.qsize()))
        late=await m.subscribe(t)
        event,data=late.get_nowait();assert event=='done' and data['error']=='expected'
        live=m.create('live');m.publish(live,'progress',{'n':1});m.publish(live,'progress',{'n':2})
        replay=await m.subscribe(live,1)
        assert replay.get_nowait()[1]['n']==2
        return m,t.id,live.id
    _,done_id,live_id=asyncio.run(run())
    # 模拟原进程已退出；同一存活进程的另一个 manager 不得抢占/中断原任务。
    import sqlite3
    with sqlite3.connect(tmp_path/'jobs.sqlite3') as db:
        for task_id in (done_id, live_id):
            raw = json.loads(db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            raw['owner_pid'] = 0
            db.execute('UPDATE tasks SET body=? WHERE id=?', (json.dumps(raw), task_id))
    restored=TaskManager();restored.initialize(tmp_path/'jobs.sqlite3')
    assert restored.get(done_id).status=='failed'
    assert restored.get(live_id).status=='interrupted'


def test_task_view_and_restore_keep_run_id():
    """Step 1 的 run_id 必须随任务视图/日记透出：前端 SSE 晚订阅时靠快照拿到，取消才能删断点。"""
    from app.tasks.manager import TaskManager
    m = TaskManager()
    t = m.create("build_kg")
    assert t.view()["run_id"] is None  # 其他任务类型不伪造 run_id
    t.run_id = "kgrun-test"
    assert t.view()["run_id"] == "kgrun-test"
    restored = TaskManager()._restore(m._payload(t))
    assert restored.view()["run_id"] == "kgrun-test"


def test_cancel_does_not_release_inflight_thread_resource():
    from app.tasks.manager import TaskManager,ResourceBusyError,worker_call
    async def run():
        m=TaskManager();t=m.create('test',('resource',))
        started=threading.Event();release=threading.Event()
        def worker(): started.set();release.wait(5)
        try:
            m.run(t,worker_call(t,worker))
            while not started.is_set(): await asyncio.sleep(.01)
            assert m.cancel(t.id)
            await t._async_task
            assert t.status=='canceled'
            with pytest.raises(ResourceBusyError):m.create('overlap',('resource',))
        finally: release.set()
        await asyncio.gather(*t.worker_futures)
        m.create('next',('resource',))
    asyncio.run(run())


def test_cancel_before_first_tick_does_not_run_body():
    from app.tasks.manager import TaskManager
    async def run():
        m=TaskManager();t=m.create('test');called=[]
        async def body():called.append(True)
        m.run(t,body());m.cancel(t.id)
        await t._async_task
        assert t.status=='canceled' and not called
        event,_=(await m.subscribe(t)).get_nowait();assert event=='done'
    asyncio.run(run())


def test_policy_shared_and_unknown_fields_rejected():
    from app.routers.testset import GenIn,ExternalGenIn
    from pydantic import ValidationError
    common={'llm_preset_id':'l','embedding_preset_id':'e','token_budget':42,'language':'zh','llm_timeout':3}
    assert GenIn(kg_id='kg',**common).execution_kwargs()==ExternalGenIn(mode='chunks',chunks=[],**common).execution_kwargs()
    with pytest.raises(ValidationError):ExternalGenIn(mode='chunks',**common,token_buget=1)
    with pytest.raises(ValidationError):GenIn(kg_id='kg',**(common|{'llm_timeout':0}))


def test_external_documents_pass_policy_to_both_stages(monkeypatch):
    from app.policies import GenerationPolicy
    from app.ragas_adapter import testset as ts
    from app.tasks.manager import Task
    seen={}
    from app.ragas_adapter import testset_external as ext  # 补丁打在真正调用的子模块上
    monkeypatch.setattr(ext,'add_document',lambda *a,**k:{'id':'doc'})
    async def kg(*a,**kw):seen['kg']=kw['policy'];return {'kg_id':'kg','node_count':1,'relationship_count':0}
    async def generate(*a,**kw):seen['generation']=kw;return {'ok':True}
    monkeypatch.setattr(ext,'build_kg',kg);monkeypatch.setattr(ext,'generate_testset',generate)
    asyncio.run(ts.generate_testset_external(Task('test'),'documents',None,None,[{'name':'doc','text':'text'}],{}, {},1,None,True,'name',
                                             policy=GenerationPolicy(token_budget=42,language='zh',llm_timeout=3)))
    assert seen['kg'].token_budget==seen['generation']['policy'].token_budget==42
    assert seen['kg'].llm_timeout==seen['generation']['policy'].llm_timeout==3


def test_budget_callback_blocks_new_llm_calls():
    from app.ragas_adapter.telemetry import GenerationTelemetry
    tele=GenerationTelemetry(token_budget=10);tele.budget_exceeded=True
    with pytest.raises(RuntimeError):tele.on_llm_start({},['prompt'],run_id='test')
    assert tele.calls_started==0


def test_checkpoint_signature_ignores_execution_controls():
    """G6：预算 / 重试 / 超时 / 并发只影响怎么跑，改了也能续跑；旧口径断点仍可识别。"""
    from app.ragas_adapter.kg import _run_signature, _validate_resume_meta

    class Transform:
        min_tokens = 10

    def sig(policy, legacy=False):
        return _run_signature({"doc-1": {"text_sha256": "h"}}, ["doc-1"], [Transform()],
                              llm_id="l", emb_id="e", min_tokens=10, max_tokens=20,
                              enhance_chunks=False, mode="ragas", policy=policy, legacy=legacy)

    old = {"token_budget": 100_000, "max_retries": 3, "llm_timeout": 60, "max_workers": 8, "language": "zh"}
    new = {"token_budget": 200_000, "max_retries": 5, "llm_timeout": 180, "max_workers": 4, "language": "zh"}
    assert sig(old) == sig(new)
    assert sig(old) != sig(dict(new, language="en"))
    # 升级前写下的断点（旧口径）：同一份参数可续跑，调了预算则仍按旧规则拒绝
    legacy_meta = {"signature_version": 1, "run_signature": sig(old, legacy=True)}
    _validate_resume_meta(legacy_meta, sig(old), 1, 3, sig(old, legacy=True))
    with pytest.raises(ValueError, match="配置已变化"):
        _validate_resume_meta(legacy_meta, sig(new), 1, 3, sig(new, legacy=True))
