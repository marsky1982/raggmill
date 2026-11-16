"""RAGFlow 连接、数据源快照与配置/缓存的本地落盘。"""
from __future__ import annotations

import hashlib
import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .. import crypto
from . import base


# source_id 单段上限（connection_id / dataset_id）与总长上限，见设计 §3.3
_RAGFLOW_SEGMENT_MAX = 64


_RAGFLOW_SOURCE_ID_MAX = 120

# 单段白名单：ASCII 字母数字 + `.` `_` `-`，首字符不含 `.`，长度上限由上方的常量决定。
# 正则只在这里拼出来：`validate_ragflow_name` 与路由的 `Path(pattern=...)`（见 app/schemas.py）
# 用的是同一份规则，改上限只改常量，不会两份悄悄失配。
_SEGMENT_HEAD = r"[A-Za-z0-9_-]"
_SEGMENT_REST = r"[A-Za-z0-9._-]"


def _segment_body(max_len: int) -> str:
    """单段 id 的正则主体：1 个首字符 + 至多 max_len-1 个其余字符。"""
    return rf"{_SEGMENT_HEAD}{_SEGMENT_REST}{{0,{max_len - 1}}}"


RAGFLOW_ID_PATTERN = rf"^{_segment_body(_RAGFLOW_SEGMENT_MAX)}$"


RAGFLOW_SOURCE_ID_PATTERN = rf"^{_segment_body(_RAGFLOW_SOURCE_ID_MAX)}$"


def _ragflow_pointer_paths() -> list[Path]:
    """所有数据源的 current.json 路径：连接式布局 sources/ 与旧布局都算。

    `connections/` 存凭据、不是数据源目录；旧布局的 `{dataset_id}/current.json` 仍在读。
    """
    paths: list[Path] = []
    if base.RAGFLOW_SOURCES_DIR.exists():
        paths.extend(sorted(base.RAGFLOW_SOURCES_DIR.glob("*/current.json")))
    if base.RAGFLOW_DIR.exists():
        for child in sorted(p for p in base.RAGFLOW_DIR.iterdir() if p.is_dir()):
            if child.name not in ("sources", "connections"):
                paths.append(child / "current.json")
    return paths


def _pointer_source_view(path: Path, pointer: dict) -> dict:
    """指针回显给用户的数据源身份（409 / 删除成功都复用它）。

    `connection_name` 来自连接记录，便于用户认出「这是哪个库」；连接记录损坏或历史布局
    没有 connection_id 时返回 null，由前端退回展示 dataset_id。
    """
    connection_id = pointer.get("connection_id")
    connection_name = None
    if connection_id:
        try:
            connection_name = (ragflow_get_connection(str(connection_id)) or {}).get("name")
        except Exception:  # noqa: BLE001 —— 连接记录损坏不该让删除守卫失效
            connection_name = None
    return {
        "source_id": pointer.get("source_id") or path.parent.name,
        "dataset_id": pointer.get("dataset_id"),
        "connection_id": connection_id,
        "connection_name": connection_name,
    }


@base.synchronized
def ragflow_pointers_referencing_kg(kg_id: str) -> list[dict]:
    """当前指针仍以该投影 KG 为准的数据源（新布局 sources/ 与旧布局都查）。

    `kg_delete` 的前置守卫：指针指向它时删掉，下一次同步拿不到旧投影，
    增量复用与 stale 判定都会断链。损坏的指针文件直接跳过，交给各读路径报错。
    """
    refs: list[dict] = []
    for path in _ragflow_pointer_paths():
        try:
            pointer = base._read_json(path, None)
        except base.StorageCorruptionError:
            continue
        if isinstance(pointer, dict) and str(pointer.get("projection_kg_id") or "") == kg_id:
            refs.append(_pointer_source_view(path, pointer))
    return refs


