"""RAGFlow 数据源的实体规范化与消歧（按 source 开关）。

本模块只负责「实体名池合并」，不做图谱投影，也不碰路由层：

    blocking（0 token）→ 规则（0 token）→ hybrid/llm（仅在规则未定且缓存未命中时花钱）

设计文档：`设计文档`。要点：
- `disabled` / `rules` / 缓存命中都是 0 token；
- `hybrid` 只把「规则未定 + 缓存未命中」的候选对送给 LLM，返回三态 `same/different/unsure`；
- `unsure` 一律不合并（少一条边比错并实体安全）；
- 任何失败都抛 `EntityResolutionError`，由同步任务在写快照/投影/指针之前中止。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
from typing import Any, Callable

from .. import storage

log = logging.getLogger(__name__)

ENTITY_RESOLUTION_MODES = ("disabled", "rules", "llm", "hybrid")
LLM_MODES = ("llm", "hybrid")
PROMPT_VERSION = "entity-resolve-v1"
RULES_VERSION = "rules-v1"
SETTINGS_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1

# blocking 阈值。Jaccard 用 2-gram（中英混排都能覆盖，按字符计）；模糊匹配用 stdlib 的
# `SequenceMatcher.ratio()` = 2*M/(len(a)+len(b))，即按两串**总长**归一，不是编辑距离。
# 口径：两串等长 L 时 ratio = 1 - d/L，`≥ 0.7` 恰好等价于「编辑距离 d ≤ 30% × L」；
# 长度不等时（分母变大）比「短名 30%」更严，前缀扩展类写法基本进不来。
# 长度门槛 4：3 字及以下的名字上 1 个字符的差异常常已经换了对象，真正的同义写法
# （如「阿里云」/「阿里云计算」）仍可由 2-gram Jaccard 或同 chunk 共现进入候选。
# 候选偏多只影响 token（合并仍要 rules / LLM 判定），所以这里宁可宽一点。
_JACCARD_MIN = 0.6
_EDIT_MIN_LEN = 4
_EDIT_SIMILARITY = 0.7
# 单个分块里实体数超过该值时跳过「共现」候选，避免 O(n²) 爆炸（正常分块远低于此）。
_MAX_COEXIST_ENTITIES = 60
# 同一首字符桶过大时跳过模糊匹配（极端长尾）。
_MAX_BUCKET = 400

# 候选对超限时按「更可能同义」打分排序取 top-K，不再用字典序（2026-11-08）。
# 权重取舍：
# - 词形相似度是「同义」的直接证据，权重最高；Jaccard 用 2-gram，对 CJK 与长名更稳，
#   故 2.0 高于 `SequenceMatcher` 的 1.5（后者在短串上区分力弱）。
# - 同块共现只是「相关」证据：每个共现分块 +0.6，最多计 3 块（1.8）。
#   单块共现压不过高相似对，避免真实库里 400+ 共现对把付费预算全吃掉；
#   但「共现 + 中等相似」会稳定排在「只是相似」之前。
# 同分时按规范化键字典序兜底，保证同一快照两次同步的送出顺序完全一致。
_W_SCORE_JACCARD = 2.0
_W_SCORE_EDIT = 1.5
_W_SCORE_COOCCUR = 0.6
_COOCCUR_SCORE_CAP = 3

_CACHE_LIMIT = 2000


def _cache_keep_limit(in_use: int) -> int:
    """缓存写回时保留的条数：至少 `_CACHE_LIMIT`，且不少于**本轮用到的结论数**（命中 + 新判定）。

    缓存按最近使用时间淘汰。候选对多于 `_CACHE_LIMIT` 时，固定上限会把本轮刚判定、刚付过钱的
    结论淘汰掉，下次同步又送 LLM 重付，永远收敛不了。本轮用到的结论 used_at 都是最新的，
    排在最前，保留条数不少于它们就不会被挤掉；更早的、已不再出现的旧键照常淘汰。
    """
    return max(_CACHE_LIMIT, int(in_use))

# 单对候选的 token 三档系数（输入 + 输出合计）。2026-11-08 用真实供应商（某推理模型）
# 校准：413 对候选实花 输入 156,961 + 输出 32,596 = 189,557 token，均值 459/对
# （输入 380 / 输出 79）。按 413 对的 prompt 长度反推单对区间约 325–495（prompt 419–707 字，
# 中文约 0.59 token/字），故下限取最短 prompt 档 320、预期取实测均值 460、
# 最坏取实测上限再加约 20% 余量（覆盖偶发重试与更长输出）。校准记录见
# `.run-logs/entity_resolution_calibration_20261108.md`，换模型/prompt 版本后需重测。
_PAIR_TOKENS_LOW = 320
_PAIR_TOKENS_EXPECTED = 460
_PAIR_TOKENS_HIGH = 600


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        log.warning("环境变量 %s=%r 不是整数，回落默认 %d", name, raw, default)
        return default


# 口径类开关留在环境变量（属于「怎么算」，不是「怎么跑」）；超时/重试/并发统一走 GenerationPolicy。
_MAX_PAIRS = max(0, _env_int("RAGFLOW_ENTITY_LLM_MAX_PAIRS", 200))
_STRICT = (os.environ.get("RAGFLOW_ENTITY_RESOLUTION_STRICT") or "").strip().lower() in (
    "1", "true", "yes", "on",
)


class EntityResolutionError(RuntimeError):
    """实体消歧失败：同步任务据此中止，且不写快照/投影/指针。"""


def _normalize(value: Any) -> str:
    """复用 ragflow 的确定性规范化，避免两套口径。

    惰性 import：ragflow 会在顶层 import 本模块，直接 import 会循环。
    """
    from .ragflow import normalize_entity

    return normalize_entity(value)


def default_settings() -> dict:
    """settings.json 的默认值（开关关闭，行为与现状一致）。"""
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "entity_resolution": "disabled",
        "llm_preset_id": None,
        "prompt_version": PROMPT_VERSION,
        "updated_at": "",
    }


def normalize_mode(value: Any) -> str:
    mode = str(value or "disabled").strip().lower()
    if mode not in ENTITY_RESOLUTION_MODES:
        raise ValueError(f"entity_resolution 必须是 {ENTITY_RESOLUTION_MODES} 之一")
    return mode


def _normalized_aliases(aliases: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for canonical, items in (aliases or {}).items():
        ckey = _normalize(canonical)
        if not ckey:
            continue
        values = {_normalize(item) for item in (items or [])}
        values.discard("")
        values.discard(ckey)
        if values:
            out[ckey] = sorted(values)
    return dict(sorted(out.items()))


def compute_config_fingerprint(
    mode: str,
    preset_id: str | None = None,
    model: str | None = None,
    aliases: dict | None = None,
) -> str:
    """开关 + 预设 + 阈值 + 别名的配置指纹；任一变化都会换指纹并触发重建。"""
    payload = {
        "entity_resolution": normalize_mode(mode),
        "prompt_version": PROMPT_VERSION,
        "rules_version": RULES_VERSION,
        "llm_preset_id": preset_id,
        "llm_model": model,
        "thresholds": {
            "jaccard_min": _JACCARD_MIN,
            "edit_min_len": _EDIT_MIN_LEN,
            "edit_similarity": _EDIT_SIMILARITY,
            "max_pairs": _MAX_PAIRS,
        },
        "aliases": _normalized_aliases(aliases),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- 实体池与候选对（0 token） ----------

def _entity_context(text: str, name: str, limit: int = 200) -> str:
    """取实体名附近的上下文摘要（≤ limit 字符）。"""
    if not text:
        return ""
    low = text.casefold()
    idx = low.find(name.casefold())
    start = max(0, idx - 40) if idx >= 0 else 0
    return text[start:start + limit].strip()


def build_entity_pool(snapshot: dict) -> dict:
    """把 chunk 的实体字符串摊平成：规范化键 → 出现次数 / 展示名 / 所在分块 / 上下文。"""
    chunks = snapshot.get("chunks") or {}

    def _order(item: tuple[str, dict]) -> tuple[int, str]:
        chunk = item[1] or {}
        try:
            pos = int(chunk.get("position", 0) or 0)
        except (TypeError, ValueError):
            pos = 0
        return pos, str(item[0])

    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    chunk_keys: dict[str, set[str]] = {}
    context: dict[str, str] = {}
    for chunk_id, chunk in sorted(chunks.items(), key=_order):
        chunk = chunk or {}
        content = str(chunk.get("page_content") or "")
        keys: set[str] = set()
        for raw in chunk.get("entities") or []:
            key = _normalize(raw)
            if not key:
                continue
            name = str(raw).strip()
            counts[key] = counts.get(key, 0) + 1
            prev = display.get(key)
            # 同一规范化键的展示名：取字典序最小的原始写法（与 _apply_kb_entities 的稳定口径一致）
            if prev is None or (name.casefold(), name) < (prev.casefold(), prev):
                display[key] = name
            keys.add(key)
            if key not in context:
                context[key] = _entity_context(content, name)
        chunk_keys[str(chunk_id)] = keys
    return {"counts": counts, "display": display, "chunk_keys": chunk_keys, "context": context}


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)} or {text}


def _bucket(key: str) -> str:
    return key[:1] or "_"


def build_candidates(pool: dict, exclude_keys: set[str] | None = None) -> set[tuple[str, str]]:
    """产出候选对（规范化键，已排序）。只用本地信息，0 token。"""
    excluded = exclude_keys or set()
    keys = sorted(k for k in pool["counts"] if k not in excluded)
    pairs: set[tuple[str, str]] = set()

    # 1) 同分块共现
    for chunk_keys in pool["chunk_keys"].values():
        live = sorted(k for k in chunk_keys if k not in excluded)
        if len(live) > _MAX_COEXIST_ENTITIES:
            continue
        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                pairs.add((live[i], live[j]))

    # 2/3) 同首字符桶内的 2-gram Jaccard / 编辑距离近似
    buckets: dict[str, list[str]] = {}
    for key in keys:
        buckets.setdefault(_bucket(key), []).append(key)
    grams = {key: _bigrams(key) for key in keys}
    for bucket in buckets.values():
        if len(bucket) > _MAX_BUCKET:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                ga, gb = grams[a], grams[b]
                if len(ga & gb) / max(1, len(ga | gb)) >= _JACCARD_MIN:
                    pairs.add((a, b))
                    continue
                if len(a) >= _EDIT_MIN_LEN and len(b) >= _EDIT_MIN_LEN:
                    if difflib.SequenceMatcher(None, a, b).ratio() >= _EDIT_SIMILARITY:
                        pairs.add((a, b))
    return pairs


def rank_candidate_pairs(pool: dict, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按「更可能同义」给候选对打分并降序返回（0 token）。

    打分只用本地素材：同块共现次数 + 2-gram Jaccard + SequenceMatcher 相似度，
    权重与取舍见文件顶部 `_W_SCORE_*` 注释。截断只调用这个函数，不再依赖字典序。
    """
    key_chunks: dict[str, set[str]] = {}
    for chunk_id, keys in pool["chunk_keys"].items():
        for key in keys:
            key_chunks.setdefault(key, set()).add(str(chunk_id))
    grams = {key: _bigrams(key) for pair in pairs for key in pair}

    def score(pair: tuple[str, str]) -> float:
        a, b = pair
        ga, gb = grams[a], grams[b]
        jaccard = len(ga & gb) / max(1, len(ga | gb))
        edit = 0.0
        if len(a) >= _EDIT_MIN_LEN and len(b) >= _EDIT_MIN_LEN:
            edit = difflib.SequenceMatcher(None, a, b).ratio()
        shared = min(len(key_chunks.get(a, set()) & key_chunks.get(b, set())), _COOCCUR_SCORE_CAP)
        return round(
            _W_SCORE_JACCARD * jaccard + _W_SCORE_EDIT * edit + _W_SCORE_COOCCUR * shared,
            6,
        )

    return sorted(pairs, key=lambda pair: (-score(pair), pair[0], pair[1]))


