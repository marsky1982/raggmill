"""文档输入：上传 / 文件夹 / 列表 / token 预估。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pathlib import Path
from pydantic import BaseModel

from .. import storage
from ..config import UPLOADS_DIR
from ..schemas import DocEstimateOut, DocOut, OkResult, SafeId

router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED_EXT = {".md", ".html", ".docx", ".txt"}

# 文本提取逻辑统一放在 storage 中，避免重复
_extract_text = storage._extract_text


def _validate_ext(name: str) -> None:
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {name}（支持 md/html/docx/txt）")


@router.post("/upload", response_model=list[DocOut])
async def upload(files: list[UploadFile], overwrite: bool = False):
    # 先整批校验、再写入：文档按文件名识别，文件夹里 a/README.md 与 b/README.md
    # 只取文件名后同名；旧实现边写边查，第二个才报 409，留下「上传了一半」的状态
    names: list[str] = []
    origin: dict[str, list[str]] = {}
    for f in files:
        # 只取最后一段文件名，丢弃客户端带来的目录部分（../、\、盘符等），防路径穿越
        name = Path((f.filename or "").replace("\\", "/")).name
        if name in ("", ".", ".."):
            raise HTTPException(400, f"非法文件名: {f.filename!r}")
        _validate_ext(name)
        names.append(name)
        origin.setdefault(name, []).append(f.filename or name)
    dup_in_batch = {n: srcs for n, srcs in origin.items() if len(srcs) > 1}
    if dup_in_batch:
        detail = "；".join(f"{n}（{'、'.join(srcs)}）" for n, srcs in dup_in_batch.items())
        raise HTTPException(409, f"这批文件里有重名：{detail}。文档按文件名识别，请改名后再上传，本次未写入任何文件")
    if not overwrite:
        existing = [n for n in names if storage.document_by_name(n) is not None]
        if existing:
            raise HTTPException(409, f"同名文档已存在：{'、'.join(existing)}；如需替换请显式传 overwrite=true，本次未写入任何文件")
    out = []
    for f, name in zip(files, names):
        dest = UPLOADS_DIR / name
        if not dest.resolve().is_relative_to(UPLOADS_DIR.resolve()):
            raise HTTPException(400, f"非法文件名: {f.filename!r}")
        raw = await f.read()
        dest.write_bytes(raw)
        try:
            doc = storage.add_document(name, _extract_text(name, raw), len(raw), overwrite=overwrite)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        out.append(storage.doc_view(doc))
    return out


class FolderIn(BaseModel):
    path: str
    glob: str = "**/*.md"


@router.post("/folder", response_model=list[DocOut])
def folder(inp: FolderIn, overwrite: bool = False):
    root = Path(inp.path)
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在: {inp.path}")
    out = []
    for p in sorted(root.glob(inp.glob)):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
            continue
        if storage.document_by_name(str(p)) is not None and not overwrite:
            raise HTTPException(409, f"同名文档已存在：{p}; 如需替换请显式传 overwrite=true")
        raw = p.read_bytes()
        try:
            doc = storage.add_document(str(p), _extract_text(str(p), raw), len(raw), source="folder", overwrite=overwrite)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        out.append(storage.doc_view(doc))
    if not out:
        raise HTTPException(400, f"目录中没有匹配 {inp.glob} 的支持文件")
    return out


@router.get("", response_model=list[DocOut])
def list_docs():
    return [storage.doc_view(d) for d in storage.document_snapshot().values()]


@router.post("/estimate", response_model=DocEstimateOut)
def estimate(body: dict):
    documents = storage.document_snapshot()
    ids = body.get("document_ids") or list(documents)
    docs = [documents[i] for i in ids if i in documents]
    per_file = [{"name": d["name"], "est_tokens": d["est_tokens"]} for d in docs]
    total = sum(x["est_tokens"] for x in per_file)
    avg = total / len(docs) if docs else 0
    bin_ = "long" if avg > 500 else ("mid" if avg >= 100 else "short")
    warning = None
    if docs and avg < 100:
        warning = "ragas 要求文档平均 ≥100 tokens，当前过短，default_transforms 会报错"
    return {"total_tokens": total, "avg_tokens": round(avg, 1), "bin": bin_, "warning": warning, "per_file": per_file}


@router.delete("/{doc_id}", response_model=OkResult)
def delete_doc(doc_id: SafeId):
    if storage.remove_document(doc_id):
        return {"ok": True}
    raise HTTPException(404, "文档不存在")
