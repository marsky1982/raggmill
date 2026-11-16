"""文档身份契约的唯一构造处：`doc_id` / `source_ref` / `source_type` / `doc_name`。

所有来源的节点都从这里拿身份，别处不再自己拼字段（阶段 3 契约硬切，不做双写与回退）：

| 来源 | doc_id |
|---|---|
| 本地上传（含先登记进文档库的外部 documents） | 沿用 `storage.stable_doc_id()`，即 `doc-` + md5(文件名)[:8] |
| RAGFlow 投影 | `rf-` + sha256(source_id + ":" + 上游 document id)[:12] |
| 外部整份 KG JSON | `ext-` + sha256(导入名 + ":" + document 节点 id)[:12] |
| external-chunks（无 document 节点） | `ext-` + sha256(导入名 + ":" + 全部 chunk 内容指纹)[:12] |

不 import ragas，也不碰文件系统。
"""
from __future__ import annotations

import hashlib
import json
import typing as t

SOURCE_LOCAL = "local"
SOURCE_RAGFLOW = "ragflow"
SOURCE_EXTERNAL = "external"
SOURCE_MIXED = "mixed"

DOC_ID_PREFIX_LOCAL = "doc-"
DOC_ID_PREFIX_RAGFLOW = "rf-"
DOC_ID_PREFIX_EXTERNAL = "ext-"

#: 身份字段的固定集合与顺序；`apply_identity` 只写这四个键。
IDENTITY_KEYS = ("doc_id", "source_ref", "source_type", "doc_name")

_HASH_LEN = 12


def _digest(*parts: str) -> str:
    """sha256(parts 用 ":") 拼接取前 12 位；与设计文档里的 `a + ":" + b` 同口径。"""
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:_HASH_LEN]


def content_fingerprint(texts: t.Iterable[str]) -> str:
    """一批 chunk 的内容指纹：`page_content` 排序后序列化成 JSON 数组再 sha256。"""
    ordered = sorted(str(text or "") for text in texts)
    payload = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def local_document_identity(doc: dict, source_type: str = SOURCE_LOCAL,
                            doc_name: str | None = None) -> dict:
    """本地文档（外部 documents 导入也算）身份：doc_id 用文档库里的 id，不要重算哈希。"""
    from ..storage import stable_doc_id

    name = str(doc_name if doc_name is not None else doc.get("name") or "")
    doc_id = str(doc.get("id") or stable_doc_id(name))
    return {"doc_id": doc_id, "source_ref": "", "source_type": source_type, "doc_name": name}


def ragflow_doc_id(source_id: str, upstream_document_id: str) -> str:
    """RAGFlow 文档的 `doc_id`：只对「连接来源 + 上游文档 id」稳定。

    投影染色与改绑重写共用这一处，别处不要自己拼前缀与哈希。
    """
    return DOC_ID_PREFIX_RAGFLOW + _digest(str(source_id), str(upstream_document_id))


def ragflow_document_identity(source_id: str, upstream_document_id: str, doc_name: str) -> dict:
    """RAGFlow 投影里的文档身份：doc_id 只对「连接来源 + 上游文档 id」稳定。"""
    return {
        "doc_id": ragflow_doc_id(source_id, upstream_document_id),
        "source_ref": str(upstream_document_id),
        "source_type": SOURCE_RAGFLOW,
        "doc_name": str(doc_name or ""),
    }


def ragflow_doc_id_remap(old_source_id: str, new_source_id: str,
                         upstream_document_ids: t.Iterable[str]) -> dict[str, str]:
    """改绑时的「旧 doc_id → 新 doc_id」映射；只含真正变化的项。

    输入是各投影里 document / chunk 节点上的 `source_ref`（上游 document id）。映射里
    没有的旧 id 原样保留，调用方不要自己按前缀猜。
    """
    mapping: dict[str, str] = {}
    for upstream_document_id in upstream_document_ids:
        upstream_document_id = str(upstream_document_id or "")
        if not upstream_document_id:
            continue
        old = ragflow_doc_id(old_source_id, upstream_document_id)
        new = ragflow_doc_id(new_source_id, upstream_document_id)
        if old != new:
            mapping[old] = new
    return mapping


