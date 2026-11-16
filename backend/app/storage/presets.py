"""模型预设：LLM / llm_testset / embedding 三类预设的落盘与默认项维护。"""
from __future__ import annotations

from pathlib import Path

from .. import crypto
from . import base


def _check_kind(kind: str) -> str:
    if kind not in base.PRESET_KINDS:
        raise base.UnsafePathError(f"kind 必须是 {base.PRESET_KINDS} 之一")
    return kind


def _preset_dir(kind: str) -> Path:
    d = base.PRESETS_DIR / _check_kind(kind)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _preset_file(kind: str, preset_id: str) -> Path:
    return base._safe_join(_preset_dir(kind), f"{base.check_id(preset_id, 'preset_id')}.json")


@base.synchronized
def list_presets(kind: str) -> list[dict]:
    out = []
    for f in sorted(_preset_dir(kind).glob("*.json")):
        try:
            p = base._read_json(f, {})
        except base.StorageCorruptionError:
            continue
        if base._is_deleted(p):
            continue  # 跳过已标记删除的条目
        p.pop("api_key_enc", None)
        out.append(p)
    # 兼容旧数据：如果该 kind 没有任何默认预设，自动将第一个设为默认
    _ensure_has_default(kind, out)
    return out


def _ensure_has_default(kind: str, items: list[dict]) -> None:
    """迁移旧数据：该 kind 无默认时，将列表第一条写盘标记为默认。"""
    if any(p.get("is_default") for p in items):
        return
    if not items:
        return
    target = items[0]
    f = _preset_dir(kind) / f"{target['id']}.json"
    if f.exists():
        # 走 _read_json 而不是 json.loads：坏文件要按损坏检测报错，不能静默当空对象覆盖
        d = base._read_json(f, {})
        d["is_default"] = True
        base._write_json(f, d)
        target["is_default"] = True


@base.synchronized
def get_preset(preset_id: str) -> dict | None:
    for kind in base.PRESET_KINDS:
        f = _preset_file(kind, preset_id)
        if f.exists():
            data = base._read_json(f, {})
            if base._is_deleted(data):
                continue  # 已删除的视为不存在
            return data
    return None


def generation_presets(llm_preset_id: str, embedding_preset_id: str) -> tuple[dict, dict]:
    """取一对生成用预设并校验类型：LLM 位置只收 LLM 类预设，Embedding 位置只收 embedding。

    旧实现只查存在性，可以把 embedding 预设当 LLM 传入，到调用供应商时才报晦涩错误。
    不存在或类型不符时抛 ValueError（中文原因，路由转 400）。
    """
    llm = get_preset(llm_preset_id) if llm_preset_id else None
    emb = get_preset(embedding_preset_id) if embedding_preset_id else None
    if not llm or not emb:
        raise ValueError("LLM / Embedding 预设不存在，请先到「模型配置」创建")
    if llm.get("kind") not in base.LLM_PRESET_KINDS:
        raise ValueError(f"「{llm.get('name') or llm_preset_id}」不是 LLM 预设，请在 LLM 位置选择 LLM 类预设")
    if emb.get("kind") not in base.EMBEDDING_PRESET_KINDS:
        raise ValueError(f"「{emb.get('name') or embedding_preset_id}」不是 Embedding 预设，请在 Embedding 位置选择 Embedding 预设")
    return llm, emb


