"""Tests for no-LLM Kanban timer/audit reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_audit as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_done_summary as kds


REQUIRED_BOARDS = [
    "hermes-process-mastery",
    "kanban-ownership-cursor-governance",
    "hermes-potential-flywheel",
]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return home


def _seed_board(slug: str):
    suffix = hashlib.sha1(slug.encode("utf-8")).hexdigest()
    done_id = f"t_{suffix[:8]}"
    blocked_id = f"t_{suffix[8:16]}"
    if slug != kb.DEFAULT_BOARD:
        kb.create_board(slug)
    with kb.connect_closing(board=slug) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, created_by, created_at,
                started_at, completed_at, workspace_kind, goal_mode,
                last_heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scratch', ?, ?)
            """,
            (
                done_id,
                "Implement timer audit",
                "Ownership:\n- Task owner: default\n- Project owner: t_parent\n- Kanban owner: t_board\n- Stage owner: default\n- Escalation target: Alex",
                "default",
                "done",
                "user",
                1000,
                1010,
                1070,
                1,
                1040,
            ),
        )
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, created_by, created_at,
                started_at, completed_at, workspace_kind, goal_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scratch', ?)
            """,
            (
                blocked_id,
                "review worker diff",
                "missing owner ledger",
                "reviewer",
                "blocked",
                "user",
                1100,
                1110,
                None,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
            (blocked_id, json.dumps({"reason": "review-required: inspect diff"}), 1120),
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'default', 'latest note', ?)",
            (blocked_id, 1130),
        )
        conn.commit()


def _seed_terminal_project(
    slug: str,
    *,
    external_followup: bool = True,
    summary: str = "Shipped deterministic closeout summaries with duplicate suppression.",
) -> str:
    if slug != kb.DEFAULT_BOARD:
        kb.create_board(slug)
    root_id = "t_aaaa1111"
    child_id = "t_bbbb2222"
    follow_id = "t_cccc3333"
    with kb.connect_closing(board=slug) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, created_by, created_at,
                started_at, completed_at, workspace_kind, goal_mode
            ) VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?, 'scratch', 1)
            """,
            (
                root_id,
                "PROJECT OWNER: ship done summaries",
                "Ownership:\n- Task owner: default\n- Project owner: t_owner\n- Kanban owner: t_board",
                "default",
                "default",
                1000,
                1010,
                1100,
            ),
        )
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, created_by, created_at,
                started_at, completed_at, workspace_kind, goal_mode
            ) VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?, 'scratch', 1)
            """,
            (
                child_id,
                "Implement deterministic scanner",
                "done",
                "default",
                "default",
                1010,
                1020,
                1090,
            ),
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (root_id, child_id),
        )
        conn.execute(
            """
            INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary)
            VALUES (?, 'default', 'done', 1010, 1100, 'completed', ?)
            """,
            (root_id, summary),
        )
        if external_followup:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, body, assignee, status, created_by, created_at,
                    workspace_kind, goal_mode
                ) VALUES (?, ?, ?, ?, 'ready', ?, ?, 'scratch', 1)
                """,
                (
                    follow_id,
                    "Harden delivery target selection",
                    "future work",
                    "default",
                    "default",
                    1110,
                ),
            )
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'default', ?, 1115)",
                (root_id, f"Created follow-up {follow_id} for delivery hardening."),
            )
        conn.commit()
    return follow_id


def test_audit_report_writes_json_and_markdown_for_required_boards(kanban_home):
    for slug in REQUIRED_BOARDS:
        _seed_board(slug)

    report, json_path, md_path = ka.write_report(boards=REQUIRED_BOARDS, now=1200)

    assert json_path == kanban_home / "reports" / "kanban-timer-audit-multi-19700101T002000Z.json"
    assert md_path == kanban_home / "reports" / "kanban-timer-audit-multi-19700101T002000Z.md"
    assert json_path.exists()
    assert md_path.exists()
    assert report["summary"]["boards_count"] == 3
    assert report["summary"]["task_count"] == 6
    assert report["summary"]["ownership_gap_count"] == 3
    assert report["summary"]["handoff_contract_gap_count"] == 3
    assert report["summary"]["non_goal_card_count"] == 3
    assert report["summary"]["cursor_workflow_gap_count"] == 0
    assert report["summary"]["blocked_count"] == 3
    assert report["summary"]["review_required_count"] == 3

    first_board = report["boards"][0]
    assert first_board["handoff_contract_gaps"][0]["violations"] == [
        "review_required_without_project_owner_acceptance",
        "blocked_without_next_action",
        "blocked_without_stale_after",
        "blocked_without_evidence",
    ]
    assert first_board["duration_summaries"]["created_to_started_seconds"]["avg"] == 10
    assert first_board["duration_summaries"]["started_to_terminal_seconds"]["avg"] == 60
    assert first_board["blocked"][0]["blocked_age_seconds"] == 80
    assert "Kanban timer/audit report" in md_path.read_text(encoding="utf-8")
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert on_disk["kind"] == "kanban_timer_audit_report"


