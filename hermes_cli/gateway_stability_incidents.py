"""Read-only gateway stability incident detector.

This module powers ``hermes gateway incidents`` and is deliberately no-agent:
it reads profile-local logs/state, groups only repeated or otherwise
significant gateway-stability patterns, and prints actionable evidence.  It
never restarts or stops gateways.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Iterable, Sequence


_LOG_NAMES = (
    "gateway.log",
    "gateway.error.log",
    "errors.log",
    "gateway-shutdown-diag.log",
)

_TIMESTAMP_PATTERNS = (
    # logging.Formatter default-ish: 2026-07-08 10:20:30,123
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,\.]\d+)?"),
    # ISO with timezone suffix: 2026-07-08T10:20:30Z
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:[,\.]\d+)?Z"),
)


@dataclass(frozen=True)
class EvidenceLine:
    profile: str
    path: str
    line_number: int
    text: str
    timestamp: str | None = None

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "path": self.path,
            "line_number": self.line_number,
            "text": self.text,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class GatewayIncident:
    kind: str
    severity: str
    profile: str
    title: str
    detail: str
    evidence: tuple[EvidenceLine, ...] = field(default_factory=tuple)
    safe_next_command: str = ""
    owner: str = "gateway operator"

    @property
    def count(self) -> int:
        return len(self.evidence)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "profile": self.profile,
            "title": self.title,
            "detail": self.detail,
            "count": self.count,
            "evidence": [line.to_dict() for line in self.evidence],
            "safe_next_command": self.safe_next_command,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class _Rule:
    kind: str
    severity: str
    title: str
    detail: str
    min_count: int
    markers: tuple[str, ...]
    all_markers: tuple[str, ...] = ()
    exclude_markers: tuple[str, ...] = ()

    def matches(self, text_lower: str) -> bool:
        if self.exclude_markers and any(marker in text_lower for marker in self.exclude_markers):
            return False
        if self.all_markers and not all(marker in text_lower for marker in self.all_markers):
            return False
        return any(marker in text_lower for marker in self.markers)


_RULES: tuple[_Rule, ...] = (
    _Rule(
        kind="restart_loop_breaker_tripped",
        severity="critical",
        title="Restart-loop breaker tripped",
        detail=(
            "The gateway skipped startup auto-resume because repeated restart-interrupted "
            "boots suggest a SIGTERM-respawn loop."
        ),
        min_count=1,
        markers=("restart-loop breaker tripped",),
    ),
    _Rule(
        kind="repeated_unexpected_sigterm",
        severity="critical",
        title="Repeated unexpected gateway SIGTERM shutdowns",
        detail=(
            "The gateway received multiple unplanned SIGTERM shutdown signals in the "
            "scanned log window. This can indicate a bad launchd/systemd watchdog, a "
            "self-restarting agent action, or an external supervisor loop."
        ),
        min_count=2,
        markers=("received sigterm",),
        all_markers=("initiating shutdown",),
        exclude_markers=("planned", "takeover", "exiting cleanly"),
    ),
    _Rule(
        kind="telegram_dns_fallback_cluster",
        severity="error",
        title="Telegram/network DNS fallback cluster",
        detail=(
            "Repeated Telegram or network name-resolution/connectivity failures appeared "
            "close together. Check DNS/Tailscale/network health before restarting gateways."
        ),
        min_count=3,
        markers=(
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo",
            "fallback ip",
            "fallback_ips",
            "dns",
        ),
        all_markers=(),
    ),
    _Rule(
        kind="stale_session_recovery",
        severity="warning",
        title="Stale gateway session recovery",
        detail=(
            "The gateway healed or pruned stale session/routing state. One occurrence is "
            "already significant because it means the runtime found a stuck session guard "
            "or stale routing record."
        ),
        min_count=1,
        markers=(
            "healing stale session lock",
            "stale session",
            "stale gateway routing",
            "gateway_routing",
            "session hygiene",
            "prun",
        ),
        all_markers=(),
    ),
    _Rule(
        kind="idle_timeout_cluster",
        severity="warning",
        title="Gateway idle-timeout cluster",
        detail=(
            "Multiple idle-timeout or scale-to-zero events were seen. This may be expected "
            "for relay-only deployments, but repeated hits can explain apparent gateway "
            "silence or delayed wakeups."
        ),
        min_count=2,
        markers=("idle timeout", "idle-timeout", "scale-to-zero", "autostop"),
    ),
    _Rule(
        kind="telegram_flood_retry_cluster",
        severity="warning",
        title="Telegram flood-control retry cluster",
        detail=(
            "Repeated Telegram flood-control / RetryAfter events were seen. Reduce rich "
            "message churn, streaming edits, or restart frequency before retrying."
        ),
        min_count=2,
        markers=("flood control", "retryafter", "retry after"),
    ),
    _Rule(
        kind="gateway_instance_conflict",
        severity="error",
        title="Gateway instance conflict",
        detail=(
            "The logs show a second gateway/runtime lock/polling conflict. Resolve the "
            "duplicate process or stale service before starting another gateway."
        ),
        min_count=1,
        markers=(
            "gateway runtime lock is already held",
            "another gateway instance",
            "pid file race lost",
            "terminated by other getupdates request",
            "another bot instance is running",
        ),
    ),
)

_SEVERITY_RANK = {"warning": 0, "error": 1, "critical": 2}


def _parse_timestamp(line: str) -> datetime | None:
    for pattern in _TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        raw = match.group("ts")
        try:
            return datetime.strptime(raw.replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    return None


def _tail_lines(path: Path, max_lines: int) -> list[tuple[int, str]]:
    """Return up to ``max_lines`` from ``path`` with exact line numbers."""
    if max_lines <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            block_size = 8192
            data = b""
            pos = end
            newline_count = 0
            while pos > 0 and newline_count <= max_lines:
                read_size = min(block_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                data = chunk + data
                newline_count = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    try:
        with path.open("rb") as fh:
            total_lines = sum(
                chunk.count(b"\n") for chunk in iter(lambda: fh.read(1024 * 1024), b"")
            )
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    total_lines += 1
    except OSError:
        total_lines = newline_count
    first_line = max(1, total_lines - len(lines) + 1)
    return [(first_line + idx, line) for idx, line in enumerate(lines)]


def _candidate_log_paths(profile_home: Path) -> list[Path]:
    logs = profile_home / "logs"
    paths = [logs / name for name in _LOG_NAMES]
    # s6-profile logs are stored under logs/gateways/<profile>/current in the
    # container image.  Include them when present without recursing broadly.
    gateways_dir = logs / "gateways"
    if gateways_dir.is_dir():
        try:
            for current in gateways_dir.glob("*/current"):
                paths.append(current)
        except OSError:
            pass
    return paths


def _safe_command(profile: str, kind: str) -> str:
    if profile == "default":
        if kind in {"repeated_unexpected_sigterm", "restart_loop_breaker_tripped"}:
            return "from another shell/profile: hermes gateway status --deep --full"
        return "hermes gateway status --deep --full"
    if kind in {"repeated_unexpected_sigterm", "restart_loop_breaker_tripped"}:
        return f"from another shell/profile: hermes --profile {profile} gateway status --deep --full"
    return f"hermes --profile {profile} gateway status --deep --full"


def _owner(profile: str, kind: str) -> str:
    if profile == "default" and kind in {"repeated_unexpected_sigterm", "restart_loop_breaker_tripped"}:
        return "operator outside the active default gateway; do not restart default from inside itself"
    return f"{profile} gateway operator"


def analyze_profile_logs(
    profile: str,
    profile_home: Path,
    *,
    max_lines: int = 2000,
    since_minutes: int | None = None,
    now: datetime | None = None,
) -> list[GatewayIncident]:
    """Analyze one profile's gateway logs and return active incidents."""
    cutoff: datetime | None = None
    if since_minutes is not None and since_minutes > 0:
        base_now = now or datetime.now(timezone.utc)
        cutoff = base_now - timedelta(minutes=since_minutes)

    grouped: dict[str, list[EvidenceLine]] = {rule.kind: [] for rule in _RULES}
    for path in _candidate_log_paths(profile_home):
        if not path.is_file():
            continue
        display_path = str(path)
        try:
            display_path = str(path.relative_to(profile_home))
        except ValueError:
            pass
        for line_number, raw_line in _tail_lines(path, max_lines):
            line = raw_line.strip()
            if not line:
                continue
            parsed_ts = _parse_timestamp(line)
            if cutoff is not None and parsed_ts is not None and parsed_ts < cutoff:
                continue
            lowered = line.lower()
            # The DNS/fallback rule should not fire on unrelated docs/comments
            # unless a networking/Telegram marker is present too.
            has_network_context = any(
                marker in lowered
                for marker in (
                    "telegram",
                    "api.telegram.org",
                    "networkerror",
                    "timedout",
                    "httpx",
                    "connecterror",
                    "fallback",
                )
            )
            for rule in _RULES:
                if rule.kind == "telegram_dns_fallback_cluster" and not has_network_context:
                    continue
                if rule.matches(lowered):
                    grouped[rule.kind].append(
                        EvidenceLine(
                            profile=profile,
                            path=display_path,
                            line_number=line_number,
                            text=line,
                            timestamp=parsed_ts.isoformat() if parsed_ts else None,
                        )
                    )

    incidents: list[GatewayIncident] = []
    by_kind = {rule.kind: rule for rule in _RULES}
    for kind, evidence in grouped.items():
        rule = by_kind[kind]
        if len(evidence) < rule.min_count:
            continue
        # Keep output compact: enough exact evidence to prove the pattern
        # without dumping full logs into Telegram/cron output.
        compact_evidence = tuple(evidence[-5:])
        incidents.append(
            GatewayIncident(
                kind=rule.kind,
                severity=rule.severity,
                profile=profile,
                title=rule.title,
                detail=rule.detail,
                evidence=compact_evidence,
                safe_next_command=_safe_command(profile, kind),
                owner=_owner(profile, kind),
            )
        )

    incidents.sort(key=lambda inc: (-_SEVERITY_RANK.get(inc.severity, 0), inc.profile, inc.kind))
    return incidents


