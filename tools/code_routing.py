"""Direct-CLI coding-agent routing engine for Hermes.

General execution path for delegating a coding task to an external agent CLI
(Grok Build, Cursor Agent) with deterministic acceptance:

- **Work packet**: objective + verification, targeting either an existing git
  repository at an exact base SHA (production) or a synthetic seeded file set
  (benchmarks/tests). The packet hash covers every semantic field.
- **Isolation**: each attempt runs in a fresh clone/seeded repo; packet file
  paths are strictly validated (no absolute paths, no ``..``, no symlink
  escapes) and every write is containment-checked.
- **Profiles**: named, versioned, catalog-valid model pins — configurable via
  environment, never plain-default invocations. Sandbox policy is an explicit
  argv invariant that fails closed when the binary cannot enforce it.
- **Lifecycle**: run → external verify (protected files restored from the
  packet; allowed-path scope contract enforced) → one same-session repair
  with exact failure evidence → verify → reroute the identical packet once →
  stop.
- **Supervision**: process-group kill on POSIX, ``taskkill /T`` on Windows,
  plus a psutil descendant tracker so stragglers are reaped even after the
  direct child exits.
- **Record**: append-only JSONL under ``HERMES_HOME`` by default, with
  per-phase (run/repair) outcomes and usage.

Exposed to the agent as the ``route_code_task`` tool
(:mod:`tools.code_routing_tool`) and to operators via
``scripts/coding_agent_router.py``. Deliberately NOT here: ACP transports,
attestation frameworks, UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Optional

_POSIX = os.name == "posix"


def default_log_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(
        os.path.expanduser("~"), ".hermes")
    return Path(home) / "router" / "routes.jsonl"


# ---------------------------------------------------------------------------
# Packet path validation — nothing escapes the attempt workspace
# ---------------------------------------------------------------------------


class PacketPathError(ValueError):
    """A packet file path is unsafe (absolute, ``..``, escape, ...)."""


def validate_rel_path(rel: str) -> str:
    """Validate a packet-relative path lexically. Returns the path unchanged.

    Rejects absolute paths (POSIX and Windows forms), drive letters, empty
    paths, ``.``/``..`` segments, and backslash separators.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise PacketPathError(f"empty or non-string packet path: {rel!r}")
    if "\\" in rel:
        raise PacketPathError(f"backslash separators not allowed: {rel!r}")
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        raise PacketPathError(f"absolute packet path not allowed: {rel!r}")
    if PureWindowsPath(rel).drive:
        raise PacketPathError(f"drive-letter packet path not allowed: {rel!r}")
    # Split raw segments (PurePosixPath silently normalizes '.' away).
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        raise PacketPathError(
            f"empty/'.'/'..' path segments not allowed: {rel!r}")
    return rel


def write_packet_file(root: Path, rel: str, content: str) -> Path:
    """Containment-checked write of one packet file into *root*.

    Resolves the destination (following any symlinks already present in the
    workspace, e.g. from a cloned repo) and requires it to remain inside
    *root* — a symlinked directory pointing outside the workspace is refused.
    """
    validate_rel_path(rel)
    root_resolved = root.resolve()
    dest = root / PurePosixPath(rel)
    # Resolve the parent (symlink-following) and re-check containment.
    parent_resolved = dest.parent.resolve()
    if (root_resolved != parent_resolved
            and root_resolved not in parent_resolved.parents):
        raise PacketPathError(
            f"packet path {rel!r} escapes the workspace via symlink or layout")
    if dest.exists() and dest.is_symlink():
        raise PacketPathError(
            f"packet path {rel!r} would write through a symlink")
    parent_resolved.mkdir(parents=True, exist_ok=True)
    final = parent_resolved / dest.name
    final.write_text(content, encoding="utf-8")
    return final


# ---------------------------------------------------------------------------
# Work packet
# ---------------------------------------------------------------------------