@base.synchronized
def ragflow_detach_kg_pointers(kg_id: str) -> list[dict]:
    """把仍以该 KG 为当前投影的指针置空（`projection_kg_id` 写 null，其余字段原样保留）。

    只给「用户显式确认后删除仍被引用的知识图谱」用：快照文件、source 目录与测试集都不改写，
    指针保留 `snapshot_id` / `path` / `updated_at`，下一次同步因此仍能读到旧快照做增量对比，
    但找不到可复用/可认领的旧投影，会按首次同步口径重建一张新投影（0 LLM token）。

    原子性：先把所有目标指针读进内存，再逐个写；任一写入失败就按写入顺序倒回原值，
    并抛出 `KGPointerResetError`（说明卡在第几个 source / 哪个 source_id），
    调用方据此不会继续删除图谱。回滚本身失败也会写进异常文案，便于人工按原值恢复。
    """
    targets: list[tuple[Path, dict]] = []
    for path in _ragflow_pointer_paths():
        try:
            pointer = base._read_json(path, None)
        except base.StorageCorruptionError:
            continue
        if isinstance(pointer, dict) and str(pointer.get("projection_kg_id") or "") == kg_id:
            targets.append((path, pointer))

    written: list[tuple[Path, dict]] = []
    detached: list[dict] = []
    position = 0
    current_source_id = ""
    try:
        for position, (path, pointer) in enumerate(targets, start=1):
            current_source_id = str(pointer.get("source_id") or path.parent.name)
            original = deepcopy(pointer)  # 必须在改写前留底，rolled back 用的就是这一份
            pointer["projection_kg_id"] = None
            base._write_json(path, pointer)
            written.append((path, original))
            detached.append(_pointer_source_view(path, pointer))
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, original in reversed(written):
            try:
                base._write_json(path, original)
            except Exception as rollback_exc:  # noqa: BLE001 —— 尽力回滚，失败项逐条上报
                rollback_errors.append(f"{path.name} 恢复失败：{rollback_exc}")
        raise base.KGPointerResetError(
            kg_id, position=max(position, 1), source_id=current_source_id,
            detail=str(exc), rollback_errors=rollback_errors,
        ) from exc
    return detached


def validate_ragflow_name(value: Any, what: str = "RAGFlow 知识库 ID", max_len: int = _RAGFLOW_SEGMENT_MAX) -> str:
    """RAGFlow 名称（dataset_id / connection_id）作为单级目录名的公共校验。

    dataset_id 来自用户输入或上游响应，拼进目录名即注入面，所以适配器与存储必须共用这一套：
    白名单 `[A-Za-z0-9._-]`、非空、不以 `.` 开头、不是 `.`/`..`、单段 ≤ 64。
    判定用 `_segment_body(max_len)` 拼出的正则（与路由 `Path(pattern=...)` 同源），
    只在不通过时逐项给出中文原因。
    失败抛 `UnsafePathError`（main.py 统一映射为 HTTP 400）。
    """
    raw = str(value or "")
    if not raw:
        raise base.UnsafePathError(f"{what} 不能为空")
    if re.fullmatch(_segment_body(max_len), raw):
        return raw
    if len(raw) > max_len:
        raise base.UnsafePathError(f"{what} 过长（最多 {max_len} 个字符）")
    if raw.startswith("."):
        raise base.UnsafePathError(f"{what} 不能以 . 开头")
    raise base.UnsafePathError(f"{what} 含不支持的字符（只允许字母、数字、`.`、`_`、`-`）")


def _ragflow_dir(dataset_id: str, create: bool = False) -> Path:
    """旧布局目录（data/ragflow/{dataset_id}/，legacy 兼容只读为主）。
    只有写路径传 create=True，GET 读路径不建目录（避免任意 id 在磁盘上留空目录）。"""
    safe = validate_ragflow_name(dataset_id, "RAGFlow dataset_id")
    d = base._safe_join(base.RAGFLOW_DIR, safe)
    if create:
        (d / "snapshots").mkdir(parents=True, exist_ok=True)
    return d