def discover_profile_homes(profile_name: str | None = None) -> list[tuple[str, Path]]:
    """Return profile homes to scan.

    Uses ``hermes_cli.profiles.list_profiles`` when available so profile-scoped
    homes are resolved the same way as the rest of the CLI.  Falls back to the
    active ``HERMES_HOME`` for import/test resilience.
    """
    try:
        from hermes_cli.profiles import list_profiles

        profiles = [(p.name, p.path) for p in list_profiles()]
    except Exception:
        from hermes_constants import get_hermes_home

        profiles = [("default", get_hermes_home())]
    if profile_name:
        profiles = [(name, path) for name, path in profiles if name == profile_name]
    return profiles


def detect_gateway_incidents(
    *,
    profile_name: str | None = None,
    profile_homes: Sequence[tuple[str, Path]] | None = None,
    max_lines: int = 2000,
    since_minutes: int | None = None,
    now: datetime | None = None,
) -> list[GatewayIncident]:
    homes = list(profile_homes) if profile_homes is not None else discover_profile_homes(profile_name)
    incidents: list[GatewayIncident] = []
    for profile, home in homes:
        if profile_name and profile != profile_name:
            continue
        incidents.extend(
            analyze_profile_logs(
                profile,
                Path(home),
                max_lines=max_lines,
                since_minutes=since_minutes,
                now=now,
            )
        )
    incidents.sort(key=lambda inc: (-_SEVERITY_RANK.get(inc.severity, 0), inc.profile, inc.kind))
    return incidents


