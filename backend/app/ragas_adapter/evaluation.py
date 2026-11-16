"""D1A 离线评估适配层：观察字段门禁、默认五指标和结果制品。"""
from __future__ import annotations

import asyncio
import zlib
from typing import Any

from .. import storage
from ..config import RAGAS_AVAILABLE
from ..jsonutil import sanitize_nonfinite
from ..policies import GenerationPolicy
from ..tasks.manager import Task, manager, worker_call
from ..tasks.staged import (STOP_BUDGET_EXCEEDED, STOP_CANCELED, STOP_FAILED,
                            Stage, StagedJob, UsageLedger)
from .clients import build_embeddings, build_llm, configure_run
from .telemetry import GenerationTelemetry

METRICS = {
    "faithfulness": {"label": "Faithfulness", "required": ["user_input", "response", "retrieved_contexts"], "needs_llm": True, "needs_embedding": False},
    "answer_relevancy": {"label": "Answer Relevancy", "required": ["user_input", "response"], "needs_llm": True, "needs_embedding": True},
    "context_precision": {"label": "Context Precision", "required": ["user_input", "retrieved_contexts", "reference"], "needs_llm": True, "needs_embedding": False},
    "context_recall": {"label": "Context Recall", "required": ["user_input", "retrieved_contexts", "reference"], "needs_llm": True, "needs_embedding": False},
    "answer_correctness": {"label": "Answer Correctness", "required": ["user_input", "response", "reference"], "needs_llm": True, "needs_embedding": True},
}
DEFAULT_METRICS = tuple(METRICS)

# ragas 0.1 fork 的结果字典沿用 metric.name；其中两个指标的 name
# 与本工作台的稳定 key 不相同。保持映射集中在适配层，升级 ragas 时只改这里。
RESULT_KEYS = {
    "faithfulness": ("faithfulness",),
    "answer_relevancy": ("answer_relevancy", "response_relevancy"),
    "context_precision": ("llm_context_precision_with_reference", "context_precision"),
    "context_recall": ("context_recall", "llm_context_recall"),
    "answer_correctness": ("answer_correctness",),
}


def normalize_observations(rows: list[dict], context_limit: int = 5) -> list[dict]:
    if not rows:
        raise ValueError("observations 不能为空")
    normalized = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index + 1} 条 observation 必须是对象")
        item = dict(row)
        item["id"] = str(item.get("id") or f"row-{index + 1}")
        if "user_input" in item and item["user_input"] is not None:
            item["user_input"] = str(item["user_input"])
        if "response" in item and item["response"] is not None:
            item["response"] = str(item["response"])
        contexts = item.get("retrieved_contexts")
        if contexts is not None and (not isinstance(contexts, list) or not all(isinstance(v, str) for v in contexts)):
            raise ValueError(f"第 {index + 1} 条 retrieved_contexts 必须是字符串数组")
        if item.get("reference") is not None:
            item["reference"] = str(item["reference"])
        item["context_limit"] = context_limit
        normalized.append(item)
    return normalized


def capability_report(rows: list[dict], metric_keys: list[str]) -> dict:
    normalized = normalize_observations(rows)
    report = []
    for key in metric_keys:
        if key not in METRICS:
            raise ValueError(f"未知指标: {key}")
        required = METRICS[key]["required"]
        ready = 0
        missing = {}
        empty = {}
        for row in normalized:
            absent = []
            blank = []
            for field in required:
                if field not in row or row[field] is None:
                    absent.append(field)
                    continue
                value = row[field]
                if field == "retrieved_contexts":
                    if not value:
                        blank.append(field)
                elif not value:
                    blank.append(field)
            if absent:
                for field in absent:
                    missing[field] = missing.get(field, 0) + 1
            for field in blank:
                empty[field] = empty.get(field, 0) + 1
            if not absent and not blank:
                ready += 1
        report.append({"metric_key": key, "label": METRICS[key]["label"], "required_fields": required,
                       "ready_count": ready, "missing_count": len(normalized) - ready, "missing_fields": missing,
                       "empty_fields": empty,
                       "context_limit": 5 if key == "context_recall" else None})
    return {"sample_count": len(normalized), "metrics": report}


