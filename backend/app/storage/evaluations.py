"""评估结果的落盘（与测试集同构的 JSON 文件）。"""
from __future__ import annotations

from pathlib import Path

from . import base


@base.synchronized
def evaluation_write(evaluation_id: str, data: dict) -> str:
    base.EVALUATION_DIR.mkdir(parents=True, exist_ok=True)
    p = base._safe_join(base.EVALUATION_DIR, f"{base.check_id(evaluation_id, 'evaluation_id')}.json")
    base._write_json(p, data)
    return str(p)


@base.synchronized
def evaluation_read(evaluation_id: str) -> dict | None:
    p = base._safe_join(base.EVALUATION_DIR, f"{base.check_id(evaluation_id, 'evaluation_id')}.json")
    if not p.exists():
        return None
    # 旧版本的评估制品可能把 ragas 的 NaN 哨兵值原样落盘，读时兜底保证接口可返回。
    item = base.sanitize_nonfinite(base._read_json(p, None))
    return None if base._is_deleted(item) else item


@base.synchronized
def evaluation_delete(evaluation_id: str) -> bool:
    """删除评估制品：物理删除优先，被拦截时写 `_deleted`，读路径统一过滤。"""
    p = base._safe_join(base.EVALUATION_DIR, f"{base.check_id(evaluation_id, 'evaluation_id')}.json")
    if not p.exists():
        return False
    if base._is_deleted(base._read_json(p, None)):
        return False
    base._soft_delete(p)
    return True


@base.synchronized
def evaluation_list() -> list[dict]:
    if not base.EVALUATION_DIR.exists():
        return []
    out = []
    for p in sorted(base.EVALUATION_DIR.glob("*.json")):
        try:
            item = base._read_json(p, None)
        except base.StorageCorruptionError:
            continue
        if item and not base._is_deleted(item):
            out.append({k: item[k] for k in ("id", "name", "created_at", "status", "metric_keys", "sample_count") if k in item})
    return list(reversed(out))
