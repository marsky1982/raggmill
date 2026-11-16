"""存储层基础原语与共享状态。

锁、原子写、路径校验、事务式读改写原语、删除三件套（物理删除优先 + 软删兜底 + 启动清理）、
启动初始化，以及所有路径常量与内存态索引的唯一定义处。

子模块一律用 `base.xxx` 访问这里的名字（不要 `from .base import xxx`），
否则测试把 monkeypatch 打在 `storage.base.xxx` 上对子模块不生效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from ..config import (
    DATA_DIR,
    EVALUATION_DIR,
    KG_DIR,
    PRESETS_DIR,
    RAGFLOW_DIR,
    TESTSET_DIR,
    UPLOADS_DIR,
    ensure_data_dirs,
)
from ..jsonutil import sanitize_nonfinite


# 预设类型的唯一定义处（路由的校验、列表都引用这里）
LLM_PRESET_KINDS = ("llm", "llm_testset")


EMBEDDING_PRESET_KINDS = ("embedding",)


PRESET_KINDS = LLM_PRESET_KINDS + EMBEDDING_PRESET_KINDS


# LLM 预设 extra.reasoning_effort 的可选值（唯一定义处；前端 ConfigPage 的下拉选项由一致性测试守护）。
# 留空 = 不发送，沿用模型默认。部分带思考链的模型默认思考很长（20 字回答 700+ token、
# 10 秒以上），minimal 时 1 秒、十几个 token（2026-11-12 实测）；长 prompt 开着思考容易超过网关 60 秒空闲超时。
# none：部分推理模型只有在 reasoning_effort=none 时才接受 temperature（ragas 每次都带），
# 且不支持 minimal（2026-11-12 实测）。某推理模型 的 none / minimal 都可用。
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")


_LOCK = threading.RLock()


_CORRUPT: dict[str, str] = {}


class StorageCorruptionError(RuntimeError):
    """阻断受损资源写入，不把坏索引默认为空后覆盖。"""


def _source_label(src: dict) -> str:
    """数据源的人类可读标签：优先「连接名/知识库 ID」，缺连接名时退回 source_id。"""
    dataset_id = str(src.get("dataset_id") or "")
    connection_name = str(src.get("connection_name") or "")
    if connection_name and dataset_id:
        return f"{connection_name}/{dataset_id}"
    return str(src.get("source_id") or dataset_id or "未知数据源")


class KGReferencedError(RuntimeError):
    """KG 仍是 RAGFlow 数据源的当前投影：删掉会让该数据源的同步断链。"""

    def __init__(self, kg_id: str, sources: list[dict]):
        self.kg_id = kg_id
        self.sources = sources
        names = "、".join(_source_label(s) for s in sources[:3])
        more = f" 等 {len(sources)} 个数据源" if len(sources) > 3 else ""
        super().__init__(
            f"知识图谱 {kg_id} 仍是数据源 {names}{more} 的当前投影，删除会让该数据源的同步断链；"
            "请先重新同步切换到新投影，再删除旧图谱"
        )


class KGPointerResetError(RuntimeError):
    """按用户确认删除图谱时重置数据源指针失败：已回滚已改的指针，图谱本身未删除。"""

    def __init__(self, kg_id: str, *, position: int, source_id: str, detail: str,
                 rollback_errors: list[str]):
        self.kg_id = kg_id
        self.position = position
        self.source_id = source_id
        self.rollback_errors = rollback_errors
        message = f"重置数据源 {source_id}（第 {position} 个）的投影指针失败，知识图谱 {kg_id} 未删除：{detail}"
        if rollback_errors:
            message += "；回滚未完成：" + "；".join(rollback_errors)
        else:
            message += "；此前已重置的指针已恢复原值"
        super().__init__(message)


def synchronized(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return guarded


def storage_health() -> dict:
    with _LOCK:
        return {"degraded": bool(_CORRUPT), "corrupt_files": [Path(p).name for p in _CORRUPT]}


def document_snapshot() -> dict:
    with _LOCK:
        return deepcopy(DOCUMENTS)


# 拼进文件名的 id（preset_id / run_id / kg_id / ts_id / snapshot_id）只允许单级安全名，
# 防止 ../ 或 Windows 下 %5C 解码出的 \ 被当成路径分隔符。
# 正则字符串导出给路由的 `Path(pattern=...)` 共用，名字只定义一次（见 app/schemas.py）。
SAFE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


_SAFE_ID = re.compile(SAFE_ID_PATTERN)


class UnsafePathError(ValueError):
    """非法 id / 路径越界；main.py 统一映射为 HTTP 400。"""


def check_id(value: Any, what: str = "id") -> str:
    s = str(value or "")
    if not _SAFE_ID.match(s):
        raise UnsafePathError(f"非法 {what}: {s!r}（只允许字母、数字、_ 和 -）")
    return s


def _within(base: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def _safe_join(base: Path, *parts: str) -> Path:
    """base / parts，并校验结果仍在 base 之内。"""
    p = base.joinpath(*parts)
    if not _within(base, p):
        raise UnsafePathError(f"路径越界: {p}")
    return p


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def stable_doc_id(name: str) -> str:
    """基于文件名生成确定性 doc id（同一文件每次启动都拿到同一 id）。"""
    return f"doc-{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}"


@synchronized
def _read_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            key = str(path.resolve())
            if key not in _CORRUPT:
                backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}")
                backup.write_bytes(path.read_bytes())
                _CORRUPT[key] = str(backup)
                logging.error("存储文件损坏，已保留原文件并备份：%s", path.name)
            raise StorageCorruptionError(f"存储文件 {path.name} 损坏，已阻止写入，请恢复备份后重试") from exc
        _CORRUPT.pop(str(path.resolve()), None)
        return value
    return default


@synchronized
def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace，崩溃时目标文件要么是旧版要么是新版，不会半截。
    临时文件以 . 开头、.tmp 结尾，不会被 *.json 的 glob 扫到。"""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        if str(path.resolve()) in _CORRUPT:
            raise StorageCorruptionError(f"存储文件 {path.name} 尚未恢复，禁止覆盖")
        with tmp.open("w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@synchronized
def _write_json(path: Path, data: Any, *, indent: int | None = 2, default: Any = None) -> None:
    # 即使调用方没有先读取，也不能覆盖既有坏JSON。
    if path.exists():
        _read_json(path, None)
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent, default=default))


