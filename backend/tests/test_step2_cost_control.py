"""Step 2 成本控制与生成编排回归（由 .run-logs/verify_step2_cost.py 迁入，工程化遗留「验证脚本可复现」）。

原脚本依赖本机 data/kg/kg-d8f42f1c.json（用户数据，不入库），新环境必定 exit 2。这里改为用代码生成
一张合成样例图谱（3 篇文档 / 24 个分块，带摘要向量、主题、实体、summary_similarity 与 entities_overlap 边），
打桩 LLM（按 prompt 类型回固定 JSON、每次计 500 入 / 1000 出 token），覆盖原脚本的 7 组用例：
0 比例题型不提交、比例归一化、预检 0 token 报错、单条失败不拖垮整批、全部失败也落账单、
三个阶段的取消、超预算熔断（含 partial）、条数分配口径一致、场景截断。
不连任何外部服务、0 token。测试模块顶层不 import app。
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time

import pytest

KG_ID = "kg-step2-cost-fixture"
QA = ("单跳出题", "多跳出题")
ONLY_SINGLE = {"single_hop_specific": 1, "multi_hop_abstract": 0, "multi_hop_specific": 0}


# ---------------- 合成样例图谱 ----------------

def _vector(seed: int, dim: int = 8) -> list[float]:
    raw = [math.sin(seed * 1.7 + i * 0.9) + 1.5 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _build_kg():
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship

    entity_pool = ["星河物流", "调度平台", "路径规划", "订单中心", "司机端", "冷链仓储", "OR-Tools", "阿里云",
                   "准点率", "降级派车", "运力池", "结算系统"]
    theme_pool = ["物流调度", "系统架构", "运维保障", "成本优化", "数据治理", "客户体验"]
    kg = KnowledgeGraph()
    chunks = []
    for d in range(3):
        doc = Node(type=NodeType.DOCUMENT, properties={
            "page_content": f"文档{d}", "document_metadata": {"filename": f"doc{d}.txt"},
            "summary": f"第 {d} 篇文档讲星河物流调度平台的第 {d} 个子系统。", "summary_embedding": _vector(100 + d),
        })
        kg.add(doc)
        prev = None
        for c in range(8):
            i = d * 8 + c
            chunk = Node(type=NodeType.CHUNK, properties={
                "page_content": f"第 {d} 篇第 {c} 段：" + "星河物流调度平台的业务说明。" * 12,
                "summary": f"分块 {i} 的摘要：介绍{theme_pool[i % 6]}相关内容。",
                "summary_embedding": _vector(i),
                "themes": [theme_pool[i % 6], theme_pool[(i + 1) % 6]],
                "entities": [entity_pool[i % 12], entity_pool[(i + 3) % 12], entity_pool[(i + 7) % 12]],
            })
            kg.add(chunk)
            kg.add(Relationship(type="child", source=doc, target=chunk))
            if prev is not None:
                kg.add(Relationship(type="next", source=prev, target=chunk))
                kg.add(Relationship(type="summary_similarity", source=prev, target=chunk, bidirectional=True,
                                    properties={"summary_similarity": 0.82}))
            prev = chunk
            chunks.append(chunk)
    for a in range(len(chunks)):
        for b in range(a + 1, len(chunks)):
            shared = sorted(set(chunks[a].properties["entities"]) & set(chunks[b].properties["entities"]))
            if shared:
                kg.add(Relationship(type="entities_overlap", source=chunks[a], target=chunks[b], bidirectional=True,
                                    properties={"entities_overlap_score": round(len(shared) / 3, 3),
                                                "overlapped_items": [[s, s] for s in shared]}))
    return kg


# ---------------- 打桩 LLM ----------------

class Stub:
    lock = threading.Lock()
    delay = 0.05
    persona_delay = 0.05
    fail: set[tuple[str, int]] = set()
    counts: dict[str, int] = {}
    started: list[tuple[float, str]] = []
    finished = 0

    @classmethod
    def reset(cls, delay=0.05, persona_delay=None, fail=()):
        with cls.lock:
            cls.delay = delay
            cls.persona_delay = delay if persona_delay is None else persona_delay
            cls.fail = set(fail)
            cls.counts = {}
            cls.started = []
            cls.finished = 0

    @classmethod
    def n_started(cls, label=None):
        with cls.lock:
            return sum(1 for _, lb in cls.started if label is None or lb == label)


def _respond(label: str, text: str) -> str:
    i, j = text.rfind("input: "), text.rfind("Output: ")
    data = json.loads(text[i + 7: j]) if i >= 0 and j > i else {}
    if label == "人格生成":
        return json.dumps({"name": "测试员", "role_description": "验证 Step 2 成本控制"}, ensure_ascii=False)
    if label == "主题×人格匹配":
        names = [p["name"] for p in data.get("personas", [])] or ["测试员"]
        return json.dumps({"mapping": {n: data.get("themes", [])[:2] for n in names}}, ensure_ascii=False)
    if label == "概念组合":
        lists = [x for x in data.get("lists_of_concepts", []) if x]
        combo = [lst[0] for lst in lists[:2]] or ["概念A", "概念B"]
        return json.dumps({"combinations": [combo]}, ensure_ascii=False)
    return json.dumps({"query": "桩问题？", "answer": "桩答案。"}, ensure_ascii=False)


def _make_stub_llm():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from ragas.llms import LangchainLLMWrapper

    from app.ragas_adapter.telemetry import _build_prompt_registry

    registry = _build_prompt_registry()

    def label_of(text: str) -> str:
        head = text.lstrip()[:60]
        for prefix, label in registry:
            if head.startswith(prefix[: len(head)]) or prefix in text[:400]:
                return label
        return "?"

    class StubChat(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "stub"

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            raise NotImplementedError("只走异步路径")

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            text = "\n".join(str(m.content) for m in messages)
            label = label_of(text)
            with Stub.lock:
                n = Stub.counts.get(label, 0) + 1
                Stub.counts[label] = n
                Stub.started.append((time.time(), label))
            await asyncio.sleep(Stub.persona_delay if label == "人格生成" else Stub.delay)
            if (label, n) in Stub.fail:
                raise ValueError(f"桩注入失败 {label}#{n}")
            with Stub.lock:
                Stub.finished += 1
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=_respond(label, text)),
                                            generation_info={"finish_reason": "stop"})],
                llm_output={"token_usage": {"prompt_tokens": 500, "completion_tokens": 1000},
                            "model_name": "stub"})

    return lambda preset, **kw: LangchainLLMWrapper(StubChat())


SAVED: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def _env():
    from app import config, storage
    from app.ragas_adapter import testset_generation as gen

    config.ensure_data_dirs()
    storage.init_storage()
    path = config.DATA_DIR / "kg" / f"{KG_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_kg().save(str(path))
    storage.kg_upsert({"id": KG_ID, "name": KG_ID, "kg_path": str(path), "mode": "ragas",
                       "created_at": storage.now_iso()})

    def ts_write(ts_id, data):
        SAVED[ts_id] = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        return f"memory://{ts_id}"

    with pytest.MonkeyPatch.context() as mp:  # 补丁打在真正调用的子模块上（testset.py 只是门面）
        mp.setattr(gen, "build_llm", _make_stub_llm())
        mp.setattr(gen, "ts_write", ts_write)
        mp.setattr(gen, "ts_upsert", lambda entry: None)
        yield


# ---------------- 用例驱动（与原脚本 run_case 同口径）----------------

def run_case(title, *, dist, size, rp="summary_similarity", budget=0, workers=4,
             cancel_when=None, delay=0.05, persona_delay=None, fail=(), observe=1.0):
    from app.policies import GenerationPolicy
    from app.ragas_adapter import testset as ts
    from app.tasks.manager import manager

    async def _run():
        Stub.reset(delay=delay, persona_delay=persona_delay, fail=fail)
        SAVED.clear()
        task = manager.create("verify")
        q = await manager.subscribe(task)
        events: list[tuple[str, dict]] = []

        async def consume():
            while True:
                ev, data = await q.get()
                events.append((ev, data))
                if ev == "done":
                    return

        consumer = asyncio.create_task(consume())
        t0 = time.time()
        manager.run(task, ts.generate_testset(
            task, KG_ID, {"model": "stub"}, {"model": "stub"}, size, dist, dist is None, f"verify-{title}", rp,
            policy=GenerationPolicy(max_retries=3, llm_timeout=30, max_workers=workers,
                                    token_budget=budget, num_personas=2, language="zh")))
        cancel_info = None
        if cancel_when:
            while not cancel_when() and not task._async_task.done():
                await asyncio.sleep(0.05)
            cancel_info = {"at": Stub.n_started(), "t": time.time() - t0}
            manager.cancel(task.id)
        try:
            await asyncio.wait_for(asyncio.shield(task._async_task), timeout=120)
        except (asyncio.CancelledError, Exception):
            pass
        t_end = time.time()
        try:
            await asyncio.wait_for(consumer, timeout=5)
        except asyncio.TimeoutError:
            consumer.cancel()
        calls_at_end = Stub.n_started()
        await asyncio.sleep(observe)  # 观察窗口：任务结束后是否还有调用发出
        saved = list(SAVED.values())
        out = {
            "status": task.status, "error": task.error,
            "done_events": [d.get("status") for e, d in events if e == "done"],
            "llm_calls_total": Stub.n_started(),
            "calls_after_task_end": Stub.n_started() - calls_at_end,
            "calls_by_prompt": dict(Stub.counts),
            "saved": saved,
            "result": task.result or {},
            "logs": list(task.logs),
        }
        if cancel_info:
            out["calls_started_after_cancel"] = Stub.n_started() - cancel_info["at"]
            out["cancel_to_end_s"] = t_end - t0 - cancel_info["t"]
        return out

    return asyncio.run(_run())


def _by_type(saved: dict) -> dict:
    names = sorted({s["synthesizer_name"] for s in saved["samples"]})
    return {n: sum(1 for s in saved["samples"] if s["synthesizer_name"] == n) for n in names}


def _qa(r) -> int:
    return sum(r["calls_by_prompt"].get(k, 0) for k in QA)


# ---------------- 1. 比例 ----------------

def test_zero_ratio_types_are_not_submitted():
    r = run_case("zero-ratio", dist=ONLY_SINGLE, size=4)
    assert r["status"] == "succeeded" and len(r["saved"][0]["samples"]) == 4
    assert not any(k in r["calls_by_prompt"] for k in ("概念组合", "多跳出题"))


def test_ratios_are_normalized():
    r = run_case("normalize", dist={"single_hop_specific": 1, "multi_hop_abstract": 1, "multi_hop_specific": 1},
                 size=6)
    assert r["status"] == "succeeded" and len(r["saved"][0]["samples"]) == 6, "三项都填 1，size=6 → 6 条而不是 18 条"
    assert _qa(r) == 6


# ---------------- 2. 预检 ----------------

def test_precheck_fails_with_zero_tokens_on_wrong_relation_property():
    r = run_case("precheck", dist={"single_hop_specific": 0.5, "multi_hop_abstract": 0.5, "multi_hop_specific": 0},
                 size=4, rp="entities_overlap")
    assert r["status"] == "failed" and r["done_events"] == ["failed"]
    assert r["llm_calls_total"] == 0
    assert "entities_overlap_score" in (r["error"] or "")


# ---------------- 3. 失败 ----------------

def test_single_failure_does_not_sink_the_batch():
    r = run_case("one-fail", dist=ONLY_SINGLE, size=5, fail=[("单跳出题", 2)])
    saved = r["saved"][0]
    assert r["status"] == "succeeded" and len(saved["samples"]) == 4 and saved["failed_samples"] == 1


def test_all_failures_still_persist_the_bill():
    r = run_case("all-fail", dist=ONLY_SINGLE, size=3, fail=[("单跳出题", i) for i in range(1, 10)])
    assert r["status"] == "failed" and r["done_events"] == ["failed"]
    assert r["saved"] and len(r["saved"][0]["samples"]) == 0 and r["saved"][0]["usage"]["total_tokens"] > 0


# ---------------- 4. 取消 ----------------

def test_cancel_during_samples_stops_new_calls_and_saves_partial():
    r = run_case("cancel-samples", dist=None, size=20, workers=2, delay=1.0,
                 cancel_when=lambda: Stub.n_started("单跳出题") + Stub.n_started("多跳出题") >= 5)
    assert r["status"] == "canceled" and r["done_events"] == ["canceled"]
    assert r["calls_started_after_cancel"] == 0
    assert r["cancel_to_end_s"] < 3.0
    assert r["saved"] and r["saved"][0]["stop_reason"] == "canceled" and r["saved"][0]["usage"]["total_tokens"] > 0
    assert _qa(r) < 10


def test_cancel_during_scenarios_interrupts_inflight_job():
    r = run_case("cancel-scenarios", dist=None, size=20, workers=2, delay=1.0,
                 cancel_when=lambda: Stub.n_started("主题×人格匹配") >= 2)
    assert r["status"] == "canceled"
    assert r["calls_started_after_cancel"] == 0
    assert r["cancel_to_end_s"] < 3.0
    assert _qa(r) == 0


def test_cancel_during_personas_interrupts_inflight_call():
    """人格阶段的取消必须**打断在途调用**，而不是等它自然跑完（本用例人格调用耗时 2 秒）。"""
    r = run_case("cancel-personas", dist=None, size=20, workers=2, delay=0.05, persona_delay=2.0,
                 cancel_when=lambda: Stub.n_started("人格生成") >= 1)
    assert r["status"] == "canceled"
    assert set(r["calls_by_prompt"]) <= {"人格生成"}
    assert any("人格生成阶段" in m for m in r["logs"])
    # 取消到任务结束远早于人格调用自身耗时（2s），说明在途调用被中断；上限含骨架 ≤1s 的日志冲刷
    assert r["cancel_to_end_s"] < 1.9


def test_persona_calls_respect_max_workers():
    """并发上限对人格阶段同样生效：max_workers=1 时第二个人格不会与第一个同时发出。"""
    r = run_case("persona-workers", dist=None, size=20, workers=1, delay=0.05, persona_delay=2.0,
                 cancel_when=lambda: Stub.n_started("人格生成") >= 1)
    assert r["status"] == "canceled"
    assert r["calls_by_prompt"].get("人格生成") == 1


# ---------------- 5. 预算 ----------------

def test_budget_exceeded_stops_and_saves_bill():
    r = run_case("budget", dist=None, size=20, workers=2, delay=0.1, budget=12000)
    assert r["status"] != "running" and len(r["done_events"]) == 1
    assert r["llm_calls_total"] < 20
    assert r["saved"] and r["saved"][0]["stop_reason"] == "budget_exceeded"
    assert r["calls_after_task_end"] == 0


def test_budget_exceeded_with_samples_is_partial_success():
    # 人格 2 + 场景 20 次 ≈ 33k，进入出题阶段后再触发熔断
    r = run_case("budget-partial", dist=ONLY_SINGLE, size=20, workers=2, delay=0.1, budget=45000)
    assert r["status"] == "succeeded" and r["result"].get("partial") and len(r["saved"][0]["samples"]) > 0
    assert len(r["saved"][0]["samples"]) < 20


# ---------------- 6. 条数口径 / 7. 场景截断 ----------------

def test_split_shown_equals_estimate_equals_actual():
    from app.ragas_adapter import testset as ts
    from app.routers import calc as calc_router

    d3 = {"single_hop_specific": 0.5, "multi_hop_abstract": 0.25, "multi_hop_specific": 0.25}
    shown = calc_router.splits(calc_router.SplitsIn(testset_size=10, distribution=d3))["splits"]
    est = ts.estimate_generation_cost(KG_ID, 10, d3, False)["splits"]
    expected = {"SingleHopSpecificQuery": 5, "MultiHopAbstractQuery": 3, "MultiHopSpecificQuery": 2}
    assert shown == est == expected
    r = run_case("split-10", dist=d3, size=10)
    assert _by_type(r["saved"][0]) == {"single_hop_specific_query_synthesizer": 5,
                                        "multi_hop_abstract_query_synthesizer": 3,
                                        "multi_hop_specific_query_synthesizer": 2}
    assert _qa(r) == 10


def test_scenarios_are_truncated_to_requested_size():
    """只要单跳 37 条：ragas 按 ceil(37/24)=2 逐节点累加会凑出 48 条场景，必须截到 37。"""
    r = run_case("trunc-37", dist=ONLY_SINGLE, size=37, workers=8, delay=0.01, observe=0.3)
    assert len(r["saved"][0]["samples"]) == 37 and r["calls_by_prompt"].get("单跳出题") == 37


# ---------------- 8. 取消路径的协程收尾 ----------------

def test_cancel_does_not_leave_unawaited_coroutines():
    """取消后不应留下「从未 await」的协程——它们只在 GC 时抛 RuntimeWarning，属于 stderr 噪声。

    GC 期警告没法靠 ``-W error`` 变成失败（异常发生在对象析构里，只会被打成
    "Exception ignored"），所以用 catch_warnings(record=True) 显式捕获并断言条数为 0。
    """
    import gc
    import warnings

    gc.collect()  # 先清掉其他用例遗留的对象，避免把旧警告算到本次头上
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        r = run_case("cancel-unawaited", dist=None, size=20, workers=2, delay=1.0,
                     cancel_when=lambda: Stub.n_started("单跳出题") + Stub.n_started("多跳出题") >= 5)
        gc.collect()  # 收紧窗口，让本次取消留下的对象在此刻析构
    leaked = [str(w.message) for w in caught
              if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)]
    assert r["status"] == "canceled" and r["calls_started_after_cancel"] == 0
    assert not leaked, f"取消后留下 {len(leaked)} 个未 await 的协程，例如：{leaked[:3]}"


# ---------------- 9. batch_size 批量路径（取消安全） ----------------

def _batch_executor(**kwargs):
    from app.ragas_adapter.testset_synth import _new_executor

    return _new_executor(desc="batch", show_progress=False, keep_progress_bar=False, **kwargs)


def test_executor_batch_size_runs_all_jobs_in_batches():
    """batch_size 不再被拒绝：分批路径跑完全部 job，且并发确实按批次受控。"""
    import asyncio

    concurrent = 0
    peak = 0
    started_batches: list[int] = []

    async def job(i):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        started_batches.append(i)
        await asyncio.sleep(0.02)
        concurrent -= 1
        return i

    async def _run():
        ex = _batch_executor(batch_size=2)  # 不传 run_config → max_workers=-1（非批量会 5 条全并发）
        for i in range(5):
            ex.submit(job, i)
        return await ex.aresults()

    assert asyncio.run(_run()) == [0, 1, 2, 3, 4]
    assert peak == 2, "必须真的走分批分支：每批最多 batch_size 个并发"
    assert len(started_batches) == 5


def test_executor_batch_cancel_propagates_and_closes_unstarted_coroutines():
    """批内取消：CancelledError 原样抛出、不再派发排队 job、不留下未 await 的协程。"""
    import asyncio
    import gc
    import warnings

    from ragas.run_config import RunConfig

    started: list[int] = []

    async def job(i):
        started.append(i)
        await asyncio.sleep(5)
        return i

    async def _run():
        ex = _batch_executor(batch_size=4, run_config=RunConfig(max_workers=1))
        for i in range(8):
            ex.submit(job, i)
        task = asyncio.create_task(ex.aresults())
        while not started:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        propagated = asyncio.run(_run())
        gc.collect()
    leaked = [str(w.message) for w in caught
              if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)]
    assert propagated, "取消必须原样抛出 CancelledError，不能被批量路径吞掉"
    assert started == [0], "取消后不得再派发同批排队 job，也不得进入下一批"
    assert not leaked, f"批量路径取消后留下 {len(leaked)} 个未 await 的协程，例如：{leaked[:3]}"


def test_executor_batch_cancel_stops_dispatching_later_batches():
    """批间取消：当前批跑完后按取消信号停发，后续批次不派发、不抛异常，返回已完成的结果。"""
    import asyncio
    import gc
    import warnings

    started: list[int] = []

    async def job(i):
        started.append(i)
        await asyncio.sleep(0.05)
        return i

    async def _run():
        ex = _batch_executor(batch_size=2)
        for i in range(6):
            ex.submit(job, i)
        task = asyncio.create_task(ex.aresults())
        while len(started) < 2:
            await asyncio.sleep(0.005)
        ex.cancel()
        return await task

    # 只置标志的取消：取消检查必须先于 as_completed 的 next()，否则每轮都会留下 never-awaited 协程
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        results = asyncio.run(_run())
        gc.collect()
    leaked = [str(w.message) for w in caught
              if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)]

    assert results == [0], "当前批已完成的样本保留，未完成的不再等待"
    assert sorted(started) == [0, 1], "取消后不得再派发后续批次"
    assert not leaked, f"批量路径取消后留下 {len(leaked)} 个未 await 的协程，例如：{leaked[:3]}"


def test_executor_batch_exception_semantics_match_non_batch():
    """异常上报口径与非批量路径一致：False 记 (idx, nan) 继续跑，True 立即抛。"""
    import asyncio
    import math

    async def ok(i):
        await asyncio.sleep(0)
        return i

    async def boom(i):
        raise ValueError(f"boom-{i}")

    async def _collect(raise_exceptions):
        ex = _batch_executor(batch_size=2, raise_exceptions=raise_exceptions)
        for i in range(6):
            ex.submit(boom if i in (1, 4) else ok, i)
        return await ex.aresults()

    soft = asyncio.run(_collect(False))
    assert len(soft) == 6 and soft[0] == 0 and soft[2] == 2 and soft[3] == 3 and soft[5] == 5
    assert math.isnan(soft[1]) and math.isnan(soft[4]), "单条失败记 nan，不拖垮其他批次"

    with pytest.raises(ValueError, match="boom-1"):
        asyncio.run(_collect(True))


def test_batch_path_completes_and_persists_terminal_state(monkeypatch):
    """批量路径的终态与日志沿用骨架：正常完成时样本与账单照常落盘。"""
    import app.ragas_adapter.testset_synth as synth

    real = synth._new_executor
    monkeypatch.setattr(synth, "_new_executor",
                        lambda **kw: real(**{**kw, "batch_size": 2, "show_progress": False}))

    r = run_case("batch-ok", dist=ONLY_SINGLE, size=6, delay=0.01)
    saved = r["saved"][0]
    assert r["status"] == "succeeded" and r["done_events"] == ["succeeded"]
    assert len(saved["samples"]) == 6 and saved["stop_reason"] == "completed"
    assert saved["usage"]["total_tokens"] > 0


def test_cancel_in_batch_path_persists_partial_and_stops_new_calls(monkeypatch):
    """批量路径中途取消：已完成样本 + 账单落盘、不再发新调用，取消及时生效。"""
    import app.ragas_adapter.testset_synth as synth

    real = synth._new_executor
    monkeypatch.setattr(synth, "_new_executor",
                        lambda **kw: real(**{**kw, "batch_size": 2, "show_progress": False}))

    r = run_case("cancel-batch", dist=None, size=20, workers=2, delay=1.0,
                 cancel_when=lambda: Stub.n_started("单跳出题") + Stub.n_started("多跳出题") >= 3)
    assert r["status"] == "canceled" and r["done_events"] == ["canceled"]
    assert r["calls_started_after_cancel"] == 0
    assert r["cancel_to_end_s"] < 3.0
    saved = r["saved"][0]
    assert saved["stop_reason"] == "canceled" and saved["usage"]["total_tokens"] > 0

