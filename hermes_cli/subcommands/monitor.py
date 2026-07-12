"""``hermes monitor`` subcommand parser.

Handler injected to avoid importing ``main`` (same pattern as
``hermes_cli/subcommands/insights.py``).
"""

from __future__ import annotations

from typing import Callable


def build_monitor_parser(subparsers, *, cmd_monitor: Callable) -> None:
    """Attach the ``monitor`` subcommand to ``subparsers``."""
    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Show the performance monitor's catch-up report",
        description=(
            "Report model-request latency, context size, token usage, tool "
            "runtimes, and health gauge trends recorded by the built-in "
            "event-driven performance monitor"
        ),
    )
    monitor_parser.add_argument(
        "--days", type=int, default=7, help="Number of days to analyze (default: 7)"
    )
    monitor_parser.set_defaults(func=cmd_monitor)
