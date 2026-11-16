"""Windows service launcher shared by PowerShell and Git Bash; no legacy PID trust."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import secrets
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
LOG = ROOT / '.run-logs'
STATE = LOG / 'services.json'


def workspace_id(root=ROOT):
    return hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:16]


def inventory():
    script = """[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
@(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','node.exe') } | ForEach-Object {
 @{pid=[int]$_.ProcessId; executable=$_.ExecutablePath; command=$_.CommandLine;
 created=[string]$_.CreationDate.ToFileTimeUtc()}
}) | ConvertTo-Json -Compress"""
    r = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                       capture_output=True, encoding='utf-8', check=True, timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(r.stdout or '[]')


def owned(row, root=ROOT):
    command = (row.get('command') or '').replace('\\','/').casefold()
    exe = Path(row.get('executable') or '').name.casefold()
    def arg(path):
        value = str(path.resolve()).replace('\\','/').casefold()
        return f'"{value}"' in command or value in command.split()
    return ((exe in ('python.exe','pythonw.exe') and '-m uvicorn app.main:app' in command
             and '--app-dir' in command and arg(root/'backend'))
            or (exe == 'node.exe' and arg(root/'frontend/node_modules/vite/bin/vite.js')))


def matches(saved, current, root=ROOT):
    return all(saved.get(k) == current.get(k) for k in ('pid','created','command','executable')) and owned(current, root)


def terminate_verified(row):
    # Hold a handle, then verify creation time: a reused PID cannot target a new process.
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE,wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1001, False, row['pid'])
    if not handle:
        raise OSError(ctypes.get_last_error(), 'cannot open managed process')
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise OSError(ctypes.get_last_error(), 'cannot verify creation time')
        # CIM DateTime -> FILETIME conversion rounds by a few 100ns ticks.
        # Allow only this conversion noise; a reused PID differs by process lifetime.
        actual = (times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        if abs(actual - int(row['created'])) > 10:
            raise RuntimeError('PID reused; refusing to stop')
        if not kernel.TerminateProcess(handle, 0):
            raise OSError(ctypes.get_last_error(), 'cannot stop managed process')
    finally:
        kernel.CloseHandle(handle)


def write_state(state):
    LOG.mkdir(exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_bytes(json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
    os.replace(tmp, STATE)


def read_state():
    if not STATE.exists(): return None
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if state.get('workspace_id') != workspace_id():
        raise RuntimeError('manifest belongs to another workspace')
    return state


def free_port(candidates):
    for port in candidates:
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1',port)); return port
            except OSError: pass
    raise RuntimeError('no free port in requested range')


def healthy(state):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(f"http://127.0.0.1:{state['backend_port']}/api/health",timeout=1) as r:
            data = json.load(r)
        return (data.get('app_id') == 'ragas-testset-studio' and data.get('workspace_id') == workspace_id()
                and data.get('instance_id') == state['instance_id'] and data.get('mode') == state['mode'])
    except (OSError,ValueError,KeyError): return False


def start(args):
    old, rows = read_state(), inventory()
    if old and any(matches(s,r) for s in old['processes'] for r in rows):
        if old['mode'] == args.mode and healthy(old):
            print(f"Already running: http://127.0.0.1:{old['frontend_port']}"); return
        raise RuntimeError('managed instance exists; stop it before restarting or changing mode')
    strays = [r for r in rows if owned(r)]
    if strays:
        raise RuntimeError('本工作区有不在清单里的服务进程，请先确认并手动停止后再启动：\n'
                           + describe(strays)
                           + '\n（PowerShell：Stop-Process -Id <PID>；确认是本工作区的 uvicorn / vite 再停）')
    bp = free_port([args.backend_port] if args.backend_port else [8100,*range(8000,8011)])
    fp = free_port([args.frontend_port] if args.frontend_port else list(range(5173,5201)))
    python, node, vite = ROOT/'backend/.venv/Scripts/python.exe', shutil.which('node'), ROOT/'frontend/node_modules/vite/bin/vite.js'
    if not python.is_file() or not node or not vite.is_file():
        raise RuntimeError('backend environment or frontend dependencies missing; no automatic install')
    write_token = secrets.token_urlsafe(32)
    # 写令牌也记进清单（.run-logs 已被 git 忽略）：stop 用它请求后端优雅关闭
    state = dict(workspace_id=workspace_id(),instance_id=uuid.uuid4().hex,mode=args.mode,backend_port=bp,frontend_port=fp,
                 processes=[],write_token=write_token)
    env = dict(os.environ,RAGAS_DEMO='1' if args.mode=='demo' else '0',RAGAS_STUDIO_INSTANCE_ID=state['instance_id'],
               RAGAS_STUDIO_WRITE_TOKEN=write_token,PYTHONUTF8='1')
    path = ROOT/'frontend/.env.local'
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    lines = [v for v in lines if not v.startswith('VITE_API_TARGET=')]
    lines = [v for v in lines if not v.startswith('VITE_STUDIO_WRITE_TOKEN=')]
    path.write_bytes(('\n'.join(lines+[f'VITE_API_TARGET=http://127.0.0.1:{bp}',
                                          f'VITE_STUDIO_WRITE_TOKEN={write_token}'])+'\n').encode('utf-8'))
    commands = [('backend',[str(python),'-m','uvicorn','app.main:app','--app-dir',str(ROOT/'backend'),'--host','127.0.0.1','--port',str(bp)]),
                ('frontend',[node,str(vite),'--host','127.0.0.1','--port',str(fp),'--strictPort'])]
    write_state(state)
    for role,cmd in commands:
        with (LOG/f'{role}.log').open('ab') as log:
            subprocess.Popen(cmd,cwd=ROOT/role,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)
        time.sleep(.3)
        state['processes'] = [r for r in inventory() if owned(r)]
        write_state(state)
    for _ in range(30):
        if healthy(state):
            try:
                with socket.create_connection(('127.0.0.1',fp),timeout=1): break
            except OSError: pass
        time.sleep(1)
    else:
        raise RuntimeError('startup failed; inspect .run-logs logs; use stop to clean managed processes')
    state['processes'] = [r for r in inventory() if owned(r)]
    write_state(state)
    print(f'READY frontend=http://127.0.0.1:{fp} backend=http://127.0.0.1:{bp} mode={args.mode}')
    if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{fp}')


def describe(rows):
    return '\n'.join(f"  PID {r.get('pid')}  {r.get('executable') or ''}  {(r.get('command') or '')[:160]}" for r in rows)


def request_graceful_shutdown(state, wait=45):
    """先请后端自己退出：取消在跑任务、等它们把已完成样本和账单落盘、释放锁。

    旧清单没有写令牌、或后端已无响应时返回 False，由调用方回退到强制终止。
    """
    token, port = state.get('write_token'), state.get('backend_port')
    if not token or not port:
        return False
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/admin/shutdown', method='POST',
                                     headers={'X-RAGAS-Write-Token': token})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as r:
            if r.status != 200:
                return False
    except (OSError, ValueError):
        return False
    backend = [s for s in state['processes'] if Path(s.get('executable') or '').name.casefold().startswith('python')]
    deadline = time.time() + wait
    while time.time() < deadline:
        if not any(matches(s, r) for s in backend for r in inventory()):
            return True
        time.sleep(1)
    return False


def stop():
    state = read_state()
    if not state:
        print('No managed instance. Legacy .pid files are ignored; no processes stopped.'); return
    if request_graceful_shutdown(state):
        print('后端已优雅退出（在跑任务已取消并保存已完成结果）')
    rows = inventory()
    skipped = []
    for saved in reversed(state['processes']):
        current = next((r for r in rows if r['pid']==saved['pid']),None)
        if current is None: continue
        if not matches(saved,current):
            # 记录的进程已不在，这个 PID 被别的进程复用了：不是我们的，不能杀，也不该挡住其余进程的停止
            # （旧实现在这里直接 raise，剩下的 vite 停不掉，start 又要求先 stop → 启停死锁，）
            skipped.append(current); continue
        try:
            terminate_verified(current)
        except OSError:
            # 打开句柄失败多半是进程刚好已退出；下面的复查会兜底
            pass
    for _ in range(20):
        remaining = [r for r in inventory() for s in state['processes'] if matches(s, r)]
        if not remaining: break
        time.sleep(.1)
    else:
        raise RuntimeError('以下托管进程仍在运行，已保留清单与锁，请手动确认后停止：\n' + describe(remaining))
    if skipped:
        print('以下 PID 已被其他进程复用（与清单记录的创建时间 / 命令行不符），未停止：\n' + describe(skipped))
    # Backend owns its own lock. Dead locks are reclaimed at next backend startup.
    STATE.unlink()
    print('Managed instance stopped; other processes and backend lock untouched.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['start','stop'])
    parser.add_argument('--mode',choices=['ragas','demo'],default='ragas')
    parser.add_argument('--backend-port',type=int,default=0)
    parser.add_argument('--frontend-port',type=int,default=0)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if os.name!='nt': parser.error('launcher supports Windows PowerShell / Git Bash')
    if not all(0<=p<=65535 for p in (args.backend_port,args.frontend_port)): parser.error('invalid port')
    import msvcrt
    LOG.mkdir(exist_ok=True)
    with (LOG/'launcher.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError: parser.error('another start/stop operation is in progress')
        try: start(args) if args.action=='start' else stop()
        except RuntimeError as exc:
            # 预期内的拒绝 / 失败：打印中文原因、非零退出，不甩英文 Traceback
            print(f'[run.py] {exc}', file=sys.stderr); raise SystemExit(1)
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


if __name__=='__main__': main()
