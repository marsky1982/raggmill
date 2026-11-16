"""RAGgmill 后端入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hmac
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import storage
from .config import MODE, ensure_data_dirs
from .errors import error_response, install_error_handlers
from .routers import calc, documents, evaluation, knowledge_graph, models, ragflow, tasks, testset
from .single_instance import ensure_single_instance, release_lock
from .storage import UnsafePathError
from .tasks.manager import manager, ResourceBusyError


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # import 本模块不碰文件系统；所有启动副作用都在这里，顺序固定：
    # 1) 建 data 目录（锁文件要落在里面）
    # 2) 单实例锁：uvicorn 先跑 lifespan startup 再绑端口，所以仍在绑端口前拒绝重复实例，
    #    避免多实例并发写同一份 data/*/index.json 互相覆盖
    # 3) 持锁后才恢复文档索引、扫描 uploads、清理软删文件
    ensure_data_dirs()
    ensure_single_instance()
    try:
        storage.init_storage()
        manager.initialize(storage.DATA_DIR / "tasks.sqlite3")
        yield
        # 正常关闭（Ctrl+C / 启动器的优雅停止）：先让在跑任务按「先落盘再结束」收尾
        await manager.shutdown()
    finally:
        release_lock()


app = FastAPI(title="RAGgmill", version="0.1.0-demo", lifespan=lifespan)
# 非 2xx 统一错误体 {detail, error_code, extra?}（含 HTTPException / 422 / ApiError）
install_error_handlers(app)
INSTANCE_ID = os.environ.get("RAGAS_STUDIO_INSTANCE_ID") or uuid.uuid4().hex
WORKSPACE_ID = hashlib.sha256(str(Path(__file__).resolve().parents[2]).casefold().encode()).hexdigest()[:16]
_DEFAULT_HOSTS = ("127.0.0.1", "localhost", "testserver")
# 回环名始终放行：容器 / 启动器的健康检查都按 127.0.0.1 访问。配置的域名只做「追加」——
# 旧实现设了 RAGAS_STUDIO_ALLOWED_HOSTS 就整体替换，对外部署只填域名时健康检查被 400，
# compose 判后端 unhealthy、前端起不来。回环名不削弱防护：DNS rebinding 的 Host 是攻击者域名。
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """RAGAS_STUDIO_ALLOWED_HOSTS（逗号分隔）→ 白名单；未设置时用默认值，任何情况下都含回环名。"""
    configured = [v.strip().lower() for v in (raw if raw is not None else ",".join(_DEFAULT_HOSTS)).split(",")]
    hosts = list(dict.fromkeys(h for h in configured if h)) or list(_DEFAULT_HOSTS)
    return tuple(dict.fromkeys([*hosts, *_LOOPBACK_HOSTS]))


_ALLOWED_HOSTS = _allowed_hosts(os.environ.get("RAGAS_STUDIO_ALLOWED_HOSTS"))
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 进程启动时间：health 里给前端判断「后端是否还是旧进程」用（模块导入即进程启动时刻）
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_dirs(repo_root: Path) -> tuple[Path, Path] | None:
    """返回 (HEAD 所在目录, refs 所在的公共目录)。

    普通仓库两者都是 `.git/`；git worktree 里 `.git` 是一个文件（`gitdir: <路径>`），
    HEAD 在 `<主仓库>/.git/worktrees/<名字>/`，分支 refs 与 packed-refs 在它 `commondir` 指向的主仓库 `.git/`。
    """
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git, dot_git
    if not dot_git.is_file():
        return None
    text = dot_git.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(text[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    common = git_dir
    commondir = git_dir / "commondir"
    if commondir.is_file():
        rel = Path(commondir.read_text(encoding="utf-8").strip())
        common = rel if rel.is_absolute() else (git_dir / rel).resolve()
    return git_dir, common


def _read_git_commit(repo_root: Path) -> tuple[str | None, str | None]:
    """直读 .git 拿当前 commit（不调 git 命令）；兼容普通仓库与 git worktree。"""
    try:
        dirs = _git_dirs(repo_root)
        if dirs is None:
            return None, "仓库根目录下没有 .git（可能是打包产物）"
        git_dir, common = dirs
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:12], "读自 detached HEAD"
        ref = head[4:].strip()
        for base in dict.fromkeys((git_dir, common)):
            ref_file = base / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip()[:12], "读自 .git/HEAD 指向的 ref"
        packed = common / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:12], "读自 .git/packed-refs"
        return None, f"拿不到：ref {ref} 不存在"
    except OSError as exc:
        return None, f"拿不到：{type(exc).__name__}"


@lru_cache(maxsize=1)
def _code_commit() -> tuple[str | None, str | None]:
    """尽力读出当前代码版本：环境变量优先，其次直读 .git（不调 git 命令）。

    返回 (commit, 拿不到的原因)。只读两三个小文件，结果缓存，不参与业务逻辑。
    """
    env = os.environ.get("RAGAS_STUDIO_COMMIT")
    if env:
        return env.strip()[:12], "来自环境变量 RAGAS_STUDIO_COMMIT"
    return _read_git_commit(Path(__file__).resolve().parents[2])


def _allowed_local_origin(origin: str) -> bool:
    """只允许本机工作台来源；跨站页面即使能访问 loopback 也不能写数据。"""
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in _ALLOWED_HOSTS


def _security_error(detail: str, error_code: str) -> JSONResponse:
    return error_response(403, detail, error_code)


app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_ALLOWED_HOSTS))


@app.middleware("http")
async def _local_write_guard(request: Request, call_next):
    # 环境变量存在时，启动器生成的实例 token 是写请求的第二道保护；手工启动未配置 token 时，
    # 仍以 Origin/Referer 同源检查保护浏览器请求，便于 curl 和本地测试使用。
    if request.method in _WRITE_METHODS and request.url.path.startswith("/api/"):
        token = os.environ.get("RAGAS_STUDIO_WRITE_TOKEN", "")
        supplied = request.headers.get("x-ragas-write-token", "")
        if token and not hmac.compare_digest(supplied, token):
            return _security_error("写请求缺少有效的工作台实例令牌", "write_token_required")

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin and not _allowed_local_origin(origin):
            return _security_error("拒绝跨站写请求", "cross_site_request")
        if not origin and referer:
            try:
                referer_origin = f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}"
            except ValueError:
                referer_origin = ""
            if not _allowed_local_origin(referer_origin):
                return _security_error("拒绝跨站写请求", "cross_site_request")
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    # 前端 vite 会因端口冲突避让到 5180/5183/5186 等，统一放行所有 localhost 端口
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(UnsafePathError)
async def _unsafe_path(_request: Request, exc: UnsafePathError):
    # 非法 id / 路径越界（../、%5C 等）统一 400，而不是 500
    return error_response(400, str(exc), "unsafe_path")


@app.exception_handler(storage.StorageCorruptionError)
async def _corrupt_storage(_request: Request, exc: storage.StorageCorruptionError):
    return error_response(503, str(exc), "storage_corrupt")


@app.exception_handler(ResourceBusyError)
async def _resource_busy(_request: Request, exc: ResourceBusyError):
    return error_response(409, str(exc), "resource_busy")


for r in (models.router, documents.router, knowledge_graph.router, ragflow.router, testset.router, evaluation.router, tasks.router, calc.router):
    app.include_router(r)


@app.post("/api/admin/shutdown")
async def shutdown():
    """启动器 `run.py stop` 的优雅停止入口：只对启动器托管、配置了写令牌的实例开放。

    触发 uvicorn 的正常退出流程 → lifespan 收尾（取消在跑任务、等它们落盘）→ 释放单实例锁。
    写令牌由 _local_write_guard 校验；手工启动（无令牌）时拒绝，避免本机任意进程无凭据停服。
    """
    import asyncio
    import signal

    if not os.environ.get("RAGAS_STUDIO_WRITE_TOKEN"):
        return error_response(403, "仅启动器托管的实例支持远程停止；手工启动请在终端按 Ctrl+C", "not_managed")
    # 先返回响应再发信号；uvicorn 在 Windows 上用 signal.signal 接管 SIGTERM，收到后走正常关闭
    asyncio.get_running_loop().call_later(0.2, signal.raise_signal, signal.SIGTERM)
    return {"ok": True, "detail": "正在停止：取消在跑任务并保存已完成结果"}


@app.get("/api/health")
def health():
    commit, commit_note = _code_commit()
    return {"status": "ok", "mode": MODE, "app_id": "raggmill",
            "workspace_id": WORKSPACE_ID, "instance_id": INSTANCE_ID, "process_id": os.getpid(),
            "started_at": STARTED_AT, "commit": commit, "commit_note": commit_note,
            "storage": storage.storage_health()}
