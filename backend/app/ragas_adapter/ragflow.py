"""RAGFlow -> RAGAS 投影适配器。

这一层只负责读取 RAGFlow 已经完成的 chunk/结构制品并生成 RAGAS 所需的
chunk 图。它不触发 RAGFlow 解析或知识图谱抽取，也不在本地重新做实体抽取。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .. import config, storage
from ..connectors import http as http_connector
from ..policies import GenerationPolicy
from ..tasks.manager import Task, manager, worker_call
from ..tasks.staged import Stage, StagedJob, UsageLedger
from . import entity_resolution
from .doc_identity import (
    SOURCE_RAGFLOW, apply_identity, ragflow_doc_id_remap, ragflow_document_identity,
)
from .telemetry import GenerationTelemetry

log = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_VERSION = 1
_UUID_NAMESPACE = uuid.UUID("4e1228b7-7e0d-4c2d-9c95-0ccbe438a019")


def _env_int(name: str, default: int) -> int:
    """读整数型环境变量；非法值回落默认并记 warning（默认值保持现网行为）。"""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        log.warning("环境变量 %s=%r 不是整数，回落默认 %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """读浮点型环境变量；非法值回落默认并记 warning。"""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        log.warning("环境变量 %s=%r 不是数字，回落默认 %s", name, raw, default)
        return default


# 分页护栏：RAGFlow 若忽略 page 参数会返回完全相同的一页，
# 不设上限就是死循环。页数/实体行数都用显式上限兜底，触发时记 warning 并中止。
# C2 起改为可配置，默认值与原常量一致，不改变现网行为；
# 单页条数与页数上限的默认值由连接器层统一（2026-11-13 阶段 4-③）。
_PAGE_SIZE = http_connector.DEFAULT_PAGE_SIZE
_MAX_PAGES = _env_int("RAGFLOW_MAX_PAGES", http_connector.DEFAULT_MAX_PAGES)  # page_size=100 时单次分页最多 5 万条
_MAX_ENTITY_ROWS = _env_int("RAGFLOW_MAX_ENTITY_ROWS", 200_000)  # KB 级结构图是单次响应，没有分页参数
# RAGFlow 服务端大库采样阈值（structure_graph_common.py 的 GRAPH_TOP_ENTITIES）。
# 这是 RAGFlow 侧配置，本项目改不了，只能据此判断「实体池是否可能被截断」并提示。
_GRAPH_TOP_ENTITIES_CAP = _env_int("RAGFLOW_GRAPH_TOP_ENTITIES", 256)

# 实体重叠策略。默认值：
# - 分数阈值 0.0：只要共现就建边（原实现无阈值）；
# - 黑名单用「文档频率自适应」门槛 max(最小绝对频次, 比例×分块数)。
# - 建边口径的最小绝对频次 2026-11-11 由 3 改为 5（rc3 / 1.0.0 真实 A/B，报告 §17.16–§17.17）：
#   9 块的业务文档库上门槛原为 max(3, ⌈2.7⌉)=3，把只出现在 3~4 块的具体实体（温控传感器、
#   嘉兴冷链仓、电子回单…）也拉黑，只剩 5 条边；人名豁免对没有人名的业务文档不起作用。
#   改为 5 后只拦几乎每块都有的泛化词；17 块以上由比例决定，不受影响。
# - 消歧候选排除的最小频次**单独**一个参数、保持 3：两边共用一个门槛时，调高建边门槛会让
#   送 LLM 的候选对变多（按对计费）。两种口径相反，见下方注释。
_OVERLAP_MIN_SCORE = _env_float("RAGFLOW_OVERLAP_MIN_SCORE", 0.0)
_OVERLAP_BLACKLIST_DF_RATIO = _env_float("RAGFLOW_OVERLAP_BLACKLIST_DF_RATIO", 0.30)
_OVERLAP_BLACKLIST_MIN_DF = _env_int("RAGFLOW_OVERLAP_BLACKLIST_MIN_DF", 5)
_DISAMBIGUATION_EXCLUDE_MIN_DF = _env_int("RAGFLOW_DISAMBIGUATION_EXCLUDE_MIN_DF", 3)
# 存量投影的隐含建边参数：projection_config 里没有 `overlap` 明细的投影，都是按这组参数建的。
# 刻意**不**跟着上面的默认值改——当前参数与它不同时写明细、复用条件不成立，存量投影下次同步
# 重建一次（0 次 LLM：增强成果按分块继承，节点 id 与正文不变，测试集出处判定不受影响），
# 把旧门槛误删的边补回来；之后 projection_config 带明细，正常复用。
_OVERLAP_LEGACY_PARAMS = {
    "min_score": 0.0,
    "blacklist_df_ratio": 0.30,
    "blacklist_min_df": 3,
}

# 建边黑名单与「LLM 消歧候选排除」是**两套相反口径**，不要合并成一份：
# - 建边（`_overlap_blacklist`）：高频词的意义是「跨块/跨文档主线」。实测 9 块小库里，
#   人名 df 高（严世蕃 9/9、张居正 8/9）恰恰说明它是整段剧情的主线，拉黑后两块分块
#   保留实体数为 0，重叠边从 36 条掉到 5 条（86% 丢失）。所以 `type=person` 永不拉黑。
# - 消歧排除（`_disambiguation_blacklist`）：送 LLM 判定同义候选要按对计费，高频词
#   几乎不可能只靠名字模糊匹配就同义，排除掉能省最多 token，因此继续按纯 df 拉黑。
# 同一份黑名单无法同时满足「建边要保留」与「消歧要排除」。
_PERSON_ENTITY_TYPES = frozenset({"person", "people"})
# 老快照没有 `entity_types`（实体类型）时的兜底：小库上「按 30% 算出的门槛」会把
# 大多数实体判成高频（9 块时门槛=3），退化为「只拉黑每块都出现的通用词」。
_LEGACY_BLACKLIST_SMALL_LIB_CHUNKS = 12


#  P1：chunk 级内容校验。RAGFlow 的 chunk 行没有任何时间戳/指纹字段
# （真实端点实测：只有 available/content/document_id/id/positions 等，无 update_time），
# 所以只靠文档 update_time 会漏掉「只改 chunk 正文」的变更。这里逐文档拉 chunk 列表
# 做内容指纹对比。成本守卫：已存快照分块总数超过预算时跳过，并显式 warning。
_CHUNK_VERIFY_MODE = (os.environ.get("RAGFLOW_CHUNK_VERIFY") or "auto").strip().lower()
_CHUNK_VERIFY_MAX_CHUNKS = _env_int("RAGFLOW_CHUNK_VERIFY_MAX_CHUNKS", 5000)

#  S2：连接测试与知识库探测的固定预算。
# test 只打 3 次有界请求（库列表 / 版本 / 首库文档首页），默认单请求 10s；
# probe 默认抽样 3 篇、上限 10 篇，请求上限 3 + 2×sample_docs。
_PROBE_SAMPLE_DOCS = _env_int("RAGFLOW_PROBE_SAMPLE_DOCS", 3)
_PROBE_TIMEOUT = _env_int("RAGFLOW_PROBE_TIMEOUT", 10)
_PROBE_MIN_SOURCE_RATIO = _env_float("RAGFLOW_PROBE_MIN_SOURCE_RATIO", 0.5)


class RAGFlowError(RuntimeError):
    """RAGFlow 响应不可用或字段契约不满足时抛出。"""


class RAGFlowHTTPError(RAGFlowError):
    """带错误码的上游 HTTP/JSON 失败。

    error_code 供路由层原样返回给前端；upstream_status 是上游 HTTP 状态，
    网络层失败（超时/连不上）时为 None。
    """

    def __init__(self, detail: str, error_code: str = "upstream_error",
                 upstream_status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.error_code = error_code
        self.upstream_status = upstream_status


class SyncLeaseError(RuntimeError):
    """调用方未持有数据源同步租约（2026-11-13）。"""


def _require_sync_lease(task: Task, keys: tuple[str, ...], *, operation: str = "同步") -> None:
    """运行时租约检查：`keys` 必须已由传入任务占用（`manager.create` 写入 `task.resources`）。

    键一律由 `storage.ragflow_sync_resources()` 生成，与路由占用的资源口径一致，不另造键名。
    正常入口先 `manager.create` 再调用本模块，检查自然通过；绕过任务直接调用时没有租约，
    这里直接抛 `SyncLeaseError`，不静默并发。

    两处调用点的防护强度不同（2026-11-13 评审确认）：
    - `sync_ragflow` 是真正的拦截点——任务由路由 / 脚本传入，没占租约就会在这里被拒；
    - `rebind_source` 的检查是自洽性校验——它自己刚 `manager.create` 拿到任务，正常路径不可能失败，
      只有 `manager.create` 被打桩或将来改成不写 `resources` 时才会触发。不要把它算作额外防护。
    """
    held = set(getattr(task, "resources", ()) or ())
    missing = [key for key in keys if key not in held]
    if missing:
        raise SyncLeaseError(
            f"RAGFlow {operation}缺少数据源任务租约（{('、'.join(missing))}）："
            "必须先经任务管理器占租约"
            "（manager.create(\"ragflow_sync\", storage.ragflow_sync_resources(dataset_id, connection_id))），"
            "或由调用方自行占租约后再调用；绕过任务直接调用会被拒绝，不会静默继续。"
        )


def normalize_entity(value: Any) -> str:
    """确定性实体规范化；LLM 消歧由独立 issue 提供。"""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = " ".join(text.split()).casefold()
    return text.strip(" \t\r\n,.;:，。；：、()[]【】{}<>《》\"'")


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_id(kind: str, dataset_id: str, source_id: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{kind}:{dataset_id}:{source_id}"))


def projection_edge_id(edge_type: str, source_id: str, target_id: str) -> str:
    left, right = sorted((str(source_id), str(target_id)))
    return f"{edge_type}:{left}:{right}"


# 连接器层统一协议白名单 / 同源重定向 / 超时 / 有限重试（2026-11-13 阶段 4-③）。
# 三个环境变量只是 RAGFlow 侧的覆盖口，默认值在连接器里；重试只对幂等的 GET 生效，
# 且只覆盖连接错误 / 超时 / 5xx / 429（4xx 与业务错误码不重试）。
_OPENER = http_connector.HttpTransport(
    max_attempts=max(1, _env_int("RAGFLOW_HTTP_RETRIES", 2) + 1),
    backoff_seconds=_env_float("RAGFLOW_HTTP_BACKOFF_SECONDS", 0.5),
    backoff_max_seconds=_env_float("RAGFLOW_HTTP_BACKOFF_MAX", 8.0),
)

normalize_base_url = http_connector.normalize_base_url

# RAGFlow 业务码（HTTP 200 + body.code）的分类；取值见 v0.27.1 api/settings 与 rc3 internal/common/error_code.go。
# 唯一定义处：客户端判定与测试都引用这里。
_AUTH_FAILED_CODES = frozenset({109, 401})            # v0.27.1 AUTHENTICATION_ERROR；rc3 CodeUnauthorized
_NOT_VISIBLE_CODES = frozenset({102, 108, 403, 404})   # v0.27.1 DATA_ERROR；rc3 Permission / Forbidden / NotFound


def _extract_rows(payload: Any, keys: tuple[str, ...], label: str) -> tuple[str, list[dict], int | None]:
    """从 RAGFlow 列表响应取行，兼容 `data` 为数组 / 分页 dict 两种形状。

    `_get()` 会把 `data` 为 dict 的内层展开、为数组时原样返回整个 body，
    所以这里必须同时处理：
    - `{"code":0,"data":[...]}`                     → array（完整数组，无分页）
    - `{"code":0,"data":{"datasets":[...],...}}`    → paged（`_get` 返回内层 dict）
    - `{"datasets":[...],"total":N}`                → paged（测试桩直接给的形状）
    返回 (shape, rows, total)；形状不符抛 RAGFlowError。
    """
    if isinstance(payload, list):
        return "array", [r for r in payload if isinstance(r, dict)], None
    if isinstance(payload, dict):
        for key in keys:
            rows = payload.get(key)
            if isinstance(rows, list):
                total = payload.get("total")
                return "paged", [r for r in rows if isinstance(r, dict)], total if isinstance(total, int) else None
        data = payload.get("data")
        if isinstance(data, list):
            # v0.27.1 与 rc3 的知识库列表都是 {"data":[...], "total_datasets": N}：数组是**当前页**，
            # 总数在顶层 total_datasets
            total = payload.get("total_datasets", payload.get("total"))
            return "array", [r for r in data if isinstance(r, dict)], total if isinstance(total, int) else None
        if isinstance(data, dict):
            for key in keys:
                rows = data.get(key)
                if isinstance(rows, list):
                    total = data.get("total")
                    return "paged", [r for r in rows if isinstance(r, dict)], total if isinstance(total, int) else None
    expected = " / ".join(keys)
    raise RAGFlowError(f"RAGFlow {label}列表响应形状不符：既不是数组，也没有 {expected} 数组")


class RAGFlowClient:
    """stdlib HTTP client，避免给项目增加新的生产依赖。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30,
                 attempts: int | None = None):
        base = normalize_base_url(base_url)
        self.base_url = base if base.endswith("/api/v1") else f"{base}/api/v1"
        self.api_key = api_key or ""
        self.timeout = max(5, min(int(timeout), 180))
        # 总尝试次数覆盖（含首次）：None = 用连接器默认（后台同步），1 = 不重试
        # （界面上的 test / probe 要快速失败，网络黑洞时不该把 10s 超时放大成 ~31.5s）。
        self.attempts = attempts
        # 供调用方（连接测试/探测）记录最近一次上游状态；不参与重试或判定。
        self.last_status: int | None = None

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET 一个 RAGFlow JSON 端点，失败一律抛带 error_code 的 RAGFlowHTTPError。

        判定口径：HTTP 可达 + Content-Type 含 application/json +
        可解析为对象 + 业务 `code == 0`，四项同时满足才算成功。禁止只看 status == 200。
        """
        query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")}, doseq=True)
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(url, headers=headers, method="GET")
        # attempts 只在显式指定、且 opener 是真实连接器时透传：旧打桩 opener（测试）只认
        # open(request, timeout=)，而它们本来就一次到底、没有重试，不需要这个覆盖。
        open_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.attempts is not None and isinstance(_OPENER, http_connector.HttpTransport):
            open_kwargs["attempts"] = self.attempts
        try:
            with _OPENER.open(request, **open_kwargs) as response:
                status = getattr(response, "status", None)
                content_type = response.headers.get("Content-Type", "") if response.headers else ""
                payload = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            status = exc.code
            headers = exc.headers or {}
            content_type = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
            if status in (401, 403):
                raise RAGFlowHTTPError(
                    "该连接无法通过鉴权：请检查 API key 是否属于当前实例",
                    "upstream_auth_failed", status,
                ) from exc
            raise RAGFlowHTTPError(f"RAGFlow 返回 HTTP {status}: {path}", "upstream_error", status) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RAGFlowHTTPError(
                f"连不上 RAGFlow 服务（{self.base_url}）: {exc}", "upstream_unreachable", None,
            ) from exc
        self.last_status = status
        if "application/json" not in (content_type or "").lower():
            raise RAGFlowHTTPError(
                f"RAGFlow 返回内容不是 JSON（Content-Type={content_type or '缺失'}）: {path}",
                "upstream_not_json", status,
            )
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RAGFlowHTTPError(f"RAGFlow 返回内容无法解析为 JSON: {path}", "upstream_not_json", status) from exc
        if not isinstance(body, dict):
            raise RAGFlowHTTPError(f"RAGFlow 返回的 JSON 不是对象: {path}", "upstream_not_json", status)
        code = body.get("code", 0)
        if code not in (0, None):
            message = body.get("message") or code
            # 鉴权失败也可能以 HTTP 200 + 业务码出现（rc3 统一权限层：401 未登录；v0.27.1：109）
            if code in _AUTH_FAILED_CODES:
                raise RAGFlowHTTPError(
                    f"该连接无法通过鉴权：{message}", "upstream_auth_failed", status,
                )
            # 知识库 / 文档不可见：v0.27.1 回 102「You don't own the dataset」，rc3 回 403 / 404 / 108。
            # rc3 对无权限资源会隐藏存在性，分不清「ID 填错」和「没权限」，统一按「看不到」报
            if code in _NOT_VISIBLE_CODES and path.lstrip("/").startswith("datasets/"):
                raise RAGFlowHTTPError(
                    f"该连接看不到这个知识库或文档（ID 不存在或无权访问）：{message}",
                    "dataset_not_found", status,
                )
            raise RAGFlowHTTPError(f"RAGFlow 返回错误: {message}", "upstream_error", status)
        return body

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        body = self._request_json(path, params)
        return body.get("data") if isinstance(body.get("data"), dict) else body

    def _paginate(self, path: str, key: str, params: dict[str, Any], label: str) -> list[dict]:
        """带护栏的分页拉取。

        三种失控形态都必须中止而不是静默返回半份数据：
        1) 服务端忽略 page 参数 → 第二页与已抓取页完全相同 → 重复页检测；
        2) 服务端永远返回整页（或分页无尽头）→ 达到页数上限；
        3) 翻页期间列表发生变化（有行被删除 / 排序键被更新，后面的行整体前移）→ 漏读。
           判据：各页报告的 total 不一致，或读到的行数少于 total。只比行数不够——删掉一篇已读的、
           漏掉一篇未读的，行数恰好相等。漏读的文档会被当成「已删除」、连带误标测试集 stale，
           所以先整轮重读一次，仍不稳定就中止本次同步（失败的同步不会覆盖上一份投影）。
           同时增删各一篇且 total 不变的情况判不出来，属已知局限。
        行按 id 去重：列表在翻页期间后移时同一行可能出现在两页。
        """
        rows, total, stable = self._paginate_once(path, key, params, label)
        if total is None or (stable and len(rows) >= total):
            return rows
        log.warning("RAGFlow %s列表读到 %d 行、上游报告 %d 行%s，可能在翻页期间发生变化，重读一次",
                    label, len(rows), total, "" if stable else "（各页 total 不一致）")
        rows, total, stable = self._paginate_once(path, key, params, label)
        if total is None or (stable and len(rows) >= total):
            return rows
        raise RAGFlowError(
            f"RAGFlow {label}列表读取不完整：读到 {len(rows)} 行，上游报告 {total} 行"
            f"{'' if stable else '，且翻页期间总数在变化'}。为避免把漏读的{label}误判为已删除，"
            f"本次同步已中止，上一份投影保持不变，可稍后重试"
        )

    def _paginate_once(self, path: str, key: str, params: dict[str, Any],
                       label: str) -> tuple[list[dict], int | None, bool]:
        """整轮翻页一次；返回 (按 id 去重后的行, 最后一页报告的 total, 各页 total 是否一致)。"""
        out: dict[str, dict] = {}
        total: int | None = None
        totals: set[int] = set()
        seen_pages: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            data = self._get(path, {**params, "page": page, "page_size": _PAGE_SIZE})
            rows = data.get(key)
            if not isinstance(rows, list):
                raise RAGFlowError(f"RAGFlow {label}列表响应缺少 {key} 数组")
            signature = _sha([str(row.get("id")) for row in rows if isinstance(row, dict)])
            if rows and signature in seen_pages:
                log.warning("RAGFlow %s 第 %d 页与已抓取页重复（服务端可能忽略 page 参数），已中止", label, page)
                raise RAGFlowError(
                    f"RAGFlow {label}分页返回重复页，已中止（服务端可能忽略了 page 参数）"
                )
            seen_pages.add(signature)
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    out.setdefault(str(row["id"]), row)
            reported = data.get("total")
            total = reported if isinstance(reported, int) and not isinstance(reported, bool) else None
            if total is not None:
                totals.add(total)
            if len(rows) < _PAGE_SIZE:
                return list(out.values()), total, len(totals) <= 1
        log.warning("RAGFlow %s 分页达到上限 %d 页，已中止", label, _MAX_PAGES)
        raise RAGFlowError(
            http_connector.page_limit_message(
                f"RAGFlow {label}", _MAX_PAGES, env_hint="RAGFLOW_MAX_PAGES",
            )
        )

    def list_documents(self, dataset_id: str) -> list[dict]:
        # A2：按创建时间升序翻页。旧实现按 update_time 升序，同步期间被更新的文档会挪到末尾、
        # 后面的行整体前移，于是漏读一篇（复现：105 篇只读到 104 篇）。create_time 不随更新变化，
        # 新上传的文档排在末尾也不会挤掉已读的行；v0.27.1 与 rc3 都支持（rc3 实测，未知字段会报错）。
        return self._paginate(
            f"datasets/{dataset_id}/documents", "docs",
            {"orderby": "create_time", "desc": "false"}, "文档",
        )

    def list_chunks(self, dataset_id: str, document_id: str) -> list[dict]:
        return self._paginate(
            f"datasets/{dataset_id}/documents/{document_id}/chunks", "chunks", {}, "chunk",
        )

    # ---  S2：发现接口（兼容 data 数组 / 分页 dict 两种形状） ---

    def _list_page(self, path: str, keys: tuple[str, ...], label: str,
                   params: dict[str, Any], page: int, page_size: int) -> dict:
        """取单页列表并归一化。

        真实上游（v0.27.1 / rc3）的数组形状都是服务端分页的当前页；只有当上游**没给总数且返回条数
        超过 page_size**（说明它忽略了分页参数、一次给了全量）时，才在本地切片。
        """
        payload = self._get(path, {**params, "page": page, "page_size": page_size})
        shape, rows, total = _extract_rows(payload, keys, label)
        if shape == "array" and total is None and len(rows) > page_size:
            total = len(rows)
            start = (page - 1) * page_size
            items = rows[start:start + page_size]
            has_more = start + page_size < total
        elif shape == "array":
            items = rows
            has_more = (page * page_size < total) if total is not None else (len(rows) >= page_size)
        else:
            items = rows
            has_more = (page * page_size < total) if total is not None else (len(rows) >= page_size)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            "shape": shape,
            "upstream_status": self.last_status,
        }

    def list_datasets_page(self, page: int = 1, page_size: int = _PAGE_SIZE) -> dict:
        """单个知识库列表页。v0.27.1 返回 data 数组，rc2 可能返回 data.datasets 分页字典。"""
        return self._list_page("datasets", ("datasets",), "知识库", {}, page, page_size)

    def list_documents_page(self, dataset_id: str, page: int = 1, page_size: int = _PAGE_SIZE) -> dict:
        return self._list_page(
            f"datasets/{dataset_id}/documents", ("docs", "documents"), "文档",
            {"orderby": "create_time", "desc": "false"}, page, page_size,
        )

    def list_chunks_page(self, dataset_id: str, document_id: str,
                         page: int = 1, page_size: int = _PAGE_SIZE) -> dict:
        return self._list_page(
            f"datasets/{dataset_id}/documents/{document_id}/chunks", ("chunks",), "chunk",
            {}, page, page_size,
        )

    def list_datasets(self) -> list[dict]:
        """全量知识库列表；分页形状复用既有护栏（重复页 / 页数上限 / 不足一页即结束）。

        数组形状代表服务端一次返回完整列表，取一页即结束（再翻页只会拿到同一份数组）。
        """
        out: list[dict] = []
        seen_pages: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            payload = self._get("datasets", {"page": page, "page_size": _PAGE_SIZE})
            shape, rows, total = _extract_rows(payload, ("datasets",), "知识库")
            signature = _sha([str(row.get("id")) for row in rows if row.get("id")])
            if rows and signature in seen_pages:
                log.warning("RAGFlow 知识库第 %d 页与已抓取页重复（服务端可能忽略 page 参数），已中止", page)
                raise RAGFlowError("RAGFlow 知识库分页返回重复页，已中止（服务端可能忽略了 page 参数）")
            seen_pages.add(signature)
            out.extend(row for row in rows if row.get("id"))
            # 上游没给总数、又一次返回了超过一页的数据：说明它不分页，这就是全量
            if shape == "array" and total is None and len(rows) > _PAGE_SIZE:
                return out
            if total is not None and len(out) >= total:
                return out
            if len(rows) < _PAGE_SIZE:
                return out
        log.warning("RAGFlow 知识库分页达到上限 %d 页，已中止", _MAX_PAGES)
        raise RAGFlowError(
            http_connector.page_limit_message(
                "RAGFlow 知识库", _MAX_PAGES, env_hint="RAGFLOW_MAX_PAGES",
            )
        )

    def document_structure(self, dataset_id: str, document_id: str) -> dict:
        """单文档结构行。**仅用于调试对照**：供料路径不读它。

        RAGFlow 的 KB 级合并任务会做跨文档语义归并（T3-3 实测：文档级名字并集 50 个实体，
        KB 级只有 34 个），逐文档拉取再按名字合并会得到不同的实体池，因此实体来源统一走
        `kb_structure`。
        """
        data = self._get(f"datasets/{dataset_id}/documents/{document_id}/structure/graph")
        if "templates" not in data:
            raise RAGFlowError("RAGFlow 结构图响应缺少 templates 字段")
        return data

    def kb_structure(self, dataset_id: str) -> dict:
        """KB 级结构图行（实体/关系行），实体池的唯一权威来源。

        注意：大库会触发采样（GRAPH_TOP_ENTITIES=256），但只有这个端点带 RAGFlow 已完成
        的实体归并结果；文档级行本地合并不等价（T3-3）。
        """
        data = self._get(f"datasets/{dataset_id}/artifacts/structure", {"kind": "graph"})
        if "templates" not in data:
            raise RAGFlowError("RAGFlow KB 级结构图响应缺少 templates 字段")
        return data

    def get_version(self) -> str | None:
        """读取 RAGFlow 服务版本（GET /api/v1/system/version）。

        该端点无鉴权要求。两种返回形状都认：v0.27.1 / rc3 是 `{"code": 0, "data": "<version>"}`，
        v1.0.0 起改为 `{"code": 0, "data": {"version": "v1.0.0"}}`。拿不到时不抛错，
        由调用方把 None 写进快照并记 warning —— 版本缺失必须显式可见，不能静默省略。
        """
        try:
            payload = self._get("system/version")
        except RAGFlowError as exc:
            log.warning("RAGFlow 版本探测失败（ragflow_version 将记为 None）: %s", exc)
            return None
        # _get 已把 dict 形态的 data 展开一层，所以两种形状要分开取：
        # v1.0.0 的 {"version": ...} 就是 payload 本身；v0.27.1 的字符串版本号仍留在 body["data"]。
        # 再无条件取一次 data 会让 v1.0.0 恒为 None（响应被二次解包）。
        value: Any = payload
        if isinstance(value, dict):
            value = value.get("version") if "version" in value else value.get("data")
        if isinstance(value, dict):
            value = value.get("version")
        if not isinstance(value, str) or not value.strip():
            log.warning("RAGFlow 版本响应缺少可用版本号（ragflow_version 将记为 None）: %r", value)
            return None
        return re.sub(r"^v", "", value.strip(), flags=re.IGNORECASE) or None

    def alteration(self, dataset_id: str) -> dict:
        """读取文档级新增/删除提示；graph 类型没有 chunk 级 changed 列表。"""
        return self._get(f"datasets/{dataset_id}/artifacts/alteration", {"kind": "graph"})


# ---------------------------------------------------------------------------
#  S2：连接测试（同步、≤3 请求）与知识库探测（TaskManager、可取消）
# ---------------------------------------------------------------------------

_STAGE_DEFS: tuple[tuple[str, str], ...] = (
    ("connect", "连通性"),
    ("auth", "鉴权"),
    ("datasets", "知识库列表"),
    ("documents", "文档可读"),
    ("chunks", "分块可读"),
    ("kb_graph", "库级图谱"),
    ("document_graph", "文档级图谱"),
)
_STAGE_KEYS = tuple(key for key, _ in _STAGE_DEFS)


def _stage(key: str, label: str, status: str = "skipped", detail: str = "未检查",
           upstream_status: int | None = None, counts: dict | None = None) -> dict:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "upstream_status": upstream_status,
        "counts": counts or {},
        "checked_at": storage.now_iso() if status != "skipped" else None,
    }


def _new_stages() -> list[dict]:
    """7 个阶段恒在、顺序固定，失败标 fail，未测标 skipped。"""
    return [_stage(key, label) for key, label in _STAGE_DEFS]


def _mark(stages: list[dict], key: str, status: str, detail: str = "",
          upstream_status: int | None = None, counts: dict | None = None) -> None:
    for index, (stage_key, label) in enumerate(_STAGE_DEFS):
        if stage_key == key:
            stages[index] = _stage(stage_key, label, status, detail, upstream_status, counts)
            return


def _classify_upstream(exc: RAGFlowHTTPError) -> str:
    """把上游失败映射成 verdict 枚举（§4.3 的优先级：unreachable / auth_failed / incompatible）。"""
    if exc.error_code == "upstream_unreachable":
        return "unreachable"
    if exc.error_code in ("upstream_auth_failed", "dataset_not_found"):
        # 「看不到这个库」本质是访问问题；具体原因由 result.error_code = dataset_not_found 区分
        return "auth_failed"
    return "incompatible"


def run_connection_test(client: RAGFlowClient, timeout: int = 10) -> dict:
    """连接测试入口：整轮不重试（`attempts=1`）。

    test 是用户在界面上手动点的，遇到网络黑洞时快速失败比重试更重要（超时 10s，
    默认重试会放大到约 31.5s）；后台同步不经过这里，仍沿用连接器默认重试。
    """
    previous = client.attempts
    client.attempts = 1
    try:
        return _run_connection_test(client, timeout)
    finally:
        client.attempts = previous


def _run_connection_test(client: RAGFlowClient, timeout: int = 10) -> dict:
    """同步连接测试：最多 3 次请求（库列表首页 / 版本 / 首库文档首页）。

    后 4 段（chunks / kb_graph / document_graph）在这里恒为 skipped：深度检查必须走
    probe（它会读抽样文档），不在 test 里扩大请求预算。
    0 个可见知识库时直接用 2 次请求判 `graph_empty`（与 probe 的空库判定同口径）。
    """
    stages = _new_stages()
    started = time.monotonic()
    request_count = 0
    probed_version: str | None = None

    def remaining() -> float:
        return timeout - (time.monotonic() - started)

    request_count += 1
    try:
        page = client.list_datasets_page(1, 1)
    except RAGFlowHTTPError as exc:
        verdict = _classify_upstream(exc)
        if verdict == "auth_failed":
            _mark(stages, "connect", "ok", "HTTP 可达，但鉴权失败", exc.upstream_status)
            _mark(stages, "auth", "fail", exc.detail, exc.upstream_status)
        elif verdict == "unreachable":
            _mark(stages, "connect", "fail", exc.detail, None)
        elif exc.error_code == "upstream_not_json":
            _mark(stages, "connect", "fail", exc.detail, exc.upstream_status)
            verdict = "incompatible"
        else:
            # 业务 code != 0：HTTP 与 JSON 都正常，但不能当成成功。
            _mark(stages, "connect", "ok", "HTTP 可达", exc.upstream_status)
            _mark(stages, "auth", "ok", "鉴权通过", exc.upstream_status)
            _mark(stages, "datasets", "fail", exc.detail, exc.upstream_status)
            verdict = "incompatible"
        return {"verdict": verdict, "probed_version": None, "stages": stages,
                "request_count": request_count}
    except RAGFlowError as exc:
        _mark(stages, "connect", "ok", "HTTP 可达", client.last_status)
        _mark(stages, "auth", "ok", "鉴权通过", client.last_status)
        _mark(stages, "datasets", "fail", str(exc), client.last_status)
        return {"verdict": "incompatible", "probed_version": None, "stages": stages,
                "request_count": request_count}

    total = page["total"] if page["total"] is not None else len(page["items"])
    upstream = page.get("upstream_status")
    _mark(stages, "connect", "ok", "HTTP 可达", upstream)
    _mark(stages, "auth", "ok", "鉴权通过", upstream)
    _mark(stages, "datasets", "ok", f"可见 {total} 个知识库", upstream, {"datasets": total})

    if remaining() > 0:
        request_count += 1
        probed_version = client.get_version()

    if total <= 0:
        # 0 个可见知识库 = 没有可出题的图谱素材，与 probe 对空库的判定保持一致（§4.3）。
        # documents 及后 4 段保持 skipped，不额外发请求；不把它当「连通即可用」的 ready。
        return {"verdict": "graph_empty", "probed_version": probed_version, "stages": stages,
                "request_count": request_count,
                "detail": "该账号在此实例下没有可见知识库"}

    first = page["items"][0] if page["items"] else None
    dataset_id = str(first.get("id") or "") if isinstance(first, dict) else ""
    if dataset_id and remaining() > 0:
        request_count += 1
        try:
            docs = client.list_documents_page(dataset_id, 1, 1)
            doc_total = docs["total"] if docs["total"] is not None else len(docs["items"])
            if doc_total > 0:
                _mark(stages, "documents", "ok", f"首个知识库有 {doc_total} 篇文档",
                      docs.get("upstream_status"), {"documents": doc_total})
            else:
                _mark(stages, "documents", "fail", "首个知识库还没有文档",
                      docs.get("upstream_status"), {"documents": 0})
        except RAGFlowHTTPError as exc:
            _mark(stages, "documents", "fail", exc.detail, exc.upstream_status)
        except RAGFlowError as exc:
            _mark(stages, "documents", "fail", str(exc), client.last_status)

    # N9②：test 只回答「这条连接能不能用」（连通 + 鉴权 + 有可见知识库）。
    # 旧实现拿「列表里第一个库」有没有文档来判整条连接：首库恰好为空、其他库正常时，整条连接被判 graph_empty。
    # 首库文档数只作参考；目标库能否出题由 probe（「检查图谱」）回答。后 3 段恒为 skipped 是设计如此，不是未验。
    stages_by_key = {stage["key"]: stage for stage in stages}
    documents = stages_by_key["documents"]
    if documents["status"] == "fail" and "还没有文档" in documents["detail"]:
        _mark(stages, "documents", "skipped",
              "列表里第一个知识库还没有文档（不影响连接判定；请对目标库点「检查图谱」）",
              documents.get("upstream_status"), {"documents": 0})
    # 读首库文档真失败（不是「没有文档」）仍算部分可用：说明这把 key 能列库但读不了内容
    documents_now = next(stage for stage in stages if stage["key"] == "documents")  # _mark 会替换字典，重新取
    verdict = "partial" if documents_now["status"] == "fail" else "ready"
    return {"verdict": verdict, "probed_version": probed_version, "stages": stages,
            "request_count": request_count}


# 文档解析状态的取值（唯一定义处）。v0.27.1 的 run：UNSTART/RUNNING/DONE/FAIL/CANCEL；
# rc3 的 ingestion_status 是任务状态（internal/common/constants.go）：CREATED/SCHEDULED/RUNNING/COMPLETED/
# FAILED/STOPPED/STOPPING，没有任务时为 UNSTART。
_RUN_DONE = frozenset({"DONE", "SUCCESS", "FINISHED", "COMPLETED"})
_RUN_NOT_DONE = frozenset({"RUNNING", "FAIL", "FAILED", "CANCEL", "CANCELED", "PENDING", "UNSTART", "UNKNOWN",
                           "CREATED", "SCHEDULED", "STOPPED", "STOPPING"})


def _doc_run_state(doc: dict):
    """文档解析状态：v0.27.1 叫 `run`，rc3 改名 `ingestion_status`（取值也不同，见 _RUN_DONE / _RUN_NOT_DONE）。"""
    return doc.get("run") or doc.get("ingestion_status")


def _doc_done(doc: dict) -> bool:
    """判断文档是否解析完成。真实行有 run(ingestion_status)/status/progress 三种线索，缺字段时按完成处理。

    rc3 没有 `run`，而它的 `status` 是「启用」标志（未解析的文档也是 "1"）——不先认 ingestion_status，
    所有未完成文档都会被当成完成，探测永远判不出 partial（rc3 实测）。
    """
    run = str(_doc_run_state(doc) or "").strip().upper()
    if run in _RUN_DONE:
        return True
    if run in _RUN_NOT_DONE:
        return False
    status = str(doc.get("status") or "").strip().upper()
    if status in ("DONE", "SUCCESS", "FINISHED", "1"):
        return True
    if status in ("RUNNING", "FAIL", "CANCEL", "CANCELED", "PENDING", "0"):
        return False
    progress = doc.get("progress")
    try:
        return float(progress) >= 1.0
    except (TypeError, ValueError):
        return True


def _structure_entities(data: dict) -> list[dict]:
    rows: list[dict] = []
    for template in data.get("templates") or []:
        if isinstance(template, dict):
            rows.extend(row for row in template.get("entities") or [] if isinstance(row, dict))
    return rows


def _structure_relationships(data: dict) -> int:
    total = 0
    for template in data.get("templates") or []:
        if not isinstance(template, dict):
            continue
        for key in ("relationships", "relations", "edges"):
            rows = template.get(key)
            if isinstance(rows, list):
                total += len(rows)
                break
    return total


def _entity_has_source(row: dict) -> bool:
    for key in ("source_chunk_ids", "source_chunks", "chunk_ids", "source_ids"):
        value = row.get(key)
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


class _ProbeBudgetExceeded(RuntimeError):
    """探测请求数达到上限；调用方据此标 partial，而不是继续放大请求。"""


async def probe_ragflow(task: Task, connection_id: str, dataset_id: str,
                        base_url: str, api_key: str,
                        sample_docs: int | None = None) -> dict:
    """知识库探测：抽样文档读图谱与分块，**不写快照、不建投影、不调 LLM**。

    请求上限 `3 + 2×sample_docs`：3 次固定请求（库列表 / 文档列表 / 库级图谱），
    每篇抽样文档再打 2 次（chunk 首页 + 文档级图谱）。
    """
    sample = sample_docs if isinstance(sample_docs, int) else _PROBE_SAMPLE_DOCS
    sample = max(1, min(sample, 10))
    limit = 3 + 2 * sample
    source_id = storage.ragflow_source_id(connection_id, dataset_id)
    # 手动探测同样不重试：网络黑洞时快速失败，用户自己再点一次即可。
    client = RAGFlowClient(base_url, api_key, timeout=_PROBE_TIMEOUT, attempts=1)
    stages = _new_stages()
    counts: dict[str, int] = {
        "datasets": 0, "documents": 0, "sampled_documents": 0, "chunks": 0,
        "graph_nodes": 0, "graph_edges": 0, "sampled_graphs": 0, "entity_sources": 0,
    }
    used = 0
    stopped_reason: str | None = None
    unfinished_docs = 0
    source_ratio: float | None = None

    async def call(fn, *args):
        nonlocal used
        if task.cancel_event.is_set():
            raise asyncio.CancelledError
        if used >= limit:
            raise _ProbeBudgetExceeded(f"请求数已达上限 {limit}")
        used += 1
        return await worker_call(task, fn, *args)

    def result(verdict: str, detail: str | None = None, error_code: str | None = None) -> dict:
        return {
            "verdict": verdict,
            # 细分原因（如 dataset_not_found）：verdict 枚举只有 6 个，前端据此给更准确的处理建议
            "error_code": error_code,
            "stages": stages,
            "counts": counts,
            "dataset_id": dataset_id,
            # 前端按「连接 + 知识库」认领结果，不必在前端复刻 source_id 的拼法
            "connection_id": connection_id,
            "source_id": source_id,
            "request_count": used,
            "stopped_reason": stopped_reason,
            "detail": detail,
        }

    # 1) 库列表：同时覆盖 connect / auth / datasets
    try:
        page = await call(client.list_datasets_page, 1, 100)
    except RAGFlowHTTPError as exc:
        verdict = _classify_upstream(exc)
        if verdict == "auth_failed":
            _mark(stages, "connect", "ok", "HTTP 可达，但鉴权失败", exc.upstream_status)
            _mark(stages, "auth", "fail", exc.detail, exc.upstream_status)
        elif verdict == "unreachable":
            _mark(stages, "connect", "fail", exc.detail, None)
        else:
            _mark(stages, "connect", "fail", exc.detail, exc.upstream_status)
        return result(verdict, exc.detail)
    except RAGFlowError as exc:
        _mark(stages, "connect", "ok", "HTTP 可达", client.last_status)
        _mark(stages, "auth", "ok", "鉴权通过", client.last_status)
        _mark(stages, "datasets", "fail", str(exc), client.last_status)
        return result("incompatible", str(exc))

    total = page["total"] if page["total"] is not None else len(page["items"])
    upstream = page.get("upstream_status")
    counts["datasets"] = total
    seen = any(str(row.get("id") or "") == dataset_id for row in page["items"])
    detail = f"可见 {total} 个知识库"
    if not seen:
        detail += "（首屏未见目标知识库，继续按 ID 读取）"
    _mark(stages, "connect", "ok", "HTTP 可达", upstream)
    _mark(stages, "auth", "ok", "鉴权通过", upstream)
    _mark(stages, "datasets", "ok", detail, upstream, {"datasets": total})

    # 2) 文档首页
    try:
        docs_page = await call(client.list_documents_page, dataset_id, 1, 100)
    except RAGFlowHTTPError as exc:
        verdict = _classify_upstream(exc)
        if exc.error_code == "dataset_not_found":
            # 能列库、但看不到这个库：ID 填错或没权限
            _mark(stages, "documents", "fail", exc.detail, exc.upstream_status)
            return result(verdict, exc.detail, exc.error_code)
        if verdict == "auth_failed":
            _mark(stages, "auth", "fail", exc.detail, exc.upstream_status)
        elif verdict == "unreachable":
            _mark(stages, "connect", "fail", exc.detail, None)
        else:
            _mark(stages, "documents", "fail", exc.detail, exc.upstream_status)
        return result(verdict, exc.detail)
    except RAGFlowError as exc:
        _mark(stages, "documents", "fail", str(exc), client.last_status)
        return result("incompatible", str(exc))

    docs = [row for row in docs_page["items"] if isinstance(row, dict) and row.get("id")]
    doc_total = docs_page["total"] if docs_page["total"] is not None else len(docs)
    counts["documents"] = doc_total
    if not docs and doc_total == 0:
        _mark(stages, "documents", "fail", "该知识库还没有文档", docs_page.get("upstream_status"),
              {"documents": 0})
        _mark(stages, "chunks", "fail", "没有文档可分块", docs_page.get("upstream_status"))
        _mark(stages, "kb_graph", "fail", "没有文档，图谱为空")
        _mark(stages, "document_graph", "fail", "没有文档，图谱为空")
        return result("graph_empty", "该知识库还没有文档")
    unfinished_docs = sum(1 for row in docs if not _doc_done(row))
    # N6：未解析完成的篇数要露出来（旧实现只用它判 partial，界面上看不到原因）
    counts["unfinished_documents"] = unfinished_docs
    detail = f"可见 {doc_total} 篇文档"
    if unfinished_docs:
        detail += f"，其中 {unfinished_docs} 篇还没解析完"
    _mark(stages, "documents", "ok", detail, docs_page.get("upstream_status"),
          {"documents": doc_total, "unfinished_documents": unfinished_docs})

    # 3) KB 级图谱
    kb_entities: list[dict] = []
    try:
        kb_data = await call(client.kb_structure, dataset_id)
    except RAGFlowHTTPError as exc:
        _mark(stages, "kb_graph", "fail", exc.detail, exc.upstream_status)
    except RAGFlowError as exc:
        _mark(stages, "kb_graph", "fail", str(exc), client.last_status)
    else:
        kb_entities = _structure_entities(kb_data)
        counts["graph_nodes"] = len(kb_entities)
        counts["graph_edges"] = _structure_relationships(kb_data)
        if kb_entities or counts["graph_edges"]:
            _mark(stages, "kb_graph", "ok",
                  f"库级图谱有 {counts['graph_nodes']} 个实体 / {counts['graph_edges']} 条关系",
                  client.last_status, dict(counts))
        else:
            _mark(stages, "kb_graph", "fail", "库级图谱为空（Graph 未编译）", client.last_status)

    # 4) 抽样文档：chunk 首页 + 文档级图谱
    sampled = docs[:sample]
    chunk_ok = 0
    graph_ok = 0
    doc_entities = 0
    doc_sources = 0
    for row in sampled:
        doc_id = str(row.get("id") or "")
        if not doc_id:
            continue
        try:
            chunk_page = await call(client.list_chunks_page, dataset_id, doc_id, 1, 100)
        except _ProbeBudgetExceeded as exc:
            stopped_reason = "request_budget_exceeded"
            _mark(stages, "chunks", "fail", str(exc))
            break
        except RAGFlowHTTPError as exc:
            _mark(stages, "chunks", "fail", exc.detail, exc.upstream_status)
        except RAGFlowError as exc:
            _mark(stages, "chunks", "fail", str(exc), client.last_status)
        else:
            chunk_rows = [r for r in chunk_page["items"] if isinstance(r, dict)]
            counts["chunks"] += len(chunk_rows)
            if chunk_rows:
                chunk_ok += 1
                counts["sampled_documents"] += 1

        try:
            graph_data = await call(client.document_structure, dataset_id, doc_id)
        except _ProbeBudgetExceeded as exc:
            stopped_reason = "request_budget_exceeded"
            _mark(stages, "document_graph", "fail", str(exc))
            break
        except RAGFlowHTTPError as exc:
            _mark(stages, "document_graph", "fail", exc.detail, exc.upstream_status)
        except RAGFlowError as exc:
            _mark(stages, "document_graph", "fail", str(exc), client.last_status)
        else:
            entities = _structure_entities(graph_data)
            if entities:
                graph_ok += 1
                counts["sampled_graphs"] += 1
                doc_entities += len(entities)
                doc_sources += sum(1 for entity in entities if _entity_has_source(entity))

    counts["entity_sources"] = doc_sources
    statuses = {stage["key"]: stage["status"] for stage in stages}
    if chunk_ok:
        _mark(stages, "chunks", "ok",
              f"{chunk_ok}/{len(sampled)} 篇抽样文档可读，共 {counts['chunks']} 个分块",
              client.last_status, {"sampled_documents": counts["sampled_documents"], "chunks": counts["chunks"]})
    elif statuses["chunks"] == "skipped":
        _mark(stages, "chunks", "fail", "抽样文档都没有可读分块", client.last_status)

    if graph_ok:
        detail = f"{graph_ok}/{len(sampled)} 篇抽样文档有图谱，{doc_sources}/{doc_entities} 个实体带来源"
        _mark(stages, "document_graph", "ok", detail, client.last_status,
              {"sampled_graphs": counts["sampled_graphs"], "entity_sources": doc_sources})
    elif statuses["document_graph"] == "skipped":
        _mark(stages, "document_graph", "fail", "抽样文档都没有可读图谱", client.last_status)

    if doc_entities > 0:
        source_ratio = doc_sources / doc_entities

    statuses = {stage["key"]: stage["status"] for stage in stages}
    if stopped_reason:
        verdict = "partial"
    elif statuses["kb_graph"] == "fail" and statuses["document_graph"] == "fail":
        verdict = "graph_empty"
    elif unfinished_docs > 0 or (source_ratio is not None and source_ratio < _PROBE_MIN_SOURCE_RATIO):
        verdict = "partial"
    elif all(statuses[key] == "ok" for key in _STAGE_KEYS):
        verdict = "ready"
    else:
        verdict = "partial"
    return result(verdict)


def _chunk_content(row: dict) -> str:
    return str(row.get("content") or row.get("page_content") or row.get("text") or "")


def _flatten_templates(data: dict) -> list[dict]:
    entities: list[dict] = []
    for template in data.get("templates") or []:
        if not isinstance(template, dict):
            continue
        entities.extend(row for row in template.get("entities") or [] if isinstance(row, dict))
    if len(entities) > _MAX_ENTITY_ROWS:
        log.warning("RAGFlow KB 级实体行数 %d 超过上限 %d，已中止", len(entities), _MAX_ENTITY_ROWS)
        raise RAGFlowError(
            f"RAGFlow KB 级实体行数 {len(entities)} 超过上限 {_MAX_ENTITY_ROWS}，已中止（防止一次同步撑爆内存）；"
            f"确认内存充足后可调大 RAGFLOW_MAX_ENTITY_ROWS，或缩小知识库后再同步"
        )
    if len(entities) >= _GRAPH_TOP_ENTITIES_CAP:
        log.warning(
            "RAGFlow KB 级实体行 %d ≥ 服务端采样阈值 %d，实体池可能已被截断，"
            "投影的 entities_overlap 边会偏少（如需全量请调大 RAGFlow 侧 GRAPH_TOP_ENTITIES）",
            len(entities), _GRAPH_TOP_ENTITIES_CAP,
        )
    return entities


def _apply_kb_entities(sources: dict[str, dict], entity_rows: list[dict]) -> dict[str, str]:
    """把 KB 级实体行按 source_chunk_ids 归到各 chunk 的 entities 属性。

    实体池只认 KB 级端点：RAGFlow 的 KB 合并任务做过跨文档语义归并
    （T3-3 实测：文档级名字并集 50 个实体，KB 级响应只有 34 个）。逐文档结构行
    本地按名字合并无法复现这层归并，会让投影图与 RAGFlow 实际图谱不一致。
    没有 source_chunk_ids 的实体归不到任何 chunk，自然丢弃（T8-3）。

    返回「归一化实体名 → 类型」映射（rc3 实体行字段 `type`，取值如 person/location/org），
    供建边黑名单做人名豁免；行里没有类型时不写该键（视为未知，按通用门槛处理）。
    """
    display: dict[str, str] = {}
    chunk_keys: dict[str, set[str]] = {}
    entity_types: dict[str, str] = {}
    # 按归一化名排序，保证同名不同写法的展示名选择稳定（否则响应行序变化会让指纹抖动）
    for row in sorted(entity_rows, key=lambda r: normalize_entity(
            str(r.get("name") or r.get("entity_name") or ""))):
        name = str(row.get("name") or row.get("entity_name") or "").strip()
        key = normalize_entity(name)
        if not key:
            continue
        display.setdefault(key, name)
        row_type = str(row.get("type") or "").strip().casefold()
        if row_type:
            entity_types.setdefault(key, row_type)
        for chunk_id in row.get("source_chunk_ids") or []:
            chunk_keys.setdefault(str(chunk_id), set()).add(key)

    for source in sources.values():
        for chunk_id, chunk in (source.get("chunks") or {}).items():
            keys = chunk_keys.get(str(chunk_id), set())
            chunk["entities"] = sorted({display[key] for key in keys}, key=str.casefold)
    return entity_types


def _document_source(doc: dict, chunks: list[dict]) -> dict:
    chunk_rows: dict[str, dict] = {}
    for index, row in enumerate(chunks):
        chunk_id = str(row.get("id"))
        chunk_rows[chunk_id] = {
            "id": chunk_id,
            "document_id": str(doc.get("id")),
            "position": index,
            "page_content": _chunk_content(row),
            "content_hash": _sha(_chunk_content(row)),
        }

    for row in chunk_rows.values():
        row["entities"] = []

    return {
        "document": {
            "id": str(doc.get("id")),
            "name": str(doc.get("name") or doc.get("location") or doc.get("id")),
            "update_time": doc.get("update_time"),
            "run": _doc_run_state(doc),
            "status": doc.get("status"),
            "chunk_count": len(chunk_rows),
        },
        "chunks": chunk_rows,
    }


def _projection_fingerprint(snapshot: dict) -> str:
    """只对“会影响投影内容”的字段取指纹。

    与 `source_hash` 不同：`update_time` / `status` / `run` / `chunk_count` 这类元数据变化
    不应触发重建投影（投影里根本不含这些字段），否则文档只要被 RAGFlow 触碰一次，
    就会生成一份内容字节级相同的“新投影”，连带作废旧测试集。
    新旧快照都能算：不依赖快照里是否存有本字段。
    """
    documents = {
        str(doc_id): {"id": str(doc_id), "name": str((doc or {}).get("name") or doc_id)}
        for doc_id, doc in (snapshot.get("documents") or {}).items()
    }
    chunks = {
        str(chunk_id): {
            "id": str(chunk_id),
            "document_id": str((chunk or {}).get("document_id")),
            "position": (chunk or {}).get("position"),
            "page_content": (chunk or {}).get("page_content") or "",
            "entities": (chunk or {}).get("entities") or [],
            "content_hash": (chunk or {}).get("content_hash"),
        }
        for chunk_id, chunk in (snapshot.get("chunks") or {}).items()
    }
    entity_types = {
        str(key): str(value)
        for key, value in (snapshot.get("entity_types") or {}).items()
    }
    return _sha({
        "dataset_id": str(snapshot.get("dataset_id")),
        "documents": documents,
        "chunks": chunks,
        # 实体类型影响建边黑名单（person 豁免），类型变了边就会变；老快照无此键按 {} 比
        "entity_types": entity_types,
    })


def _source_content_signature(source: dict) -> str:
    """单文档的内容签名：chunk id + 正文哈希。

    不含顺序：position 只是上游列表返回的次序，RAGFlow 分块行没有任何排序依据
    （rc3 实测无 positions / 页码 / 时间字段，分块列表的 orderby 参数也被忽略），顺序变了
    不等于正文变了；含进签名会让一次乱序就重建投影、把文档算进「正文变化」。
    不含 entities（实体每次按 KB 级端点重算，由 changed_chunk_ids 另行比较），
    也不含文档名/状态等元数据（这些由 metadata_changed 判定）。
    """
    chunks = source.get("chunks") or {}
    return _sha([
        [str(chunk_id), (chunk or {}).get("content_hash")]
        for chunk_id, chunk in sorted(chunks.items())
    ])


def _keep_previous_order(fresh: dict, previous: dict | None) -> dict:
    """分块集合与正文都没变时沿用上次的顺序，避免仅因上游返回次序变化而重建投影。

    有分块增删或正文变化时，按本次返回的次序重新编号。
    """
    if not previous or _source_content_signature(fresh) != _source_content_signature(previous):
        return fresh
    old_chunks = previous.get("chunks") or {}
    for chunk_id, chunk in (fresh.get("chunks") or {}).items():
        chunk["position"] = (old_chunks.get(chunk_id) or {}).get("position", chunk.get("position"))
    return fresh


def _chunk_verify_enabled(previous: dict, full_refresh: bool) -> tuple[bool, str]:
    """是否逐文档做 chunk 内容校验；第二个返回值是「为什么跳过」（空串表示不跳过）。"""
    if full_refresh:
        return False, ""
    if _CHUNK_VERIFY_MODE == "off":
        return False, "RAGFLOW_CHUNK_VERIFY=off"
    previous_chunks = previous.get("chunks") or {}
    if not previous_chunks:
        return False, ""
    if _CHUNK_VERIFY_MODE == "always":
        return True, ""
    if len(previous_chunks) <= _CHUNK_VERIFY_MAX_CHUNKS:
        return True, ""
    return False, (
        f"已存快照分块数 {len(previous_chunks)} 超过校验预算 {_CHUNK_VERIFY_MAX_CHUNKS}"
        f"（可调 RAGFLOW_CHUNK_VERIFY_MAX_CHUNKS）"
    )


def fetch_snapshot(client: RAGFlowClient, dataset_id: str, previous: dict | None = None, full_refresh: bool = False) -> tuple[dict, dict]:
    """读取当前 source snapshot，并返回 snapshot 与变更摘要。"""
    previous = previous or {}
    previous_docs = previous.get("documents") or {}
    previous_sources = previous.get("document_sources") or {}
    current_docs = {str(row["id"]): row for row in client.list_documents(dataset_id)}
    # 版本锚定（T8-4）：拿不到时字段仍写 None，并在日志里说明原因，不静默省略
    ragflow_version = client.get_version()
    if ragflow_version is None:
        log.warning("快照 ragflow_version 未能获取，记为 None（来源: GET /api/v1/system/version）")
    # 实体池的唯一权威来源是 KB 级端点（T3-3），每次同步都读一次
    kb_entity_rows = _flatten_templates(client.kb_structure(dataset_id))
    previous_alteration: dict = {}
    try:
        previous_alteration = client.alteration(dataset_id)
    except RAGFlowError as exc:
        log.warning("RAGFlow alteration 探测失败，退化为本地快照比较: %s", exc)

    hinted = set(previous_alteration.get("newly_uploaded_doc_ids") or [])
    fetched_sources: dict[str, dict] = {}
    changed_doc_ids: set[str] = set()
    metadata_changed_doc_ids: set[str] = set()
    content_changed_doc_ids: set[str] = set()
    chunk_verified_doc_ids: set[str] = set()
    verify_chunks, verify_skip_reason = _chunk_verify_enabled(previous, full_refresh)
    if verify_skip_reason:
        log.warning("chunk 内容指纹校验已跳过：%s；本次仅按文档元数据判断变更，可能漏检正文改动",
                    verify_skip_reason)
    for doc_id, doc in current_docs.items():
        old = previous_docs.get(doc_id) or {}
        metadata_changed = any(doc.get(k) != old.get(k) for k in ("update_time", "name", "status", "run", "chunk_count"))
        if old and metadata_changed:
            metadata_changed_doc_ids.add(doc_id)
        should_fetch = full_refresh or not previous or doc_id not in previous_sources or metadata_changed or doc_id in hinted
        if should_fetch:
            chunks = client.list_chunks(dataset_id, doc_id)
            fetched_sources[doc_id] = _keep_previous_order(_document_source(doc, chunks),
                                                           previous_sources.get(doc_id))
            continue
        #  P1：元数据没变也要校验一次 chunk 正文（RAGFlow 的 chunk 行没有时间戳）。
        # 签名一致时继续复用旧 source，保证投影指纹/字节级幂等不受影响。
        source = deepcopy(previous_sources[doc_id])
        if verify_chunks:
            try:
                chunks = client.list_chunks(dataset_id, doc_id)
            except RAGFlowError as exc:
                raise RAGFlowError(
                    f"文档 {doc_id} 的 chunk 列表拉取失败，无法确认正文是否变更，已中止本次同步"
                    f"（不静默当作无变化，可重试）: {exc}"
                ) from exc
            chunk_verified_doc_ids.add(doc_id)
            fresh = _document_source(doc, chunks)
            if _source_content_signature(fresh) != _source_content_signature(previous_sources[doc_id]):
                content_changed_doc_ids.add(doc_id)
                source = fresh
        fetched_sources[doc_id] = source

    # 无论文档是否重新拉取，实体归属都按 KB 级口径重算：
    # 实体变化（哪怕文档 update_time 未变）也会体现在 changed_chunk_ids 里
    entity_types = _apply_kb_entities(fetched_sources, kb_entity_rows)

    deleted_doc_ids = set(previous_sources) - set(current_docs)
    previous_chunks = previous.get("chunks") or {}
    current_chunks = {
        chunk_id: chunk
        for source in fetched_sources.values()
        for chunk_id, chunk in (source.get("chunks") or {}).items()
    }
    added_chunk_ids = set(current_chunks) - set(previous_chunks)
    deleted_chunk_ids = set(previous_chunks) - set(current_chunks)
    changed_chunk_ids = {
        chunk_id for chunk_id in set(current_chunks) & set(previous_chunks)
        if current_chunks[chunk_id].get("content_hash") != previous_chunks[chunk_id].get("content_hash")
        or current_chunks[chunk_id].get("entities") != previous_chunks[chunk_id].get("entities")
    }
    changed_doc_ids |= metadata_changed_doc_ids
    changed_doc_ids |= content_changed_doc_ids
    changed_doc_ids |= {str(current_chunks[chunk_id].get("document_id")) for chunk_id in changed_chunk_ids}

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "documents": {doc_id: {
            "id": doc_id,
            "name": str(doc.get("name") or doc.get("location") or doc_id),
            "update_time": doc.get("update_time"),
            "run": _doc_run_state(doc),
            "status": doc.get("status"),
            "chunk_count": doc.get("chunk_count"),
        } for doc_id, doc in current_docs.items()},
        "document_sources": fetched_sources,
        "chunks": current_chunks,
        # 实体类型（归一化名 → person/location/org…）：建边黑名单据此豁免人名，
        # 老快照没有该字段，读写都要按「缺省为空」处理
        "entity_types": entity_types,
        # 版本锚定（T8-4）：拿不到时为 None，字段必须存在
        "ragflow_version": ragflow_version,
        "fetched_at": storage.now_iso(),
    }
    # source_hash 是内容指纹，不含 fetched_at / ragflow_version：
    # 版本端点偶发失败（None）不应把整份快照误判成“内容变化”，破坏幂等。
    snapshot["source_hash"] = _sha({
        k: v for k, v in snapshot.items()
        if k not in ("fetched_at", "source_hash", "ragflow_version")
    })
    delta = {
        "added_doc_ids": sorted(set(current_docs) - set(previous_docs)),
        "changed_doc_ids": sorted(changed_doc_ids),
        "deleted_doc_ids": sorted(deleted_doc_ids),
        "added_chunk_ids": sorted(added_chunk_ids),
        "changed_chunk_ids": sorted(changed_chunk_ids),
        "deleted_chunk_ids": sorted(deleted_chunk_ids),
        #  P1：元数据未变、但 chunk 正文指纹变化的文档（内容校验发现）
        "content_changed_doc_ids": sorted(content_changed_doc_ids),
        "chunk_verified_doc_ids": sorted(chunk_verified_doc_ids),
        "chunk_verify_skipped": verify_skip_reason,
        "changed": bool(added_chunk_ids or changed_chunk_ids or deleted_chunk_ids or deleted_doc_ids
                        or content_changed_doc_ids),
        "alteration": previous_alteration,
    }
    return snapshot, delta


def _entity_df(entity_keys: dict[str, set[str]]) -> dict[str, int]:
    """每个实体出现在多少个分块里（文档频率 df）。"""
    df: dict[str, int] = {}
    for keys in entity_keys.values():
        for key in keys:
            df[key] = df.get(key, 0) + 1
    return df


def _df_cutoff(chunk_count: int, min_df: int) -> int:
    """按比例自适应的高频门槛 max(min_df, ⌈比例 × 分块数⌉)。绝对频次下限很重要：小库上 30% 只有个位数，
    会把真正有区分度的实体全部拉黑。建边与消歧排除各自传自己的下限（两种口径相反，见模块常量注释）。"""
    return max(min_df, math.ceil(_OVERLAP_BLACKLIST_DF_RATIO * chunk_count))


def _overlap_blacklist(
    chunks: dict,
    entity_keys: dict[str, set[str]],
    entity_types: dict[str, str] | None,
) -> tuple[set[str], int]:
    """**建边口径**：返回「无区分度、不该用来连边」的实体与本次门槛。

    与 `_disambiguation_blacklist` 的取舍相反，原因见模块常量处的注释：
    建边要的是「跨块主线」，人名 df 高反而说明它该连边，因此 `type=person` 永不拉黑；
    其余实体仍按 df >= max(最小绝对频次, 30% × 分块数) 拉黑。

    拿不到类型（老快照没有 `entity_types`）时退化为：
    - 分块数 ≤ 12：门槛 = 分块数（下限仍是 `RAGFLOW_OVERLAP_BLACKLIST_MIN_DF`，
      所以 1~2 块这种没有「高频」可言的库实际等于不拉黑）；
    - 分块数 > 12：仍按比例口径（大库上 30% 才是合理的高频定义）。

    `RAGFLOW_OVERLAP_BLACKLIST_DF_RATIO<=0` 视为关闭黑名单。
    """
    if _OVERLAP_BLACKLIST_DF_RATIO <= 0:
        return set(), 0
    chunk_count = len(chunks)
    df = _entity_df(entity_keys)
    if entity_types:
        cutoff = _df_cutoff(chunk_count, _OVERLAP_BLACKLIST_MIN_DF)
        blacklist = {
            key for key, count in df.items()
            if count >= cutoff
            and str(entity_types.get(key) or "").strip().casefold() not in _PERSON_ENTITY_TYPES
        }
        return blacklist, cutoff
    cutoff = (max(chunk_count, _OVERLAP_BLACKLIST_MIN_DF)
              if chunk_count <= _LEGACY_BLACKLIST_SMALL_LIB_CHUNKS
              else _df_cutoff(chunk_count, _OVERLAP_BLACKLIST_MIN_DF))
    if cutoff <= 0:
        return set(), 0
    return {key for key, count in df.items() if count >= cutoff}, cutoff


def _disambiguation_blacklist(chunks: dict, entity_keys: dict[str, set[str]]) -> tuple[set[str], int]:
    """**LLM 消歧候选排除口径**：按纯 df 拉黑，不看类型、不做小库兜底。

    消歧按候选对计费，高频词（人名也好、地点也好）几乎不可能只靠名字相似就同义，
    排除它们能省最多 token；与建边口径的目标相反，所以刻意不复用 `_overlap_blacklist`。
    """
    if _OVERLAP_BLACKLIST_DF_RATIO <= 0:
        return set(), 0
    cutoff = _df_cutoff(len(chunks), _DISAMBIGUATION_EXCLUDE_MIN_DF)
    df = _entity_df(entity_keys)
    return {key for key, count in df.items() if count >= cutoff}, cutoff


def _overlap_params() -> dict:
    return {
        "min_score": _OVERLAP_MIN_SCORE,
        "blacklist_df_ratio": _OVERLAP_BLACKLIST_DF_RATIO,
        "blacklist_min_df": _OVERLAP_BLACKLIST_MIN_DF,
    }


def _overlap_config_override() -> dict:
    """当前建边参数与存量投影的隐含参数（`_OVERLAP_LEGACY_PARAMS`）不同时，才把明细写进 projection_config。

    相同则不写，与存量投影的 `{"entity_resolution": "disabled"}` 一致、直接复用；不同则写明细、
    复用条件不成立 → 重建一次。默认最小频次从 3 改为 5 后，存量投影都会在下次同步时重建一次，
    这是有意为之（补回被旧门槛误删的边）。界面的「是否需要重建」也读这里（routers/ragflow.py）。
    """
    params = _overlap_params()
    if params == _OVERLAP_LEGACY_PARAMS:
        return {}
    return {"overlap": params}


def current_overlap_config() -> dict:
    """本进程按当前建边参数会写进 projection_config 的 overlap 部分（参数等于存量隐含参数时为空）。

    供界面判断「下次同步是否会重建」：已生效投影的 `overlap` 与它不同，就会重建。
    """
    return _overlap_config_override()


def entity_exclusion_keys(snapshot: dict) -> set[str]:
    """消歧与成本预估共用的高频实体排除口径（与建边黑名单刻意不同，见上方常量注释）。"""
    chunks = snapshot.get("chunks") or {}
    entity_keys_by_chunk: dict[str, set[str]] = {}
    for chunk in chunks.values():
        chunk = chunk or {}
        chunk_id = str(chunk.get("id"))
        for entity in chunk.get("entities") or []:
            key = normalize_entity(entity)
            if key:
                entity_keys_by_chunk.setdefault(chunk_id, set()).add(key)
    blacklist, _ = _disambiguation_blacklist(chunks, entity_keys_by_chunk)
    return blacklist


def _projection_config_core(config_data: dict | None) -> dict:
    """用于「配置是否变化」的比较：剔除每次同步都会变的统计字段，并把历史缺省补齐。

    `entity_resolution_stats` 里含 llm_calls / cache_hits 等运行期数字，
    若参与比较，配置没变也会每次判成不一致、永远重建。
    `entity_resolution` 缺失 = 该投影从未做过实体消歧，等价于 `disabled`；
    补上缺省后，关闭态的存量投影继续复用，不会因为新增了这个字段被一次性重建。
    """
    core = {key: value for key, value in (config_data or {}).items()
            if key != "entity_resolution_stats"}
    core.setdefault("entity_resolution", "disabled")
    if core["entity_resolution"] == "disabled":
        # 关闭态没有口径指纹可比：遗留字段（曾开过又关掉、或人工写入）不参与比较，
        # 否则会出现「界面说不重建、同步却重建」的不一致
        for key in ("config_fingerprint", "prompt_version", "llm_preset_id", "llm_model"):
            core.pop(key, None)
    return core


def _entity_resolution_payload(mode: str, resolution: dict | None) -> dict:
    if resolution is None:
        return {"mode": mode}
    return {"mode": mode, **resolution["stats"]}


def _restate_delta_after_resolution(delta: dict, snapshot: dict, previous: dict | None) -> dict:
    """实体口径重写后按「已固化口径」重算变化集。

    `fetch_snapshot` 里的实体比较拿「本次原始实体」比「上次已固化实体」，
    只要开关参与过，就会把所有带实体的分块都判成变化，进而给历史测试集误打 stale。
    实体口径的语义是「同一实体的不同写法并成一个名字」，正文一个字都没变；
    样本的 reference_contexts 引用的是正文，不是实体属性。
    因此本次或上次快照带 `entity_resolution` 时，分块是否变化**只按正文指纹**判定；
    实体差异仍会让快照哈希变化、触发投影重建（重建由配置/哈希决定，与 delta 无关）。
    """
    if not previous:
        return delta
    resolution_involved = bool(snapshot.get("entity_resolution") or previous.get("entity_resolution"))
    prev_chunks = previous.get("chunks") or {}
    cur_chunks = snapshot.get("chunks") or {}
    common = set(prev_chunks) & set(cur_chunks)
    changed_chunk_ids = sorted(
        chunk_id for chunk_id in common
        if (cur_chunks[chunk_id] or {}).get("content_hash") != (prev_chunks[chunk_id] or {}).get("content_hash")
        or (not resolution_involved
            and (cur_chunks[chunk_id] or {}).get("entities") != (prev_chunks[chunk_id] or {}).get("entities"))
    )
    prev_docs = previous.get("documents") or {}
    cur_docs = snapshot.get("documents") or {}
    metadata_doc_ids = {
        doc_id for doc_id in set(prev_docs) & set(cur_docs)
        if any((cur_docs[doc_id] or {}).get(key) != (prev_docs[doc_id] or {}).get(key)
               for key in ("update_time", "name", "status", "run", "chunk_count"))
    }
    changed_doc_ids = (
        metadata_doc_ids
        | set(delta.get("content_changed_doc_ids") or [])
        | {str((cur_chunks[chunk_id] or {}).get("document_id")) for chunk_id in changed_chunk_ids}
    )
    result = dict(delta)
    result["changed_chunk_ids"] = changed_chunk_ids
    result["changed_doc_ids"] = sorted(changed_doc_ids)
    result["changed"] = bool(
        result.get("added_chunk_ids") or changed_chunk_ids or result.get("deleted_chunk_ids")
        or result.get("deleted_doc_ids") or result.get("content_changed_doc_ids")
    )
    return result


# 生成前增强写回投影的属性（唯一定义处；与 kg.enhance_existing_kg 的产出一致）。
# 分块：摘要 / 主题 / 摘要向量；文档：由子分块摘要拼出的摘要 + 摘要向量。相似边由向量本地重算，不继承。
ENHANCED_CHUNK_PROPS = ("summary", "themes", "summary_embedding")
ENHANCED_DOCUMENT_PROPS = ("summary", "summary_embedding")


def _node_kind(node: dict) -> str:
    """投影节点类型。新投影写的是 "chunk"；增强后经 ragas 序列化可能是 "NodeType.CHUNK"。"""
    return str(node.get("type") or "").rsplit(".", 1)[-1].lower()


def _doc_content_signature(nodes: list[dict]) -> dict[str, tuple]:
    """每篇文档名下全部分块的（分块 ID, 正文）集合：任何一个分块变了，文档摘要就要重算。

    分组键是 `doc_id`（阶段 3 契约）。换成 `source_document_id` 会让不同来源的同名上游 id
    撞在一起；换成别的键又会静默失配（增强继承失效，每次同步白付摘要与向量费用）。
    """
    by_doc: dict[str, list] = {}
    for node in nodes:
        if _node_kind(node) != "chunk":
            continue
        props = node.get("properties") or {}
        by_doc.setdefault(str(props.get("doc_id") or ""), []).append(
            (str(props.get("source_chunk_id")), props.get("page_content") or ""))
    return {doc: tuple(sorted(items)) for doc, items in by_doc.items()}


def inherit_enhancement(projection: dict, previous: dict | None) -> dict:
    """把旧投影里**内容未变**节点的增强成果搬到新投影上，返回统计。

    背景：知识库里只要有一篇文档变化，同步就整张重建投影；
    旧投影里已付费补好的摘要、主题、摘要向量全部丢失，下次出题前要按全库重新增强（2 个分块就要约 5k token，
    成本随分块数线性增长）。投影节点 ID 由（库 ID, 分块 / 文档 ID）确定性生成，新旧两张图同一节点 ID 相同。

    规则：分块 —— ID 相同且正文完全一致才继承；文档 —— 名下全部分块的 ID 与正文都没变才继承
    （文档摘要由子分块摘要拼成）。相似边不继承：增强时由继承来的向量本地重算，0 token。只改 projection，不碰 previous。
    """
    stats = {"chunks": 0, "documents": 0}
    if not previous:
        return stats
    old_nodes = {str(n.get("id")): n for n in previous.get("nodes") or []}
    old_docs = _doc_content_signature(previous.get("nodes") or [])
    new_docs = _doc_content_signature(projection.get("nodes") or [])
    # 文档节点 → doc_id 直接从节点属性取：节点 id 是按「上游文档 id」算的，
    # 而 doc_id 是 rf- 再哈希，两者不能互相推导（推导错了继承会静默失效）。
    doc_ids_by_node = {
        str(node.get("id")): str((node.get("properties") or {}).get("doc_id") or "")
        for node in projection.get("nodes") or []
        if _node_kind(node) == "document"
    }
    for node in projection.get("nodes") or []:
        old = old_nodes.get(str(node.get("id")))
        if not old:
            continue
        kind = _node_kind(node)
        props = node.setdefault("properties", {})
        old_props = old.get("properties") or {}
        if kind == "chunk":
            if (old_props.get("page_content") or "") != (props.get("page_content") or ""):
                continue
            keys = ENHANCED_CHUNK_PROPS
        elif kind == "document":
            doc_id = doc_ids_by_node.get(str(node.get("id")))
            if not doc_id or old_docs.get(doc_id) != new_docs.get(doc_id):
                continue
            keys = ENHANCED_DOCUMENT_PROPS
        else:
            continue
        carried = False
        for key in keys:
            value = old_props.get(key)
            if value not in (None, "", []) and props.get(key) in (None, "", []):
                props[key] = value
                carried = True
        if carried:
            stats["chunks" if kind == "chunk" else "documents"] += 1
    return stats


def build_ragas_projection(snapshot: dict, *, source_id: str) -> dict:
    """把 source snapshot 转为当前 RAGAS 合成器可消费的最小图。

    `source_id` 是「连接 × 知识库」的来源身份（`connection_id__dataset_id`），是 `rf-` 文档身份的
    唯一作用域，**必填**：退化成数据集 id 会让不同连接下的同名上游文档 id 撞成同一个 `doc_id`，
    而这类撞车只在改绑/多连接场景才暴露、且会静默污染样本溯源，所以不再提供任何回退。
    """
    dataset_id = str(snapshot["dataset_id"])
    source_scope = str(source_id)
    nodes: list[dict] = []
    relationships: list[dict] = []
    chunks = snapshot.get("chunks") or {}
    documents = snapshot.get("documents") or {}
    identities: dict[str, dict] = {}

    for doc_id, doc in documents.items():
        doc_name = str(doc.get("name") or doc_id)
        identity = ragflow_document_identity(source_scope, doc_id, doc_name)
        identities[str(doc_id)] = identity
        properties = {
            "page_content": doc_name,
            "document_metadata": {"filename": doc_name, "source": "ragflow"},
        }
        nodes.append({
            "id": _stable_id("document", dataset_id, doc_id),
            "type": "document",
            "properties": apply_identity(properties, identity),
        })

    for chunk_id, chunk in chunks.items():
        upstream_doc_id = str(chunk.get("document_id"))
        identity = identities.get(upstream_doc_id) or ragflow_document_identity(
            source_scope, upstream_doc_id, upstream_doc_id)
        doc_id = upstream_doc_id
        properties = {
            "page_content": chunk.get("page_content") or "",
            "entities": chunk.get("entities") or [],
            "source_chunk_id": chunk_id,
        }
        nodes.append({
            "id": _stable_id("chunk", dataset_id, chunk_id),
            "type": "chunk",
            "properties": apply_identity(properties, identity),
        })
        relationships.append({
            "id": _stable_id("edge", dataset_id, f"child:{doc_id}:{chunk_id}"),
            "type": "child",
            "source": _stable_id("document", dataset_id, doc_id),
            "target": _stable_id("chunk", dataset_id, chunk_id),
            "bidirectional": False,
            "properties": {},
        })

    by_doc: dict[str, list[dict]] = {}
    for chunk in chunks.values():
        by_doc.setdefault(str(chunk.get("document_id")), []).append(chunk)
    for doc_chunks in by_doc.values():
        doc_chunks.sort(key=lambda item: (int(item.get("position", 0)), str(item.get("id"))))
        for left, right in zip(doc_chunks, doc_chunks[1:]):
            relationships.append({
                "id": _stable_id("edge", dataset_id, f"next:{left['id']}:{right['id']}"),
                "type": "next",
                "source": _stable_id("chunk", dataset_id, left["id"]),
                "target": _stable_id("chunk", dataset_id, right["id"]),
                "bidirectional": False,
                "properties": {},
            })

    entity_chunks: dict[str, list[tuple[str, str]]] = {}
    entity_keys_by_chunk: dict[str, set[str]] = {}
    for chunk in chunks.values():
        for entity in chunk.get("entities") or []:
            key = normalize_entity(entity)
            if key:
                entity_chunks.setdefault(key, []).append((str(chunk["id"]), str(entity)))
                entity_keys_by_chunk.setdefault(str(chunk["id"]), set()).add(key)
    blacklist, cutoff = _overlap_blacklist(
        chunks, entity_keys_by_chunk, snapshot.get("entity_types") or {})
    if blacklist:
        log.info(
            "实体重叠黑名单：共 %d 个 chunk、门槛 df>=%d，剔除 %d 个高频实体（%s）",
            len(chunks), cutoff, len(blacklist),
            ", ".join(sorted(blacklist)[:10]) + (" 等" if len(blacklist) > 10 else ""),
        )
    # 分母只算「未被拉黑」的实体数，否则被剔除的高频词仍会稀释分数
    entity_count_by_chunk = {
        chunk_id: max(1, len(keys - blacklist)) for chunk_id, keys in entity_keys_by_chunk.items()
    }
    pair_items: dict[tuple[str, str], list[list[str]]] = {}
    for key, values in entity_chunks.items():
        if key in blacklist:
            continue
        unique = {}
        for chunk_id, display in values:
            unique[chunk_id] = display
        ids = sorted(unique)
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                pair_items.setdefault((left, right), []).append([unique[left], unique[right]])
    for (left, right), items in sorted(pair_items.items()):
        score = len(items) / max(1, min(entity_count_by_chunk.get(left, 1), entity_count_by_chunk.get(right, 1)))
        score = round(min(1.0, score), 6)
        if score < _OVERLAP_MIN_SCORE:
            continue  # 阈值可配置（RAGFLOW_OVERLAP_MIN_SCORE），默认 0.0 = 保持历史行为
        relationships.append({
            "id": _stable_id("edge", dataset_id, f"entities_overlap:{left}:{right}"),
            "type": "entities_overlap",
            "source": _stable_id("chunk", dataset_id, left),
            "target": _stable_id("chunk", dataset_id, right),
            "bidirectional": True,
            "properties": {
                "entities_overlap_score": score,
                "overlapped_items": items,
            },
        })
    return {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "projection_type": "ragflow-ragas",
        "dataset_id": dataset_id,
        "nodes": nodes,
        "relationships": relationships,
    }


def _source_ids_from_texts(samples: list[dict], projection: dict) -> None:
    """迁移期兜底：按上下文文本反查上游 chunk id。

     P2 起，真实生成路径已在生成时从 ragas 场景节点落下结构化出处
    （`source_content_hashes` 等，见 `provenance.attach_provenance`），
    这些样本**不再走文本反查**（文本会变，反查既证明不了同源也证明不了未变）。
    """
    text_to_ids: dict[str, list[str]] = {}
    for node in projection.get("nodes", []):
        if node.get("type") != "chunk":
            continue
        props = node.get("properties") or {}
        source_id = props.get("source_chunk_id")
        if source_id:
            text_to_ids.setdefault(str(props.get("page_content") or ""), []).append(str(source_id))
    for sample in samples:
        if sample.get("source_content_hashes"):
            continue
        ids: set[str] = set()
        for context in sample.get("reference_contexts") or []:
            value = re.sub(r"^<\d+-hop>\n\n", "", str(context or ""))
            ids.update(text_to_ids.get(value, []))
        sample["source_chunk_ids"] = sorted(ids)


def annotate_samples(
    samples: list[dict],
    projection: dict,
    projection_version: str,
    dataset_id: str,
    connection_id: str = "",
) -> list[dict]:
    _source_ids_from_texts(samples, projection)
    for sample in samples:
        sample["source_dataset_id"] = dataset_id
        sample["source_projection_version"] = projection_version
        if connection_id:
            sample["source_connection_id"] = connection_id
        # 样本级 stale 的落盘形态**统一为「缺键 = 正常」**：只有置位时写 `stale: true`
        # （+`stale_reason`），回收时删键。这里把显式 `false` 也收敛成缺键（已是 true 的保持），
        # 避免同一文件里「无键」与「false」两种形态并存；读路径一律按 `.get("stale")` 处理，两种存量形态都兼容。
        if not sample.get("stale"):
            sample.pop("stale", None)
    return samples


def _testset_belongs_to_source(
    data: dict,
    dataset_id: str,
    previous_kg_id: str | None,
    source_connection_id: str | None,
    legacy_source: bool,
) -> bool:
    """测试集是否属于本次同步的数据源。

    1) `kg_id` 命中本次的旧投影 id：唯一且必然属于同一数据源，直接认领；
    2) 记了 `source_connection_id`：必须与本次连接的 id 相同（同 dataset_id 的不同实例互不干扰）；
    3) 历史测试集（无 `source_connection_id`）只在 legacy 来源同步时按 `source_dataset_id` 匹配，
       新连接不再认领——宁可漏标，不可误标对方实例的测试集。
    """
    if data.get("kg_id") and previous_kg_id and data.get("kg_id") == previous_kg_id:
        return True
    ts_conn = data.get("source_connection_id")
    if ts_conn:
        return bool(source_connection_id) and ts_conn == source_connection_id
    return legacy_source and data.get("source_dataset_id") == dataset_id


def mark_testsets_stale(
    dataset_id: str,
    previous_kg_id: str | None,
    delta: dict,
    *,
    source_connection_id: str | None = None,
    legacy_source: bool = False,
) -> list[str]:
    changed_chunks = set(delta.get("added_chunk_ids") or []) | set(delta.get("changed_chunk_ids") or []) | set(delta.get("deleted_chunk_ids") or [])
    if not changed_chunks and not delta.get("deleted_doc_ids"):
        return []
    changed_testsets: list[str] = []

    def _mark(data: dict) -> bool:
        if not _testset_belongs_to_source(
            data, dataset_id, previous_kg_id, source_connection_id, legacy_source,
        ):
            return False
        samples = data.get("samples") or []
        unknown_sources = False
        for sample in samples:
            #  P2：新样本的 `source_chunk_ids` 是投影节点 id（uuid5），
            # 与 delta 的上游 RAGFlow chunk id 不同口径；stale 传播优先用上游 id，
            # 历史样本（无该字段）继续用 source_chunk_ids 兜底。
            source_ids = set(sample.get("source_upstream_chunk_ids") or sample.get("source_chunk_ids") or [])
            if not source_ids:
                unknown_sources = True
            stale = bool(source_ids & changed_chunks) or bool(delta.get("deleted_doc_ids")) and not source_ids
            if stale:
                sample["stale"] = True
                sample["stale_reason"] = "source_projection_changed"
        if unknown_sources and not any(s.get("source_chunk_ids") for s in samples):
            for sample in samples:
                sample["stale"] = True
                sample["stale_reason"] = "source_projection_changed_without_provenance"
        stale_count = sum(1 for sample in samples if sample.get("stale"))
        if not stale_count:
            return False
        data["stale"] = True
        data["stale_sample_count"] = stale_count
        return True

    # 每个测试集在存储锁内重读、判定、写回：与用户的删除 / 采纳操作互斥，
    # 不会把刚删除的测试集写回来，也不会用旧副本覆盖刚改的采纳状态。
    for entry in storage.ts_list():
        ts_id = entry.get("id", "")
        if ts_id and storage.ts_update(ts_id, _mark):
            changed_testsets.append(ts_id)
    return changed_testsets


def reconcile_testsets_stale(
    dataset_id: str,
    projection_kg_id: str | None,
    *,
    previous_kg_id: str | None = None,
    source_connection_id: str | None = None,
    legacy_source: bool = False,
) -> list[dict]:
    """回收**过期**的 stale 标记：按 P2 出处判定复核，引用已恢复一致就清掉。

    与 `mark_testsets_stale` 用同一套测试集归属判定，判据换成 `provenance.clear_recovered_stale`
    （只看引用分块的正文哈希），因此「只是实体变化」不再被旧的 chunk 集合口径当成正文变化。

    - 有内容指纹且复核 valid → 清样本 `stale` / `stale_reason`，并按剩余数重算
      `stale_sample_count`（清空时整条测试集的 `stale` 一并置 false）；
    - legacy（无内容指纹）样本无法复核 → **保持原标记不动**，只记 `undetermined_legacy`。
    """
    from .provenance import _projection_chunks, clear_recovered_stale

    projection = storage.kg_read_json(projection_kg_id) if projection_kg_id else None
    if not projection:
        return []
    chunks = _projection_chunks(projection)
    reconciled: list[dict] = []

    def _reconcile(data: dict) -> dict | None:
        samples = data.get("samples") or []
        if not data.get("stale") and not any(sample.get("stale") for sample in samples):
            return None
        belongs = _testset_belongs_to_source(
            data, dataset_id, projection_kg_id, source_connection_id, legacy_source,
        ) or bool(previous_kg_id) and str(data.get("kg_id") or "") == previous_kg_id
        if not belongs:
            return None
        outcome = clear_recovered_stale(samples, chunks)
        if not outcome["cleared"]:
            return None
        data["stale"] = bool(outcome["remaining"])
        if outcome["remaining"]:
            data["stale_sample_count"] = outcome["remaining"]
        else:
            # stale 已全部回收：与置位时对称，把计数一并撤掉（读路径按缺省 0 处理）
            data.pop("stale_sample_count", None)
        return {"testset_id": data["id"], **outcome}

    # 与置位同理：锁内重读 → 复核 → 写回，不与用户的删除 / 采纳操作交错
    for entry in storage.ts_list():
        ts_id = entry.get("id", "")
        row = storage.ts_update(ts_id, _reconcile) if ts_id else None
        if row:
            reconciled.append(row)
    return reconciled


def _reconcile_note(reconciled: list[dict]) -> str:
    """回收结果的统一文案（重建与复用/复查两条路径共用，避免各自再统计一遍造成口径漂移）。"""
    if not reconciled:
        return ""
    return (f"回收 {len(reconciled)} 个测试集的过期 stale 标记"
            f"（{sum(row['cleared'] for row in reconciled)} 条样本）")


# 同步返回值里的三个 stale 字段语义（别把前两个读成同一件事）：
#   stale_testset_ids —— 本次**置位**动作命中的测试集（随后可能被回收，不一定还 stale）
#   stale_reconciled  —— 本次**被回收**（部分或全部解除 stale）的测试集明细
#   still_stale_ids   —— 置位后**仍然**处于 stale 的测试集（= stale_testset_ids − 已**全部**回收的；
#                        部分回收、还剩 stale 样本的测试集仍算在内）


# ---------- 数据源跟踪状态：「已停止跟踪」与重新绑定 ----------
#
# 连接是凭据实体，删除连接不删已同步的图谱/测试集；但引用它的测试集从此无法再跟随该数据源
# 增量同步。这种断裂必须显式可见。测试集顶层的落盘形态（不新增归属键、不改既有键）：
#   source_connection_id   原样保留 —— 「曾绑定在哪条连接」的证据，重新绑定时靠它找回旧目录
#   source_unbound         true = 已停止跟踪
#   source_unbound_reason  connection_deleted（删除动作写入）/ connection_missing（读取时推导）
#   source_unbound_at      置位时间（存量推导出的记录没有该字段）
# 读取一律走 `testset_tracking_state()`：显式标记优先，其次按「该连接当前是否存在」推导，
# 所以连接早已被删、从未写过标记的存量记录也会显示为已停止跟踪。


def testset_tracking_state(data: dict, connection_ids: set[str] | None = None) -> dict:
    """测试集的数据源跟踪状态（只读推导，不改落盘）。"""
    conn = str(data.get("source_connection_id") or "")
    if data.get("source_unbound"):
        return {"unbound": True,
                "reason": data.get("source_unbound_reason") or "connection_deleted",
                "since": data.get("source_unbound_at"),
                "connection_id": conn or None}
    if not conn:
        return {"unbound": False, "reason": None, "since": None, "connection_id": None}
    ids = connection_ids if connection_ids is not None else {
        str(c.get("id")) for c in storage.ragflow_list_connections()
    }
    return {"unbound": conn not in ids,
            "reason": None if conn in ids else "connection_missing",
            "since": None, "connection_id": conn}


def testset_projection_state(data: dict) -> dict:
    """投影失联（只读推导，不落盘）：测试集自己的图谱已删除，数据源也没有新的当前投影。

    与 `source_unbound` 的区别：连接仍然存在，重新同步成功后指针会指向新投影、状态自动消失，
    因此**不写** `source_unbound` 字段，也不扩展 `source_unbound_reason` 枚举。
    """
    dataset_id = str(data.get("source_dataset_id") or "")
    kg_id = str(data.get("kg_id") or "")
    if not dataset_id or not kg_id or storage.kg_get(kg_id):
        return {"missing": False, "kg_id": None}
    connection_id = str(data.get("source_connection_id") or "")
    try:
        # 与 `testset_regenerate.current_projection_kg_id` 同一读取口径（legacy 走旧布局）
        if connection_id and connection_id != storage.legacy_connection_id(dataset_id):
            _, pointer = storage.ragflow_source_snapshot(
                storage.ragflow_source_id(connection_id, dataset_id))
        else:
            _, pointer = storage.ragflow_snapshot_current(dataset_id)
    except Exception:  # noqa: BLE001 —— 路径非法 / 读取失败一律当作「找不到当前投影」
        pointer = None
    current = str((pointer or {}).get("projection_kg_id") or "")
    if current and storage.kg_get(current):
        return {"missing": False, "kg_id": None}
    return {"missing": True, "kg_id": kg_id}


def mark_testsets_unbound(connection_id: str, *, reason: str = "connection_deleted") -> list[dict]:
    """把仍引用该连接的测试集标记为「已停止跟踪」，返回被标记的清单供接口回显。

    只新增状态字段；`source_connection_id` 不覆盖 —— 它是「曾绑定」的证据，也是重新绑定的线索。
    已经是同一原因的标记不重复写盘。
    """
    marked: list[dict] = []
    for entry in storage.ts_list():
        data = storage.ts_read(entry.get("id", ""))
        if not data or str(data.get("source_connection_id") or "") != connection_id:
            continue
        if data.get("source_unbound") and data.get("source_unbound_reason") == reason:
            continue
        data["source_unbound"] = True
        data["source_unbound_reason"] = reason
        data["source_unbound_at"] = storage.now_iso()
        storage.ts_write(data["id"], data)
        storage.ts_upsert(_testset_index_entry(data))
        marked.append({"testset_id": data["id"], "name": data.get("name"),
                       "sample_count": len(data.get("samples") or [])})
    return marked


def _testset_index_entry(data: dict) -> dict:
    """测试集索引条目（与既有写路径同口径：去掉 samples，补 path / count）。"""
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry["path"] = str(storage.TESTSET_DIR / f"{data['id']}.json")
    entry["count"] = len(data.get("samples") or [])
    return entry


def _source_dir_for(connection_id: str, dataset_id: str) -> Path:
    """连接对应的数据源目录：旧 config.json 合成出来的 legacy 连接走旧布局。"""
    if connection_id == storage.legacy_connection_id(dataset_id):
        return storage.ragflow_legacy_source_dir(dataset_id)
    return storage.ragflow_source_dir(connection_id, dataset_id)


def _testsets_of_source(dataset_id: str, connection_id: str) -> list[dict]:
    out: list[dict] = []
    for entry in storage.ts_list():
        data = storage.ts_read(entry.get("id", ""))
        if not data:
            continue
        if str(data.get("source_dataset_id") or "") != dataset_id:
            continue
        if str(data.get("source_connection_id") or "") != connection_id:
            continue
        out.append(data)
    return out


def rebind_source(new_connection_id: str, dataset_id: str, from_connection_id: str,
                  client: RAGFlowClient) -> dict:
    """重新绑定的入口：先在任务管理器里占住新旧两个数据源的资源键，再执行改绑。

    改绑要搬目录、改写投影与测试集记录。只查「同步进行中」集合挡不住正在这个数据源上
    出题或探测的任务（它们只占任务管理器的资源键），出题收尾时会把旧连接写回、或被搬走目录。
    资源被占用时抛 ResourceBusyError（接口返回 409），不做任何改动、也不产生备份。
    """
    ds = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    new_conn = storage.validate_ragflow_name(new_connection_id, "RAGFlow connection_id")
    old_conn = storage.validate_ragflow_name(from_connection_id, "RAGFlow connection_id")
    old_key_conn = None if old_conn == storage.legacy_connection_id(ds) else old_conn
    resources = tuple(dict.fromkeys(
        storage.ragflow_sync_resources(ds, old_key_conn) + storage.ragflow_sync_resources(ds, new_conn)))
    task = manager.create("ragflow_rebind", resources)
    try:
        # 自洽性校验（不是拦截点）：任务由上面的 manager.create 刚创建，正常路径必然通过；
        # 真正拦住「绕过任务直接调用」的是 sync_ragflow 入口那处检查。
        _require_sync_lease(task, resources, operation="改绑")
        result = _rebind_source_locked(new_conn, ds, old_conn, client)
    except BaseException as exc:
        manager.complete(task, "failed", str(exc))
        raise
    manager.complete(task, "succeeded")
    return result


def _remap_projection_identity(data: dict, mapping: dict[str, str]) -> int:
    """改绑时把投影图里 document / chunk 节点的 `doc_id` 换成新来源作用域下的值。

    只动 `doc_id`：`source_ref` / `source_type` / `doc_name` 与 `source_id` 无关。
    返回被改写的节点数；映射里没有的旧 id 原样保留。
    """
    changed = 0
    for node in data.get("nodes") or []:
        props = node.get("properties")
        if not isinstance(props, dict):
            continue
        new_doc_id = mapping.get(str(props.get("doc_id") or ""))
        if new_doc_id and new_doc_id != props.get("doc_id"):
            props["doc_id"] = new_doc_id
            changed += 1
    return changed


def _remap_sample_document_ids(data: dict, mapping: dict[str, str]) -> int:
    """改绑时把测试集样本 `source_document_ids` 里的旧 `rf-` 换成新值。

    返回被改写的样本行数；映射里没有的值原样保留（历史图里可能已不存在的文档）。
    """
    changed = 0
    for sample in data.get("samples") or []:
        ids = sample.get("source_document_ids")
        if not isinstance(ids, list):
            continue
        remapped = [mapping.get(str(item), item) for item in ids]
        if remapped != ids:
            sample["source_document_ids"] = remapped
            changed += 1
    return changed


def _rebind_source_locked(new_connection_id: str, dataset_id: str, from_connection_id: str,
                          client: RAGFlowClient) -> dict:
    """把某个数据源从旧连接改绑到新连接（同一 dataset_id）。调用方已占住资源键。

    前置：新旧连接不同、新连接确实能看到该库、旧目录存在且新目录不存在；动作前先把旧目录
    整体备份到 `.backups/ragflow-sources/`。任何一步失败都回滚目录与记录，并把备份位置写进错误文案。

    改绑会换 source_id，同一篇上游文档的 `rf-` 随之改变，所以投影图的 `doc_id` 与测试集样本的
    `source_document_ids` 一并按旧→新映射重写（映射只在 `doc_identity.py` 生成）；投影原文先落到
    `备份目录/kg-json/`，回滚时写回原文。
    """
    ds = storage.validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    new_conn = storage.validate_ragflow_name(new_connection_id, "RAGFlow connection_id")
    old_conn = storage.validate_ragflow_name(from_connection_id, "RAGFlow connection_id")
    if new_conn == old_conn:
        raise ValueError("新旧连接相同，无需重新绑定")
    old_dir = _source_dir_for(old_conn, ds)
    new_dir = storage.ragflow_source_dir(new_conn, ds)
    if not old_dir.exists():
        raise ValueError(f"找不到旧连接 {old_conn} 的数据源目录，无法重新绑定；可改用新连接重新同步")
    if new_dir.exists():
        raise ValueError(f"新连接下已存在知识库 {ds} 的同步目录（未做任何改动），请先确认再操作")
    # legacy 连接的旧布局目录要按 source_dir=None 读指针
    old_source_dir_arg = None if old_conn == storage.legacy_connection_id(ds) else old_dir
    _, pointer = storage.ragflow_snapshot_current(ds, source_dir=old_source_dir_arg)
    if not pointer or not pointer.get("snapshot_id") or not pointer.get("projection_kg_id"):
        raise ValueError("旧数据源缺少有效的 current.json 指针，无法重新绑定；可改用新连接重新同步")

    # 前置校验：新连接必须真的能看到这个库，否则改绑后同步立刻 404
    visible = {str(row.get("id")) for row in client.list_datasets()}
    if ds not in visible:
        raise RAGFlowHTTPError(
            "新连接看不到这个知识库：请确认连接地址与 API key 对应同一个 RAGFlow 实例", "dataset_not_found")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = config.BACKUP_DIR / "ragflow-sources" / f"{old_dir.name}-{stamp}"
    if backup_dir.exists():
        backup_dir = backup_dir.with_name(f"{backup_dir.name}-{uuid.uuid4().hex[:6]}")
    try:
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old_dir, backup_dir)
    except OSError as exc:
        raise ValueError(f"备份旧数据源失败，已中止（未做任何改动）：{exc}") from exc

    testsets = _testsets_of_source(ds, old_conn)
    kgs = [entry for entry in storage.kg_list()
           if entry.get("mode") == "ragflow-projection"
           and str(entry.get("source_dataset_id") or "") == ds
           and str(entry.get("source_connection_id") or "") == old_conn]
    original_testsets = [deepcopy(data) for data in testsets]
    original_kgs = [deepcopy(entry) for entry in kgs]
    new_source_id = storage.ragflow_source_id(new_conn, ds)
    old_sid = storage.ragflow_source_id(old_conn, ds)

    # 改绑换了 source_id，同一篇上游文档的 rf- 随之改变：投影图与样本里的旧 doc_id 要一并改写。
    # 改写前把各投影原文留两份（内存 + backup_dir/kg-json/），失败时按原文写回，不做不可逆覆盖。
    original_projections: dict[str, dict] = {}
    for entry in kgs:
        data = storage.kg_read_json(str(entry["id"]))
        if isinstance(data, dict):
            original_projections[str(entry["id"])] = data
    identity_mapping = ragflow_doc_id_remap(
        old_sid, new_source_id,
        {
            str((node.get("properties") or {}).get("source_ref") or "")
            for projection in original_projections.values()
            for node in projection.get("nodes") or []
            if isinstance(node.get("properties"), dict)
        },
    )
    if identity_mapping:
        try:
            originals_dir = backup_dir / "kg-json"
            originals_dir.mkdir(parents=True, exist_ok=True)
            for kg_id, projection in original_projections.items():
                (originals_dir / f"{kg_id}.json").write_bytes(
                    json.dumps(projection, ensure_ascii=False, default=str).encode("utf-8"))
        except OSError as exc:
            raise ValueError(f"备份投影原文失败，已中止（未做任何改动）：{exc}") from exc
    rewritten_kg_ids: list[str] = []
    rewritten_samples = 0
    try:
        steps: list[storage.OrderedWriteStep] = []

        def _move_dir_back() -> None:
            if new_dir.exists() and not old_dir.exists():
                shutil.move(str(new_dir), str(old_dir))

        def _move_dir() -> None:
            try:
                shutil.move(str(old_dir), str(new_dir))
            except OSError as exc:
                # 目录可能已搬了一半：先尝试搬回，再按原文案报错（此时其他步骤都还没执行）
                try:
                    _move_dir_back()
                except OSError as undo_exc:
                    log.error("移动数据源目录失败且未能搬回（备份 %s）: %s", backup_dir, undo_exc)
                    raise ValueError(
                        f"移动数据源目录失败且未能搬回（备份在 {backup_dir}，请按备份恢复）：{exc}"
                    ) from exc
                raise ValueError(f"移动数据源目录失败，已中止（备份在 {backup_dir}）：{exc}") from exc

        steps.append(storage.OrderedWriteStep("移动数据源目录", _move_dir, _move_dir_back))
        new_snapshot_path = str(
            storage.ragflow_snapshot_path(ds, pointer["snapshot_id"], source_dir=new_dir))
        steps.append(storage.OrderedWriteStep(
            "提交快照指针",
            lambda: storage.ragflow_snapshot_commit(
                ds, pointer["snapshot_id"], new_snapshot_path, pointer["projection_kg_id"],
                str(pointer.get("projection_version") or ""), source_dir=new_dir,
                connection_id=new_conn, source_id=new_source_id),
            # 指针文件在数据源目录内：先写回新目录（回滚时目录还在新位置），再由下一步的
            # 目录搬回把旧指针一起带回旧位置，顺序正好由「逆序回滚」保证。
            lambda: storage.ragflow_snapshot_pointer_write(ds, pointer, source_dir=new_dir),
        ))

        originals_by_id = {str(item["id"]): item for item in original_kgs}
        for entry in kgs:
            kg_id = str(entry["id"])
            original = original_projections.get(kg_id)
            rewrite_plan = deepcopy(original) if original is not None else None
            if rewrite_plan is not None and not _remap_projection_identity(rewrite_plan, identity_mapping):
                rewrite_plan = None  # 没有可改写的 doc_id：只切连接字段，不重写投影原文
            def _apply_kg(entry=entry, kg_id=kg_id, rewrite_plan=rewrite_plan) -> None:
                entry["source_connection_id"] = new_conn
                entry["source_id"] = new_source_id
                if rewrite_plan is not None:
                    storage.kg_write_json(kg_id, rewrite_plan)
                    rewritten_kg_ids.append(kg_id)
                storage.kg_upsert(entry)
            def _undo_kg(kg_id=kg_id, original=original) -> None:
                if original is not None:
                    storage.kg_write_json(kg_id, original)
                storage.kg_upsert(originals_by_id[kg_id])
                if kg_id in rewritten_kg_ids:
                    rewritten_kg_ids.remove(kg_id)
            steps.append(storage.OrderedWriteStep(f"改写投影 {kg_id}", _apply_kg, _undo_kg))

        originals_ts = {str(item["id"]): item for item in original_testsets}
        for data in testsets:
            ts_id = str(data["id"])
            def _apply_ts(data=data) -> None:
                nonlocal rewritten_samples
                data["source_connection_id"] = new_conn
                data.pop("source_unbound", None)
                data.pop("source_unbound_reason", None)
                data.pop("source_unbound_at", None)
                for sample in data.get("samples") or []:
                    if str(sample.get("source_connection_id") or "") == old_conn:
                        sample["source_connection_id"] = new_conn
                rewritten_samples += _remap_sample_document_ids(data, identity_mapping)
                storage.ts_write(data["id"], data)
                storage.ts_upsert(_testset_index_entry(data))
            def _undo_ts(ts_id=ts_id) -> None:
                original = originals_ts[ts_id]
                storage.ts_write(ts_id, original)
                storage.ts_upsert(_testset_index_entry(original))
            steps.append(storage.OrderedWriteStep(f"改写测试集 {ts_id}", _apply_ts, _undo_ts))

        storage.ordered_write(steps)
    except storage.OrderedWriteError as exc:
        if exc.rollback_errors:
            log.error("重新绑定回滚未完成，需按备份人工恢复（%s）: %s",
                      backup_dir, "；".join(exc.rollback_errors))
            # 只有存在 doc_id 改写时才会把投影原文另存到 备份目录/kg-json/，没有就别提这个目录
            originals_hint = (f"，改绑前的图谱原文在 {backup_dir}/kg-json/"
                              if identity_mapping else "")
            raise ValueError(
                f"重新绑定失败且回滚未完成，请用备份恢复"
                f"（{backup_dir}{originals_hint}）：{exc.cause}") from exc.cause
        log.warning("重新绑定失败，已按逆序回滚（备份 %s）: %s", backup_dir, exc.cause)
        raise ValueError(f"重新绑定失败，改动已回滚：{exc.cause}") from exc.cause

    return {"rebound": True, "connection_id": new_conn, "from_connection_id": old_conn,
            "dataset_id": ds, "source_id": new_source_id, "backup": str(backup_dir),
            "testset_ids": [data["id"] for data in testsets],
            "projection_kg_ids": [entry["id"] for entry in kgs],
            "identity_rewrite": {"documents": len(identity_mapping),
                                 "projection_kg_ids": rewritten_kg_ids,
                                 "samples": rewritten_samples},
            "rebound_at": storage.now_iso()}


_RETAIN_EVIDENCE_LIMIT = 3


def _referenced_projection_ids(projections: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """扫描全部测试集，找出仍被引用的投影 id（旧投影引用保护）。

    两路判据都算「被引用」：
    1) 测试集记录里的 `kg_id` 直接命中投影 id（正常路径）；
    2) 测试集只记了 `source_projection_version` 时，用投影的 `projection_version` 反查。
    返回（被引用集合, 证据字典：投影 id → 引用它的测试集 id 示例，最多 3 条）。
    """
    projection_ids = {str(row["id"]) for row in projections if row.get("id")}
    by_version = {
        str(row["projection_version"]): str(row["id"])
        for row in projections
        if row.get("id") and row.get("projection_version")
    }
    referenced: set[str] = set()
    evidence: dict[str, list[str]] = {}
    for entry in storage.ts_list():
        ts_id = str(entry.get("id") or "")
        record = (storage.ts_read(ts_id) if ts_id else None) or entry
        kg_id = str(record.get("kg_id") or "")
        hit = kg_id if kg_id in projection_ids else by_version.get(
            str(record.get("source_projection_version") or ""), "")
        if not hit:
            continue
        referenced.add(hit)
        refs = evidence.setdefault(hit, [])
        if ts_id and ts_id not in refs and len(refs) < _RETAIN_EVIDENCE_LIMIT:
            refs.append(ts_id)
    return referenced, evidence


def _cleanup_old_projections(
    dataset_id: str,
    keep_kg_id: str,
    source_connection_id: str | None = None,
) -> dict:
    """清理同一数据源下不再被 current.json 指向的旧投影。

    候选必须**同时**满足 `source_dataset_id == dataset_id` 且
    `source_connection_id == 本次连接 id`：两个实例同 dataset_id 时，A 的同步
    不会删掉 B 的投影。历史投影（升级前写入、没有 `source_connection_id`）不认领，
    宁可留垃圾也不误删 —— 与 §2.2「不做物理迁移」的代价一致。

    止血（用户 2026-11-04 拍板）：语料变更触发重建时，**仍被任何测试集引用的旧投影不删**，
    否则历史测试集的原图/质检/再生成会 404 断链（真实影响 3 个测试集 / 9 条样本）。
    删除照旧走 `storage.kg_delete`（物理优先 + `_deleted` 软删兜底，并移除索引条目）。
    扫描测试集失败时保守「不删」：宁可留垃圾，不可断链。
    """
    if not source_connection_id:
        return {"removed": [], "retained": [], "scan_failed": False}
    candidates = [
        entry for entry in storage.kg_list()
        if entry.get("id") != keep_kg_id
        and entry.get("mode") == "ragflow-projection"
        and entry.get("source_dataset_id") == dataset_id
        and entry.get("source_connection_id") == source_connection_id
    ]
    if not candidates:
        return {"removed": [], "retained": [], "scan_failed": False}
    try:
        referenced, evidence = _referenced_projection_ids(candidates)
    except Exception as exc:  # noqa: BLE001
        log.warning("扫描测试集引用失败，本次跳过旧投影清理（宁可留垃圾不可断链）: %s", exc)
        return {"removed": [], "retained": [], "scan_failed": True}
    # 正在被出题 / 增强任务使用的投影同样算「被引用」：任务结束前测试集还没落盘，
    # 只看测试集会把它误删
    in_use = {r.split(":", 1)[1] for r in manager.busy_resources() if r.startswith("kg:")}
    removed: list[str] = []
    retained: list[dict] = []
    for entry in candidates:
        kg_id = str(entry["id"])
        if kg_id in referenced or kg_id in in_use:
            retained.append({"kg_id": kg_id, "testset_ids": evidence.get(kg_id, []),
                             **({"in_use_by_task": True} if kg_id in in_use else {})})
        elif storage.kg_delete(kg_id):
            removed.append(kg_id)
    return {"removed": removed, "retained": retained, "scan_failed": False}


async def sync_ragflow(
    task: Task,
    dataset_id: str,
    base_url: str,
    api_key: str,
    name: str = "",
    full_refresh: bool = False,
    timeout: int = 30,
    connection_id: str | None = None,
    *,
    policy: GenerationPolicy | None = None,
) -> dict:
    """手动同步入口：指针提交成功后才标记 stale；旧投影在指针切换后清理。

    `connection_id` 为空时保持升级前的单库行为（旧布局 `data/ragflow/{dataset_id}/`、
    继续写 config.json），用于兼容 `POST /api/ragflow/sync` 与存量脚本；
    传入连接 id 时写入 `data/ragflow/sources/{connection_id}__{dataset_id}/`，
    两个实例同 dataset_id 也不再互毁。

    互斥只由任务租约提供（`storage.ragflow_sync_resources()`，路由在 `manager.create` 时占用）。
    函数入口做运行时租约检查：传入任务的 `resources` 必须包含
    `storage.ragflow_sync_resources(dataset_id, connection_id)` 生成的键，否则抛 `SyncLeaseError`
    （中文说明必须经任务占租约，或由调用方自行占租约）。正常调用方先 `manager.create` 再调用，
    检查自然通过；**绕过任务直接调用会被拒绝，不会静默并发**。
    `policy` 决定实体消歧的 LLM 超时 / 重试 / 并发与预算（默认取 `GenerationPolicy()`），
    路由从同步请求体的同名字段取值；它不进投影指纹，改它不会触发投影重建或缓存失效。
    """
    _require_sync_lease(
        task, storage.ragflow_sync_resources(dataset_id, connection_id), operation="同步")
    source_connection_id = connection_id or storage.legacy_connection_id(dataset_id)
    source_id = storage.ragflow_source_id(source_connection_id, dataset_id)
    source_dir = storage.ragflow_source_dir(source_connection_id, dataset_id) if connection_id else None
    return await _sync_ragflow_staged(
        task, dataset_id, base_url, api_key, name, full_refresh, timeout,
        connection_id=source_connection_id,
        source_id=source_id,
        source_dir=source_dir,
        legacy_source=connection_id is None,
        policy=policy or GenerationPolicy(),
    )


def _sync_report(snapshot: dict, delta: dict, *, reused: bool,
                 inherited: dict | None = None,
                 entity_resolution: dict | None = None) -> dict:
    """同步报告：读取情况、来源质量、本次变化分开给，前端据此展示，不再只刷新列表。

    可出题能力不在这里重复计算：前端拿 kg_id 调 `GET /api/testset/capabilities/{kg_id}`（同一口径）。
    """
    documents = snapshot.get("documents") or {}
    chunks = snapshot.get("chunks") or {}
    return {
        "read": {
            "documents": len(documents),
            "chunks": len(chunks),
            # 还没解析完的文档读不到分块，出题素材会偏少
            "unfinished_documents": sum(1 for doc in documents.values() if not _doc_done(doc or {})),
        },
        "source_quality": {
            "chunks": len(chunks),
            "chunks_with_entities": sum(1 for chunk in chunks.values() if (chunk or {}).get("entities")),
        },
        "changes": {
            "added_chunks": len(delta.get("added_chunk_ids") or []),
            "changed_chunks": len(delta.get("changed_chunk_ids") or []),
            "deleted_chunks": len(delta.get("deleted_chunk_ids") or []),
            "deleted_documents": len(delta.get("deleted_doc_ids") or []),
            "reused_projection": reused,
            "inherited_chunks": int((inherited or {}).get("chunks") or 0),
            "inherited_documents": int((inherited or {}).get("documents") or 0),
        },
        "entity_resolution": entity_resolution or {},
    }


def _describe_content_verify(delta: dict) -> str:
    """一句话说明本次 chunk 内容指纹校验的结论，空串表示本次没做校验。"""
    changed = delta.get("content_changed_doc_ids") or []
    verified = delta.get("chunk_verified_doc_ids") or []
    skipped = delta.get("chunk_verify_skipped") or ""
    if changed:
        return (
            f"chunk 内容指纹校验发现 {len(changed)} 个文档正文变化"
            f"（{','.join(changed[:5])}{' 等' if len(changed) > 5 else ''}），已按内容重建"
        )
    if skipped:
        return f"chunk 内容指纹校验已跳过：{skipped}"
    if verified:
        return f"已校验 {len(verified)} 个文档的 chunk 内容指纹，均无变化"
    return ""


async def _sync_ragflow_staged(
    task: Task,
    dataset_id: str,
    base_url: str,
    api_key: str,
    name: str,
    full_refresh: bool,
    timeout: int,
    *,
    connection_id: str,
    source_id: str,
    source_dir,
    legacy_source: bool,
    policy: GenerationPolicy,
) -> dict:
    """同步主体：五个阶段交给 StagedJob 驱动（拉取 / 消歧 / 投影 / 增强继承 / 快照提交）。

    阶段边界与迁移前的语句顺序一致：拉取 → 重算实体口径 → 复用判定 → 建投影 → 继承增强
    → 写快照与 KG → 提交指针 → 标 stale → 回收 → 清理旧投影。
    指针提交前任何阶段失败/取消都不写快照与投影；提交后失败只记 warning，不回滚已提交的投影。
    """
    state: dict[str, Any] = {"delta": {}, "resolution": None, "reused": False, "inherited": None}

    def _fetch(ctx) -> None:
        # 旧端点（无连接）继续写 config.json，保持升级前行为；连接模式凭据落在 connections/。
        if legacy_source:
            storage.ragflow_save_config(dataset_id, {"base_url": base_url, "api_key": api_key})
        previous, pointer = storage.ragflow_snapshot_current(dataset_id, source_dir=source_dir)
        # 实体规范化 / LLM 消歧开关（按数据源）。读取与校验放在最前面：
        # 预设不可用时直接失败，避免先拉一遍 RAGFlow 再报错。
        settings = storage.ragflow_settings_read(dataset_id, source_dir=source_dir)
        try:
            resolution_mode = entity_resolution.normalize_mode(
                settings.get("entity_resolution") or "disabled"
            )
        except ValueError as exc:
            log.warning("实体消歧配置非法（%s），按默认 disabled 继续同步", exc)
            resolution_mode = "disabled"
        aliases = settings.get("aliases") if isinstance(settings.get("aliases"), dict) else {}
        resolution_preset = None
        if resolution_mode in entity_resolution.LLM_MODES:
            preset_id = settings.get("llm_preset_id")
            resolution_preset = storage.get_preset(preset_id) if preset_id else None
            if not resolution_preset or resolution_preset.get("kind") not in storage.LLM_PRESET_KINDS:
                raise entity_resolution.EntityResolutionError(
                    "实体消歧开关已开启，但测试集生成用的 LLM 预设不可用："
                    "请到配置页检查预设，或关闭开关后重试"
                )
        config_fingerprint = entity_resolution.compute_config_fingerprint(
            resolution_mode,
            settings.get("llm_preset_id"),
            (resolution_preset or {}).get("model"),
            aliases,
        )
        client = RAGFlowClient(base_url, api_key, timeout=timeout)
        ctx.log(f"开始读取 RAGFlow 知识库 {dataset_id}")
        snapshot, delta = fetch_snapshot(client, dataset_id, previous, full_refresh)
        state.update({
            "previous": previous, "pointer": pointer, "settings": settings,
            "resolution_mode": resolution_mode, "resolution_preset": resolution_preset,
            "aliases": aliases, "config_fingerprint": config_fingerprint,
            "snapshot": snapshot, "delta": delta,
        })

    async def _resolve(ctx) -> None:
        resolution_mode = state["resolution_mode"]
        previous = state["previous"]
        snapshot = state["snapshot"]
        delta = state["delta"]
        resolution = None
        # 开关开启时在复用判定之前重算实体口径并固化进快照：
        # 已判定过的候选对靠缓存命中做到 0 token，本地 blocking / rules 是 CPU。
        # ⚠ 候选对超过 `_MAX_PAIRS` 时，被截断的对会在后续同步里继续判定（见 resolve_entities_async），
        # 所以「重复同步 0 token」要等所有候选判定完才成立：真实库实测（412 对 / 上限 200）
        # 前三轮分别花 98,602 / 94,763 / 5,240 token，第四轮才 412 对全命中缓存、0 token。
        # 不要据此以为第二次同步就免费。
        if resolution_mode != "disabled":
            ctx.log(f"实体口径：{resolution_mode}，先合并同义实体再建投影")
            try:
                resolution = await entity_resolution.resolve_entities_async(
                    snapshot,
                    mode=resolution_mode,
                    preset=state["resolution_preset"],
                    aliases=state["aliases"],
                    dataset_id=dataset_id,
                    source_dir=source_dir,
                    exclude_keys=entity_exclusion_keys(snapshot),
                    config_fingerprint=state["config_fingerprint"],
                    task=task,
                    telemetry=getattr(task, "generation_telemetry", None),
                    policy=policy,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败必须在写快照/投影/指针之前中止
                ctx.log(f"实体消歧失败，本次同步中止（未写快照/投影/指针）：{exc}", level="error")
                raise
            rewritten = entity_resolution.apply_to_snapshot(snapshot, resolution)
            delta = _restate_delta_after_resolution(delta, snapshot, previous)
            # 重算内容指纹：排除 entity_resolution 段，避免 cache_hits 抖动改变生成指纹
            snapshot["source_hash"] = _sha({
                key: value for key, value in snapshot.items()
                if key not in ("fetched_at", "source_hash", "ragflow_version", "entity_resolution")
            })
            stats = resolution["stats"]
            ctx.log(
                f"实体消歧完成：候选 {stats['pairs_observed']} 对"
                f"（截断 {stats['pairs_truncated']}，送 LLM {stats['pairs_sent']}），"
                f"规则命中 {stats['rule_hits']}，"
                f"LLM {stats['llm_calls']} 次，缓存命中 {stats['cache_hits']}，改写 {rewritten} 个分块",
                **stats,
            )
        elif previous and previous.get("entity_resolution"):
            # 开关从开切到关：上次快照里是合并后的实体名，本次是 RAGFlow 原始写法，
            # fetch_snapshot 按实体比较会把所有带实体的分块判成变化、给历史测试集误打 stale。
            # 与开启时同一口径：只按正文指纹判定分块变化（投影仍会因配置变化重建）。
            delta = _restate_delta_after_resolution(delta, snapshot, previous)
        verify_note = _describe_content_verify(delta)
        ctx.log(f"源快照完成：新增 {len(delta['added_chunk_ids'])}、变化 {len(delta['changed_chunk_ids'])}、删除 {len(delta['deleted_chunk_ids'])} 个 chunk"
                + (f"；{verify_note}" if verify_note else ""),
                **{k: v for k, v in delta.items() if k.endswith("ids")})
        state.update({"delta": delta, "resolution": resolution, "verify_note": verify_note})

    def _project(ctx) -> None:
        previous = state["previous"]
        pointer = state["pointer"]
        settings = state["settings"]
        snapshot = state["snapshot"]
        resolution_mode = state["resolution_mode"]
        resolution = state["resolution"]
        # 幂等复用：只按“影响投影内容”的指纹判断。
        # 元数据（update_time/status/run）变化不再触发重建，连续两次同步投影字节级一致。
        # C2：投影参数（实体重叠阈值/黑名单）也纳入复用条件——调了参数要重新算边，
        # 但参数保持默认时配置字典与存量投影完全一致，不会产生一次性无谓重建。
        projection_config = {"entity_resolution": resolution_mode, **_overlap_config_override()}
        if resolution_mode != "disabled":
            # 启用态额外固化口径指纹与统计：指纹变化即触发重建，统计只供审计（比较时剔除）
            projection_config.update({
                "config_fingerprint": state["config_fingerprint"],
                "prompt_version": entity_resolution.PROMPT_VERSION,
                "llm_preset_id": settings.get("llm_preset_id"),
                "llm_model": (state["resolution_preset"] or {}).get("model"),
                "entity_resolution_stats": dict((resolution or {}).get("stats") or {}),
            })
        existing = storage.kg_get(pointer.get("projection_kg_id", "")) if (previous and pointer) else None
        if (existing
                and _projection_fingerprint(snapshot) == _projection_fingerprint(previous)
                and _projection_config_core(existing.get("projection_config")) == _projection_config_core(projection_config)):
            verify_note = state.get("verify_note") or ""
            ctx.log((
                "源快照未变化，复用现有投影（未重建、未调用 LLM）" if resolution_mode == "disabled"
                else "源快照未变化且实体口径一致，复用现有投影（未重建）"
            ) + (f"；{verify_note}" if verify_note else ""), reused=True)
            state["reused"] = True
            return
        projection = build_ragas_projection(snapshot, source_id=source_id)
        snapshot_id = storage.new_id("rfsnap")
        projection_id = storage.new_id("kg")
        projection_version = snapshot_id
        projection["projection_version"] = projection_version
        projection["projection_config"] = projection_config
        previous_kg_id = (pointer or {}).get("projection_kg_id") if pointer else None
        # 指针里可能残留已被清理的旧投影 id：索引与文件都不存在时置空、按首次同步处理。
        # 否则下面会拿死 id 去认领测试集（kg_id 命中即视为同源），把无关测试集误标 stale。
        if previous_kg_id and not storage.kg_get(previous_kg_id) \
                and not storage.kg_file_exists(previous_kg_id):
            ctx.log(f"旧投影 {previous_kg_id} 已失效（索引与文件均不存在），本次按首次同步处理",
                    missing_kg_id=previous_kg_id)
            previous_kg_id = None
        state.update({
            "projection": projection, "snapshot_id": snapshot_id, "projection_id": projection_id,
            "projection_version": projection_version, "previous_kg_id": previous_kg_id,
        })

    def _inherit(ctx) -> None:
        if state["reused"]:
            return
        # R-2：内容未变的节点继承旧投影的增强成果，避免每次同步都按全库重付增强 token
        inherited = {"chunks": 0, "documents": 0}
        previous_kg_id = state["previous_kg_id"]
        if previous_kg_id:
            try:
                previous_projection = storage.kg_read_json(previous_kg_id)
                inherited = inherit_enhancement(state["projection"], previous_projection)
            except Exception as exc:  # noqa: BLE001 —— 继承只是省钱，失败不影响同步本身
                log.warning("继承旧投影增强成果失败（照常重建，出题前会重新增强）: %s", exc)
        if inherited["chunks"] or inherited["documents"]:
            ctx.log(f"沿用旧投影的增强成果：{inherited['chunks']} 个分块、{inherited['documents']} 篇文档"
                    "（内容未变，出题前不必重新生成摘要 / 向量）",
                    inherited_chunks=inherited["chunks"], inherited_documents=inherited["documents"])
        state["inherited"] = inherited

    async def _commit(ctx) -> dict:
        snapshot = state["snapshot"]
        delta = state["delta"]
        resolution_mode = state["resolution_mode"]
        resolution = state["resolution"]
        verify_note = state.get("verify_note") or ""
        pointer = state["pointer"]
        if state["reused"]:
            # 复查路径也要回收：快照没变但历史 stale 可能是旧口径（实体变化）留下的，
            # 按出处判定复核仍一致就清掉；legacy 样本不动。
            try:
                reconciled = await worker_call(
                    task, reconcile_testsets_stale, dataset_id, pointer["projection_kg_id"],
                    source_connection_id=connection_id, legacy_source=legacy_source,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("回收过期 stale 标记失败（源快照未变化）: %s", exc)
                reconciled = []
            note = _reconcile_note(reconciled)
            if note:
                # 复用/复查路径也要打这行，口径与重建路径一致（同一条文案来源）
                ctx.log(note, stale_cleared=sum(row["cleared"] for row in reconciled))
            return {"kg_id": pointer["projection_kg_id"], "snapshot_id": pointer["snapshot_id"],
                    "delta": delta, "stale_testset_ids": [], "stale_reconciled": reconciled,
                    "still_stale_ids": [], "reused": True,
                    "entity_resolution": _entity_resolution_payload(resolution_mode, resolution),
                    "report": _sync_report(snapshot, delta, reused=True,
                                           entity_resolution=_entity_resolution_payload(resolution_mode, resolution))}
        projection = state["projection"]
        snapshot_id = state["snapshot_id"]
        projection_id = state["projection_id"]
        projection_version = state["projection_version"]
        previous_kg_id = state["previous_kg_id"]
        inherited = state["inherited"]
        source_path = await worker_call(
            task, storage.ragflow_snapshot_write, dataset_id, snapshot_id, snapshot, source_dir=source_dir,
        )
        kg_path = await worker_call(task, storage.kg_write_json, projection_id, projection)
        entry = {
            "id": projection_id,
            "name": name or f"RAGFlow 投影-{dataset_id}-{storage.now_iso()}",
            "kg_path": kg_path,
            "created_at": storage.now_iso(),
            "mode": "ragflow-projection",
            "source_type": SOURCE_RAGFLOW,
            "source_dataset_id": dataset_id,
            "source_connection_id": connection_id,
            "source_id": source_id,
            "source_snapshot_id": snapshot_id,
            "source_hash": snapshot["source_hash"],
            "ragflow_version": snapshot.get("ragflow_version"),
            "projection_version": projection_version,
            "projection_config": projection["projection_config"],
            "source_files": [doc.get("name") or doc_id for doc_id, doc in snapshot.get("documents", {}).items()],
            "source_chunk_ids": sorted(snapshot.get("chunks", {})),
            "changed_chunk_ids": delta["changed_chunk_ids"],
            "deleted_chunk_ids": delta["deleted_chunk_ids"],
            "node_count": len(projection["nodes"]),
            "relationship_count": len(projection["relationships"]),
            "stale_testset_ids": [],
        }
        await worker_call(task, storage.kg_upsert, entry)
        # A2：先提交指针，再标记 stale。指针失败时回滚新投影，历史样本保持原状。
        try:
            await worker_call(
                task, storage.ragflow_snapshot_commit,
                dataset_id, snapshot_id, source_path, projection_id, projection_version,
                source_dir=source_dir, connection_id=connection_id, source_id=source_id,
            )
        except Exception:
            await worker_call(task, storage.kg_delete, projection_id)
            raise
        # stale 标记是派生动作：失败只记 warning，不回滚已提交的投影（否则指针与索引会不一致）。
        try:
            changed_testsets = await worker_call(
                task, mark_testsets_stale, dataset_id, previous_kg_id, delta,
                source_connection_id=connection_id, legacy_source=legacy_source,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("标记历史测试集为 stale 失败（投影与指针已提交）: %s", exc)
            changed_testsets = []
        else:
            # 只回填 stale 标记：锁内重读条目再改，避免用同步开始时的旧副本覆盖增强 / 改绑写入的字段
            await worker_call(
                task, storage.kg_update, projection_id,
                lambda current: current.update(stale_testset_ids=changed_testsets),
            )
        # 反向回收：来源恢复（正文回到被引用时的样子）或只是实体变化时，撤掉过期的 stale 标记。
        # 判据复用 P2 出处判定，legacy（无内容指纹）样本保持原标记。
        try:
            reconciled = await worker_call(
                task, reconcile_testsets_stale, dataset_id, projection_id,
                previous_kg_id=previous_kg_id, source_connection_id=connection_id, legacy_source=legacy_source,
            )
        except Exception as exc:  # noqa: BLE001 —— 派生动作，失败不阻断已提交的投影
            log.warning("回收过期 stale 标记失败（投影与指针已提交）: %s", exc)
            reconciled = []
        # A3：指针已切到新投影，再清理同数据源的旧投影（含 KG 索引条目）。
        # 止血：仍被任何测试集引用的旧投影保留，避免历史测试集的原图/质检/再生成断链。
        try:
            cleanup = await worker_call(task, _cleanup_old_projections, dataset_id, projection_id, connection_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("清理旧投影失败（新投影已提交，旧投影保留）: %s", exc)
            cleanup = {"removed": [], "retained": [], "scan_failed": True}
        removed = cleanup["removed"]
        retained = cleanup["retained"]
        retained_note = ""
        if retained:
            detail = "；".join(
                f"{row['kg_id']} ← {','.join(row['testset_ids']) or '（引用方 id 未记录）'}"
                for row in retained[:_RETAIN_EVIDENCE_LIMIT]
            )
            retained_note = f"，保留 {len(retained)} 个仍被测试集引用的旧投影（{detail}）"
        if cleanup["scan_failed"]:
            retained_note += "，旧投影清理已跳过（测试集引用扫描失败）"
        reconcile_note = _reconcile_note(reconciled)
        if reconcile_note:
            reconcile_note = "，" + reconcile_note
        # 置位后仍处于 stale 的测试集：只剔除本次**全部**回收（remaining == 0）的；
        # 部分回收的测试集还有 stale 样本，界面仍要提示
        still_stale_ids = sorted(
            set(changed_testsets) - {row["testset_id"] for row in reconciled if not row.get("remaining")}
        )
        ctx.log(f"投影已保存：{entry['name']}（{entry['node_count']} 节点 / {entry['relationship_count']} 关系），标记 {len(changed_testsets)} 个历史测试集为 stale"
                + (f"，清理 {len(removed)} 个旧投影" if removed else "") + reconcile_note + retained_note
                + (f"；{verify_note}" if verify_note else ""),
                projection_kg_id=projection_id, snapshot_id=snapshot_id)
        return {"kg_id": projection_id, "snapshot_id": snapshot_id, "delta": delta,
                "stale_testset_ids": changed_testsets, "stale_reconciled": reconciled,
                "still_stale_ids": still_stale_ids,
                "node_count": entry["node_count"],
                "relationship_count": entry["relationship_count"], "removed_kg_ids": removed,
                "retained_kg_ids": [row["kg_id"] for row in retained],
                "entity_resolution": _entity_resolution_payload(resolution_mode, resolution),
                "report": _sync_report(snapshot, delta, reused=False, inherited=inherited,
                                       entity_resolution=_entity_resolution_payload(resolution_mode, resolution))}

    telemetry = getattr(task, "generation_telemetry", None) or GenerationTelemetry(
        token_budget=policy.token_budget
    )
    task.generation_telemetry = telemetry
    task.policy = policy.execution_kwargs()
    ledger = UsageLedger()
    ledger.update(telemetry.snapshot())
    job = StagedJob(
        task,
        [
            Stage("fetching", _fetch),
            Stage("entity_resolution", _resolve),
            Stage("projection", _project),
            Stage("inherit", _inherit),
            Stage("commit", _commit),
        ],
        policy=policy,
        persist=_persist_sync,
        ledger=ledger,
        name="ragflow_sync",
    )
    job.ctx.bind_telemetry(telemetry)
    return await job.run()


async def _persist_sync(ctx) -> None:
    """同步没有半成品：快照 / 投影 / 指针都在 commit 阶段一并提交，失败时按设计不落盘。

    终态账单由 ``manager.worker_finished`` 从 ``task.generation_telemetry`` 回写；
    这里再刷新一次，保证取消时已花的 token 也能落到任务上。
    """
    ctx.refresh_usage()
    ctx.task.usage.update(ctx.ledger.snapshot())