def _metric_instance(key: str, llm, embeddings, sync_embedding=None):
    from ragas import metrics
    classes = {
        "faithfulness": metrics.Faithfulness,
        "answer_relevancy": metrics.ResponseRelevancy,
        "context_precision": metrics.LLMContextPrecisionWithReference,
        "context_recall": metrics.LLMContextRecall,
        "answer_correctness": metrics.AnswerCorrectness,
    }
    kwargs: dict[str, Any] = {"llm": llm}
    if key in ("answer_relevancy", "answer_correctness"):
        kwargs["embeddings"] = embeddings
    if key == "answer_relevancy":
        # DashScope 兼容层仅支持 n=1；ragas 默认 strictness=3 会以 n=3 发起请求被拒
        kwargs["strictness"] = 1
        # fork 版 ResponseRelevancy.calculate_similarity 调 langchain 风格的
        # embed_query/embed_documents（同步），ragas 原生 OpenAIEmbeddings 没有这两个方法，
        # 且其 sync 路径会「新线程+新事件循环」复用共享 AsyncOpenAI client，
        # httpx 连接池跨事件循环直接 RuntimeError(Event loop is closed)。
        # 所以这里必须用独立同步 client 的适配器。
        kwargs["embeddings"] = sync_embedding or _SyncEmbeddingAdapter_error()
    return classes[key](**kwargs)


def _SyncEmbeddingAdapter_error():
    raise ValueError("answer_relevancy 需要 Embedding 预设构建同步适配器")


# 演示评分的值域（0.55–0.95），不做成配置项
_DEMO_SCORE_BASE = 0.55
_DEMO_SCORE_SPAN = 0.4


def _demo_scores(metric_key: str, row_ids: list[str]) -> list[dict]:
    """演示模式的确定性模拟评分：同 (指标, 观察行) 恒得同分，值域 0.55–0.95。

    只在 RAGAS_AVAILABLE=False（RAGAS_DEMO=1）时被调用，不 import ragas。
    """
    values = []
    for row_id in row_ids:
        digest = zlib.crc32(f"{metric_key}:{row_id}".encode("utf-8"))
        value = round(_DEMO_SCORE_BASE + _DEMO_SCORE_SPAN * (digest % 1000) / 1000, 3)
        values.append({"observation_id": row_id, "value": value,
                       "raw": {"mock": True}, "status": "scored"})
    return values


class _SyncEmbeddingAdapter:
    """给 ResponseRelevancy 提供 langchain 命名的 embed_query/embed_documents。

    用独立同步 OpenAI client 直连，每次调用带重试与遥测计量
    （口径与 clients.MeteredEmbeddingEndpoint 一致：重试上限 N = 最多发 N 次）。
    """

    def __init__(self, preset: dict, *, timeout: float = 180, sdk_max_retries: int = 0,
                 max_retries: int = 3, telemetry=None):
        from openai import OpenAI

        key = storage.preset_plain_key(preset) or "not-set"
        self._client = OpenAI(api_key=key, base_url=preset.get("base_url") or None,
                              timeout=timeout, max_retries=sdk_max_retries)
        self._model = preset.get("model", "")
        self._max_retries = max(1, max_retries)
        self._telemetry = telemetry

    def _create(self, **kwargs):
        import openai
        last: Exception | None = None
        for _ in range(self._max_retries):
            if self._telemetry is not None:
                self._telemetry.begin_embedding()
            try:
                response = self._client.embeddings.create(**kwargs)
            except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
                last = exc
                if self._telemetry is not None:
                    self._telemetry.fail_embedding()
                continue
            except BaseException:
                if self._telemetry is not None:
                    self._telemetry.fail_embedding()
                raise
            if self._telemetry is not None:
                self._telemetry.end_embedding(getattr(response, "usage", None))
            return response
        raise last

    def embed_query(self, text: str) -> list[float]:
        response = self._create(input=text, model=self._model)
        return response.data[0].embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        response = self._create(input=texts, model=self._model)
        return [item.embedding for item in response.data]



