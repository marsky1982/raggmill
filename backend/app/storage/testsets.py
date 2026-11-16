"""测试集索引与数据文件读写。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import base


@base.synchronized
def ts_list() -> list[dict]:
    return base._read_json(base.TS_INDEX, [])


@base.synchronized
def ts_upsert(entry: dict) -> None:
    items = ts_list()
    items = [x for x in items if x["id"] != entry["id"]] + [entry]
    base._write_json(base.TS_INDEX, items)


@base.synchronized
def ts_get(ts_id: str) -> dict | None:
    return next((x for x in ts_list() if x["id"] == ts_id), None)


@base.synchronized
def ts_delete(ts_id: str) -> bool:
    entry = ts_get(ts_id)
    if not entry:
        return False
    p = Path(entry["path"])
    if p.exists() and base._within(base.TESTSET_DIR, p):  # 索引里的路径越界则只删索引
        base._soft_delete(p)
    base._write_json(base.TS_INDEX, [x for x in ts_list() if x["id"] != ts_id])
    return True


@base.synchronized
def ts_read(ts_id: str) -> dict | None:
    entry = ts_get(ts_id)
    if not entry:
        return None
    p = Path(entry["path"])
    if not p.exists():
        return None
    data = base._read_json(p, None)
    return None if base._is_deleted(data) else data


@base.synchronized
def ts_write(ts_id: str, data: dict) -> str:
    base.TESTSET_DIR.mkdir(parents=True, exist_ok=True)
    p = base._safe_join(base.TESTSET_DIR, f"{base.check_id(ts_id, 'testset_id')}.json")
    base._write_json(p, data)
    return str(p)


@base.synchronized
def ts_update(ts_id: str, mutate):
    """在存储锁内「重读 → 修改 → 写回文件与索引」，供后台派生动作（stale 置位 / 回收）使用。

    分开调 ts_read / ts_write / ts_upsert 时，读与写之间若有人删除测试集，写回会让它「复活」
    （文件与索引都被重新写出）；同时点的「采纳」也会被旧副本覆盖。这里整段持锁：
    测试集已不存在 → 不调 mutate、返回 None；`mutate(data)` 返回假值 → 不写盘、返回 None；
    返回真值 → 写文件并按同一口径刷新索引条目，再把该值原样返回。
    """
    data = ts_read(ts_id)
    if not data:
        return None
    result = mutate(data)
    if not result:
        return None
    path = ts_write(ts_id, data)
    entry = {k: v for k, v in data.items() if k != "samples"}
    entry["path"] = path
    entry["count"] = len(data.get("samples") or [])
    ts_upsert(entry)
    return result


@base.synchronized
def ts_patch_sample(ts_id: str, index: int, adopted: bool) -> bool:
    data = ts_read(ts_id)
    if data is None:
        return False
    for sample in data.get("samples", []):
        if sample["index"] == index:
            sample["adopted"] = adopted
            ts_write(ts_id, data)
            return True
    return False
