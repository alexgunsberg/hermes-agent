#!/usr/bin/env python3
"""Lightweight local Hermes speed/overhead baseline scorecard.

The scorecard is intentionally non-secret: it records timings, counts, and byte
sizes only. It does not restart gateways, edit config, or print cron prompts,
log contents, environment variables, or memory contents.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _maybe_reexec_project_python() -> None:
    """Use the repo venv on macOS where /usr/bin/env python3 may be 3.9.

    Hermes itself requires Python 3.11+. Re-execing keeps the executable script
    path convenient while avoiding a slow pair of subprocess fallbacks for tool
    schema collection on older system interpreters.
    """
    if sys.version_info >= (3, 11) or os.environ.get("HERMES_SPEED_SCORECARD_REEXEC"):
        return
    for candidate in (
        _REPO_ROOT / ".venv" / "bin" / "python",
        _REPO_ROOT / "venv" / "bin" / "python",
    ):
        if not candidate.exists() or str(candidate) == sys.executable:
            continue
        env = os.environ.copy()
        env["HERMES_SPEED_SCORECARD_REEXEC"] = "1"
        os.execve(str(candidate), [str(candidate), __file__, *sys.argv[1:]], env)


_maybe_reexec_project_python()
_DEFAULT_COMMAND_TIMEOUT_S = 15.0
_LOG_TAIL_BYTES = 256 * 1024
_MAX_LISTED_FILES = 25
_MAX_PROCESS_SAMPLES = 10

_COMPRESSION_PATTERNS = (
    "compression warning",
    "preflight compression",
    "context window getting full",
    "context window",
    "compressing conversation",
)

_MEMORY_FILE_NAMES = (
    "memory.json",
    "user_profile.json",
    "memory_store.db",
    "memories.db",
    "holographic_memory.db",
    "facts.db",
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

_WORK_CARD_KEYWORDS = (
    "implement",
    "review",
    "orchestrat",
    "project owner",
    "cursor",
    "refactor",
    "fix ",
    "build ",
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
    "project-memory-bundles/*/handoff.md",
    "project-memory-bundles/*/active_state.md",
    "project-memory-bundles/*/artifacts/eval_raw/*packet*.md",
    "project-memory-bundles/*/artifacts/eval_raw/*packet*.json",
    "project_memory_bundles/*/*packet*.md",
    "project_memory_bundles/*/*packet*.json",
    "project_memory_bundles/*/handoff.md",
    "project_memory_bundles/*/active_state.md",
    "project_memory_bundles/*/artifacts/eval_raw/*packet*.md",
    "project_memory_bundles/*/artifacts/eval_raw/*packet*.json",
    "projects/*/pmb/*packet*.md",
    "projects/*/pmb/*packet*.json",
    "projects/*/pmb/handoff.md",
    "projects/*/pmb/active_state.md",
    "projects/*/PMB/*packet*.md",
    "projects/*/PMB/*packet*.json",
    "projects/*/PMB/handoff.md",
    "projects/*/PMB/active_state.md",
)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _human_bytes(size: int | None) -> str:
    if size is None:
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _safe_rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _iter_existing(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        yield resolved


def _time_command(cmd: list[str], timeout_s: float, cwd: Path) -> dict[str, Any]:
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
        return {
            "command": " ".join(cmd),
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "elapsed_s": round(elapsed, 4),
            "stdout_bytes": len(proc.stdout or b""),
            "stderr_bytes": len(proc.stderr or b""),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        return {
            "command": " ".join(cmd),
            "ok": False,
            "exit_code": None,
            "elapsed_s": round(elapsed, 4),
            "stdout_bytes": len(exc.stdout or b""),
            "stderr_bytes": len(exc.stderr or b""),
            "timed_out": True,
        }
    except OSError as exc:
        elapsed = time.perf_counter() - start
        return {
            "command": " ".join(cmd),
            "ok": False,
            "exit_code": None,
            "elapsed_s": round(elapsed, 4),
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "timed_out": False,
            "error_type": type(exc).__name__,
        }


def _hermes_cmd() -> list[str]:
    hermes = shutil.which("hermes")
    if hermes:
        return [hermes]
    return [sys.executable, "-m", "hermes_cli.main"]


def _project_python() -> str:
    for candidate in (
        _REPO_ROOT / ".venv" / "bin" / "python",
        _REPO_ROOT / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def collect_command_timings(
    *,
    timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
    live_chat: bool = False,
    cwd: Path = _REPO_ROOT,
) -> dict[str, Any]:
    hermes = _hermes_cmd()
    timings = {
        "python_startup": _time_command(
            [sys.executable, "-c", "pass"], timeout_s=min(timeout_s, 5.0), cwd=cwd
        ),
        "hermes_cli_version_startup": _time_command(hermes + ["--version"], timeout_s=timeout_s, cwd=cwd),
        "gateway_status_latency_proxy": _time_command(
            hermes + ["gateway", "status"], timeout_s=timeout_s, cwd=cwd
        ),
        "simple_chat_latency_proxy": _time_command(
            hermes + ["chat", "--help"], timeout_s=timeout_s, cwd=cwd
        ),
    }
    if live_chat:
        timings["simple_chat_live_no_tools"] = _time_command(
            hermes
            + [
                "chat",
                "-q",
                "Reply exactly: HERMES_SPEED_OK",
                "-Q",
                "--toolsets",
                "",
            ],
            timeout_s=max(timeout_s, 60.0),
            cwd=cwd,
        )
    else:
        timings["simple_chat_live_no_tools"] = {"skipped": True, "reason": "pass --live-chat to run a model call"}
    return timings


def collect_tool_schema_metrics(*, exact_tools: bool = False) -> dict[str, Any]:
    def _static_summary(label: str) -> dict[str, Any]:
        try:
            names: list[str] = []
            per_tool: list[dict[str, Any]] = []
            schema_source_bytes = 0
            for path in sorted((_REPO_ROOT / "tools").glob("*.py")):
                if path.name in {"__init__.py", "registry.py", "mcp_tool.py"}:
                    continue
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                for node in tree.body:
                    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                        continue
                    func = node.value.func
                    if not (
                        isinstance(func, ast.Attribute)
                        and func.attr == "register"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "registry"
                    ):
                        continue
                    tool_name = ""
                    schema_text = ""
                    for keyword in node.value.keywords:
                        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                            tool_name = str(keyword.value.value)
                        elif keyword.arg == "schema":
                            schema_text = ast.get_source_segment(source, keyword.value) or ""
                    schema_bytes = len(schema_text.encode("utf-8"))
                    if tool_name:
                        names.append(tool_name)
                        per_tool.append(
                            {
                                "name": tool_name,
                                "source_file": path.name,
                                "schema_bytes": schema_bytes + len(tool_name.encode("utf-8")),
                                "approx_schema_tokens": (schema_bytes + len(tool_name.encode("utf-8"))) // 4,
                            }
                        )
                    schema_source_bytes += schema_bytes
            payload_bytes = schema_source_bytes + sum(len(name.encode("utf-8")) for name in names)
            per_tool.sort(key=lambda item: int(item["schema_bytes"]), reverse=True)
            return {
                "label": label,
                "count": len(names),
                "schema_bytes": payload_bytes,
                "approx_schema_tokens": payload_bytes // 4,
                "names_sample": sorted(names)[:50],
                "top_heavy_tools": per_tool[:15],
                "collector": "static_ast_approx_no_imports",
            }
        except Exception as exc:
            return {"label": label, "error": type(exc).__name__, "message": str(exc)[:200]}

    def _subprocess_summary(label: str, strip_kanban_env: bool, reason: str) -> dict[str, Any]:
        env = os.environ.copy()
        if strip_kanban_env:
            env.pop("HERMES_KANBAN_TASK", None)
            env.pop("HERMES_KANBAN_BOARD", None)
        code = f"""