@dataclass
class Packet:
    """Immutable work unit. Production packets set ``repo`` + ``base_sha``;
    benchmark packets seed ``files`` into a fresh repo. Both may combine."""

    name: str
    prompt: str
    verify: list
    files: dict = field(default_factory=dict)
    repo: Optional[str] = None          # local path or git URL
    base_sha: Optional[str] = None      # exact commit to build on (with repo)
    allowed_paths: list = field(default_factory=list)  # glob/dir prefixes; [] = unrestricted
    task_class: str = "default"
    risk: str = "medium"                # low | medium | high
    expected_duration_s: int = 600
    protected_files: list = field(default_factory=list)
    preferred_profile: Optional[str] = None

    def __post_init__(self):
        for rel in list(self.files) + list(self.protected_files):
            validate_rel_path(rel)
        if self.risk not in ("low", "medium", "high"):
            raise ValueError(f"packet risk must be low|medium|high, got {self.risk!r}")
        if not self.verify:
            raise ValueError("packet must declare at least one verify command")

    @classmethod
    def from_dict(cls, d: dict) -> "Packet":
        missing = {"name", "prompt", "verify"} - set(d)
        if missing:
            raise ValueError(f"packet missing keys: {sorted(missing)}")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"packet has unknown keys: {sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in known})

    def digest(self) -> str:
        """SHA-256 over every semantic field, so any change to objective,
        sources, scope, verification, or routing inputs yields a new
        identity."""
        canonical = json.dumps(
            {
                "name": self.name,
                "prompt": self.prompt,
                "files": self.files,
                "verify": self.verify,
                "repo": self.repo,
                "base_sha": self.base_sha,
                "allowed_paths": sorted(self.allowed_paths),
                "task_class": self.task_class,
                "risk": self.risk,
                "expected_duration_s": self.expected_duration_s,
                "protected_files": sorted(self.protected_files),
                "preferred_profile": self.preferred_profile,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Profiles — explicit, configurable, catalog-valid; sandbox fails closed
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    name: str
    harness: str  # grok | cursor | stub
    model: str
    binary: str
    run_argv: Callable
    repair_argv: Callable
    version_argv: list = field(default_factory=list)
    auth_check: Callable = staticmethod(lambda: None)
    sandbox: Optional[str] = None   # enforced argv invariant when set
    config_check: Callable = staticmethod(lambda: None)

    def availability(self) -> Optional[str]:
        if shutil.which(self.binary) is None:
            return f"binary `{self.binary}` not found on PATH"
        reason = self.config_check()
        if reason:
            return reason
        return self.auth_check()

    def resolved_version(self) -> str:
        if not self.version_argv:
            return "unversioned"
        try:
            out = subprocess.run(
                self.version_argv, capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            return (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception as exc:  # pragma: no cover - defensive
            return f"version-check-failed: {exc}"


def _grok_auth() -> Optional[str]:
    if os.environ.get("XAI_API_KEY"):
        return None
    if (Path.home() / ".grok" / "auth.json").exists():
        return None
    return "no ~/.grok/auth.json and XAI_API_KEY unset (run `grok login --device-code`)"


def _cursor_auth() -> Optional[str]:
    if os.environ.get("CURSOR_API_KEY"):
        return None
    try:
        probe = subprocess.run(
            ["cursor-agent", "status"], capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        return f"cursor-agent status failed: {exc}"
    if probe.returncode == 0 and "not" not in probe.stdout.lower():
        return None
    return "CURSOR_API_KEY unset and `cursor-agent status` reports no login"


def _binary_supports_flag(binary: str, flag: str) -> bool:
    try:
        out = subprocess.run([binary, "--help"], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL)
        return flag in (out.stdout or "") + (out.stderr or "")
    except Exception:
        return False


def _grok_profile(name: str) -> Profile:
    """Direct Grok profile. Executable, model, and sandbox are separately
    configurable; the model default is catalog-valid on grok 0.2.109
    (`grok models` lists `grok-4.5`). Note the removed-in-0.2.109 flags
    (--check/--best-of-n) are never emitted."""
    binary = os.environ.get("ROUTER_GROK_BIN", "grok")
    model = os.environ.get("ROUTER_GROK_MODEL", "grok-4.5")
    effort = os.environ.get("ROUTER_GROK_EFFORT", "high")
    sandbox = os.environ.get("ROUTER_GROK_SANDBOX") or None

    def common() -> list:
        argv = [
            binary, "--no-auto-update", "--always-approve",
            "-m", model, "--reasoning-effort", effort,
            "--max-turns", "40", "--no-subagents", "--no-memory",
            "--disable-web-search", "--output-format", "json",
        ]
        if sandbox:
            argv += ["--sandbox", sandbox]
        return argv

    def config_check() -> Optional[str]:
        # Sandbox is an enforced invariant: if a policy is requested but this
        # binary/version can't take the flag, the profile is ineligible.
        if sandbox and not _binary_supports_flag(binary, "--sandbox"):
            return (f"sandbox policy {sandbox!r} requested but `{binary}` "
                    "does not support --sandbox — failing closed")
        return None

    return Profile(
        name=name, harness="grok", model=f"{model}[effort={effort}]",
        binary=binary,
        run_argv=lambda prompt, sid: [*common(), "-s", sid, "-p", prompt],
        repair_argv=lambda prompt, sid: [*common(), "-r", sid, "-p", prompt],
        version_argv=[binary, "--version"],
        auth_check=_grok_auth,
        sandbox=sandbox,
        config_check=config_check,
    )


def _cursor_profile(name: str, model: Optional[str],
                    require_model_env: Optional[str] = None) -> Profile:
    def config_check() -> Optional[str]:
        if not model:
            return (f"profile `{name}` has no model configured — set "
                    f"{require_model_env} to a model from the authenticated "
                    "`cursor-agent models` catalog (failing closed rather "
                    "than pinning a stale default)")
        return None

    return Profile(
        name=name, harness="cursor", model=model or "unconfigured",
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
        config_check=config_check,
    )


def builtin_profiles() -> dict:
    return {
        "grok-high": _grok_profile("grok-high"),
        "cursor-grok-high-fast-off": _cursor_profile(
            "cursor-grok-high-fast-off",
            os.environ.get("ROUTER_CURSOR_GROK_MODEL",
                           "grok-4.5[effort=high,fast=false]")),
        "cursor-composer": _cursor_profile(
            "cursor-composer",
            os.environ.get("ROUTER_CURSOR_COMPOSER_MODEL", "composer")),
        # A moving "frontier" alias must be explicitly chosen by the operator
        # from the live catalog; there is no default (fail closed).
        "cursor-frontier": _cursor_profile(
            "cursor-frontier",
            os.environ.get("ROUTER_CURSOR_FRONTIER_MODEL"),
            require_model_env="ROUTER_CURSOR_FRONTIER_MODEL"),
    }


def stub_profile(name: str, run_cmd: str, repair_cmd: Optional[str] = None,
                 sandbox: Optional[str] = None) -> Profile:
    """Test/offline profile: command templates with {prompt} and {sid}."""

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
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# Routing policy — provisional; promoted/demoted from JSONL production records
# ---------------------------------------------------------------------------

ROUTING_TABLE = {
    "small-iteration": ["cursor-composer", "grok-high"],
    "ui-domain": ["cursor-frontier", "grok-high"],
    "bounded-tooling": ["grok-high", "cursor-grok-high-fast-off"],
    "test-repair": ["grok-high", "cursor-grok-high-fast-off"],
    "high-risk": ["grok-high", "cursor-grok-high-fast-off"],
    "default": ["cursor-grok-high-fast-off", "grok-high"],
}


def select_route(packet: Packet, profiles: dict) -> tuple:
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
        if packet.risk == "high" and not prof.sandbox:
            # Risk policy: high-risk packets only run under an enforced
            # sandbox policy.
            skipped[name] = ("risk=high requires a sandbox-enforcing profile "
                            "(profile has no sandbox policy set)")
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
# Supervised execution — no descendant survives the harness
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    state: str  # completed | timeout | spawn-error
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    orphans_killed: bool = False


class _DescendantTracker:
    """Snapshot the child's descendant pids while it runs (via psutil), so
    stragglers can be reaped even after the direct child has exited — the
    case a process-group signal can miss on Windows (no group to signal) and
    on POSIX when a grandchild called setsid()."""

    def __init__(self, pid: int):
        self._pid = pid
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        try:
            import psutil
            self._psutil = psutil
        except ImportError:  # pragma: no cover - psutil is a core dependency
            self._psutil = None

    def __enter__(self):
        if self._psutil is not None:
            self._thread = threading.Thread(target=self._watch, daemon=True)
            self._thread.start()
        return self

    def _watch(self):
        while not self._stop.is_set():
            try:
                parent = self._psutil.Process(self._pid)
                for child in parent.children(recursive=True):
                    self._seen.add(child.pid)
            except Exception:
                pass
            self._stop.wait(0.2)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def reap_survivors(self) -> bool:
        """Kill any tracked descendant still alive. Returns True if any."""
        if self._psutil is None:
            return False
        killed = False
        for pid in sorted(self._seen):
            try:
                proc = self._psutil.Process(pid)
                if proc.status() == self._psutil.STATUS_ZOMBIE:
                    continue
                proc.kill()
                killed = True
            except Exception:
                continue
        return killed


def run_supervised(argv: list, cwd: Path, timeout: int) -> RunOutcome:
    start = time.monotonic()
    popen_kwargs: dict = {}
    if _POSIX:
        popen_kwargs["start_new_session"] = True
    else:  # pragma: no cover - exercised on Windows only
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, **popen_kwargs,
        )
    except OSError as exc:
        return RunOutcome(state="spawn-error", stderr=str(exc))
    with _DescendantTracker(proc.pid) as tracker:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            state, rc = "completed", proc.returncode
            orphans = _tree_alive(proc.pid)
            if orphans:
                _terminate_tree(proc.pid, grace_s=1.0)
        except subprocess.TimeoutExpired:
            _terminate_tree(proc.pid)
            stdout, stderr = proc.communicate()
            state, rc = "timeout", None
            orphans = True
    # Second line of defense: descendants that left the group / outlived the
    # direct child.
    orphans = tracker.reap_survivors() or orphans
    return RunOutcome(
        state=state, returncode=rc, stdout=stdout or "", stderr=stderr or "",
        duration_s=round(time.monotonic() - start, 2), orphans_killed=orphans,
    )


def _terminate_tree(pid: int, grace_s: float = 5.0) -> None:
    if not _POSIX:  # pragma: no cover - exercised on Windows only
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, stdin=subprocess.DEVNULL)
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
# Workspace preparation — general repo@SHA or synthetic seed
# ---------------------------------------------------------------------------


class WorkspaceError(RuntimeError):
    pass


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "router", "GIT_AUTHOR_EMAIL": "router@local",
    "GIT_COMMITTER_NAME": "router", "GIT_COMMITTER_EMAIL": "router@local",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, env={**os.environ, **_GIT_ENV},
    )


def prepare_workspace(packet: Packet, root: Path, label: str) -> Path:
    """Create the isolated attempt workspace.

    With ``packet.repo``: clone (local path or URL) and hard-check out the
    exact ``base_sha`` — the original checkout is never touched. Packet files
    are then overlaid (containment-checked) and committed as the seed state.
    Without a repo: initialize a fresh repository from ``packet.files``.
    """
    workdir = root / label
    workdir.parent.mkdir(parents=True, exist_ok=True)
    if packet.repo:
        clone = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--quiet", packet.repo,
             str(workdir)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, **_GIT_ENV},
        )
        if clone.returncode != 0:
            raise WorkspaceError(
                f"git clone of {packet.repo!r} failed: "
                f"{(clone.stderr or clone.stdout).strip()[-500:]}")
        if packet.base_sha:
            rev = _git(workdir, "rev-parse", "--verify", "--quiet",
                       packet.base_sha + "^{commit}")
            if rev.returncode != 0:
                raise WorkspaceError(
                    f"base_sha {packet.base_sha!r} does not resolve in "
                    f"{packet.repo!r}")
            co = _git(workdir, "checkout", "--quiet", "--detach",
                      packet.base_sha)
            if co.returncode != 0:
                raise WorkspaceError(
                    f"checkout of {packet.base_sha!r} failed: "
                    f"{(co.stderr or co.stdout).strip()[-500:]}")
    else:
        workdir.mkdir()
        _git(workdir, "init", "-q")
    for rel, contents in packet.files.items():
        write_packet_file(workdir, rel, contents)
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-qm", "packet-seed", "--allow-empty")
    return workdir


