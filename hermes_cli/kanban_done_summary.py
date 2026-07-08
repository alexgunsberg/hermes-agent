"""Low-overhead Kanban done-project summary scanner.

The scanner is deliberately deterministic and read-mostly: it walks Kanban
boards, finds newly-terminal boards/project-root cards, formats Alex's canonical
semantic closeout summary, and records a tiny state fingerprint so cron or a
watchdog can run it repeatedly without duplicate spam.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb


_TERMINAL_STATUSES = {"done", "archived"}
_STATE_VERSION = 1
_MAX_FOLLOWUPS = 3


@dataclass
class DoneProjectSummary:
    """One newly-terminal board/project summary."""

    key: str
    board: str
    scope: str
    title: str
    fingerprint: str
    project: str
    measured_return: list[str]
    delivered: list[str]
    achieved: str
    followups: list[dict[str, str]]
    improvement_ideas: list[str]
    state: str

    @property
    def text(self) -> str:
        lines = [
            f"✅ {self.title} done",
            f"- Project: {self.project}",
        ]
        if self.measured_return:
            lines.append("- Measured return:")
            lines.extend(f"  • {item}" for item in self.measured_return)
        if self.delivered:
            lines.append("- Delivered:")
            lines.extend(f"  • {item}" for item in self.delivered)
        lines.extend(_format_followup_lines(self.followups))
        if self.improvement_ideas:
            lines.append("- Improvement ideas captured:")
            lines.extend(f"  • {item}" for item in self.improvement_ideas)
        lines.append(f"- State: {self.state}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "board": self.board,
            "scope": self.scope,
            "title": self.title,
            "fingerprint": self.fingerprint,
            "project": self.project,
            "measured_return": self.measured_return,
            "delivered": self.delivered,
            "gains": [*self.measured_return, *self.delivered],
            "achieved": self.achieved,
            "followups": self.followups,
            "improvement_ideas": self.improvement_ideas,
            "state": self.state,
            "text": self.text,
        }


def _utc_stamp(now: int) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _db_path_for_board(slug: str) -> Path:
    """Return a board DB path while intentionally ignoring worker DB pins.

    Kanban workers run with HERMES_KANBAN_DB set to the task's board.  A
    multi-board scanner must still read sibling boards, so it cannot call the
    normal board resolver that gives that env var precedence.
    """
    if slug == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban.db"
    return kb.board_dir(slug) / "kanban.db"


def default_state_path() -> Path:
    return kb.kanban_home() / "kanban" / "done_project_summaries.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": _STATE_VERSION, "emitted": {}}
    if not isinstance(raw, dict):
        return {"version": _STATE_VERSION, "emitted": {}}
    emitted = raw.get("emitted")
    if not isinstance(emitted, dict):
        emitted = {}
    return {"version": _STATE_VERSION, "emitted": emitted}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _payload(row: Any) -> dict[str, Any]:
    raw = row["payload"] if hasattr(row, "keys") and "payload" in row.keys() else None
    return _json_obj(raw)


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _short_sentence(text: Optional[str], *, fallback: str) -> str:
    if not text:
        return fallback
    for raw_line in str(text).splitlines():
        line = raw_line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        # Remove common handoff prefixes without trying to rewrite substance.
        for prefix in ("summary:", "result:"):
            if line.casefold().startswith(prefix):
                line = line[len(prefix):].strip()
        if len(line) > 220:
            line = line[:217].rstrip() + "..."
        return line or fallback
    return fallback


def _clean_project_name(text: str) -> str:
    """Return a human-facing project name without ownership/control prefixes."""
    cleaned = " ".join(str(text or "").split()).strip()
    for prefix in (
        "PROJECT OWNER:",
        "KANBAN OWNER:",
        "TASK OWNER:",
        "PROJECT:",
    ):
        if cleaned.casefold().startswith(prefix.casefold()):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned or "Kanban project"


def _looks_operational_dump_line(line: str) -> bool:
    low = line.casefold()
    if line.startswith("|") or re.fullmatch(r"[-|: ]+", line):
        return True
    operational_prefixes = (
        "task id:",
        "task_id:",
        "status:",
        "assignee:",
        "run id:",
        "run_id:",
        "changed_files:",
        "tests_run:",
        "metadata:",
    )
    if low.startswith(operational_prefixes):
        return True
    return False


def _looks_followup_pointer(line: str) -> bool:
    low = line.casefold()
    return ("follow-up" in low or "followup" in low) and (
        "created" in low or "card" in low or "t_" in low or "kanban follow-up" in low
    )


def _summary_lines(text: Optional[str]) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for raw_line in str(text).replace("\r", "\n").splitlines():
        line = raw_line.strip().lstrip("-•* ").strip()
        if not line or _looks_operational_dump_line(line):
            continue
        for prefix in ("summary:", "result:"):
            if line.casefold().startswith(prefix):
                line = line[len(prefix):].strip()
        line = re.sub(r"\bt_[0-9a-f]+\b", "Kanban follow-up", line)
        if line:
            out.append(line)
    return out


def _semantic_items(text: Optional[str], *, fallback: str, limit: int = 3) -> list[str]:
    """Extract a few concise deterministic bullets without an LLM rewrite."""
    items: list[str] = []
    for line in _summary_lines(text):
        if _looks_followup_pointer(line):
            continue
        if len(line) > 220:
            line = line[:217].rstrip() + "..."
        if line and line not in items:
            items.append(line)
        if len(items) >= limit:
            break
    return items or [fallback]


_MEASURED_RETURN_MARKERS = (
    "→",
    "->",
    "before",
    "after",
    "saved",
    "faster",
    "slower",
    "reduced",
    "reduction",
    "latency",
    "cost",
    "eur",
    "$",
    "%",
    "×",
)


def _looks_measured_return(line: str) -> bool:
    low = line.casefold()
    if not any(ch.isdigit() for ch in low):
        return False
    if any(marker in low for marker in _MEASURED_RETURN_MARKERS):
        return True
    return bool(
        re.search(
            r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|second|seconds|min|mins|minute|minutes|h|hr|hour|hours|x)\b",
            low,
        )
    )


def _split_semantic_outcomes(text: Optional[str], *, fallback: str) -> tuple[list[str], list[str]]:
    measured: list[str] = []
    delivered: list[str] = []
    for item in _semantic_items(text, fallback=fallback, limit=4):
        if _looks_measured_return(item):
            measured.append(item)
        else:
            delivered.append(item)
    if not measured and not delivered:
        delivered.append(fallback)
    return measured[:3], delivered[:3]


def _followup_status_heading(statuses: list[str]) -> str:
    clean = [s.strip() for s in statuses if s and s.strip()]
    if not clean:
        return "none"
    unique = sorted(set(clean))
    if len(unique) != 1:
        return "mixed state"
    status = unique[0]
    if status == "done":
        return "all done"
    if status in {"todo", "ready", "running", "scheduled"}:
        return "waiting"
    return status.replace("_", "-")


def _format_followup_lines(items: list[dict[str, str]]) -> list[str]:
    if not items:
        return ["- Follow-ups created — none"]
    statuses = [str(item.get("status") or "") for item in items[:_MAX_FOLLOWUPS]]
    heading = _followup_status_heading(statuses)
    lines = [f"- Follow-ups created — {heading}:"]
    mixed = heading == "mixed state"
    for item in items[:_MAX_FOLLOWUPS]:
        title = str(item.get("title") or "untitled follow-up").strip()
        if mixed and item.get("status"):
            title = f"{title} — {item['status']}"
        lines.append(f"  • {title}")
    if len(items) > _MAX_FOLLOWUPS:
        lines.append(f"  • +{len(items) - _MAX_FOLLOWUPS} more follow-up(s) in Kanban")
    return lines


def _row_has(row: Any, field: str) -> bool:
    return hasattr(row, "keys") and field in row.keys()


def _task_text(task: Any) -> str:
    return "\n".join(str(task[field] or "") for field in ("title", "body", "result") if _row_has(task, field))


def _ids_from_text(text: str) -> list[str]:
    found: list[str] = []
    for token in str(text or "").replace("`", " ").replace(",", " ").split():
        clean = token.strip(".;:()[]{}<>\"'")
        if clean.startswith("t_") and clean not in found:
            found.append(clean)
    return found


def _fingerprint(parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fetch_board_rows(conn) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
    tasks = list(conn.execute("SELECT * FROM tasks ORDER BY created_at ASC, id ASC"))
    links = list(conn.execute("SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"))
    events = list(conn.execute("SELECT * FROM task_events ORDER BY id ASC"))
    runs = list(conn.execute("SELECT * FROM task_runs ORDER BY id ASC"))
    comments = list(conn.execute("SELECT * FROM task_comments ORDER BY id ASC"))
    return tasks, links, events, runs, comments


def _latest_run_summaries(runs: Iterable[Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        tid = run["task_id"]
        prev = latest.get(tid)
        if prev is not None and int(prev["id"] or 0) >= int(run["id"] or 0):
            continue
        latest[tid] = {
            "id": run["id"],
            "summary": run["summary"],
            "metadata": _json_obj(run["metadata"]),
            "ended_at": run["ended_at"],
            "outcome": run["outcome"],
        }
    return latest


def _descendants(root_id: str, children_by: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(children_by.get(root_id, [])))
    while stack:
        tid = stack.pop()
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
        stack.extend(reversed(children_by.get(tid, [])))
    return out


def _scope_fingerprint(
    *,
    board: str,
    scope: str,
    task_ids: Iterable[str],
    tasks_by_id: dict[str, Any],
    events_by_task: dict[str, list[Any]],
    latest_runs: dict[str, dict[str, Any]],
) -> str:
    task_part = []
    max_event_id = 0
    for tid in sorted(set(task_ids)):
        task = tasks_by_id[tid]
        for ev in events_by_task.get(tid, []):
            max_event_id = max(max_event_id, int(ev["id"] or 0))
        run = latest_runs.get(tid, {})
        task_part.append(
            {
                "id": tid,
                "title": task["title"],
                "status": task["status"],
                "completed_at": task["completed_at"],
                "result": task["result"],
                "run_id": run.get("id"),
                "run_summary": run.get("summary"),
            }
        )
    return _fingerprint(
        {"board": board, "scope": scope, "tasks": task_part, "max_event_id": max_event_id}
    )


def _explicit_followup_ids(
    *,
    scope_ids: set[str],
    tasks_by_id: dict[str, Any],
    events_by_task: dict[str, list[Any]],
    latest_runs: dict[str, dict[str, Any]],
    comments_by_task: dict[str, list[Any]],
) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            candidates = [value, *_ids_from_text(value)]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            return
        for raw in candidates:
            tid = str(raw).strip()
            if tid.startswith("t_") and tid not in found:
                found.append(tid)

    for tid in scope_ids:
        for ev in events_by_task.get(tid, []):
            payload = _payload(ev)
            if ev["kind"] == "completed":
                add(payload.get("followups") or payload.get("follow_up_cards"))
            if ev["kind"] == "created_followup":
                add(payload.get("task_id") or payload.get("task_ids"))
        run = latest_runs.get(tid, {})
        metadata = run.get("metadata") or {}
        add(metadata.get("followups"))
        add(metadata.get("follow_up_cards"))
        add(metadata.get("created_followups"))
        searchable = str(run.get("summary") or "")
        if "follow-up" in searchable.casefold() or "followup" in searchable.casefold():
            add(searchable)
        for comment in comments_by_task.get(tid, []):
            body = str(comment["body"] or "")
            if "follow-up" in body.casefold() or "followup" in body.casefold():
                add(body)
    return found


def _coerce_text_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in _summary_lines(value)]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _explicit_improvement_ideas(
    *,
    scope_ids: set[str],
    latest_runs: dict[str, dict[str, Any]],
    comments_by_task: dict[str, list[Any]],
) -> list[str]:
    ideas: list[str] = []

    def add(value: Any) -> None:
        for item in _coerce_text_items(value):
            if len(item) > 220:
                item = item[:217].rstrip() + "..."
            if item and item not in ideas:
                ideas.append(item)

    for tid in scope_ids:
        metadata = latest_runs.get(tid, {}).get("metadata") or {}
        add(metadata.get("improvement_ideas"))
        add(metadata.get("improvements"))
        add(metadata.get("ideas"))
        add(metadata.get("backlog_ideas"))
        for comment in comments_by_task.get(tid, []):
            body = str(comment["body"] or "")
            marker = "improvement ideas:"
            lower = body.casefold()
            if marker in lower:
                start = lower.index(marker) + len(marker)
                add(body[start:].strip())
    return ideas[:3]


def _lookup_task_any_board(task_id: str) -> Optional[dict[str, str]]:
    for board in [kb.DEFAULT_BOARD, *[b["slug"] for b in kb.list_boards(include_archived=False)]]:
        try:
            db_path = _db_path_for_board(board)
            with kb.connect_closing(db_path=db_path) as conn:
                row = conn.execute(
                    "SELECT id, title, status FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
        except Exception:
            continue
        if row is not None:
            title = str(row["title"] or "untitled follow-up")
            if board != kb.DEFAULT_BOARD:
                title = f"{title} [{board}]"
            return {"id": str(row["id"]), "title": title, "status": str(row["status"])}
    return None


def _followup_cards(
    *,
    explicit_ids: Iterable[str],
    tasks_by_id: dict[str, Any],
    scope_ids: set[str],
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    for tid in explicit_ids:
        if tid in seen:
            continue
        seen.add(tid)
        # Only explicit follow-up IDs are listed.  If the follow-up already lives
        # inside the terminal scope, keep it with its current status (often
        # ``done``) so the user sees that the promised next card was closed.
        task = tasks_by_id.get(tid)
        if task is not None:
            cards.append({"id": tid, "title": str(task["title"]), "status": str(task["status"])})
            continue
        external = _lookup_task_any_board(tid)
        if external is not None:
            cards.append(external)
    return cards


def _terminal_state_line(status_counts: Counter[str]) -> str:
    total = sum(status_counts.values())
    if total == 0:
        return "no scoped tasks"
    if status_counts.get("archived") and status_counts.get("done"):
        return f"all {total} scoped task(s) terminal"
    if status_counts.get("archived"):
        return f"all {total} scoped task(s) archived"
    return f"all {total} scoped task(s) done"


def _board_candidate(
    *,
    board: str,
    meta: dict[str, Any],
    tasks: list[Any],
    tasks_by_id: dict[str, Any],
    parents_by: dict[str, list[str]],
    events_by_task: dict[str, list[Any]],
    comments_by_task: dict[str, list[Any]],
    latest_runs: dict[str, dict[str, Any]],
) -> Optional[DoneProjectSummary]:
    if not tasks:
        return None
    active = [t for t in tasks if t["status"] not in _TERMINAL_STATUSES]
    if active:
        return None
    scope_ids = {t["id"] for t in tasks}
    status_counts: Counter[str] = Counter(t["status"] for t in tasks)
    done_tasks = [t for t in tasks if t["status"] == "done"]
    if not done_tasks:
        return None
    latest_done = max(done_tasks, key=lambda t: (t["completed_at"] or 0, t["created_at"] or 0, t["id"]), default=None)
    root_done = [t for t in done_tasks if not parents_by.get(t["id"])]
    projectish = [
        t for t in root_done
        if "project" in f"{t['title']}\n{t['body'] or ''}".casefold()
        or "kanban owner" in f"{t['title']}\n{t['body'] or ''}".casefold()
    ]
    achieved_task = max(projectish or root_done or ([latest_done] if latest_done else []), key=lambda t: (t["completed_at"] or 0, t["created_at"] or 0, t["id"])) if (projectish or root_done or latest_done) else None
    fallback = f"Completed {len(done_tasks)} Kanban task(s) on {board}."
    achieved_text = None
    if achieved_task is not None:
        achieved_text = latest_runs.get(achieved_task["id"], {}).get("summary") or achieved_task["result"]
    explicit = _explicit_followup_ids(
        scope_ids=scope_ids,
        tasks_by_id=tasks_by_id,
        events_by_task=events_by_task,
        latest_runs=latest_runs,
        comments_by_task=comments_by_task,
    )
    improvement_ideas = _explicit_improvement_ideas(
        scope_ids=scope_ids,
        latest_runs=latest_runs,
        comments_by_task=comments_by_task,
    )
    display = meta.get("name") or board
    achieved = _short_sentence(achieved_text, fallback=fallback)
    measured_return, delivered = _split_semantic_outcomes(achieved_text, fallback=fallback)
    return DoneProjectSummary(
        key=f"board:{board}",
        board=board,
        scope="board",
        title=str(display),
        fingerprint=_scope_fingerprint(
            board=board,
            scope="board",
            task_ids=scope_ids,
            tasks_by_id=tasks_by_id,
            events_by_task=events_by_task,
            latest_runs=latest_runs,
        ),
        project=_clean_project_name(str(display)),
        measured_return=measured_return,
        delivered=delivered,
        achieved=achieved,
        followups=_followup_cards(explicit_ids=explicit, tasks_by_id=tasks_by_id, scope_ids=scope_ids),
        improvement_ideas=improvement_ideas,
        state=_terminal_state_line(status_counts),
    )


def _project_candidates(
    *,
    board: str,
    tasks: list[Any],
    tasks_by_id: dict[str, Any],
    children_by: dict[str, list[str]],
    parents_by: dict[str, list[str]],
    events_by_task: dict[str, list[Any]],
    comments_by_task: dict[str, list[Any]],
    latest_runs: dict[str, dict[str, Any]],
) -> list[DoneProjectSummary]:
    out: list[DoneProjectSummary] = []
    for task in tasks:
        if task["status"] != "done" or not children_by.get(task["id"]):
            continue
        text = f"{task['title']}\n{task['body'] or ''}".casefold()
        if parents_by.get(task["id"]) and "project owner" not in text and "kanban owner" not in text:
            continue
        descendant_ids = _descendants(task["id"], children_by)
        if not descendant_ids:
            continue
        scope_ids = {task["id"], *descendant_ids}
        if any(tasks_by_id[tid]["status"] not in _TERMINAL_STATUSES for tid in scope_ids):
            continue
        status_counts: Counter[str] = Counter(tasks_by_id[tid]["status"] for tid in scope_ids)
        explicit = _explicit_followup_ids(
            scope_ids=scope_ids,
            tasks_by_id=tasks_by_id,
            events_by_task=events_by_task,
            latest_runs=latest_runs,
            comments_by_task=comments_by_task,
        )
        improvement_ideas = _explicit_improvement_ideas(
            scope_ids=scope_ids,
            latest_runs=latest_runs,
            comments_by_task=comments_by_task,
        )
        achieved_text = latest_runs.get(task["id"], {}).get("summary") or task["result"]
        project = _clean_project_name(str(task["title"]))
        fallback = f"Completed project: {task['title']}."
        achieved = _short_sentence(achieved_text, fallback=fallback)
        measured_return, delivered = _split_semantic_outcomes(achieved_text, fallback=fallback)
        out.append(
            DoneProjectSummary(
                key=f"project:{board}:{task['id']}",
                board=board,
                scope="project",
                title=f"{board}/{project}",
                fingerprint=_scope_fingerprint(
                    board=board,
                    scope=f"project:{task['id']}",
                    task_ids=scope_ids,
                    tasks_by_id=tasks_by_id,
                    events_by_task=events_by_task,
                    latest_runs=latest_runs,
                ),
                project=project,
                measured_return=measured_return,
                delivered=delivered,
                achieved=achieved,
                followups=_followup_cards(
                    explicit_ids=explicit,
                    tasks_by_id=tasks_by_id,
                    scope_ids=scope_ids,
                ),
                improvement_ideas=improvement_ideas,
                state=_terminal_state_line(status_counts),
            )
        )
    return out


def scan_board(board: str, *, scope: str = "all") -> list[DoneProjectSummary]:
    normed = kb._normalize_board_slug(board)
    if not normed:
        raise ValueError("board slug is required")
    if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
        raise ValueError(f"board {normed!r} does not exist")
    db_path = _db_path_for_board(normed)
    with kb.connect_closing(db_path=db_path) as conn:
        tasks, links, events, runs, comments = _fetch_board_rows(conn)
    tasks_by_id = {t["id"]: t for t in tasks}
    children_by: dict[str, list[str]] = defaultdict(list)
    parents_by: dict[str, list[str]] = defaultdict(list)
    for link in links:
        children_by[link["parent_id"]].append(link["child_id"])
        parents_by[link["child_id"]].append(link["parent_id"])
    events_by_task: dict[str, list[Any]] = defaultdict(list)
    for ev in events:
        events_by_task[ev["task_id"]].append(ev)
    comments_by_task: dict[str, list[Any]] = defaultdict(list)
    for comment in comments:
        comments_by_task[comment["task_id"]].append(comment)
    latest_runs = _latest_run_summaries(runs)

    candidates: list[DoneProjectSummary] = []
    board_summary = None
    if scope in {"all", "board"}:
        board_summary = _board_candidate(
            board=normed,
            meta=kb.read_board_metadata(normed),
            tasks=tasks,
            tasks_by_id=tasks_by_id,
            parents_by=parents_by,
            events_by_task=events_by_task,
            comments_by_task=comments_by_task,
            latest_runs=latest_runs,
        )
        if board_summary is not None:
            candidates.append(board_summary)
    # When the whole board is terminal, the board-level closeout is the
    # shortest meaningful summary.  Do not also emit every terminal root/card
    # project in the default scanner path; operators can request
    # ``--scope project`` when they need per-project backfill.
    if scope == "project" or (scope == "all" and board_summary is None):
        candidates.extend(
            _project_candidates(
                board=normed,
                tasks=tasks,
                tasks_by_id=tasks_by_id,
                children_by=children_by,
                parents_by=parents_by,
                events_by_task=events_by_task,
                comments_by_task=comments_by_task,
                latest_runs=latest_runs,
            )
        )
    return candidates


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
            if normed and normed not in out:
                out.append(normed)
        if out:
            return out
    return [kb.get_current_board()]


def due_summaries(
    *,
    boards: Optional[Iterable[str]] = None,
    all_boards: bool = False,
    scope: str = "all",
    state_path: Optional[str | Path] = None,
    update_state: bool = True,
    now: Optional[int] = None,
) -> tuple[list[DoneProjectSummary], Path]:
    if scope not in {"all", "board", "project"}:
        raise ValueError("scope must be one of: all, board, project")
    path = Path(state_path).expanduser() if state_path else default_state_path()
    state = _load_state(path)
    emitted = state.setdefault("emitted", {})
    summaries: list[DoneProjectSummary] = []
    for board in resolve_boards(boards=boards, all_boards=all_boards):
        for summary in scan_board(board, scope=scope):
            if emitted.get(summary.key) == summary.fingerprint:
                continue
            summaries.append(summary)
            if update_state:
                emitted[summary.key] = summary.fingerprint
    if update_state and summaries:
        state["updated_at"] = int(now if now is not None else _now_ts())
        _write_state(path, state)
    return summaries, path


def render_text(summaries: Iterable[DoneProjectSummary]) -> str:
    blocks = [s.text for s in summaries]
    return "\n\n".join(blocks).rstrip()


def write_report(
    summaries: Iterable[DoneProjectSummary],
    *,
    output_dir: Optional[str | Path] = None,
    now: Optional[int] = None,
) -> tuple[Optional[Path], Optional[Path]]:
    summaries = list(summaries)
    if not summaries:
        return None, None
    now_ts = int(now if now is not None else _now_ts())
    out_dir = Path(output_dir).expanduser() if output_dir else kb.kanban_home() / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp(now_ts)
    json_path = out_dir / f"kanban-done-project-summaries-{stamp}.json"
    md_path = out_dir / f"kanban-done-project-summaries-{stamp}.md"
    payload = {
        "kind": "kanban_done_project_summaries",
        "generated_at": now_ts,
        "generated_at_iso": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "summaries": [s.to_dict() for s in summaries],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_text(summaries) + "\n", encoding="utf-8")
    return json_path, md_path