def test_audit_report_multi_board_ignores_worker_db_env_override(kanban_home, monkeypatch):
    for slug in REQUIRED_BOARDS[:2]:
        _seed_board(slug)
    pinned = kb.board_dir(REQUIRED_BOARDS[0]) / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    report = ka.build_report(boards=REQUIRED_BOARDS[:2], now=1200)

    paths = [board["db_path"] for board in report["boards"]]
    assert paths == [
        str(kb.board_dir(REQUIRED_BOARDS[0]) / "kanban.db"),
        str(kb.board_dir(REQUIRED_BOARDS[1]) / "kanban.db"),
    ]
    assert len({task["id"] for board in report["boards"] for task in board["tasks"]}) == 4


def test_audit_report_cli_writes_paths_and_json_metadata(kanban_home):
    _seed_board("hermes-process-mastery")

    out = kc.run_slash("audit-report --boards hermes-process-mastery --json")
    payload = json.loads(out)

    assert payload["boards"] == ["hermes-process-mastery"]
    assert payload["summary"]["task_count"] == 2
    assert payload["summary"]["cursor_workflow_gap_count"] == 0
    assert Path(payload["json_path"]).exists()
    assert Path(payload["markdown_path"]).exists()


def test_resolve_boards_rejects_all_boards_with_explicit_list(kanban_home):
    with pytest.raises(ValueError, match="either --boards or --all-boards"):
        ka.resolve_boards(boards=["default"], all_boards=True)


def test_done_summary_emits_short_project_format_and_state_dedupes(kanban_home):
    follow_id = _seed_terminal_project("hermes-potential-flywheel")
    state_path = kanban_home / "kanban" / "done-summary-state.json"

    summaries, path = kds.due_summaries(
        boards=["hermes-potential-flywheel"],
        scope="project",
        state_path=state_path,
        update_state=True,
        now=1200,
    )

    assert path == state_path
    assert len(summaries) == 1
    text = summaries[0].text
    assert text.splitlines()[0] == "✅ hermes-potential-flywheel/ship done summaries done"
    assert "- Project: ship done summaries" in text
    assert "- Delivered:\n  • Shipped deterministic closeout summaries with duplicate suppression." in text
    assert "- Follow-ups created — waiting:\n  • Harden delivery target selection" in text
    assert follow_id not in text
    assert "| status |" not in text
    assert "assignee" not in text
    assert "- State: all 2 scoped task(s) done" in text

    second, _ = kds.due_summaries(
        boards=["hermes-potential-flywheel"],
        scope="project",
        state_path=state_path,
        update_state=True,
        now=1201,
    )
    assert second == []


def test_done_summary_cli_scans_requested_board_despite_worker_db_env(kanban_home, monkeypatch):
    _seed_terminal_project("pinned-board", external_followup=False)
    _seed_terminal_project("terminal-board", external_followup=False)
    pinned = kb.board_dir("pinned-board") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    out = kc.run_slash("done-summary --boards terminal-board --scope board --dry-run --json")
    payload = json.loads(out)

    assert len(payload["summaries"]) == 1
    assert payload["summaries"][0]["board"] == "terminal-board"
    assert payload["summaries"][0]["scope"] == "board"
    assert payload["summaries"][0]["text"].startswith("✅ Terminal Board done\n- Project: Terminal Board\n")


def test_done_summary_puts_measured_return_before_delivered_when_present(kanban_home):
    _seed_terminal_project(
        "measured-board",
        external_followup=False,
        summary=(
            "Shipped deterministic closeout summaries with duplicate suppression.\n"
            "Created follow-up t_dddd4444 for delivery hardening.\n"
            "Cold CLI/tool-schema path: ~10.3s → ~0.22s, about 47× faster."
        ),
    )

    summaries, _ = kds.due_summaries(
        boards=["measured-board"],
        scope="project",
        state_path=kanban_home / "kanban" / "measured-state.json",
        update_state=False,
    )

    text = summaries[0].text
    measured_idx = text.index("- Measured return:")
    delivered_idx = text.index("- Delivered:")
    assert measured_idx < delivered_idx
    assert "  • Cold CLI/tool-schema path: ~10.3s → ~0.22s, about 47× faster." in text
    assert "  • Shipped deterministic closeout summaries with duplicate suppression." in text
    assert "t_dddd4444" not in text
    assert "Created follow-up" not in text