def format_incidents(incidents: Sequence[GatewayIncident], *, show_empty: bool = True) -> str:
    if not incidents:
        return "No gateway stability incidents detected." if show_empty else ""
    lines = ["Gateway stability incidents detected:"]
    for inc in incidents:
        lines.append(f"- [{inc.severity}] {inc.profile}: {inc.title} ({inc.count} evidence line(s))")
        lines.append(f"  Detail: {inc.detail}")
        if inc.safe_next_command:
            lines.append(f"  Safe next command: {inc.safe_next_command}")
        lines.append(f"  Owner: {inc.owner}")
        lines.append("  Evidence:")
        for ev in inc.evidence:
            lines.append(f"    {ev.path}:{ev.line_number}: {ev.text}")
    return "\n".join(lines)


def incidents_to_json(incidents: Sequence[GatewayIncident]) -> str:
    return json.dumps([inc.to_dict() for inc in incidents], indent=2, sort_keys=True)


def cmd_gateway_incidents(args) -> int:
    incidents = detect_gateway_incidents(
        profile_name=getattr(args, "profile_name", None),
        max_lines=max(1, int(getattr(args, "max_lines", 2000) or 2000)),
        since_minutes=getattr(args, "since_minutes", None),
    )
    if getattr(args, "json", False):
        print(incidents_to_json(incidents))
        return 0
    text = format_incidents(incidents, show_empty=not getattr(args, "quiet", False))
    if text:
        print(text)
    return 0
