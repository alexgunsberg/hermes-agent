"""Cursor Cloud Agent delegation tool for Hermes.

This module exposes a single ``cursor_agent`` tool that lets Hermes create and
monitor Cursor Cloud Agent runs. Cursor's public API is agent/run oriented (not
OpenAI-compatible chat completions), so this is intentionally a delegation
surface rather than a model provider.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.registry import registry


API_BASE = "https://api.cursor.com"
TERMINAL_STATUSES = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}


class CursorAgentError(RuntimeError):
    """Raised for Cursor API/tool validation errors."""


def _get_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _load_env_file(path: Path) -> None:
    """Best-effort minimal .env loader; never prints values."""
    if not path.exists():
        return
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip().strip('"').strip("'")
            os.environ[key] = value
    except OSError:
        return


def _cursor_api_key() -> str:
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if key:
        return key
    # Hermes loads profile env in many paths, but direct tests/tool runs may not.
    # Fall back to the active profile's .env without leaking profile boundaries.
    _load_env_file(_get_hermes_home() / ".env")
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if key:
        return key
    raise CursorAgentError(
        "CURSOR_API_KEY is not configured. Put it in the active Hermes profile .env or the process environment."
    )


def check_cursor_agent_requirements() -> bool:
    try:
        return bool(_cursor_api_key())
    except Exception:
        return False


def _auth_headers() -> Dict[str, str]:
    # Cursor docs use Basic auth with username=API key and blank password (-u KEY:).
    token = base64.b64encode((_cursor_api_key() + ":").encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    accept: Optional[str] = None,
    timeout: int = 60,
) -> Any:
    url = API_BASE + path
    headers = {**_auth_headers()}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"body": raw}
        return {"error": True, "status": e.code, "reason": e.reason, "payload": payload}
    except urllib.error.URLError as e:
        return {"error": True, "reason": str(e)}


def _clean_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


def _build_repos(repo_url: str = "", starting_ref: str = "", pr_url: str = "") -> List[Dict[str, str]]:
    if not repo_url and not pr_url:
        return []
    if pr_url and not repo_url:
        raise CursorAgentError("repo_url is required when pr_url is provided.")
    repo = {"url": repo_url}
    if starting_ref:
        repo["startingRef"] = starting_ref
    if pr_url:
        repo["prUrl"] = pr_url
    return [repo]


def _model_obj(model: str = "", model_params: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    if not model:
        return None
    out: Dict[str, Any] = {"id": model}
    if model_params:
        out["params"] = model_params
    return out


def _create(args: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise CursorAgentError("prompt is required for action='create'.")
    body = _clean_dict(
        {
            "prompt": {"text": prompt},
            "model": _model_obj(args.get("model", ""), args.get("model_params")),
            "name": args.get("name"),
            "repos": _build_repos(args.get("repo_url", ""), args.get("starting_ref", ""), args.get("pr_url", "")),
            "workOnCurrentBranch": args.get("work_on_current_branch"),
            "autoCreatePR": args.get("auto_create_pr"),
            "skipReviewerRequest": args.get("skip_reviewer_request"),
            "mode": args.get("mode"),
            "env": args.get("env"),
            "envVars": args.get("env_vars"),
            "mcpServers": args.get("mcp_servers"),
            "customSubagents": args.get("custom_subagents"),
            "agentId": args.get("client_agent_id"),
        }
    )
    result = _request("POST", "/v1/agents", body)
    if args.get("wait_for_completion") and not result.get("error"):
        agent = result.get("agent") or {}
        run = result.get("run") or {}
        if agent.get("id") and run.get("id"):
            result["terminal_run"] = _wait_run(
                agent["id"],
                run["id"],
                int(args.get("timeout_seconds") or 600),
                int(args.get("poll_interval_seconds") or 10),
            )
    return result


def _followup(args: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = str(args.get("agent_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if not agent_id or not prompt:
        raise CursorAgentError("agent_id and prompt are required for action='followup'.")
    body = _clean_dict(
        {
            "prompt": {"text": prompt},
            "mcpServers": args.get("mcp_servers"),
            "mode": args.get("mode"),
        }
    )
    result = _request("POST", f"/v1/agents/{urllib.parse.quote(agent_id)}/runs", body)
    if args.get("wait_for_completion") and not result.get("error"):
        run = result.get("run") or {}
        if run.get("id"):
            result["terminal_run"] = _wait_run(
                agent_id,
                run["id"],
                int(args.get("timeout_seconds") or 600),
                int(args.get("poll_interval_seconds") or 10),
            )
    return result


def _get_run(agent_id: str, run_id: str) -> Dict[str, Any]:
    if not agent_id or not run_id:
        raise CursorAgentError("agent_id and run_id are required for action='get_run'.")
    return _request(
        "GET",
        f"/v1/agents/{urllib.parse.quote(agent_id)}/runs/{urllib.parse.quote(run_id)}",
    )


def _wait_run(agent_id: str, run_id: str, timeout_seconds: int, poll_interval_seconds: int) -> Dict[str, Any]:
    deadline = time.time() + max(1, timeout_seconds)
    poll = max(2, poll_interval_seconds)
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = _get_run(agent_id, run_id)
        status = str(last.get("status", "")).upper()
        if status in TERMINAL_STATUSES or last.get("error"):
            return last
        time.sleep(poll)
    return {"error": True, "reason": "timeout", "last_run": last}


def _parse_sse(lines: Iterable[str], max_events: int) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    event_name = "message"
    data_lines: List[str] = []
    last_id = None

    def flush() -> None:
        nonlocal event_name, data_lines, last_id
        if not data_lines and event_name == "message":
            return
        data_raw = "\n".join(data_lines)
        data: Any = data_raw
        if data_raw:
            try:
                data = json.loads(data_raw)
            except json.JSONDecodeError:
                data = data_raw
        events.append(_clean_dict({"event": event_name, "id": last_id, "data": data}))
        event_name = "message"
        data_lines = []

    for raw in lines:
        line = raw.rstrip("\n")
        if line == "":
            flush()
            if len(events) >= max_events:
                break
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            last_id = value
    flush()
    return {"events": events[:max_events], "last_event_id": last_id, "truncated": len(events) >= max_events}


def _stream_run(args: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = str(args.get("agent_id") or "").strip()
    run_id = str(args.get("run_id") or "").strip()
    if not agent_id or not run_id:
        raise CursorAgentError("agent_id and run_id are required for action='stream_run'.")
    max_events = max(1, min(int(args.get("max_events") or 50), 200))
    timeout = max(5, min(int(args.get("timeout_seconds") or 120), 600))
    path = f"/v1/agents/{urllib.parse.quote(agent_id)}/runs/{urllib.parse.quote(run_id)}/stream"
    url = API_BASE + path
    headers = {**_auth_headers(), "Accept": "text/event-stream"}
    if args.get("last_event_id"):
        headers["Last-Event-ID"] = str(args["last_event_id"])
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _parse_sse((b.decode("utf-8", errors="replace") for b in resp), max_events)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"body": raw}
        return {"error": True, "status": e.code, "reason": e.reason, "payload": payload}
    except urllib.error.URLError as e:
        return {"error": True, "reason": str(e)}


def _cancel_run(agent_id: str, run_id: str) -> Dict[str, Any]:
    if not agent_id or not run_id:
        raise CursorAgentError("agent_id and run_id are required for action='cancel_run'.")
    return _request(
        "POST",
        f"/v1/agents/{urllib.parse.quote(agent_id)}/runs/{urllib.parse.quote(run_id)}/cancel",
    )


def call_cursor_agent(args: Dict[str, Any]) -> Dict[str, Any]:
    """Call Cursor Cloud Agent API using the Hermes Cursor tool contract."""
    try:
        action = str(args.get("action") or "list_models")
        if action == "whoami":
            return _request("GET", "/v1/me")
        if action == "list_models":
            return _request("GET", "/v1/models")
        if action == "list_agents":
            limit = args.get("limit")
            qs = f"?limit={int(limit)}" if limit else ""
            return _request("GET", "/v1/agents" + qs)
        if action == "create":
            return _create(args)
        if action == "followup":
            return _followup(args)
        if action == "get_run":
            return _get_run(str(args.get("agent_id") or ""), str(args.get("run_id") or ""))
        if action == "wait_run":
            return _wait_run(
                str(args.get("agent_id") or ""),
                str(args.get("run_id") or ""),
                int(args.get("timeout_seconds") or 600),
                int(args.get("poll_interval_seconds") or 10),
            )
        if action == "stream_run":
            return _stream_run(args)
        if action == "cancel_run":
            return _cancel_run(
                str(args.get("agent_id") or ""),
                str(args.get("run_id") or ""),
            )
        return {"error": True, "reason": f"Unknown action: {action}"}
    except CursorAgentError as e:
        return {"error": True, "reason": str(e)}
    except Exception as e:  # defensive: tools should return structured failure
        return {"error": True, "reason": f"{type(e).__name__}: {e}"}


def _handle_cursor_agent(args: Dict[str, Any], **_: Any) -> Dict[str, Any]:
    return call_cursor_agent(args)


CURSOR_AGENT_SCHEMA = {
    "name": "cursor_agent",
    "description": (
        "Delegate bounded repo/code tasks to Cursor Cloud Agents via Cursor's API; "
        "list Cursor models, create agents/runs, follow up, poll, wait, or stream results. "
        "Requires CURSOR_API_KEY. This is not a chat-completions model provider."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "whoami",
                    "list_models",
                    "list_agents",
                    "create",
                    "followup",
                    "get_run",
                    "wait_run",
                    "stream_run",
                    "cancel_run",
                ],
                "default": "list_models",
            },
            "prompt": {"type": "string", "description": "Task prompt for create/followup."},
            "model": {"type": "string", "description": "Cursor model ID from list_models, e.g. composer-2.5."},
            "model_params": {"type": "array", "items": {"type": "object"}},
            "name": {"type": "string", "description": "Optional display name for a new agent."},
            "repo_url": {"type": "string", "description": "GitHub repo URL for a cloud agent workspace."},
            "starting_ref": {"type": "string", "description": "Branch or commit SHA to start from."},
            "pr_url": {"type": "string", "description": "Existing GitHub PR URL to work on."},
            "agent_id": {"type": "string", "description": "Existing Cursor agent ID (bc-...)."},
            "client_agent_id": {"type": "string", "description": "Optional idempotency ID for create (bc-uuid)."},
            "run_id": {"type": "string", "description": "Cursor run ID (run-...)."},
            "mode": {"type": "string", "enum": ["agent", "plan"]},
            "auto_create_pr": {"type": "boolean"},
            "skip_reviewer_request": {"type": "boolean"},
            "work_on_current_branch": {"type": "boolean"},
            "env": {"type": "object", "description": "Cursor env selection object, e.g. {type: 'cloud', name: '...'}"},
            "env_vars": {"type": "object", "description": "Session env vars for the cloud agent; do not include secrets unless intended."},
            "mcp_servers": {"type": "array", "items": {"type": "object"}},
            "custom_subagents": {"type": "array", "items": {"type": "object"}},
            "wait_for_completion": {"type": "boolean", "default": False},
            "timeout_seconds": {"type": "integer", "default": 600},
            "poll_interval_seconds": {"type": "integer", "default": 10},
            "max_events": {"type": "integer", "default": 50},
            "last_event_id": {"type": "string"},
            "limit": {"type": "integer", "description": "Optional list limit for list_agents."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="cursor_agent",
    toolset="cursor",
    schema=CURSOR_AGENT_SCHEMA,
    handler=_handle_cursor_agent,
    check_fn=check_cursor_agent_requirements,
    requires_env=["CURSOR_API_KEY"],
    emoji="🖱️",
    max_result_size_chars=120_000,
)
