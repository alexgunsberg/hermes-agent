"""Opt-in task orchestration tools for ``hermes mcp serve``.

This module keeps the default MCP messaging bridge narrow.  When explicitly
requested (e.g. ``hermes mcp serve --orchestration cursor``), it registers a
small code-delegation surface suitable for Claude Code/Fable.

Two backends are available and can be enabled independently or together
(``--orchestration all``):

* ``cursor`` — Cursor Cloud Agent (remote). Needs the repo/ref pushed to
  GitHub. Tools: ``cursor_delegate``, ``task_status``, ``task_events_poll``,
  ``task_cancel``.
* ``codex`` — the local Codex CLI (``codex exec``). Runs on a local working
  directory without any GitHub push, so it fits unpushed/local-only or
  sensitive-file changes. Tools: ``codex_delegate`` plus the shared
  ``task_status``/``task_events_poll``/``task_cancel`` (dispatched by backend).

The implementation is deliberately broker-shaped: Claude Code does not receive
Cursor credentials, the raw Cursor API, or an arbitrary local shell.  Hermes
applies model/repo/dir allowlists, persists task state, and returns reviewable
branch/changed-file metadata.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TERMINAL_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}
DEFAULT_ALLOWED_MODELS = ["composer-2.5"]

# Codex (local CLI) defaults.
DEFAULT_CODEX_BIN = "codex"
DEFAULT_CODEX_SANDBOX = "workspace-write"
ALLOWED_CODEX_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _db_path() -> Path:
    return _get_hermes_home() / "mcp_tasks.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            prompt TEXT,
            repo_url TEXT,
            starting_ref TEXT,
            model TEXT,
            agent_id TEXT,
            run_id TEXT,
            result_json TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            message TEXT,
            payload_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _task_row(task_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def _insert_event(task_id: str, event_type: str, message: str = "", payload: Optional[Dict[str, Any]] = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO task_events (task_id, timestamp, type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
            (task_id, _now(), event_type, message, _json_dumps(payload or {})),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def _upsert_task(task: Dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, backend, status, created_at, updated_at, prompt,
                repo_url, starting_ref, model, agent_id, run_id, result_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at,
                prompt=excluded.prompt,
                repo_url=excluded.repo_url,
                starting_ref=excluded.starting_ref,
                model=excluded.model,
                agent_id=excluded.agent_id,
                run_id=excluded.run_id,
                result_json=excluded.result_json,
                error=excluded.error
            """,
            (
                task["task_id"],
                task.get("backend", "cursor"),
                task.get("status", "UNKNOWN"),
                task.get("created_at") or _now(),
                task.get("updated_at") or _now(),
                task.get("prompt", ""),
                task.get("repo_url", ""),
                task.get("starting_ref", ""),
                task.get("model", ""),
                task.get("agent_id", ""),
                task.get("run_id", ""),
                _json_dumps(task.get("result", {})),
                task.get("error", ""),
            ),
        )
        conn.commit()


