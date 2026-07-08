#!/usr/bin/env python3
"""Silent-by-default Hermes performance metrics watchdog.

Designed for no-agent cron use: it records local metrics and prints nothing when
there is no above-threshold regression.  Use --json or --print-report for manual
inspection.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli import performance_metrics as pm


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes local performance metrics watchdog")
    parser.add_argument("--store", default=None, help="SQLite metrics DB path (default: <HERMES_HOME>/performance_metrics/metrics.db)")
    parser.add_argument("--live-chat", action="store_true", help="Run the optional live no-tools model smoke timing")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-command timeout in seconds (default: 15)")
    parser.add_argument("--threshold-ratio", type=float, default=1.5, help="Regression ratio threshold (default: 1.5)")
    parser.add_argument("--min-delta-s", type=float, default=1.0, help="Minimum median slowdown in seconds (default: 1.0)")
    parser.add_argument("--create-kanban-tasks", action="store_true", help="Create deduped goal-mode Kanban cards for regressions")
    parser.add_argument("--dry-run", action="store_true", help="Do not create Kanban cards; report planned actions")
    parser.add_argument("--board", default=None, help="Kanban board for synthetic regression cards")
    parser.add_argument("--assignee", default="default", help="Assignee for synthetic regression cards")
    parser.add_argument("--json", action="store_true", help="Print full JSON report even when there is no regression")
    parser.add_argument("--print-report", action="store_true", help="Print a human report even when there is no regression")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store_path = Path(args.store).expanduser() if args.store else None
    report = pm.run_watchdog(
        store_path=store_path,
        live_chat=args.live_chat,
        timeout_s=args.timeout,
        threshold_ratio=args.threshold_ratio,
        min_delta_s=args.min_delta_s,
        create_tasks=args.create_kanban_tasks,
        dry_run=args.dry_run,
        board=args.board,
        assignee=args.assignee,
    )

    has_regression = bool(report.get("regressions"))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.print_report or has_regression:
        print(pm.render_text_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
