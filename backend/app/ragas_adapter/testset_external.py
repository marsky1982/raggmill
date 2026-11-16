"""Step 2 外部导入：外部 KG JSON / 分块 / 文档直接出题。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

from ..policies import GenerationPolicy
from ..storage import add_document, kg_upsert, kg_write_json, new_id, now_iso
from ..tasks.manager import Task, manager
from .doc_identity import (
    SOURCE_EXTERNAL, apply_identity, external_chunks_identity, external_kg_document_identity,
    node_kind, validate_external_kg,
)
from .kg import _kg_to_dict, build_kg
from .testset_generation import generate_testset


# ---------- 外部导入：跳过 Step1 UI，直接生成测试集 ----------

def _color_external_kg(kg_json: dict, source_name: str) -> None:
    """外部整份 KG JSON 的染色：document 节点按节点 id 取身份，chunk 沿 `child` 边继承。

    没有 `child` 归属的 chunk 合并成一篇「游离分块」文档，用内容指纹区分同名不同内容。
    """
    nodes = kg_json.get("nodes") or []
    by_id = {str(node.get("id")): node for node in nodes}
    doc_identities: dict[str, dict] = {}
    for node in nodes:
        if node_kind(node) != "document":
            continue
        props = node.setdefault("properties", {})
        filename = str((props.get("document_metadata") or {}).get("filename") or "")
        identity = external_kg_document_identity(source_name, str(node.get("id")),
                                                 filename or source_name)
        apply_identity(props, identity)
        doc_identities[str(node.get("id"))] = identity

    colored: set[str] = set()
    for rel in kg_json.get("relationships") or []:
        if str(rel.get("type") or "") != "child":
            continue
        identity = doc_identities.get(str(rel.get("source")))
        target = by_id.get(str(rel.get("target")))
        if identity is None or target is None or node_kind(target) != "chunk":
            continue
        apply_identity(target.setdefault("properties", {}), identity)
        colored.add(str(target.get("id")))

    orphans = [node for node in nodes
               if node_kind(node) == "chunk" and str(node.get("id")) not in colored]
    if orphans:
        identity = external_chunks_identity(source_name, [
            str((node.get("properties") or {}).get("page_content") or "") for node in orphans
        ])
        for node in orphans:
            apply_identity(node.setdefault("properties", {}), identity)


def prepare_external_kg_from_json(kg_json: dict, source_name: str) -> str:
    """模式 A：外部直接传入完整 KG JSON 快照（ragas KnowledgeGraph 序列化格式）。

    写盘并登记到 kg_index，返回 kg_id。节点 id 缺失/重复时拒绝导入（身份不稳定）。
    快照须含 nodes / relationships 两个数组，节点/关系结构须与 ragas graph.py 一致。
    """
    if not isinstance(kg_json, dict) or "nodes" not in kg_json:
        raise ValueError("kg_json 格式不正确：须为含 nodes 数组的 dict")
    validate_external_kg(kg_json)
    _color_external_kg(kg_json, source_name)
    kg_id = new_id("kg")
    kg_path = kg_write_json(kg_id, kg_json)
    node_count = len(kg_json.get("nodes", []))
    rel_count = len(kg_json.get("relationships", []))
    entry = {
        "id": kg_id, "name": source_name or f"外部导入KG-{now_iso()}",
        "created_at": now_iso(), "mode": "external", "source_type": SOURCE_EXTERNAL,
        "kg_path": kg_path, "node_count": node_count,
        "relationship_count": rel_count, "doc_ids": [], "doc_names": [],
        "llm_preset_id": None, "embedding_preset_id": None,
    }
    kg_upsert(entry)
    return kg_id


def prepare_external_kg_from_chunks(chunks: list[dict], source_name: str) -> tuple[str, dict]:
    """模式 B：外部传入预切分 chunks，构建最小 KG（仅 chunk 节点，无 transforms）。

    每个 chunk 形如 {"page_content": "...", "properties": {"themes": [...], ...}}。
    properties 可选，但 themes/entities 缺失时 MultiHop 题型将无法生成。
    返回 (kg_id, health_report)。
    """
    from ragas.testset.graph import KnowledgeGraph, Node, NodeType

    if not chunks:
        raise ValueError("chunks 列表不能为空")

    # 无 document 节点：整份 chunks 合成一篇文档身份，哈希输入带内容指纹
    chunk_texts = [str(c.get("page_content", "") or "") for c in chunks]
    identity = external_chunks_identity(source_name, chunk_texts)

    nodes: list = []
    has_themes = has_entities = has_summary = has_embedding = 0
    for c in chunks:
        props = {"page_content": c.get("page_content", "")}
        extra = c.get("properties") or {}
        for k, v in extra.items():
            props[k] = v
        apply_identity(props, identity)
        if "themes" in props:
            has_themes += 1
        if "entities" in props:
            has_entities += 1
        if "summary" in props:
            has_summary += 1
        if "summary_embedding" in props or "embedding" in props:
            has_embedding += 1
        nodes.append(Node(type=NodeType.CHUNK, properties=props))

    kg = KnowledgeGraph(nodes=nodes)
    kg_id = new_id("kg")
    kg_path = kg_write_json(kg_id, _kg_to_dict(kg))

    n = len(nodes)
    warnings: list[str] = []
    if has_themes == 0:
        warnings.append("文本分块（chunk）缺少主题标签（themes）→ 多跳抽象题无法按主题出题")
    if has_entities == 0:
        warnings.append("文本分块（chunk）缺少实体标签（entities）→ 建不出实体重叠边（entities_overlap），"
                        "只能靠语义相似边（summary_similarity）串联")
    if has_summary == 0:
        warnings.append("文本分块（chunk）缺少摘要（summary）→ 建不出语义相似边（summary_similarity），"
                        "多跳抽象题会生成失败")

    health = {
        "node_count": n, "relationship_count": 0,
        "themes_coverage": f"{has_themes}/{n}",
        "entities_coverage": f"{has_entities}/{n}",
        "summary_coverage": f"{has_summary}/{n}",
        "embedding_coverage": f"{has_embedding}/{n}",
        "warnings": warnings,
    }

    entry = {
        "id": kg_id, "name": source_name or f"外部导入Chunks-{now_iso()}",
        "created_at": now_iso(), "mode": "external-chunks", "source_type": SOURCE_EXTERNAL,
        "kg_path": kg_path, "node_count": n, "relationship_count": 0,
        "doc_ids": [], "doc_names": [],
        "llm_preset_id": None, "embedding_preset_id": None,
    }
    kg_upsert(entry)
    return kg_id, health


async def generate_testset_external(
    task: Task,
    mode: str,                         # "kg_json" | "chunks" | "documents"
    kg_json: dict | None,
    chunks: list[dict] | None,
    documents: list[dict] | None,     # [{"name": "...", "text": "..."}]
    llm_preset: dict,
    emb_preset: dict,
    testset_size: int,
    distribution: dict | None,
    use_default: bool,
    name: str,
    abstract_relation_property: str = "summary_similarity",
    source_name: str = "",
    min_tokens: int = 500,
    max_tokens: int = 1000,
    *,
    policy: GenerationPolicy | None = None,
) -> dict:
    """外部导入编排：根据 mode 准备 KG（或链式构建），再调 generate_testset。"""
    from .telemetry import GenerationTelemetry
    policy = policy or GenerationPolicy()
    task.policy = policy.execution_kwargs()
    if not getattr(task, "generation_telemetry", None):
        task.generation_telemetry = GenerationTelemetry(token_budget=policy.token_budget)
    task.generation_telemetry.should_stop = task.cancel_event.is_set
    health: dict | None = None

    if mode == "kg_json":
        if not kg_json:
            raise ValueError("mode=kg_json 时 kg_json 不能为空")
        kg_id = prepare_external_kg_from_json(kg_json, source_name)
        manager.log(task, f"外部 KG 快照已导入：kg_id={kg_id}", extra={"stage": "kg_prepared", "progress": 0.05})

    elif mode == "chunks":
        if not chunks:
            raise ValueError("mode=chunks 时 chunks 不能为空")
        kg_id, health = prepare_external_kg_from_chunks(chunks, source_name)
        manager.log(task, f"外部 chunks 已构建为 KG：kg_id={kg_id}（{health['node_count']} 节点）",
                    extra={"stage": "kg_prepared", "progress": 0.05, "health": health})
        for w in health.get("warnings", []):
            manager.log(task, f"⚠ {w}", extra={"stage": "warn", "progress": 0.06})

    elif mode == "documents":
        if not documents:
            raise ValueError("mode=documents 时 documents 不能为空")
        # 注册外部文档到 DOCUMENTS，再调 build_kg（含 A′ 分块增强）
        doc_ids: list[str] = []
        for d in documents:
            doc = add_document(d.get("name", f"doc-{now_iso()}"),
                               d.get("text", ""), len(d.get("text", "")), source="external")
            doc_ids.append(doc["id"])
        manager.log(task, f"外部文档已注册（{len(doc_ids)} 篇），开始构建知识图谱"
                          f"（含分块摘要与语义相似边增强）……",
                    extra={"stage": "kg_building", "progress": 0.02})
        result = await build_kg(
            task, doc_ids, llm_preset, emb_preset,
            name=source_name or f"外部导入-{now_iso()}",
            min_tokens=min_tokens, max_tokens=max_tokens,
            enhance_chunks=True,
            source_type=SOURCE_EXTERNAL,
            policy=policy,
        )
        kg_id = result["kg_id"]
        manager.log(task, f"KG 构建完成：{result['node_count']} 节点 / {result['relationship_count']} 关系",
                    extra={"stage": "kg_built", "progress": 0.1})

    else:
        raise ValueError(f"不支持的 mode: {mode}（仅 kg_json / chunks / documents）")

    # 复用核心生成逻辑
    return await generate_testset(
        task, kg_id, llm_preset, emb_preset, testset_size,
        distribution, use_default, name, abstract_relation_property,
        policy=policy,
    )
