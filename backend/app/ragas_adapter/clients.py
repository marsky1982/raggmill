"""ragas 适配层：真实 ragas 可用时走 SDK，否则演示模式（mock 数据）。"""
from __future__ import annotations

import os

from types import SimpleNamespace
from urllib.parse import urlsplit

from ..config import MODE, RAGAS_AVAILABLE  # noqa: F401

__all__ = ["MODE", "RAGAS_AVAILABLE", "build_llm", "build_embeddings", "per_loop_async_http_client",
           "llm_streaming_enabled", "is_dashscope_endpoint", "resolve_enable_thinking"]

# LLM 流式（SSE）是**可选项，默认关闭**。
# 2026-11-12 起曾默认开启，想绕开网关的 60 秒空闲超时；但实测发现：不少兼容网关只发布非流式接口：`stream=true` 一律 400「streaming is not published by this unary-only
# API Profile」，默认开启会让所有调用失败。改回默认关闭；支持流式的服务商（如 DashScope）可用
# 环境变量 RAGAS_STUDIO_LLM_STREAMING=1 全局开启，或在预设 extra.streaming 里单独开启。
# 长调用若超过网关的空闲超时，需在网关侧调大超时，或改用支持流式的服务商。
_STREAMING_DEFAULT = os.environ.get("RAGAS_STUDIO_LLM_STREAMING", "0").strip().lower() in (
    "1", "true", "yes", "on")


def llm_streaming_enabled(preset: dict) -> bool:
    """该预设的 LLM 调用是否走流式：预设 extra.streaming 优先，其次全局默认（开）。"""
    extra = preset.get("extra") or {}
    if "streaming" in extra:
        return bool(extra["streaming"])
    return _STREAMING_DEFAULT


# DashScope 兼容层默认开启「思考模式」（某推理模型 实测：默认 41/100 token 里 86 是思考 token、
# 1.9 秒；关掉后 15/13、0.9 秒），出题时输出被放大到输入的近 3 倍。这里默认关掉。
# 该字段只在 DashScope 上认：兼容网关与其它服务商收到 `enable_thinking` 会直接 400，
# 所以非 DashScope 端点一律**不发送**这个字段。
_DASHSCOPE_HOSTS = ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com")


def is_dashscope_endpoint(base_url: str | None) -> bool:
    """base_url 的 host 是否属于 DashScope 兼容层（只认完整子域，避免 xxxdashscope... 误判）。

    允许省略协议头（`dashscope.aliyuncs.com/compatible-mode/v1` 这种写法在预设里出现过）。
    """
    raw = (base_url or "").strip()
    if raw and "://" not in raw:
        raw = "//" + raw
    host = (urlsplit(raw).hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _DASHSCOPE_HOSTS)


def resolve_enable_thinking(preset: dict) -> bool | None:
    """该预设的 `enable_thinking` 取值：True/False 表示要发送的值，None 表示不发送该字段。

    非 DashScope 端点恒 None（含显式配了 extra.enable_thinking 的情况——发了会 400）；
    DashScope 端点默认 False（关思考），可用 `extra.enable_thinking` 显式开回来。
    """
    if not is_dashscope_endpoint(preset.get("base_url")):
        return None
    flag = (preset.get("extra") or {}).get("enable_thinking")
    return flag if isinstance(flag, bool) else False


def per_loop_async_http_client(timeout: float | None):
    """给 openai / langchain-openai 用的异步 HTTP 客户端：**每个事件循环各用一份连接池**。

    问题（2026-11-06 rc3 出题实测）：langchain-openai 用 lru_cache 按 (base_url, timeout) 缓存了一个
    模块级共享的 httpx.AsyncClient；httpx 的异步连接池绑在第一次使用它的事件循环上。本工具一次出题会
    依次经过多个事件循环（生成前增强的每个 apply_transforms、人格、场景、样本各自 asyncio.run），
    第二个循环起复用旧连接池即报 `RuntimeError: Event loop is closed`，被 openai 包成
    `APIConnectionError: Connection error`（耗时 0.0s，请求根本没发出去）——增强只成功一半、人格全失败。
    这里按当前运行的事件循环分别创建底层 transport，彻底不共享。
    """
    import asyncio
    import os
    import threading
    import weakref

    import httpx

    # 显式传 transport 时 httpx 不再读代理环境变量，这里自己读一次，保持与默认行为一致
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY") \
        or os.environ.get("all_proxy") or None

    class _PerLoopTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncHTTPTransport]" = \
                weakref.WeakKeyDictionary()
            self._lock = threading.Lock()

        def _current(self) -> httpx.AsyncHTTPTransport:
            loop = asyncio.get_running_loop()
            with self._lock:
                transport = self._by_loop.get(loop)
                if transport is None:
                    transport = httpx.AsyncHTTPTransport(proxy=proxy)
                    self._by_loop[loop] = transport
                return transport

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await self._current().handle_async_request(request)

        async def aclose(self) -> None:
            loop = asyncio.get_running_loop()
            with self._lock:
                transport = self._by_loop.pop(loop, None)
            if transport is not None:
                await transport.aclose()

    return httpx.AsyncClient(transport=_PerLoopTransport(), timeout=timeout, follow_redirects=True)


