""" P2：样本出处（provenance）落盘与只读判定。

设计见 设计文档 的 P2 章节。

核心口径：
- 出处**只在生成时**从 ragas 场景节点（`scenario.nodes`）直接取，不做文本反查 ——
  文本正是会变的东西，一旦正文被编辑，文本比对既证明不了「同一出处」也证明不了「内容未变」。
- 内容指纹复用 `ragflow._sha`（对 `page_content` 做 sha256），与快照/投影的
  `content_hash` 完全同口径，不另写一份哈希函数。
- 没有内容哈希的历史样本一律判 `legacy`：**来源不可信、不能自动判有效**，
  也不回填伪造哈希（现有的 3 个 RAGFlow 测试集样本属于这一类）。
- 本模块只判定，不删样本、不改样本、不触发重生成（P3 才做定向重生成）。

线程/分层约束：本模块属于 `ragas_adapter/`，不 import ragas；路由层只调本模块函数。
"""
from __future__ import annotations

from .ragflow import _sha  # 复用同一指纹口径：sha256(json.dumps(value))

# 本方案版本号：落进样本，便于后续判定逻辑升级时区分老新出处格式
PROVENANCE_VERSION = 1

STATUS_VALID = "valid"        # 引用的 chunk 全部存在且 content_hash 一致
STATUS_STALE = "stale"        # 引用的 chunk 存在但内容已变
STATUS_ORPHAN = "orphan"      # 引用的 chunk 在投影里已不存在
STATUS_LEGACY = "legacy"      # 无内容哈希（历史样本），不参与自动判定
STATUS_UNKNOWN = "unknown"    # 当前投影读不到，无法判定（降级为「未知」而不是「有效」）

REASON_NO_HASH = "no_content_hash"
REASON_CHUNK_MISSING = "source_chunk_missing"
REASON_CONTENT_CHANGED = "content_changed"
REASON_PROJECTION_UNAVAILABLE = "projection_unavailable"


def content_hash(text: str | None) -> str:
    """chunk 正文的内容指纹，口径与 `ragflow` 快照的 `content_hash` 完全一致。"""
    return _sha(text or "")


# ---------- 生成时落盘 ----------

def kg_revision(kg_entry: dict) -> str | None:
    """取 KG 索引里已有的版本字段（投影版本）；非投影图没有版本，返回 None。"""
    entry = kg_entry or {}
    return entry.get("projection_version") or entry.get("source_snapshot_id")


def _node_type_name(value) -> str:
    """Node.type 可能是 NodeType 枚举，也可能是 JSON 里的字符串；统一成小写名字。"""
    name = getattr(value, "name", None)
    return str(name or value or "").lower()


def _node_id(node) -> str:
    return str(getattr(node, "id", ""))


def _node_props(node) -> dict:
    return getattr(node, "properties", None) or {}


def attach_provenance(samples: list[dict], node_ids_by_index: dict[int, list[str]],
                     kg, kg_entry: dict) -> None:
    """把每条样本实际引用的 chunk 节点落成结构化出处（生成时调用，0 LLM）。

    `node_ids_by_index` 来自 `_PhasedGeneration` 在提交样本 job 时记录的
    `scenario.nodes` 节点 id —— 这是 ragas 侧的原始映射，不是文本反查。
    解析不到 chunk 节点的样本保持无出处（读取时判 legacy）。

    文档归属直接读 chunk 的 `doc_id`（阶段 3 契约）：不再沿 `child` 边反查 document 节点，
    否则不同来源的文档 id 会在样本里混成两种口径。字段名 `source_document_ids` 保持不变，
    值即各 chunk 的 `doc_id`。
    """
    nodes = {_node_id(n): n for n in (getattr(kg, "nodes", None) or [])}

    revision = kg_revision(kg_entry)
    for index, sample in enumerate(samples):
        cited: list[str] = []
        seen: set[str] = set()
        for raw_id in node_ids_by_index.get(index) or []:
            node_id = str(raw_id)
            node = nodes.get(node_id)
            if node is None or _node_type_name(getattr(node, "type", None)) != "chunk":
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            cited.append(node_id)
        if not cited:
            continue
        hashes = [content_hash(_node_props(node).get("page_content")) for node in (nodes[c] for c in cited)]
        document_ids = sorted({str(_node_props(nodes[c]).get("doc_id") or "") for c in cited} - {""})
        upstream_ids = [
            str(_node_props(nodes[c]).get("source_chunk_id"))
            for c in cited if _node_props(nodes[c]).get("source_chunk_id")
        ]
        sample["source_chunk_ids"] = cited
        sample["source_content_hashes"] = hashes
        sample["source_document_ids"] = document_ids
        # 上游 RAGFlow chunk id：仅供既有的 stale 传播逻辑继续可用（投影节点 id 与它不同）
        if upstream_ids:
            sample["source_upstream_chunk_ids"] = upstream_ids
        sample["kg_revision"] = revision
        sample["provenance_version"] = PROVENANCE_VERSION


