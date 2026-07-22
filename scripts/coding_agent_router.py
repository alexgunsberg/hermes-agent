#!/usr/bin/env python3
"""Direct-CLI coding-agent router for Hermes.

Takes an immutable work packet (objective, seeded files, acceptance commands),
selects a delegate profile by task class, runs the harness headless in an
isolated scratch git repo under process-group supervision, verifies the result
deterministically OUTSIDE candidate control, performs at most one same-session
repair with the exact failure evidence, reroutes the identical packet once to
the fallback profile, then stops. Every attempt is appended to a durable JSONL
task record.

Deliberately NOT here: ACP transports, attestation frameworks, benchmark
suites (see ``scripts/coding_agent_bench.py``), or UI. Two direct adapters +
repair/reroute + JSONL + supervision — nothing else.

Usage:
    python scripts/coding_agent_router.py --packet packet.json --log routes.jsonl
    python scripts/coding_agent_router.py --packet packet.json --route grok-lean-high,cursor-grok-high-fast-off

Packet JSON:
    {
      "name": "fix-retry-logic",
      "task_class": "bounded-tooling",       // see ROUTING_TABLE
      "risk": "medium",                       // low | medium | high
      "expected_duration_s": 300,
      "prompt": "Fix ... run the tests ...",
      "files": {"pager.py": "...", "test_pager.py": "..."},
      "verify": ["python test_pager.py"],
      "protected_files": ["test_pager.py"],  // restored from the packet before every verification
      "preferred_profile": null               // optional explicit primary
    }

Only stdlib is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Work packet
# ---------------------------------------------------------------------------


@dataclass
class Packet:
    name: str
    prompt: str
    files: dict
    verify: list
    task_class: str = "default"
    risk: str = "medium"
    expected_duration_s: int = 600
    protected_files: list = field(default_factory=list)
    preferred_profile: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Packet":
        missing = {"name", "prompt", "files", "verify"} - set(d)
        if missing:
            raise ValueError(f"packet missing keys: {sorted(missing)}")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def digest(self) -> str:
        """Hash of the immutable inputs — identifies the packet across
        attempts and reroutes, and pins what the candidate must not alter."""
        canonical = json.dumps(
            {
                "prompt": self.prompt,
                "files": self.files,
                "verify": self.verify,
                "protected_files": sorted(self.protected_files),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Profiles — versioned, explicit; no plain-default invocations
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """A named harness+model configuration.

    ``run_argv``/``repair_argv`` receive (prompt, session_id) and return the
    full argv. ``repair_argv`` must resume the SAME session the run created.
    """

    name: str
    harness: str  # grok | cursor | stub
    model: str
    binary: str
    run_argv: "callable"
    repair_argv: "callable"
    version_argv: list = field(default_factory=list)
    auth_check: "callable" = staticmethod(lambda: None)

    def availability(self) -> str | None:
        if shutil.which(self.binary) is None:
            return f"binary `{self.binary}` not found on PATH"
        return self.auth_check()

    def resolved_version(self) -> str:
        if not self.version_argv:
            return "unversioned"
        try:
            out = subprocess.run(
                self.version_argv, capture_output=True, text=True, timeout=30,
            )
            return (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception as exc:  # pragma: no cover - defensive
            return f"version-check-failed: {exc}"


def _grok_auth() -> str | None:
    if os.environ.get("XAI_API_KEY"):
        return None
    if (Path.home() / ".grok" / "auth.json").exists():
        return None
    return "no ~/.grok/auth.json and XAI_API_KEY unset (run `grok login --device-code`)"


def _cursor_auth() -> str | None:
    if os.environ.get("CURSOR_API_KEY"):
        return None
    try:
        probe = subprocess.run(
            ["cursor-agent", "status"], capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return f"cursor-agent status failed: {exc}"
    if probe.returncode == 0 and "not" not in probe.stdout.lower():
        return None
    return "CURSOR_API_KEY unset and `cursor-agent status` reports no login"


# Verified against grok 0.2.109: --check / --best-of-n are GONE (existed in
# 0.2.106). The lean profile is bounded autonomy — turn cap, no subagents, no
# memory, no web, JSON terminal output. Sandbox profile is passed through
# GROK_SANDBOX (env) when the operator sets one; there is no unsandboxed
# fallback flag added here.
def _grok_common(model: str, effort: str) -> list:
    return [
        "grok", "--no-auto-update", "--always-approve",
        "-m", model, "--reasoning-effort", effort,
        "--max-turns", "40", "--no-subagents", "--no-memory",
        "--disable-web-search", "--output-format", "json",
    ]


def _grok_profile(name: str, model: str, effort: str = "high") -> Profile:
    return Profile(
        name=name,
        harness="grok",
        model=f"{model}[effort={effort}]",
        binary="grok",
        run_argv=lambda prompt, sid: [
            *_grok_common(model, effort), "-s", sid, "-p", prompt,
        ],
        repair_argv=lambda prompt, sid: [
            *_grok_common(model, effort), "-r", sid, "-p", prompt,
        ],
        version_argv=["grok", "--version"],
        auth_check=_grok_auth,
    )


def _cursor_profile(name: str, model: str) -> Profile:
    # cursor-agent has no session-id flag for new sessions; repair uses
    # --continue, which resumes the most recent session for the workdir —
    # correct here because each packet attempt gets its own scratch repo.
    return Profile(
        name=name,
        harness="cursor",
        model=model,
        binary="cursor-agent",
        run_argv=lambda prompt, sid: [
            "cursor-agent", "-p", "--force", "-m", model,
            "--output-format", "json", prompt,
        ],
        repair_argv=lambda prompt, sid: [
            "cursor-agent", "--continue", "-p", "--force", "-m", model,
            "--output-format", "json", prompt,
        ],
        version_argv=["cursor-agent", "--version"],
        auth_check=_cursor_auth,
    )


def builtin_profiles() -> dict:
    return {
        "grok-lean-high": _grok_profile("grok-lean-high", "grok-lean", "high"),
        "cursor-grok-high-fast-off": _cursor_profile(
            "cursor-grok-high-fast-off", "grok-4.5[effort=high,fast=false]"),
        "cursor-composer": _cursor_profile("cursor-composer", "composer"),
        "cursor-frontier": _cursor_profile(
            "cursor-frontier", os.environ.get("ROUTER_CURSOR_FRONTIER_MODEL",
                                              "sonnet-4.5")),
    }


def stub_profile(name: str, run_cmd: str, repair_cmd: str | None = None) -> Profile:
    """Test/offline profile: shell command templates with {prompt} and {sid}."""

    def build(template):
        def argv(prompt, sid):
            return [
                (prompt if part == "{prompt}" else sid if part == "{sid}" else part)
                for part in shlex.split(template)
            ]
        return argv

    return Profile(
        name=name, harness="stub", model="stub", binary=shlex.split(run_cmd)[0],
        run_argv=build(run_cmd), repair_argv=build(repair_cmd or run_cmd),
    )


# ---------------------------------------------------------------------------
# Routing policy — provisional; updated from JSONL production records
# ---------------------------------------------------------------------------

ROUTING_TABLE = {
    "small-iteration": ["cursor-composer", "grok-lean-high"],
    "ui-domain": ["cursor-frontier", "grok-lean-high"],
    "bounded-tooling": ["grok-lean-high", "cursor-grok-high-fast-off"],
    "test-repair": ["grok-lean-high", "cursor-grok-high-fast-off"],
    "high-risk": ["grok-lean-high", "cursor-grok-high-fast-off"],
    "default": ["cursor-grok-high-fast-off", "grok-lean-high"],
}


def select_route(packet: Packet, profiles: dict) -> tuple[list, dict]:
    """Return (ordered available profiles — max 2, skipped {name: reason})."""
    order = list(ROUTING_TABLE.get(packet.task_class, ROUTING_TABLE["default"]))
    if packet.preferred_profile:
        order = [packet.preferred_profile] + [
            p for p in order if p != packet.preferred_profile]
    route, skipped = [], {}
    for name in order:
        prof = profiles.get(name)
        if prof is None:
            skipped[name] = "unknown profile"
            continue
        reason = prof.availability()
        if reason:
            skipped[name] = reason
            continue
        route.append(prof)
        if len(route) == 2:
            break
    return route, skipped


# ---------------------------------------------------------------------------
# Supervised execution — process-group ownership, no orphans
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    state: str  # completed | timeout | spawn-error
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    orphans_killed: bool = False


_POSIX = os.name == "posix"


def run_supervised(argv: list, cwd: Path, timeout: int) -> RunOutcome:
    """Run argv under supervision so no descendant survives the harness.

    POSIX: the child gets its own session/process group; on timeout the whole
    group is TERMed then KILLed. Windows: the child gets its own process
    group and ``taskkill /T`` fells the whole tree.
    """
    start = time.monotonic()
    popen_kwargs: dict = {}
    if _POSIX:
        popen_kwargs["start_new_session"] = True
    else:  # pragma: no cover - exercised on Windows only
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, **popen_kwargs,
        )
    except OSError as exc:
        return RunOutcome(state="spawn-error", stderr=str(exc))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        state, rc = "completed", proc.returncode
        orphans = _reap_tree(proc.pid)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc.pid)
        stdout, stderr = proc.communicate()
        state, rc = "timeout", None
        orphans = True
    return RunOutcome(
        state=state, returncode=rc, stdout=stdout or "", stderr=stderr or "",
        duration_s=round(time.monotonic() - start, 2), orphans_killed=orphans,
    )


def _terminate_tree(pid: int, grace_s: float = 5.0) -> None:
    if not _POSIX:  # pragma: no cover - exercised on Windows only
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
        return
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)  # windows-footgun: ok — POSIX-gated
    # start_new_session makes the child a session/group leader, so its pid IS
    # the pgid — and stays valid for signalling surviving group members even
    # after the leader itself has exited.
    for sig, wait in ((signal.SIGTERM, grace_s), (sigkill, 2.0)):
        try:
            os.killpg(pid, sig)  # windows-footgun: ok — POSIX-gated
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _tree_alive(pid):
                return
            time.sleep(0.1)


def _reap_tree(pid: int) -> bool:
    """After a completed run, kill any descendants the agent left behind.
    Returns True if stragglers had to be killed."""
    if not _tree_alive(pid):
        return False
    _terminate_tree(pid, grace_s=1.0)
    return True


def _tree_alive(pid: int) -> bool:
    if not _POSIX:  # pragma: no cover - exercised on Windows only
        try:
            import psutil
            return psutil.pid_exists(pid)
        except ImportError:
            return False
    try:
        os.killpg(pid, 0)  # windows-footgun: ok — POSIX-gated
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True


# ---------------------------------------------------------------------------
# Protected verification — decisive acceptance outside candidate control
# ---------------------------------------------------------------------------


def verify_packet(packet: Packet, repo: Path) -> tuple[bool, str]:
    # Restore protected inputs from the packet so a candidate that edited the
    # acceptance tests cannot pass by tampering.
    for rel in packet.protected_files:
        if rel not in packet.files:
            return False, f"protected file `{rel}` not present in packet files"
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(packet.files[rel], encoding="utf-8")
    for cmd in packet.verify:
        check = subprocess.run(
            ["bash", "-c", cmd], cwd=repo, capture_output=True, text=True,
            timeout=300,
        )
        if check.returncode != 0:
            evidence = (check.stderr or check.stdout or "").strip()[-1500:]
            return False, f"verify command failed: `{cmd}`\n{evidence}"
    return True, ""


# ---------------------------------------------------------------------------
# Lifecycle: run → verify → one repair → verify → reroute once → stop
# ---------------------------------------------------------------------------


REPAIR_PROMPT = (
    "Your previous attempt did not pass acceptance. Exact failure evidence:\n\n"
    "{evidence}\n\n"
    "Fix the code so the acceptance commands pass. Do not modify these "
    "protected files: {protected}. Acceptance commands: {verify}"
)


def _seed_repo(packet: Packet, root: Path, attempt_label: str) -> Path:
    repo = root / attempt_label
    repo.mkdir(parents=True)
    for rel, contents in packet.files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "router", "GIT_AUTHOR_EMAIL": "router@local",
           "GIT_COMMITTER_NAME": "router", "GIT_COMMITTER_EMAIL": "router@local"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "packet-seed"]):
        subprocess.run(["git", "-C", str(repo), *args], env=env,
                       capture_output=True, text=True)
    return repo


def _failure_class(outcome: RunOutcome, verified: bool) -> str:
    if outcome.state == "spawn-error":
        return "spawn-error"
    if outcome.state == "timeout":
        return "timeout"
    if outcome.returncode not in (0, None):
        return "agent-error"
    if not verified:
        return "verify-failed"
    return "none"


def route_packet(packet: Packet, profiles: dict, log_path: Path,
                 workdir_root: Path | None = None,
                 route_override: list | None = None) -> dict:
    """Execute the full lifecycle for one packet. Returns the summary record
    (also appended to the JSONL log after every attempt record)."""
    workdir_root = workdir_root or Path(
        tempfile.mkdtemp(prefix=f"router-{packet.name}-"))
    if route_override is not None:
        route, skipped = route_override, {}
    else:
        route, skipped = select_route(packet, profiles)
    packet_hash = packet.digest()
    attempts: list = []
    accepted = False
    started = time.time()

    for idx, profile in enumerate(route):
        rerouted = idx > 0
        session_id = str(uuid.uuid4())
        repo = _seed_repo(packet, workdir_root, f"{idx}-{profile.name}")
        version = profile.resolved_version()
        timeout = max(packet.expected_duration_s, 60)

        outcome = run_supervised(
            profile.run_argv(packet.prompt, session_id), repo, timeout)
        verified, evidence = (False, "")
        if outcome.state == "completed" and outcome.returncode == 0:
            verified, evidence = verify_packet(packet, repo)
        repair_used = False

        if not verified and outcome.state == "completed":
            repair_used = True
            repair_prompt = REPAIR_PROMPT.format(
                evidence=evidence or (outcome.stderr or outcome.stdout)[-1500:],
                protected=packet.protected_files, verify=packet.verify)
            repair_outcome = run_supervised(
                profile.repair_argv(repair_prompt, session_id), repo, timeout)
            if repair_outcome.state == "completed" and repair_outcome.returncode == 0:
                verified, evidence = verify_packet(packet, repo)
            outcome.duration_s = round(
                outcome.duration_s + repair_outcome.duration_s, 2)
            if repair_outcome.state != "completed":
                outcome.state = repair_outcome.state

        record = {
            "type": "attempt",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "packet": packet.name,
            "packet_hash": packet_hash,
            "task_class": packet.task_class,
            "risk": packet.risk,
            "profile": profile.name,
            "harness": profile.harness,
            "model": profile.model,
            "resolved_version": version,
            "session_id": session_id,
            "rerouted": rerouted,
            "repair_used": repair_used,
            "accepted": verified,
            "elapsed_s": outcome.duration_s,
            "terminal_state": outcome.state,
            "failure_class": _failure_class(outcome, verified),
            "orphans_killed": outcome.orphans_killed,
            "usage": _extract_usage(outcome.stdout),
            "workdir": str(repo),
        }
        attempts.append(record)
        _append_jsonl(log_path, record)
        if verified:
            accepted = True
            break
        # reroute the identical packet exactly once, then stop
        if rerouted:
            break

    summary = {
        "type": "summary",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packet": packet.name,
        "packet_hash": packet_hash,
        "task_class": packet.task_class,
        "accepted": accepted,
        "attempts": len(attempts),
        "route": [p.name for p in route],
        "skipped_profiles": skipped,
        "total_elapsed_s": round(time.time() - started, 2),
        "final_profile": attempts[-1]["profile"] if attempts else None,
        "final_workdir": attempts[-1]["workdir"] if attempts else None,
    }
    _append_jsonl(log_path, summary)
    return summary


def _extract_usage(stdout: str) -> dict:
    """Best-effort usage/cost extraction from a JSON terminal object."""
    for line in reversed(stdout.strip().splitlines() or []):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = {}
        for key in ("usage", "cost", "total_cost_usd", "tokens", "num_turns",
                    "duration_ms"):
            if isinstance(obj, dict) and key in obj:
                usage[key] = obj[key]
        return usage
    return {}


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--packet", required=True, help="packet JSON file")
    ap.add_argument("--log", default="router-log.jsonl",
                    help="append-only JSONL task record")
    ap.add_argument("--route", default=None,
                    help="comma-separated profile names overriding the routing table")
    ap.add_argument("--stub-run", default=None,
                    help="register a `stub` profile: run command template ({prompt}/{sid})")
    ap.add_argument("--stub-repair", default=None,
                    help="repair command template for the stub profile")
    args = ap.parse_args(argv)

    packet = Packet.from_dict(
        json.loads(Path(args.packet).read_text(encoding="utf-8")))
    profiles = builtin_profiles()
    if args.stub_run:
        profiles["stub"] = stub_profile("stub", args.stub_run, args.stub_repair)

    route_override = None
    if args.route:
        names = [n.strip() for n in args.route.split(",") if n.strip()]
        unknown = [n for n in names if n not in profiles]
        if unknown:
            ap.error(f"unknown profiles: {unknown} (known: {sorted(profiles)})")
        route_override = [profiles[n] for n in names[:2]]

    summary = route_packet(packet, profiles, Path(args.log),
                           route_override=route_override)
    print(json.dumps(summary, indent=2))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
