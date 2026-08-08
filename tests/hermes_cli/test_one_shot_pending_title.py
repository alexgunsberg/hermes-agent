"""Regression tests for source-owned titles in quiet one-shot sessions."""

from unittest.mock import MagicMock

from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


def test_one_shot_pending_title_uses_atomic_dedupe_without_stdout(monkeypatch, capsys):
    from agent import title_generator

    cli = CLIAgentSetupMixin()
    cli._session_db = MagicMock()
    cli.session_id = "session-1"
    setattr(cli, "_pending_title", "Repair semantic session titles · t_b21733fb")
    setattr(cli, "_single_query_mode", True)

    cli.agent = MagicMock()
    cli.agent._session_db_created = True
    cli.agent._ensure_db_session.return_value = None

    persist = MagicMock(return_value="Repair semantic session titles · t_b21733fb #2")
    monkeypatch.setattr(title_generator, "_persist_session_title", persist)

    getattr(cli, "_apply_pending_session_title")()

    persist.assert_called_once_with(
        cli._session_db,
        cli.session_id,
        "Repair semantic session titles · t_b21733fb",
    )
    assert cli._pending_title is None
    assert "Session title applied" not in capsys.readouterr().out


def test_interactive_pending_title_preserves_strict_setter(capsys):
    cli = CLIAgentSetupMixin()
    cli._session_db = MagicMock()
    cli.session_id = "session-1"
    setattr(cli, "_pending_title", "Manual title")
    cli.agent = MagicMock()
    cli.agent._session_db_created = True
    cli.agent._ensure_db_session.return_value = None

    getattr(cli, "_apply_pending_session_title")()

    cli._session_db.set_session_title.assert_called_once_with(
        "session-1", "Manual title"
    )
    assert cli._pending_title is None
    assert "Session title applied: Manual title" in capsys.readouterr().out
