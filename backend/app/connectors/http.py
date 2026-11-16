"""连接器层的 HTTP 基座（2026-11-13，架构演进阶段 4-③）。

统一五件事：**协议白名单**（只允许 http/https）、**只跟随同源重定向**（跨源直接拒绝，
认证头不会被带给第三方）、**超时**、**有限重试**、**分页上限常量**。

## 为什么对外还是「urllib 形状」

`HttpTransport.open(request, timeout=)` 的签名与 urllib opener 一致：入参是
`urllib.request.Request`，失败抛 `urllib.error.HTTPError` / `URLError`。
RAGFlow 客户端的判成功规则（`application/json` + 可解析对象 + 业务 `code == 0`）
和既有打桩测试都建立在这个形状上，换成 httpx 原生 API 会连带改掉 20 多处测试桩。
内部实现是完全的 httpx，调用方不需要知道这一点。

## 重试口径

- **只重试 GET / HEAD**（幂等）。POST 等一律只发一次。
- 只对连接错误 / 超时 / 5xx / 429 重试；4xx 不重试。
- 次数与退避由构造参数给定（`max_attempts` 含首次；指数退避封顶 `backoff_max_seconds`）。
- **业务错误码不在这里判断**：那要 HTTP 200 + 可解析 JSON 才能看到，属于调用方（RAGFlow）
  的判成功规则，本层看到 200 就返回。
- 上限常量 `DEFAULT_MAX_PAGES` 是「分页最多翻多少页」的统一政策，但翻页过程中
  的重复页 / 总数漂移 / 形状兼容是各服务自己的逻辑，不在本层。

## LLM / Embedding 客户端不并入（两条付过学费的铁律）

1. 必须按事件循环创建（`clients.per_loop_async_http_client`）：共享实例会复发 rc3 的
   「0.0 秒 `APIConnectionError`」，连接池绑在第一个事件循环上。
2. 不加自动重试：LLM 重试只由 `RunConfig` 一处兜住、SDK 层 0 次，再加会变成乘数
   （见 实现基线「Step 2 的 token 成本 → 重试相乘」）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

import httpx

log = logging.getLogger("app.connectors.http")

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# 分页上限的统一政策：单页条数与最多翻多少页。调用方可用环境变量覆盖页数上限。
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 500

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({429})


def origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port)；端口缺省按协议补全，未知协议为 None。"""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    return scheme, (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme)


def require_http_url(url: str) -> None:
    """协议白名单：只接受 http/https 且必须带主机名。"""
    scheme, host, _ = origin(url)
    if scheme not in ALLOWED_SCHEMES or not host:
        raise ValueError(f"只支持 http/https 地址: {url!r}")