class OrderedWriteStep(NamedTuple):
    label: str                      # 中文步骤名，会进回滚失败明细
    apply: Callable[[], None]
    undo: Callable[[], None]


class OrderedWriteError(ValueError):
    """按序写的某一步失败。`attempted` 是已经执行（含失败那一步）的步数。"""

    def __init__(self, cause: BaseException, attempted: int, rollback_errors: list[str]):
        self.cause = cause
        self.attempted = attempted
        self.rollback_errors = rollback_errors
        super().__init__(str(cause))


def ordered_write(steps: Sequence[OrderedWriteStep]) -> None:
    """按顺序执行各步；某步抛错时对已执行的步按逆序回滚，再抛 OrderedWriteError。

    步进列表在调用 `apply()` **之前**登记，所以中途失败的那一步自己也会被 undo（写了一半的
    文件能回到原样）；因此各步的 `undo` 必须可重复执行、对「apply 没跑完」也成立。
    回滚本身失败不吞异常：明细进 `rollback_errors`，由调用方决定文案与是否需要人工按备份恢复。
    """
    done: list[OrderedWriteStep] = []
    with _LOCK:
        try:
            for step in steps:
                done.append(step)
                step.apply()
        except Exception as exc:  # noqa: BLE001 —— 任何一步失败都回滚，原异常包进 OrderedWriteError
            errors: list[str] = []
            for step in reversed(done):
                try:
                    step.undo()
                except Exception as undo_exc:  # noqa: BLE001
                    errors.append(f"{step.label}：{undo_exc}")
            raise OrderedWriteError(exc, len(done), errors) from exc


