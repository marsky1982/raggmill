"""知识图谱索引与生成断点（不可变快照 + meta 指针）。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from . import base
from . import ragflow


def ckpt_kg_path(run_id: str) -> Path:
    return base._safe_join(base.KG_CKPT_DIR, f"{base.check_id(run_id, 'run_id')}.json")


def ckpt_meta_path(run_id: str) -> Path:
    return base._safe_join(base.KG_CKPT_DIR, f"{base.check_id(run_id, 'run_id')}.meta.json")


@base.synchronized
def ckpt_write_meta(run_id: str, meta: dict) -> None:
    base.KG_CKPT_DIR.mkdir(parents=True, exist_ok=True)
    base._write_json(ckpt_meta_path(run_id), meta)


@base.synchronized
def ckpt_read_meta(run_id: str) -> dict | None:
    p = ckpt_meta_path(run_id)
    if not p.exists():
        return None
    meta = base._read_json(p, None)
    # 已被放弃（软删除）的断点视为不存在，防止重复 DELETE 或 resume 已删断点
    if base._is_deleted(meta):
        return None
    return meta


@base.synchronized
def ckpt_save(run_id: str, meta: dict, kg: Any = None) -> None:
    """不可变阶段快照 + 单一meta指针；发布meta失败不会覆盖旧阶段的图。"""
    base.KG_CKPT_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = ckpt_meta_path(run_id)
    if kg is not None and hasattr(kg, "save"):
        snapshot_name = f"{base.check_id(run_id)}.snapshot-{uuid.uuid4().hex}.json"
        kg_path = base._safe_join(base.KG_CKPT_DIR, snapshot_name)
        tmp = kg_path.with_name(f".{kg_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            kg.save(tmp)
            os.replace(tmp, kg_path)
        finally:
            tmp.unlink(missing_ok=True)
        meta = dict(meta, snapshot_file=snapshot_name,
                    snapshot_hash=hashlib.sha256(kg_path.read_bytes()).hexdigest())
    base._write_json(meta_path, meta)


@base.synchronized
def ckpt_update(run_id: str, mutate, kg: Any = None) -> dict | None:
    """断点 meta 的事务式更新：锁内重读 → mutate(meta) → 写回（可同时发布新快照）。

    断点已被删除/放弃 → 不调 mutate、不写盘、返回 None（不把已放弃的断点写活）。
    `mutate(meta)` 可就地修改并返回 None，也可返回新的 dict；返回 False 表示放弃本次写入。
    """
    meta = ckpt_read_meta(run_id)
    if meta is None:
        return None
    result = mutate(meta)
    if result is False:
        return None
    if isinstance(result, dict):
        meta = result
    ckpt_save(run_id, meta, kg)
    return meta


@base.synchronized
def ckpt_load(run_id: str) -> tuple[dict | None, Any]:
    """返回 (meta, kg_or_None)。kg 需要调用方用 KnowledgeGraph.load 还原（避免 storage 强依赖 ragas）。"""
    meta = ckpt_read_meta(run_id)
    if meta is None:
        return None, None
    if meta.get("snapshot_file"):
        snapshot = meta["snapshot_file"]
        if Path(snapshot).name != snapshot or not snapshot.startswith(f"{base.check_id(run_id)}.snapshot-"):
            raise base.UnsafePathError("断点快照名称不合法")
        kg_path = base._safe_join(base.KG_CKPT_DIR, snapshot)
        if not kg_path.exists() or hashlib.sha256(kg_path.read_bytes()).hexdigest() != meta.get("snapshot_hash"):
            raise base.StorageCorruptionError("断点快照缺失或校验失败，不能继续")
    else:
        kg_path = ckpt_kg_path(run_id)  # 兼容旧断点读取
    return meta, (kg_path if kg_path.exists() else None)


@base.synchronized
def ckpt_list() -> list[dict]:
    """列出可恢复的断点（运行中或失败，未完成，未被放弃）。供前端「从断点继续」使用。"""
    if not base.KG_CKPT_DIR.exists():
        return []
    out = []
    for f in sorted(base.KG_CKPT_DIR.glob("*.meta.json")):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        # 过滤：被放弃（_deleted）、已完成
        if base._is_deleted(meta):
            continue
        status = meta.get("status")
        total = meta.get("total_stages") or 0
        idx = meta.get("stage_index") or 0
        # 可恢复：未完成态，且还有剩余阶段
        if status in ("running", "failed") and idx < total:
            out.append(meta)
    out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return out


@base.synchronized
def ckpt_delete(run_id: str) -> bool:
    # meta 已被软删除（_deleted 标记）→ 视为不存在，返回 False（DELETE 幂等 404）
    meta = ckpt_read_meta(run_id)
    if meta is None and ckpt_meta_path(run_id).exists():
        return False
    removed = False
    for p in (ckpt_meta_path(run_id), ckpt_kg_path(run_id), *base.KG_CKPT_DIR.glob(f"{base.check_id(run_id)}.snapshot-*.json")):
        if p.exists():
            base._soft_delete(p)
            removed = True
    return removed


@base.synchronized
def kg_list() -> list[dict]:
    return base._read_json(base.KG_INDEX, [])


@base.synchronized
def kg_upsert(entry: dict) -> None:
    items = kg_list()
    items = [x for x in items if x["id"] != entry["id"]] + [entry]
    base._write_json(base.KG_INDEX, items)


@base.synchronized
def kg_get(kg_id: str) -> dict | None:
    return next((x for x in kg_list() if x["id"] == kg_id), None)


@base.synchronized
def kg_delete(kg_id: str, detach_sources: bool = False) -> bool:
    entry = kg_get(kg_id)
    if not entry:
        return False
    # 指针仍指向它时不能删：下一次同步要拿旧投影做增量复用 / stale 判定，删掉就断链。
    # 用户显式确认（detach_sources=True）时先重置指针再删，顺序保证不会留下「活指针指向已删图」。
    refs = ragflow.ragflow_pointers_referencing_kg(kg_id)
    if refs and not detach_sources:
        raise base.KGReferencedError(kg_id, refs)
    if refs:
        detached = ragflow.ragflow_detach_kg_pointers(kg_id)
        logging.warning(
            "按用户确认删除知识图谱 %s，已重置数据源指针（下次同步按首次同步重建投影）: %s",
            kg_id,
            "、".join(str(item.get("source_id") or item.get("dataset_id")) for item in detached) or "无",
        )
    p = Path(entry["kg_path"])
    if p.exists() and base._within(base.KG_DIR, p):  # 索引里的路径越界则只删索引
        base._soft_delete(p)
    base._write_json(base.KG_INDEX, [x for x in kg_list() if x["id"] != kg_id])
    return True


@base.synchronized
def kg_read_json(kg_id: str) -> dict | None:
    entry = kg_get(kg_id)
    if not entry:
        return None
    p = Path(entry["kg_path"])
    if not p.exists():
        return None
    data = base._read_json(p, None)
    return None if base._is_deleted(data) else data


@base.synchronized
def kg_file_exists(kg_id: str) -> bool:
    """KG 文件是否还在：索引条目可能已丢，指针里残留旧 id 时要靠它判定是否彻底失效。"""
    try:
        p = base._safe_join(base.KG_DIR, f"{base.check_id(kg_id, 'kg_id')}.json")
    except base.UnsafePathError:
        return False
    return p.exists()


@base.synchronized
def kg_write_json(kg_id: str, data: dict) -> str:
    p = base._safe_join(base.KG_DIR, f"{base.check_id(kg_id, 'kg_id')}.json")
    base._write_json(p, data, indent=None, default=str)
    return str(p)


@base.synchronized
def kg_update(kg_id: str, mutate, *, data: dict | None = None) -> dict | None:
    """KG 条目的事务式更新：锁内重读条目 → mutate(entry) → 写回索引（可同时写投影原文）。

    条目已不存在 → 不调 mutate、不写盘、返回 None（不复活已删除的图谱）。
    `data` 非 None 时在同一把锁内与索引一起落盘，避免「读到条目存在 → 写文件 →
    索引被别人改写」之间互相覆盖。**先 mutate 再写盘**：mutate 抛错时磁盘上什么都没动，
    不会留下「JSON 已更新、索引还是旧的」半状态。
    """
    entry = kg_get(kg_id)
    if not entry:
        return None
    mutate(entry)
    if data is not None:
        entry["kg_path"] = kg_write_json(kg_id, data)
    kg_upsert(entry)
    return entry
