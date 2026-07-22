"""Tests for tools/code_routing.py — the general coding-agent routing engine.

Stub profiles stand in for the paid delegate CLIs so the full lifecycle —
run → protected verify → same-session repair → reroute-once → stop — plus
path containment, repo@SHA workspaces, scope contracts, risk policy,
supervision, and the JSONL record are all provable without credentials.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import code_routing as router


PACKET = router.Packet(
    name="make-hello",
    prompt="Create hello.txt containing hi.",
    files={"seed.txt": "seed\n", "test_check.sh": "grep -q hi hello.txt\n"},
    verify=["bash test_check.sh"],
    protected_files=["test_check.sh"],
    task_class="bounded-tooling",
)

SOLVE = "open('hello.txt', 'w').write('hi\\n')\n"
NOOP = "pass\n"


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _stub(tmp_path, name, run_body, repair_body=None, sandbox=None):
    run = _script(tmp_path, f"{name}-run.py", run_body)
    repair = _script(tmp_path, f"{name}-repair.py", repair_body or run_body)
    return router.stub_profile(
        name,
        f"{sys.executable} {run} {{prompt}}",
        f"{sys.executable} {repair} {{prompt}}",
        sandbox=sandbox,
    )


def _clone_packet(**overrides):
    base = dict(PACKET.__dict__)
    base.update(overrides)
    return router.Packet(**base)


# ── Packet identity & validation ────────────────────────────────────────────


def test_digest_covers_every_semantic_field():
    base = PACKET.digest()
    for change in (
        {"prompt": "other"},
        {"task_class": "ui-domain"},
        {"risk": "high"},
        {"expected_duration_s": 60},
        {"preferred_profile": "grok-high"},
        {"name": "other-name"},
        {"allowed_paths": ["src/"]},
        {"repo": "/some/repo"},
        {"base_sha": "deadbeef"},
    ):
        assert _clone_packet(**change).digest() != base, change


def test_packet_rejects_unknown_keys_and_missing_verify():
    with pytest.raises(ValueError):
        router.Packet.from_dict({"name": "x", "prompt": "y", "verify": ["true"],
                                 "bogus_key": 1})
    with pytest.raises(ValueError):
        router.Packet(name="x", prompt="y", verify=[])


@pytest.mark.parametrize("bad", [
    "/etc/passwd",
    "C:\\Windows\\evil.txt",
    "..",
    "../sibling.txt",
    "a/../../escape.txt",
    "a/./b.txt",
    "",
    "a\\b.txt",
])
def test_adversarial_packet_paths_rejected(bad):
    with pytest.raises((router.PacketPathError, ValueError)):
        router.Packet(name="x", prompt="y", verify=["true"],
                      files={bad: "boom"})


def test_symlink_escape_is_refused(tmp_path):
    # A cloned repo may legitimately contain a symlinked directory pointing
    # outside the workspace; packet writes must not follow it out.
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "esc").symlink_to(outside, target_is_directory=True)
    with pytest.raises(router.PacketPathError):
        router.write_packet_file(ws, "esc/x.txt", "boom")
    assert not (outside / "x.txt").exists()


def test_write_through_symlinked_file_is_refused(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link.txt").symlink_to(victim)
    with pytest.raises(router.PacketPathError):
        router.write_packet_file(ws, "link.txt", "boom")
    assert victim.read_text() == "original"


# ── Workspaces: synthetic seed and repo@SHA ────────────────────────────────


def _make_repo(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env,
                              capture_output=True, text=True)
    git("init", "-q")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-qm", "one")
    sha_one = git("rev-parse", "HEAD").stdout.strip()
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-qm", "two")
    return repo, sha_one


def test_repo_packet_runs_against_exact_base_sha(tmp_path):
    origin, sha_one = _make_repo(tmp_path)
    packet = router.Packet(
        name="bump-value",
        prompt="Set VALUE to 41 in app.py.",
        repo=str(origin),
        base_sha=sha_one,
        allowed_paths=["app.py"],
        verify=["python -c \"import app; assert app.VALUE == 41\""],
    )
    profile = _stub(tmp_path, "solver",
                    "open('app.py', 'w').write('VALUE = 41\\n')\n")
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(packet, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is True
    # The original checkout is untouched (still at commit two).
    assert (origin / "app.py").read_text() == "VALUE = 2\n"
    # The attempt workspace was based on the exact base SHA before the edit.
    workdir = Path(summary["final_workdir"])
    seed_parent = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD~1"],
        capture_output=True, text=True).stdout.strip()
    assert seed_parent == sha_one


def test_repo_packet_with_bad_base_sha_is_packet_error(tmp_path):
    origin, _ = _make_repo(tmp_path)
    packet = router.Packet(
        name="bad-sha", prompt="x", repo=str(origin),
        base_sha="0" * 40, verify=["true"])
    profile = _stub(tmp_path, "never-runs", SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(packet, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is False
    attempt = json.loads(log.read_text().splitlines()[0])
    assert attempt["failure_class"] == "packet-error"
    assert "does not resolve" in attempt["detail"]
    # A bad packet fails identically everywhere — no reroute happened.
    assert summary["attempts"] == 1


def test_scope_contract_rejects_out_of_scope_changes(tmp_path):
    origin, sha_one = _make_repo(tmp_path)
    packet = router.Packet(
        name="scope", prompt="x", repo=str(origin), base_sha=sha_one,
        allowed_paths=["app.py"],
        verify=["true"])
    cheat = ("open('app.py','w',encoding='utf-8').write('VALUE = 41\\n'); "
             "open('rogue.txt','w',encoding='utf-8').write('x')\n")
    profile = _stub(tmp_path, "rogue", cheat, cheat)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(packet, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is False
    attempt = json.loads(log.read_text().splitlines()[0])
    assert "scope violation" in attempt["detail"]
    assert "rogue.txt" in attempt["detail"]


# ── Lifecycle ───────────────────────────────────────────────────────────────


def test_accept_on_first_attempt_with_full_record(tmp_path):
    profile = _stub(tmp_path, "good", SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(PACKET, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is True
    attempt = json.loads(log.read_text().splitlines()[0])
    assert attempt["repair_used"] is False
    assert attempt["failure_class"] == "none"
    assert [p["phase"] for p in attempt["phases"]] == ["run"]
    for key in ("packet_hash", "task_class", "risk", "model", "sandbox",
                "resolved_version", "session_id", "elapsed_s"):
        assert key in attempt


def test_same_session_repair_recovers_and_is_recorded_per_phase(tmp_path):
    profile = _stub(tmp_path, "flaky", NOOP, SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(PACKET, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is True
    attempt = json.loads(log.read_text().splitlines()[0])
    assert attempt["repair_used"] is True
    assert [p["phase"] for p in attempt["phases"]] == ["run", "repair"]
    assert summary["attempts"] == 1


def test_failed_repair_with_nonzero_exit_is_agent_error(tmp_path):
    profile = _stub(tmp_path, "worse", NOOP, "import sys; sys.exit(3)\n")
    log = tmp_path / "log.jsonl"
    router.route_packet(PACKET, {}, log, workdir_root=tmp_path / "runs",
                        route_override=[profile])
    attempt = json.loads(log.read_text().splitlines()[0])
    # The REPAIR phase's outcome drives classification, not the first run's.
    assert attempt["failure_class"] == "agent-error"
    assert attempt["phases"][-1]["returncode"] == 3


def test_repair_prompt_carries_failure_evidence(tmp_path):
    evidence_file = tmp_path / "seen-prompt.txt"
    repair_body = (
        "import sys\n"
        f"open({str(evidence_file)!r}, 'w').write(sys.argv[1])\n" + SOLVE
    )
    profile = _stub(tmp_path, "witness", NOOP, repair_body)
    router.route_packet(PACKET, {}, tmp_path / "log.jsonl",
                        workdir_root=tmp_path / "runs",
                        route_override=[profile])
    seen = evidence_file.read_text()
    assert "verify command failed" in seen
    assert "test_check.sh" in seen


def test_reroute_once_then_stop(tmp_path):
    bad = _stub(tmp_path, "bad", NOOP, NOOP)
    good = _stub(tmp_path, "good", SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(PACKET, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[bad, good])
    assert summary["accepted"] is True
    assert summary["attempts"] == 2
    first, second = [json.loads(l) for l in log.read_text().splitlines()[:2]]
    assert first["accepted"] is False and first["repair_used"] is True
    assert second["rerouted"] is True and second["accepted"] is True


def test_both_routes_fail_stops_after_two(tmp_path):
    bad1 = _stub(tmp_path, "bad1", NOOP, NOOP)
    bad2 = _stub(tmp_path, "bad2", NOOP, NOOP)
    summary = router.route_packet(
        PACKET, {}, tmp_path / "log.jsonl", workdir_root=tmp_path / "runs",
        route_override=[bad1, bad2])
    assert summary["accepted"] is False
    assert summary["attempts"] == 2


def test_protected_verification_defeats_test_tampering(tmp_path):
    cheat = "open('test_check.sh', 'w').write('true\\n')\n"
    profile = _stub(tmp_path, "cheater", cheat, cheat)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(PACKET, {}, log,
                                  workdir_root=tmp_path / "runs",
                                  route_override=[profile])
    assert summary["accepted"] is False
    attempt = json.loads(log.read_text().splitlines()[0])
    assert attempt["failure_class"] == "verify-failed"


def test_spawn_error_is_classified_and_not_repaired(tmp_path):
    profile = router.stub_profile(
        "ghost", "definitely-not-a-real-binary-xyz {prompt}")
    summary = router.route_packet(
        PACKET, {}, tmp_path / "log.jsonl", workdir_root=tmp_path / "runs",
        route_override=[profile])
    attempt = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert attempt["failure_class"] == "spawn-error"
    assert attempt["repair_used"] is False
    assert summary["accepted"] is False


# ── Supervision ─────────────────────────────────────────────────────────────


def test_timeout_kills_whole_process_group(tmp_path):
    marker = tmp_path / "child.pid"
    body = (
        "import subprocess, time, sys\n"
        "child = subprocess.Popen(['sleep', '120'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    script = _script(tmp_path, "hang.py", body)
    outcome = router.run_supervised(
        [sys.executable, str(script), "x"], tmp_path, timeout=2)
    assert outcome.state == "timeout"
    _assert_pid_reaped(int(marker.read_text()))


@pytest.mark.live_system_guard_bypass  # reaps its own reparented `sleep`, which the guard cannot attribute
def test_descendant_outliving_parent_is_reaped(tmp_path):
    # Parent exits immediately, leaving a detached (setsid) descendant that a
    # process-group signal cannot reach — the tracker must reap it.
    marker = tmp_path / "child.pid"
    body = (
        "import subprocess, os, sys, time\n"
        "child = subprocess.Popen(['sleep', '120'], start_new_session=True,\n"
        "                         stdout=subprocess.DEVNULL,\n"
        "                         stderr=subprocess.DEVNULL)\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(1)\n"  # give the tracker a snapshot window
    )
    script = _script(tmp_path, "leaver.py", body)
    outcome = router.run_supervised(
        [sys.executable, str(script), "x"], tmp_path, timeout=30)
    assert outcome.state == "completed"
    assert outcome.orphans_killed is True
    _assert_pid_reaped(int(marker.read_text()))


def _assert_pid_reaped(child_pid: int):
    psutil = pytest.importorskip("psutil")
    for _ in range(30):
        if not psutil.pid_exists(child_pid):
            return
        try:
            child = psutil.Process(child_pid)
            if child.status() == psutil.STATUS_ZOMBIE or \
                    "sleep" not in " ".join(child.cmdline()):
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.1)
    pytest.fail("descendant survived supervision")


# ── Profiles & routing policy ──────────────────────────────────────────────


def test_grok_profile_catalog_valid_bounded_and_version_gated(monkeypatch):
    monkeypatch.delenv("ROUTER_GROK_MODEL", raising=False)
    monkeypatch.delenv("ROUTER_GROK_SANDBOX", raising=False)
    prof = router.builtin_profiles()["grok-high"]
    argv = prof.run_argv("do it", "sid-1")
    assert argv[argv.index("-m") + 1] == "grok-4.5"  # catalog-valid default
    for flag in ("--no-auto-update", "--always-approve", "--max-turns",
                 "--no-subagents", "--no-memory", "--disable-web-search"):
        assert flag in argv, flag
    assert "--check" not in argv and "--best-of-n" not in argv  # removed in 0.2.109
    repair = prof.repair_argv("fix it", "sid-1")
    assert repair[repair.index("-r") + 1] == "sid-1"


def test_grok_sandbox_is_argv_invariant_and_fails_closed(monkeypatch):
    monkeypatch.setenv("ROUTER_GROK_SANDBOX", "strict")
    prof = router.builtin_profiles()["grok-high"]
    argv = prof.run_argv("do it", "sid")
    assert argv[argv.index("--sandbox") + 1] == "strict"
    # Binary that can't enforce the sandbox -> ineligible, not silently open.
    monkeypatch.setattr(router, "_binary_supports_flag", lambda b, f: False)
    reason = prof.config_check()
    assert reason and "failing closed" in reason


def test_cursor_frontier_fails_closed_without_explicit_model(monkeypatch):
    monkeypatch.delenv("ROUTER_CURSOR_FRONTIER_MODEL", raising=False)
    prof = router.builtin_profiles()["cursor-frontier"]
    reason = prof.config_check()
    assert reason and "ROUTER_CURSOR_FRONTIER_MODEL" in reason
    monkeypatch.setenv("ROUTER_CURSOR_FRONTIER_MODEL", "opus-4.8")
    prof = router.builtin_profiles()["cursor-frontier"]
    assert prof.config_check() is None
    argv = prof.run_argv("x", "sid")
    assert argv[argv.index("-m") + 1] == "opus-4.8"


def test_high_risk_requires_sandbox_enforcing_profile(monkeypatch):
    profiles = {
        "grok-high": router.stub_profile("grok-high", "true {prompt}"),
        "cursor-grok-high-fast-off": router.stub_profile(
            "cursor-grok-high-fast-off", "true {prompt}"),
    }
    packet = _clone_packet(risk="high", task_class="high-risk")
    route, skipped = router.select_route(packet, profiles)
    assert route == []
    assert all("sandbox" in reason for reason in skipped.values())
    # With a sandbox-bearing profile, high risk routes.
    profiles["grok-high"] = router.stub_profile(
        "grok-high", "true {prompt}", sandbox="strict")
    route, _ = router.select_route(packet, profiles)
    assert [p.name for p in route] == ["grok-high"]


def test_routing_table_selection_and_preferred_override(monkeypatch):
    profiles = router.builtin_profiles()
    for prof in profiles.values():
        monkeypatch.setattr(prof, "availability", lambda: None)
    packet = _clone_packet(task_class="small-iteration")
    route, _ = router.select_route(packet, profiles)
    assert [p.name for p in route] == ["cursor-composer", "grok-high"]
    packet = _clone_packet(task_class="small-iteration",
                           preferred_profile="grok-high")
    route, _ = router.select_route(packet, profiles)
    assert route[0].name == "grok-high"


def test_unavailable_profiles_are_skipped_with_reason():
    profiles = router.builtin_profiles()
    route, skipped = router.select_route(_clone_packet(task_class="default"),
                                         profiles)
    for reason in skipped.values():
        assert reason
    assert len(route) + len(skipped) >= 2


# ── JSONL log location ─────────────────────────────────────────────────────


def test_default_log_path_lives_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    assert router.default_log_path() == tmp_path / "hh" / "router" / "routes.jsonl"