KG_INDEX = KG_DIR / "index.json"


KG_CKPT_DIR = KG_DIR / "checkpoints"  # 生成断点（meta + KnowledgeGraph JSON）


TS_INDEX = TESTSET_DIR / "index.json"


RAGFLOW_CONNECTIONS_DIR = RAGFLOW_DIR / "connections"


RAGFLOW_SOURCES_DIR = RAGFLOW_DIR / "sources"


DOC_INDEX_DIR = DATA_DIR / "documents"


DOC_INDEX = DOC_INDEX_DIR / "index.json"


DOCUMENTS: dict[str, dict] = {}


def _is_deleted(obj: Any) -> bool:
    """软删标记的唯一判断口径（JSON 里的 `_deleted: true`）。"""
    return bool(isinstance(obj, dict) and obj.get("_deleted"))


def _soft_delete(path: Path) -> None:
    """统一删除入口：物理删除优先，被沙箱拦截（OSError）时按文件类型降级软删。

    - `documents/texts/` 的正文制品：清空内容（未被索引引用的制品没有任何读路径，清空即不再泄露原文）；
    - `uploads/` 下的原始上传文件：登记 `.trashed.json` 墓碑，重启扫描不会复活；
    - 其余 JSON：改写内容写 `_deleted: true`，读路径一律用 `_is_deleted()` 过滤。
    """
    if not path.exists():
        return
    try:
        path.unlink()
        return
    except OSError:
        pass
    if _within(DOC_INDEX.parent / "texts", path):
        try:
            if path.stat().st_size:
                path.write_bytes(b"")
        except OSError:
            pass
        return
    if _within(UPLOADS_DIR, path):
        names = _trashed_names()
        names.add(path.name)
        _set_trashed(names)
        return
    # 沙箱拦截兜底：软删除标记
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["_deleted"] = True
    try:
        _write_json(path, data)
    except Exception as e:
        # 写入失败时静默记录，不抛异常（避免 500）
        logging.warning(f"标记 _deleted 失败 {path.name}: {e}")


