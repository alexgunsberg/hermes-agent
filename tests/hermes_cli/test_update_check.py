"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return it without an update check."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    repo_dir = tmp_path / "hermes-agent"
    _git("init", "-b", "main", str(repo_dir))

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 3,
                "ver": __version__,
                "rev": None,
                "repo": str(repo_dir.resolve()),
            }
        )
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(repo_dir))
    with patch("hermes_cli.banner._check_via_local_git") as mock_check:
        result = check_for_updates()

    assert result == 3
    mock_check.assert_not_called()


def test_check_for_updates_prefers_managed_runtime_root_over_packaged_docker_stamp(tmp_path, monkeypatch):
    """A packaged Docker stamp must not suppress the selected runtime checkout."""
    import hermes_cli.banner as banner

    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    runtime_repo = tmp_path / "candidate"
    _git("init", "-b", "main", str(runtime_repo))
    runtime_selector = tmp_path / "hermes-active"
    runtime_selector.symlink_to(runtime_repo, target_is_directory=True)
    packaged_module = tmp_path / "site-packages" / "hermes_cli" / "banner.py"
    packaged_module.parent.mkdir(parents=True)
    packaged_module.touch()

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(runtime_selector))
    monkeypatch.setattr(banner, "__file__", str(packaged_module))

    checked = []
    monkeypatch.setattr(
        banner,
        "_check_via_local_git",
        lambda repo_dir: checked.append(repo_dir) or 7,
    )

    with patch("hermes_cli.config.detect_install_method", return_value="docker"):
        assert banner.check_for_updates() == 7
    assert checked == [runtime_repo.resolve()]


def test_resolve_repo_dir_fails_closed_for_invalid_managed_runtime_root(tmp_path, monkeypatch):
    """An explicit managed runtime root must never fall back to another checkout."""
    import hermes_cli.banner as banner

    hermes_home = tmp_path / "home"
    fallback_repo = hermes_home / "hermes-agent"
    imported_repo = tmp_path / "imported"
    _git("init", "-b", "main", str(fallback_repo))
    _git("init", "-b", "main", str(imported_repo))
    imported_module = imported_repo / "hermes_cli" / "banner.py"
    imported_module.parent.mkdir()
    imported_module.touch()

    dangling_target = tmp_path / "missing-runtime"
    runtime_selector = tmp_path / "hermes-active"
    runtime_selector.symlink_to(dangling_target, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(runtime_selector))
    monkeypatch.setattr(banner, "__file__", str(imported_module))

    assert banner._resolve_repo_dir() is None


def test_resolve_repo_dir_rejects_git_marker_without_a_worktree(tmp_path, monkeypatch):
    """A .git entry alone must not make an explicit runtime root trustworthy."""
    import hermes_cli.banner as banner

    runtime_root = tmp_path / "not-a-worktree"
    runtime_root.mkdir()
    (runtime_root / ".git").mkdir()
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(runtime_root))

    assert banner._resolve_repo_dir() is None


def test_check_for_updates_invalidates_cache_when_runtime_target_changes(tmp_path, monkeypatch):
    """A selector flip must not reuse the previous candidate's cached count."""
    import hermes_cli.banner as banner
    from hermes_cli import __version__

    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    old_repo = tmp_path / "old-candidate"
    new_repo = tmp_path / "new-candidate"
    for repo in (old_repo, new_repo):
        _git("init", "-b", "main", str(repo))
    runtime_selector = tmp_path / "hermes-active"
    runtime_selector.symlink_to(new_repo, target_is_directory=True)
    (hermes_home / ".update_check").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 3,
                "ver": __version__,
                "rev": None,
                "repo": str(old_repo.resolve()),
            }
        )
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(runtime_selector))
    checked = []
    monkeypatch.setattr(
        banner,
        "_check_via_local_git",
        lambda repo_dir: checked.append(repo_dir) or 9,
    )

    assert banner.check_for_updates() == 9
    assert checked == [new_repo.resolve()]


