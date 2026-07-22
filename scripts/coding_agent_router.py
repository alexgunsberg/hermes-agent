#!/usr/bin/env python3
"""Operator CLI for the Hermes coding-agent router.

The engine lives in :mod:`tools.code_routing` (packaged with Hermes and
exposed to the agent as the ``route_code_task`` tool); this script is the
operator/CI entry point.

Usage:
    python scripts/coding_agent_router.py --packet packet.json
    python scripts/coding_agent_router.py --packet packet.json --route grok-high

Packet JSON (production shape — target a real repo at an exact commit):
    {
      "name": "fix-retry-logic",
      "repo": "/path/to/checkout-or-url",
      "base_sha": "abc123...",
      "prompt": "Fix ... run the tests ...",
      "allowed_paths": ["gateway/", "tests/gateway/"],
      "verify": ["python -m pytest tests/gateway -q"],
      "task_class": "bounded-tooling",
      "risk": "medium",
      "expected_duration_s": 600
    }

Synthetic packets (benchmarks/tests) omit repo/base_sha and seed "files"
instead. See tools/code_routing.py for the full field reference.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import code_routing  # noqa: E402


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--packet", required=True, help="packet JSON file")
    ap.add_argument("--log", default=None,
                    help="append-only JSONL task record "
                         "(default: HERMES_HOME/router/routes.jsonl)")
    ap.add_argument("--route", default=None,
                    help="comma-separated profile names overriding the routing table")
    ap.add_argument("--stub-run", default=None,
                    help="register a `stub` profile: run command template ({prompt}/{sid})")
    ap.add_argument("--stub-repair", default=None,
                    help="repair command template for the stub profile")
    args = ap.parse_args(argv)

    packet = code_routing.Packet.from_dict(
        json.loads(Path(args.packet).read_text(encoding="utf-8")))
    profiles = code_routing.builtin_profiles()
    if args.stub_run:
        profiles["stub"] = code_routing.stub_profile(
            "stub", args.stub_run, args.stub_repair)

    route_override = None
    if args.route:
        names = [n.strip() for n in args.route.split(",") if n.strip()]
        unknown = [n for n in names if n not in profiles]
        if unknown:
            ap.error(f"unknown profiles: {unknown} (known: {sorted(profiles)})")
        route_override = [profiles[n] for n in names[:2]]

    log_path = Path(args.log) if args.log else code_routing.default_log_path()
    summary = code_routing.route_packet(packet, profiles, log_path,
                                        route_override=route_override)
    print(json.dumps(summary, indent=2))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
