"""Built-in event-driven performance monitor.

Implements phase 1 of ``docs/design/perf-usage-monitoring.md``: a first-party,
always-on consumer of the existing observer hooks (``post_api_request``,
``post_tool_call``) that records latency, token usage, context size, and tool
runtime to a dedicated SQLite file, samples cheap health gauges once per
process start, and answers lazy catch-up reports (``hermes monitor``).

Deliberate properties:

- **Event-driven.** Nothing runs while Hermes is idle; recording happens only
  when a hook fires and gauges are sampled once at install time.
- **Fail-open.** Every entry point swallows its own exceptions; a broken
  monitor must never affect the agent loop (matching the observer-hook
  contract in ``docs/observability/README.md``).
- **Separate database.** Metrics live in ``monitor.db``, never ``state.db``
  — the shared state database already suffers WAL write-lock convoys under
  load (see ``hermes_state.py``), and the monitor must not add to them.
- **No content.** Only sizes, durations, counts, and identifiers are stored —
  never message, memory, or tool-output content.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
_INSTALL_LOCK = threading.Lock()
_installed = False

_SCHEMA_VERSION = 1
_RETENTION_DAYS = 90


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    return get_hermes_home() / "monitor.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'interactive',
            model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            api_call_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            finish_reason TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_events_created ON api_events(created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'interactive',
            tool_name TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            error_type TEXT,
            output_chars INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_events_created ON tool_events(created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gauges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            value REAL NOT NULL,
            detail TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gauges_name_created ON gauges(name, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL DEFAULT 0,
            threshold REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alerts_kind_created ON alerts(kind, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS compression_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            tokens_before INTEGER,
            tokens_after INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            event TEXT NOT NULL DEFAULT '',
            command TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            timed_out INTEGER NOT NULL DEFAULT 0,
            error TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _current_origin() -> str:
    if os.environ.get("HERMES_CRON_SESSION") == "1":
        return "cron"
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "kanban"
    return "interactive"


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Thresholds (config-backed, cached; all alert-only)
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS = {
    "alert_context_tokens": 150_000,   # per-request prompt size alert
    "alert_session_tokens": 2_000_000, # cumulative per-session total-token alert
    "alert_tool_seconds": 120,         # single tool call runtime alert
    "alert_tool_error_rate": 0.3,      # 24h per-tool error-rate alert (min 10 calls)
    "maintenance": True,               # startup FTS optimize / WAL checkpoint
}
_ALERT_DEDUP_WINDOW_S = 3600  # one alert per (kind, session) per hour

# post_tool_call statuses that count as tool FAILURES. The full status set
# emitted by model_tools/tool_executor is: "ok" (success), "error",
# "timeout", "blocked" (policy veto), "cancelled" (interruption). Only
# genuine malfunctions belong in error rates — a policy block or a user
# cancel is not a failing tool.
_TOOL_FAILURE_STATUSES = ("error", "timeout")
_TOOL_FAILURE_SQL = "status IN ('error', 'timeout')"


def _threshold(key: str):
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        return cfg_get(
            load_config_readonly(), "monitor", key, default=_DEFAULT_THRESHOLDS[key]
        )
    except Exception:
        return _DEFAULT_THRESHOLDS[key]


def _record_alert(
    conn: sqlite3.Connection,
    kind: str,
    session_id: str,
    detail: str,
    value: float,
    threshold: float,
) -> None:
    """Insert an alert unless the same (kind, session) fired within the hour."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=_ALERT_DEDUP_WINDOW_S)
    ).isoformat()
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE kind = ? AND session_id = ? AND created_at >= ? LIMIT 1",
        (kind, session_id, cutoff),
    ).fetchone()
    if dup:
        return
    conn.execute(
        "INSERT INTO alerts (created_at, kind, session_id, detail, value, threshold)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_utc_now(), kind, session_id, detail, value, threshold),
    )
    logger.warning("perf_monitor alert [%s] %s (value=%.0f threshold=%.0f)",
                   kind, detail, value, threshold)


# ---------------------------------------------------------------------------
# Hook callbacks (fail-open)
# ---------------------------------------------------------------------------


