"""health 里的 commit 读取：普通仓库、git worktree、packed-refs、detached HEAD 四种布局。

测试模块顶层不得 import app（conftest 需要先设置 DATA_DIR）。
"""
from __future__ import annotations

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_reads_commit_from_plain_repository(tmp_path):
    from app.main import _read_git_commit

    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(SHA + "\n", encoding="utf-8")
    assert _read_git_commit(tmp_path)[0] == SHA[:12]


def test_reads_commit_inside_git_worktree(tmp_path):
    """worktree 里 .git 是文件：HEAD 在 worktrees/<名字>/，分支 ref 在主仓库（经 commondir 找到）。"""
    from app.main import _read_git_commit

    main_git = tmp_path / "main" / ".git"
    wt_git = main_git / "worktrees" / "feature"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    (main_git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n" f"{SHA} refs/heads/feature\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_git.as_posix()}\n", encoding="utf-8")

    commit, note = _read_git_commit(worktree)
    assert commit == SHA[:12]
    assert "packed-refs" in note

    # 分支 ref 是松散文件时同样能找到（在主仓库的 refs/heads 下）
    (main_git / "refs" / "heads").mkdir(parents=True)
    other = "f" * 40
    (main_git / "refs" / "heads" / "feature").write_text(other + "\n", encoding="utf-8")
    assert _read_git_commit(worktree)[0] == other[:12]


def test_detached_head_and_missing_git(tmp_path):
    from app.main import _read_git_commit

    assert _read_git_commit(tmp_path)[0] is None
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(SHA + "\n", encoding="utf-8")
    assert _read_git_commit(tmp_path)[0] == SHA[:12]
