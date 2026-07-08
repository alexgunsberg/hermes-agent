import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb


class DummyGateway(GatewaySlashCommandsMixin):
    _kanban_notifier_profile = "default"

    def _active_profile_name(self):
        return "default"


def _source(thread_id="thread-general"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-test",
        chat_type="group",
        user_id="user-1",
        user_name="Alex",
        thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_kanban_inbox_bind_and_plain_message_creates_triage_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    gateway = DummyGateway()
    source = _source()

    bind_reply = await gateway._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )
    assert "Plain messages here now queue" in bind_reply

    reply = await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(
            text="timer freezes after editing a WOD",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m1",
        )
    )
    assert reply is not None
    assert "→ `general`" in reply
    assert "· `bug` · triage" in reply

    with kb.connect_closing(board="general") as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].status == "triage"
    assert tasks[0].assignee == "default"


@pytest.mark.asyncio
async def test_kanban_inbox_is_idempotent_per_platform_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    gateway = DummyGateway()
    source = _source()
    await gateway._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )
    event = MessageEvent(text="Bug: duplicate smoke", message_type=MessageType.TEXT, source=source, message_id="same")

    first = await gateway._maybe_handle_kanban_inbox_message(event)
    second = await gateway._maybe_handle_kanban_inbox_message(event)

    assert first == second
    with kb.connect_closing(board="general") as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_kanban_inbox_skips_slash_commands_and_unbound_topics(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    gateway = DummyGateway()
    source = _source()
    await gateway._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )

    assert await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(text="/status", message_type=MessageType.TEXT, source=source, message_id="cmd")
    ) is None
    assert await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(text="plain in another thread", message_type=MessageType.TEXT, source=_source("other"), message_id="m2")
    ) is None