def on_post_api_request(**kwargs: Any) -> None:
    """Record one completed model request. Never raises."""
    try:
        usage: Dict[str, Any] = kwargs.get("usage") or {}
        duration_ms = int(float(kwargs.get("api_duration") or 0.0) * 1000)
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO api_events (
                        created_at, session_id, turn_id, platform, origin,
                        model, provider, api_call_count, duration_ms,
                        finish_reason, message_count, input_tokens,
                        output_tokens, cache_read_tokens, cache_write_tokens,
                        prompt_tokens, total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now(),
                        str(kwargs.get("session_id") or ""),
                        str(kwargs.get("turn_id") or ""),
                        str(kwargs.get("platform") or ""),
                        _current_origin(),
                        str(kwargs.get("model") or ""),
                        str(kwargs.get("provider") or ""),
                        _as_int(kwargs.get("api_call_count")),
                        duration_ms,
                        kwargs.get("finish_reason"),
                        _as_int(kwargs.get("message_count")),
                        _as_int(usage.get("input_tokens")),
                        _as_int(usage.get("output_tokens")),
                        _as_int(usage.get("cache_read_tokens")),
                        _as_int(usage.get("cache_write_tokens")),
                        _as_int(usage.get("prompt_tokens")),
                        _as_int(usage.get("total_tokens")),
                    ),
                )
                session_id = str(kwargs.get("session_id") or "")
                prompt_tokens = _as_int(usage.get("prompt_tokens"))
                ctx_ceiling = _as_int(_threshold("alert_context_tokens"))
                if ctx_ceiling > 0 and prompt_tokens >= ctx_ceiling:
                    _record_alert(
                        conn, "context_size", session_id,
                        f"request context {prompt_tokens:,} tokens "
                        f"(model={kwargs.get('model')})",
                        prompt_tokens, ctx_ceiling,
                    )
                sess_ceiling = _as_int(_threshold("alert_session_tokens"))
                if sess_ceiling > 0 and session_id:
                    total = conn.execute(
                        "SELECT COALESCE(SUM(total_tokens), 0) FROM api_events"
                        " WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                    if total >= sess_ceiling:
                        _record_alert(
                            conn, "session_tokens", session_id,
                            f"session cumulative {total:,} tokens",
                            total, sess_ceiling,
                        )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: api event not recorded: %s", exc)


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one completed tool call. Never raises."""
    try:
        result = kwargs.get("result")
        output_chars = len(result) if isinstance(result, str) else 0
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO tool_events (
                        created_at, session_id, turn_id, origin, tool_name,
                        duration_ms, status, error_type, output_chars
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now(),
                        str(kwargs.get("session_id") or ""),
                        str(kwargs.get("turn_id") or ""),
                        _current_origin(),
                        str(kwargs.get("tool_name") or ""),
                        _as_int(kwargs.get("duration_ms")),
                        str(kwargs.get("status") or ""),
                        kwargs.get("error_type"),
                        output_chars,
                    ),
                )
                tool_name = str(kwargs.get("tool_name") or "")
                session_id = str(kwargs.get("session_id") or "")
                duration_ms = _as_int(kwargs.get("duration_ms"))
                slow_s = _as_int(_threshold("alert_tool_seconds"))
                if slow_s > 0 and duration_ms >= slow_s * 1000:
                    _record_alert(
                        conn, "slow_tool", session_id,
                        f"tool {tool_name} ran {duration_ms / 1000:.0f}s",
                        duration_ms / 1000, slow_s,
                    )
                status = str(kwargs.get("status") or "")
                if status in _TOOL_FAILURE_STATUSES:
                    rate_ceiling = float(_threshold("alert_tool_error_rate") or 0)
                    if rate_ceiling > 0 and tool_name:
                        day_ago = (
                            datetime.now(timezone.utc) - timedelta(hours=24)
                        ).isoformat()
                        calls, errors = conn.execute(
                            "SELECT COUNT(*),"
                            f" SUM(CASE WHEN {_TOOL_FAILURE_SQL}"
                            " THEN 1 ELSE 0 END)"
                            " FROM tool_events WHERE tool_name = ? AND created_at >= ?",
                            (tool_name, day_ago),
                        ).fetchone()
                        errors = errors or 0
                        if calls >= 10 and errors / calls >= rate_ceiling:
                            _record_alert(
                                conn, "tool_error_rate", tool_name,
                                f"tool {tool_name}: {errors}/{calls} failed in 24h",
                                errors / calls, rate_ceiling,
                            )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: tool event not recorded: %s", exc)


def record_compression(
    session_id: str,
    ok: bool,
    tokens_before: Optional[int] = None,
    tokens_after: Optional[int] = None,
) -> None:
    """Record one compression attempt; alert on a failure streak. Never raises."""
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO compression_events"
                    " (created_at, session_id, ok, tokens_before, tokens_after)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (_utc_now(), session_id or "", 1 if ok else 0,
                     tokens_before, tokens_after),
                )
                if not ok:
                    recent = conn.execute(
                        "SELECT ok FROM compression_events ORDER BY id DESC LIMIT 3"
                    ).fetchall()
                    if len(recent) == 3 and all(r["ok"] == 0 for r in recent):
                        _record_alert(
                            conn, "compression_failures", session_id or "",
                            "3 consecutive compression failures — oversized "
                            "sessions are not shrinking; consider rerouting the "
                            "compression model",
                            3, 3,
                        )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: compression event not recorded: %s", exc)


def record_hook_event(
    event: str,
    command: str,
    duration_ms: int,
    timed_out: bool = False,
    error: Optional[str] = None,
) -> None:
    """Record one shell-hook execution. Never raises."""
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO hook_events"
                    " (created_at, event, command, duration_ms, timed_out, error)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (_utc_now(), event or "", command or "",
                     _as_int(duration_ms), 1 if timed_out else 0, error),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: hook event not recorded: %s", exc)


# ---------------------------------------------------------------------------
# Startup gauges
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: Path, max_entries: int = 20_000) -> int:
    """Cheap recursive directory size; bails out on huge trees."""
    total = 0
    seen = 0
    try:
        stack = [path]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return total
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
    except OSError:
        pass
    return total


def sample_gauges() -> None:
    """Sample cheap health gauges once (called at install time). Never raises."""
    try:
        home = get_hermes_home()
        samples: List[tuple] = []

        state_db = home / "state.db"
        if state_db.exists():
            samples.append(("state_db_bytes", float(state_db.stat().st_size), None))
        monitor_db = _db_path()
        if monitor_db.exists():
            samples.append(("monitor_db_bytes", float(monitor_db.stat().st_size), None))

        try:
            from hermes_constants import get_skills_dir

            skills_dir = Path(get_skills_dir())
            if skills_dir.is_dir():
                count = sum(1 for _ in skills_dir.rglob("SKILL.md"))
                samples.append(("skills_count", float(count), None))
        except Exception:
            pass

        for name, sub in (("memory_dir_bytes", "memory"), ("logs_dir_bytes", "logs")):
            target = home / sub
            if target.is_dir():
                samples.append((name, float(_dir_size_bytes(target)), None))

        if not samples:
            return
        now = _utc_now()
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.executemany(
                    "INSERT INTO gauges (created_at, name, value, detail) VALUES (?, ?, ?, ?)",
                    [(now, n, v, d) for n, v, d in samples],
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: gauges not sampled: %s", exc)


def _prune_old_events() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)).isoformat()
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                conn.execute("DELETE FROM api_events WHERE created_at < ?", (cutoff,))
                conn.execute("DELETE FROM tool_events WHERE created_at < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        logger.debug("perf_monitor: retention prune skipped: %s", exc)


# ---------------------------------------------------------------------------
# Startup maintenance (auto, cheap, at most once per day)
# ---------------------------------------------------------------------------

_MAINTENANCE_INTERVAL_S = 24 * 3600


def _run_maintenance() -> None:
    """Passive WAL checkpoint + FTS optimize on state.db, at most once/day.

    This is the automatic half of the design's maintenance table: FTS5
    segment fragmentation is state.db's self-documented decay mode, and the
    built-in optimize cadence (every 1000 writes) leaves long gaps on
    lightly-used deployments. Everything here is fail-open and uses a short
    busy timeout so a live writer is never stalled.
    """
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'last_maintenance_at'"
                ).fetchone()
                if row:
                    try:
                        last = datetime.fromisoformat(row["value"])
                        age = (datetime.now(timezone.utc) - last).total_seconds()
                        if age < _MAINTENANCE_INTERVAL_S:
                            return
                    except ValueError:
                        pass
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value)"
                    " VALUES ('last_maintenance_at', ?)",
                    (_utc_now(),),
                )
                conn.commit()
            finally:
                conn.close()

        state_db = get_hermes_home() / "state.db"
        if not state_db.exists():
            return
        state = sqlite3.connect(state_db, timeout=1.0)
        try:
            state.execute("PRAGMA busy_timeout=1000")
            for stmt in (
                "PRAGMA wal_checkpoint(PASSIVE)",
                "INSERT INTO messages_fts(messages_fts) VALUES('optimize')",
                "INSERT INTO messages_fts_trigram(messages_fts_trigram)"
                " VALUES('optimize')",
            ):
                try:
                    state.execute(stmt)
                    state.commit()
                except sqlite3.Error:
                    continue  # missing table / locked — skip, never stall
        finally:
            state.close()
        logger.info("perf_monitor: ran daily state.db maintenance "
                    "(passive checkpoint + FTS optimize)")
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: maintenance skipped: %s", exc)


def _check_catchup() -> None:
    """Flag an idle gap so the next report leads with a catch-up header."""
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT MAX(created_at) FROM api_events"
                ).fetchone()
                last = row[0] if row else None
                if not last:
                    return
                try:
                    gap_s = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last)
                    ).total_seconds()
                except ValueError:
                    return
                if gap_s >= 24 * 3600:
                    _record_alert(
                        conn, "catchup", "",
                        f"resumed after {gap_s / 86400:.1f} idle day(s); "
                        "run `hermes monitor` for the catch-up report",
                        gap_s / 86400, 1,
                    )
                    conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # fail-open by contract
        logger.debug("perf_monitor: catchup check skipped: %s", exc)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _monitor_enabled() -> bool:
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        return bool(cfg_get(load_config_readonly(), "monitor", "enabled", default=True))
    except Exception:
        return True


def install() -> bool:
    """Register monitor callbacks on the process-wide hook manager.

    Idempotent and fail-open; returns True when the monitor is active.
    Called from agent initialization so callbacks exist before the first
    API or tool call fires its ``has_hook``-gated payload build.
    """
    global _installed
    first_install = False
    with _INSTALL_LOCK:
        try:
            if not _monitor_enabled():
                return False
            from hermes_cli.plugins import register_builtin_hook

            # Re-assert registration every time: register_builtin_hook is
            # idempotent, and the plugin manager singleton can be recreated
            # (plugin reloads, tests), which would drop our callbacks.
            register_builtin_hook("post_api_request", on_post_api_request)
            register_builtin_hook("post_tool_call", on_post_tool_call)
            first_install = not _installed
            _installed = True
        except Exception as exc:
            logger.debug("perf_monitor: install skipped: %s", exc)
            return False
    if first_install:
        # Outside the lock: one-time startup work, individually fail-open.
        _check_catchup()
        sample_gauges()
        _prune_old_events()
        if bool(_threshold("maintenance")):
            _run_maintenance()
    return True


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def generate_report(days: int = 7) -> Dict[str, Any]:
    """Build the lazy catch-up report over the last ``days`` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    report: Dict[str, Any] = {"days": days, "api": {}, "tools": [], "origins": [], "gauges": []}
    with _DB_LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT duration_ms, prompt_tokens, total_tokens, cache_read_tokens,"
                " input_tokens, output_tokens, origin FROM api_events WHERE created_at >= ?",
                (cutoff,),
            ).fetchall()
            durations = sorted(float(r["duration_ms"]) for r in rows)
            report["api"] = {
                "calls": len(rows),
                "mean_ms": (sum(durations) / len(durations)) if durations else 0.0,
                "median_ms": _percentile(durations, 50),
                "p90_ms": _percentile(durations, 90),
                "max_ms": durations[-1] if durations else 0.0,
                "mean_prompt_tokens": (
                    sum(r["prompt_tokens"] for r in rows) / len(rows) if rows else 0.0
                ),
                "max_prompt_tokens": max((r["prompt_tokens"] for r in rows), default=0),
                "total_tokens": sum(r["total_tokens"] for r in rows),
                "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
                "uncached_tokens": sum(
                    max(0, r["total_tokens"] - r["cache_read_tokens"]) for r in rows
                ),
            }
            per_origin: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                bucket = per_origin.setdefault(
                    r["origin"] or "interactive", {"calls": 0, "total_tokens": 0}
                )
                bucket["calls"] += 1
                bucket["total_tokens"] += r["total_tokens"]
            report["origins"] = [
                {"origin": k, **v}
                for k, v in sorted(per_origin.items(), key=lambda kv: -kv[1]["total_tokens"])
            ]
            report["tools"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT tool_name,
                           COUNT(*) AS calls,
                           SUM(duration_ms) AS total_ms,
                           MAX(duration_ms) AS max_ms,
                           SUM(CASE WHEN status IN ('error', 'timeout') THEN 1 ELSE 0 END) AS errors,
                           SUM(output_chars) AS output_chars
                    FROM tool_events WHERE created_at >= ?
                    GROUP BY tool_name ORDER BY total_ms DESC LIMIT 15
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            report["gauges"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT g.name,
                           MIN(g.created_at) AS first_at,
                           MAX(g.created_at) AS last_at,
                           (SELECT value FROM gauges g2 WHERE g2.name = g.name
                            ORDER BY g2.created_at ASC, g2.id ASC LIMIT 1) AS first_value,
                           (SELECT value FROM gauges g3 WHERE g3.name = g.name
                            ORDER BY g3.created_at DESC, g3.id DESC LIMIT 1) AS last_value
                    FROM gauges g GROUP BY g.name ORDER BY g.name
                    """
                ).fetchall()
            ]
            report["alerts"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT created_at, kind, session_id, detail FROM alerts"
                    " WHERE created_at >= ? ORDER BY created_at DESC LIMIT 20",
                    (cutoff,),
                ).fetchall()
            ]
            comp = conn.execute(
                "SELECT COUNT(*) AS attempts,"
                " SUM(ok) AS successes,"
                " AVG(CASE WHEN ok = 1 THEN tokens_before - tokens_after END)"
                "   AS avg_saved"
                " FROM compression_events WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()
            report["compression"] = dict(comp) if comp else {}
            hooks = conn.execute(
                "SELECT command,"
                " COUNT(*) AS runs,"
                " SUM(timed_out) AS timeouts,"
                " SUM(duration_ms) AS total_ms"
                " FROM hook_events WHERE created_at >= ?"
                " GROUP BY command ORDER BY total_ms DESC LIMIT 10",
                (cutoff,),
            ).fetchall()
            report["hooks"] = [dict(r) for r in hooks]
        finally:
            conn.close()
    report["proposals"] = _build_proposals(report)
    return report


def _build_proposals(report: Dict[str, Any]) -> List[str]:
    """Maintenance proposals — operator-confirmed actions, surfaced per design.

    Automatic actions (FTS optimize, WAL checkpoint, event pruning) run on
    their own; everything that needs judgment (pruning, rerouting, curation)
    is proposed here instead of executed.
    """
    proposals: List[str] = []
    latest = {g["name"]: (g.get("last_value") or 0) for g in report.get("gauges") or []}

    if latest.get("logs_dir_bytes", 0) > 500 * 1e6:
        proposals.append(
            f"logs directory is {latest['logs_dir_bytes'] / 1e9:.1f} GB — rotate or "
            "compact old log files (a known fixed-overhead growth vector)."
        )
    if latest.get("state_db_bytes", 0) > 1e9:
        proposals.append(
            f"state.db is {latest['state_db_bytes'] / 1e9:.1f} GB — consider archiving "
            "old sessions; large state.db lengthens WAL write-lock holds."
        )
    if latest.get("skills_count", 0) > 200:
        proposals.append(
            f"{latest['skills_count']:.0f} skills installed — the skills index in the "
            "system prompt is uncapped; run a curation pass and archive unused skills."
        )

    api = report.get("api") or {}
    ctx_ceiling = _as_int(_threshold("alert_context_tokens"))
    if ctx_ceiling > 0 and (api.get("mean_prompt_tokens") or 0) >= ctx_ceiling:
        proposals.append(
            f"mean request context is {api['mean_prompt_tokens']:,.0f} tokens — prune "
            "oversized route pins / start fresh sessions (`/new`) on the affected chats."
        )

    comp = report.get("compression") or {}
    if (comp.get("attempts") or 0) >= 3 and not (comp.get("successes") or 0):
        proposals.append(
            "all compression attempts in this period failed — reroute auxiliary "
            "compression to a cheaper, reliable model so oversized sessions shrink."
        )

    rate_ceiling = float(_threshold("alert_tool_error_rate") or 0)
    for t in report.get("tools") or []:
        calls = t.get("calls") or 0
        errors = t.get("errors") or 0
        if rate_ceiling > 0 and calls >= 20 and errors / calls >= rate_ceiling:
            proposals.append(
                f"tool {t['tool_name']} failed {errors}/{calls} times — failed calls "
                "trigger extra model turns; investigate or disable it."
            )

    for h in report.get("hooks") or []:
        runs = h.get("runs") or 0
        timeouts = h.get("timeouts") or 0
        if runs >= 5 and timeouts / runs >= 0.5:
            proposals.append(
                f"shell hook `{h['command']}` timed out {timeouts}/{runs} times — its "
                "circuit breaker is throttling it; fix or remove the hook target."
            )
    return proposals


def format_report(report: Dict[str, Any]) -> str:
    """Render a report dict as terminal text."""
    api = report.get("api") or {}
    lines: List[str] = []
    lines.append(f"Hermes performance monitor — last {report.get('days', '?')} day(s)")
    lines.append("")
    if not api.get("calls"):
        lines.append("No material usage recorded in this period.")
        return "\n".join(lines)
    lines.append(
        f"Model requests: {api['calls']}  "
        f"latency mean {api['mean_ms'] / 1000:.1f}s / median {api['median_ms'] / 1000:.1f}s"
        f" / p90 {api['p90_ms'] / 1000:.1f}s / max {api['max_ms'] / 1000:.1f}s"
    )
    lines.append(
        f"Active context per request (prompt tokens): mean {api['mean_prompt_tokens']:,.0f}"
        f" / max {api['max_prompt_tokens']:,}"
    )
    lines.append(
        f"Cumulative processed tokens across {api['calls']} requests: "
        f"{api['total_tokens']:,} total / {api['cache_read_tokens']:,} cache-read "
        f"/ {api['uncached_tokens']:,} uncached"
    )
    origins = report.get("origins") or []
    if origins:
        lines.append("")
        lines.append("By origin:")
        for o in origins:
            lines.append(
                f"  {o['origin']:<12} {o['calls']:>6} calls  {o['total_tokens']:>14,} tokens"
            )
    tools = report.get("tools") or []
    if tools:
        lines.append("")
        lines.append("Top tools by total runtime:")
        for t in tools[:10]:
            calls = t.get("calls") or 0
            errors = t.get("errors") or 0
            err = f"  {errors} err" if errors else ""
            lines.append(
                f"  {t['tool_name']:<24} {calls:>5} calls"
                f"  {(t.get('total_ms') or 0) / 1000:>8.1f}s total"
                f"  {(t.get('max_ms') or 0) / 1000:>7.1f}s max"
                f"  {(t.get('output_chars') or 0):>12,} chars{err}"
            )
    gauges = report.get("gauges") or []
    if gauges:
        lines.append("")
        lines.append("Health gauges (first → latest sample):")
        for g in gauges:
            first = g.get("first_value") or 0.0
            last = g.get("last_value") or 0.0
            if g["name"].endswith("_bytes"):
                lines.append(
                    f"  {g['name']:<20} {first / 1e6:>10.1f} MB → {last / 1e6:>10.1f} MB"
                )
            else:
                lines.append(f"  {g['name']:<20} {first:>10.0f} → {last:>10.0f}")
    comp = report.get("compression") or {}
    if comp.get("attempts"):
        lines.append("")
        saved = comp.get("avg_saved")
        saved_txt = f", avg {saved:,.0f} tokens saved" if saved else ""
        lines.append(
            f"Compression: {comp.get('successes') or 0}/{comp['attempts']} "
            f"succeeded{saved_txt}"
        )
    hooks = report.get("hooks") or []
    if hooks:
        lines.append("")
        lines.append("Shell hooks by total runtime:")
        for h in hooks:
            lines.append(
                f"  {h['command'][:40]:<40} {h['runs']:>5} runs"
                f"  {(h.get('total_ms') or 0) / 1000:>8.1f}s total"
                f"  {h.get('timeouts') or 0} timeouts"
            )
    alerts = report.get("alerts") or []
    if alerts:
        lines.append("")
        lines.append("Alerts:")
        for a in alerts:
            lines.append(f"  [{a['created_at'][:16]}] {a['kind']}: {a['detail']}")
    proposals = report.get("proposals") or []
    if proposals:
        lines.append("")
        lines.append("Proposed maintenance (operator-confirmed):")
        for p in proposals:
            lines.append(f"  • {p}")
    return "\n".join(lines)


def _reset_for_tests() -> None:
    """Test hook: forget installation state."""
    global _installed
    with _INSTALL_LOCK:
        _installed = False


__all__ = [
    "install",
    "on_post_api_request",
    "on_post_tool_call",
    "record_compression",
    "record_hook_event",
    "sample_gauges",
    "generate_report",
    "format_report",
]