def external_kg_document_identity(source_name: str, node_id: str, doc_name: str) -> dict:
    """外部整份 KG JSON 的文档身份：以图里 document 节点的 id 为锚，同一份图重复导入不变。

    `source_ref` 留空：整份 KG JSON 自带的上游 id 不可信，也不参与回写。
    """
    return {
        "doc_id": DOC_ID_PREFIX_EXTERNAL + _digest(str(source_name), str(node_id)),
        "source_ref": "",
        "source_type": SOURCE_EXTERNAL,
        "doc_name": str(doc_name or source_name or ""),
    }


def external_chunks_identity(source_name: str, texts: t.Iterable[str]) -> dict:
    """没有 document 节点的图（external-chunks / 外部图里游离的 chunk）合成一篇文档身份。"""
    return {
        "doc_id": DOC_ID_PREFIX_EXTERNAL + _digest(str(source_name), content_fingerprint(texts)),
        "source_ref": "",
        "source_type": SOURCE_EXTERNAL,
        "doc_name": str(source_name or ""),
    }


def apply_identity(properties: dict, identity: dict) -> dict:
    """把身份写进节点 properties（只增这四个键，其余属性原样保留）。"""
    for key in IDENTITY_KEYS:
        properties[key] = identity[key]
    return properties


def summarize_source_types(values: t.Iterable[str]) -> str:
    """条目级 source_type：单值原样，混合写 `mixed`，空集合返回空串。"""
    unique = {str(value) for value in values if value}
    if not unique:
        return ""
    if len(unique) == 1:
        return next(iter(unique))
    return SOURCE_MIXED


def node_kind(value: t.Any) -> str:
    """节点类型统一成小写名字：兼容 NodeType 枚举、`NodeType.CHUNK` 字符串与 dict。"""
    raw = value.get("type") if isinstance(value, dict) else value
    name = getattr(raw, "value", None) or getattr(raw, "name", None) or raw
    return str(name or "").rsplit(".", 1)[-1].lower()


def validate_external_kg(kg_json: t.Any) -> None:
    """外部整份 KG JSON 的接地校验：节点 id 必须存在且唯一，否则身份不稳定。

    ragas 的 `Node.id` 缺省是新 uuid4，导入一次就换一个身份；所以缺 id 或 id 重复必须
    在同步预检里就拒绝（HTTP 400），不能等它变成后台任务的 failed。
    """
    nodes = kg_json.get("nodes") if isinstance(kg_json, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("kg_json 格式不正确：须为含非空 nodes 数组的对象")

    # 节点不是对象（字符串 / 数字 / null）时读不到 id，和缺 id 一样属于身份不稳定
    not_object = [index + 1 for index, node in enumerate(nodes) if not isinstance(node, dict)]
    if not_object:
        sample = "、".join(str(index) for index in not_object[:5])
        raise ValueError(f"外部 KG JSON 有 {len(not_object)} 个节点不是对象（第 {sample} 个），"
                         "无法读取节点 id：请检查文件格式后重新导入")

    missing = [index + 1 for index, node in enumerate(nodes)
               if not str(node.get("id") or "").strip()]
    if missing:
        sample = "、".join(str(index) for index in missing[:5])
        raise ValueError(f"外部 KG JSON 有 {len(missing)} 个节点缺少 id（第 {sample} 个），"
                         "无法生成稳定的文档身份：请在导出图谱时保留节点 id 后重新导入")

    seen: set[str] = set()
    duplicated: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id"))
        if node_id in seen:
            duplicated.add(node_id)
        seen.add(node_id)
    if duplicated:
        sample = "、".join(sorted(duplicated)[:5])
        raise ValueError(f"外部 KG JSON 存在重复的节点 id（{sample}），无法确定文档身份："
                         "请修掉重复 id 后重新导入")
