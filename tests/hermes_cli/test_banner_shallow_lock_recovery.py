"""Regressions for shallow-lock cleanup and deepen-based behind counts.

Active candidate installs share a common ``.git`` and can leave
``shallow.lock`` debris after a crashed ``git fetch --deepen``. Combined with
``--depth 1`` re-shallowing, the update checker collapses to ``behind=-1``
(Desktop badge ``(update)`` instead of ``(+N)``).

These cover the recovery path added to ``hermes_cli.banner``:

* only ``shallow.lock`` is ever unlinked (never index/HEAD/packed-refs)
* age gate leaves live concurrent locks alone
* common-dir is consulted so linked worktrees are covered
* shallow fetch uses ``--deepen``, never ``--depth 1``
* tip-diff + merge-base yields a real count; otherwise NO_COUNT sentinel
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cli import banner


def _touch(path: Path, *, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_clear_stale_shallow_locks_removes_only_old_shallow_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)

    now = 1_700_000_000.0
    stale = _touch(git_dir / "shallow.lock", mtime=now - 300)
    fresh_index = _touch(git_dir / "index.lock", mtime=now - 300)  # must NEVER be touched
    young_shallow = None  # placed via common-dir mock below

    common = tmp_path / "common.git"
    common.mkdir()
    young_shallow = _touch(common / "shallow.lock", mtime=now - 10)
    old_common = _touch(common / "shallow.lock.spare", mtime=now - 400)  # wrong name
    # actual common shallow.lock is young_shallow; also place an old one under
    # a second resolved path only if distinct — here common is the common-dir.

    def fake_stdout(args, *, cwd, timeout=5):
        assert Path(cwd) == repo
        if args == ["rev-parse", "--git-common-dir"]:
            return str(common)
        if args == ["rev-parse", "--git-dir"]:
            return str(git_dir)
        return None

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)

    removed = banner._clear_stale_shallow_locks(repo, now=now)

    assert str(stale) in removed
    assert not stale.exists()
    assert fresh_index.exists(), "index.lock must never be swept"
    assert young_shallow.exists(), "fresh shallow.lock must survive age gate"
    assert old_common.exists()  # wrong filename
    assert str(young_shallow) not in removed


def test_clear_stale_shallow_locks_skips_fresh_lock_even_when_only_git_dir(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    now = 1_700_000_100.0
    lock = _touch(git_dir / "shallow.lock", mtime=now - 5)

    monkeypatch.setattr(
        banner,
        "_git_stdout",
        lambda args, *, cwd, timeout=5: str(git_dir)
        if args[0] == "rev-parse"
        else None,
    )

    removed = banner._clear_stale_shallow_locks(repo, now=now, min_age_seconds=120)
    assert removed == []
    assert lock.exists()


def test_clear_stale_shallow_locks_never_raises_on_bad_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(banner, "_git_stdout", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert banner._clear_stale_shallow_locks(tmp_path) == []


def test_resolve_git_dirs_dedupes_relative_and_absolute(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_dir = repo / ".git"
    git_dir.mkdir()

    def fake_stdout(args, *, cwd, timeout=5):
        if args == ["rev-parse", "--git-common-dir"]:
            return ".git"
        if args == ["rev-parse", "--git-dir"]:
            return str(git_dir)
        return None

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    dirs = banner._resolve_git_dirs(repo)
    assert dirs == [git_dir.resolve()]


def _cmd_key(cmd) -> tuple:
    return tuple(cmd)


def test_shallow_check_uses_deepen_not_depth_one_and_counts(monkeypatch, tmp_path):
    """Shallow tip-diff path: deepen twice, count when merge-base exists."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    head = "aaa111"
    upstream = "bbb222"
    calls: list[tuple] = []

    def fake_stdout(args, *, cwd, timeout=5):
        calls.append(("stdout", tuple(args)))
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args == ["rev-parse", "HEAD"]:
            return head
        if args == ["rev-parse", "FETCH_HEAD"]:
            return upstream
        if args == ["rev-parse", "origin/main"]:
            return upstream
        if args == ["merge-base", "HEAD", "FETCH_HEAD"]:
            return "base000"
        if args[:2] == ["rev-parse", "--git-common-dir"]:
            return str(repo / ".git")
        if args[:2] == ["rev-parse", "--git-dir"]:
            return str(repo / ".git")
        return None

    run_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_cmds.append(list(cmd))
        key = tuple(cmd)
        if key[:3] == ("git", "rev-list", "--count"):
            return MagicMock(returncode=0, stdout="7\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])
    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = banner._check_via_local_git(repo)

    assert result == 7

    deepen_fetches = [
        c for c in run_cmds if c[:2] == ["git", "fetch"] and "--deepen" in c
    ]
    assert deepen_fetches, "expected at least one deepen fetch"
    for c in deepen_fetches:
        assert "--depth" not in c, f"must not re-shallow: {c}"
        assert str(banner._SHALLOW_DEEPEN_COMMITS) in c
    depth_one = [c for c in run_cmds if "--depth" in c and "1" in c]
    assert depth_one == []


def test_shallow_check_falls_back_to_no_count_without_merge_base(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args == ["rev-parse", "HEAD"]:
            return "aaa111"
        if args == ["rev-parse", "FETCH_HEAD"]:
            return "bbb222"
        if args == ["merge-base", "HEAD", "FETCH_HEAD"]:
            return None
        if args[:1] == ["rev-parse"] and args[1].startswith("--git"):
            return str(repo / ".git")
        return None

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])
    with patch(
        "hermes_cli.banner.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="", stderr=""),
    ):
        result = banner._check_via_local_git(repo)

    assert result == banner.UPDATE_AVAILABLE_NO_COUNT


def test_shallow_check_equal_tips_returns_zero_without_second_deepen(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    tip = "same000"

    def fake_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args in (
            ["rev-parse", "HEAD"],
            ["rev-parse", "FETCH_HEAD"],
            ["rev-parse", "origin/main"],
        ):
            return tip
        if args[:1] == ["rev-parse"] and args[1].startswith("--git"):
            return str(repo / ".git")
        return None

    run_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_cmds.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])
    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = banner._check_via_local_git(repo)

    assert result == 0
    # Only the initial refresh fetch — no second deepen after equal tips.
    deepen = [c for c in run_cmds if "--deepen" in c]
    assert len(deepen) == 1


def test_full_clone_path_unchanged_count(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:1] == ["rev-parse"] and args[1].startswith("--git"):
            return str(repo / ".git")
        return None

    def fake_run(cmd, **kwargs):
        if tuple(cmd)[:3] == ("git", "rev-list", "--count"):
            return MagicMock(returncode=0, stdout="12\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner, "_clear_stale_shallow_locks", lambda *a, **k: [])
    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        assert banner._check_via_local_git(repo) == 12


def test_count_commits_behind_rejects_non_digit(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=0, stdout="not-a-number\n", stderr="")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        assert banner._count_commits_behind(tmp_path, "origin/main") is None
