"""单实例锁：防止多个后端实例并发写同一份 index.json 造成数据竞争。

启动时若已有存活的同名后端进程则拒绝启动；正常退出时清理锁文件。
崩溃残留的锁文件靠下次启动时的探活兜底（进程不存活则接管）。

存活判断 = PID 存活 **且** 进程创建时间与记录一致：只看 PID 时，强杀后残留的锁 / 租约
在 PID 被别的进程复用后会被误判为「仍在运行」，后端拒绝启动、任务显示运行中且取消不了。
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

from .config import DATA_DIR

LOCK_FILE = DATA_DIR / ".backend.lock"


def _pid_alive(pid: int) -> bool:
    """跨平台检测进程是否存活。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: OpenProcess 对已退出但句柄未释放的进程仍可能返回有效句柄，
        # 必须用 GetExitCodeProcess 检查退出码是否为 STILL_ACTIVE
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 无权探测的进程按存活处理，不能因权限不足抢占其资源。
            return kernel32.GetLastError() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    # POSIX: 信号 0 不实际发信号，仅做存在性检查
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_started(pid: int) -> int | None:
    """进程创建时间（Windows：FILETIME 100ns 刻度；Linux：开机后的时钟滴答）。拿不到返回 None。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat.rsplit(")", 1)[1].split()[19])  # 第 22 字段 starttime
    except (OSError, ValueError, IndexError):
        return None


def current_identity() -> dict:
    """本进程身份：写进锁文件 / 任务记录 / 资源租约，供重启后判断「还是不是同一个进程」。"""
    return {"pid": os.getpid(), "started": process_started(os.getpid())}


def identity_alive(pid: int, started: int | None = None) -> bool:
    """记录的进程是否仍是同一个进程：PID 存活且创建时间一致。

    旧记录没有 started 时只能按 PID 判（保持旧行为）；拿不到当前创建时间（无权限等）时按存活处理，
    宁可保守不抢占。
    """
    if not _pid_alive(int(pid or 0)):
        return False
    if not started:
        return True
    actual = process_started(int(pid))
    return actual is None or actual == int(started)


def _read_lock() -> dict:
    """锁文件内容：新格式 JSON {pid, started}；兼容旧格式纯 PID。"""
    try:
        raw = LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return {"pid": int(data)}
    except (ValueError, TypeError):
        return {}


def release_lock() -> None:
    """释放锁：只删自己写的锁（lifespan 关闭时调用，atexit 兜底；重复调用无害）。"""
    if _read_lock().get("pid") != os.getpid():
        return
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        # 沙箱环境可能拦截 unlink；下次启动靠 PID 探活兜底，不影响正确性
        pass


def ensure_single_instance() -> None:
    """启动时调用：若已有存活后端实例则拒绝启动。"""
    if LOCK_FILE.exists():
        prev = _read_lock()
        try:
            prev_pid = int(prev.get("pid") or 0)
        except (TypeError, ValueError):
            prev_pid = 0
        if prev_pid and prev_pid != os.getpid() and identity_alive(prev_pid, prev.get("started")):
            sys.exit(
                f"[single-instance] 后端已在运行 (PID {prev_pid})。"
                f"如需重启请先停止它，或删除 {LOCK_FILE}"
            )
    # 写入当前进程身份（PID + 创建时间）
    LOCK_FILE.write_text(json.dumps(current_identity()), encoding="utf-8")
    atexit.register(release_lock)
