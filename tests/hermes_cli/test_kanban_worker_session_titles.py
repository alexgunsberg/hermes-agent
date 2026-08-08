"""Regression tests for meaningful Kanban worker session titles."""

from hermes_cli import kanban_db as kb


def _task(title: str) -> kb.Task:
    return kb.Task(
        id="t_b21733fb",
        title=title,
        body=None,
        assignee="default",
        status="in_progress",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def test_worker_session_title_uses_card_title_and_unique_task_suffix():
    assert kb.kanban_worker_session_title(_task("Repair semantic session titles")) == (
        "Repair semantic session titles · t_b21733fb"
    )


def test_worker_session_title_collapses_whitespace_and_is_db_safe():
    title = kb.kanban_worker_session_title(
        _task("  Repair   semantic session titles " + "safely " * 30)
    )

    assert title.startswith("Repair semantic session titles")
    assert title.endswith("... · t_b21733fb")
    assert len(title) <= 80


def test_worker_session_title_removes_control_and_bidi_characters():
    assert kb.kanban_worker_session_title(
        _task("Repair\x00 semantic\u202e titles")
    ) == "Repair semantic titles · t_b21733fb"


def test_worker_session_title_handles_blank_card_title():
    assert kb.kanban_worker_session_title(_task(" \n\t ")) == (
        "Kanban task · t_b21733fb"
    )
