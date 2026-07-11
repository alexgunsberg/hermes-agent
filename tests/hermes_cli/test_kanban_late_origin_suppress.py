"""Canonical-target late-origin subscription suppression tests.

Covers the decision helper plus gateway command-mode ``/kanban create``
integration, reusing the kanban_home / notifier fixture style from
``test_kanban_notify.py`` and Matrix home-channel fixtures from the
dashboard/notifier suites.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_late_origin import (
    origin_matches_target,
    resolve_canonical_report_target,
    should_suppress_late_origin_subscription,
)


# ---------------------------------------------------------------------------
# Fixtures (aligned with tests/hermes_cli/test_kanban_notify.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


@pytest.fixture
def telegram_home_channel(monkeypatch):
    """Matrix/notifier-style home-channel env overlay (telegram + matrix)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-100111")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "17")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Reports")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "matrix_fake")
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example")
    monkeypatch.setenv("MATRIX_HOME_ROOM", "!reports:example.org")
    monkeypatch.setenv("MATRIX_HOME_ROOM_NAME", "Matrix Reports")


# ---------------------------------------------------------------------------
# Pure decision helper
# ---------------------------------------------------------------------------


def test_no_canonical_target_allows_late_origin(monkeypatch):
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with patch(
        "hermes_cli.kanban_late_origin.resolve_canonical_report_target",
        return_value=None,
    ):
        assert should_suppress_late_origin_subscription(
            platform="telegram",
            chat_id="chat-origin",
            thread_id="9",
        ) is False


def test_origin_is_home_allows_late_origin(telegram_home_channel):
    canonical = resolve_canonical_report_target("telegram")
    assert canonical is not None
    assert canonical["chat_id"] == "-100111"
    assert should_suppress_late_origin_subscription(
        platform="telegram",
        chat_id="-100111",
        thread_id="17",
        canonical_target=canonical,
    ) is False


def test_home_exists_and_origin_differs_suppresses(telegram_home_channel):
    canonical = resolve_canonical_report_target("telegram")
    assert should_suppress_late_origin_subscription(
        platform="telegram",
        chat_id="chat-command",
        thread_id="99",
        canonical_target=canonical,
    ) is True


def test_existing_non_origin_sub_suppresses_even_without_home():
    existing = [
        {
            "platform": "telegram",
            "chat_id": "-100999",
            "thread_id": "1",
        }
    ]
    assert should_suppress_late_origin_subscription(
        platform="telegram",
        chat_id="chat-command",
        thread_id="2",
        existing_subs=existing,
        canonical_target=None,
    ) is True


def test_existing_origin_only_sub_does_not_suppress():
    existing = [
        {
            "platform": "telegram",
            "chat_id": "chat-command",
            "thread_id": "2",
        }
    ]
    assert should_suppress_late_origin_subscription(
        platform="telegram",
        chat_id="chat-command",
        thread_id="2",
        existing_subs=existing,
        canonical_target=None,
    ) is False


def test_matrix_home_is_canonical_report_target(telegram_home_channel):
    canonical = resolve_canonical_report_target("matrix")
    assert canonical is not None
    assert canonical["chat_id"] == "!reports:example.org"
    assert should_suppress_late_origin_subscription(
        platform="matrix",
        chat_id="!other:example.org",
        canonical_target=canonical,
    ) is True
    assert should_suppress_late_origin_subscription(
        platform="matrix",
        chat_id="!reports:example.org",
        canonical_target=canonical,
    ) is False


# ---------------------------------------------------------------------------
# Full routing matrix (~480 rows)
# platforms × home_mode × origin_lane × thread_mode × existing_sub_mode
# ---------------------------------------------------------------------------

_MATRIX_PLATFORMS = (
    "telegram",
    "discord",
    "slack",
    "matrix",
    "signal",
    "whatsapp",
    "feishu",
    "wecom",
    "mattermost",
    "bluebubbles",
)
_HOME_MODES = ("none", "configured")
_ORIGIN_LANES = ("home", "other", "empty_chat")
_THREAD_MODES = ("none", "match_home", "mismatch")
_EXISTING_MODES = ("none", "origin_only", "canonical_other", "unrelated")

_ROUTING_MATRIX = list(
    itertools.product(
        _MATRIX_PLATFORMS,
        _HOME_MODES,
        _ORIGIN_LANES,
        _THREAD_MODES,
        _EXISTING_MODES,
    )
)
assert len(_ROUTING_MATRIX) == 480, len(_ROUTING_MATRIX)


def _expected_suppress(
    home_mode: str,
    origin_lane: str,
    thread_mode: str,
    existing_mode: str,
) -> bool:
    if origin_lane == "empty_chat":
        return False

    # Existing non-origin sub always suppresses.
    if existing_mode in ("canonical_other", "unrelated"):
        return True

    if home_mode == "none":
        return False

    # Home configured: suppress when origin is not the home lane.
    if origin_lane == "other":
        return True

    # origin_lane == "home": thread must also match for allow.
    if thread_mode == "mismatch":
        return True
    return False