def changed_paths(repo: Path) -> list:
    """Paths changed vs the seed commit (tracked + untracked)."""
    _git(repo, "add", "-A")
    out = _git(repo, "diff", "--cached", "--name-only", "HEAD").stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _path_allowed(path: str, allowed: list) -> bool:
    from fnmatch import fnmatch
    for pattern in allowed:
        pattern = pattern.rstrip("/")
        if fnmatch(path, pattern) or path == pattern or \
                path.startswith(pattern + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Verification — decisive acceptance outside candidate control
# ---------------------------------------------------------------------------


def verify_packet(packet: Packet, repo: Path) -> tuple:
    """Returns (ok, evidence). Restores protected files, enforces the
    allowed-paths scope contract, then runs the acceptance commands."""
    for rel in packet.protected_files:
        if rel not in packet.files:
            return False, f"protected file `{rel}` not present in packet files"
        try:
            write_packet_file(repo, rel, packet.files[rel])
        except PacketPathError as exc:
            return False, str(exc)
    if packet.allowed_paths:
        protected = set(packet.protected_files)
        violations = [
            p for p in changed_paths(repo)
            if p not in protected and not _path_allowed(p, packet.allowed_paths)
        ]
        if violations:
            return False, (
                "scope violation — changed paths outside allowed_paths: "
                + ", ".join(violations[:20]))
    for cmd in packet.verify:
        check = subprocess.run(
            ["bash", "-c", cmd] if _POSIX else ["cmd", "/c", cmd],  # windows-footgun: ok — platform-gated
            cwd=repo, capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL,
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
    "protected files: {protected}. Only change files under: {allowed}. "
    "Acceptance commands: {verify}"
)


def _phase_record(phase: str, outcome: RunOutcome) -> dict:
    return {
        "phase": phase,
        "state": outcome.state,
        "returncode": outcome.returncode,
        "duration_s": outcome.duration_s,
        "orphans_killed": outcome.orphans_killed,
        "usage": _extract_usage(outcome.stdout),
    }


def _failure_class(phases: list, verified: bool) -> str:
    if verified:
        return "none"
    last = phases[-1] if phases else {}
    state = last.get("state")
    if state == "spawn-error":
        return "spawn-error"
    if state == "timeout":
        return "timeout"
    if last.get("returncode") not in (0, None):
        return "agent-error"
    return "verify-failed"


def route_packet(packet: Packet, profiles: dict, log_path: Path,
                 workdir_root: Optional[Path] = None,
                 route_override: Optional[list] = None) -> dict:
    """Execute the full lifecycle for one packet; returns the summary record
    (each attempt and the summary are appended to the JSONL log)."""
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
        phases: list = []
        verified, evidence = False, ""
        repair_used = False
        try:
            repo = prepare_workspace(packet, workdir_root,
                                     f"{idx}-{profile.name}")
        except (WorkspaceError, PacketPathError) as exc:
            record = _attempt_record(
                packet, packet_hash, profile, session_id, rerouted,
                repair_used=False, accepted=False, phases=[],
                failure_class="packet-error", workdir="", detail=str(exc))
            attempts.append(record)
            _append_jsonl(log_path, record)
            break  # a bad packet fails identically everywhere — do not reroute
        version = profile.resolved_version()
        timeout = max(packet.expected_duration_s, 60)

        outcome = run_supervised(
            profile.run_argv(packet.prompt, session_id), repo, timeout)
        phases.append(_phase_record("run", outcome))
        if outcome.state == "completed" and outcome.returncode == 0:
            verified, evidence = verify_packet(packet, repo)

        if not verified and outcome.state == "completed":
            repair_used = True
            repair_prompt = REPAIR_PROMPT.format(
                evidence=evidence or (outcome.stderr or outcome.stdout)[-1500:],
                protected=packet.protected_files,
                allowed=packet.allowed_paths or "any path",
                verify=packet.verify)
            repair_outcome = run_supervised(
                profile.repair_argv(repair_prompt, session_id), repo, timeout)
            phases.append(_phase_record("repair", repair_outcome))
            if repair_outcome.state == "completed" and \
                    repair_outcome.returncode == 0:
                verified, evidence = verify_packet(packet, repo)

        record = _attempt_record(
            packet, packet_hash, profile, session_id, rerouted, repair_used,
            verified, phases, _failure_class(phases, verified), str(repo),
            detail="" if verified else evidence[:500],
            resolved_version=version)
        attempts.append(record)
        _append_jsonl(log_path, record)
        if verified:
            accepted = True
            break
        if rerouted:  # reroute the identical packet exactly once, then stop
            break

    summary = {
        "type": "summary",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packet": packet.name,
        "packet_hash": packet_hash,
        "task_class": packet.task_class,
        "risk": packet.risk,
        "accepted": accepted,
        "attempts": len(attempts),
        "route": [p.name for p in route],
        "skipped_profiles": skipped,
        "total_elapsed_s": round(time.time() - started, 2),
        "final_profile": attempts[-1]["profile"] if attempts else None,
        "final_workdir": attempts[-1]["workdir"] if attempts else None,
        "failure_class": attempts[-1]["failure_class"] if attempts else "no-route",
    }
    _append_jsonl(log_path, summary)
    return summary


def _attempt_record(packet: Packet, packet_hash: str, profile: Profile,
                    session_id: str, rerouted: bool, repair_used: bool,
                    accepted: bool, phases: list, failure_class: str,
                    workdir: str, detail: str = "",
                    resolved_version: str = "unresolved") -> dict:
    return {
        "type": "attempt",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packet": packet.name,
        "packet_hash": packet_hash,
        "task_class": packet.task_class,
        "risk": packet.risk,
        "profile": profile.name,
        "harness": profile.harness,
        "model": profile.model,
        "sandbox": profile.sandbox,
        "resolved_version": resolved_version,
        "session_id": session_id,
        "rerouted": rerouted,
        "repair_used": repair_used,
        "accepted": accepted,
        "phases": phases,
        "elapsed_s": round(sum(p["duration_s"] for p in phases), 2),
        "failure_class": failure_class,
        "detail": detail,
        "workdir": workdir,
    }


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
