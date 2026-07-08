"""No-LLM Kanban timer and ownership audit reports.

This module reads Kanban SQLite boards directly and writes timestamped JSON +
Markdown reports under ``<kanban-home>/reports`` (normally ``~/.hermes/reports``).
It is deliberately pure data collection/formatting: no model calls, no tools, no
board mutation.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


_TERMINAL_STATUSES = {"done", "archived"}
_BLOCK_EVENTS = {"blocked", "block_loop_detected"}
_HEARTBEAT_EVENTS = {"heartbeat", "claim_heartbeat", "activity", "worker_activity"}
_REVIEW_EVENTS = {"review", "review_required", "review_queued", "claimed"}


def _db_path_for_board(slug: str) -> Path:
    """Return a board DB path while intentionally ignoring HERMES_KANBAN_DB.

    Kanban workers receive HERMES_KANBAN_DB pinned to their own board.  A
    multi-board audit must still be able to read sibling boards, so it cannot
    call ``kanban_db_path(board=...)`` (that helper quite correctly gives the
    env override precedence for normal worker operations).
    """
    if slug == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban.db"
    return kb.board_dir(slug) / "kanban.db"


def _utc_stamp(now: int) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_utc(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_seconds_since(now: int, ts: Optional[int]) -> Optional[int]:
    if ts is None:
        return None
    return max(0, int(now) - int(ts))


def _duration(start: Optional[int], end: Optional[int]) -> Optional[int]:
    if start is None or end is None:
        return None
    return max(0, int(end) - int(start))


def _payload(row: Any) -> dict[str, Any]:
    raw = row["payload"] if hasattr(row, "keys") and "payload" in row.keys() else None
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _latest_event_ts(events: Iterable[Any], kinds: set[str]) -> Optional[int]:
    vals = [int(ev["created_at"]) for ev in events if ev["kind"] in kinds and ev["created_at"] is not None]
    return max(vals) if vals else None


def _latest_block_event(events: list[Any]) -> tuple[Optional[int], Optional[str]]:
    latest_ts: Optional[int] = None
    latest_reason: Optional[str] = None
    for ev in events:
        if ev["kind"] not in _BLOCK_EVENTS:
            continue
        ts = int(ev["created_at"] or 0)
        if latest_ts is None or ts >= latest_ts:
            latest_ts = ts
            reason = _payload(ev).get("reason")
            latest_reason = str(reason) if reason is not None else None
    return latest_ts, latest_reason


def _latest_comment_by_task(conn) -> dict[str, int]:
    return {
        row["task_id"]: int(row["created_at"])
        for row in conn.execute(
            "SELECT task_id, MAX(created_at) AS created_at "
            "FROM task_comments GROUP BY task_id"
        )
        if row["created_at"] is not None
    }


def _events_by_task(conn, task_ids: list[str]) -> dict[str, list[Any]]:
    out = {task_id: [] for task_id in task_ids}
    if not task_ids:
        return out
    placeholders = ",".join(["?"] * len(task_ids))
    for row in conn.execute(
        f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(task_ids),
    ):
        out.setdefault(row["task_id"], []).append(row)
    return out


def _runs_by_task(conn, task_ids: list[str]) -> dict[str, list[Any]]:
    out = {task_id: [] for task_id in task_ids}
    if not task_ids:
        return out
    placeholders = ",".join(["?"] * len(task_ids))
    for row in conn.execute(
        f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(task_ids),
    ):
        out.setdefault(row["task_id"], []).append(row)
    return out


def _max_run_heartbeat(runs: Iterable[Any]) -> Optional[int]:
    vals = [int(r["last_heartbeat_at"]) for r in runs if r["last_heartbeat_at"] is not None]
    return max(vals) if vals else None


def _summarize_seconds(values: Iterable[Optional[int]]) -> dict[str, Optional[float | int]]:
    clean = [int(v) for v in values if v is not None]
    if not clean:
        return {"count": 0, "min": None, "max": None, "avg": None}
    return {
        "count": len(clean),
        "min": min(clean),
        "max": max(clean),
        "avg": round(mean(clean), 2),
    }


def _review_required_age(
    *,
    task: Any,
    events: list[Any],
    blocked_at: Optional[int],
    blocked_reason: Optional[str],
) -> Optional[int]:
    status = task["status"]
    if status == "review":
        return _latest_event_ts(events, _REVIEW_EVENTS) or task["started_at"] or task["created_at"]
    if status == "blocked" and blocked_reason and "review-required" in blocked_reason.casefold():
        return blocked_at or task["created_at"]
    return None


def _task_report(
    *,
    task: Any,
    events: list[Any],
    runs: list[Any],
    latest_comment_at: Optional[int],
    diagnostics: list[kd.Diagnostic],
    now: int,
) -> dict[str, Any]:
    created_at = int(task["created_at"])
    started_at = int(task["started_at"]) if task["started_at"] is not None else None
    completed_at = int(task["completed_at"]) if task["completed_at"] is not None else None
    event_heartbeat_at = _latest_event_ts(events, _HEARTBEAT_EVENTS)
    run_heartbeat_at = _max_run_heartbeat(runs)
    task_heartbeat_at = int(task["last_heartbeat_at"]) if task["last_heartbeat_at"] is not None else None
    latest_heartbeat_at = max(
        [ts for ts in (event_heartbeat_at, run_heartbeat_at, task_heartbeat_at) if ts is not None],
        default=None,
    )
    blocked_at, blocked_reason = _latest_block_event(events)
    if task["status"] == "blocked" and blocked_at is None:
        blocked_at = started_at or created_at
    review_at = _review_required_age(
        task=task,
        events=events,
        blocked_at=blocked_at,
        blocked_reason=blocked_reason,
    )
    diag_payloads = [d.to_dict() for d in diagnostics]
    ownership = [d for d in diag_payloads if d.get("kind") == "missing_ownership_ledger"]
    handoff_gaps = [d for d in diag_payloads if d.get("kind") == "invalid_handoff_contract"]
    cursor_gaps = [d for d in diag_payloads if d.get("kind") == "unbounded_cursor_workflow"]
    return {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "assignee": task["assignee"],
        "created_at": created_at,
        "created_at_iso": _iso_utc(created_at),
        "started_at": started_at,
        "started_at_iso": _iso_utc(started_at),
        "completed_at": completed_at,
        "completed_at_iso": _iso_utc(completed_at),
        "created_to_started_seconds": _duration(created_at, started_at),
        "started_to_terminal_seconds": (
            _duration(started_at, completed_at)
            if completed_at is not None and task["status"] in _TERMINAL_STATUSES
            else None
        ),
        "latest_heartbeat_at": latest_heartbeat_at,
        "latest_heartbeat_at_iso": _iso_utc(latest_heartbeat_at),
        "seconds_since_heartbeat": _safe_seconds_since(now, latest_heartbeat_at),
        "latest_comment_at": latest_comment_at,
        "latest_comment_at_iso": _iso_utc(latest_comment_at),
        "seconds_since_comment": _safe_seconds_since(now, latest_comment_at),
        "blocked_at": blocked_at,
        "blocked_at_iso": _iso_utc(blocked_at),
        "blocked_age_seconds": _safe_seconds_since(now, blocked_at) if task["status"] == "blocked" else None,
        "blocked_reason": blocked_reason,
        "review_required_at": review_at,
        "review_required_at_iso": _iso_utc(review_at),
        "review_required_age_seconds": _safe_seconds_since(now, review_at),
        "goal_mode": bool(task["goal_mode"]),
        "diagnostics": diag_payloads,
        "ownership_missing": ownership[0].get("data", {}).get("missing", []) if ownership else [],
        "handoff_contract_violations": handoff_gaps[0].get("data", {}).get("violations", []) if handoff_gaps else [],
        "handoff_contract_missing_fields": handoff_gaps[0].get("data", {}).get("missing_fields", []) if handoff_gaps else [],
        "cursor_workflow_missing": cursor_gaps[0].get("data", {}).get("missing", []) if cursor_gaps else [],
        "non_goal_work_card": any(d.get("kind") == "non_goal_work_card" for d in diag_payloads),
    }


def build_board_report(slug: str, *, include_archived: bool = False, now: Optional[int] = None) -> dict[str, Any]:
    """Return a timer/audit report for one board without mutating it."""
    normed = kb._normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
        raise ValueError(f"board {normed!r} does not exist")
    now_ts = int(now if now is not None else datetime.now(tz=timezone.utc).timestamp())
    db_path = _db_path_for_board(normed)
    with kb.connect_closing(db_path=db_path) as conn:
        where = "" if include_archived else "WHERE status != 'archived'"
        tasks = list(conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at ASC, id ASC"))
        task_ids = [row["id"] for row in tasks]
        events_by = _events_by_task(conn, task_ids)
        runs_by = _runs_by_task(conn, task_ids)
        comments_by = _latest_comment_by_task(conn)
        diag_cfg = {"ownership_audit": True}
        task_reports: list[dict[str, Any]] = []
        for task in tasks:
            task_events = events_by.get(task["id"], [])
            task_runs = runs_by.get(task["id"], [])
            diagnostics = kd.compute_task_diagnostics(
                task,
                task_events,
                task_runs,
                now=now_ts,
                config=diag_cfg,
            )
            task_reports.append(
                _task_report(
                    task=task,
                    events=task_events,
                    runs=task_runs,
                    latest_comment_at=comments_by.get(task["id"]),
                    diagnostics=diagnostics,
                    now=now_ts,
                )
            )

    status_counts = Counter(t["status"] for t in task_reports)
    return {
        "slug": normed,
        "db_path": str(db_path),
        "generated_at": now_ts,
        "generated_at_iso": _iso_utc(now_ts),
        "task_count": len(task_reports),
        "status_counts": dict(sorted(status_counts.items())),
        "duration_summaries": {
            "created_to_started_seconds": _summarize_seconds(
                t["created_to_started_seconds"] for t in task_reports
            ),
            "started_to_terminal_seconds": _summarize_seconds(
                t["started_to_terminal_seconds"] for t in task_reports
            ),
            "seconds_since_heartbeat": _summarize_seconds(
                t["seconds_since_heartbeat"] for t in task_reports
            ),
            "seconds_since_comment": _summarize_seconds(
                t["seconds_since_comment"] for t in task_reports
            ),
            "blocked_age_seconds": _summarize_seconds(
                t["blocked_age_seconds"] for t in task_reports
            ),
            "review_required_age_seconds": _summarize_seconds(
                t["review_required_age_seconds"] for t in task_reports
            ),
        },
        "ownership_gaps": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "assignee": t["assignee"],
                "missing": t["ownership_missing"],
            }
            for t in task_reports
            if t["ownership_missing"]
        ],
        "non_goal_cards": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "assignee": t["assignee"],
            }
            for t in task_reports
            if t["non_goal_work_card"]
        ],
        "handoff_contract_gaps": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "assignee": t["assignee"],
                "violations": t["handoff_contract_violations"],
                "missing_fields": t["handoff_contract_missing_fields"],
            }
            for t in task_reports
            if t["handoff_contract_violations"] or t["handoff_contract_missing_fields"]
        ],
        "cursor_workflow_gaps": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "assignee": t["assignee"],
                "missing": t["cursor_workflow_missing"],
            }
            for t in task_reports
            if t["cursor_workflow_missing"]
        ],
        "blocked": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "assignee": t["assignee"],
                "blocked_age_seconds": t["blocked_age_seconds"],
                "reason": t["blocked_reason"],
            }
            for t in task_reports
            if t["blocked_age_seconds"] is not None
        ],
        "review_required": [
            {
                "task_id": t["id"],
                "title": t["title"],
                "assignee": t["assignee"],
                "review_required_age_seconds": t["review_required_age_seconds"],
                "reason": t["blocked_reason"],
            }
            for t in task_reports
            if t["review_required_age_seconds"] is not None
        ],
        "tasks": task_reports,
    }


def resolve_boards(
    *,
    boards: Optional[Iterable[str]] = None,
    all_boards: bool = False,
) -> list[str]:
    if boards and all_boards:
        raise ValueError("pass either --boards or --all-boards, not both")
    if all_boards:
        return [b["slug"] for b in kb.list_boards(include_archived=False)]
    if boards:
        out: list[str] = []
        for raw in boards:
            normed = kb._normalize_board_slug(raw)
            if not normed:
                continue
            if normed not in out:
                out.append(normed)
        if out:
            return out
    return [kb.get_current_board()]


def build_report(
    *,
    boards: Optional[Iterable[str]] = None,
    all_boards: bool = False,
    include_archived: bool = False,
    now: Optional[int] = None,
) -> dict[str, Any]:
    now_ts = int(now if now is not None else datetime.now(tz=timezone.utc).timestamp())
    slugs = resolve_boards(boards=boards, all_boards=all_boards)
    board_reports = [
        build_board_report(slug, include_archived=include_archived, now=now_ts)
        for slug in slugs
    ]
    status_counts: Counter[str] = Counter()
    for board in board_reports:
        status_counts.update(board["status_counts"])
    summary = {
        "boards_count": len(board_reports),
        "task_count": sum(int(b["task_count"]) for b in board_reports),
        "status_counts": dict(sorted(status_counts.items())),
        "ownership_gap_count": sum(len(b["ownership_gaps"]) for b in board_reports),
        "handoff_contract_gap_count": sum(len(b["handoff_contract_gaps"]) for b in board_reports),
        "non_goal_card_count": sum(len(b["non_goal_cards"]) for b in board_reports),
        "cursor_workflow_gap_count": sum(len(b["cursor_workflow_gaps"]) for b in board_reports),
        "blocked_count": sum(len(b["blocked"]) for b in board_reports),
        "review_required_count": sum(len(b["review_required"]) for b in board_reports),
    }
    return {
        "kind": "kanban_timer_audit_report",
        "generated_at": now_ts,
        "generated_at_iso": _iso_utc(now_ts),
        "boards": board_reports,
        "summary": summary,
    }


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "-"
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Kanban timer/audit report",
        "",
        f"Generated: {report['generated_at_iso']}",
        "",
        "## Summary",
        "",
        f"- Boards: {report['summary']['boards_count']}",
        f"- Tasks: {report['summary']['task_count']}",
        f"- Ownership gaps: {report['summary']['ownership_gap_count']}",
        f"- Handoff contract gaps: {report['summary']['handoff_contract_gap_count']}",
        f"- Non-goal work cards: {report['summary']['non_goal_card_count']}",
        f"- Cursor workflow gaps: {report['summary']['cursor_workflow_gap_count']}",
        f"- Blocked cards: {report['summary']['blocked_count']}",
        f"- Review-required cards: {report['summary']['review_required_count']}",
        "",
    ]
    for board in report["boards"]:
        lines.extend([
            f"## Board: `{board['slug']}`",
            "",
            f"DB: `{board['db_path']}`",
            "",
            "### Status counts",
            "",
        ])
        if board["status_counts"]:
            for status, count in board["status_counts"].items():
                lines.append(f"- {status}: {count}")
        else:
            lines.append("- (no tasks)")
        lines.extend(["", "### Duration summaries", ""])
        lines.append("| Metric | Count | Min | Avg | Max |")
        lines.append("|---|---:|---:|---:|---:|")
        for metric, values in board["duration_summaries"].items():
            lines.append(
                "| "
                + metric
                + f" | {values['count']} | {_fmt_seconds(values['min'])} | {_fmt_seconds(values['avg'])} | {_fmt_seconds(values['max'])} |"
            )
        lines.extend(["", "### Ownership gaps", ""])
        if board["ownership_gaps"]:
            for item in board["ownership_gaps"][:50]:
                missing = ", ".join(item["missing"])
                lines.append(f"- `{item['task_id']}` {item['status']} @{item['assignee'] or '-'} — {item['title']} (missing: {missing})")
        else:
            lines.append("- None")
        lines.extend(["", "### Handoff contract gaps", ""])
        if board["handoff_contract_gaps"]:
            for item in board["handoff_contract_gaps"][:50]:
                labels = list(item.get("violations") or []) + [
                    f"missing_{field}" for field in (item.get("missing_fields") or [])
                ]
                lines.append(f"- `{item['task_id']}` {item['status']} @{item['assignee'] or '-'} — {item['title']} ({', '.join(labels)})")
        else:
            lines.append("- None")
        lines.extend(["", "### Non-goal work cards", ""])
        if board["non_goal_cards"]:
            for item in board["non_goal_cards"][:50]:
                lines.append(f"- `{item['task_id']}` {item['status']} @{item['assignee'] or '-'} — {item['title']}")
        else:
            lines.append("- None")
        lines.extend(["", "### Cursor workflow gaps", ""])
        if board["cursor_workflow_gaps"]:
            for item in board["cursor_workflow_gaps"][:50]:
                missing = ", ".join(item["missing"])
                lines.append(f"- `{item['task_id']}` {item['status']} @{item['assignee'] or '-'} — {item['title']} (missing: {missing})")
        else:
            lines.append("- None")
        lines.extend(["", "### Blocked / review-required", ""])
        if board["blocked"] or board["review_required"]:
            for item in board["blocked"][:50]:
                lines.append(
                    f"- blocked `{item['task_id']}` age={_fmt_seconds(item['blocked_age_seconds'])} "
                    f"@{item['assignee'] or '-'} — {item['title']}"
                )
            for item in board["review_required"][:50]:
                lines.append(
                    f"- review-required `{item['task_id']}` age={_fmt_seconds(item['review_required_age_seconds'])} "
                    f"@{item['assignee'] or '-'} — {item['title']}"
                )
        else:
            lines.append("- None")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    *,
    boards: Optional[Iterable[str]] = None,
    all_boards: bool = False,
    include_archived: bool = False,
    output_dir: Optional[str | Path] = None,
    now: Optional[int] = None,
) -> tuple[dict[str, Any], Path, Path]:
    report = build_report(
        boards=boards,
        all_boards=all_boards,
        include_archived=include_archived,
        now=now,
    )
    out_dir = Path(output_dir).expanduser() if output_dir else kb.kanban_home() / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp(int(report["generated_at"]))
    board_part = "multi" if len(report["boards"]) != 1 else report["boards"][0]["slug"]
    json_path = out_dir / f"kanban-timer-audit-{board_part}-{stamp}.json"
    md_path = out_dir / f"kanban-timer-audit-{board_part}-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return report, json_path, md_path
