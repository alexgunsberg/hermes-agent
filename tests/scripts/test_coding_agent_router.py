"""Offline tests for scripts/coding_agent_router.py.

Stub profiles stand in for the paid delegate CLIs so the full lifecycle —
run → protected verify → same-session repair → reroute-once → stop — plus the
process-group supervision and the JSONL task record are all provable without
credentials or network.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import scripts.coding_agent_router as router


PACKET = router.Packet(
    name="make-hello",
    prompt="Create hello.txt containing hi.",
    files={"seed.txt": "seed\n", "test_check.sh": "grep -q hi hello.txt\n"},
    verify=["bash test_check.sh"],
    protected_files=["test_check.sh"],
    task_class="bounded-tooling",
)


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _stub(tmp_path, name, run_body, repair_body=None):
    run = _script(tmp_path, f"{name}-run.py", run_body)
    repair = _script(tmp_path, f"{name}-repair.py", repair_body or run_body)
    return router.stub_profile(
        name,
        f"{sys.executable} {run} {{prompt}}",
        f"{sys.executable} {repair} {{prompt}}",
    )


SOLVE = "open('hello.txt', 'w').write('hi\\n')\n"
NOOP = "pass\n"


def test_packet_digest_is_stable_and_input_sensitive():
    a = PACKET.digest()
    assert a == PACKET.digest()
    other = router.Packet(**{**PACKET.__dict__, "prompt": "different"})
    assert other.digest() != a


def test_accept_on_first_attempt(tmp_path):
    profile = _stub(tmp_path, "good", SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(
        PACKET, {}, log, workdir_root=tmp_path / "runs",
        route_override=[profile])
    assert summary["accepted"] is True
    assert summary["attempts"] == 1
    records = [json.loads(l) for l in log.read_text().splitlines()]
    attempt = records[0]
    assert attempt["repair_used"] is False
    assert attempt["rerouted"] is False
    assert attempt["failure_class"] == "none"
    # Durable record carries identity + provenance fields.
    for key in ("packet_hash", "task_class", "model", "resolved_version",
                "session_id", "elapsed_s", "usage"):
        assert key in attempt


def test_same_session_repair_recovers(tmp_path):
    # Run fails acceptance; repair (with failure evidence in the prompt) fixes.
    profile = _stub(tmp_path, "flaky", NOOP, SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(
        PACKET, {}, log, workdir_root=tmp_path / "runs",
        route_override=[profile])
    assert summary["accepted"] is True
    attempt = json.loads(log.read_text().splitlines()[0])
    assert attempt["repair_used"] is True
    assert attempt["accepted"] is True
    assert summary["attempts"] == 1  # no reroute needed


def test_repair_prompt_carries_failure_evidence(tmp_path):
    # The repair stub records its argv; the evidence must mention the verify
    # command that failed.
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
    bad = _stub(tmp_path, "bad", NOOP, NOOP)  # fails run AND repair
    good = _stub(tmp_path, "good", SOLVE)
    log = tmp_path / "log.jsonl"
    summary = router.route_packet(
        PACKET, {}, log, workdir_root=tmp_path / "runs",
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
    assert summary["attempts"] == 2  # reroute exactly once, then stop


def test_protected_verification_defeats_test_tampering(tmp_path):
    # Candidate "cheats": rewrites the acceptance script to always pass but
    # never produces hello.txt. Restoration from the packet must catch it.
    cheat = "open('test_check.sh', 'w').write('true\\n')\n"
    profile = _stub(tmp_path, "cheater", cheat, cheat)
    summary = router.route_packet(
        PACKET, {}, tmp_path / "log.jsonl", workdir_root=tmp_path / "runs",
        route_override=[profile])
    assert summary["accepted"] is False
    attempt = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert attempt["failure_class"] == "verify-failed"


def test_timeout_kills_whole_process_group(tmp_path):
    # The stub spawns a long-lived child, then hangs. Supervision must kill
    # both the stub and its descendant on timeout.
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
    child_pid = int(marker.read_text())
    # The descendant must be gone (or a zombie reparented to init, not running).
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        # Still visible — confirm it is not actually running `sleep`.
        cmdline = Path(f"/proc/{child_pid}/cmdline")
        if not cmdline.exists() or b"sleep" not in cmdline.read_bytes():
            break
        time.sleep(0.1)
    else:
        pytest.fail("descendant survived the process-group kill")


def test_spawn_error_is_classified(tmp_path):
    profile = router.stub_profile(
        "ghost", "definitely-not-a-real-binary-xyz {prompt}")
    summary = router.route_packet(
        PACKET, {}, tmp_path / "log.jsonl", workdir_root=tmp_path / "runs",
        route_override=[profile])
    attempt = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert attempt["failure_class"] == "spawn-error"
    assert attempt["repair_used"] is False  # nothing to resume
    assert summary["accepted"] is False


def test_routing_table_selection_and_preferred_override(monkeypatch):
    profiles = router.builtin_profiles()
    for prof in profiles.values():
        monkeypatch.setattr(prof, "availability", lambda: None)
    packet = router.Packet(**{**PACKET.__dict__, "task_class": "small-iteration"})
    route, skipped = router.select_route(packet, profiles)
    assert [p.name for p in route] == ["cursor-composer", "grok-lean-high"]
    packet = router.Packet(**{**packet.__dict__,
                              "preferred_profile": "grok-lean-high"})
    route, _ = router.select_route(packet, profiles)
    assert route[0].name == "grok-lean-high"
    assert len(route) == 2


def test_unavailable_profiles_are_skipped_with_reason():
    profiles = router.builtin_profiles()
    packet = router.Packet(**{**PACKET.__dict__, "task_class": "default"})
    route, skipped = router.select_route(packet, profiles)
    # In a credential-less environment nothing is routable — but every skip
    # carries its reason instead of stalling on a login prompt.
    for name, reason in skipped.items():
        assert reason
    assert len(route) + len(skipped) >= 2


def test_grok_profile_is_version_gated_bounded_autonomy():
    prof = router.builtin_profiles()["grok-lean-high"]
    argv = prof.run_argv("do it", "sid-1")
    # Bounded-autonomy invariants for the 0.2.109 profile:
    for flag in ("--no-auto-update", "--always-approve", "--max-turns",
                 "--no-subagents", "--no-memory", "--disable-web-search"):
        assert flag in argv, flag
    assert argv[argv.index("-m") + 1] == "grok-lean"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    # Removed in grok 0.2.109 — must NOT be emitted:
    assert "--check" not in argv
    assert "--best-of-n" not in argv
    repair = prof.repair_argv("fix it", "sid-1")
    assert repair[repair.index("-r") + 1] == "sid-1"


def test_cursor_profile_pins_model_and_format():
    prof = router.builtin_profiles()["cursor-grok-high-fast-off"]
    argv = prof.run_argv("do it", "sid-1")
    assert argv[argv.index("-m") + 1] == "grok-4.5[effort=high,fast=false]"
    assert "--force" in argv and "--output-format" in argv
    repair = prof.repair_argv("fix it", "sid-1")
    assert "--continue" in repair


def test_cli_end_to_end(tmp_path):
    solver = _script(tmp_path, "solver.py", SOLVE)
    packet_file = tmp_path / "packet.json"
    packet_file.write_text(json.dumps({
        "name": PACKET.name, "prompt": PACKET.prompt, "files": PACKET.files,
        "verify": PACKET.verify, "protected_files": PACKET.protected_files,
        "task_class": PACKET.task_class,
    }), encoding="utf-8")
    log = tmp_path / "log.jsonl"
    rc = router.main([
        "--packet", str(packet_file), "--log", str(log),
        "--stub-run", f"{sys.executable} {solver} {{prompt}}",
        "--route", "stub",
    ])
    assert rc == 0
    records = [json.loads(l) for l in log.read_text().splitlines()]
    assert records[-1]["type"] == "summary"
    assert records[-1]["accepted"] is True