@pytest.mark.parametrize(
    "platform,home_mode,origin_lane,thread_mode,existing_mode",
    _ROUTING_MATRIX,
    ids=[
        f"{p}|home={h}|origin={o}|thr={t}|exist={e}"
        for p, h, o, t, e in _ROUTING_MATRIX
    ],
)
def test_late_origin_suppress_routing_matrix(
    platform, home_mode, origin_lane, thread_mode, existing_mode
):
    home_chat = f"{platform}-home"
    home_thread = "42"
    other_chat = f"{platform}-other"
    other_thread = "99"

    if home_mode == "configured":
        canonical = {
            "platform": platform,
            "chat_id": home_chat,
            "thread_id": home_thread,
        }
    else:
        canonical = None

    if origin_lane == "empty_chat":
        chat_id = ""
        thread_id = None
    elif origin_lane == "home":
        chat_id = home_chat
        if thread_mode == "none":
            thread_id = None
        elif thread_mode == "match_home":
            thread_id = home_thread
        else:
            thread_id = other_thread
    else:  # other
        chat_id = other_chat
        if thread_mode == "none":
            thread_id = None
        elif thread_mode == "match_home":
            thread_id = home_thread
        else:
            thread_id = other_thread

    existing: list[dict] = []
    if existing_mode == "origin_only" and chat_id:
        existing = [
            {
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": thread_id,
            }
        ]
    elif existing_mode == "canonical_other":
        existing = [
            {
                "platform": platform,
                "chat_id": home_chat,
                "thread_id": home_thread,
            }
        ]
    elif existing_mode == "unrelated":
        existing = [
            {
                "platform": platform,
                "chat_id": f"{platform}-report",
                "thread_id": "7",
            }
        ]

    # When origin is home with thread_mode none and home has a thread, the
    # origin does not match the canonical key — expect suppress when home
    # is configured (covered by _expected_suppress via mismatch semantics
    # only for explicit mismatch; refine for none-vs-home-thread).
    expect = _expected_suppress(home_mode, origin_lane, thread_mode, existing_mode)
    if (
        home_mode == "configured"
        and origin_lane == "home"
        and thread_mode == "none"
        and existing_mode in ("none", "origin_only")
    ):
        # Home pins thread_id=42; origin has no thread → not the same lane.
        expect = True

    got = should_suppress_late_origin_subscription(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        existing_subs=existing,
        canonical_target=canonical,
    )
    assert got is expect


def test_origin_matches_target_helper():
    target = {"platform": "telegram", "chat_id": "1", "thread_id": "2"}
    assert origin_matches_target("telegram", "1", "2", target)
    assert not origin_matches_target("telegram", "1", "3", target)


# ---------------------------------------------------------------------------
# Gateway command-mode integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_create_suppresses_late_origin_when_home_differs(
    kanban_home, telegram_home_channel
):
    """Telegram ``/kanban create`` from a non-home topic must not late-
    subscribe the origin when a canonical home report target exists."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat-command",
        thread_id="th-cmd",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban create "late-origin suppress" --assignee alice',
        source=source,
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "Created t_" in out
    assert "subscribed" not in out.lower()

    conn = kb.connect()
    try:
        tasks = kb.list_tasks(conn)
        subs = kb.list_notify_subs(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert subs == [], (
        "late origin subscription must be suppressed when telegram home "
        f"channel is configured; got {subs!r}"
    )


@pytest.mark.asyncio
async def test_gateway_create_still_subscribes_when_origin_is_home(
    kanban_home, telegram_home_channel
):
    """Creating from the home channel itself must still auto-subscribe."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="-100111",
        thread_id="17",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban create "from home" --assignee alice',
        source=source,
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)
    assert "subscribed" in out.lower()

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["chat_id"] == "-100111"
    assert str(subs[0]["thread_id"]) == "17"


@pytest.mark.asyncio
async def test_gateway_create_suppresses_when_existing_canonical_sub(
    kanban_home, monkeypatch
):
    """Even without a home channel, an existing non-origin sub suppresses
    the late origin write (e.g. prior home-subscribe / inherited target)."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    # Ensure no home channel is configured for this case.
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    # Pre-create will happen inside the handler; seed by patching
    # list_notify_subs to report an existing canonical sub after create.
    real_list = kb.list_notify_subs
    real_add = kb.add_notify_sub
    add_calls: list = []

    def _list(conn, task_id=None, **kw):
        # After create the task exists; pretend a canonical sub is already there.
        return [
            {
                "platform": "telegram",
                "chat_id": "-100999",
                "thread_id": "1",
                "task_id": task_id,
            }
        ]

    def _add(conn, **kw):
        add_calls.append(kw)
        return real_add(conn, **kw)

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat-command",
        thread_id="th1",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban create "has canonical" --assignee alice',
        source=source,
    )

    with patch("hermes_cli.kanban_db.list_notify_subs", side_effect=_list), patch(
        "hermes_cli.kanban_db.add_notify_sub", side_effect=_add
    ):
        out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "Created t_" in out
    assert "subscribed" not in out.lower()
    assert add_calls == []