def ragflow_source_id(connection_id: str, dataset_id: str) -> str:
    """数据源身份 = connection_id + dataset_id（两个实例同 dataset_id 也不再互毁）。"""
    conn = validate_ragflow_name(connection_id, "RAGFlow connection_id")
    ds = validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    sid = f"{conn}__{ds}"
    if len(sid) > _RAGFLOW_SOURCE_ID_MAX:
        raise base.UnsafePathError(f"RAGFlow source_id 过长（connection_id + 知识库 ID 合计最多 {_RAGFLOW_SOURCE_ID_MAX} 个字符）")
    return sid


def ragflow_sync_resources(dataset_id: str, connection_id: str | None) -> tuple[str, ...]:
    """同一数据源的同步 / 探测 / 出题任务共用的互斥资源键（唯一定义处）。

    · 连接式数据源：`{connection_id}__{dataset_id}`；
    · 历史（legacy）数据源：旧端点用过 `ragflow:{dataset_id}`，连接式入口用合成 id 的 source_id，
      两个键都占上，避免同一旧目录被两个入口同时受理。
    出题任务对 RAGFlow 投影也占这组键：生成期间不允许同步切指针、清理旧投影，
    否则正在出题的投影会被当成「未被引用」删除，测试集断链或被增强写回「复活」。
    """
    ds = validate_ragflow_name(dataset_id, "RAGFlow 知识库 ID")
    if connection_id:
        return (ragflow_source_id(connection_id, ds),)
    return (f"ragflow:{ds}", ragflow_source_id(legacy_connection_id(ds), ds))


def ragflow_projection_resources(kg_entry: dict | None) -> tuple[str, ...]:
    """投影 KG 对应的数据源互斥键；非投影 KG 返回空元组。"""
    if not kg_entry or kg_entry.get("mode") != "ragflow-projection" or not kg_entry.get("source_dataset_id"):
        return ()
    return ragflow_sync_resources(str(kg_entry["source_dataset_id"]), kg_entry.get("source_connection_id"))


def ragflow_source_dir(connection_id: str, dataset_id: str, create: bool = False) -> Path:
    """新布局目录：data/ragflow/sources/{connection_id}__{dataset_id}/。"""
    d = base._safe_join(base.RAGFLOW_SOURCES_DIR, ragflow_source_id(connection_id, dataset_id))
    if create:
        (d / "snapshots").mkdir(parents=True, exist_ok=True)
    return d


def ragflow_legacy_source_dir(dataset_id: str) -> Path:
    """旧布局的数据源目录（data/ragflow/{dataset_id}/）。仅重新绑定等维护动作需要直接拿目录，
    常规读路径继续用 `source_dir=None` 走 `_ragflow_source_root`。"""
    return _ragflow_dir(dataset_id)


def legacy_connection_id(dataset_id: str) -> str:
    """旧 config.json 合成的连接 id（确定性：同一 dataset_id 每次启动都拿到同一条）。"""
    return "conn-legacy-" + hashlib.md5(str(dataset_id).encode("utf-8")).hexdigest()[:8]


def _ragflow_source_root(dataset_id: str, source_dir: Path | None, create: bool = False) -> Path:
    """快照根目录：source_dir 为空走旧布局（兼容旧端点/存量脚本），否则用连接专属目录。"""
    if source_dir is None:
        return _ragflow_dir(dataset_id, create)
    root = Path(source_dir)
    if not base._within(base.RAGFLOW_DIR, root):
        raise base.UnsafePathError("RAGFlow 快照目录越界")
    if create:
        (root / "snapshots").mkdir(parents=True, exist_ok=True)
    return root


def ragflow_config_path(dataset_id: str, create: bool = False) -> Path:
    return _ragflow_dir(dataset_id, create) / "config.json"