def build_llm(preset: dict, *, timeout: float | None = 180, sdk_max_retries: int | None = 0, callbacks=None):
    """LLM 配置预设 -> ragas BaseRagasLLM。演示模式返回占位。

    timeout / sdk_max_retries 不传时沿用 langchain-openai 默认（Step 1 走这条）。
    Step 2 显式传 sdk_max_retries=0：openai SDK 自带重试（默认 2 次）会和 ragas RunConfig 的
    tenacity 重试**相乘**（3×3=9 次整份 prompt 重发），界面上的「失败重试上限」形同虚设。
    关掉 SDK 层后，重试全部由 RunConfig.max_retries 一处兜住。
    """
    if not RAGAS_AVAILABLE:
        return {"__demo__": True, "model": preset.get("model")}
    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    from ..storage import preset_plain_key

    kwargs: dict = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if sdk_max_retries is not None:
        kwargs["max_retries"] = sdk_max_retries
    if callbacks:
        kwargs["callbacks"] = callbacks
    # 不用 langchain-openai 的模块级共享异步客户端（跨事件循环复用会瞬间报 Connection error），见上
    kwargs["http_async_client"] = per_loop_async_http_client(timeout)
    from ..storage import REASONING_EFFORTS

    effort = (preset.get("extra") or {}).get("reasoning_effort")
    if effort in REASONING_EFFORTS:
        # 推理强度（思考长度）：Chat Completions 请求体里的 reasoning_effort 字段，空值不发送
        kwargs["reasoning_effort"] = effort
    thinking = resolve_enable_thinking(preset)
    if thinking is not None:
        # 思考模式开关（DashScope 专用，默认关）：只对 DashScope 端点发送，其它服务商发了会 400
        kwargs["extra_body"] = {"enable_thinking": thinking}
    streaming = llm_streaming_enabled(preset)
    if streaming:
        # stream_usage：让服务端在流末尾回传 token 用量（stream_options.include_usage），
        # 否则流式下拿不到用量、账单与预算控制失效。超时语义随之变为「两段输出之间最长间隔」。
        kwargs["streaming"] = True
        kwargs["stream_usage"] = True
    raw = ChatOpenAI(
        base_url=preset.get("base_url") or None,
        api_key=preset_plain_key(preset) or "not-set",
        model=preset.get("model", ""),
        temperature=(preset.get("extra") or {}).get("temperature", 0),
        **kwargs,
    )
    # bypass_n 恒为 True：ragas 不再给 ChatOpenAI 设 n，请求体里没有 `n` 字段，n>1 拆成 n 次单独调用
    # （只有评估指标会用到 n>1）。两个原因：① 兼容网关的请求校验不接受 `n` 字段，
    # 连 n=1 都返回 400 ZodError（2026-11-12 实测）；② 流式下 langchain-openai 只取每个分片的
    # 第一个候选，n>1 会把多个候选拼成一段。
    return LangchainLLMWrapper(raw, bypass_n=True)


