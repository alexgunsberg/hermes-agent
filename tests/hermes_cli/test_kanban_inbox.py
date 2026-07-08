import json
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_inbox as inbox
from hermes_cli.kanban import run_slash


def _event(text="timer freezes after editing WOD", message_id="123", thread_id="7"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="42",
            thread_id=thread_id,
        ),
        message_id=message_id,
    )


def test_inbox_bind_status_unbind_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(board="general")

    out = run_slash(
        "inbox bind general --platform telegram --chat-id -1001 --thread-id 7 "
        "--board general --assignee default --json"
    )
    payload = json.loads(out)
    assert payload["binding"]["board"] == "general"
    assert payload["binding"]["assignee"] == "default"

    status = json.loads(run_slash("inbox status --json"))
    assert status["bindings"][0]["name"] == "general"
    assert status["bindings"][0]["thread_id"] == "7"

    removed = json.loads(run_slash("inbox unbind general --json"))
    assert removed["removed"] is True
    assert inbox.load_bindings() == []


def test_plain_bound_message_creates_idempotent_triage_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(board="general")
    inbox.upsert_binding(
        name="general",
        platform="telegram",
        chat_id="-1001",
        thread_id="7",
        board="general",
        assignee="default",
    )

    result1 = inbox.capture_inbox_message(_event())
    result2 = inbox.capture_inbox_message(_event())

    assert result1 is not None
    assert result2 is not None
    assert result1["task_id"] == result2["task_id"]
    assert "Queued" in result1["receipt"]
    with kb.connect_closing(board="general") as conn:
        task = kb.get_task(conn, result1["task_id"])
        assert task is not None
        assert task.status == "triage"
        assert task.assignee == "default"
        assert "timer freezes after editing WOD" in (task.body or "")
        subs = kb.list_notify_subs(conn, task.id)
    assert subs and subs[0]["thread_id"] == "7"


def test_slash_and_unbound_messages_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db(board="general")
    inbox.upsert_binding(
        name="general",
        platform="telegram",
        chat_id="-1001",
        thread_id="7",
        board="general",
        assignee="default",
    )

    assert inbox.capture_inbox_message(_event(text="/kanban list")) is None
    assert inbox.capture_inbox_message(_event(thread_id="8")) is None


def test_profile_binding_store_is_isolated(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "profiles" / "websites").mkdir(parents=True)

    inbox.upsert_binding(
        name="websites",
        profile="websites",
        platform="telegram",
        chat_id="-1002",
        thread_id="1",
        board="websites",
        assignee="websites",
    )

    assert inbox.load_bindings() == []
    prof_bindings = inbox.load_bindings("websites")
    assert len(prof_bindings) == 1
    assert prof_bindings[0].board == "websites"