@base.synchronized
def ragflow_save_config(dataset_id: str, data: dict) -> dict:
    """保存 RAGFlow 连接配置；API token 只以 Fernet 密文落盘。"""
    stored = {
        "dataset_id": dataset_id,
        "base_url": str(data.get("base_url") or "").rstrip("/"),
        "updated_at": base.now_iso(),
    }
    token = data.get("api_key")
    if token:
        stored["api_key_enc"] = crypto.encrypt(token)
        stored["api_key_mask"] = crypto.mask(token)
    else:
        old = base._read_json(ragflow_config_path(dataset_id), {})
        for key in ("api_key_enc", "api_key_mask"):
            if old.get(key):
                stored[key] = old[key]
    base._write_json(ragflow_config_path(dataset_id, create=True), stored)
    view = dict(stored)
    view.pop("api_key_enc", None)
    return view


@base.synchronized
def ragflow_get_config(dataset_id: str) -> dict | None:
    p = ragflow_config_path(dataset_id)
    if not p.exists():
        return None
    data = base._read_json(p, {})
    if not data or base._is_deleted(data):
        return None
    data["api_key"] = crypto.decrypt(data.get("api_key_enc")) if data.get("api_key_enc") else ""
    data.pop("api_key_enc", None)
    return data


def ragflow_settings_path(dataset_id: str, source_dir: Path | None = None, create: bool = False) -> Path:
    """实体规范化 / LLM 消歧开关：按数据源存 settings.json（与快照同目录、同生命周期）。"""
    return _ragflow_source_root(dataset_id, source_dir, create) / "settings.json"


@base.synchronized
def ragflow_settings_read(dataset_id: str, source_dir: Path | None = None) -> dict:
    """读开关配置；文件缺失或损坏按空字典返回（调用方回落默认 disabled），不卡死同步。"""
    p = ragflow_settings_path(dataset_id, source_dir)
    if not p.exists():
        return {}
    try:
        data = base._read_json(p, {})
    except base.StorageCorruptionError:
        logging.warning("RAGFlow 实体消歧配置损坏，按默认 disabled 读取：%s", p.name)
        return {}
    return data if isinstance(data, dict) else {}


@base.synchronized
def ragflow_settings_write(dataset_id: str, data: dict, source_dir: Path | None = None) -> dict:
    base._write_json(ragflow_settings_path(dataset_id, source_dir, create=True), data)
    return data


def ragflow_entity_cache_path(dataset_id: str, source_dir: Path | None = None, create: bool = False) -> Path:
    """LLM 消歧结论缓存：与 settings 同目录，按数据源隔离。"""
    return _ragflow_source_root(dataset_id, source_dir, create) / "entity_cache.json"


@base.synchronized
def ragflow_entity_cache_read(dataset_id: str, source_dir: Path | None = None) -> dict:
    """读缓存条目；缓存是尽力而为的加速层，损坏时按空缓存继续（不让同步失败）。"""
    p = ragflow_entity_cache_path(dataset_id, source_dir)
    if not p.exists():
        return {}
    try:
        data = base._read_json(p, {})
    except base.StorageCorruptionError:
        logging.warning("RAGFlow 实体消歧缓存损坏，按空缓存继续：%s", p.name)
        return {}
    entries = data.get("entries") if isinstance(data, dict) else {}
    return dict(entries) if isinstance(entries, dict) else {}


@base.synchronized
def ragflow_entity_cache_write(
    dataset_id: str,
    entries: dict,
    source_dir: Path | None = None,
    *,
    limit: int = 2000,
    schema_version: int = 1,
) -> None:
    """写缓存，按最近使用时间保留最近 `limit` 条；写失败只告警（缓存非关键路径）。"""
    ordered = sorted(
        entries.items(),
        key=lambda kv: str((kv[1] or {}).get("used_at") or "") if isinstance(kv[1], dict) else "",
        reverse=True,
    )[: max(0, int(limit))]
    payload = {"schema_version": schema_version, "entries": dict(ordered)}
    try:
        base._write_json(ragflow_entity_cache_path(dataset_id, source_dir, create=True), payload)
    except base.StorageCorruptionError:
        logging.warning("RAGFlow 实体消歧缓存文件损坏，本次不写入缓存")