@synchronized
def _purge() -> None:
    """启动时物理清理历史软删除残留（脱离沙箱启动时生效；仍被拦截则保留标记，下次再试）。

    覆盖：预设 / 上传文件墓碑 / KG / 断点（meta + 旧图 + 不可变快照）/ 测试集 / 评估 /
    RAGFlow 连接墓碑与数据源快照。legacy 连接的墓碑必须保留，否则下次启动会从旧 config.json 重新合成。
    """
    # 惰性 import：子模块在模块级 import base，这里模块级反过来引会成环
    from . import presets

    candidates: list[Path] = []

    def _collect_json(d: Path, *, skip: set[str] = frozenset(),
                      ignore_suffix: tuple[str, ...] = (), keep_legacy: bool = False) -> None:
        if not d.exists():
            return
        for f in d.glob("*.json"):
            if f.name in skip or f.name.endswith(ignore_suffix):
                continue
            # 断点 / RAGFlow 快照动辄数 MB：先探首尾字节有没有 "_deleted"，没有就不解析
            try:
                size = f.stat().st_size
                with f.open("rb") as fh:
                    probe = fh.read(512)
                    if size > 4096:
                        fh.seek(max(0, size - 4096))
                    probe += fh.read()
            except OSError:
                continue
            if b'"_deleted"' not in probe:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not _is_deleted(data):
                continue
            if keep_legacy and data.get("legacy"):
                continue
            candidates.append(f)

    # 断点：meta 带 _deleted 的，连带旧图与全部不可变快照
    if KG_CKPT_DIR.exists():
        for f in KG_CKPT_DIR.glob("*.meta.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not _is_deleted(m):
                continue
            run_id = f.name[:-len(".meta.json")]
            candidates.append(f)
            kgp = f.with_name(f"{run_id}.json")  # 由磁盘文件名推出，不走 check_id
            if kgp.exists():
                candidates.append(kgp)
            candidates.extend(KG_CKPT_DIR.glob(f"{run_id}.snapshot-*.json"))
        # meta 已物理删除、只剩带标记的快照：按文件内容兜底（meta 已在上面收过）
        _collect_json(KG_CKPT_DIR, ignore_suffix=(".meta.json",))
    # KG / 测试集 / 评估数据文件（index.json 本身不含 _deleted，跳过）
    _collect_json(KG_DIR, skip={"index.json"})
    _collect_json(TESTSET_DIR, skip={"index.json"})
    _collect_json(EVALUATION_DIR)
    # RAGFlow：连接墓碑（legacy 保留）与数据源 current.json / snapshots/*.json
    _collect_json(RAGFLOW_CONNECTIONS_DIR, keep_legacy=True)
    if RAGFLOW_SOURCES_DIR.exists():
        for source_dir in RAGFLOW_SOURCES_DIR.iterdir():
            if source_dir.is_dir():
                _collect_json(source_dir)
                _collect_json(source_dir / "snapshots")
    # 预设
    for kind in PRESET_KINDS:
        _collect_json(presets._preset_dir(kind))
    # uploads 墓碑名单
    trashed = _trashed_names()
    for name in trashed:
        p = _upload_path(name)  # 越界名（旧版 folder 导入的绝对路径）一律不碰
        if p is not None and p.exists():
            candidates.append(p)
    for p in candidates:
        try:
            p.unlink()
        except OSError:
            pass
    # 名单收缩：文件已不存在的（删成功/本来就没有）从墓碑中移除
    if trashed:
        kept = {n for n in trashed if (up := _upload_path(n)) is not None and up.exists()}
        if kept != trashed:
            _set_trashed(kept)


def _upload_path(name: str) -> Path | None:
    """uploads 下的文件路径；name 为绝对路径（folder 导入）或越界时返回 None。"""
    if not name:
        return None
    p = UPLOADS_DIR / name
    return p if _within(UPLOADS_DIR, p) and p.resolve() != UPLOADS_DIR.resolve() else None


def _trashed_names() -> set[str]:
    """uploads 中已删除文件的墓碑名单（沙箱拦截 unlink 时的兜底，防止重启后复活）。"""
    return set(_read_json(UPLOADS_DIR / ".trashed.json", []))


def _set_trashed(names: set[str]) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(UPLOADS_DIR / ".trashed.json", sorted(names))


@synchronized
def init_storage() -> None:
    """启动初始化：建目录 → 恢复文档索引 → 扫描 uploads/ → 清理历史软删文件。

    由 main.py 的 lifespan 在单实例锁之后调用；import 本模块本身不碰文件系统
    （否则测试一 import 就会扫描、物理删除真实 data/ 下的文件）。
    脱离 FastAPI 直接用 storage 的脚本需自行先调用一次。
    """
    # 惰性 import：documents / ragflow 在模块级 import base，反向只在这里引，避免成环
    from . import documents, ragflow

    ensure_data_dirs()
    DOC_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        documents._load_persisted_docs()
    except StorageCorruptionError:
        # 启动服务提供诊断；不可自动扫描上传文件重建后覆盖丢失的外部文档索引。
        return
    documents._scan_uploads()
    # S5：清理历史遗留的、未被索引引用的正文制品（旧版本从不清理）
    documents._gc_text_artifacts({d["text_file"] for d in _read_json(DOC_INDEX, {}).values() if d.get("text_file")})
    # 物理清理历史软删除文件（脱离沙箱启动时生效；仍被拦截则保留标记下次再试）
    _purge()
    # 旧布局 config.json → 合成 legacy 连接（只读旧文件，不写旧目录）
    ragflow._synthesize_legacy_connections()
