"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()


def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return (result.stdout or "").strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    (path / "README").write_text("one\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    return path


def test_clear_stale_shallow_locks_removes_old_only(tmp_path):
    """Only aged shallow.lock is removed; young locks and other lock names stay."""
    import hermes_cli.banner as banner

    repo = _init_repo(tmp_path / "repo")
    git_dir = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()

    stale = git_dir / "shallow.lock"
    fresh = git_dir / "shallow.lock.fresh-marker"  # not used; we toggle mtime
    index_lock = git_dir / "index.lock"
    head_lock = git_dir / "HEAD.lock"
    packed_lock = git_dir / "packed-refs.lock"

    stale.write_text("stale\n", encoding="utf-8")
    index_lock.write_text("live-index\n", encoding="utf-8")
    head_lock.write_text("live-head\n", encoding="utf-8")
    packed_lock.write_text("live-packed\n", encoding="utf-8")

    old = time.time() - (banner._STALE_SHALLOW_LOCK_MIN_AGE_SECONDS + 30)
    os.utime(stale, (old, old))

    # Fresh shallow.lock should survive
    # (recreate after first sweep with recent mtime)
    removed = banner._clear_stale_shallow_locks(repo)
    assert str(stale) in removed
    assert not stale.exists()
    assert index_lock.exists()
    assert head_lock.exists()
    assert packed_lock.exists()

    fresh_lock = git_dir / "shallow.lock"
    fresh_lock.write_text("fresh\n", encoding="utf-8")
    removed_fresh = banner._clear_stale_shallow_locks(repo)
    assert removed_fresh == []
    assert fresh_lock.exists()


def test_clear_stale_shallow_locks_uses_common_dir_for_worktree(tmp_path):
    """Candidate/worktree installs store shallow.lock in the common git dir."""
    import hermes_cli.banner as banner

    main = _init_repo(tmp_path / "main")
    # Need a second commit so worktree can detach cleanly on older git.
    (main / "README").write_text("two\n", encoding="utf-8")
    _git(main, "add", "README")
    _git(main, "commit", "-m", "two")

    wt = tmp_path / "worktree"
    _git(main, "worktree", "add", "--detach", str(wt), "HEAD~0")

    common = Path(_git(wt, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (wt / common).resolve()

    lock = common / "shallow.lock"
    lock.write_text("stale-common\n", encoding="utf-8")
    old = time.time() - (banner._STALE_SHALLOW_LOCK_MIN_AGE_SECONDS + 5)
    os.utime(lock, (old, old))

    # Also plant a non-shallow lock in common dir — must survive.
    other = common / "index.lock"
    other.write_text("nope\n", encoding="utf-8")
    os.utime(other, (old, old))

    removed = banner._clear_stale_shallow_locks(wt)
    assert any(Path(p).name == "shallow.lock" for p in removed)
    assert not lock.exists()
    assert other.exists()


def test_shallow_clone_returns_real_count_when_history_connects(tmp_path, monkeypatch):
    """Shallow installs must not force behind=-1 when rev-list can count.

    Desktop maps -1 → "(update)" and >0 → "(+N)". Candidate/worktree deploys
    are shallow; without a real count the status bar never shows (+N).
    """
    import hermes_cli.banner as banner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    calls = {"deepen": 0, "depth1": 0, "cleared": 0}

    def fake_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args[:2] == ["rev-parse", "HEAD"]:
            return "aaa111"
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            return "bbb222"
        if args[:2] == ["rev-parse", "origin/main"]:
            return "bbb222"
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:1] == ["merge-base"]:
            return "aaa111"
        if args[:1] == ["rev-parse"] and args[1] in ("--git-common-dir", "--git-dir"):
            return str(repo / ".git")
        return None

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        argv = list(args)
        if argv and argv[0] == "git":
            argv = argv[1:]
        if argv[:1] == ["fetch"]:
            if "--deepen" in argv:
                calls["deepen"] += 1
            if "--depth" in argv:
                calls["depth1"] += 1
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if argv[:1] == ["rev-list"]:
            return type("R", (), {"returncode": 0, "stdout": "7\n", "stderr": ""})()
        return real_run(args, **kwargs)

    def fake_clear(repo_dir, **kwargs):
        calls["cleared"] += 1
        return []

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", fake_clear)

    behind = banner._check_via_local_git(repo)
    assert behind == 7
    assert calls["deepen"] >= 1
    assert calls["depth1"] == 0
    assert calls["cleared"] >= 1


def test_shallow_tip_equal_returns_zero_without_count(tmp_path, monkeypatch):
    """Matching tips on a shallow clone are up-to-date — no deepen/count needed."""
    import hermes_cli.banner as banner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args[:2] == ["rev-parse", "HEAD"]:
            return "same"
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            return "same"
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:1] == ["rev-parse"] and args[1] in ("--git-common-dir", "--git-dir"):
            return str(repo / ".git")
        return None

    def fake_run(args, **kwargs):
        argv = list(args)
        if argv and argv[0] == "git":
            argv = argv[1:]
        if argv[:1] == ["fetch"]:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if argv[:1] == ["rev-list"]:
            raise AssertionError("rev-list must not run when tips match")
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "nope"})()

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])

    assert banner._check_via_local_git(repo) == 0


def test_shallow_without_merge_base_falls_back_to_no_count(tmp_path, monkeypatch):
    """When deepen cannot connect history, keep the presence-only sentinel."""
    import hermes_cli.banner as banner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args[:2] == ["rev-parse", "HEAD"]:
            return "aaa"
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            return "bbb"
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:1] == ["merge-base"]:
            return None
        if args[:1] == ["rev-parse"] and args[1] in ("--git-common-dir", "--git-dir"):
            return str(repo / ".git")
        return None

    def fake_run(args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])

    assert banner._check_via_local_git(repo) == banner.UPDATE_AVAILABLE_NO_COUNT


def test_shallow_fetch_never_uses_depth_one(tmp_path, monkeypatch):
    """Re-fetching with --depth 1 destroys merge-base; deepen is required."""
    import hermes_cli.banner as banner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seen = []

    def fake_stdout(args, *, cwd, timeout=5):
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args[:2] == ["rev-parse", "HEAD"]:
            return "h1"
        if args[:2] == ["rev-parse", "FETCH_HEAD"]:
            return "h2"
        if args[:3] == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args[:1] == ["merge-base"]:
            return "h1"
        if args[:1] == ["rev-parse"] and args[1] in ("--git-common-dir", "--git-dir"):
            return str(repo / ".git")
        return None

    def fake_run(args, **kwargs):
        argv = list(args)
        if argv and argv[0] == "git":
            argv = argv[1:]
        if argv[:1] == ["fetch"]:
            seen.append(argv)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if argv[:1] == ["rev-list"]:
            return type("R", (), {"returncode": 0, "stdout": "3\n", "stderr": ""})()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner.subprocess, "run", fake_run)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])

    assert banner._check_via_local_git(repo) == 3
    assert seen, "expected at least one fetch"
    for argv in seen:
        assert "--depth" not in argv
        assert "--deepen" in argv
