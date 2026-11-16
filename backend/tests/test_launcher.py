import importlib.util
from pathlib import Path
import socket


def launcher():
    spec=importlib.util.spec_from_file_location('studio_launcher',Path(__file__).resolve().parents[2]/'run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_process_ownership_and_pid_reuse(tmp_path):
    m=launcher();root=tmp_path/'studio with spaces'
    own={'pid':42,'created':'123','executable':'C:/Python/python.exe',
         'command':f'python -m uvicorn app.main:app --app-dir "{root / "backend"}"'}
    assert m.owned(own,root)
    assert m.matches(own,dict(own),root)
    assert not m.matches(own,own|{'created':'124'},root)
    assert not m.owned(own|{'command':'python -m uvicorn app.main:app --app-dir C:/other/backend'},root)
    assert not m.owned(own|{'command':f'python -m uvicorn app.main:app --app-dir "{root / "backend-other"}"'},root)
    assert not m.owned({'executable':'C:/node/node.exe','command':'node vite'},root)


def test_port_in_use_is_not_reused():
    m=launcher()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));sock.listen()
        busy=sock.getsockname()[1]
        try:m.free_port([busy])
        except RuntimeError:pass
        else:raise AssertionError('unrelated listener reused')


def test_stop_skips_reused_pid_but_stops_the_rest(tmp_path,monkeypatch):
    """启停死锁：某个 PID 被复用时不能杀它，但也不能因此停不掉其余托管进程。"""
    import pytest
    m=launcher();state=tmp_path/'services.json';state.write_text('keep')
    root=Path(m.ROOT)
    vite={'pid':7,'created':'1','executable':'C:/node/node.exe',
          'command':f'node "{(root / "frontend/node_modules/vite/bin/vite.js").resolve()}"'}
    reused={'pid':42,'created':'old'}
    alive=[reused|{'created':'new'},dict(vite)]
    killed=[]
    monkeypatch.setattr(m,'STATE',state)
    monkeypatch.setattr(m,'read_state',lambda:{'processes':[vite,reused]})
    monkeypatch.setattr(m,'inventory',lambda:list(alive))
    monkeypatch.setattr(m,'request_graceful_shutdown',lambda _state:False)
    def kill(row):
        if row['pid']==42: pytest.fail('must not kill a reused PID')
        killed.append(row['pid']); alive.remove(row)
    monkeypatch.setattr(m,'terminate_verified',kill)
    m.stop()
    assert killed==[7]
    assert not state.exists()  # 托管进程都已不在，清单收口，下次 start 不会被卡住


def test_stop_keeps_manifest_when_managed_process_survives(tmp_path,monkeypatch):
    import pytest
    m=launcher();state=tmp_path/'services.json';state.write_text('keep')
    root=Path(m.ROOT)
    vite={'pid':7,'created':'1','executable':'C:/node/node.exe',
          'command':f'node "{(root / "frontend/node_modules/vite/bin/vite.js").resolve()}"'}
    monkeypatch.setattr(m,'STATE',state)
    monkeypatch.setattr(m,'read_state',lambda:{'processes':[vite]})
    monkeypatch.setattr(m,'inventory',lambda:[dict(vite)])
    monkeypatch.setattr(m,'request_graceful_shutdown',lambda _state:False)
    monkeypatch.setattr(m,'terminate_verified',lambda _row:None)  # 杀不掉
    monkeypatch.setattr(m.time,'sleep',lambda _s:None)
    with pytest.raises(RuntimeError,match='PID 7'):m.stop()
    assert state.read_text()=='keep'
