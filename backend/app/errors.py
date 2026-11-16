"""统一错误模型：非 2xx 只有一种错误体。

    {detail: str, error_code: str, extra?: object}

判定规则：**请求本身没做成 → 非 2xx 错误体；请求做成了、结论是「不通过」→ 2xx 类型化结果**
（例如模型连通性自检、RAGFlow 的 test / probe）。

`error_code` 是机器可读的稳定标识，前端据此给文案；`detail` 一律是人类可读的中文句子；
需要附加结构化信息（字段级校验错误、上游状态码、被引用的数据源）时放进 `extra`，不再平铺到顶层。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

# 没显式给 error_code 的 HTTPException 按状态码兜底（含 FastAPI 自己的 404 / 405 / 422）
_DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
}

_LOC_PREFIXES = frozenset({"body", "query", "path", "header", "cookie"})

# Pydantic 校验错误类型 → 中文提示模板（{field} 为字段路径，其余占位符来自 ctx）
_VALIDATION_TEMPLATES: dict[str, str] = {
    "missing": "{field} 为必填项",
    "missing_argument": "{field} 为必填项",
    "string_too_short": "{field} 不能为空",
    "string_too_long": "{field} 超出长度上限",
    "int_parsing": "{field} 必须是整数",
    "int_type": "{field} 必须是整数",
    "float_parsing": "{field} 必须是数字",
    "float_type": "{field} 必须是数字",
    "bool_parsing": "{field} 必须是布尔值",
    "string_type": "{field} 必须是字符串",
    "list_type": "{field} 必须是数组",
    "dict_type": "{field} 必须是对象",
    "json_invalid": "请求体不是合法的 JSON",
    # 路径参数 / 字段的正则不匹配：不把正则本身暴露给前端（消息对两种 id 规则都成立）
    "string_pattern_mismatch": "{field} 格式不合法（含不支持的字符或长度超限）",
    "literal_error": "{field} 取值不合法",
    "enum": "{field} 取值不合法",
    "greater_than": "{field} 必须大于 {gt}",
    "greater_than_equal": "{field} 不能小于 {ge}",
    "less_than": "{field} 必须小于 {lt}",
    "less_than_equal": "{field} 不能大于 {le}",
    "value_error": "{field} 取值不合法",
}


class ApiError(Exception):
    """带机器可读错误码的业务异常；由本模块的全局处理器输出统一错误体。"""

    def __init__(self, status_code: int, detail: str, error_code: str | None = None,
                 extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code or _DEFAULT_CODES.get(status_code, "error")
        self.extra = extra


def error_body(detail: str, error_code: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一错误体的唯一构造处（路由直接返回 JSONResponse 时也用它，别再手拼字段）。"""
    body: dict[str, Any] = {"detail": detail, "error_code": error_code}
    if extra:
        body["extra"] = extra
    return body


def error_response(status_code: int, detail: str, error_code: str,
                   extra: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(detail, error_code, extra),
                        headers=headers)


def _field_path(err: dict) -> str:
    loc = err.get("loc") or ()
    return ".".join(str(part) for part in loc if part not in _LOC_PREFIXES)


def validation_message(err: dict) -> str:
    """单条校验错误 → 中文提示；自定义校验器抛出的中文原文直接保留。"""
    raw = str(err.get("msg") or "")
    if raw.startswith("Value error, "):
        return raw[len("Value error, "):]
    field = _field_path(err) or "参数"
    template = _VALIDATION_TEMPLATES.get(str(err.get("type") or ""))
    if template:
        ctx = err.get("ctx") if isinstance(err.get("ctx"), dict) else {}
        try:
            return template.format(field=field, **ctx)
        except (KeyError, IndexError):
            return f"{field} 取值不合法"
    return f"{field}：{raw}" if raw else f"{field} 取值不合法"


async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.detail, exc.error_code, exc.extra)


async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail: Any = exc.detail
    error_code: str | None = None
    extra: dict[str, Any] | None = None
    if isinstance(detail, dict):
        # 兼容旧写法 detail={"message": ..., "error_code": ...}：拍平进统一错误体
        payload = dict(detail)
        error_code = str(payload.pop("error_code", "") or "") or None
        message = payload.pop("message", None)
        extra = payload or None
        detail = message if message is not None else detail
    if not isinstance(detail, str):
        detail = str(detail)
    return error_response(exc.status_code, detail,
                          error_code or _DEFAULT_CODES.get(exc.status_code, "error"),
                          extra, headers=exc.headers)


async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 也用统一错误体：`detail` 是中文可读句子，字段级明细放 `extra.errors`。"""
    errors = exc.errors()
    detail = "；".join(dict.fromkeys(validation_message(err) for err in errors)) or "请求参数校验失败"
    return error_response(422, detail, "validation_error", {"errors": [
        {"loc": [str(part) for part in (err.get("loc") or ())],
         "msg": str(err.get("msg") or ""),
         "type": str(err.get("type") or "")}
        for err in errors
    ]})


async def _unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
    """兜底：未捕获异常也给统一错误体（否则返回 text/plain 的 Internal Server Error）。

    具体异常与堆栈只进后端日志，不回给前端。
    """
    log.exception("未捕获异常：%s %s", _request.method, _request.url.path)
    return error_response(500, "服务器内部错误，请查看后端日志", "internal_error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)