async def score_offline(task: Task, rows: list[dict], metric_keys: list[str],
                        llm_preset: dict, emb_preset: dict, policy: GenerationPolicy,
                        name: str = "") -> dict:
    """离线评估：按指标逐步评分，走统一 ``StagedJob`` 骨架。

    终态（``stop_reason``）：``completed`` / ``canceled`` / ``budget_exceeded`` / ``failed``；
    每种结局都先把评估 JSON 落盘，再由骨架决定返回或抛出。落盘键与既有格式保持一致
    （``status`` / ``stop_reason`` / ``usage`` / ``scores`` 等），``usage`` 仍是遥测快照。
    """
    normalized = normalize_observations(rows)
    capability = capability_report(normalized, metric_keys)
    tele = getattr(task, "generation_telemetry", None) or GenerationTelemetry(token_budget=policy.token_budget)
    tele.should_stop = task.cancel_event.is_set
    task.generation_telemetry = tele
    task.policy = policy.execution_kwargs()
    llm = build_llm(llm_preset, timeout=policy.llm_timeout, sdk_max_retries=0, callbacks=[tele])
    # 演示模式（RAGAS_DEMO=1）下 build_llm 返回桩 dict，configure_run 会当场 AttributeError，
    # 评估任务必然秒失败——所以真 ragas 侧的构造只在 RAGAS_AVAILABLE 时做，
    # 评分改走 _demo_scores 确定性模拟（见 make_stage 内分支）。
    run_config = None
    embeddings = None
    sync_embedding = None
    if RAGAS_AVAILABLE:
        run_config = configure_run(llm, timeout=policy.llm_timeout, retries=policy.max_retries, workers=policy.max_workers)
    needs_embeddings = any(METRICS[key]["needs_embedding"] for key in metric_keys)
    if needs_embeddings:
        if not emb_preset:
            raise ValueError("所选指标需要 Embedding 预设")
        if RAGAS_AVAILABLE:
            embeddings = build_embeddings(emb_preset, timeout=policy.llm_timeout, sdk_max_retries=0,
                                          telemetry=tele, max_retries=policy.max_retries)
            if "answer_relevancy" in metric_keys:
                sync_embedding = _SyncEmbeddingAdapter(emb_preset, timeout=policy.llm_timeout, sdk_max_retries=0,
                                                       max_retries=policy.max_retries, telemetry=tele)
    scores: list[dict] = []
    evaluation_id = storage.new_id("eval")
    data = {"id": evaluation_id, "name": name or f"离线评估-{storage.now_iso()}", "created_at": storage.now_iso(),
            "status": "running", "metric_keys": metric_keys, "sample_count": len(normalized),
            "observations": normalized, "capability": capability, "scores": scores, "usage": tele.snapshot(),
            "context_recall_limit": 5}
    if not RAGAS_AVAILABLE:
        # mock 标记必须在第一次写盘之前落进去：否则两步之间进程退出，落盘文件就没有这个标记
        data["is_mock"] = True
    await asyncio.to_thread(storage.evaluation_write, evaluation_id, data)
    if not RAGAS_AVAILABLE:
        manager.log(task, "⚠ 警告：当前为 MOCK 演示评分，分数为确定性模拟值，不代表模型质量",
                    extra={"stage": "demo_mock", "progress": 0.02})

    def save(snapshot: dict) -> None:
        data["scores"] = scores
        data["usage"] = snapshot
        storage.evaluation_write(evaluation_id, data)

    def make_stage(metric: dict) -> Stage:
        key = metric["metric_key"]

        async def _stage(ctx) -> None:
            if metric["ready_count"] == 0:
                scores.append({"metric_key": key, "status": "missing_input", "scores": [], "capability": metric})
                await asyncio.to_thread(save, tele.snapshot())
                return
            metric_rows = []
            row_ids = []
            for row in normalized:
                if row["id"] in row_ids:
                    raise ValueError(f"observation id 重复: {row['id']}")
                required = METRICS[key]["required"]
                if any(field not in row or row[field] is None or not row[field] for field in required):
                    continue
                item = {field: row[field] for field in required}
                if key == "context_recall":
                    item["retrieved_contexts"] = item["retrieved_contexts"][:5]
                metric_rows.append(item)
                row_ids.append(row["id"])
            if task.cancel_event.is_set():
                raise asyncio.CancelledError

            def run_one():
                from ragas import evaluate
                from ragas.dataset_schema import EvaluationDataset
                dataset = EvaluationDataset.from_list(metric_rows)
                result = evaluate(dataset, metrics=[_metric_instance(key, llm, embeddings, sync_embedding)],
                                  run_config=run_config, raise_exceptions=False, show_progress=False)
                values = []
                result_rows = getattr(result, "scores", None) or []
                for row_id, item in zip(row_ids, result_rows):
                    raw = sanitize_nonfinite(dict(item))
                    value = next((raw.get(name) for name in RESULT_KEYS[key] if raw.get(name) is not None), None)
                    values.append({"observation_id": row_id, "value": value, "raw": raw,
                                   "status": "scored" if value is not None else "no_score"})
                return values

            if RAGAS_AVAILABLE:
                values = await worker_call(task, run_one)
            else:
                # 演示模式：每个指标停 0.8s，留出可点击「取消」的窗口；模拟分数确定性可复现
                await asyncio.sleep(0.8)
                if task.cancel_event.is_set():
                    raise asyncio.CancelledError
                values = _demo_scores(key, row_ids)
            metric_status = "scored" if any(item["status"] == "scored" for item in values) else "no_score"
            scores.append({"metric_key": key, "status": metric_status, "scores": values, "capability": metric})
            task.usage.update(tele.snapshot())
            ctx.ledger.update(tele.snapshot())
            ctx.log(f"· 指标 {metric['label']} 完成：{metric_status}")
            await asyncio.to_thread(save, tele.snapshot())

        return Stage(f"metric:{key}", _stage)

    async def persist(ctx) -> None:
        has_scores = any(item["status"] == "scored" for item in scores)
        data["scores"] = scores
        data["usage"] = ctx.ledger.snapshot() or tele.snapshot()
        if ctx.stop_reason == STOP_CANCELED:
            data["status"] = "canceled"
            data["stop_reason"] = "canceled"
            data["partial"] = bool(scores)
        elif ctx.stop_reason == STOP_FAILED:
            data["status"] = "failed"
            data["stop_reason"] = "failed"
            data["partial"] = bool(scores)
            data["error"] = ctx.error or "评估失败"
        else:
            data["status"] = "succeeded" if has_scores else "failed"
            data["stop_reason"] = "completed" if has_scores else "failed"
            if not has_scores:
                data["error"] = "所有可评估指标均未返回有效分数"
            if ctx.stop_reason == STOP_BUDGET_EXCEEDED:
                # 与 Step 2 语义一致：超预算也是「有结果就保留、没结果就失败」
                data["stop_reason"] = "budget_exceeded"
                data["partial"] = bool(scores)
                if not has_scores:
                    data["error"] = f"token 预算已用尽，已保存评估结果 {evaluation_id}"
        await asyncio.to_thread(storage.evaluation_write, evaluation_id, data)

    ledger = UsageLedger()
    ledger.update(tele.snapshot())
    job = StagedJob(task, [make_stage(metric) for metric in capability["metrics"]],
                    policy=policy, persist=persist, ledger=ledger, name=f"离线评估 {evaluation_id}")
    # 预算熔断：超预算时遥测拒绝新调用，骨架在阶段边界收口为 budget_exceeded 终态
    job.ctx.bind_telemetry(tele)
    await job.run()
    return {"evaluation_id": evaluation_id, "status": data["status"], "sample_count": len(normalized),
            "scores": scores, "usage": data["usage"], "is_mock": bool(data.get("is_mock"))}