import json, sys
sys.path.insert(0, {str(_REPO_ROOT)!r})
from model_tools import get_tool_definitions
tools = get_tool_definitions(quiet_mode=True)
payload = json.dumps(tools, sort_keys=True, separators=(",", ":"))
names = [tool.get("function", {{}}).get("name", "") for tool in tools]
print(json.dumps({{
    "label": {label!r},
    "count": len(tools),
    "schema_bytes": len(payload.encode("utf-8")),
    "approx_schema_tokens": len(payload) // 4,
    "names_sample": names[:50],
    "collector": "subprocess",
    "subprocess_reason": {reason[:120]!r},
}}))
"""
        try:
            proc = subprocess.run(
                [_project_python(), "-c", code],
                cwd=str(_REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
            )
        except Exception as exc:
            return {"label": label, "error": type(exc).__name__, "message": str(exc)[:200]}
        if proc.returncode != 0:
            return {
                "label": label,
                "error": "subprocess_failed",
                "exit_code": proc.returncode,
                "stderr_bytes": len(proc.stderr or ""),
            }
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {"label": label, "error": "no_json_from_subprocess"}

    def _summarize(label: str, strip_kanban_env: bool) -> dict[str, Any]:
        old_task = os.environ.get("HERMES_KANBAN_TASK")
        old_board = os.environ.get("HERMES_KANBAN_BOARD")
        if strip_kanban_env:
            os.environ.pop("HERMES_KANBAN_TASK", None)
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                from model_tools import get_tool_definitions

                tools = get_tool_definitions(quiet_mode=True)
            payload = json.dumps(tools, sort_keys=True, separators=(",", ":"))
            names = [tool.get("function", {}).get("name", "") for tool in tools]
            return {
                "label": label,
                "count": len(tools),
                "schema_bytes": len(payload.encode("utf-8")),
                "approx_schema_tokens": len(payload) // 4,
                "names_sample": names[:50],
                "collector": "in_process",
            }
        except Exception as exc:
            fallback = _subprocess_summary(label, strip_kanban_env, f"{type(exc).__name__}: {exc}")
            if "error" not in fallback:
                return fallback
            return {"label": label, "error": type(exc).__name__, "message": str(exc)[:200]}
        finally:
            if strip_kanban_env:
                if old_task is not None:
                    os.environ["HERMES_KANBAN_TASK"] = old_task
                if old_board is not None:
                    os.environ["HERMES_KANBAN_BOARD"] = old_board

    if not exact_tools:
        static = _static_summary("normal_chat_baseline")
        return {
            "mode": "static_ast_approx_no_imports",
            "current_environment_has_kanban_task": bool(os.environ.get("HERMES_KANBAN_TASK")),
            "current_environment": static,
            "normal_chat_baseline": static,
            "note": "Default is a lightweight AST schema approximation; pass --exact-tools to import tools and run availability check_fn probes.",
        }
    return {
        "mode": "exact_available_after_check_fn",
        "current_environment_has_kanban_task": bool(os.environ.get("HERMES_KANBAN_TASK")),
        "current_environment": _summarize("current_environment", strip_kanban_env=False),
        "normal_chat_baseline": _summarize("normal_chat_baseline", strip_kanban_env=True),
    }


def collect_memory_metrics(hermes_home: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    candidates.extend(hermes_home / name for name in _MEMORY_FILE_NAMES)
    candidates.extend((hermes_home / "memory").glob("**/*") if (hermes_home / "memory").exists() else [])
    candidates.extend((hermes_home / "memories").glob("**/*") if (hermes_home / "memories").exists() else [])
    candidates.extend((hermes_home / "holographic").glob("**/*") if (hermes_home / "holographic").exists() else [])

    files = []
    total_bytes = 0
    for path in _iter_existing(candidates):
        if not path.is_file():
            continue
        size = _file_size(path)
        if size is None:
            continue
        total_bytes += size
        files.append({"path": _safe_rel(path, hermes_home), "bytes": size})
    files.sort(key=lambda item: item["bytes"], reverse=True)
    return {
        "total_bytes": total_bytes,
        "human_total": _human_bytes(total_bytes),
        "file_count": len(files),
        "largest_files": files[:_MAX_LISTED_FILES],
    }


def _skill_roots(hermes_home: Path, repo_root: Path) -> list[Path]:
    roots = [hermes_home / "skills", repo_root / "skills", repo_root / "optional-skills"]
    extra = os.environ.get("HERMES_SKILLS_PATH", "")
    for raw in extra.split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw).expanduser())
    return [path for path in _iter_existing(roots) if path.is_dir()]


def _skill_name_from_path(path: Path, root: Path) -> str:
    return path.parent.relative_to(root).as_posix()


def _looks_like_skill_metric_name(name: str) -> bool:
    if not (0 < len(name) <= 96):
        return False
    if any(ch in name for ch in "\\{}[]()+*?|\n\r\t"):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.@/-]+", name))


def collect_skill_metrics(hermes_home: Path, repo_root: Path) -> dict[str, Any]:
    """Measure loaded-skill frequency and prompt-heavy skill files."""
    skill_files: list[dict[str, Any]] = []
    for root in _skill_roots(hermes_home, repo_root):
        for path in root.glob("**/SKILL.md"):
            if not path.is_file():
                continue
            size = _file_size(path)
            if size is None:
                continue
            try:
                name = _skill_name_from_path(path, root)
            except ValueError:
                name = path.parent.name
            base = repo_root if str(root).startswith(str(repo_root)) else hermes_home
            skill_files.append({
                "name": name,
                "root": _safe_rel(root, base),
                "skill_md_bytes": size,
                "approx_skill_md_tokens": size // 4,
            })
    skill_files.sort(key=lambda item: int(item["skill_md_bytes"]), reverse=True)

    state_db = hermes_home / "state.db"
    frequency: Counter[str] = Counter()
    scanned_rows = 0
    sessions_scanned = 0
    regexes = (
        re.compile(r'launched this CLI session with the "([^"]+)" skill preloaded', re.IGNORECASE),
        re.compile(r'user has invoked the "([^"]+)" skill', re.IGNORECASE),
        re.compile(r'\[IMPORTANT:.*?"([^"]+)" skill', re.IGNORECASE | re.DOTALL),
    )
    if state_db.exists():
        try:
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            try:
                seen_sessions = set()
                rows: list[tuple[Any, str]] = []
                rows.extend(
                    (row[0], row[1])
                    for row in conn.execute(
                        """
                        SELECT id, COALESCE(system_prompt, '')
                        FROM sessions
                        WHERE COALESCE(archived, 0) = 0
                          AND (started_at IS NULL OR started_at >= strftime('%s','now','-30 days'))
                          AND system_prompt LIKE '%skill%'
                        ORDER BY started_at DESC
                        LIMIT 1000
                        """
                    ).fetchall()
                )
                rows.extend(
                    (row[0], row[1])
                    for row in conn.execute(
                        """
                        SELECT m.session_id, COALESCE(m.content, '')
                        FROM messages m
                        JOIN sessions s ON s.id = m.session_id
                        WHERE COALESCE(s.archived, 0) = 0
                          AND (s.started_at IS NULL OR s.started_at >= strftime('%s','now','-30 days'))
                          AND m.content LIKE '%skill%'
                        ORDER BY s.started_at DESC
                        LIMIT 2000
                        """
                    ).fetchall()
                )
                for session_id, haystack in rows:
                    scanned_rows += 1
                    seen_sessions.add(session_id)
                    names_in_row: set[str] = set()
                    for regex in regexes:
                        for match in regex.finditer(haystack):
                            name = match.group(1).strip()
                            if _looks_like_skill_metric_name(name):
                                names_in_row.add(name)
                    frequency.update(names_in_row)
                sessions_scanned = len(seen_sessions)
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    return {
        "installed_skill_count": len(skill_files),
        "top_prompt_heavy_skills": skill_files[:15],
        "loaded_frequency_window": "last 30 days, non-archived state.db rows containing skill markers",
        "loaded_frequency_rows_scanned": scanned_rows,
        "loaded_frequency_sessions_scanned": sessions_scanned,
        "loaded_frequency_top": [
            {"name": name, "count": count} for name, count in frequency.most_common(15)
        ],
        "non_secret_policy": "skill names, counts, and byte sizes only; no skill/session content",
    }


def _load_legacy_sessions_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = []
    for key, entry in data.items():
        if str(key).startswith("_") or not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry.setdefault("session_key", key)
        entries.append(entry)
    return entries


def collect_session_route_metrics(hermes_home: Path) -> dict[str, Any]:
    """Count active sessions and route-token bloat without printing prompts."""
    state_db = hermes_home / "state.db"
    report: dict[str, Any] = {
        "state_db": "state.db",
        "state_db_exists": state_db.exists(),
        "active_session_count": 0,
        "total_session_count": 0,
        "route_pin_count": 0,
        "legacy_route_pin_count": 0,
        "high_prompt_route_pins_50k": 0,
        "high_prompt_route_pins_100k": 0,
        "ended_session_route_pins": 0,
        "suspended_route_pins": 0,
        "resume_pending_route_pins": 0,
        "top_route_pins_by_prompt_tokens": [],
        "active_sessions_by_source": {},
        "non_secret_policy": "session ids, sources, counts, and token totals only; no routing keys, prompts, messages, or origin JSON",
    }

    active_sessions: dict[str, dict[str, Any]] = {}
    all_sessions: dict[str, dict[str, Any]] = {}
    route_entries: list[dict[str, Any]] = []
    if state_db.exists():
        try:
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                session_rows = conn.execute(
                    """
                    SELECT id, source, started_at, ended_at, end_reason,
                           message_count, tool_call_count, input_tokens,
                           output_tokens, cache_read_tokens, cache_write_tokens,
                           archived
                    FROM sessions
                    """
                ).fetchall()
                for row in session_rows:
                    sid = str(row["id"])
                    item = dict(row)
                    all_sessions[sid] = item
                    if not row["ended_at"] and not row["end_reason"] and not row["archived"]:
                        active_sessions[sid] = item
                report["active_session_count"] = len(active_sessions)
                report["total_session_count"] = len(all_sessions)
                report["active_sessions_by_source"] = dict(
                    Counter(str(row.get("source") or "unknown") for row in active_sessions.values())
                )

                for row in conn.execute("SELECT scope, session_key, entry_json, updated_at FROM gateway_routing").fetchall():
                    try:
                        entry = json.loads(row["entry_json"])
                    except Exception:
                        continue
                    if isinstance(entry, dict):
                        entry["_scope"] = row["scope"]
                        entry["_updated_at"] = row["updated_at"]
                        route_entries.append(entry)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            report["state_db_error"] = type(exc).__name__

    legacy_entries: list[dict[str, Any]] = []
    for path in _iter_existing([hermes_home / "sessions" / "sessions.json"]):
        if path.is_file():
            legacy_entries.extend(_load_legacy_sessions_json(path))
    report["legacy_route_pin_count"] = len(legacy_entries)
    report["route_pin_count"] = len(route_entries)

    high_samples: list[dict[str, Any]] = []
    for entry in [*route_entries, *legacy_entries]:
        sid = str(entry.get("session_id") or "")
        last_prompt = int(entry.get("last_prompt_tokens") or 0)
        if last_prompt >= 50_000:
            report["high_prompt_route_pins_50k"] += 1
        if last_prompt >= 100_000:
            report["high_prompt_route_pins_100k"] += 1
        if entry.get("suspended"):
            report["suspended_route_pins"] += 1
        if entry.get("resume_pending"):
            report["resume_pending_route_pins"] += 1
        session = all_sessions.get(sid)
        ended = bool(session and (session.get("ended_at") or session.get("end_reason") or session.get("archived")))
        if ended:
            report["ended_session_route_pins"] += 1
        if last_prompt:
            high_samples.append({
                "session_id": sid,
                "source": (session or {}).get("source") or entry.get("platform"),
                "last_prompt_tokens": last_prompt,
                "total_tokens": int(entry.get("total_tokens") or 0),
                "suspended": bool(entry.get("suspended")),
                "resume_pending": bool(entry.get("resume_pending")),
                "ended_in_state_db": ended,
            })
    high_samples.sort(key=lambda item: int(item.get("last_prompt_tokens") or 0), reverse=True)
    report["top_route_pins_by_prompt_tokens"] = high_samples[:_MAX_PROCESS_SAMPLES]
    return report


def build_fast_path_recommendations(report: dict[str, Any]) -> list[str]:
    recommendations = [
        "Use scoped toolsets for routine chat/cron/delegation runs; keep exact broad tool schemas only for sessions that need them.",
        "Load heavy skill linked files on demand instead of preloading all support material into the prompt.",
        "Move detailed durable project knowledge to PMB packets and keep always-injected memory compact.",
        "For high-token route pins, prefer /new or a compact preflight packet after the task mode changes; prune only after backup and exact target evidence.",
    ]
    routes = report.get("session_routes", {}) if isinstance(report.get("session_routes"), dict) else {}
    skills = report.get("skills", {}) if isinstance(report.get("skills"), dict) else {}
    tools = report.get("tool_schemas", {}) if isinstance(report.get("tool_schemas"), dict) else {}
    normal_tools = tools.get("normal_chat_baseline", {}) if isinstance(tools, dict) else {}
    if int(routes.get("high_prompt_route_pins_50k") or 0):
        recommendations.append("Immediate candidate: start fresh gateway sessions for route pins above 50k prompt tokens before doing more agent work in those topics.")
    if skills.get("top_prompt_heavy_skills"):
        recommendations.append("Immediate candidate: inspect top_prompt_heavy_skills and split reference/runbook material into linked files if it is always preloaded.")
    if int(normal_tools.get("approx_schema_tokens") or 0) > 10_000:
        recommendations.append("Immediate candidate: make recurring jobs pass enabled_toolsets so narrow tasks do not pay broad schema cost.")
    return recommendations


def _candidate_pmb_files(roots: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            paths.append(root)
            continue
        for pattern in _PMB_GLOBS:
            try:
                paths.extend(path for path in root.glob(pattern) if path.is_file())
            except OSError:
                continue
    return list(_iter_existing(paths))


def collect_pmb_metrics(hermes_home: Path, repo_root: Path, pmb_paths: list[Path] | None = None) -> dict[str, Any]:
    roots = pmb_paths or [hermes_home, repo_root]
    files = []
    total_bytes = 0
    for path in _candidate_pmb_files(roots):
        size = _file_size(path)
        if size is None:
            continue
        total_bytes += size
        base = hermes_home if str(path).startswith(str(hermes_home)) else repo_root
        files.append({"path": _safe_rel(path, base), "bytes": size})
    files.sort(key=lambda item: item["bytes"], reverse=True)
    return {
        "total_bytes": total_bytes,
        "human_total": _human_bytes(total_bytes),
        "file_count": len(files),
        "largest_files": files[:_MAX_LISTED_FILES],
        "note": "Pass --pmb-path to measure a specific PMB packet/source if auto-discovery misses it.",
    }


def collect_compression_warning_metrics(hermes_home: Path) -> dict[str, Any]:
    log_dir = hermes_home / "logs"
    counts: Counter[str] = Counter()
    files_seen = 0
    bytes_scanned = 0
    if not log_dir.exists():
        return {"total_matches": 0, "files_seen": 0, "bytes_scanned": 0, "by_pattern": {}}

    for path in sorted(log_dir.glob("*.log")):
        if not path.is_file():
            continue
        files_seen += 1
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _LOG_TAIL_BYTES:
                    handle.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
                raw = handle.read(_LOG_TAIL_BYTES)
        except OSError:
            continue
        bytes_scanned += len(raw)
        text = raw.decode("utf-8", errors="ignore").lower()
        for pattern in _COMPRESSION_PATTERNS:
            counts[pattern] += text.count(pattern)
    return {
        "total_matches": sum(counts.values()),
        "files_seen": files_seen,
        "bytes_scanned": bytes_scanned,
        "by_pattern": dict(counts),
        "scan_scope": f"last {_human_bytes(_LOG_TAIL_BYTES)} per log file under logs/",
    }


def collect_cron_metrics(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "cron" / "jobs.json"
    if not path.exists():
        return {"jobs_path": "cron/jobs.json", "exists": False, "total": 0, "enabled": 0, "disabled": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"jobs_path": "cron/jobs.json", "exists": True, "error": type(exc).__name__}

    if isinstance(data, dict):
        raw_jobs = data.get("jobs", data)
        jobs = list(raw_jobs.values()) if isinstance(raw_jobs, dict) else list(raw_jobs or [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []

    status_counts: Counter[str] = Counter()
    enabled = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("enabled", True):
            enabled += 1
        status = str(job.get("status") or ("enabled" if job.get("enabled", True) else "disabled"))
        status_counts[status] += 1
    return {
        "jobs_path": "cron/jobs.json",
        "exists": True,
        "total": len(jobs),
        "enabled": enabled,
        "disabled": len(jobs) - enabled,
        "by_status": dict(status_counts),
    }


def _row_value(row: sqlite3.Row | dict[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        if name in row.keys():
            return row[name]
    except Exception:
        pass
    return default


def _compact_task_sample(row: sqlite3.Row | dict[str, Any], **extra: Any) -> dict[str, Any]:
    sample = {
        "task_id": _row_value(row, "id"),
        "status": _row_value(row, "status"),
        "assignee": _row_value(row, "assignee"),
    }
    sample.update(extra)
    return sample


def _missing_marker_keys(text: str, markers: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = text.casefold()
    return [key for key, choices in markers.items() if not any(choice.casefold() in lowered for choice in choices)]


def _looks_like_handoff_contract(text: str) -> bool:
    lowered = text.casefold()
    if not re.search(r"\bhandoff\b", lowered):
        return False
    # Many governance cards contain generic policy prose such as "no handoff
    # valid until...". Count only actual handoff records/comments/events that
    # include a handoff label or from/to field, not policy text about handoffs.
    if "handoff:" in lowered:
        return True
    boundary_markers = _HANDOFF_MARKERS["handoff_from"] + _HANDOFF_MARKERS["handoff_to"]
    return any(marker.casefold() in lowered for marker in boundary_markers)


def _parse_event_payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception:
            return payload
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if isinstance(parsed, (dict, list)) else str(parsed)
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return str(payload)


def _latest_event_ts(events: list[sqlite3.Row], kinds: set[str]) -> int | None:
    for event in reversed(events):
        if str(_row_value(event, "kind", "")) in kinds:
            ts = _row_value(event, "created_at")
            return int(ts) if ts is not None else None
    return None


def _duration_observation(
    bucket: dict[str, Any],
    *,
    seconds: int,
    task_id: str | None = None,
    status: str | None = None,
    label: str | None = None,
) -> None:
    seconds = max(0, int(seconds))
    bucket["observation_count"] += 1
    bucket["total_seconds"] += seconds
    bucket["max_seconds"] = max(bucket["max_seconds"], seconds)
    if len(bucket["samples"]) < _MAX_PROCESS_SAMPLES and task_id:
        sample: dict[str, Any] = {"task_id": task_id, "seconds": seconds}
        if status:
            sample["status"] = status
        if label:
            sample["label"] = label
        bucket["samples"].append(sample)


def _new_duration_bucket() -> dict[str, Any]:
    return {"observation_count": 0, "total_seconds": 0, "max_seconds": 0, "samples": []}


def _finalize_duration_bucket(name: str, bucket: dict[str, Any]) -> dict[str, Any]:
    count = int(bucket.get("observation_count") or 0)
    total = int(bucket.get("total_seconds") or 0)
    return {
        "name": name,
        "observation_count": count,
        "total_seconds": total,
        "max_seconds": int(bucket.get("max_seconds") or 0),
        "avg_seconds": round(total / count, 2) if count else 0,
        "samples": bucket.get("samples", []),
    }


def _scorecard_kanban_db_path(kanban_board: str | None) -> tuple[str, Path]:
    try:
        from hermes_cli import kanban_db as kb

        board = kanban_board or kb.get_current_board()
        return board, kb.kanban_db_path(board)
    except Exception:
        board = kanban_board or os.environ.get("HERMES_KANBAN_BOARD") or "default"
        override = os.environ.get("HERMES_KANBAN_DB", "").strip()
        if override:
            return board, Path(override).expanduser()
        return board, _hermes_home() / "kanban.db"


def collect_process_timer_metrics(
    hermes_home: Path,
    *,
    kanban_board: str | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Measure Kanban process timers and owner gaps without leaking task prose.

    The report emits task ids, statuses, assignees, counts, and durations only.
    Card bodies/comments/event payloads are scanned in-process for markers but
    are never copied into the JSON/Markdown output.
    """
    now = int(now_ts if now_ts is not None else time.time())
    board, db_path = _scorecard_kanban_db_path(kanban_board)
    report: dict[str, Any] = {
        "board": board,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "non_secret_policy": "task ids, statuses, assignees, counts, and durations only; no card bodies, comments, or event payload text",
    }
    if not db_path.exists():
        report.update({
            "active_tasks": 0,
            "completed_tasks": 0,
            "timers": {},
            "ownership_gaps": {},
            "top_bottlenecks": [],
        })
        return report

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        report.update({"error": type(exc).__name__, "message": str(exc)[:200]})
        return report
    conn.row_factory = sqlite3.Row
    try:
        tasks = conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall()
        task_ids = [str(row["id"]) for row in tasks]
        events_by_task: dict[str, list[sqlite3.Row]] = {task_id: [] for task_id in task_ids}
        runs: list[sqlite3.Row] = []
        comments_by_task: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        if task_ids:
            placeholders = ",".join(["?"] * len(task_ids))
            for row in conn.execute(
                f"SELECT task_id, kind, payload, created_at FROM task_events WHERE task_id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
                tuple(task_ids),
            ):
                events_by_task.setdefault(str(row["task_id"]), []).append(row)
            runs = conn.execute(
                f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY started_at ASC, id ASC",
                tuple(task_ids),
            ).fetchall()
            try:
                for row in conn.execute(
                    f"SELECT task_id, body FROM task_comments WHERE task_id IN ({placeholders})",
                    tuple(task_ids),
                ):
                    comments_by_task.setdefault(str(row["task_id"]), []).append(str(row["body"] or ""))
            except sqlite3.Error:
                comments_by_task = {task_id: [] for task_id in task_ids}
    except sqlite3.Error as exc:
        report.update({"error": type(exc).__name__, "message": str(exc)[:200]})
        return report
    finally:
        conn.close()

    timer_buckets = {
        "created_to_started_wait": _new_duration_bucket(),
        "started_to_terminal_runtime": _new_duration_bucket(),
        "ready_queue_age": _new_duration_bucket(),
        "todo_dependency_age": _new_duration_bucket(),
        "running_age": _new_duration_bucket(),
        "blocked_age": _new_duration_bucket(),
        "review_required_age": _new_duration_bucket(),
        "heartbeat_age_running": _new_duration_bucket(),
        "closed_run_duration": _new_duration_bucket(),
        "active_run_age": _new_duration_bucket(),
    }
    status_counts: Counter[str] = Counter()
    missing_owner_counts: Counter[str] = Counter()
    handoff_gap_counts: Counter[str] = Counter()
    missing_owner_samples: list[dict[str, Any]] = []
    handoff_gap_samples: list[dict[str, Any]] = []
    non_goal_samples: list[dict[str, Any]] = []
    tasks_with_missing_owner_fields = 0
    handoffs_with_missing_contract_fields = 0

    for task in tasks:
        task_id = str(_row_value(task, "id"))
        status = str(_row_value(task, "status", ""))
        status_counts[status] += 1
        created_at = _row_value(task, "created_at")
        started_at = _row_value(task, "started_at")
        completed_at = _row_value(task, "completed_at")
        events = events_by_task.get(task_id, [])
        if created_at is not None and started_at is not None:
            _duration_observation(
                timer_buckets["created_to_started_wait"],
                seconds=int(started_at) - int(created_at),
                task_id=task_id,
                status=status,
            )
        if started_at is not None and completed_at is not None:
            _duration_observation(
                timer_buckets["started_to_terminal_runtime"],
                seconds=int(completed_at) - int(started_at),
                task_id=task_id,
                status=status,
            )
        if status == "running" and started_at is not None:
            _duration_observation(
                timer_buckets["running_age"],
                seconds=now - int(started_at),
                task_id=task_id,
                status=status,
            )
            heartbeat_at = _row_value(task, "last_heartbeat_at") or _latest_event_ts(events, {"heartbeat"})
            if heartbeat_at:
                _duration_observation(
                    timer_buckets["heartbeat_age_running"],
                    seconds=now - int(heartbeat_at),
                    task_id=task_id,
                    status=status,
                )
        if status == "ready":
            ready_since = _latest_event_ts(events, {"promoted", "unblocked", "reclaimed", "created"}) or created_at
            if ready_since is not None:
                _duration_observation(
                    timer_buckets["ready_queue_age"],
                    seconds=now - int(ready_since),
                    task_id=task_id,
                    status=status,
                )
        if status == "todo":
            if created_at is not None:
                _duration_observation(
                    timer_buckets["todo_dependency_age"],
                    seconds=now - int(created_at),
                    task_id=task_id,
                    status=status,
                )
        if status == "blocked":
            blocked_since = _latest_event_ts(events, {"blocked", "spawn_auto_blocked"}) or created_at
            if blocked_since is not None:
                _duration_observation(
                    timer_buckets["blocked_age"],
                    seconds=now - int(blocked_since),
                    task_id=task_id,
                    status=status,
                )
            event_payload_text = " ".join(_parse_event_payload_text(_row_value(event, "payload")) for event in events[-5:])
            if "review-required" in f"{_row_value(task, 'body', '')} {_row_value(task, 'result', '')} {event_payload_text}".casefold() and blocked_since is not None:
                _duration_observation(
                    timer_buckets["review_required_age"],
                    seconds=now - int(blocked_since),
                    task_id=task_id,
                    status=status,
                )

        body = str(_row_value(task, "body", "") or "")
        missing = _missing_marker_keys(body, _OWNER_MARKERS)
        if status not in {"done", "archived"} and missing:
            tasks_with_missing_owner_fields += 1
            missing_owner_counts.update(missing)
            if len(missing_owner_samples) < _MAX_PROCESS_SAMPLES:
                missing_owner_samples.append(_compact_task_sample(task, missing=missing))
        goal_mode = bool(_row_value(task, "goal_mode", False))
        title_and_body = f"{_row_value(task, 'title', '')} {body}".casefold()
        if status not in {"done", "archived", "triage"} and not goal_mode and any(word in title_and_body for word in _WORK_CARD_KEYWORDS):
            if len(non_goal_samples) < _MAX_PROCESS_SAMPLES:
                non_goal_samples.append(_compact_task_sample(task))

        handoff_texts = [body, *comments_by_task.get(task_id, [])]
        handoff_texts.extend(
            _parse_event_payload_text(_row_value(event, "payload"))
            for event in events
            if "handoff" in f"{_row_value(event, 'kind', '')} {_parse_event_payload_text(_row_value(event, 'payload'))}".casefold()
        )
        for text in handoff_texts:
            if not _looks_like_handoff_contract(text):
                continue
            missing_handoff = _missing_marker_keys(text, _HANDOFF_MARKERS)
            if not missing_handoff:
                continue
            handoffs_with_missing_contract_fields += 1
            handoff_gap_counts.update(missing_handoff)
            if len(handoff_gap_samples) < _MAX_PROCESS_SAMPLES:
                handoff_gap_samples.append(_compact_task_sample(task, missing=missing_handoff))
            break

    non_success_runs = 0
    run_outcomes: Counter[str] = Counter()
    for run in runs:
        task_id = str(_row_value(run, "task_id", ""))
        started_at = _row_value(run, "started_at")
        ended_at = _row_value(run, "ended_at")
        status = str(_row_value(run, "status", ""))
        outcome = str(_row_value(run, "outcome", "active") or "active")
        run_outcomes[outcome] += 1
        if outcome not in {"completed", "reclaimed", "active", "None"}:
            non_success_runs += 1
        if started_at is None:
            continue
        if ended_at is not None:
            _duration_observation(
                timer_buckets["closed_run_duration"],
                seconds=int(ended_at) - int(started_at),
                task_id=task_id,
                status=status,
                label=outcome,
            )
        elif status == "running":
            _duration_observation(
                timer_buckets["active_run_age"],
                seconds=now - int(started_at),
                task_id=task_id,
                status=status,
                label=outcome,
            )

    timers = {name: _finalize_duration_bucket(name, bucket) for name, bucket in timer_buckets.items()}
    bottlenecks = [item for item in timers.values() if item["observation_count"]]
    if non_success_runs:
        bottlenecks.append({
            "name": "non_success_run_repetition",
            "observation_count": non_success_runs,
            "total_seconds": 0,
            "max_seconds": 0,
            "avg_seconds": 0,
            "samples": [],
        })
    bottlenecks.sort(key=lambda item: (int(item.get("total_seconds") or 0), int(item.get("observation_count") or 0)), reverse=True)

    active_count = sum(1 for task in tasks if str(_row_value(task, "status", "")) not in {"done", "archived"})
    completed_count = sum(1 for task in tasks if str(_row_value(task, "status", "")) == "done")
    report.update({
        "generated_at_epoch": now,
        "active_tasks": active_count,
        "completed_tasks": completed_count,
        "status_counts": dict(status_counts),
        "run_outcomes": dict(run_outcomes),
        "non_success_runs": non_success_runs,
        "timers": timers,
        "ownership_gaps": {
            "tasks_with_missing_owner_fields": tasks_with_missing_owner_fields,
            "missing_by_field": dict(missing_owner_counts),
            "samples": missing_owner_samples,
            "non_goal_work_cards": len(non_goal_samples),
            "non_goal_samples": non_goal_samples,
            "handoffs_with_missing_contract_fields": handoffs_with_missing_contract_fields,
            "handoff_missing_by_field": dict(handoff_gap_counts),
            "handoff_samples": handoff_gap_samples,
        },
        "top_bottlenecks": bottlenecks[:10],
        "improvement_card_policy": "Create follow-up cards only from top_bottlenecks entries with observation_count > 0 and measured total_seconds or repeated non-success runs.",
    })
    return report