def test_shallow_checkout_reports_exact_bounded_behind_count(tmp_path):
    """Recent shallow installs recover an exact count without unshallowing."""
    from hermes_cli.banner import _check_via_local_git

    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    shallow = tmp_path / "shallow"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(source))
    _git("config", "user.email", "test@example.com", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    (source / "history.txt").write_text("base\n")
    _git("add", "history.txt", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    # Keep more history behind the installed revision than the passive fetch
    # target, so recovering the seven recent commits cannot unshallow the repo.
    for index in range(205):
        _git("commit", "--allow-empty", "-m", f"old {index}", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("clone", "--depth", "1", "--branch", "main", f"file://{remote}", str(shallow))

    for index in range(7):
        with (source / "history.txt").open("a") as handle:
            handle.write(f"{index}\n")
        _git("add", "history.txt", cwd=source)
        _git("commit", "-m", f"commit {index}", cwd=source)
    _git("push", "origin", "main", cwd=source)

    assert _check_via_local_git(shallow) == 7
    assert _git("rev-parse", "--is-shallow-repository", cwd=shallow).stdout.strip() == "true"


def test_unknown_shallow_state_uses_bounded_fetch(tmp_path, monkeypatch):
    """A failed shallow-state probe must never fall through to a full fetch."""
    import hermes_cli.banner as banner

    head_rev = "a" * 40

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/NousResearch/hermes-agent.git"
        if args == ["rev-parse", "--is-shallow-repository"]:
            return None
        if args in (["rev-parse", "HEAD"], ["rev-parse", "--verify", "HEAD^{commit}"]):
            return head_rev
        return None

    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return MagicMock(returncode=1, stdout="")

    monkeypatch.setattr(banner, "_git_stdout", fake_git_stdout)
    monkeypatch.setattr(banner.subprocess, "run", fake_run)

    assert banner._check_via_local_git(tmp_path) is None
    fetch_cmd = next(cmd for cmd in commands if cmd[:2] == ["git", "fetch"])
    assert fetch_cmd[fetch_cmd.index("--depth") + 1] == str(banner._SHALLOW_HISTORY_TARGET)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/acme/hermes-agent.git",
        "file://github.com/NousResearch/hermes-agent.git",
        "github.com/NousResearch/hermes-agent.git",
        "https://github.com:notaport/NousResearch/hermes-agent.git",
        "https://github.com:/NousResearch/hermes-agent.git",
        "https://github.com/NousResearch/hermes-agent.git?redirect=evil",
        "https://github.com/NousResearch/hermes-agent.git#fragment",
        "https://github.com/NousResearch/hermes-agent.git;parameter",
    ],
)
def test_nonofficial_origin_does_not_fallback_to_official_tip(monkeypatch, tmp_path, origin):
    """Fetch failure must not silently change a checkout's upstream identity."""
    import hermes_cli.banner as banner

    def fake_git_stdout(args, **_kwargs):
        if args == ["remote", "get-url", "origin"]:
            return origin
        if args == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        if args == ["rev-parse", "HEAD"]:
            return "a" * 40
        return None

    monkeypatch.setattr(banner, "_git_stdout", fake_git_stdout)
    monkeypatch.setattr(banner, "_fetch_main_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        banner,
        "_check_via_rev",
        lambda _rev: pytest.fail("nonofficial origin must not query official upstream"),
    )

    assert banner._check_via_local_git(tmp_path) is None


def test_ambient_git_config_cannot_redirect_update_fetch(tmp_path, monkeypatch):
    """Process-level Git config injection must not relabel origin/main."""
    import hermes_cli.banner as banner

    origin = tmp_path / "origin.git"
    decoy = tmp_path / "decoy.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    _git("init", "--bare", str(origin))
    _git("init", "--bare", str(decoy))
    _git("init", "-b", "main", str(source))
    _git("config", "user.email", "test@example.com", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    _git("commit", "--allow-empty", "-m", "base", cwd=source)
    _git("remote", "add", "origin", str(origin), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("push", f"file://{decoy}", "main", cwd=source)
    _git("clone", f"file://{origin}", str(checkout))
    _git("commit", "--allow-empty", "-m", "upstream update", cwd=source)
    _git("push", "origin", "main", cwd=source)

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{decoy}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"file://{origin}")

    assert banner._check_via_local_git(checkout) == 1


def test_git_graph_modifiers_are_removed_from_update_environment(monkeypatch):
    """Ambient namespace/shallow overrides must not alter the checked graph."""
    import hermes_cli.banner as banner

    hostile = {
        "GIT_NAMESPACE": "attacker",
        "GIT_SHALLOW_FILE": "/tmp/attacker-shallow",
        "GIT_GRAFT_FILE": "/tmp/attacker-grafts",
        "GIT_REPLACE_REF_BASE": "refs/attacker/replace/",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_QUARANTINE_PATH": "/tmp/attacker-objects",
        "GIT_IMPLICIT_WORK_TREE": "0",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    env = banner._sanitized_git_env()

    assert hostile.keys().isdisjoint(env)


def test_concurrent_fetch_head_overwrite_does_not_change_main_snapshot(tmp_path):
    """A second real fetch cannot relabel the main commit captured by the check."""
    import hermes_cli.banner as banner

    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    shallow = tmp_path / "shallow"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(source))
    _git("config", "user.email", "test@example.com", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    (source / "history.txt").write_text("base\n")
    _git("add", "history.txt", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("branch", "feature", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "-u", "origin", "main", "feature", cwd=source)
    _git("clone", "--depth", "1", "--branch", "main", f"file://{remote}", str(shallow))

    with (source / "history.txt").open("a") as handle:
        handle.write("main update\n")
    _git("add", "history.txt", cwd=source)
    _git("commit", "-m", "main update", cwd=source)
    _git("push", "origin", "main", cwd=source)

    real_run = subprocess.run
    overwrote_fetch_head = False

    def overwrite_after_main_fetch(cmd, **kwargs):
        nonlocal overwrote_fetch_head
        result = real_run(cmd, **kwargs)
        if (
            not overwrote_fetch_head
            and result.returncode == 0
            and cmd[:2] == ["git", "fetch"]
            and any(arg == "main" or arg.startswith("main:") for arg in cmd[2:])
        ):
            overwrite = real_run(
                ["git", "fetch", "origin", "feature", "--quiet"],
                capture_output=True,
                timeout=30,
                cwd=kwargs["cwd"],
            )
            assert overwrite.returncode == 0
            overwrote_fetch_head = True
        return result

    with patch("hermes_cli.banner.subprocess.run", side_effect=overwrite_after_main_fetch):
        assert banner._check_via_local_git(shallow) == 1

    assert overwrote_fetch_head
    assert _git("rev-parse", "FETCH_HEAD", cwd=shallow).stdout == _git("rev-parse", "HEAD", cwd=shallow).stdout


def test_runtime_root_validation_ignores_ambient_git_repository_selection(
    tmp_path, monkeypatch
):
    """GIT_DIR/GIT_WORK_TREE must not turn an arbitrary selector into a repo."""
    import hermes_cli.banner as banner

    real_repo = tmp_path / "real"
    impostor = tmp_path / "impostor"
    _git("init", "-b", "main", str(real_repo))
    impostor.mkdir()
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(impostor))
    monkeypatch.setenv("GIT_DIR", str(real_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(impostor))

    assert banner._resolve_repo_dir() is None


def test_invalid_runtime_root_overrides_embedded_revision(tmp_path, monkeypatch):
    """An explicit invalid selector fails closed even when HERMES_REVISION exists."""
    import hermes_cli.banner as banner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("HERMES_REVISION", "a" * 40)
    monkeypatch.setattr(
        banner,
        "_check_via_rev",
        lambda _rev: pytest.fail("embedded revision must not bypass selector authority"),
    )

    assert banner.check_for_updates() is None


def test_invalid_runtime_root_does_not_reuse_legacy_cache(tmp_path, monkeypatch):
    """A legacy cache without repo identity cannot validate an explicit selector."""
    import hermes_cli.banner as banner
    from hermes_cli import __version__

    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    (hermes_home / ".update_check").write_text(
        json.dumps(
            {
                "ts": time.time(),
                "behind": 9,
                "ver": __version__,
                "rev": None,
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_RUNTIME_ROOT", str(tmp_path / "missing"))

    assert banner.check_for_updates() is None


def test_failed_snapshot_fetch_cleans_created_temporary_ref(tmp_path, monkeypatch):
    """A fetch that writes its ref then reports failure must not leak the ref."""
    import hermes_cli.banner as banner

    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(source))
    _git("config", "user.email", "test@example.com", cwd=source)
    _git("config", "user.name", "Test", cwd=source)
    _git("commit", "--allow-empty", "-m", "base", cwd=source)
    _git("remote", "add", "origin", str(remote), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    _git("clone", str(remote), str(checkout))

    real_run = subprocess.run

    def report_failure_after_fetch(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        if cmd[:2] == ["git", "fetch"] and result.returncode == 0:
            return subprocess.CompletedProcess(
                result.args, 1, stdout=result.stdout, stderr=result.stderr
            )
        return result

    monkeypatch.setattr(banner.subprocess, "run", report_failure_after_fetch)
    assert banner._fetch_main_snapshot(checkout, bounded=False) is None

    refs = real_run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/hermes/update-check"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert refs == ""


def test_official_ssh_remote_mismatch_has_unknown_count(monkeypatch, tmp_path):
    """An unclassified SHA mismatch is not proof of exactly one commit behind."""
    import hermes_cli.banner as banner

    monkeypatch.setattr(
        banner,
        "_git_stdout",
        lambda args, **_kwargs: (
            "git@github.com:NousResearch/hermes-agent.git"
            if args == ["remote", "get-url", "origin"]
            else "a" * 40
        ),
    )
    monkeypatch.setattr(
        banner, "_check_via_rev", lambda _rev: banner.UPDATE_AVAILABLE_NO_COUNT
    )

    assert banner._check_via_local_git(tmp_path) == banner.UPDATE_AVAILABLE_NO_COUNT



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