def ragflow_snapshot_path(dataset_id: str, snapshot_id: str, create: bool = False, source_dir: Path | None = None) -> Path:
    return _ragflow_source_root(dataset_id, source_dir, create) / "snapshots" / f"{base.check_id(snapshot_id, 'snapshot_id')}.json"


@base.synchronized
def ragflow_snapshot_write(dataset_id: str, snapshot_id: str, data: dict, source_dir: Path | None = None) -> str:
    """先写不可变快照文件，调用方成功后再更新 current.json 指针。"""
    p = ragflow_snapshot_path(dataset_id, snapshot_id, create=True, source_dir=source_dir)
    base._write_json(p, data)
    return str(p)


@base.synchronized
def ragflow_snapshot_commit(
    dataset_id: str,
    snapshot_id: str,
    path: str,
    projection_kg_id: str,
    projection_version: str,
    source_dir: Path | None = None,
    connection_id: str | None = None,
    source_id: str | None = None,
) -> None:
    current = _ragflow_source_root(dataset_id, source_dir, create=True) / "current.json"
    payload = {
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "path": path,
        "projection_kg_id": projection_kg_id,
        "projection_version": projection_version,
        "updated_at": base.now_iso(),
    }
    # 兼容承诺：只加字段，不改原有键名/语义
    if connection_id:
        payload["connection_id"] = connection_id
    if source_id:
        payload["source_id"] = source_id
    base._write_json(current, payload)  # _write_json 本身即 tmp + os.replace


@base.synchronized
def ragflow_snapshot_pointer_write(dataset_id: str, payload: dict, source_dir: Path | None = None) -> None:
    """原样写回 current.json。只给「重新绑定失败后的回滚」用：常规提交仍走
    `ragflow_snapshot_commit`（会补 updated_at 等字段）。"""
    base._write_json(_ragflow_source_root(dataset_id, source_dir, create=True) / "current.json", payload)


@base.synchronized
def ragflow_snapshot_current(dataset_id: str, source_dir: Path | None = None) -> tuple[dict | None, dict | None]:
    pointer = base._read_json(_ragflow_source_root(dataset_id, source_dir) / "current.json", None)
    if not pointer or base._is_deleted(pointer):
        return None, None
    if pointer.get("path"):
        p = Path(pointer["path"])
    else:
        try:
            p = ragflow_snapshot_path(dataset_id, pointer.get("snapshot_id", ""), source_dir=source_dir)
        except base.UnsafePathError:
            return None, pointer
    if not p.exists():
        return None, pointer
    return base._read_json(p, None), pointer


@base.synchronized
def ragflow_source_snapshot(source_id: str) -> tuple[dict | None, dict | None]:
    """按 source_id（connection_id__dataset_id）读连接式布局的快照与指针。

    source_id 两段都可能含 `_`，不能靠分隔符反解，所以以目录内 current.json
    记录的 dataset_id / path 为准；只是把路径拼装收敛在 storage 层。
    """
    sid = validate_ragflow_name(source_id, "RAGFlow source_id", max_len=_RAGFLOW_SOURCE_ID_MAX)
    root = base._safe_join(base.RAGFLOW_SOURCES_DIR, sid)
    pointer = base._read_json(root / "current.json", None)
    if not pointer or base._is_deleted(pointer):
        return None, None
    if pointer.get("path"):
        path = Path(pointer["path"])
    else:
        dataset_id = str(pointer.get("dataset_id") or "")
        if not dataset_id:
            return None, pointer
        path = ragflow_snapshot_path(dataset_id, pointer.get("snapshot_id", ""), source_dir=root)
    if not path.exists():
        return None, pointer
    return base._read_json(path, None), pointer