def collect_scorecard(
    *,
    run_commands: bool = True,
    live_chat: bool = False,
    exact_tools: bool = False,
    command_timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
    pmb_paths: list[Path] | None = None,
    kanban_board: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    hermes_home = _hermes_home().expanduser()
    report: dict[str, Any] = {
        "generated_at": _iso_now(),
        "repo_root": str(_REPO_ROOT),
        "hermes_home": str(hermes_home),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "non_secret_policy": "counts, timings, and byte sizes only; no env values, prompts, logs, or memory contents",
    }
    report["timings"] = (
        collect_command_timings(timeout_s=command_timeout_s, live_chat=live_chat)
        if run_commands
        else {"skipped": True, "reason": "--skip-commands"}
    )
    report["tool_schemas"] = collect_tool_schema_metrics(exact_tools=exact_tools)
    report["active_memory"] = collect_memory_metrics(hermes_home)
    report["skills"] = collect_skill_metrics(hermes_home, _REPO_ROOT)
    report["session_routes"] = collect_session_route_metrics(hermes_home)
    report["pmb_packet"] = collect_pmb_metrics(hermes_home, _REPO_ROOT, pmb_paths)
    report["compression_warnings"] = collect_compression_warning_metrics(hermes_home)
    report["cron_jobs"] = collect_cron_metrics(hermes_home)
    report["process_timers"] = collect_process_timer_metrics(hermes_home, kanban_board=kanban_board)
    report["fast_path_recommendations"] = build_fast_path_recommendations(report)
    report["scorecard_runtime_s"] = round(time.perf_counter() - started, 4)
    return report


def _metric_line(label: str, value: Any) -> str:
    return f"- {label}: {value}"


def render_markdown(report: dict[str, Any]) -> str:
    timings = report.get("timings", {})
    tools = report.get("tool_schemas", {})
    normal_tools = tools.get("normal_chat_baseline", {}) if isinstance(tools, dict) else {}
    memory = report.get("active_memory", {})
    skills = report.get("skills", {}) if isinstance(report.get("skills", {}), dict) else {}
    routes = report.get("session_routes", {}) if isinstance(report.get("session_routes", {}), dict) else {}
    pmb = report.get("pmb_packet", {})
    compression = report.get("compression_warnings", {})
    cron = report.get("cron_jobs", {})
    process = report.get("process_timers", {}) if isinstance(report.get("process_timers", {}), dict) else {}
    ownership = process.get("ownership_gaps", {}) if isinstance(process, dict) else {}

    lines = [
        "# Hermes speed scorecard",
        "",
        _metric_line("generated_at", report.get("generated_at")),
        _metric_line("repo_root", report.get("repo_root")),
        _metric_line("hermes_home", report.get("hermes_home")),
        _metric_line("scorecard_runtime_s", report.get("scorecard_runtime_s")),
        "",
        "## Timings",
    ]
    if timings.get("skipped"):
        lines.append(_metric_line("commands", f"skipped ({timings.get('reason')})"))
    else:
        for key, value in timings.items():
            if isinstance(value, dict) and value.get("skipped"):
                lines.append(_metric_line(key, f"skipped ({value.get('reason')})"))
            elif isinstance(value, dict):
                lines.append(
                    _metric_line(
                        key,
                        f"{value.get('elapsed_s')}s exit={value.get('exit_code')} timeout={value.get('timed_out')}",
                    )
                )
    lines.extend([
        "",
        "## Process timers and ownership gaps",
        _metric_line("kanban_board", process.get("board")),
        _metric_line("active_tasks", process.get("active_tasks")),
        _metric_line("completed_tasks", process.get("completed_tasks")),
        _metric_line("status_counts", process.get("status_counts", {})),
        _metric_line(
            "missing_owner_fields",
            f"{ownership.get('tasks_with_missing_owner_fields', 0)} tasks / {ownership.get('missing_by_field', {})}",
        ),
        _metric_line("non_goal_work_cards", ownership.get("non_goal_work_cards", 0)),
        _metric_line(
            "handoff_contract_gaps",
            f"{ownership.get('handoffs_with_missing_contract_fields', 0)} handoffs / {ownership.get('handoff_missing_by_field', {})}",
        ),
        "",
        "### Top measured bottlenecks",
    ])
    bottlenecks = process.get("top_bottlenecks", []) if isinstance(process, dict) else []
    if bottlenecks:
        for item in bottlenecks[:5]:
            lines.append(
                _metric_line(
                    item.get("name"),
                    f"total={item.get('total_seconds')}s avg={item.get('avg_seconds')}s max={item.get('max_seconds')}s observations={item.get('observation_count')}",
                )
            )
    else:
        lines.append(_metric_line("bottlenecks", "none measured"))
    lines.extend(
        [
            "",
            "## Overhead counts",
            _metric_line(
                "normal_chat_tool_schema_count",
                normal_tools.get("count", f"error: {normal_tools.get('error')}"),
            ),
            _metric_line(
                "normal_chat_schema_bytes",
                normal_tools.get("schema_bytes", f"error: {normal_tools.get('error')}"),
            ),
            _metric_line(
                "normal_chat_approx_schema_tokens",
                normal_tools.get("approx_schema_tokens", f"error: {normal_tools.get('error')}"),
            ),
            _metric_line("active_memory", f"{memory.get('human_total')} across {memory.get('file_count')} files"),
            _metric_line("active_sessions", routes.get("active_session_count")),
            _metric_line("route_pins", f"{routes.get('route_pin_count')} primary / {routes.get('legacy_route_pin_count')} legacy"),
            _metric_line("high_prompt_route_pins", f">=50k: {routes.get('high_prompt_route_pins_50k')} / >=100k: {routes.get('high_prompt_route_pins_100k')} (primary+legacy combined)"),
            _metric_line("loaded_skill_frequency", skills.get("loaded_frequency_top", [])),
            _metric_line("top_prompt_heavy_tools", normal_tools.get("top_heavy_tools", [])[:5]),
            _metric_line("top_prompt_heavy_skills", skills.get("top_prompt_heavy_skills", [])[:5]),
            _metric_line("pmb_packet", f"{pmb.get('human_total')} across {pmb.get('file_count')} files"),
            _metric_line("compression_warning_matches", compression.get("total_matches")),
            _metric_line(
                "cron_jobs",
                f"{cron.get('total')} total / {cron.get('enabled')} enabled / {cron.get('disabled')} disabled",
            ),
            "",
            "## Notes",
            "- Gateway and simple-chat values are safe proxies unless --live-chat is passed; no gateway restart is performed.",
            "- JSON output has the full count/byte breakdown without prompts, logs, env vars, or memory contents.",
            "",
            "## Fast-path recommendations",
            *(f"- {item}" for item in report.get("fast_path_recommendations", [])),
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], output_dir: Path, prefix: str) -> tuple[Path, Path]:
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    json_path = output_dir / f"{prefix}_{stamp}.json"
    md_path = output_dir / f"{prefix}_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    (output_dir / f"{prefix}_latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (output_dir / f"{prefix}_latest.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, md_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Report directory (default: $HERMES_HOME/reports).",
    )
    parser.add_argument("--prefix", default="hermes_speed_scorecard", help="Report filename prefix.")
    parser.add_argument(
        "--skip-commands",
        action="store_true",
        help="Skip subprocess timing probes; useful for tests or very restricted shells.",
    )
    parser.add_argument(
        "--live-chat",
        action="store_true",
        help="Also run a real no-tool model call. Default only uses non-network proxies.",
    )
    parser.add_argument(
        "--exact-tools",
        action="store_true",
        help="Run tool availability checks for exact exposed schema count. Default is a faster static approximation.",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=_DEFAULT_COMMAND_TIMEOUT_S,
        help="Timeout per timing command in seconds.",
    )
    parser.add_argument(
        "--pmb-path",
        type=Path,
        action="append",
        default=None,
        help="Specific PMB packet/source path to measure; may be repeated.",
    )
    parser.add_argument(
        "--kanban-board",
        default=None,
        help="Kanban board slug to use for process timers (default: active board).",
    )
    parser.add_argument("--json-only", action="store_true", help="Print only the JSON report path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    hermes_home = _hermes_home().expanduser()
    output_dir = args.output_dir or hermes_home / "reports"
    report = collect_scorecard(
        run_commands=not args.skip_commands,
        live_chat=args.live_chat,
        exact_tools=args.exact_tools,
        command_timeout_s=args.command_timeout,
        pmb_paths=args.pmb_path,
        kanban_board=args.kanban_board,
    )
    json_path, md_path = write_reports(report, output_dir, args.prefix)
    if args.json_only:
        print(json_path)
    else:
        print(f"Wrote JSON report: {json_path}")
        print(f"Wrote Markdown report: {md_path}")
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
