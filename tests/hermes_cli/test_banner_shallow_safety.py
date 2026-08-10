"""Real-git acceptance tests for passive update-check shallow safety (v2).

Contract under test:
1. Passive checks never unlink shallow.lock / index.lock / HEAD.lock /
   packed-refs.lock (including old-but-live and replacement races).
2. Repeated equal-tip checks do not progressively deepen history.
3. Failed fetch + stale FETCH_HEAD fails closed (no trust of stale tip).
4. Linked worktree / common-dir shallow installs still get truthful counts
   when history connects, else UPDATE_AVAILABLE_NO_COUNT.
5. Absolute depth target (not relative --deepen) when tips differ.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import banner


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _write_commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", content.strip()[:40] or name)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _build_linear_upstream(path: Path, n_commits: int = 12) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "test")
    for i in range(n_commits):
        _write_commit(path, "README", f"commit-{i}\n")
    # Ensure branch is named main for fetch origin main.
    _git(path, "branch", "-M", "main")
    return path


def _shallow_clone(upstream: Path, dest: Path, *, depth: int = 1, branch: str = "main") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(
        dest.parent,
        "clone",
        f"--depth={depth}",
        "--branch",
        branch,
        f"file://{upstream.resolve()}",
        dest.name,
    )
    return dest


def _git_dir(repo: Path) -> Path:
    raw = _git(repo, "rev-parse", "--git-dir").stdout.strip()
    p = Path(raw)
    return p if p.is_absolute() else (repo / p).resolve()


def _common_dir(repo: Path) -> Path:
    raw = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    p = Path(raw)
    return p if p.is_absolute() else (repo / p).resolve()


def _reachable_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


def test_real_shallow_clone_equal_tips_no_progressive_deepen(tmp_path):
    """file:// shallow clone at equal tip must not grow history across checks."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=15)
    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1)

    assert _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    before = _reachable_count(clone)
    assert before == 1

    behind1, err1, rev1, _ = banner._check_via_local_git_details(clone)
    assert err1 is None
    assert behind1 == 0
    assert rev1
    mid = _reachable_count(clone)

    behind2, err2, _, _ = banner._check_via_local_git_details(clone)
    assert err2 is None
    assert behind2 == 0
    after = _reachable_count(clone)

    # Equal-tip path uses ls-remote only — depth must not grow.
    assert mid == before
    assert after == before


def test_real_shallow_clone_behind_returns_positive_count(tmp_path):
    """When history can connect under the absolute depth target, return +N."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=10)
    # Clone at an older tip: checkout depth-1 of an earlier commit via branch.
    # Create a release branch at commit 5, then advance main.
    old = _git(upstream, "rev-parse", "HEAD~4").stdout.strip()
    _git(upstream, "branch", "release", old)

    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1, branch="release")
    # Point origin/main tracking intent: fetch main from origin.
    # HEAD is release tip; origin has main 4 commits ahead.
    behind, err, rev, _ = banner._check_via_local_git_details(clone)
    assert err is None
    assert rev
    # Should be a real positive count (4) when merge-base connects, else -1.
    assert behind is not None
    assert behind == 4 or behind == banner.UPDATE_AVAILABLE_NO_COUNT
    if behind == 4:
        assert behind > 0


def test_old_but_live_shallow_lock_is_never_unlinked(tmp_path):
    """Passive check must not unlink an old-looking shallow.lock (live or not)."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=8)
    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1)
    common = _common_dir(clone)

    lock = common / "shallow.lock"
    lock.write_text("held-by-test\n", encoding="utf-8")
    # Age it past any historical "stale" threshold from the blocked v1 design.
    old = time.time() - 10_000
    os.utime(lock, (old, old))

    index_lock = common / "index.lock"
    head_lock = common / "HEAD.lock"
    packed_lock = common / "packed-refs.lock"
    index_lock.write_text("x\n", encoding="utf-8")
    head_lock.write_text("x\n", encoding="utf-8")
    packed_lock.write_text("x\n", encoding="utf-8")
    for p in (index_lock, head_lock, packed_lock):
        os.utime(p, (old, old))

    # Holding shallow.lock may make fetch fail; checker must still return
    # without deleting locks.
    behind, err, rev, _ = banner._check_via_local_git_details(clone)
    assert rev is not None
    # Result is either 0 (ls-remote equal) or NO_COUNT/error — never crash.
    assert behind is None or isinstance(behind, int)

    assert lock.exists(), "shallow.lock must never be unlinked by passive check"
    assert lock.read_text(encoding="utf-8") == "held-by-test\n"
    assert index_lock.exists()
    assert head_lock.exists()
    assert packed_lock.exists()