def _row_to_task(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    out["result"] = _json_loads(out.pop("result_json", ""), {})
    return out


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _cfg_path(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _orchestration_config() -> Dict[str, Any]:
    cfg = _load_config()
    return {
        "allowed_models": _cfg_path(
            cfg, "mcp_orchestration", "cursor", "allowed_models", default=DEFAULT_ALLOWED_MODELS
        ) or DEFAULT_ALLOWED_MODELS,
        "allowed_repos": _cfg_path(
            cfg, "mcp_orchestration", "cursor", "allowed_repos", default=[]
        ) or [],
        "allow_local_git_remotes": bool(
            _cfg_path(cfg, "mcp_orchestration", "cursor", "allow_local_git_remotes", default=True)
        ),
        "reject_unpushed_current_ref": bool(
            _cfg_path(cfg, "mcp_orchestration", "cursor", "reject_unpushed_current_ref", default=True)
        ),
    }


def _codex_config() -> Dict[str, Any]:
    cfg = _load_config()
    return {
        "bin": str(
            _cfg_path(cfg, "mcp_orchestration", "codex", "bin", default=DEFAULT_CODEX_BIN)
            or DEFAULT_CODEX_BIN
        ),
        "allowed_models": _cfg_path(
            cfg, "mcp_orchestration", "codex", "allowed_models", default=[]
        ) or [],
        "default_model": _cfg_path(
            cfg, "mcp_orchestration", "codex", "default_model", default=""
        ) or "",
        "allowed_dirs": _cfg_path(
            cfg, "mcp_orchestration", "codex", "allowed_dirs", default=[]
        ) or [],
        # Codex runs on the operator's own machine after an explicit opt-in, so
        # default to allowing any directory. Set to False + populate
        # allowed_dirs to restrict.
        "allow_any_dir": bool(
            _cfg_path(cfg, "mcp_orchestration", "codex", "allow_any_dir", default=True)
        ),
        "default_sandbox": str(
            _cfg_path(cfg, "mcp_orchestration", "codex", "default_sandbox", default=DEFAULT_CODEX_SANDBOX)
            or DEFAULT_CODEX_SANDBOX
        ),
        # Guardrail for the fully-unsandboxed mode; must be opted into per call
        # AND permitted here.
        "allow_danger_full_access": bool(
            _cfg_path(cfg, "mcp_orchestration", "codex", "allow_danger_full_access", default=False)
        ),
        "timeout_seconds": int(
            _cfg_path(cfg, "mcp_orchestration", "codex", "timeout_seconds", default=1800) or 1800
        ),
    }


def _resolve_dir(path: str) -> Optional[Path]:
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return None
    return resolved if resolved.is_dir() else None


def _codex_dir_allowed(cwd: Path) -> Tuple[bool, str, List[str]]:
    cfg = _codex_config()
    allowed_raw = [str(p) for p in cfg["allowed_dirs"]]
    resolved_allowed: List[str] = []
    for entry in allowed_raw:
        rp = _resolve_dir(entry)
        if rp is not None:
            resolved_allowed.append(str(rp))
    if cfg["allow_any_dir"]:
        return True, "allow_any_dir", resolved_allowed
    for base in resolved_allowed:
        try:
            cwd.relative_to(base)
            return True, "configured_allowed_dir", resolved_allowed
        except ValueError:
            continue
    return False, "dir_not_allowed", resolved_allowed


def _codex_model_allowed(model: str) -> Tuple[bool, List[str]]:
    """Codex models are only constrained when an allowlist is configured."""
    cfg = _codex_config()
    allowed = [str(m) for m in cfg["allowed_models"]]
    if not model or not allowed or model in allowed:
        return True, allowed
    return False, allowed


def _normalize_repo_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    value = value.removesuffix(".git")
    value = re.sub(r"^git@github\.com:", "github.com/", value)
    value = re.sub(r"^https?://", "", value)
    value = value.strip("/")
    return value.lower()


def _git(args: List[str], cwd: Optional[Path] = None, timeout: int = 15) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd or Path.cwd()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _local_git_remotes() -> List[str]:
    code, out, _ = _git(["remote", "-v"])
    if code != 0 or not out:
        return []
    remotes: List[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.append(parts[1])
    return sorted({_normalize_repo_url(url) for url in remotes if url})


def _repo_allowed(repo_url: str) -> Tuple[bool, str, List[str]]:
    if not repo_url:
        return True, "no_repo_task", []
    requested = _normalize_repo_url(repo_url)
    cfg = _orchestration_config()
    configured = [_normalize_repo_url(url) for url in cfg["allowed_repos"]]
    if "*" in cfg["allowed_repos"] or requested in configured:
        return True, "configured_allowlist", configured
    local = _local_git_remotes() if cfg["allow_local_git_remotes"] else []
    if requested in local:
        return True, "local_git_remote", local
    return False, "repo_not_allowed", configured + local


def _model_allowed(model: str, allow_expensive_model: bool) -> Tuple[bool, List[str]]:
    cfg = _orchestration_config()
    allowed = [str(m) for m in cfg["allowed_models"]]
    if allow_expensive_model or model in allowed:
        return True, allowed
    return False, allowed


def _local_git_preflight(repo_url: str, starting_ref: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"checked": False, "ok": True, "warnings": []}
    if not repo_url or _normalize_repo_url(repo_url) not in _local_git_remotes():
        return result
    cfg = _orchestration_config()
    result["checked"] = True

    code, dirty, _ = _git(["status", "--porcelain"])
    if code == 0 and dirty:
        result["ok"] = False
        result["warnings"].append("Local repo has uncommitted changes; Cursor Cloud Agent will not see them.")

    code, current_branch, _ = _git(["branch", "--show-current"])
    if code == 0 and starting_ref and current_branch == starting_ref and cfg["reject_unpushed_current_ref"]:
        code, upstream, _ = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if code == 0 and upstream:
            code, counts, _ = _git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
            if code == 0 and counts:
                parts = counts.split()
                ahead = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                if ahead:
                    result["ok"] = False
                    result["warnings"].append(
                        f"Local branch {current_branch} is {ahead} commit(s) ahead of {upstream}; push before delegating."
                    )
    return result


def _extract_branches(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    git = run.get("git") if isinstance(run, dict) else None
    branches = git.get("branches") if isinstance(git, dict) else None
    if isinstance(branches, list):
        return [b for b in branches if isinstance(b, dict)]
    return []


def _extract_summary(run: Dict[str, Any]) -> str:
    for key in ("result", "text", "summary"):
        value = run.get(key) if isinstance(run, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_changed_files_from_text(text: str) -> List[str]:
    if not text:
        return []
    files: List[str] = []
    in_changed_section = False
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower().rstrip(":")
        if lower in {"changed files", "files changed", "changed_files"}:
            in_changed_section = True
            continue
        if in_changed_section and re.match(r"^[A-Z][A-Za-z ]+:$", line):
            break
        if in_changed_section or "changed" in lower:
            match = re.search(r"[`\-\*\s]*([\w./-]+\.[A-Za-z0-9_+-]+)", line)
            if match:
                files.append(match.group(1))
    return sorted(set(files))


def _changed_files_from_git(repo_url: str, starting_ref: str, branches: List[Dict[str, Any]]) -> Tuple[List[str], str]:
    requested = _normalize_repo_url(repo_url)
    if not requested:
        return [], ""
    if requested not in _local_git_remotes():
        return [], "local repo remote does not match task repo"
    if not starting_ref:
        return [], "starting_ref unavailable"
    changed: List[str] = []
    errors: List[str] = []
    for branch_entry in branches:
        branch = str(branch_entry.get("branch") or "").strip()
        branch_repo = _normalize_repo_url(str(branch_entry.get("repoUrl") or repo_url))
        if not branch or branch_repo != requested:
            continue
        code, _, err = _git(["fetch", "--quiet", "origin", branch], timeout=60)
        if code != 0:
            errors.append(err or f"git fetch origin {branch} failed")
            continue
        code, out, err = _git(["diff", "--name-only", f"{starting_ref}...FETCH_HEAD"], timeout=30)
        if code != 0:
            errors.append(err or f"git diff {starting_ref}...FETCH_HEAD failed")
            continue
        changed.extend([line.strip() for line in out.splitlines() if line.strip()])
    return sorted(set(changed)), "; ".join(errors)


def _build_result(run: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    branches = _extract_branches(run)
    summary = _extract_summary(run)
    changed_files = []
    raw_changed = run.get("changedFiles") or run.get("changed_files")
    if isinstance(raw_changed, list):
        changed_files.extend([str(x) for x in raw_changed])
    changed_files.extend(_extract_changed_files_from_text(summary))
    git_changed, git_error = _changed_files_from_git(task.get("repo_url", ""), task.get("starting_ref", ""), branches)
    changed_files.extend(git_changed)
    return {
        "cursor_run": run,
        "summary": summary,
        "git_branches": branches,
        "changed_files": sorted(set(changed_files)),
        "changed_files_error": git_error,
    }


def _stream_cursor_events(task: Dict[str, Any]) -> Dict[str, Any]:
    meta = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
    last_event_id = meta.get("cursor_last_event_id")
    try:
        from tools.cursor_agent_tool import call_cursor_agent
    except Exception as e:
        return {"error": True, "reason": f"cursor tool unavailable: {e}"}
    args = {
        "action": "stream_run",
        "agent_id": task.get("agent_id", ""),
        "run_id": task.get("run_id", ""),
        "last_event_id": last_event_id or "",
        "max_events": 50,
        "timeout_seconds": 10,
    }
    out = call_cursor_agent(args)
    if out.get("error"):
        return out
    raw_events = out.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_name = str(ev.get("event") or "cursor_event")
        data = ev.get("data")
        message = ""
        if isinstance(data, dict):
            message = str(data.get("text") or data.get("status") or data.get("message") or "")
        elif isinstance(data, str):
            message = data[:500]
        _insert_event(task["task_id"], event_name, message, ev)
    if out.get("last_event_id"):
        meta["cursor_last_event_id"] = out.get("last_event_id")
        task["result"] = meta
        _upsert_task(task)
    return out


def _refresh_task(task: Dict[str, Any]) -> Dict[str, Any]:
    if task.get("status") in TERMINAL_STATUSES:
        return task
    if task.get("backend") == "codex":
        return _refresh_codex_task(task)
    return _refresh_cursor_task(task)


def _refresh_cursor_task(task: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = task.get("agent_id") or ""
    run_id = task.get("run_id") or ""
    if not agent_id or not run_id:
        return task

    _stream_cursor_events(task)
    try:
        from tools.cursor_agent_tool import call_cursor_agent
    except Exception as e:
        task["status"] = "ERROR"
        task["error"] = f"cursor tool unavailable: {e}"
        _upsert_task(task)
        return task

    run = call_cursor_agent({"action": "get_run", "agent_id": agent_id, "run_id": run_id})
    if run.get("error"):
        task["error"] = str(run.get("reason") or run.get("payload") or run)
        task["updated_at"] = _now()
        _upsert_task(task)
        return task

    status = str(run.get("status") or task.get("status") or "UNKNOWN").upper()
    old_status = task.get("status")
    task["status"] = status
    task["updated_at"] = _now()
    task["result"] = _build_result(run, task)
    task["error"] = "" if status != "ERROR" else _extract_summary(run)
    _upsert_task(task)
    if status != old_status:
        _insert_event(task["task_id"], "status", status, {"old_status": old_status, "new_status": status})
    return task


def cursor_delegate(
    prompt: str,
    repo_url: Optional[str] = None,
    starting_ref: Optional[str] = None,
    model: Optional[str] = None,
    mode: str = "agent",
    auto_create_pr: bool = False,
    wait_for_completion: bool = False,
    allow_expensive_model: bool = False,
    work_on_current_branch: bool = False,
) -> str:
    """Delegate a bounded code task to Cursor Cloud Agent through Hermes.

    Safe defaults: composer-2.5, no PR creation, async return. For GitHub repo
    tasks, the repo must be allowlisted in config or match the local git remote.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return json.dumps({"error": "prompt is required"}, indent=2)
    model = (model or DEFAULT_ALLOWED_MODELS[0]).strip()
    repo_url = (repo_url or "").strip()
    starting_ref = (starting_ref or "").strip()

    ok, allowed_models = _model_allowed(model, allow_expensive_model)
    if not ok:
        return json.dumps({
            "error": "model_not_allowed",
            "model": model,
            "allowed_models": allowed_models,
            "hint": "Use an allowed cheap model or pass allow_expensive_model=true explicitly.",
        }, indent=2)

    repo_ok, repo_reason, allowed_repos = _repo_allowed(repo_url)
    if not repo_ok:
        return json.dumps({
            "error": "repo_not_allowed",
            "repo_url": repo_url,
            "allowed_repos": allowed_repos,
            "hint": "Add this GitHub repo to mcp_orchestration.cursor.allowed_repos or run Claude Code from that repo.",
        }, indent=2)

    preflight = _local_git_preflight(repo_url, starting_ref)
    if not preflight.get("ok", True):
        return json.dumps({
            "error": "local_git_not_ready",
            "preflight": preflight,
            "hint": "Commit/push the exact ref before delegating so Cursor sees the same code.",
        }, indent=2)

    task_id = "htask_" + uuid.uuid4().hex[:16]
    augmented_prompt = (
        prompt
        + "\n\nHermes delegation contract: when finished, include an exact Changed files list "
          "and the pushed branch/PR information in your final response. Do not create a PR unless requested."
    )
    args: Dict[str, Any] = {
        "action": "create",
        "prompt": augmented_prompt,
        "model": model,
        "mode": mode,
        "auto_create_pr": bool(auto_create_pr),
        "skip_reviewer_request": True,
        "work_on_current_branch": bool(work_on_current_branch),
        "wait_for_completion": bool(wait_for_completion),
    }
    if repo_url:
        args["repo_url"] = repo_url
    if starting_ref:
        args["starting_ref"] = starting_ref

    try:
        from tools.cursor_agent_tool import call_cursor_agent
    except Exception as e:
        return json.dumps({"error": f"cursor tool unavailable: {e}"}, indent=2)

    created = call_cursor_agent(args)
    now = _now()
    raw_agent = created.get("agent")
    raw_run = created.get("run")
    agent: Dict[str, Any] = raw_agent if isinstance(raw_agent, dict) else {}
    run: Dict[str, Any] = raw_run if isinstance(raw_run, dict) else {}
    status = str(run.get("status") or ("ERROR" if created.get("error") else "CREATED")).upper()
    task = {
        "task_id": task_id,
        "backend": "cursor",
        "status": status,
        "created_at": now,
        "updated_at": now,
        "prompt": prompt,
        "repo_url": repo_url,
        "starting_ref": starting_ref,
        "model": model,
        "agent_id": str(agent.get("id") or run.get("agentId") or ""),
        "run_id": str(run.get("id") or ""),
        "result": {"create_response": created, "repo_allow_reason": repo_reason, "preflight": preflight},
        "error": str(created.get("reason") or created.get("payload") or "") if created.get("error") else "",
    }
    _upsert_task(task)
    _insert_event(task_id, "created", f"Cursor task created with status {status}", task["result"])
    return json.dumps(_public_task_payload(task), indent=2)


def _public_task_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    result = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
    return {
        "task_id": task.get("task_id"),
        "backend": task.get("backend", "cursor"),
        "status": task.get("status"),
        "repo_url": task.get("repo_url", ""),
        "starting_ref": task.get("starting_ref", ""),
        "cwd": result.get("cwd", ""),
        "model": task.get("model", ""),
        "agent_id": task.get("agent_id", ""),
        "run_id": task.get("run_id", ""),
        "summary": result.get("summary", ""),
        "git_branches": result.get("git_branches", []),
        "changed_files": result.get("changed_files", []),
        "changed_files_error": result.get("changed_files_error", ""),
        "error": task.get("error", ""),
        "poll_after_seconds": 15 if task.get("status") not in TERMINAL_STATUSES else 0,
    }


def task_status(task_id: str, wait_seconds: int = 0) -> str:
    """Return task status, optionally long-polling before returning.

    ``wait_seconds`` lets MCP clients avoid token-burning manual 15-second poll
    loops. It caps at 120 seconds.
    """
    row = _task_row(task_id)
    if not row:
        return json.dumps({"error": f"task not found: {task_id}"}, indent=2)
    task = _row_to_task(row)
    wait_seconds = max(0, min(int(wait_seconds or 0), 120))
    deadline = time.time() + wait_seconds
    initial_status = task.get("status")
    while True:
        task = _refresh_task(task)
        if task.get("status") in TERMINAL_STATUSES:
            break
        if wait_seconds <= 0:
            break
        if task.get("status") != initial_status:
            break
        if time.time() >= deadline:
            break
        time.sleep(min(5, max(1, deadline - time.time())))
    return json.dumps(_public_task_payload(task), indent=2)


def task_events_poll(task_id: str, after_cursor: int = 0, limit: int = 20) -> str:
    """Poll persisted Hermes/Cursor task progress events."""
    row = _task_row(task_id)
    if not row:
        return json.dumps({"error": f"task not found: {task_id}"}, indent=2)
    task = _row_to_task(row)
    _refresh_task(task)
    after_cursor = max(0, int(after_cursor or 0))
    limit = max(1, min(int(limit or 20), 200))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
            (task_id, after_cursor, limit),
        ).fetchall()
    events = []
    next_cursor = after_cursor
    for row in rows:
        d = dict(row)
        next_cursor = max(next_cursor, int(d["id"]))
        events.append({
            "cursor": d["id"],
            "timestamp": d["timestamp"],
            "type": d["type"],
            "message": d.get("message") or "",
            "payload": _json_loads(d.get("payload_json"), {}),
        })
    return json.dumps({"task_id": task_id, "events": events, "next_cursor": next_cursor}, indent=2)


def task_cancel(task_id: str) -> str:
    """Cancel an active orchestration task (Cursor or Codex)."""
    row = _task_row(task_id)
    if not row:
        return json.dumps({"error": f"task not found: {task_id}"}, indent=2)
    task = _row_to_task(row)
    if task.get("status") in TERMINAL_STATUSES:
        return json.dumps(_public_task_payload(task), indent=2)
    if task.get("backend") == "codex":
        return _cancel_codex_task(task)
    try:
        from tools.cursor_agent_tool import call_cursor_agent
    except Exception as e:
        return json.dumps({"error": f"cursor tool unavailable: {e}"}, indent=2)
    cancel = call_cursor_agent({
        "action": "cancel_run",
        "agent_id": task.get("agent_id", ""),
        "run_id": task.get("run_id", ""),
    })
    if cancel.get("error"):
        task["error"] = str(cancel.get("reason") or cancel.get("payload") or cancel)
    else:
        task["status"] = "CANCELLED"
    task["updated_at"] = _now()
    meta = task.get("result", {}) if isinstance(task.get("result"), dict) else {}
    meta["cancel_response"] = cancel
    task["result"] = meta
    _upsert_task(task)
    _insert_event(task_id, "cancelled", "Task cancellation requested", cancel)
    return json.dumps(_public_task_payload(task), indent=2)


# ---------------------------------------------------------------------------
# Codex (local CLI) backend
# ---------------------------------------------------------------------------


def _codex_run_dir(task_id: str) -> Path:
    path = _get_hermes_home() / "mcp_codex" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _git_dirty_paths(cwd: Path) -> List[str]:
    code, out, _ = _git(["status", "--porcelain"], cwd=cwd)
    if code != 0 or not out:
        return []
    paths: List[str] = []
    for line in out.splitlines():
        entry = line[3:] if len(line) > 3 else line.strip()
        if " -> " in entry:  # renames: "old -> new"
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            paths.append(entry)
    return paths


def _codex_changed_files(cwd: Path, pre_head: str) -> Tuple[List[str], str]:
    if _git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)[0] != 0:
        return [], "cwd is not a git repository"
    changed = set(_git_dirty_paths(cwd))
    errors: List[str] = []
    if pre_head:
        code, head, _ = _git(["rev-parse", "HEAD"], cwd=cwd)
        if code == 0 and head and head != pre_head:
            code, out, err = _git(["diff", "--name-only", f"{pre_head}..HEAD"], cwd=cwd)
            if code == 0:
                changed.update(line.strip() for line in out.splitlines() if line.strip())
            elif err:
                errors.append(err)
    return sorted(changed), "; ".join(errors)


def _codex_meta(task: Dict[str, Any]) -> Dict[str, Any]:
    meta = task.get("result")
    return meta if isinstance(meta, dict) else {}


def _ingest_codex_events(task: Dict[str, Any]) -> None:
    meta = _codex_meta(task)
    log_file = meta.get("log_file")
    if not log_file or not Path(log_file).exists():
        return
    ingested = int(meta.get("jsonl_lines_ingested", 0) or 0)
    try:
        lines = Path(log_file).read_text(errors="replace").splitlines()
    except Exception:
        return
    new_lines = lines[ingested:]
    if not new_lines:
        return
    for raw in new_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            _insert_event(task["task_id"], "codex_log", raw[:500], {"raw": raw[:2000]})
            continue
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or ev.get("event") or "codex_event")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        message = ""
        for carrier in (
            ev.get("text"),
            ev.get("message"),
            ev.get("delta"),
            item.get("text"),
            item.get("message"),
            item.get("command"),
        ):
            if isinstance(carrier, str) and carrier.strip():
                message = carrier.strip()[:500]
                break
        _insert_event(task["task_id"], etype, message, ev)
    meta["jsonl_lines_ingested"] = ingested + len(new_lines)
    task["result"] = meta
    _upsert_task(task)


def _read_last_message(meta: Dict[str, Any]) -> str:
    path = meta.get("last_message_file")
    if path and Path(path).exists():
        try:
            return Path(path).read_text(errors="replace").strip()
        except Exception:
            return ""
    return ""


def _read_exit_code(meta: Dict[str, Any]) -> Optional[int]:
    path = meta.get("exit_file")
    if not path or not Path(path).exists():
        return None
    try:
        raw = Path(path).read_text().strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except Exception:
        return None


def _log_tail(meta: Dict[str, Any], limit: int = 2000) -> str:
    path = meta.get("log_file")
    if not path or not Path(path).exists():
        return ""
    try:
        return Path(path).read_text(errors="replace")[-limit:].strip()
    except Exception:
        return ""


def _finalize_codex_task(task: Dict[str, Any], status: str, error: str = "") -> Dict[str, Any]:
    meta = _codex_meta(task)
    cwd = meta.get("cwd", "")
    changed, changed_err = ([], "")
    if cwd:
        changed, changed_err = _codex_changed_files(Path(cwd), meta.get("pre_head", ""))
    summary = _read_last_message(meta)
    meta.update({
        "summary": summary,
        "changed_files": changed,
        "changed_files_error": changed_err,
        "git_branches": [],
    })
    old_status = task.get("status")
    task["result"] = meta
    task["status"] = status
    task["updated_at"] = _now()
    task["error"] = error
    _upsert_task(task)
    if status != old_status:
        _insert_event(task["task_id"], "status", status, {"old_status": old_status, "new_status": status})
    return task


def _refresh_codex_task(task: Dict[str, Any]) -> Dict[str, Any]:
    meta = _codex_meta(task)
    pid = int(meta.get("pid", 0) or 0)
    _ingest_codex_events(task)
    meta = _codex_meta(task)

    exit_code = _read_exit_code(meta)
    if exit_code is not None:
        if exit_code == 0:
            return _finalize_codex_task(task, "FINISHED")
        return _finalize_codex_task(task, "ERROR", f"codex exited with code {exit_code}: {_log_tail(meta)}")

    if _pid_alive(pid):
        cfg = _codex_config()
        started = float(meta.get("started_epoch", 0) or 0)
        if started and cfg["timeout_seconds"] > 0 and (time.time() - started) > cfg["timeout_seconds"]:
            _kill_codex(meta)
            return _finalize_codex_task(
                task, "ERROR", f"codex timed out after {cfg['timeout_seconds']}s"
            )
        task["updated_at"] = _now()
        _upsert_task(task)
        return task

    # Process gone but no exit code recorded → abnormal termination.
    return _finalize_codex_task(task, "ERROR", f"codex process ended without an exit status: {_log_tail(meta)}")


def _kill_codex(meta: Dict[str, Any]) -> None:
    pid = int(meta.get("pid", 0) or 0)
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _cancel_codex_task(task: Dict[str, Any]) -> str:
    meta = _codex_meta(task)
    _ingest_codex_events(task)
    _kill_codex(meta)
    task = _finalize_codex_task(task, "CANCELLED", "cancelled by request")
    _insert_event(task["task_id"], "cancelled", "Codex task cancellation requested", {})
    return json.dumps(_public_task_payload(task), indent=2)


def codex_delegate(
    prompt: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    sandbox: Optional[str] = None,
    dangerously_bypass: bool = False,
    wait_for_completion: bool = False,
    wait_seconds: int = 0,
) -> str:
    """Delegate a bounded code task to the local Codex CLI (``codex exec``).

    Unlike ``cursor_delegate`` (remote, needs a GitHub push), Codex runs on a
    local working directory, so it fits unpushed/local-only changes. The task
    is launched as a detached ``codex exec --json`` subprocess and tracked in
    the shared task store; poll it with ``task_status`` / ``task_events_poll``
    and stop it with ``task_cancel``.

    Args:
        prompt: Instructions for Codex.
        cwd: Working directory Codex operates in (default: server's cwd).
        model: Optional model override (constrained only if an allowlist is set).
        sandbox: read-only | workspace-write | danger-full-access
                 (default workspace-write).
        dangerously_bypass: Skip Codex approvals/sandbox entirely. Requires
            ``mcp_orchestration.codex.allow_danger_full_access: true``.
        wait_for_completion: Block up to ``wait_seconds`` (cap 120) before
            returning, so quick tasks come back finished in one call.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return json.dumps({"error": "prompt is required"}, indent=2)

    cfg = _codex_config()
    codex_bin = cfg["bin"]
    target = _resolve_dir(cwd) if cwd else _resolve_dir(str(Path.cwd()))
    if target is None:
        return json.dumps({
            "error": "cwd_not_found",
            "cwd": cwd or str(Path.cwd()),
            "hint": "Pass an existing directory for cwd.",
        }, indent=2)

    dir_ok, dir_reason, allowed_dirs = _codex_dir_allowed(target)
    if not dir_ok:
        return json.dumps({
            "error": "dir_not_allowed",
            "cwd": str(target),
            "allowed_dirs": allowed_dirs,
            "hint": "Add this path to mcp_orchestration.codex.allowed_dirs or set allow_any_dir=true.",
        }, indent=2)

    model = (model or cfg["default_model"] or "").strip()
    model_ok, allowed_models = _codex_model_allowed(model)
    if not model_ok:
        return json.dumps({
            "error": "model_not_allowed",
            "model": model,
            "allowed_models": allowed_models,
        }, indent=2)

    sandbox = (sandbox or cfg["default_sandbox"]).strip()
    if sandbox not in ALLOWED_CODEX_SANDBOXES:
        return json.dumps({
            "error": "invalid_sandbox",
            "sandbox": sandbox,
            "allowed": sorted(ALLOWED_CODEX_SANDBOXES),
        }, indent=2)
    if (sandbox == "danger-full-access" or dangerously_bypass) and not cfg["allow_danger_full_access"]:
        return json.dumps({
            "error": "danger_mode_not_allowed",
            "hint": "Set mcp_orchestration.codex.allow_danger_full_access: true to permit unsandboxed Codex runs.",
        }, indent=2)

    if not _which(codex_bin):
        return json.dumps({
            "error": "codex_not_found",
            "bin": codex_bin,
            "hint": "Install the Codex CLI or set mcp_orchestration.codex.bin to its path.",
        }, indent=2)

    task_id = "htask_" + uuid.uuid4().hex[:16]
    run_dir = _codex_run_dir(task_id)
    log_file = run_dir / "stdout.jsonl"
    exit_file = run_dir / "exit_code"
    last_message_file = run_dir / "last_message.txt"

    pre_head = _git(["rev-parse", "HEAD"], cwd=target)[1] or ""

    augmented_prompt = (
        prompt
        + "\n\nHermes delegation contract: when finished, summarize the exact files you "
          "changed. Work only within the current directory."
    )

    cmd: List[str] = [
        codex_bin, "exec", "--json", "--skip-git-repo-check",
        "-C", str(target),
        "-s", sandbox,
        "-o", str(last_message_file),
        "-c", 'approval_policy="never"',
    ]
    if model:
        cmd += ["-m", model]
    if dangerously_bypass or sandbox == "danger-full-access":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")

    inner = " ".join(shlex.quote(c) for c in cmd)
    shell_cmd = f"{inner}; echo $? > {shlex.quote(str(exit_file))}"

    now = _now()
    error = ""
    status = "RUNNING"
    pid = 0
    try:
        log_fh = open(log_file, "wb")
        proc = subprocess.Popen(
            ["sh", "-c", shell_cmd],
            cwd=str(target),
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if proc.stdin:
            proc.stdin.write(augmented_prompt.encode("utf-8"))
            proc.stdin.close()
        pid = proc.pid
    except Exception as e:
        status = "ERROR"
        error = f"failed to launch codex: {type(e).__name__}: {e}"

    task = {
        "task_id": task_id,
        "backend": "codex",
        "status": status,
        "created_at": now,
        "updated_at": now,
        "prompt": prompt,
        "repo_url": "",
        "starting_ref": pre_head,
        "model": model,
        "agent_id": "",
        "run_id": str(pid),
        "result": {
            "cwd": str(target),
            "pid": pid,
            "pre_head": pre_head,
            "sandbox": sandbox,
            "log_file": str(log_file),
            "exit_file": str(exit_file),
            "last_message_file": str(last_message_file),
            "jsonl_lines_ingested": 0,
            "started_epoch": time.time(),
            "dir_allow_reason": dir_reason,
        },
        "error": error,
    }
    _upsert_task(task)
    _insert_event(task_id, "created", f"Codex task launched (pid {pid}, sandbox {sandbox})", {"cwd": str(target)})

    if status != "ERROR" and wait_for_completion:
        return task_status(task_id, wait_seconds=wait_seconds or 120)
    return json.dumps(_public_task_payload(task), indent=2)


def _which(binary: str) -> Optional[str]:
    if os.path.sep in binary:
        return binary if os.path.exists(binary) and os.access(binary, os.X_OK) else None
    from shutil import which as _shutil_which

    return _shutil_which(binary)


def register_orchestration_tools(mcp: Any, mode: str = "cursor") -> None:
    """Register opt-in orchestration tools on a FastMCP server.

    ``mode`` selects which backends to expose:
      * ``cursor`` — cursor_delegate + shared task tools
      * ``codex``  — codex_delegate + shared task tools
      * ``all``    — both delegates + shared task tools
    """
    if mode not in {"cursor", "codex", "all"}:
        return

    if mode in {"cursor", "all"}:
        mcp.tool()(cursor_delegate)
    if mode in {"codex", "all"}:
        mcp.tool()(codex_delegate)

    # Shared task-management surface (dispatches by backend).
    mcp.tool()(task_status)
    mcp.tool()(task_events_poll)
    mcp.tool()(task_cancel)