def normalize_base_url(base_url: str) -> str:
    """规范化并校验服务 base_url：只允许 http/https 且必须带主机名。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("RAGFlow base_url 不能为空")
    scheme, host, _ = origin(base)
    if scheme not in ALLOWED_SCHEMES or not host:
        raise ValueError(f"RAGFlow base_url 只支持 http/https 地址: {base_url!r}")
    return base


def page_limit_message(label: str, max_pages: int, *, env_hint: str) -> str:
    """分页超过上限时的统一文案（唯一出处，调用方只传标签与环境变量名）。"""
    return (
        f"{label}分页超过上限 {max_pages} 页，已中止（防止无限翻页）；"
        f"确认不是死循环后可调大 {env_hint}"
    )


class HttpResponse:
    """urllib 响应形状（status / headers / read / 上下文管理）的薄包装。"""

    def __init__(self, status: int, headers: Any, content: bytes):
        self.status = status
        self.headers = headers
        self._content = content

    def read(self) -> bytes:
        return self._content

    def __enter__(self) -> "HttpResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class HttpTransport:
    """带协议白名单、同源重定向、超时与有限重试的同步 HTTP 客户端。

    `transport` 参数只为测试注入 `httpx.MockTransport`，生产不传。
    `sleep` 同理，测试里注入空实现，避免退避真的等待。
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        backoff_max_seconds: float = 8.0,
        max_redirects: int = 5,
        default_timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.backoff_max_seconds = max(self.backoff_seconds, float(backoff_max_seconds))
        self.max_redirects = max(0, int(max_redirects))
        self.default_timeout = float(default_timeout)
        self._sleep = sleep
        # httpx.Client 可跨线程共享，但构造会加载 SSL 证书包（读 certifi 文件），
        # 所以推迟到第一次请求时再建；重定向由本层自己走（关掉 httpx 的自动跟随），
        # 才能在每一跳校验同源。
        self._transport = transport
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def _http(self) -> httpx.Client:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(follow_redirects=False, transport=self._transport)
        return self._client

    # --- urllib opener 兼容面 ---

    def open(
        self, request: Request, timeout: float | None = None, attempts: int | None = None,
    ) -> HttpResponse:
        """发一个请求，返回 urllib 形状的响应；失败抛 HTTPError / URLError。

        `attempts` 是本次调用的总尝试次数覆盖（含首次）：`attempts=1` 表示不重试。
        界面上的手动探测（`test` / `probe`）用它换取快速失败，后台同步不传、沿用默认值。
        """
        allow_retry = request.get_method().upper() in ("GET", "HEAD")
        total = self.max_attempts if allow_retry else 1
        if allow_retry and attempts is not None:
            total = max(1, int(attempts))
        last_exc: Exception | None = None
        for attempt in range(1, total + 1):
            try:
                response = self._send(request, timeout)
            except _RetryableNetworkError as exc:
                last_exc = exc
                if attempt >= total:
                    raise URLError(str(exc)) from exc
                self._backoff(request, attempt, total, str(exc))
                continue
            if self._should_retry_status(response.status_code) and attempt < total:
                self._backoff(request, attempt, total, f"HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise self._http_error(request.full_url, response)
            return HttpResponse(response.status_code, response.headers, response.content)
        raise URLError(str(last_exc))  # 理论上不可达：循环内已覆盖所有出口

    # --- 内部实现 ---

    def _send(self, request: Request, timeout: float | None) -> httpx.Response:
        """发请求并手动处理重定向：每一跳都校验协议与同源。"""
        url = request.full_url
        method = request.get_method().upper()
        headers = dict(request.header_items()) if request.headers else {}
        content = request.data
        budget = self.default_timeout if timeout is None else float(timeout)
        base_origin = origin(url)
        for _ in range(self.max_redirects + 1):
            require_http_url(url)
            try:
                response = self._http().request(
                    method, url, headers=headers, content=content, timeout=budget,
                )
            except httpx.UnsupportedProtocol as exc:
                raise ValueError(f"只支持 http/https 地址: {url!r}") from exc
            except (httpx.TimeoutException, httpx.NetworkError,
                    httpx.RemoteProtocolError, httpx.ProxyError) as exc:
                raise _RetryableNetworkError(f"{type(exc).__name__}: {exc}") from exc
            location = response.headers.get("location") if response.status_code in _REDIRECT_STATUSES else None
            if not location:
                return response
            target = urljoin(url, location)
            if origin(target) != base_origin:
                raise HTTPError(
                    url, response.status_code,
                    f"拒绝跨域重定向（{url} → {target}）", response.headers, BytesIO(b""),
                )
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method == "POST"
            ):
                method, content = "GET", None
            url = target
        raise HTTPError(
            request.full_url, 302,
            f"重定向次数超过上限 {self.max_redirects}", None, BytesIO(b""),
        )

    def _should_retry_status(self, status: int) -> bool:
        return status in _RETRYABLE_STATUSES or 500 <= status < 600

    def _backoff(self, request: Request, attempt: int, attempts: int, reason: str) -> None:
        delay = min(self.backoff_seconds * (2 ** (attempt - 1)), self.backoff_max_seconds)
        log.warning(
            "HTTP %s %s 第 %d/%d 次尝试失败（%s），%.1fs 后重试",
            request.get_method(), request.full_url, attempt, attempts, reason, delay,
        )
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _http_error(url: str, response: httpx.Response) -> HTTPError:
        return HTTPError(
            url, response.status_code, f"HTTP {response.status_code}",
            response.headers, BytesIO(response.content),
        )


class _RetryableNetworkError(Exception):
    """连接错误 / 超时 / 协议异常的归一化载体，只在连接器内部流转。"""
