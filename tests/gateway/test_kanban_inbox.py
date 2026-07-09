from datetime import datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
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
async def test_kanban_inbox_does_not_subscribe_replies_to_capture_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    gateway = DummyGateway()
    source = _source()
    await gateway._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )

    reply = await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(
            text="task: keep this as capture only",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m-no-sub",
        )
    )

    assert reply is not None
    with kb.connect_closing(board="general") as conn:
        tasks = kb.list_tasks(conn)
        subs = kb.list_notify_subs(conn, task_id=tasks[0].id)
    assert len(tasks) == 1
    assert subs == []


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
async def test_kanban_inbox_missing_message_id_uses_content_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    gateway = DummyGateway()
    source = _source()
    await gateway._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )

    first = await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(text="Bug: one", message_type=MessageType.TEXT, source=source)
    )
    second = await gateway._maybe_handle_kanban_inbox_message(
        MessageEvent(text="Bug: two", message_type=MessageType.TEXT, source=source)
    )

    assert first != second
    with kb.connect_closing(board="general") as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 2


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


def _make_runner_for_inbox_handle_message(monkeypatch, *, authorized=True):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])

    runner = cast(Any, object.__new__(GatewayRunner))
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.session_store = MagicMock()
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=authorized)
    runner._get_unauthorized_dm_behavior = MagicMock(return_value="ignore")
    runner._adapter_for_source = MagicMock(return_value=None)
    runner._kanban_notifier_profile = "default"
    runner._active_profile_name = MagicMock(return_value="default")
    runner._startup_restore_in_progress = False
    runner._update_prompt_pending = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    adapter = MagicMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    return runner


@pytest.mark.asyncio
async def test_kanban_inbox_handle_message_runs_after_auth_but_before_session_and_busy_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    runner = _make_runner_for_inbox_handle_message(monkeypatch, authorized=True)
    source = _source()
    await runner._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )
    sk = build_session_key(source)
    session_entry = SessionEntry(
        session_key=sk,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    running_agent = MagicMock()
    runner._running_agents[sk] = running_agent

    reply = await runner._handle_message(
        MessageEvent(text="bug: captured before busy path", message_type=MessageType.TEXT, source=source, message_id="m3")
    )

    assert reply is not None
    assert "Queued `" in reply
    runner._is_user_authorized.assert_called_once_with(source)
    runner.session_store.get_or_create_session.assert_not_called()
    running_agent.interrupt.assert_not_called()
    with kb.connect_closing(board="general") as conn:
        tasks = kb.list_tasks(conn)
    assert [task.title for task in tasks] == ["captured before busy path"]


@pytest.mark.asyncio
async def test_kanban_inbox_handle_message_does_not_capture_unauthorized_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.write_board_metadata("general", name="General")
    runner = _make_runner_for_inbox_handle_message(monkeypatch, authorized=False)
    source = _source()
    await runner._handle_kanban_inbox_command(
        MessageEvent(text="/kanban inbox bind general", message_type=MessageType.TEXT, source=source),
        "bind general",
    )

    reply = await runner._handle_message(
        MessageEvent(text="bug: unauthorized", message_type=MessageType.TEXT, source=source, message_id="m4")
    )

    assert reply is None
    with kb.connect_closing(board="general") as conn:
        assert kb.list_tasks(conn) == []
