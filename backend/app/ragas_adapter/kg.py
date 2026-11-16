"""Step 1：知识图谱生成（真实 ragas / 演示 mock），含断点续跑与进度增强。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
from pathlib import Path

from ..config import RAGAS_AVAILABLE
from ..policies import GenerationPolicy
from ..storage import (
    document_snapshot, ckpt_delete, ckpt_load, ckpt_read_meta, ckpt_save, ckpt_update,
    kg_upsert, kg_write_json, new_id, now_iso,
)
from ..tasks.manager import Task, manager, worker_call
from ..tasks.staged import STOP_BUDGET_EXCEEDED, STOP_COMPLETED, STOP_FAILED, Stage, StagedJob, UsageLedger
from .clients import build_embeddings, build_llm, configure_run  # noqa: F401
from .doc_identity import (
    SOURCE_LOCAL, apply_identity, local_document_identity, node_kind, summarize_source_types,
)

# 各 ragas transform 阶段的中文说明（类名 -> (标题, 说明)）
STAGE_DESC: dict[str, tuple[str, str]] = {
    "HeadlinesExtractor": ("提取文档标题结构", "扫描文档识别标题层级，构建 headline 节点"),
    "HeadlineSplitter": ("按标题切分文档", "依据标题结构将长文档切分为语义块 chunk"),
    "SummaryExtractor": ("LLM 生成块摘要", "调用 LLM 为每个 chunk 生成 summary / themes / keyphrases"),
    "CustomNodeFilter": ("过滤低质量节点", "移除过短或低信息量块，提升图谱质量"),
    "Parallel": ("并行抽取向量/主题/实体", "EmbeddingExtractor 计算向量 + ThemesExtractor/NERExtractor 抽取主题与实体"),
    "EmbeddingExtractor": ("计算文本向量", "为节点生成 embedding，供相似度关系使用"),
    "ThemesExtractor": ("抽取主题标签", "为每个节点标注主题类别"),
    "NERExtractor": ("抽取命名实体", "识别节点中的实体（产品/接口/指标等）"),
    "CosineSimilarityBuilder": ("建立余弦相似关系", "基于 embedding 计算节点间相似度并建边"),
    "OverlapScoreBuilder": ("建立实体重叠关系", "基于 NER 重叠计算节点间关系"),
    # A′ 增强：分块级补充（让 MultiHopAbstract 能按原始设计工作）
    "ChunkSummaryExtractor": ("分块级摘要补充", "为每个 chunk 调用 LLM 生成 summary，补齐抽象多跳题所需素材"),
    "ChunkEmbeddingExtractor": ("分块级向量补充", "为每个 chunk 的 summary 计算 summary_embedding 向量"),
    "ChunkCosineSimilarityBuilder": ("分块级语义相似边", "基于 chunk 向量建立 summary_similarity 边，使抽象多跳题能跨块聚类"),
}

# 演示模式每个阶段的节奏等待（秒）。单列成常量，测试可改小以加速，不改变真实流程。
_DEMO_PAUSE_SECONDS = 0.7

DEMO_STAGES = [
    "HeadlinesExtractor 提取标题结构",
    "HeadlineSplitter 切分文档",
    "SummaryExtractor 生成摘要",
    "CustomNodeFilter 过滤低质块",
    "EmbeddingExtractor 计算向量",
    "ThemesExtractor / NERExtractor 抽取主题与实体",
    "CosineSimilarityBuilder / OverlapScoreBuilder 建立关系",
]

_THEMES = ["产品能力", "部署架构", "计费策略", "安全合规", "接口协议", "性能指标", "故障处理", "数据治理"]
_ENTITIES = ["RAGAS", "KnowledgeGraph", "TestsetGenerator", "FastAPI", "React", "SSE", "Fernet", "OpenAI"]


def _demo_kg(docs: list[dict]):
    """演示模式：用确定性随机生成一份 KnowledgeGraph（可直接 save/load）。"""
    from ragas.testset.graph import KnowledgeGraph, Node, Relationship
    rng = random.Random(42)
    nodes, rels = [], []

    def add_node(ntype: str, props: dict) -> Node:
        n = Node(type=ntype, properties=props)
        nodes.append(n)
        return n

    def add_rel(rtype: str, src: Node, tgt: Node, props: dict | None = None, bi: bool = True) -> None:
        rels.append(Relationship(type=rtype, source=src, target=tgt,
                                 bidirectional=bi, properties=props or {}))

    for d in docs:
        text = d.get("text") or f"{d['name']} 的演示内容。" * 60
        doc_node = add_node("document", {
            "page_content": text[:400],
            "document_metadata": {"filename": d["name"], "source": d.get("source", "upload")},
            "page_summary": f"《{d['name']}》涵盖" + "、".join(rng.sample(_THEMES, 3)) + "等主题。",
            "themes": rng.sample(_THEMES, 4),
            "summary_embedding": [round(rng.uniform(-1, 1), 6) for _ in range(8)],
        })
        chunk_nodes = []
        for i in range(6):
            c = add_node("chunk", {
                "page_content": f"{d['name']} 第{i + 1}节：围绕{rng.choice(_THEMES)}展开说明……",
                "summary": f"该块讨论{rng.choice(_THEMES)}相关内容",
                "themes": rng.sample(_THEMES, 2),
                "keyphrases": rng.sample(_ENTITIES, 3),
                "summary_embedding": [round(rng.uniform(-1, 1), 6) for _ in range(8)],
            })
            chunk_nodes.append(c)
            add_rel("child", doc_node, c, bi=False)
        for i in range(len(chunk_nodes) - 1):
            add_rel("summary_similarity", chunk_nodes[i], chunk_nodes[i + 1],
                    {"summary_similarity": round(rng.uniform(0.5, 0.99), 4)})
            if rng.random() < 0.5:
                # 与真实 KG 同名：ragas 的实体重叠边类型是 entities_overlap，分数键是 entities_overlap_score
                add_rel("entities_overlap", chunk_nodes[i], chunk_nodes[(i + 2) % len(chunk_nodes)],
                        {"entities_overlap_score": round(rng.uniform(0.1, 0.8), 4)})
    return KnowledgeGraph(nodes=nodes, relationships=rels)


def _chunk_enhance_transforms(llm, embedding_model):
    """A′ 增强：给 chunk 补 summary / summary_embedding / summary_similarity 边。

    ragas 的 default_transforms 把这三步的 filter_nodes 写死成只作用于 DOCUMENT，
    导致 chunk 间永远连不出语义相似边，MultiHopAbstractQuerySynthesizer 饿死。
    这里在默认流水线末尾追加三个 chunk 级步骤，用子类化获得独立的类名以区分进度日志。
    幂等性：chunk 上 summary/summary_embedding 尚不存在，不会重复写入；
            CosineSimilarityBuilder 仅在 filter_nodes 命中的节点间建边，不影响文档级已有边。
    """
    from ragas.testset.graph import NodeType
    from ragas.testset.transforms.extractors import EmbeddingExtractor, SummaryExtractor
    from ragas.testset.transforms.relationship_builders import CosineSimilarityBuilder

    class ChunkSummaryExtractor(SummaryExtractor):
        """分块级摘要抽取。"""

    class ChunkEmbeddingExtractor(EmbeddingExtractor):
        """分块级向量抽取。"""

    class ChunkCosineSimilarityBuilder(CosineSimilarityBuilder):
        """分块级语义相似边；候选分块不足 2 个时跳过。

        ragas 的 `_find_similar_embedding_pairs` 开头是 `n_embeddings, _dim = embeddings.shape`：
        0 条分块时 `np.array([]).shape == (0,)` 解包失败（ValueError: expected 2, got 1），
        1 条分块时虽不崩也建不出任何边。统一在候选节点 < 2 时不发任务。
        """

        def generate_execution_plan(self, kg):
            if len(self.filter(kg).nodes) < 2:
                return []
            return super().generate_execution_plan(kg)

    def filter_chunks(node):
        return node.type == NodeType.CHUNK

    return [
        ChunkSummaryExtractor(llm=llm, filter_nodes=filter_chunks),
        ChunkEmbeddingExtractor(
            embedding_model=embedding_model,
            property_name="summary_embedding",
            embed_property_name="summary",
            filter_nodes=filter_chunks,
        ),
        ChunkCosineSimilarityBuilder(
            property_name="summary_embedding",
            new_property_name="summary_similarity",
            threshold=SUMMARY_SIMILARITY_THRESHOLD,
            filter_nodes=filter_chunks,
        ),
    ]


ENHANCEMENT_VERSION = 1
SUMMARY_SIMILARITY_THRESHOLD = 0.7


def _edge_key(rel) -> tuple:
    """关系去重键：同类型 + 无序端点，兼容 bidirectional 的正反写法。"""
    return (rel.type, *sorted((rel.source.id, rel.target.id)))


def _dedupe_documents(kg) -> int:
    """合并同一篇文档的重复 document 节点，返回移除的节点数（幂等，多跑无副作用）。

    ragas 的 `HeadlineSplitter.split` 在「切不出多个块」时 `return [node], []`，而
    `Splitter.generate_execution_plan` 会把这个**同一个**节点对象再 append 进 `kg.nodes`，
    于是短文档在节点列表里出现两次（同 id）；随后文档级 CosineSimilarityBuilder 还会在
    两个副本之间建一条 cosine≈1 的 self-loop 边。

    判定：`document_metadata.filename` 相同 **且** 正文 sha256 相同 = 同一篇文档，只留第一个。
    处理：同 id 的重复先按 id 去重；不同 id 的副本把端点重指到保留节点；端点已不存在的边、
    self-loop 与重复边一并丢弃，避免留下孤儿边。
    """
    from ragas.testset.graph import NodeType

    # ① 同 id 重复（同一个对象被 append 两次）
    unique_nodes, seen = [], set()
    for node in kg.nodes:
        if node.id in seen:
            continue
        seen.add(node.id)
        unique_nodes.append(node)
    removed = len(kg.nodes) - len(unique_nodes)

    # ② 不同 id 但同名同文的副本
    groups: dict[tuple[str, str], list] = {}
    for node in unique_nodes:
        if node.type != NodeType.DOCUMENT:
            continue
        props = node.properties or {}
        filename = str((props.get("document_metadata") or {}).get("filename") or "")
        content = props.get("page_content")
        if not filename or not isinstance(content, str):
            continue
        key = (filename, hashlib.sha256(content.encode("utf-8")).hexdigest())
        groups.setdefault(key, []).append(node)

    remap: dict = {}
    for nodes in groups.values():
        keep = nodes[0]
        for node in nodes[1:]:
            remap[node.id] = keep
    if remap:
        unique_nodes = [n for n in unique_nodes if n.id not in remap]
        seen = {n.id for n in unique_nodes}
        removed += len(remap)
    else:
        seen = {n.id for n in unique_nodes}
    kg.nodes = unique_nodes
    if removed == 0:
        return 0

    rels, edge_keys = [], set()
    for rel in kg.relationships:
        source = remap.get(rel.source.id, rel.source)
        target = remap.get(rel.target.id, rel.target)
        if source.id == target.id:      # 副本之间的 self-loop
            continue
        if source.id not in seen or target.id not in seen:
            continue
        if source is not rel.source or target is not rel.target:
            rel.source, rel.target = source, target
        key = _edge_key(rel)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        rels.append(rel)
    kg.relationships = rels
    return removed


def _derive_document_summaries(kg) -> int:
    """投影 KG 的 document.page_content 只是文件名，用子 chunk 摘要确定性拼出文档摘要（0 次 LLM）。

    ragas persona 会取所有带 summary_embedding 节点的 summary；若文档有向量却没有 summary，
    该节点会被跳过但向量仍参与聚类，导致矩阵维度错位。所以文档摘要必须补，且不应再调一次 LLM。
    """
    from ragas.testset.graph import NodeType

    doc_chunks: dict[str, list] = {}
    for rel in kg.relationships:
        if rel.type == "child":
            doc_chunks.setdefault(str(rel.source.id), []).append(rel.target)
    derived = 0
    for node in kg.nodes:
        if node.type != NodeType.DOCUMENT or node.properties.get("summary"):
            continue
        texts = [
            child.properties["summary"]
            for child in doc_chunks.get(str(node.id), [])
            if isinstance(child.properties.get("summary"), str)
            and child.properties["summary"].strip()
        ]
        if not texts:
            continue
        name = str((node.properties.get("document_metadata") or {}).get("filename") or "文档")
        node.properties["summary"] = f"《{name}》" + "；".join(texts)[:2000]
        derived += 1
    return derived


def enhance_existing_kg(kg, llm, embedding_model, *, run_config=None,
                        threshold: float = SUMMARY_SIMILARITY_THRESHOLD,
                        include_themes: bool = True) -> dict:
    """给已有 KG（如 RAGFlow 投影）补齐抽象多跳与 persona 所需素材。

    复用 Step 1 A′ 增强的同一批 ragas transform（SummaryExtractor / ThemesExtractor /
    EmbeddingExtractor / CosineSimilarityBuilder），阈值同为 0.7，不改 ragas 源码：
      · chunk 摘要：LLM（仅补缺失项）
      · chunk themes：LLM（仅补缺失项）
      · document 摘要：由子 chunk 摘要确定性拼接（0 次 LLM）
      · summary_embedding：embedding 接口（文档 + chunk）
      · summary_similarity 边：基于向量确定性计算，重复执行按端点去重
    幂等性靠 filter_nodes「只命中缺失节点」+ 边去重实现：第二次执行时所有 filter 命中 0 个节点，
    不会重复写属性、不会重复建边，也不会再发 LLM 调用。

    include_themes=False 时跳过 themes（只有多跳抽象题用它；只出单跳 / 多跳具体题时
    给每个 chunk 再调一次 LLM 纯属浪费， ①）。
    ragas 的 apply_transforms 会把同批任务全部跑完、最后才抛第一个异常，所以失败时
    **已成功节点的属性已经写进 kg**；调用方据此可以保存已付费的部分成果（见 testset._enhance_projection_kg）。
    """
    from ragas.run_config import RunConfig
    from ragas.testset.graph import NodeType
    from ragas.testset.transforms import apply_transforms
    from ragas.testset.transforms.extractors import EmbeddingExtractor, SummaryExtractor
    from ragas.testset.transforms.extractors.llm_based import ThemesExtractor
    from ragas.testset.transforms.relationship_builders import CosineSimilarityBuilder

    run_config = run_config or RunConfig()

    def chunk_missing_summary(node):
        summary = node.properties.get("summary")
        return node.type == NodeType.CHUNK and not (isinstance(summary, str) and summary.strip())

    def chunk_missing_themes(node):
        return node.type == NodeType.CHUNK and not node.properties.get("themes")

    def node_missing_embedding(node):
        summary = node.properties.get("summary")
        return (node.type in (NodeType.DOCUMENT, NodeType.CHUNK)
                and isinstance(summary, str) and bool(summary.strip())
                and node.properties.get("summary_embedding") is None)

    def chunk_with_embedding(node):
        return node.type == NodeType.CHUNK and node.properties.get("summary_embedding") is not None

    pending_summary = sum(1 for node in kg.nodes if chunk_missing_summary(node))
    pending_themes = (sum(1 for node in kg.nodes if chunk_missing_themes(node))
                      if include_themes else 0)

    if pending_summary:
        apply_transforms(kg, SummaryExtractor(llm=llm, filter_nodes=chunk_missing_summary), run_config)
    docs_derived = _derive_document_summaries(kg)
    if pending_themes:
        apply_transforms(kg, ThemesExtractor(llm=llm, filter_nodes=chunk_missing_themes), run_config)

    pending_embeddings = sum(1 for node in kg.nodes if node_missing_embedding(node))
    if pending_embeddings:
        apply_transforms(kg, EmbeddingExtractor(
            embedding_model=embedding_model,
            property_name="summary_embedding",
            embed_property_name="summary",
            filter_nodes=node_missing_embedding,
        ), run_config)

    embedded_chunks = sum(1 for node in kg.nodes if chunk_with_embedding(node))
    existing_similarity = sum(1 for rel in kg.relationships if rel.type == "summary_similarity")
    # 仅当出现新向量、或一条相似边都没有时才重算；否则重复执行只会白跑 O(n²) 且建不出新边
    if embedded_chunks >= 2 and (pending_embeddings or existing_similarity == 0):
        apply_transforms(kg, CosineSimilarityBuilder(
            property_name="summary_embedding",
            new_property_name="summary_similarity",
            threshold=threshold,
            filter_nodes=chunk_with_embedding,
        ), run_config)

    before_count = len(kg.relationships)
    unique, seen = [], set()
    for rel in kg.relationships:
        key = _edge_key(rel)
        if key in seen:
            continue
        seen.add(key)
        unique.append(rel)
    if len(unique) != before_count:
        kg.relationships = unique

    changed = bool(pending_summary or docs_derived or pending_themes
                   or pending_embeddings or len(unique) != before_count)
    return {
        "version": ENHANCEMENT_VERSION,
        "chunk_summaries": pending_summary,
        "document_summaries": docs_derived,
        "theme_chunks": pending_themes,
        "embeddings": pending_embeddings,
        "similarity_edges": sum(1 for rel in kg.relationships if rel.type == "summary_similarity"),
        "changed": changed,
    }


def estimate_similarity_edges(kg, *, threshold: float = SUMMARY_SIMILARITY_THRESHOLD,
                              property_name: str = "summary_embedding") -> dict:
    """按现有向量预演「增强后最多能建出多少条 summary_similarity 边」。

    ragas 的 CosineSimilarityBuilder 只对 CHUNK 节点做两两余弦，且只有 >= threshold 才建边；
    这里用同一口径预计算，供能力判定如实告知「补齐向量后仍然够不到阈值」的语料，
    避免能力报告承诺自动补齐、实际却在预检阶段 0 条边失败。
    纯只读：不修改 KG，不调用任何外部服务。
    """
    import numpy as np
    from ragas.testset.graph import NodeType

    groups: dict[int, list] = {}
    skipped = 0
    for node in kg.nodes:
        if node.type != NodeType.CHUNK:
            continue
        vector = node.properties.get(property_name)
        if vector is None:
            continue
        try:
            arr = np.asarray(vector, dtype=float).ravel()
        except (TypeError, ValueError):
            skipped += 1
            continue
        if arr.size == 0:
            skipped += 1
            continue
        groups.setdefault(int(arr.size), []).append(arr)

    pairs = reachable = 0
    max_similarity: float | None = None
    for vectors in groups.values():
        if len(vectors) < 2:
            continue
        matrix = np.stack(vectors)
        norms = np.linalg.norm(matrix, axis=1)
        valid = norms > 0
        matrix, norms = matrix[valid], norms[valid]
        if len(matrix) < 2:
            continue
        unit = matrix / norms[:, None]
        sims = unit @ unit.T
        values = sims[np.triu_indices(len(matrix), k=1)]
        pairs += int(values.size)
        reachable += int((values >= threshold).sum())
        peak = float(values.max())
        max_similarity = peak if max_similarity is None else max(max_similarity, peak)

    return {
        "property_name": property_name,
        "threshold": threshold,
        "node_type": "chunk",
        "embedded_nodes": sum(len(v) for v in groups.values()),
        "skipped_vectors": skipped,
        "pairs": pairs,
        "reachable_edges": reachable,
        "max_pairwise_similarity": max_similarity,
    }


def _build_step1_clients(llm_preset: dict, emb_preset: dict, policy: GenerationPolicy, telemetry):
    """统一构造 Step 1 客户端，确保 SDK 重试关闭、RunConfig 只负责一次重试。"""
    llm = build_llm(llm_preset, timeout=policy.llm_timeout, sdk_max_retries=0, callbacks=[telemetry])
    run_config = configure_run(
        llm,
        timeout=policy.llm_timeout,
        retries=policy.max_retries,
        workers=policy.max_workers,
    )
    embeddings = build_embeddings(
        emb_preset,
        timeout=policy.llm_timeout,
        sdk_max_retries=0,
        telemetry=telemetry,
        max_retries=policy.max_retries,
    )
    return llm, embeddings, run_config


def _kg_to_dict(kg) -> dict:
    """将 ragas KnowledgeGraph 转为可 json 序列化的 dict（与 save 的格式一致）。"""
    return {
        "nodes": [n.model_dump() for n in kg.nodes],
        "relationships": [r.model_dump() for r in kg.relationships],
    }


def _color_doc_identity(kg, docs: list[dict], source_type: str) -> str:
    """建图终态：按 `child` 边把文档身份写进 document / chunk 节点（唯一一次染色）。

    `child` 边在这里只剩「归谁」这一个用途：之后所有读路径都直接读 chunk 的 `doc_id`，
    不再反查边。全新建图与断点续跑走同一个完成出口，所以续跑出来的图不会缺字段。
    """
    by_name: dict[str, dict] = {}
    for doc in docs:
        identity = local_document_identity(doc, source_type=source_type)
        by_name[str(doc.get("name") or "")] = identity

    identities: dict[str, dict] = {}
    for node in kg.nodes:
        if node_kind(node.type) != "document":
            continue
        props = node.properties if isinstance(node.properties, dict) else {}
        filename = str((props.get("document_metadata") or {}).get("filename") or "")
        identity = by_name.get(filename)
        if identity is None:
            continue
        apply_identity(props, identity)
        identities[str(node.id)] = identity

    for rel in kg.relationships:
        if str(getattr(rel, "type", "")) != "child":
            continue
        source = getattr(rel, "source", None)
        target = getattr(rel, "target", None)
        identity = identities.get(str(getattr(source, "id", "")))
        if identity is None or target is None or node_kind(target.type) != "chunk":
            continue
        apply_identity(target.properties, identity)

    return summarize_source_types(identity["source_type"] for identity in identities.values())


def _stage_info(class_name: str) -> tuple[str, str]:
    return STAGE_DESC.get(class_name, (class_name, "执行该阶段的处理步骤"))


# 只影响「怎么跑」、不影响「产出什么」的执行控制项：不进断点签名。
# 否则超预算中止后调大预算续跑会被拒，已完成的阶段要重新付费。
SIGNATURE_EXCLUDED_POLICY = frozenset({"token_budget", "max_retries", "llm_timeout", "max_workers"})


def _run_signature(documents: dict[str, dict], doc_ids: list[str], transforms: list,
                   *, llm_id: str | None, emb_id: str | None, min_tokens: int,
                   max_tokens: int, enhance_chunks: bool, mode: str,
                   policy: dict | None = None, legacy: bool = False) -> str:
    """生成断点输入/阶段签名；续跑必须使用同一份输入和 transform 配置。

    `legacy=True` 复现旧口径（整份 policy 进签名），只用于识别升级前写下的断点。
    """
    policy = dict(policy or {})
    if not legacy:
        policy = {k: v for k, v in policy.items() if k not in SIGNATURE_EXCLUDED_POLICY}
    revisions = []
    for doc_id in sorted(doc_ids):
        doc = documents.get(doc_id) or {}
        text_hash = doc.get("text_sha256") or hashlib.sha256(str(doc.get("text", "")).encode("utf-8")).hexdigest()
        revisions.append([doc_id, text_hash])
    stages = []
    for transform in transforms:
        values = {}
        for key in ("min_tokens", "max_tokens", "threshold", "property_name", "relation_type", "name"):
            if hasattr(transform, key):
                value = getattr(transform, key)
                if isinstance(value, (str, int, float, bool, type(None))):
                    values[key] = value
        stages.append({"class": type(transform).__name__, "config": values})
    payload = {
        "version": 1, "mode": mode, "doc_revisions": revisions,
        "llm_preset_id": llm_id, "embedding_preset_id": emb_id,
        "min_tokens": min_tokens, "max_tokens": max_tokens,
        "enhance_chunks": enhance_chunks, "stages": stages,
        "policy": policy,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _make_meta(run_id: str, *, status: str, stage_index: int, total_stages: int,
               doc_ids: list[str], doc_names: list[str], llm_id: str | None,
               emb_id: str | None, name: str, mode: str, source_type: str,
               run_signature: str) -> dict:
    return {
        "run_id": run_id, "name": name, "mode": mode, "source_type": source_type,
        "status": status,
        "stage_index": stage_index, "total_stages": total_stages,
        "doc_ids": doc_ids, "doc_names": doc_names,
        "llm_preset_id": llm_id, "embedding_preset_id": emb_id,
        "signature_version": 1, "run_signature": run_signature,
        "node_count": 0, "relationship_count": 0,
        "created_at": now_iso(), "updated_at": now_iso(),
    }


def _validate_resume_meta(meta: dict, run_signature: str, start_index: int, total_stages: int,
                          legacy_signature: str | None = None) -> None:
    """拒绝缺少或不匹配运行指纹的断点，避免在错误输入上重复追加节点/关系。

    `legacy_signature`：旧口径签名，兼容升级前写下的断点（它们只能用同一份执行参数续跑）。
    """
    if not meta.get("run_signature") or meta.get("signature_version") != 1:
        raise ValueError("断点缺少运行指纹，不能安全续跑。请重新生成。")
    if meta["run_signature"] not in {run_signature, legacy_signature} - {None}:
        raise ValueError("断点输入或生成配置已变化，不能安全续跑。请重新生成。")
    if not 0 <= start_index <= total_stages:
        raise ValueError("断点阶段索引无效，不能继续。请重新生成。")


def _failed_checkpoint_meta(meta: dict, stage_index: int, stop_reason: str = STOP_FAILED) -> dict:
    """标记失败阶段，但保留最后成功快照的节点/边计数。

    ``stop_reason`` 一并落进 meta：失败与超预算都算失败断点，界面据此区分「失败」与「超预算（可续跑）」。
    """
    failed = dict(meta)
    failed.update(status="failed", stage_index=stage_index,
                  stop_reason=stop_reason, updated_at=now_iso())
    return failed


async def build_kg(task: Task, document_ids: list[str], llm_preset: dict, emb_preset: dict,
                   name: str, run_id: str | None = None, resume: bool = False,
                   min_tokens: int = 500, max_tokens: int = 1000,
                   enhance_chunks: bool = True, policy: GenerationPolicy | None = None,
                   source_type: str | None = None) -> dict:
    mode = "ragas" if RAGAS_AVAILABLE else "demo"
    DOCUMENTS = document_snapshot()
    policy = policy or GenerationPolicy()
    from .telemetry import GenerationTelemetry
    tele = getattr(task, "generation_telemetry", None) or GenerationTelemetry(token_budget=policy.token_budget)
    tele.should_stop = task.cancel_event.is_set
    task.generation_telemetry = tele
    task.policy = policy.execution_kwargs()
    task.usage.update(tele.snapshot())
    run_config = None

    # ---------- 解析断点（续跑）：以断点 meta 中的文档为准，而非前端当前列表 ----------
    start_index = 0
    kg = None
    if resume and run_id:
        meta, kg_path = ckpt_load(run_id)
        if meta is None:
            raise ValueError("断点不存在或已被放弃，请刷新页面后重试")
        task.run_id = run_id
        # 续跑读回断点里的来源类型：外部导入建的图续跑后不能被标成本地
        source_type = str(meta.get("source_type") or source_type or SOURCE_LOCAL)
        start_index = meta.get("stage_index", 0)
        document_ids = meta.get("doc_ids") or []
        docs = [DOCUMENTS[i] for i in document_ids if i in DOCUMENTS]
        if not docs:
            raise ValueError("断点关联的文档已被删除，无法继续。请重新上传文档后全新生成。")
        if len(docs) != len(document_ids):
            raise ValueError("断点关联的文档发生变化，不能安全续跑。请重新生成。")
        if mode == "ragas" and kg_path and Path(kg_path).exists():
            from ragas.testset.graph import KnowledgeGraph
            kg = KnowledgeGraph.load(kg_path)
            manager_log(task, f"↻ 从断点继续：已完成 {start_index}/{meta.get('total_stages')} 步，"
                              f"已提取 {len(kg.nodes)} 节点 / {len(kg.relationships)} 关系",
                          {"progress": start_index / (meta.get('total_stages', 1) + 1), "stage": "resume",
                           "stage_index": start_index, "stage_total": meta.get("total_stages", 0),
                           "run_id": run_id,
                           "node_count": len(kg.nodes), "relationship_count": len(kg.relationships),
                           "resumed": True})
        else:
            # 无 KG 快照（demo 模式 / 文件缺失）：退化为全新生成
            start_index = 0
    else:
        docs = [DOCUMENTS[i] for i in document_ids if i in DOCUMENTS]
        if not docs:
            raise ValueError("未找到文档，请先上传")
    doc_names = [d["name"] for d in docs]

    fresh = kg is None
    _doc_list = "、".join(doc_names) if doc_names else ""

    # ---------- 初始化 / 续跑准备 ----------
    if fresh:
        run_id = run_id or new_id("kgrun")
        source_type = source_type or SOURCE_LOCAL
        # 让任务快照带上断点 run_id：SSE 晚订阅也能在取消时选择「取消并删除断点」
        task.run_id = run_id
        doc_ids_for_meta = document_ids
        if mode == "ragas":
            from langchain_core.documents import Document as LCDocument
            from ragas.testset.graph import KnowledgeGraph, Node
            from ragas.testset.transforms import apply_transforms, default_transforms
            from ragas.testset.transforms.splitters import HeadlineSplitter
            kg = KnowledgeGraph(
                nodes=[Node(type="document",
                            properties={"page_content": d["text"],
                                        "document_metadata": {"filename": d["name"]}})
                       for d in docs]
            )
            lc_docs = [LCDocument(page_content=d["text"], metadata={"filename": d["name"]}) for d in docs]
            _llm, _emb, run_config = _build_step1_clients(llm_preset, emb_preset, policy, tele)
            transforms = list(default_transforms(documents=lc_docs, llm=_llm, embedding_model=_emb))
            # 用前端传入的 chunk 参数覆盖默认 HeadlineSplitter（dataclass 字段可直接改）
            for t in transforms:
                if isinstance(t, HeadlineSplitter):
                    t.min_tokens = min_tokens
                    t.max_tokens = max_tokens
            # A′ 增强：追加分块级摘要/向量/语义相似边，补齐 MultiHopAbstract 素材
            if enhance_chunks:
                transforms += _chunk_enhance_transforms(_llm, _emb)
        else:
            transforms = list(DEMO_STAGES)
            kg = _demo_kg(docs)
        total = len(transforms)
        run_signature = _run_signature(
            DOCUMENTS, [d["id"] for d in docs], transforms,
            llm_id=llm_preset.get("id"), emb_id=emb_preset.get("id"),
            min_tokens=min_tokens, max_tokens=max_tokens,
            enhance_chunks=enhance_chunks, mode=mode,
            policy=policy.execution_kwargs(),
        )
        meta = _make_meta(run_id, status="running", stage_index=0, total_stages=total,
                          doc_ids=[d["id"] for d in docs], doc_names=doc_names,
                          llm_id=llm_preset.get("id"), emb_id=emb_preset.get("id"),
                          name=name or f"知识图谱-{now_iso()}", mode=mode,
                          source_type=source_type, run_signature=run_signature)
        meta["node_count"] = len(kg.nodes)
        meta["relationship_count"] = len(kg.relationships)
        await worker_call(task, ckpt_save, run_id, meta, kg)
        manager_log(task, f"▶ 开始生成：共 {total} 个阶段，{len(docs)} 个文档\n   文档清单：{_doc_list}",
                    {"progress": 0.0, "stage": "init", "stage_total": total, "run_id": run_id,
                     "node_count": len(kg.nodes), "relationship_count": len(kg.relationships),
                     "doc_names": doc_names})
    else:
        # 续跑：用当前传入的 preset 重建 transforms（更可靠，可修正之前失效的 key）
        if mode == "ragas":
            from langchain_core.documents import Document as LCDocument
            from ragas.testset.transforms import apply_transforms, default_transforms
            from ragas.testset.transforms.splitters import HeadlineSplitter
            lc_docs = [LCDocument(page_content=d["text"], metadata={"filename": d["name"]}) for d in docs]
            _llm, _emb, run_config = _build_step1_clients(llm_preset, emb_preset, policy, tele)
            transforms = list(default_transforms(documents=lc_docs, llm=_llm, embedding_model=_emb))
            # 续跑时同样应用前端传入的 chunk 参数
            for t in transforms:
                if isinstance(t, HeadlineSplitter):
                    t.min_tokens = min_tokens
                    t.max_tokens = max_tokens
            # A′ 增强：续跑同样追加（若断点发生在增强阶段之前，会从中断处继续）
            if enhance_chunks:
                transforms += _chunk_enhance_transforms(_llm, _emb)
        else:
            transforms = list(DEMO_STAGES)
        total = len(transforms)
        run_signature = _run_signature(
            DOCUMENTS, document_ids, transforms,
            llm_id=llm_preset.get("id"), emb_id=emb_preset.get("id"),
            min_tokens=min_tokens, max_tokens=max_tokens,
            enhance_chunks=enhance_chunks, mode=mode,
            policy=policy.execution_kwargs(),
        )
        legacy_signature = _run_signature(
            DOCUMENTS, document_ids, transforms,
            llm_id=llm_preset.get("id"), emb_id=emb_preset.get("id"),
            min_tokens=min_tokens, max_tokens=max_tokens,
            enhance_chunks=enhance_chunks, mode=mode,
            policy=policy.execution_kwargs(), legacy=True,
        )
        _validate_resume_meta(meta, run_signature, start_index, total, legacy_signature)
        # 修正 total_stages（与重建后的 transforms 对齐）；旧口径断点就地升级为新签名。
        # 走 ckpt_update：锁内重读再写，断点若已被删除不会被这行写活。
        await worker_call(
            task, ckpt_update, run_id,
            lambda m: m.update(total_stages=total, run_signature=run_signature, source_type=source_type),
        )

    # ---------- 阶段化执行（StagedJob）：每个 transform 一个 Stage ----------
    # 断点、取消、账单、终态都由骨架管；这里只描述「一步做什么」与「成功断点写什么」。
    result: dict = {}
    ledger = UsageLedger()
    ledger.update(tele.snapshot())

    def flush_telemetry(ctx) -> None:
        """把遥测缓冲里的调用明细转发成日志（工作线程只写 ctx 缓冲，由主协程 drain）。"""
        snapshot = tele.snapshot()
        ctx.ledger.update(snapshot)
        for message in tele.drain():
            ctx.log(message, **snapshot)

    def counts() -> dict:
        return {"node_count": len(kg.nodes), "relationship_count": len(kg.relationships)}

    def checkpoint_stage(i: int, sname: str):
        def checkpoint(ctx, _value) -> None:
            # 锁内重读 meta + 发布快照，不会覆盖同一断点的其他更新
            ckpt_update(
                run_id,
                lambda m: m.update(status="running", stage_index=i + 1, total_stages=total,
                                   updated_at=now_iso(), **counts()),
                kg,
            )
            ctx.log(f"✓ 断点已保存（第 {i + 1}/{total} 步，{len(kg.nodes)} 节点 / {len(kg.relationships)} 关系）",
                    stage=sname, stage_index=i + 1, stage_total=total,
                    checkpoint=True, **counts())
        return checkpoint

    def ragas_stage(i: int, transform) -> Stage:
        sname, sdesc = _stage_info(type(transform).__name__)

        def run(ctx) -> dict:
            ctx.log(f"▶ 步骤 {i + 1}/{total}：{sname} — {sdesc}\n   处理文档：{_doc_list}",
                    stage=sname, stage_index=i + 1, stage_total=total,
                    doc_names=doc_names, **counts())
            try:
                apply_transforms(kg, [transform], run_config=run_config)
                if tele.budget_exceeded:
                    raise RuntimeError("建图阶段已达到token预算，已停止后续阶段")
                # 短文档会被 ragas 的 Splitter 原样再 append 一次（同 id 两个节点），
                # 在这里立刻合并，避免后续阶段白跑、并杜绝副本之间的 self-loop 边。
                merged = _dedupe_documents(kg)
                if merged:
                    ctx.log(f"🧹 合并重复的文档节点：移除 {merged} 个副本（child 边已重指到保留节点）",
                            stage=sname, **counts())
            finally:
                flush_telemetry(ctx)
                ctx.log("建图用量", **tele.snapshot())
            return counts()

        return Stage(sname, run, checkpoint=checkpoint_stage(i, sname))

    def demo_stage(i: int) -> Stage:
        sname = str(transforms[i])

        async def run(ctx) -> dict:
            ctx.log(f"▶ [演示] 步骤 {i + 1}/{total}：{sname}\n   处理文档：{_doc_list}",
                    stage=sname, stage_index=i + 1, stage_total=total,
                    doc_names=doc_names, **counts())
            await asyncio.sleep(_DEMO_PAUSE_SECONDS)
            return counts()

        return Stage(sname, run, checkpoint=checkpoint_stage(i, sname))

    def resumed(ctx) -> dict:
        return counts()

    stages: list[Stage] = []
    for i in range(total):
        stage = ragas_stage(i, transforms[i]) if mode == "ragas" else demo_stage(i)
        if i < start_index:
            # 已完成的阶段：返回非 NO_RESUME 即跳过 run，也不写断点
            stage = Stage(stage.name, stage.run, checkpoint=stage.checkpoint, resume=resumed)
        stages.append(stage)

    async def persist(ctx) -> None:
        """四种终态一律先落盘：完成写 KG 并清断点，失败/超预算/取消保留可续跑断点。"""
        if ctx.stop_reason == STOP_COMPLETED:
            def _finalize() -> str:
                _dedupe_documents(kg)  # 兜底：旧版本断点里可能残留副本
                # 染色放在「每个 chunk 都有归属」之后只做一次；逐阶段重复写会白跑且覆盖增强成果的风险更高。
                return _color_doc_identity(kg, docs, source_type)

            source_type_summary = await asyncio.to_thread(_finalize)
            kg_data = await asyncio.to_thread(_kg_to_dict, kg)
            kg_id = new_id("kg")
            kg_path = await asyncio.to_thread(kg_write_json, kg_id, kg_data)
            meta_now = ckpt_read_meta(run_id) or {}
            entry = {
                "id": kg_id,
                "name": meta_now.get("name") or name or f"知识图谱-{now_iso()}",
                "kg_path": kg_path,
                "created_at": now_iso(),
                "llm_preset_id": llm_preset.get("id"),
                "embedding_preset_id": emb_preset.get("id"),
                "source_files": doc_names,
                "node_count": len(kg_data["nodes"]),
                "relationship_count": len(kg_data["relationships"]),
                "mode": mode,
                "source_type": source_type_summary or source_type,
            }
            entry["usage"] = ctx.ledger.snapshot() or tele.snapshot()
            await asyncio.to_thread(kg_upsert, entry)
            await asyncio.to_thread(ckpt_delete, run_id)  # 成功后清除断点
            manager_log(task, f"✅ 知识图谱已保存：{entry['name']}（{entry['node_count']} 节点 / {entry['relationship_count']} 关系）",
                        {"progress": 1.0, "stage": "done", "stage_index": total, "stage_total": total,
                         "node_count": entry["node_count"], "relationship_count": entry["relationship_count"]})
            result.update(kg_id=kg_id, node_count=entry["node_count"],
                          relationship_count=entry["relationship_count"], kg_path=kg_path)
        elif ctx.stop_reason == STOP_FAILED:
            # 不分 ragas / 演示模式：失败一律标记失败断点（保留已完成的阶段），便于从最近断点续跑；
            # 部分失败的内存图不覆盖最后成功快照，节点/边计数也继续对应上一个成功快照。
            await asyncio.to_thread(
                ckpt_update, run_id,
                lambda m: _failed_checkpoint_meta(m, int(m.get("stage_index") or 0)))
            manager.log(task, "建图阶段失败，保留最后成功断点", extra=tele.snapshot())
        elif ctx.stop_reason == STOP_BUDGET_EXCEEDED:
            # 超预算同样是失败断点（meta status=failed + stop_reason），断点保留、可续跑
            await asyncio.to_thread(
                ckpt_update, run_id,
                lambda m: _failed_checkpoint_meta(m, int(m.get("stage_index") or 0),
                                                  STOP_BUDGET_EXCEEDED))
            # 任务级可见：status 仍是 failed，另带 stop_reason 供前端显示「超预算（可续跑）」
            task.stop_reason = STOP_BUDGET_EXCEEDED
            manager.log(task, "⏸ 已达到 token 预算：停止后续阶段，已保留最后成功断点，可从断点继续",
                        extra={"stage": ctx.stop_reason, **tele.snapshot()})
        # canceled：与迁移前一致——不落图、不写失败断点，已完成的断点保留

    job = StagedJob(task, stages, policy=policy, persist=persist, ledger=ledger,
                    name=name or "知识图谱生成")
    job.ctx.bind_telemetry(tele)
    try:
        await job.run()
    finally:
        # 终态后把最新用量同步到任务快照（轮询 /api/tasks/{id} 用）
        task.usage.update(tele.snapshot())

    if job.ctx.stop_reason == STOP_BUDGET_EXCEEDED:
        # TaskManager 没有独立的 budget 终态：status 沿用 failed，由 task.stop_reason 细分，
        # 断点已按失败断点落盘，可从断点继续。
        raise RuntimeError("已达到 token 预算，已保留断点，可从断点继续")
    return result


def manager_log(task: Task, msg: str, extra: dict | None = None) -> None:
    manager.log(task, msg, extra=extra)
