"""Low-overhead local Hermes performance metrics store and watchdog logic.

This module is deliberately outside the model tool surface.  It gives scripts and
CLI-adjacent maintenance jobs a tiny local SQLite store for timing observations,
baseline/trend analysis, and optional Kanban regression-card creation without
adding any always-on tool schema to agent prompts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from statistics import median
from typing import Any, Callable, Iterable, Optional

try:  # Profile-aware when imported inside Hermes; env fallback for standalone scripts/tests.
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - defensive standalone fallback
    get_hermes_home = None  # type: ignore[assignment]


_SCHEMA_VERSION = 1
_DEFAULT_TIMEOUT_S = 15.0
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LOG_TAIL_BYTES = 256 * 1024
_MAX_DETAIL_BYTES = 4096
_VALID_STATES = {"measured", "skipped", "pending_instrumentation"}

# The scoped timing areas for the process-evolution loop.  Every report includes
# all twelve, either with a measured observation, a skipped reason, or an explicit
# pending-instrumentation marker.
SCOPED_TIMING_AREAS: tuple[dict[str, str], ...] = (
    {
        "area": "python_startup",
        "description": "Python interpreter cold-start proxy used by scripts and subprocess helpers.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "hermes_cli_startup",
        "description": "Hermes CLI startup/version command latency proxy.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "gateway_status",
        "description": "Gateway status command latency proxy without restarting any gateway.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "simple_chat_help",
        "description": "No-model chat command setup/help path latency proxy.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "live_chat_no_tools",
        "description": "Optional live model round-trip with tool schemas disabled.",
        "default_state": "skipped",
        "default_reason": "disabled by default; pass --live-chat to spend a model call",
    },
    {
        "area": "tool_schema_collection",
        "description": "Static tool-schema footprint scan; no check_fn imports by default.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "active_memory_scan",
        "description": "Memory-file count/size scan without reading memory contents.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "pmb_packet_scan",
        "description": "Project Memory Bundle packet count/size scan without reading packet prose.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "compression_warning_scan",
        "description": "Tail-only log scan for compression-warning counters, not log text.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "cron_job_scan",
        "description": "Cron job count/status scan without prompts or script contents.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "kanban_process_timer_scan",
        "description": "Kanban task/run duration counters from SQLite metadata.",
        "default_state": "pending_instrumentation",
    },
    {
        "area": "kanban_ownership_handoff_scan",
        "description": "Kanban ownership/handoff marker counts without body/comment text output.",
        "default_state": "pending_instrumentation",
    },
)

AREA_NAMES = tuple(item["area"] for item in SCOPED_TIMING_AREAS)
_AREA_BY_NAME = {item["area"]: item for item in SCOPED_TIMING_AREAS}

_SENSITIVE_KEYS = {
    "authorization",
    "body",
    "command",
    "content",
    "cookie",
    "env",
    "error",
    "key",
    "log",
    "message",
    "payload",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "text",
    "token",
    "value",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|authorization|password|secret)\s*(?::|=|\s)\s*[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}\b"),
)

_MEMORY_FILE_NAMES = (
    "memory.json",
    "user_profile.json",
    "memory_store.db",
    "memories.db",
    "holographic_memory.db",
    "facts.db",
)

_PMB_GLOBS = (
    "pmb/*packet*.md",
    "pmb/*packet*.json",
    "pmb/handoff.md",
    "pmb/active_state.md",
    "pmbs/*/*packet*.md",
    "pmbs/*/*packet*.json",
    "PMB/*packet*.md",
    "PMB/*packet*.json",
    "project-memory-bundles/*/*packet*.md",
    "project-memory-bundles/*/*packet*.json",
    "project_memory_bundles/*/*packet*.md",
    "project_memory_bundles/*/*packet*.json",
)

_OWNER_MARKERS = {
    "task_owner": ("task owner:",),
    "project_owner": ("project owner:",),
    "kanban_owner": ("kanban owner:", "board owner:"),
    "stage_owner": ("stage owner:", "handoff owner:", "accepted_by", "accepting owner"),
    "escalation_target": ("escalation target:",),
}

_HANDOFF_MARKERS = {
    "handoff_from": ("handoff_from", "handoff from", "from:"),
    "handoff_to": ("handoff_to", "handoff to", "to:"),
    "accepted_by": ("accepted_by", "accepted by", "accepting owner"),
    "next_action": ("next_action", "next action"),
    "stale_after": ("stale_after", "stale after"),
    "evidence": ("evidence",),
}


@dataclass(frozen=True)
class Observation:
    area: str
    status: str
    elapsed_s: Optional[float] = None
    skip_reason: Optional[str] = None
    details: Optional[dict[str, Any]] = None
    source: str = "manual"
    observed_at: Optional[int] = None


def _hermes_home() -> Path:
    if get_hermes_home is not None:
        return get_hermes_home()  # type: ignore[misc]
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def default_store_path(hermes_home: Optional[Path] = None) -> Path:
    return (hermes_home or _hermes_home()) / "performance_metrics" / "metrics.db"


def _utc_now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_text(value: str) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=[redacted]" if m.groups() else "[redacted]", text)
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    if len(text.encode("utf-8")) > _MAX_DETAIL_BYTES:
        text = text[:_MAX_DETAIL_BYTES] + "...[truncated]"
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    return any(part in lowered for part in _SENSITIVE_KEYS)


def sanitize_details(value: Any) -> Any:
    """Return JSON-safe, redacted details suitable for durable storage."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Path):
        return _redact_text(str(value))
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _is_sensitive_key(key):
                out[key] = "[redacted]"
            else:
                out[key] = sanitize_details(raw_value)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize_details(item) for item in list(value)[:50]]
    return _redact_text(str(value))