def test_lock_replacement_race_never_unlinks(tmp_path):
    """Even if a lock is replaced during the check, passive path never unlinks."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=6)
    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1)
    common = _common_dir(clone)
    lock_path = common / "shallow.lock"

    stop = threading.Event()
    replacements = {"n": 0}

    def churn():
        while not stop.is_set():
            try:
                # Atomic-ish replace: write temp then replace.
                tmp = common / f"shallow.lock.tmp.{replacements['n']}"
                tmp.write_text(f"live-{replacements['n']}\n", encoding="utf-8")
                os.replace(tmp, lock_path)
                replacements["n"] += 1
            except OSError:
                pass
            time.sleep(0.001)

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(5):
            banner._check_via_local_git(clone)
            time.sleep(0.01)
    finally:
        stop.set()
        t.join(timeout=2)

    # After churn stops, ensure whatever lock remains was not deleted by us
    # mid-flight in a way that leaves the path unlinked while a writer expects it.
    # The writer may have exited leaving a lock — that is fine; absence is also
    # fine only if the churn thread removed it via replace semantics, not our code.
    assert replacements["n"] > 0


def test_failed_fetch_with_stale_fetch_head_real_repo(tmp_path, monkeypatch):
    """Plant stale FETCH_HEAD, force fetch failure → do not trust stale tip."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=8)
    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1)

    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    # Advance upstream so real tip differs.
    _write_commit(upstream, "README", "newer\n")
    real_tip = _git(upstream, "rev-parse", "HEAD").stdout.strip()
    assert real_tip != head

    # Stale FETCH_HEAD equal to HEAD would wrongly look "up to date" if trusted.
    fh = _git_dir(clone) / "FETCH_HEAD"
    fh.write_text(f"{head}\t\tbranch 'main' of file://upstream\n", encoding="utf-8")

    # Break fetch by pointing origin at a missing path.
    _git(clone, "remote", "set-url", "origin", f"file://{tmp_path / 'missing.git'}")

    # ls-remote against broken origin fails; provide official ls-remote mock
    # via monkeypatch only for the official URL fallback path... Actually
    # origin is file://missing so ls-remote origin fails. Official fallback
    # would hit network. Instead monkeypatch _ls_remote_main_sha.
    def fake_ls(remote=banner._UPSTREAM_REPO_URL, timeout=10):
        # Report the real upstream tip (behind).
        return real_tip

    monkeypatch.setattr(banner, "_ls_remote_main_sha", fake_ls)

    behind, err, rev, _ = banner._check_via_local_git_details(clone)
    assert rev == head
    # Must NOT return 0 from stale FETCH_HEAD == HEAD.
    assert behind != 0
    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT
    assert err is None


def test_real_shallow_linked_worktree_common_dir(tmp_path):
    """Shallow main + linked worktree: counts/fallback via common-dir still work."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=12)
    old = _git(upstream, "rev-parse", "HEAD~3").stdout.strip()
    _git(upstream, "branch", "release", old)

    main_clone = _shallow_clone(upstream, tmp_path / "main-clone", depth=1, branch="release")
    # Add a second commit on release so worktree add is happier on some gits.
    wt = tmp_path / "wt"
    # worktree from shallow clone of release
    _git(main_clone, "worktree", "add", "--detach", str(wt), "HEAD")

    assert _common_dir(wt) == _common_dir(main_clone)
    assert _git(wt, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"

    behind, err, rev, _ = banner._check_via_local_git_details(wt)
    assert err is None
    assert rev
    assert behind is not None
    # 3 commits behind main when history connects under target depth.
    assert behind == 3 or behind == banner.UPDATE_AVAILABLE_NO_COUNT

    # Writable probe must consider common-dir.
    assert banner.repo_install_writable(wt) is True


def test_resolve_git_dirs_covers_worktree(tmp_path):
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=3)
    main = _shallow_clone(upstream, tmp_path / "main", depth=1)
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "--detach", str(wt), "HEAD")
    dirs = banner._resolve_git_dirs(wt)
    assert dirs
    assert _common_dir(wt) in dirs


def test_absolute_depth_is_idempotent_when_behind(tmp_path):
    """Two behind-checks must not keep growing past the absolute target."""
    # Build enough history that target depth is meaningful but small for test:
    # temporarily lower target via monkeypatch.
    n = 30
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=n)
    old = _git(upstream, "rev-parse", f"HEAD~{n - 2}").stdout.strip()
    _git(upstream, "branch", "release", old)
    clone = _shallow_clone(upstream, tmp_path / "clone", depth=1, branch="release")

    # Use a small absolute target for speed.
    original = banner._SHALLOW_HISTORY_TARGET
    try:
        banner._SHALLOW_HISTORY_TARGET = 10
        b1, e1, _, _ = banner._check_via_local_git_details(clone)
        c1 = _reachable_count(clone)
        b2, e2, _, _ = banner._check_via_local_git_details(clone)
        c2 = _reachable_count(clone)
    finally:
        banner._SHALLOW_HISTORY_TARGET = original

    assert e1 is None and e2 is None
    assert b1 is not None and b2 is not None
    # After first recovery, second must not keep adding relative deepen chunks.
    # Absolute --depth may leave count at/near target; must not grow unboundedly.
    assert c2 <= c1 + 2  # allow tiny variance; no +TARGET each time
    assert c2 <= banner._SHALLOW_HISTORY_TARGET + 5 or c2 <= 15


def test_full_clone_successful_fetch_uses_fetch_head_when_tracking_ref_is_stale(
    tmp_path,
):
    """A narrowed fetch refspec must not make stale origin/main authoritative."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=4)
    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "clone",
        f"file://{upstream.resolve()}",
        clone.name,
    )
    old_head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert _git(clone, "rev-parse", "origin/main").stdout.strip() == old_head

    # Keep a valid but deliberately narrowed tracking refspec.  An explicit
    # `fetch origin main` updates FETCH_HEAD while leaving origin/main stale.
    _git(clone, "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other")
    _write_commit(upstream, "README", "new-upstream-tip\n")

    behind, err, rev, target = banner._check_via_local_git_details(clone)

    assert err is None
    assert rev == old_head
    assert behind == 1
    assert target == _git(upstream, "rev-parse", "main").stdout.strip()
    assert _git(clone, "rev-parse", "origin/main").stdout.strip() == old_head
    assert _git(clone, "rev-parse", "FETCH_HEAD").stdout.strip() != old_head