@base.synchronized
def save_preset(data: dict) -> dict:
    kind = data["kind"]
    pid = data.get("id") or base.new_id(f"preset-{kind}")
    is_new = not data.get("id")
    old_kind = None
    old_file = None
    old = {}
    if not is_new:
        for candidate_kind in base.PRESET_KINDS:
            candidate_file = _preset_file(candidate_kind, pid)
            if candidate_file.exists():
                old_kind = candidate_kind
                old_file = candidate_file
                old = base._read_json(candidate_file, {})
                break
    # ⚠ 已知隐患（只记录，不改逻辑）：stored 是固定 key 字典，不保留 _deleted；
    # 客户端若带已软删的旧 id 再 POST 一次，会把那条预设"复活"。修复方案另行跟踪。
    stored = {
        "id": pid,
        "kind": kind,
        "name": data["name"],
        "provider": data.get("provider", "openai_compatible"),
        "base_url": data.get("base_url", ""),
        "model": data.get("model", ""),
        "extra": data.get("extra") or {},
        "created_at": old.get("created_at", base.now_iso()),
        "revision": int(old.get("revision", 0)) + 1 if old else 1,
        "is_default": data["is_default"] if "is_default" in data else old.get("is_default", False),
    }
    # 新建时：如果该 kind 还没有默认预设，自动设为默认
    if is_new and not stored["is_default"]:
        existing = list_presets(kind)
        if not any(p.get("is_default") for p in existing):
            stored["is_default"] = True
    if data.get("api_key"):
        stored["api_key_enc"] = crypto.encrypt(data["api_key"])
        stored["api_key_mask"] = crypto.mask(data["api_key"])
    else:  # 更新未传 key：保留旧密文
        if old.get("api_key_enc"):
            stored["api_key_enc"] = old["api_key_enc"]
            stored["api_key_mask"] = old.get("api_key_mask", "****")
    target_file = _preset_file(kind, pid)
    base._write_json(target_file, stored)
    # kind 迁移先写入新位置，再清理旧位置，避免中途失败丢失可用预设。
    if old_file is not None and old_file != target_file:
        base._soft_delete(old_file)
    view = dict(stored)
    view.pop("api_key_enc", None)
    return view


@base.synchronized
def set_default_preset(preset_id: str, kind: str) -> dict | None:
    """将指定预设设为该 kind 的默认，同 kind 其他预设取消默认（含已删除的）。"""
    target = get_preset(preset_id)
    if not target or target["kind"] != kind:
        return None
    # 取消同 kind 所有其他预设的默认标记（遍历磁盘文件，不经过 list_presets 过滤）
    for f in _preset_dir(kind).glob("*.json"):
        # 坏文件交给损坏检测（StorageCorruptionError），不静默跳过
        d = base._read_json(f, {})
        if d.get("id") != preset_id and d.get("is_default"):
            d["is_default"] = False
            base._write_json(f, d)
    # 设为目标为默认
    f = _preset_file(kind, preset_id)
    d = base._read_json(f, {})
    d["is_default"] = True
    base._write_json(f, d)
    d.pop("api_key_enc", None)
    return d


@base.synchronized
def delete_preset(preset_id: str) -> bool:
    for kind in base.PRESET_KINDS:
        f = _preset_file(kind, preset_id)
        if f.exists():
            base._soft_delete(f)
            return True
    return False


def preset_plain_key(preset: dict) -> str:
    enc = preset.get("api_key_enc")
    return crypto.decrypt(enc) if enc else ""


def find_preset_by_name(kind: str, name: str) -> dict | None:
    """同名同 kind 查重：新建前拦同名副本，避免存量预设难以区分。

    只用于 POST 新建；PUT 编辑不做查重——历史遗留的同名副本仍允许正常编辑。
    """
    target = (name or "").strip().casefold()
    if not target:
        return None
    for preset in list_presets(kind):
        if (preset.get("name") or "").strip().casefold() == target:
            return preset
    return None


class PresetNameConflict(ValueError):
    """新建预设撞到同 kind 同名副本；路由转 409。"""

    def __init__(self, existing: dict):
        self.existing = existing
        super().__init__(
            f"同 kind 下已存在同名预设「{existing.get('name')}」（id={existing.get('id')}），"
            "请改名或直接编辑该预设")


@base.synchronized
def create_preset_if_absent(data: dict) -> dict:
    """事务式新建预设：查重与写入在同一把锁内，堵住「查重通过 → 别人插入同名 → 再写入」的窗口。

    同名同 kind 已存在时抛 PresetNameConflict（文案与旧路由一致）。
    """
    duplicate = find_preset_by_name(data["kind"], data.get("name", ""))
    if duplicate:
        raise PresetNameConflict(duplicate)
    return save_preset(data)
