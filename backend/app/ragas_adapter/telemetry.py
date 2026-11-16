"""Step 2 生成过程的 LLM 级遥测：调用计数 / token 用量 / 重试与修复 / 阶段追踪。

为什么需要这一层
----------------
ragas 的 tqdm 只有两个进度条：
  · "Generating Scenarios" 总数 = 合成器个数（3）—— 一格代表一个合成器的**全部**场景生成，
    内部可能是几十次 LLM 调用，所以进度条 15 分钟不动是常态，看不出死活；
  · "Generating Samples"  总数 = testset_size —— 这个粒度可用。
再加上 tenacity 的重试（RunConfig.max_retries 默认 10）和 ragas 的 FixOutputFormat 修复链
**完全静默**，token 就在这些看不见的地方烧掉了。

本模块用 langchain 回调（`generate(callbacks=[...])`）把每一次 LLM 调用摊开：
调用了什么 prompt、进出多少 token、耗时多久、失败没有、是不是重发的同一个 prompt。

线程约束（重要）
----------------
回调在 ragas 的工作线程里触发，而 `manager.log` 最终会 `asyncio.Queue.put_nowait`——
跨线程操作事件循环的队列不是线程安全的。所以这里**只加锁写缓冲**，
由主协程（阶段化任务的 JobContext 排水）调 `drain()` / `snapshot()` 取走再落日志。
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - ragas 不可用时本模块不会被引用
    BaseCallbackHandler = object  # type: ignore[assignment,misc]


# ---------- prompt 指纹表：把 ragas 内部 prompt 映射成中文名 ----------

def _build_prompt_registry() -> list[tuple[str, str]]:
    """返回 [(instruction 前缀, 中文名), ...]。

    直接从 ragas 的 prompt 类上读 `instruction`，ragas 升级改文案也能自动跟上，
    不写死字符串。取前 60 字符做前缀匹配（渲染后的 prompt 以 instruction 开头）。
    """
    specs = [
        ("ragas.testset.synthesizers.prompts", "ThemesPersonasMatchingPrompt", "主题×人格匹配"),
        ("ragas.testset.synthesizers.multi_hop.prompts", "ConceptCombinationPrompt", "概念组合"),
        ("ragas.testset.synthesizers.multi_hop.prompts", "QueryAnswerGenerationPrompt", "多跳出题"),
        ("ragas.testset.synthesizers.single_hop.prompts", "QueryAnswerGenerationPrompt", "单跳出题"),
        ("ragas.testset.persona", "PersonaGenerationPrompt", "人格生成"),
        # 生成前增强（投影 KG）：此前记成「未识别 prompt」，看不出摘要与主题各花多少
        ("ragas.testset.transforms.extractors.llm_based", "SummaryExtractorPrompt", "增强·分块摘要"),
        ("ragas.testset.transforms.extractors.llm_based", "ThemesAndConceptsExtractorPrompt", "增强·主题"),
        ("ragas.prompt.pydantic_prompt", "FixOutputFormat", "⚠输出格式修复"),
    ]
    out: list[tuple[str, str]] = []
    for mod_name, cls_name, label in specs:
        try:
            mod = __import__(mod_name, fromlist=[cls_name])
            instruction = getattr(getattr(mod, cls_name), "instruction", "") or ""
        except Exception:
            continue
        instruction = instruction.strip()
        if instruction:
            out.append((instruction[:60], label))
    # 长前缀优先匹配，避免两个 prompt 开头雷同时误判
    out.sort(key=lambda x: -len(x[0]))
    return out


def _billing_unknown(error: BaseException) -> bool:
    """调用失败时供应商是否可能已计费：取消、超时属于「请求已发出、结果未知」。

    鉴权失败 / 参数错误等由服务端明确拒绝的不计入（不会产生费用）。
    """
    if isinstance(error, (TimeoutError,)):
        return True
    name = type(error).__name__
    return "Cancel" in name or "Timeout" in name


def llm_result_token_usage(response: Any) -> tuple[int, int, bool]:
    """从 LangChain 的 LLMResult 取 token 用量：(输入, 输出, 是否拿到)。唯一定义处。

    非流式调用的用量在 `llm_output.token_usage`；流式调用（`streaming=True` + `stream_usage=True`）
    没有 llm_output，用量挂在每条生成消息的 `usage_metadata` 上，按生成逐条累加。
    遥测回调与 Step 2 账单（CostCallbackHandler 的解析函数）共用这里，两处口径一致。
    """
    usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    if usage:
        return (int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                int(usage.get("completion_tokens") or usage.get("output_tokens") or 0), True)
    ti = to = 0
    found = False
    for generations in getattr(response, "generations", None) or []:
        for generation in generations or []:
            meta = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if meta:
                found = True
                ti += int(meta.get("input_tokens") or 0)
                to += int(meta.get("output_tokens") or 0)
    return ti, to, found


class GenerationTelemetry(BaseCallbackHandler):  # type: ignore[misc]
    """按 LLM 调用粒度累计用量与异常，线程安全缓冲，供主协程消费。

    Parameters
    ----------
    token_budget : int
        token 预算上限（输入+输出累计）。>0 时超限置 `budget_exceeded`，
        由主协程决定是否中止；0 = 不限。
    """

    # langchain 要求：非 LLM 事件不需要时可声明忽略，减少无谓回调开销
    ignore_retriever = True
    ignore_agent = True
    raise_error = True

    def __init__(self, token_budget: int = 0, on_budget_exceeded=None) -> None:
        self._lock = threading.Lock()
        self._msgs: list[str] = []
        self._registry = _build_prompt_registry()

        self.token_budget = token_budget
        self.budget_exceeded = False
        # 超预算的即时钩子：在回调线程里直接触发（须线程安全，且不得碰 manager.log），
        # 不等主协程 1s 轮询——轮询间隙里还可能又发出一批调用
        self._on_budget_exceeded = on_budget_exceeded

        # 累计量
        self.calls_started = 0
        self.calls_ok = 0
        self.calls_failed = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.embedding_tokens = 0
        self.embedding_calls = 0
        self.embedding_calls_failed = 0
        self.usage_missing_calls = 0
        # G5：被取消 / 超时的在途调用——请求可能已被供应商计费，但拿不到 usage
        self.usage_unknown_calls = 0
        self.est_unreported_input_chars = 0
        self.fix_calls = 0          # FixOutputFormat 调用次数（=解析失败次数）
        self.retry_calls = 0        # 同一 prompt 被重复发送的次数（tenacity 重试）
        self.phase = ""
        self.by_prompt: dict[str, dict[str, int]] = {}

        self._inflight: dict[str, tuple[float, int, str]] = {}  # run_id -> (ts, chars, label)
        self._prompt_seen: dict[str, int] = {}                  # prompt hash -> 次数
        self._t0 = time.time()

    # ---------- 内部工具 ----------

    def _label_of(self, text: str) -> str:
        head = (text or "").lstrip()[:60]
        for prefix, label in self._registry:
            if head.startswith(prefix[: len(head)]) or prefix in (text or "")[:400]:
                return label
        return "未识别 prompt"

    def _emit(self, msg: str) -> None:
        self._msgs.append(msg)

    def _bump(self, label: str, key: str, n: int = 1) -> None:
        slot = self.by_prompt.setdefault(
            label, {"calls": 0, "in": 0, "out": 0, "failed": 0})
        slot[key] = slot.get(key, 0) + n

    def _on_start(self, run_id: Any, text: str) -> None:
        if self.budget_exceeded or (getattr(self, "should_stop", None) and self.should_stop()):
            raise RuntimeError("生成已取消或达到token预算，禁止发起新的LLM请求")
        label = self._label_of(text)
        h = hashlib.md5((text or "").encode("utf-8", "ignore")).hexdigest()
        with self._lock:
            self.calls_started += 1
            self._bump(label, "calls")
            self._inflight[str(run_id)] = (time.time(), len(text or ""), label)

            seen = self._prompt_seen.get(h, 0) + 1
            self._prompt_seen[h] = seen
            if seen > 1:
                # 同一 prompt 再次发出 = tenacity 重试（或 ragas 内部重发）
                self.retry_calls += 1
                self._emit(f"↻ 重试：{label}（同一 prompt 第 {seen} 次发送，{len(text or ''):,} 字符）")
            if label.startswith("⚠"):
                self.fix_calls += 1
                self._emit(f"⚠ 解析失败触发格式修复：重发原 prompt + 原输出（{len(text or ''):,} 字符）")

    # ---------- langchain 回调 ----------

    def on_chat_model_start(self, serialized, messages, *, run_id=None, **kw: Any) -> None:
        text = ""
        try:
            text = "\n".join(getattr(m, "content", "") or ""
                             for batch in (messages or []) for m in (batch or []))
        except Exception:
            pass
        self._on_start(run_id, text)

    def on_llm_start(self, serialized, prompts, *, run_id=None, **kw: Any) -> None:
        self._on_start(run_id, "\n".join(prompts or []))

    def on_llm_end(self, response, *, run_id=None, **kw: Any) -> None:
        ti = to = 0
        usage = {}
        try:
            ti, to, found = llm_result_token_usage(response)
            if found:
                usage = {"input_tokens": ti, "output_tokens": to}
        except Exception:
            pass
        fire = False
        with self._lock:
            started, chars, label = self._inflight.pop(str(run_id), (time.time(), 0, "?"))
            self.calls_ok += 1
            self.input_tokens += ti
            self.output_tokens += to
            self._bump(label, "in", ti)
            self._bump(label, "out", to)
            total = self.input_tokens + self.output_tokens + self.embedding_tokens
            if not usage:
                self.usage_missing_calls += 1
            cost = f"{ti:,}→{to:,} token" if (ti or to) else f"{chars:,} 字符(无 usage 回传)"
            self._emit(f"· {label} 完成，{cost}，耗时 {time.time() - started:.1f}s"
                       f"｜累计 {total:,} token / {self.calls_ok} 次调用")
            if self.token_budget and total >= self.token_budget and not self.budget_exceeded:
                self.budget_exceeded = True
                fire = True
                self._emit(f"⛔ 已超 token 预算（{total:,} > {self.token_budget:,}），准备中止任务")
        if fire and self._on_budget_exceeded is not None:
            try:
                self._on_budget_exceeded()
            except Exception:
                pass

    def on_llm_error(self, error, *, run_id=None, **kw: Any) -> None:
        with self._lock:
            started, chars, label = self._inflight.pop(str(run_id), (time.time(), 0, "?"))
            self.calls_failed += 1
            self._bump(label, "failed")
            if _billing_unknown(error):
                # 请求已发出后被取消 / 超时：供应商可能已计费，账单只能标为不完整（G5）
                self.usage_unknown_calls += 1
                self.est_unreported_input_chars += chars
            msg = str(error).replace("\n", " ")[:300]
            self._emit(f"✗ {label} 调用失败（耗时 {time.time() - started:.1f}s）：{msg}")

    def on_chain_start(self, serialized, inputs, **kw: Any) -> None:
        name = ""
        try:
            name = (serialized or {}).get("name") or kw.get("name") or ""
        except Exception:
            pass
        if name in ("Scenario Generation", "Sample Generation", "ragas testset generation"):
            with self._lock:
                self.phase = name
                self._emit(f"▶ 进入阶段：{name}")

    # ---------- 主协程消费 ----------

    def begin_embedding(self) -> None:
        with self._lock:
            if self.budget_exceeded or (getattr(self, "should_stop", None) and self.should_stop()):
                raise RuntimeError("生成已取消或达到 token 预算，禁止发起新的 Embedding 请求")
            self.embedding_calls += 1

    def end_embedding(self, usage) -> None:
        # SDK 返回对象；部分兼容服务只有 total_tokens，不能把缺失 usage 记成零费用。
        tokens = getattr(usage, "total_tokens", None)
        if tokens is None and isinstance(usage, dict):
            tokens = usage.get("total_tokens", usage.get("prompt_tokens"))
        fire = False
        with self._lock:
            if tokens is None:
                self.usage_missing_calls += 1
            else:
                self.embedding_tokens += max(0, int(tokens))
            total = self.input_tokens + self.output_tokens + self.embedding_tokens
            if self.token_budget and total >= self.token_budget and not self.budget_exceeded:
                self.budget_exceeded = True
                fire = True
            self._emit(f"· Embedding 完成，累计 {self.embedding_tokens:,} token" +
                       ("（本次无 usage 回传）" if tokens is None else ""))
        if fire and self._on_budget_exceeded:
            self._on_budget_exceeded()

    def fail_embedding(self) -> None:
        with self._lock:
            self.embedding_calls_failed += 1

    def drain(self) -> list[str]:
        with self._lock:
            msgs, self._msgs = self._msgs, []
            return msgs

    def snapshot(self) -> dict:
        with self._lock:
            total = self.input_tokens + self.output_tokens + self.embedding_tokens
            # 快照时仍在途（既无 end 也无 error 回调）的调用同样可能已计费（G5）
            inflight = len(self._inflight)
            unknown = self.usage_unknown_calls + inflight
            unreported_chars = self.est_unreported_input_chars + sum(c for _, c, _ in self._inflight.values())
            missing = self.usage_missing_calls + unknown
            return {
                "llm_calls": self.calls_started,
                "llm_calls_ok": self.calls_ok,
                "llm_calls_failed": self.calls_failed,
                "llm_retries": self.retry_calls,
                "llm_fix_calls": self.fix_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": total,
                "llm_total_tokens": self.input_tokens + self.output_tokens,
                "embedding_tokens": self.embedding_tokens,
                "embedding_calls": self.embedding_calls,
                "embedding_calls_failed": self.embedding_calls_failed,
                "usage_missing_calls": missing,
                "usage_complete": missing == 0,
                "usage_unknown_calls": unknown,
                # 未回传用量的输入 token 粗估下限（按 2.5 字符/token，与文档估算同口径；不含输出）
                "est_unreported_input_tokens": int(unreported_chars / 2.5),
                "budget_scope": "reported_llm_and_embedding_tokens",
                "elapsed_s": int(time.time() - self._t0),
                "phase": self.phase,
                "budget_exceeded": self.budget_exceeded,
            }

    def summary_lines(self) -> list[str]:
        """收尾用的分项账单：哪种 prompt 花了多少 token。"""
        snap = self.snapshot()
        lines = [
            f"■ token 结算：合计 {snap['total_tokens']:,}"
            f"（输入 {snap['input_tokens']:,} / 输出 {snap['output_tokens']:,}）",
            f"■ LLM 调用 {snap['llm_calls']} 次"
            f"（成功 {snap['llm_calls_ok']} / 失败 {snap['llm_calls_failed']}"
            f" / 重试 {snap['llm_retries']} / 格式修复 {snap['llm_fix_calls']}）",
        ]
        with self._lock:
            rows = sorted(self.by_prompt.items(),
                          key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
        for label, r in rows:
            lines.append(
                f"   - {label}: {r['calls']} 次，"
                f"{r['in'] + r['out']:,} token（入 {r['in']:,} / 出 {r['out']:,}）"
                + (f"，失败 {r['failed']}" if r["failed"] else "")
            )
        if snap["llm_retries"] or snap["llm_fix_calls"]:
            lines.append(
                f"   ⚠ 其中 {snap['llm_retries']} 次重试 + {snap['llm_fix_calls']} 次格式修复"
                f" 属于重复发送，是 token 浪费的主要来源"
                f"（重试上限由 max_retries 控制，格式修复由模型 JSON 输出质量决定）"
            )
        return lines