def ragflow_connection_path(connection_id: str, create: bool = False) -> Path:
    cid = validate_ragflow_name(connection_id, "RAGFlow connection_id")
    if create:
        base.RAGFLOW_CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return base._safe_join(base.RAGFLOW_CONNECTIONS_DIR, f"{cid}.json")


def _connection_view(data: dict, reveal_key: bool = False) -> dict:
    """对外视图：永不返回密文；reveal_key=True 时解密成 api_key（只给内部拉取用）。"""
    view = {k: v for k, v in data.items() if k != "api_key_enc"}
    if reveal_key:
        view["api_key"] = crypto.decrypt(data.get("api_key_enc")) if data.get("api_key_enc") else ""
    return view


@base.synchronized
def _ragflow_connection_raw(connection_id: str) -> dict | None:
    p = ragflow_connection_path(connection_id)
    if not p.exists():
        return None
    data = base._read_json(p, {})
    if not data or base._is_deleted(data):
        return None
    return data


@base.synchronized
def ragflow_get_connection(connection_id: str, reveal_key: bool = False) -> dict | None:
    data = _ragflow_connection_raw(connection_id)
    return _connection_view(data, reveal_key=reveal_key) if data else None


@base.synchronized
def ragflow_list_connections() -> list[dict]:
    """目录 glob + 跳过 `_deleted`（不引入 index.json，天然没有共享索引互斥问题）。"""
    if not base.RAGFLOW_CONNECTIONS_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(base.RAGFLOW_CONNECTIONS_DIR.glob("*.json")):
        try:
            data = base._read_json(p, {})
        except base.StorageCorruptionError:
            continue
        if not data or base._is_deleted(data):
            continue
        out.append(_connection_view(data))
    return out


@base.synchronized
def ragflow_save_connection(data: dict) -> dict:
    """保存连接；API key 只以 Fernet 密文落盘，返回值不含明文与密文。"""
    cid = validate_ragflow_name(data.get("id") or base.new_id("conn"), "RAGFlow connection_id")
    old = _ragflow_connection_raw(cid) or {}
    stored = {
        "id": cid,
        "name": str(data.get("name") or old.get("name") or ""),
        "base_url": str(data.get("base_url") or old.get("base_url") or "").rstrip("/"),
        "declared_version": data.get("declared_version", old.get("declared_version")),
        "probed_version": data.get("probed_version", old.get("probed_version")),
        "last_test": data.get("last_test", old.get("last_test")),
        "legacy": bool(data.get("legacy", old.get("legacy", False))),
        "created_at": old.get("created_at", base.now_iso()),
        "updated_at": base.now_iso(),
    }
    # legacy 连接保留指向旧布局的 dataset_id（旧目录读路径据此还原）
    if data.get("dataset_id") or old.get("dataset_id"):
        stored["dataset_id"] = data.get("dataset_id") or old.get("dataset_id")
    token = data.get("api_key")
    if token:
        stored["api_key_enc"] = crypto.encrypt(token)
        stored["api_key_mask"] = crypto.mask(token)
    else:  # 更新未传 key：保留旧密文（与预设同一规则）
        for key in ("api_key_enc", "api_key_mask"):
            if old.get(key):
                stored[key] = old[key]
    base._write_json(ragflow_connection_path(cid, create=True), stored)
    return _connection_view(stored)


@base.synchronized
def ragflow_record_connection_test(connection_id: str, probed_version, last_test: dict) -> bool:
    """回写最近一次连接测试结果；连接已删除（或软删）时什么都不写，返回 False。

    旧实现直接调 ragflow_save_connection，测试进行中连接被删掉时会重新写出一条无名连接。
    """
    raw = _ragflow_connection_raw(connection_id)
    if raw is None:
        return False
    raw = dict(raw, probed_version=probed_version, last_test=last_test, updated_at=base.now_iso())
    base._write_json(ragflow_connection_path(connection_id, create=True), raw)
    return True