def _json_dumps_redacted(details: Optional[dict[str, Any]]) -> str:
    if not details:
        return "{}"
    redacted = sanitize_details(details)
    try:
        return json.dumps(redacted, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return json.dumps(sanitize_details(str(redacted)), sort_keys=True, ensure_ascii=False)


def init_store(store_path: Optional[Path] = None) -> Path:
    path = store_path or default_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS performance_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                area TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                elapsed_s REAL,
                skip_reason TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'manual',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_perf_obs_area_time
                ON performance_observations(area, observed_at, id);
            CREATE TABLE IF NOT EXISTS performance_regression_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                area TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(area, fingerprint)
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _connect_store(store_path: Optional[Path] = None) -> sqlite3.Connection:
    path = init_store(store_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def record_observation(
    area: str,
    *,
    elapsed_s: Optional[float] = None,
    status: str = "measured",
    skip_reason: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    source: str = "manual",
    observed_at: Optional[int] = None,
    store_path: Optional[Path] = None,
) -> int:
    if area not in _AREA_BY_NAME:
        raise ValueError(f"unknown performance area {area!r}; expected one of {', '.join(AREA_NAMES)}")
    if status not in _VALID_STATES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATES)}")
    if status == "measured" and elapsed_s is None:
        raise ValueError("measured observations require elapsed_s")
    if status != "measured" and not skip_reason:
        raise ValueError("skipped/pending observations require skip_reason")
    now = _utc_now()
    conn = _connect_store(store_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO performance_observations(
                area, observed_at, status, elapsed_s, skip_reason, details_json, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                area,
                int(observed_at if observed_at is not None else now),
                status,
                round(float(elapsed_s), 6) if elapsed_s is not None else None,
                _redact_text(skip_reason) if skip_reason else None,
                _json_dumps_redacted(details),
                _redact_text(source),
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def record_observations(observations: Iterable[Observation], *, store_path: Optional[Path] = None) -> list[int]:
    ids: list[int] = []
    for obs in observations:
        ids.append(
            record_observation(
                obs.area,
                elapsed_s=obs.elapsed_s,
                status=obs.status,
                skip_reason=obs.skip_reason,
                details=obs.details,
                source=obs.source,
                observed_at=obs.observed_at,
                store_path=store_path,
            )
        )
    return ids


def load_observations(
    *,
    store_path: Optional[Path] = None,
    area: Optional[str] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    conn = _connect_store(store_path)
    try:
        params: list[Any] = []
        where = ""
        if area:
            where = "WHERE area = ?"
            params.append(area)
        params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT * FROM performance_observations
            {where}
            ORDER BY observed_at ASC, id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        out.append(
            {
                "id": row["id"],
                "area": row["area"],
                "observed_at": row["observed_at"],
                "observed_at_iso": _iso_utc(row["observed_at"]),
                "status": row["status"],
                "elapsed_s": row["elapsed_s"],
                "skip_reason": row["skip_reason"],
                "details": details,
                "source": row["source"],
            }
        )
    return out


def build_area_coverage(observations: Optional[Iterable[dict[str, Any]]] = None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for obs in observations or []:
        latest[str(obs.get("area"))] = dict(obs)
    coverage: dict[str, dict[str, Any]] = {}
    for spec in SCOPED_TIMING_AREAS:
        area = spec["area"]
        obs = latest.get(area)
        if obs:
            coverage[area] = {
                "status": obs.get("status"),
                "observed_at": obs.get("observed_at"),
                "elapsed_s": obs.get("elapsed_s"),
                "skip_reason": obs.get("skip_reason"),
                "description": spec["description"],
            }
        else:
            coverage[area] = {
                "status": spec.get("default_state", "pending_instrumentation"),
                "skip_reason": spec.get("default_reason", "no observation recorded yet"),
                "description": spec["description"],
            }
    return coverage


def _measured_values(observations: list[dict[str, Any]]) -> list[float]:
    return [float(obs["elapsed_s"]) for obs in observations if obs.get("status") == "measured" and obs.get("elapsed_s") is not None]


def compute_baseline_and_trends(
    *,
    store_path: Optional[Path] = None,
    threshold_ratio: float = 1.5,
    min_delta_s: float = 1.0,
    min_baseline: int = 3,
    recent_window: int = 3,
    baseline_window: int = 20,
) -> dict[str, Any]:
    observations = load_observations(store_path=store_path, limit=10000)
    by_area: dict[str, list[dict[str, Any]]] = {area: [] for area in AREA_NAMES}
    for obs in observations:
        if obs["area"] in by_area:
            by_area[obs["area"]].append(obs)

    trends: dict[str, dict[str, Any]] = {}
    regressions: list[dict[str, Any]] = []
    for area in AREA_NAMES:
        measured = _measured_values(by_area[area])
        if len(measured) < min_baseline + recent_window:
            trend = {
                "area": area,
                "trend_status": "insufficient_baseline",
                "measured_count": len(measured),
                "required_count": min_baseline + recent_window,
            }
        else:
            recent_values = measured[-recent_window:]
            baseline_values = measured[-(baseline_window + recent_window):-recent_window]
            if len(baseline_values) < min_baseline:
                baseline_values = measured[:-recent_window]
            baseline = float(median(baseline_values))
            recent = float(median(recent_values))
            delta = recent - baseline
            ratio = recent / baseline if baseline > 0 else (float("inf") if recent > 0 else 1.0)
            is_regression = ratio >= threshold_ratio and delta >= min_delta_s
            trend = {
                "area": area,
                "trend_status": "regression" if is_regression else "below_threshold",
                "measured_count": len(measured),
                "baseline_median_s": round(baseline, 6),
                "recent_median_s": round(recent, 6),
                "delta_s": round(delta, 6),
                "ratio": round(ratio, 6) if ratio != float("inf") else "inf",
                "threshold_ratio": threshold_ratio,
                "min_delta_s": min_delta_s,
            }
            if is_regression:
                regressions.append(trend)
        trends[area] = trend

    return {
        "generated_at": _iso_utc(_utc_now()),
        "coverage": build_area_coverage(observations),
        "trends": trends,
        "regressions": regressions,
        "regression_count": len(regressions),
    }


def _fingerprint_regression(trend: dict[str, Any]) -> str:
    material = {
        "area": trend.get("area"),
        "baseline": trend.get("baseline_median_s"),
        "recent": trend.get("recent_median_s"),
        "ratio": trend.get("ratio"),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"perf-regression:{trend.get('area')}:{digest}"


def create_synthetic_regression_tasks(
    trends_report: dict[str, Any],
    *,
    board: Optional[str] = None,
    assignee: str = "default",
    dry_run: bool = False,
    store_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Create one goal-mode Kanban follow-up per regression, deduped by key."""
    regressions = [r for r in trends_report.get("regressions", []) if r.get("trend_status") == "regression"]
    if not regressions:
        return []
    actions: list[dict[str, Any]] = []
    kb = None
    conn = None
    if not dry_run:
        from hermes_cli import kanban_db as kb_mod

        kb = kb_mod
        conn = kb.connect(board=board)
    try:
        for trend in regressions:
            area = str(trend.get("area"))
            spec = _AREA_BY_NAME.get(area, {"description": area})
            fingerprint = _fingerprint_regression(trend)
            title = f"Investigate Hermes performance regression: {area}"
            body = (
                "Synthetic performance regression card created by the local metrics watchdog.\n\n"
                f"Area: {area}\n"
                f"Description: {spec.get('description', area)}\n"
                "Metrics evidence citation:\n"
                "- Source: performance_metrics.compute_baseline_and_trends\n"
                f"- Generated at: {trends_report.get('generated_at')}\n"
                f"- Area: {area}\n"
                f"- Baseline median: {trend.get('baseline_median_s')}s\n"
                f"- Recent median: {trend.get('recent_median_s')}s\n"
                f"- Delta: {trend.get('delta_s')}s\n"
                f"- Ratio: {trend.get('ratio')}\n"
                f"- Threshold: ratio >= {trend.get('threshold_ratio')} and delta >= {trend.get('min_delta_s')}s\n\n"
                "Acceptance criteria:\n"
                "- Reproduce the slowdown with a bounded local check.\n"
                "- Identify whether the regression is real, environmental, or below-actionable noise.\n"
                "- Patch or create a scoped follow-up with measured evidence.\n\n"
                "Ownership:\n"
                f"- Task owner: {assignee}\n"
                "- Project owner: performance metrics evolution loop\n"
                "- Kanban owner: process-mastery board owner\n"
                f"- Stage owner: {assignee} until review-required or terminal\n"
                "- Escalation target: Alex only for credentials, destructive consent, login/CAPTCHA/2FA, payment/account action, or explicit product/taste decision\n"
            )
            action = {
                "area": area,
                "fingerprint": fingerprint,
                "title": title,
                "dry_run": dry_run,
            }
            if dry_run:
                action["task_id"] = None
                action["created"] = False
            else:
                assert kb is not None and conn is not None
                task_id = kb.create_task(
                    conn,
                    title=title,
                    body=body,
                    assignee=assignee,
                    created_by="performance-metrics-watchdog",
                    priority=8,
                    idempotency_key=fingerprint,
                    goal_mode=True,
                    board=board,
                )
                action["task_id"] = task_id
                action["created"] = True
                _record_regression_task(area, fingerprint, task_id, store_path=store_path)
            actions.append(action)
    finally:
        if conn is not None:
            conn.close()
    return actions


def _record_regression_task(area: str, fingerprint: str, task_id: str, *, store_path: Optional[Path]) -> None:
    conn = _connect_store(store_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO performance_regression_tasks(area, fingerprint, task_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (area, fingerprint, task_id, _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def _project_python() -> str:
    for candidate in (_REPO_ROOT / ".venv" / "bin" / "python", _REPO_ROOT / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _hermes_cmd() -> list[str]:
    hermes = shutil.which("hermes")
    if hermes:
        return [hermes]
    return [_project_python(), "-m", "hermes_cli.main"]


def _time_command(area: str, cmd: list[str], *, timeout_s: float, cwd: Path) -> Observation:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=timeout_s,
            check=False,
        )
        elapsed = time.perf_counter() - start
        return Observation(
            area=area,
            status="measured",
            elapsed_s=elapsed,
            details={
                "exit_code": proc.returncode,
                "stdout_bytes": len(proc.stdout or b""),
                "stderr_bytes": len(proc.stderr or b""),
                "timed_out": False,
            },
            source="performance_watchdog",
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        return Observation(
            area=area,
            status="measured",
            elapsed_s=elapsed,
            details={
                "exit_code": None,
                "stdout_bytes": len(exc.stdout or b""),
                "stderr_bytes": len(exc.stderr or b""),
                "timed_out": True,
            },
            source="performance_watchdog",
        )
    except OSError as exc:
        return Observation(
            area=area,
            status="skipped",
            skip_reason=f"command failed to launch: {type(exc).__name__}",
            details={"error_type": type(exc).__name__},
            source="performance_watchdog",
        )


def _measure_callable(area: str, fn: Callable[[], dict[str, Any]]) -> Observation:
    start = time.perf_counter()
    try:
        details = fn()
        return Observation(
            area=area,
            status="measured",
            elapsed_s=time.perf_counter() - start,
            details=details,
            source="performance_watchdog",
        )
    except Exception as exc:
        return Observation(
            area=area,
            status="skipped",
            skip_reason=f"collector failed: {type(exc).__name__}",
            details={"error_type": type(exc).__name__},
            source="performance_watchdog",
        )


def _static_tool_schema_scan() -> dict[str, Any]:
    tools = 0
    schema_bytes = 0
    for path in sorted((_REPO_ROOT / "tools").glob("*.py")):
        if path.name in {"__init__.py", "registry.py", "mcp_tool.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        tools += text.count("registry.register(")
        schema_bytes += text.count('"schema"') + text.count("schema=")
        schema_bytes += len(re.findall(r"description\s*[:=]", text))
    return {"registered_tool_files": tools, "schema_signal_count": schema_bytes, "collector": "static_no_imports"}


def _scan_memory_sizes() -> dict[str, Any]:
    home = _hermes_home()
    candidates = [home / name for name in _MEMORY_FILE_NAMES]
    for dirname in ("memory", "memories", "holographic"):
        root = home / dirname
        if root.exists():
            candidates.extend(path for path in root.glob("**/*") if path.is_file())
    total = 0
    count = 0
    for path in candidates:
        try:
            if path.is_file():
                total += path.stat().st_size
                count += 1
        except OSError:
            continue
    return {"file_count": count, "total_bytes": total}


def _scan_pmb_sizes() -> dict[str, Any]:
    roots = [_hermes_home(), _REPO_ROOT]
    seen: set[Path] = set()
    count = 0
    total = 0
    for root in roots:
        for pattern in _PMB_GLOBS:
            for path in root.glob(pattern) if root.exists() else []:
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen or not resolved.is_file():
                    continue
                seen.add(resolved)
                try:
                    total += resolved.stat().st_size
                    count += 1
                except OSError:
                    continue
    return {"file_count": count, "total_bytes": total}


def _scan_compression_warnings() -> dict[str, Any]:
    log_dir = _hermes_home() / "logs"
    patterns = ("compression warning", "preflight compression", "context window getting full", "compressing conversation")
    counts: Counter[str] = Counter()
    files = 0
    bytes_scanned = 0
    if not log_dir.exists():
        return {"files_seen": 0, "bytes_scanned": 0, "matches": 0}
    for path in sorted(log_dir.glob("*.log")):
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _LOG_TAIL_BYTES:
                    handle.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
                raw = handle.read(_LOG_TAIL_BYTES)
        except OSError:
            continue
        files += 1
        bytes_scanned += len(raw)
        lowered = raw.decode("utf-8", errors="ignore").casefold()
        for pattern in patterns:
            counts[pattern] += lowered.count(pattern)
    return {"files_seen": files, "bytes_scanned": bytes_scanned, "matches": sum(counts.values()), "by_pattern": dict(counts)}


def _scan_cron_jobs() -> dict[str, Any]:
    path = _hermes_home() / "cron" / "jobs.json"
    if not path.exists():
        return {"exists": False, "total": 0, "enabled": 0, "disabled": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exists": True, "error_type": type(exc).__name__}
    raw = data.get("jobs", data) if isinstance(data, dict) else data
    jobs = list(raw.values()) if isinstance(raw, dict) else list(raw or []) if isinstance(raw, list) else []
    enabled = sum(1 for job in jobs if isinstance(job, dict) and job.get("enabled", True))
    return {"exists": True, "total": len(jobs), "enabled": enabled, "disabled": len(jobs) - enabled}


def _kanban_db_path() -> Path:
    try:
        from hermes_cli import kanban_db as kb

        return kb.kanban_db_path()
    except Exception:
        override = os.environ.get("HERMES_KANBAN_DB", "").strip()
        if override:
            return Path(override).expanduser()
        return _hermes_home() / "kanban.db"


def _scan_kanban_process_timers() -> dict[str, Any]:
    db_path = _kanban_db_path()
    if not db_path.exists():
        return {"exists": False, "active_tasks": 0}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        status_counts = dict(Counter(row["status"] for row in conn.execute("SELECT status FROM tasks")))
        run_count = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        active_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'archived')").fetchone()[0]
        return {"exists": True, "status_counts": status_counts, "run_count": run_count, "active_tasks": active_tasks}
    finally:
        conn.close()


def _missing_marker_keys(text: str, markers: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = text.casefold()
    return [key for key, choices in markers.items() if not any(choice.casefold() in lowered for choice in choices)]


def _looks_like_handoff(text: str) -> bool:
    lowered = text.casefold()
    return "handoff:" in lowered or "handoff_from" in lowered or "handoff to" in lowered or "handoff from" in lowered


def _scan_kanban_ownership() -> dict[str, Any]:
    db_path = _kanban_db_path()
    if not db_path.exists():
        return {"exists": False, "missing_owner_tasks": 0, "handoff_gaps": 0}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        missing_owner_tasks = 0
        missing_by_field: Counter[str] = Counter()
        for row in conn.execute("SELECT body FROM tasks WHERE status NOT IN ('done', 'archived')"):
            missing = _missing_marker_keys(str(row["body"] or ""), _OWNER_MARKERS)
            if missing:
                missing_owner_tasks += 1
                missing_by_field.update(missing)
        handoff_gaps = 0
        handoff_missing_by_field: Counter[str] = Counter()
        for row in conn.execute("SELECT body FROM task_comments"):
            text = str(row["body"] or "")
            if not _looks_like_handoff(text):
                continue
            missing = _missing_marker_keys(text, _HANDOFF_MARKERS)
            if missing:
                handoff_gaps += 1
                handoff_missing_by_field.update(missing)
        return {
            "exists": True,
            "missing_owner_tasks": missing_owner_tasks,
            "missing_by_field": dict(missing_by_field),
            "handoff_gaps": handoff_gaps,
            "handoff_missing_by_field": dict(handoff_missing_by_field),
        }
    finally:
        conn.close()


def collect_current_observations(*, live_chat: bool = False, timeout_s: float = _DEFAULT_TIMEOUT_S) -> list[Observation]:
    hermes = _hermes_cmd()
    observations = [
        _time_command("python_startup", [_project_python(), "-c", "pass"], timeout_s=min(timeout_s, 5.0), cwd=_REPO_ROOT),
        _time_command("hermes_cli_startup", hermes + ["--version"], timeout_s=timeout_s, cwd=_REPO_ROOT),
        _time_command("gateway_status", hermes + ["gateway", "status"], timeout_s=timeout_s, cwd=_REPO_ROOT),
        _time_command("simple_chat_help", hermes + ["chat", "--help"], timeout_s=timeout_s, cwd=_REPO_ROOT),
    ]
    if live_chat:
        observations.append(
            _time_command(
                "live_chat_no_tools",
                hermes + ["chat", "-q", "Reply exactly: HERMES_PERF_OK", "-Q", "--toolsets", ""],
                timeout_s=max(timeout_s, 60.0),
                cwd=_REPO_ROOT,
            )
        )
    else:
        observations.append(
            Observation(
                area="live_chat_no_tools",
                status="skipped",
                skip_reason="disabled by default; pass --live-chat to spend a model call",
                source="performance_watchdog",
            )
        )
    observations.extend(
        [
            _measure_callable("tool_schema_collection", _static_tool_schema_scan),
            _measure_callable("active_memory_scan", _scan_memory_sizes),
            _measure_callable("pmb_packet_scan", _scan_pmb_sizes),
            _measure_callable("compression_warning_scan", _scan_compression_warnings),
            _measure_callable("cron_job_scan", _scan_cron_jobs),
            _measure_callable("kanban_process_timer_scan", _scan_kanban_process_timers),
            _measure_callable("kanban_ownership_handoff_scan", _scan_kanban_ownership),
        ]
    )
    return observations


def render_text_report(report: dict[str, Any]) -> str:
    lines = ["Hermes performance metrics report", f"generated_at: {report.get('generated_at')}"]
    regressions = report.get("regressions", [])
    if regressions:
        lines.append("regressions:")
        for trend in regressions:
            lines.append(
                f"- {trend.get('area')}: {trend.get('baseline_median_s')}s -> {trend.get('recent_median_s')}s "
                f"(ratio {trend.get('ratio')}, delta {trend.get('delta_s')}s)"
            )
    else:
        lines.append("regressions: none above threshold")
    lines.append("coverage:")
    for area, coverage in sorted((report.get("coverage") or {}).items()):
        status = coverage.get("status")
        elapsed = coverage.get("elapsed_s")
        suffix = f" {elapsed}s" if elapsed is not None else f" ({coverage.get('skip_reason')})"
        lines.append(f"- {area}: {status}{suffix}")
    actions = report.get("task_actions") or []
    if actions:
        lines.append("kanban_actions:")
        for action in actions:
            lines.append(f"- {action.get('area')}: {action.get('task_id') or 'dry-run'}")
    return "\n".join(lines) + "\n"


def run_watchdog(
    *,
    store_path: Optional[Path] = None,
    live_chat: bool = False,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    threshold_ratio: float = 1.5,
    min_delta_s: float = 1.0,
    create_tasks: bool = False,
    dry_run: bool = False,
    board: Optional[str] = None,
    assignee: str = "default",
) -> dict[str, Any]:
    observations = collect_current_observations(live_chat=live_chat, timeout_s=timeout_s)
    record_observations(observations, store_path=store_path)
    trends = compute_baseline_and_trends(
        store_path=store_path,
        threshold_ratio=threshold_ratio,
        min_delta_s=min_delta_s,
    )
    actions = []
    if create_tasks:
        actions = create_synthetic_regression_tasks(
            trends,
            board=board,
            assignee=assignee,
            dry_run=dry_run,
            store_path=store_path,
        )
    trends["task_actions"] = actions
    trends["suppressed"] = not bool(trends.get("regressions"))
    return trends


__all__ = [
    "AREA_NAMES",
    "SCOPED_TIMING_AREAS",
    "Observation",
    "build_area_coverage",
    "collect_current_observations",
    "compute_baseline_and_trends",
    "create_synthetic_regression_tasks",
    "default_store_path",
    "init_store",
    "load_observations",
    "record_observation",
    "record_observations",
    "render_text_report",
    "run_watchdog",
    "sanitize_details",
]
