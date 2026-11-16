"""文档会话：上传正文抽取、索引落盘、正文制品回收与 uploads 扫描。"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import base


@base.synchronized
def document_by_name(name: str) -> dict | None:
    return next((doc for doc in base.DOCUMENTS.values() if doc.get("name") == name), None)


def _extract_text(name: str, raw: bytes) -> str:
    """从上传文件抽取纯文本（供 token 预估与 ragas 抽取使用）。"""
    ext = Path(name).suffix.lower()
    if ext in (".md", ".txt"):
        return raw.decode("utf-8", errors="ignore")
    if ext == ".html":
        text = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text, flags=re.I)
        return re.sub(r"<[^>]+>", " ", text)
    if ext == ".docx":
        try:
            import io
            from docx import Document
            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return ""
    return ""


def _persist_docs() -> None:
    """将 DOCUMENTS 落盘（不含大段 text 以免体积膨胀，仅保留元数据 + 文本指针）。"""
    if str(base.DOC_INDEX.resolve()) in base._CORRUPT:
        raise base.StorageCorruptionError("文档索引或正文尚未恢复，禁止修改文档")
    # 正文按内容 hash 写为不可变制品，再原子发布索引。索引失败时旧正文仍可读。
    text_dir = base.DOC_INDEX.parent / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    payload = {}
    for key, doc in base.DOCUMENTS.items():
        text = doc.get("text", "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        target = text_dir / f"{base.check_id(key, 'document_id')}-{digest}.txt"
        if not target.exists():
            base._atomic_write_text(target, text)
        payload[key] = {k: v for k, v in doc.items() if k != "text"}
        payload[key]["text_file"] = target.name
        payload[key]["text_sha256"] = digest
    base._write_json(base.DOC_INDEX, payload)
    # S5：索引发布成功后，清掉不再被引用的正文制品（删除 / 覆盖上传后的用户原文）
    _gc_text_artifacts({v["text_file"] for v in payload.values()})


def _gc_text_artifacts(referenced: set[str]) -> int:
    """删除 documents/texts/ 下未被索引引用的正文制品，返回清理个数。

    只在索引已成功落盘之后调用（崩溃时最多残留，不会删掉仍被引用的正文）。
    物理删除被拦截时把内容清空兜底——未被引用的制品没有任何读路径，清空即不再泄露原文。
    """
    text_dir = base.DOC_INDEX.parent / "texts"
    if not text_dir.exists():
        return 0
    removed = 0
    for f in text_dir.glob("*.txt"):
        if f.name in referenced or not f.is_file():
            continue
        base._soft_delete(f)
        removed += 1
    return removed


def _load_persisted_docs() -> None:
    """启动时从磁盘索引恢复文档（仅内存缓存，文本字段一并恢复）。"""
    data = base._read_json(base.DOC_INDEX, {})
    restored = {}
    for k, v in data.items():
        doc = dict(v)
        if "text_file" in doc:
            try:
                target = base._safe_join(base.DOC_INDEX.parent / "texts", doc["text_file"])
                text = target.read_bytes().decode("utf-8")
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != doc.get("text_sha256"):
                    raise ValueError("正文 hash 不匹配")
            except (OSError, UnicodeError, ValueError) as exc:
                base._CORRUPT[str(base.DOC_INDEX.resolve())] = str(target) if 'target' in locals() else ""
                raise base.StorageCorruptionError(f"文档 {k} 的正文缺失或损坏，请恢复制品后重试") from exc
            doc["text"] = text
        restored[k] = doc
    base.DOCUMENTS.clear()
    base.DOCUMENTS.update(restored)


def _scan_uploads() -> None:
    """启动时扫描 uploads/ 目录，把未在索引中的文件加入文档列表。"""
    if not base.UPLOADS_DIR.exists():
        return
    indexed_names = {d.get("name") for d in base.DOCUMENTS.values()}
    trashed = base._trashed_names()
    for p in sorted(base.UPLOADS_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".md", ".html", ".docx", ".txt"}:
            continue
        if p.name in indexed_names or p.name in trashed:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        text = _extract_text(p.name, raw)
        doc = add_document(p.name, text, len(raw), source="disk")
        indexed_names.add(doc["name"])


@base.synchronized
def add_document(name: str, text: str, size: int, source: str = "upload", *, overwrite: bool = False) -> dict:
    # 重新上传同名文件视为恢复，清除删除墓碑
    trashed = base._trashed_names()
    if name in trashed:
        trashed.discard(name)
        base._set_trashed(trashed)
    # 粗估 token：中文约 1 字/token，英文约 4 字符/token，折中按 2.5
    est = max(1, int(len(text) / 2.5)) if text else max(1, size // 3)
    doc = {
        "id": base.stable_doc_id(name),
        "name": name,
        "size": size,
        "est_tokens": est,
        "chars": len(text),
        "text": text,
        "source": source,
        "status": "ok" if text else "binary",  # docx 未解析时标记
    }
    previous = base.DOCUMENTS.get(doc["id"])
    if previous is not None and not overwrite:
        raise FileExistsError(f"同名文档已存在：{name}")
    base.DOCUMENTS[doc["id"]] = doc
    try:
        _persist_docs()
    except Exception:
        if previous is None:
            base.DOCUMENTS.pop(doc["id"], None)
        else:
            base.DOCUMENTS[doc["id"]] = previous
        raise
    return doc


@base.synchronized
def remove_document(doc_id: str) -> bool:
    if doc_id not in base.DOCUMENTS:
        return False
    doc = base.DOCUMENTS.pop(doc_id)
    try:
        _persist_docs()
    except Exception:
        base.DOCUMENTS[doc_id] = doc
        raise
    # 同步删除 uploads 源文件；沙箱拦截 unlink 时记墓碑，防止 _scan_uploads 复活。
    # folder 导入的 name 是用户原文件的绝对路径：不在 UPLOADS_DIR 内的一律只删索引，绝不碰原文件
    p = base._upload_path(doc.get("name", ""))
    if p is not None:
        base._soft_delete(p)  # 物理删除优先，被拦截时由统一入口记墓碑
    return True


def doc_view(d: dict) -> dict:
    return {k: d[k] for k in ("id", "name", "size", "est_tokens", "chars", "source", "status")}