# ---------- 规则层（0 token） ----------

# `normalize_entity` 会去首尾标点，所以 `阿里巴巴（Alibaba）` 规范化后是 `阿里巴巴(alibaba`：
# 右括号在末尾被剥掉，正则必须同时容忍「有/无右括号」。
_PAREN = re.compile(r"^(.*?)[（(]([^）)]*)[）)]?$")
_SEPARATOR = re.compile(r"[\s\-_]+")


def _alias_map(aliases: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, items in (aliases or {}).items():
        ckey = _normalize(canonical)
        if not ckey:
            continue
        for item in items or []:
            akey = _normalize(item)
            if akey and akey != ckey:
                out[akey] = ckey
    return out


def _rule_same(a: str, b: str, alias_map: dict[str, str]) -> bool:
    """确定性规则：只有能明确判定「同义」才返回 True，其余交给 LLM 或保持不合并。"""
    if alias_map:
        if alias_map.get(a) == b or alias_map.get(b) == a:
            return True
        if alias_map.get(a) and alias_map.get(a) == alias_map.get(b):
            return True
    # 括号缩写/全称：阿里巴巴（Alibaba） ↔ 阿里巴巴
    for left, right in ((a, b), (b, a)):
        match = _PAREN.match(left)
        if not match:
            continue
        head = _normalize(match.group(1))
        tail = _normalize(match.group(2))
        if (head and head == right) or (tail and tail == right):
            return True
    # 单复数 / 常见词尾
    for left, right in ((a, b), (b, a)):
        if len(left) >= 3 and left.endswith("es") and left[:-2] == right:
            return True
        if len(left) >= 3 and left.endswith("s") and left[:-1] == right:
            return True
    # 纯分隔符差异：or-tools ↔ or_tools
    if _SEPARATOR.sub("", a) == _SEPARATOR.sub("", b):
        return True
    return False


# ---------- 分析计划 ----------

def _analyze(snapshot: dict, aliases: dict | None, exclude_keys: set[str] | None) -> dict:
    """blocking + 规则判定，产出候选对与规则命中（0 token）。"""
    pool = build_entity_pool(snapshot)
    pairs = sorted(build_candidates(pool, exclude_keys))
    alias_map = _alias_map(aliases)
    rule_same = {pair for pair in pairs if _rule_same(pair[0], pair[1], alias_map)}
    return {"pool": pool, "pairs": pairs, "rule_same": rule_same}


def _cache_key(a: str, b: str, context_a: str, context_b: str, model: str | None) -> str:
    payload = {
        "pair": sorted([a, b]),
        "contexts": sorted([context_a or "", context_b or ""]),
        "model": model or "",
        "prompt_version": PROMPT_VERSION,
        "rules_version": RULES_VERSION,
        "thresholds": {
            "jaccard_min": _JACCARD_MIN,
            "edit_min_len": _EDIT_MIN_LEN,
            "edit_similarity": _EDIT_SIMILARITY,
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_prompt(pair: dict) -> str:
    return (
        "你是知识图谱实体消歧助手。给定两个实体名和它们在知识库中的上下文，"
        "判断它们是否指同一个事物。\n"
        "只输出 JSON，不要解释：{\"verdict\": \"same\"} 或 {\"verdict\": \"different\"} 或 {\"verdict\": \"unsure\"}。\n"
        "拿不准时必须输出 unsure。\n"
        "示例：实体A：阿里巴巴，实体B：阿里，上下文都指向电商集团 → {\"verdict\": \"same\"}\n"
        "示例：实体A：苹果，实体B：苹果公司，上下文分别指向水果与公司 → {\"verdict\": \"different\"}\n"
        f"实体A：{pair['name_a']}\n"
        f"实体B：{pair['name_b']}\n"
        f"上下文A：{pair['context_a'] or '（无）'}\n"
        f"上下文B：{pair['context_b'] or '（无）'}\n"
        "输出："
    )


_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(same|different|unsure)"', re.IGNORECASE)


def _parse_verdict(text: str) -> str:
    match = _VERDICT_RE.search(text or "")
    return match.group(1).lower() if match else "unsure"


async def _ask_llm(llm: Any, prompt: str) -> str:
    from langchain_core.prompt_values import StringPromptValue

    result = await llm.agenerate_text(StringPromptValue(text=prompt))
    return result.generations[0][0].text


async def _classify_pairs(
    items: list[dict],
    preset: dict,
    task: Any,
    telemetry: Any,
    on_result: Callable[[tuple[str, str], str], None],
    *,
    policy: Any = None,
) -> dict[tuple[str, str], str]:
    """对候选对逐个调 LLM（可打桩接缝）。

    `on_result` 在每对成功判定后立即回调，便于失败时把已得出的结论落进缓存。
    超时 / 重试 / 并发由 GenerationPolicy 决定（不再读环境变量）。
    """
    if preset is None:
        raise EntityResolutionError("实体消歧需要可用的 LLM 预设")
    from ..policies import GenerationPolicy
    from .clients import build_llm

    policy = policy or GenerationPolicy()
    attempts = policy.max_retries
    llm = build_llm(preset, timeout=policy.llm_timeout, sdk_max_retries=0, callbacks=[telemetry])
    semaphore = asyncio.Semaphore(policy.max_workers)
    results: dict[tuple[str, str], str] = {}

    async def _one(item: dict) -> None:
        pair = (item["a"], item["b"])
        async with semaphore:
            if task is not None and getattr(task, "cancel_event", None) is not None:
                if task.cancel_event.is_set():
                    raise asyncio.CancelledError
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    text = await _ask_llm(llm, _build_prompt(item))
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 统一按「调用失败」重试
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.5 * (attempt + 1))
            else:
                raise EntityResolutionError(
                    f"实体消歧 LLM 调用失败（已尝试 {attempts} 次）：{last_error}"
                )
            verdict = _parse_verdict(text)
            results[pair] = verdict
            on_result(pair, verdict)

    await asyncio.gather(*(_one(item) for item in items))
    return results


def _canonical_groups(decisions: dict[tuple[str, str], str], counts: dict[str, int],
                      display: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """union-find 合并；canonical 取组内出现次数最多、同频字典序最小的键。"""
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (a, b), verdict in decisions.items():
        if verdict == "same":
            union(a, b)

    groups: dict[str, list[str]] = {}
    for key in parent:
        groups.setdefault(find(key), []).append(key)
    canonical: dict[str, str] = {}
    canonical_display: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        best = min(members, key=lambda k: (-counts.get(k, 0), k))
        for member in members:
            canonical[member] = best
            canonical_display[member] = display.get(best, best)
    return canonical, canonical_display


async def resolve_entities_async(
    snapshot: dict,
    *,
    mode: str,
    preset: dict | None,
    aliases: dict | None,
    dataset_id: str,
    source_dir: Any = None,
    exclude_keys: set[str] | None = None,
    config_fingerprint: str,
    task: Any = None,
    telemetry: Any = None,
    policy: Any = None,
) -> dict:
    """同步时重算实体口径；返回 canonical 映射与统计（不直接改 snapshot）。"""
    mode = normalize_mode(mode)
    if mode == "disabled":
        raise ValueError("禁用态不应调用实体消歧")
    if mode in LLM_MODES and preset is None:
        raise EntityResolutionError("实体消歧已开启，但 LLM 预设不可用")

    analysis = await asyncio.to_thread(_analyze, snapshot, aliases, exclude_keys)
    pool, pairs, rule_same = analysis["pool"], analysis["pairs"], analysis["rule_same"]

    stats = {
        # 三档口径：真实候选（全量 blocking）/ 被截断丢弃 / 实际送 LLM
        "pairs_observed": len(pairs),
        "pairs_truncated": 0,
        "pairs_sent": 0,
        "rule_hits": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "unsure": 0,
        "tokens": {"input": 0, "output": 0},
    }
    decisions: dict[tuple[str, str], str] = {}
    # 规则层先跑全量候选：0 token 的确定性合并不能被付费上限截掉。
    # `_analyze` 已经对全量 pairs 跑过同一套 `_rule_same`，这里直接用它的结果。
    if mode != "llm":
        for pair in pairs:
            if pair in rule_same:
                decisions[pair] = "same"
                stats["rule_hits"] += 1

    pending: list[dict] = []
    pending_by_pair: dict[tuple[str, str], dict] = {}
    cache_dirty = False
    model = (preset or {}).get("model")
    # rules 模式全程 0 token：连缓存都不读，避免误以为会调用模型
    cache_entries: dict = {}
    if mode in LLM_MODES:
        cache_entries = storage.ragflow_entity_cache_read(dataset_id, source_dir=source_dir)
        # 缓存命中也属于 0 token 判定：先查缓存，再对「规则未定 + 缓存未命中」的剩余对截断，
        # 否则历史结论会占掉付费预算。
        for a, b in pairs:
            if (a, b) in decisions:
                continue
            context_a = pool["context"].get(a, "")
            context_b = pool["context"].get(b, "")
            key = _cache_key(a, b, context_a, context_b, model)
            hit = cache_entries.get(key)
            if hit and hit.get("verdict") in ("same", "different", "unsure"):
                decisions[(a, b)] = hit["verdict"]
                hit["used_at"] = storage.now_iso()
                stats["cache_hits"] += 1
                cache_dirty = True
                continue
            item = {
                "a": a,
                "b": b,
                "name_a": pool["display"].get(a, a),
                "name_b": pool["display"].get(b, b),
                "context_a": context_a,
                "context_b": context_b,
                "_cache_key": key,
            }
            pending.append(item)
            pending_by_pair[(a, b)] = item

    # 截断只作用于「真正要花钱」的剩余对，并按同义可能性打分取 top-K（不再按字典序）。
    if mode in LLM_MODES and _MAX_PAIRS and len(pending) > _MAX_PAIRS:
        truncated = len(pending) - _MAX_PAIRS
        if _STRICT:
            raise EntityResolutionError(
                f"实体消歧未决候选对 {len(pending)} 超过上限 {_MAX_PAIRS}（strict 模式中止）"
            )
        log.warning("实体消歧未决候选对 %d 超过上限 %d，按同义打分本次送前 %d 对；"
                    "其余 %d 对留到后续同步继续判定（已判定的结论进缓存，不会重复付费）",
                    len(pending), _MAX_PAIRS, _MAX_PAIRS, truncated)
        ranked = await asyncio.to_thread(
            rank_candidate_pairs, pool, [(item["a"], item["b"]) for item in pending]
        )
        # 按评分顺序重建 pending（同分时 rank 已用字典序兜底，保证两次同步顺序一致）
        pending = [pending_by_pair[pair] for pair in ranked[:_MAX_PAIRS]]
        stats["pairs_truncated"] = truncated
    stats["pairs_sent"] = len(pending)

    if pending:
        if telemetry is None:
            from ..policies import GenerationPolicy
            from .telemetry import GenerationTelemetry

            telemetry = GenerationTelemetry(
                token_budget=(policy or GenerationPolicy()).token_budget
            )

        answered = 0

        def _remember(pair: tuple[str, str], verdict: str) -> None:
            nonlocal answered
            answered += 1
            decisions[pair] = verdict
            item = pending_by_pair[pair]
            cache_entries[item["_cache_key"]] = {"verdict": verdict, "used_at": storage.now_iso()}

        try:
            await _classify_pairs(pending, preset, task, telemetry, _remember, policy=policy)
        finally:
            # 失败也保留已成功的结论（键含模型与 prompt，可安全复用）；失败结论不入缓存
            storage.ragflow_entity_cache_write(
                dataset_id, cache_entries, source_dir=source_dir,
                limit=_cache_keep_limit(stats["cache_hits"] + answered),
                schema_version=CACHE_SCHEMA_VERSION,
            )
        snapshot_usage = telemetry.snapshot()
        # 取两者较大值：打桩时遥测没有真实调用，取候选数；真实调用有重试时遥测更准
        stats["llm_calls"] = max(answered, int(snapshot_usage.get("llm_calls") or 0))
        stats["tokens"] = {
            "input": snapshot_usage.get("input_tokens", 0),
            "output": snapshot_usage.get("output_tokens", 0),
        }
    elif cache_dirty:
        # 只有缓存命中时也把最近使用时间写回（保持 LRU 语义）
        storage.ragflow_entity_cache_write(
            dataset_id, cache_entries, source_dir=source_dir,
            limit=_cache_keep_limit(stats["cache_hits"]), schema_version=CACHE_SCHEMA_VERSION,
        )

    stats["unsure"] = sum(1 for verdict in decisions.values() if verdict == "unsure")
    canonical, canonical_display = _canonical_groups(decisions, pool["counts"], pool["display"])
    canonical_of = {
        pool["display"].get(key, key): canonical_display[key]
        for key, target in canonical.items()
        if key != target and pool["display"].get(key, key) != canonical_display[key]
    }
    canonical_of = dict(sorted(canonical_of.items()))
    return {
        "mode": mode,
        "config_fingerprint": config_fingerprint,
        "prompt_version": PROMPT_VERSION,
        "llm_model": model,
        "llm_preset_id": (preset or {}).get("id"),
        "canonical_of": canonical_of,
        "canonical_display": canonical_display,
        "stats": stats,
    }


def apply_to_snapshot(snapshot: dict, result: dict) -> int:
    """按 canonical 展示名重写 chunk.entities，并把消歧段写进快照；返回被改写的分块数。"""
    canonical_display = result["canonical_display"]
    changed = 0
    for chunk in (snapshot.get("chunks") or {}).values():
        chunk = chunk or {}
        names: list[str] = []
        for raw in chunk.get("entities") or []:
            key = _normalize(raw)
            if not key:
                continue
            names.append(canonical_display.get(key) or str(raw).strip())
        rewritten = sorted(set(names), key=str.casefold)
        if rewritten != sorted(chunk.get("entities") or [], key=str.casefold):
            changed += 1
        chunk["entities"] = rewritten
    snapshot["entity_resolution"] = {
        "mode": result["mode"],
        "config_fingerprint": result["config_fingerprint"],
        "canonical_of": result["canonical_of"],
        **result["stats"],
    }
    return changed


def estimate_resolution(
    snapshot: dict,
    *,
    mode: str,
    aliases: dict | None,
    dataset_id: str,
    source_dir: Any = None,
    exclude_keys: set[str] | None = None,
    model: str | None = None,
) -> dict:
    """基于现有快照离线估算成本：0 请求、0 token（不拉 RAGFlow）。"""
    mode = normalize_mode(mode)
    analysis = _analyze(snapshot, aliases, exclude_keys)
    pairs, rule_same = analysis["pairs"], analysis["rule_same"]
    rule_hits = 0 if mode == "llm" else len(rule_same)
    cache_entries = storage.ragflow_entity_cache_read(dataset_id, source_dir=source_dir)
    pool = analysis["pool"]
    llm_pairs = 0
    cache_hits = 0
    for a, b in pairs:
        if mode != "llm" and (a, b) in rule_same:
            continue
        key = _cache_key(a, b, pool["context"].get(a, ""), pool["context"].get(b, ""), model)
        if cache_entries.get(key, {}).get("verdict") in ("same", "different", "unsure"):
            cache_hits += 1
        else:
            llm_pairs += 1
    # 与同步侧同一口径三档：真实候选 / 会被截断 / 实际送 LLM，成本只按实际送出的对数估
    truncated = 0
    if mode in LLM_MODES and _MAX_PAIRS and llm_pairs > _MAX_PAIRS:
        truncated = llm_pairs - _MAX_PAIRS
        llm_pairs = _MAX_PAIRS
    estimated = {"low": 0, "expected": 0, "high": 0}
    if mode in LLM_MODES:
        estimated = {
            "low": llm_pairs * _PAIR_TOKENS_LOW,
            "expected": llm_pairs * _PAIR_TOKENS_EXPECTED,
            "high": llm_pairs * _PAIR_TOKENS_HIGH,
        }
    return {
        "mode": mode,
        "candidate_pairs": len(pairs),
        "rule_hits": rule_hits,
        "cache_hits": cache_hits,
        "llm_pairs": llm_pairs,
        "pairs_truncated": truncated,
        "estimated": estimated,
        "basis": "snapshot",
    }