def build_embeddings(preset: dict, *, timeout: float = 180, sdk_max_retries: int = 0,
                     telemetry=None, max_retries: int = 3):
    """Embedding 配置预设 -> ragas BaseRagasEmbedding。

    注意：langchain-openai 1.1.x 的 OpenAIEmbeddings 发送的请求体格式
    （input.contents 嵌套结构）不被 DashScope 兼容层接受，会报
    'contents is neither str nor list of str'。
    改用 ragas 原生 OpenAIEmbeddings + AsyncOpenAI client 绕过此问题。
    """
    if not RAGAS_AVAILABLE:
        return {"__demo__": True, "model": preset.get("model")}
    from openai import AsyncOpenAI
    from ragas.embeddings import OpenAIEmbeddings as RagasOpenAIEmbeddings

    from ..storage import preset_plain_key

    key = preset_plain_key(preset) or "not-set"
    base = preset.get("base_url") or None
    model = preset.get("model", "")

    # ragas 原生 wrapper 接受 sync client（embed_text）和 async client（aembed_text）
    # ragas 的 EmbeddingExtractor 优先调用 aembed_text，所以必须传 async client
    async_client = AsyncOpenAI(api_key=key, base_url=base, timeout=timeout, max_retries=sdk_max_retries,
                               http_client=per_loop_async_http_client(timeout))
    openai_client = async_client
    create = async_client.embeddings.create
    if telemetry is not None:
        # ragas 仅保留 embedding 向量，在 SDK 响应丢弃 usage 前计量；不改上游源码。
        create = MeteredEmbeddingEndpoint(create, telemetry, timeout=timeout, max_retries=max_retries).create
    # 计量在内层：拆成单条请求时每条都单独记账
    # openai_client：底层 AsyncOpenAI，供测试与排障检查（如确认用的是按事件循环建的 HTTP 客户端）
    async_client = SimpleNamespace(embeddings=SingleTextFallbackEndpoint(create), openai_client=openai_client)
    return RagasOpenAIEmbeddings(client=async_client, model=model)


class SingleTextFallbackEndpoint:
    """embeddings.create 的包装：服务端声明「一次只能一条文本」时，把批量请求拆成单条并发请求。

    部分兼容网关的文本 Embedding 一次只接受一条文本：数组里多于一个元素即 400
    「supports one text per request; use a string or a singleton array」（2026-11-12 实测）。
    ragas 的 aembed_texts 会把整批文本放进一个请求。这里第一次遇到该错误后记住限制，之后同一客户端
    直接拆分（并发上限 _MAX_CONCURRENCY，结果按原顺序合并，用量相加）；接受批量的服务商不受影响。
    """

    _MAX_CONCURRENCY = 8
    _SINGLE_TEXT_HINTS = ("one text per request", "singleton array")

    def __init__(self, create):
        self._create = create
        self.single_text_only = False

    @classmethod
    def _is_single_text_error(cls, exc: BaseException) -> bool:
        import openai
        return isinstance(exc, openai.BadRequestError) and any(
            hint in str(exc).lower() for hint in cls._SINGLE_TEXT_HINTS)

    async def create(self, **kwargs):
        texts = kwargs.get("input")
        if not isinstance(texts, list) or len(texts) <= 1:
            return await self._create(**kwargs)
        if not self.single_text_only:
            try:
                return await self._create(**kwargs)
            except Exception as exc:  # noqa: BLE001 —— 只吞「一次一条」这一种错误，其余原样抛出
                if not self._is_single_text_error(exc):
                    raise
                self.single_text_only = True
        return await self._create_one_by_one(texts, kwargs)

    async def _create_one_by_one(self, texts: list, kwargs: dict):
        import asyncio

        semaphore = asyncio.Semaphore(self._MAX_CONCURRENCY)

        async def one(text):
            async with semaphore:
                return await self._create(**{**kwargs, "input": [text]})

        responses = await asyncio.gather(*(one(text) for text in texts))
        data = [SimpleNamespace(index=i, embedding=r.data[0].embedding, object="embedding")
                for i, r in enumerate(responses)]

        def total(field: str):
            values = [getattr(getattr(r, "usage", None), field, None) for r in responses]
            return None if any(v is None for v in values) else sum(values)

        usage = SimpleNamespace(prompt_tokens=total("prompt_tokens"), total_tokens=total("total_tokens"))
        return SimpleNamespace(data=data, usage=usage, model=getattr(responses[0], "model", None))


class MeteredEmbeddingEndpoint:
    def __init__(self, create, telemetry, *, timeout: float, max_retries: int):
        self._create = create
        self.telemetry = telemetry
        from ragas.run_config import RunConfig, add_async_retry
        import openai
        config = RunConfig(timeout=timeout, max_retries=max_retries)
        config.exception_types = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
        self._retry = add_async_retry(self._attempt, config)

    async def _attempt(self, **kwargs):
        self.telemetry.begin_embedding()
        try:
            response = await self._create(**kwargs)
        except BaseException:
            self.telemetry.fail_embedding()
            raise
        self.telemetry.end_embedding(getattr(response, "usage", None))
        return response

    async def create(self, **kwargs):
        return await self._retry(**kwargs)


def configure_run(llm, *, timeout: int, retries: int, workers: int):
    from ragas.run_config import RunConfig
    import openai
    config = RunConfig(timeout=timeout, max_retries=retries, max_workers=workers)
    llm.set_run_config(config)
    # Wrapper的setter会收窄异常类型，必须在其后设置。
    config.exception_types = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)
    return config
