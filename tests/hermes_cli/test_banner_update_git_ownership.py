"""Tests for ownership-safe local git update checks in hermes_cli.banner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli.banner import (
    UPDATE_AVAILABLE_NO_COUNT,
    _check_via_local_git_details,
    _classify_git_stderr,
    repo_install_writable,
)


def test_classify_git_stderr_ownership_and_permission():
    assert (
        _classify_git_stderr(
            "fatal: detected dubious ownership in repository at '/opt/hermes-agent'"
        )
        == "git-ownership"
    )
    assert _classify_git_stderr("error: cannot open '.git/FETCH_HEAD': Permission denied") == (
        "git-permission"
    )
    assert _classify_git_stderr("fatal: unable to access 'https://...': Could not resolve host") == (
        "offline"
    )
    assert _classify_git_stderr("fatal: Unable to create '/tmp/x/.git/shallow.lock': File exists.") == (
        "check-failed"
    )


def test_repo_install_writable_false_when_root_not_writable(tmp_path, monkeypatch):
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr("hermes_cli.banner.os.access", lambda path, mode: False)
    assert repo_install_writable(repo) is False


def test_check_via_local_git_falls_back_to_ls_remote_when_fetch_blocked(tmp_path, monkeypatch):
    """Root-owned / non-writable .git: read HEAD with process-local safe.directory,
    skip fetch writes, compare via ls-remote. Never touch global safe.directory.
    """
    import hermes_cli.banner as banner

    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    (repo / ".git").mkdir()

    head_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    upstream_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""

        # Process-local ownership relax for read-only ops only.
        if cmd[:1] == ["git"] and "-c" in cmd:
            cfg = cmd[cmd.index("-c") + 1]
            assert cfg.startswith("safe.directory=")
            assert "safe.directory" not in "".join(
                c for c in cmd if c.startswith("--global")
            )

        if "rev-parse" in cmd and "HEAD" in cmd and "FETCH_HEAD" not in cmd:
            completed.stdout = head_sha + "\n"
            return completed
        if "remote" in cmd and "get-url" in cmd:
            completed.stdout = "https://github.com/NousResearch/hermes-agent.git\n"
            return completed
        if "rev-parse" in cmd and "--is-shallow-repository" in cmd:
            completed.stdout = "false\n"
            return completed
        if "fetch" in cmd:
            completed.returncode = 128
            completed.stderr = (
                "error: cannot open '.git/FETCH_HEAD': Permission denied\n"
            )
            return completed
        if "ls-remote" in cmd:
            completed.stdout = f"{upstream_sha}\trefs/heads/main\n"
            return completed

        completed.returncode = 1
        completed.stderr = "unexpected"
        return completed

    monkeypatch.setattr(banner.subprocess, "run", fake_run)

    behind, error_code, current_revision, _ = _check_via_local_git_details(repo)

    assert behind == UPDATE_AVAILABLE_NO_COUNT
    assert error_code is None
    assert current_revision == head_sha

    fetch_cmds = [c for c in calls if "fetch" in c]
    assert fetch_cmds, "expected a fetch attempt"
    for cmd in fetch_cmds:
        # Fetch must not paper over ownership with -c safe.directory.
        assert "-c" not in cmd

    read_cmds = [c for c in calls if "rev-parse" in c and "HEAD" in c]
    assert any("-c" in c for c in read_cmds)

    # Never mutate global git config in this path.
    assert not any("config" in c and "--global" in c for c in calls)


def test_check_via_local_git_reports_ownership_when_head_unreadable(tmp_path, monkeypatch):
    import hermes_cli.banner as banner

    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_run(cmd, **kwargs):
        completed = MagicMock()
        completed.returncode = 128
        completed.stdout = ""
        completed.stderr = (
            "fatal: detected dubious ownership in repository at "
            f"'{repo}'\n"
        )
        return completed

    monkeypatch.setattr(banner.subprocess, "run", fake_run)

    behind, error_code, current_revision, _ = _check_via_local_git_details(repo)
    assert behind is None
    assert error_code == "git-ownership"
    assert current_revision is None