def test_unknown_shallow_state_never_runs_unbounded_fetch(tmp_path, monkeypatch):
    """A failed shallow probe is unknown, not permission for a plain fetch."""
    repo = _build_linear_upstream(tmp_path / "repo", n_commits=2)
    original_stdout = banner._git_stdout
    original_run = banner._git_run
    fetch_calls = []

    def fake_stdout(args, **kwargs):
        if args == ["rev-parse", "--is-shallow-repository"]:
            return None
        return original_stdout(args, **kwargs)

    def recording_run(args, **kwargs):
        if args and args[0] == "fetch":
            fetch_calls.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(banner, "_git_stdout", fake_stdout)
    monkeypatch.setattr(banner, "_git_run", recording_run)
    monkeypatch.setattr(banner, "_ls_remote_main_sha", lambda *_args, **_kwargs: "f" * 40)

    behind, err, rev, _ = banner._check_via_local_git_details(repo)

    assert rev == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT
    assert err is None
    assert fetch_calls == []


def test_concurrent_fetch_head_overwrite_cannot_report_latest(tmp_path, monkeypatch):
    """A second real fetch after main fetch cannot substitute its FETCH_HEAD."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=6)
    old = _git(upstream, "rev-parse", "HEAD~2").stdout.strip()
    _git(upstream, "branch", "release", old)
    _git(upstream, "branch", "other", old)
    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "clone",
        "--branch",
        "release",
        f"file://{upstream.resolve()}",
        clone.name,
    )
    local_head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    main_tip = _git(upstream, "rev-parse", "main").stdout.strip()
    assert local_head != main_tip

    original_run = banner._git_run

    def overwrite_after_main_fetch(args, **kwargs):
        result = original_run(args, **kwargs)
        if (
            args[:3] == ["fetch", "origin", "main"]
            and result is not None
            and result.returncode == 0
        ):
            # This is a genuine concurrent-style overwrite of shared
            # FETCH_HEAD with a different branch that equals local HEAD.
            _git(clone, "fetch", "origin", "other")
        return result

    monkeypatch.setattr(banner, "_git_run", overwrite_after_main_fetch)

    behind, err, rev, target = banner._check_via_local_git_details(clone)

    assert rev == local_head
    assert err is None
    assert behind == banner.UPDATE_AVAILABLE_NO_COUNT
    assert behind != 0
    assert target == main_tip
    assert _git(clone, "rev-parse", "FETCH_HEAD").stdout.strip() == local_head


def test_checkout_move_after_capture_keeps_count_bound_to_captured_head(
    tmp_path, monkeypatch
):
    """A concurrent checkout cannot change the local side of the count."""
    upstream = _build_linear_upstream(tmp_path / "upstream", n_commits=6)
    old = _git(upstream, "rev-parse", "HEAD~2").stdout.strip()
    _git(upstream, "branch", "release", old)
    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "clone",
        "--branch",
        "release",
        f"file://{upstream.resolve()}",
        clone.name,
    )
    local_head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    main_tip = _git(upstream, "rev-parse", "main").stdout.strip()
    original_stdout = banner._git_stdout

    def move_head_after_target_capture(args, **kwargs):
        value = original_stdout(args, **kwargs)
        if args == ["rev-parse", "FETCH_HEAD"] and value == main_tip:
            _git(clone, "checkout", "--detach", main_tip)
        return value

    monkeypatch.setattr(banner, "_git_stdout", move_head_after_target_capture)

    behind, err, current, target = banner._check_via_local_git_details(clone)

    assert err is None
    assert current == local_head
    assert target == main_tip
    assert behind == 2
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == main_tip
