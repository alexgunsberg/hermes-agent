"""Hermes tool surface for the coding-agent router (:mod:`tools.code_routing`).

Registers ``route_code_task``: delegate one bounded coding task to an external
agent CLI (Grok Build / Cursor) with deterministic acceptance, one
same-session repair, and one reroute — the general Hermes delegation route,
not a benchmark runner. The durable JSONL task record lives under
``HERMES_HOME/router/routes.jsonl`` unless overridden.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.registry import registry
from tools import code_routing


ROUTE_CODE_TASK_SCHEMA = {
    "name": "route_code_task",
    "description": (
        "Delegate a bounded coding task to an external coding-agent CLI "
        "(Grok Build or Cursor) selected by task class. The task runs in an "
        "isolated clone at an exact base commit (or a synthetic seeded repo), "
        "is verified with your acceptance commands outside the agent's "
        "control, gets one same-session repair on failure, and is rerouted "
        "once to the fallback harness before stopping. Returns the summary "
        "record; the full attempt history is appended to the router JSONL "
        "log. Requires at least one delegate CLI to be installed and "
        "authenticated — unavailable profiles are reported with reasons."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short packet name."},
            "prompt": {"type": "string",
                       "description": "The task objective for the delegate."},
            "verify": {"type": "array", "items": {"type": "string"},
                       "description": "Acceptance shell commands; all must exit 0."},
            "repo": {"type": "string",
                     "description": "Git repo to work on: local path or URL. "
                                    "Omit to seed a scratch repo from files."},
            "base_sha": {"type": "string",
                         "description": "Exact commit to build on (with repo)."},
            "allowed_paths": {"type": "array", "items": {"type": "string"},
                              "description": "Paths/globs the delegate may "
                                             "change; empty = unrestricted."},
            "files": {"type": "object",
                      "description": "Relative path -> contents overlaid onto "
                                     "the workspace (also used for synthetic "
                                     "packets)."},
            "protected_files": {"type": "array", "items": {"type": "string"},
                                "description": "Packet files restored before "
                                               "every verification so the "
                                               "delegate cannot tamper with "
                                               "acceptance."},
            "task_class": {"type": "string",
                           "enum": ["small-iteration", "ui-domain",
                                    "bounded-tooling", "test-repair",
                                    "high-risk", "default"],
                           "description": "Routing class (default: default)."},
            "risk": {"type": "string", "enum": ["low", "medium", "high"],
                     "description": "high requires a sandbox-enforcing "
                                    "profile (default: medium)."},
            "expected_duration_s": {"type": "integer",
                                    "description": "Per-attempt timeout "
                                                   "(default 600)."},
            "preferred_profile": {"type": "string",
                                  "description": "Override the routing "
                                                 "table's primary profile."},
            "log_path": {"type": "string",
                         "description": "JSONL record path (default: "
                                        "HERMES_HOME/router/routes.jsonl)."},
        },
        "required": ["name", "prompt", "verify"],
    },
}


def _route_code_task_handler(args: dict, **_kw) -> str:
    log_path = Path(args.pop("log_path", "") or code_routing.default_log_path())
    try:
        packet = code_routing.Packet.from_dict(args)
    except (ValueError, code_routing.PacketPathError) as exc:
        return json.dumps({"error": f"invalid packet: {exc}"})
    profiles = code_routing.builtin_profiles()
    summary = code_routing.route_packet(packet, profiles, log_path)
    summary["log_path"] = str(log_path)
    return json.dumps(summary, indent=2)


def check_code_routing_requirements() -> bool:
    """The route is exposed when at least one delegate CLI binary exists;
    auth/config problems are reported per-profile in the result instead of
    hiding the tool."""
    import shutil
    return any(shutil.which(b) for b in ("grok", "cursor-agent"))


registry.register(
    name="route_code_task",
    toolset="delegation",
    schema=ROUTE_CODE_TASK_SCHEMA,
    handler=_route_code_task_handler,
    check_fn=check_code_routing_requirements,
)