def annotate_legacy_flag(samples: list[dict]) -> list[dict]:
    """读取时标注（只在内存里）：无内容哈希的历史样本标 `legacy: true`。

    不回填伪造哈希、不落盘 —— 来源不可信就不假装可信，后续判定一律走 legacy。
    """
    for sample in samples:
        sample["legacy"] = not bool(sample.get("source_content_hashes"))
    return samples


# ---------- 只读判定 ----------

def _dict_node_type(node: dict) -> str:
    return _node_type_name(node.get("type"))


def _projection_chunks(projection: dict) -> dict[str, str]:
    """投影 JSON 的 chunk 节点：节点 id -> page_content。"""
    chunks: dict[str, str] = {}
    for node in (projection or {}).get("nodes") or []:
        if _dict_node_type(node) != "chunk":
            continue
        props = node.get("properties") or {}
        chunks[str(node.get("id"))] = str(props.get("page_content") or "")
    return chunks


def judge_sample(sample: dict, chunks: dict[str, str], projection_available: bool) -> dict:
    """单条样本的出处判定（只读）。

    优先级：legacy（无哈希）> unknown（投影不可用）> orphan（有 chunk 缺失）
    > stale（内容变了）> valid。
    """
    cited = [str(v) for v in sample.get("source_chunk_ids") or []]
    hashes = [str(v) for v in sample.get("source_content_hashes") or []]
    result = {"index": sample.get("index"), "status": STATUS_LEGACY, "legacy": True,
              "reason": REASON_NO_HASH, "source_chunk_ids": cited}
    if not cited or not hashes or len(cited) != len(hashes):
        return result
    result["legacy"] = False
    if not projection_available:
        result.update(status=STATUS_UNKNOWN, reason=REASON_PROJECTION_UNAVAILABLE)
        return result
    missing = [chunk_id for chunk_id in cited if chunk_id not in chunks]
    if missing:
        result.update(status=STATUS_ORPHAN, reason=REASON_CHUNK_MISSING,
                      missing_chunk_ids=sorted(missing))
        return result
    changed = [chunk_id for chunk_id, old_hash in zip(cited, hashes)
               if content_hash(chunks[chunk_id]) != old_hash]
    if changed:
        result.update(status=STATUS_STALE, reason=REASON_CONTENT_CHANGED,
                      changed_chunk_ids=sorted(changed))
        return result
    result.update(status=STATUS_VALID, reason=None)
    return result


def clear_recovered_stale(samples: list[dict], chunks: dict[str, str]) -> dict:
    """按出处判定回收**过期**的 stale 标记（只清「有内容指纹且复核有效」的样本）。

    背景：`mark_testsets_stale` 只看「受影响的 chunk 集合」，把实体变化也算作正文变化，
    与 P2 出处判定（只比正文哈希）口径不一致；来源恢复、或只是实体重算时，
    旧的 stale 标记会一直留着。这里复用同一套 `judge_sample`，判为 valid 才清。

    legacy（无内容指纹）样本无法复核，**保持原标记不动**（返回 undetermined_legacy 计数），
    既不假装「已恢复」也不额外置位。只改内存，不落盘。
    """
    cleared = 0
    undetermined_legacy = 0
    for sample in samples:
        if not sample.get("stale"):
            continue
        row = judge_sample(sample, chunks, True)
        if row["legacy"]:
            undetermined_legacy += 1
            continue
        if row["status"] != STATUS_VALID:
            continue
        sample.pop("stale", None)
        sample.pop("stale_reason", None)
        cleared += 1
    return {
        "cleared": cleared,
        "remaining": sum(1 for sample in samples if sample.get("stale")),
        "undetermined_legacy": undetermined_legacy,
    }


def evaluate_samples_provenance(ts_data: dict, projection: dict | None) -> dict:
    """对一个已读入的测试集做只读判定（不写回任何文件）。"""
    samples = ts_data.get("samples") or []
    projection_available = projection is not None
    chunks = _projection_chunks(projection or {})
    judged = [judge_sample(sample, chunks, projection_available) for sample in samples]
    counts = {status: 0 for status in
              (STATUS_VALID, STATUS_STALE, STATUS_ORPHAN, STATUS_LEGACY, STATUS_UNKNOWN)}
    for row in judged:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "testset_id": ts_data.get("id"),
        "kg_id": ts_data.get("kg_id"),
        "projection_available": projection_available,
        "total": len(judged),
        "counts": counts,
        "samples": judged,
    }


def evaluate_testset_provenance(ts_id: str, projection_kg_id: str | None = None) -> dict:
    """输入 testset_id + 当前投影，输出每条样本的判定（供 P3 使用，只读）。

    默认用测试集自身的 `kg_id` 作为「当前投影」；同步完成后可显式传新投影 id。
    """
    from .. import storage

    data = storage.ts_read(ts_id)
    if data is None:
        raise ValueError(f"测试集 {ts_id} 不存在")
    target_kg_id = projection_kg_id or data.get("kg_id")
    projection = storage.kg_read_json(target_kg_id) if target_kg_id else None
    result = evaluate_samples_provenance(data, projection)
    result["judged_against_kg_id"] = target_kg_id
    return result