def _strip_legacy_config_key(dataset_id: str) -> None:
    """删除 legacy 连接时，把旧布局 config.json 里的同一份密钥也抹掉（否则旧 /sync 仍能直接用它）。"""
    try:
        p = ragflow_config_path(dataset_id)
    except base.UnsafePathError:
        return
    if not p.exists():
        return
    data = base._read_json(p, {})
    if not any(k in data for k in ("api_key_enc", "api_key_mask")):
        return
    for key in ("api_key_enc", "api_key_mask"):
        data.pop(key, None)
    data["updated_at"] = base.now_iso()
    base._write_json(p, data)


@base.synchronized
def ragflow_delete_connection(connection_id: str) -> bool:
    """只删凭据、停用拉取；不删 sources/、不删投影、不删测试集。

    N7：① 先把文件改写成**不含任何密文**的墓碑，再尝试物理删除——被沙箱拦截降级为软删时，
    留下的也只是墓碑；② legacy 连接保留墓碑（不物理删），否则下次启动会从旧 config.json 重新合成，
    并同时抹掉旧 config.json 里的密钥。
    """
    p = ragflow_connection_path(connection_id)
    if not p.exists():
        return False
    raw = base._read_json(p, {})
    if not raw or base._is_deleted(raw):
        return False
    tomb = {"id": raw.get("id") or connection_id, "_deleted": True, "deleted_at": base.now_iso(),
            "legacy": bool(raw.get("legacy"))}
    if raw.get("dataset_id"):
        tomb["dataset_id"] = raw["dataset_id"]
    base._write_json(p, tomb)
    if raw.get("legacy"):
        if raw.get("dataset_id"):
            _strip_legacy_config_key(str(raw["dataset_id"]))
        return True
    base._soft_delete(p)
    return True


@base.synchronized
def ragflow_find_connection_by_name(name: str, exclude_id: str | None = None) -> dict | None:
    target = (name or "").strip().casefold()
    if not target:
        return None
    for conn in ragflow_list_connections():
        if conn.get("id") == exclude_id:
            continue
        if (conn.get("name") or "").strip().casefold() == target:
            return conn
    return None


@base.synchronized
def _synthesize_legacy_connections() -> int:
    """扫旧布局 `data/ragflow/{dataset_id}/config.json`，合成 `legacy: true` 连接。

    只读旧文件：不改内容、不改 mtime，也不往旧目录写任何新数据；合成结果落在
    `data/ragflow/connections/`。已经存在同名合成文件（含软删墓碑）时跳过，保证幂等。
    """
    if not base.RAGFLOW_DIR.exists():
        return 0
    created = 0
    for cfg in sorted(base.RAGFLOW_DIR.glob("*/config.json")):
        parent = cfg.parent
        if parent.name in ("connections", "sources"):
            continue
        try:
            old = base._read_json(cfg, {})
        except base.StorageCorruptionError:
            continue
        if not old:
            continue
        dataset_id = str(old.get("dataset_id") or parent.name)
        try:
            validate_ragflow_name(dataset_id, "RAGFlow dataset_id")
            cid = legacy_connection_id(dataset_id)
            target = ragflow_connection_path(cid, create=True)
        except (base.UnsafePathError, OSError):
            continue
        if target.exists():
            continue
        record = {
            "id": cid,
            "name": f"历史配置-{dataset_id}",
            "base_url": str(old.get("base_url") or "").rstrip("/"),
            "declared_version": None,
            "probed_version": None,
            "last_test": None,
            "legacy": True,
            "dataset_id": dataset_id,
            "created_at": old.get("updated_at") or base.now_iso(),
            "updated_at": old.get("updated_at") or base.now_iso(),
        }
        for key in ("api_key_enc", "api_key_mask"):
            if old.get(key):
                record[key] = old[key]
        try:
            base._write_json(target, record)
        except OSError:
            continue
        created += 1
    return created
