"""Tests for the built-in event-driven performance monitor."""

from __future__ import annotations

import sqlite3

import pytest

from agent import perf_monitor
from agent.iteration_budget import IterationBudget
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _fresh_monitor():
    perf_monitor._reset_for_tests()
    yield
    perf_monitor._reset_for_tests()


def _api_kwargs(**overrides):
    kwargs = dict(
        session_id="sess-1",
        turn_id="turn-1",
        platform="cli",
        model="test-model",
        provider="test-provider",
        api_call_count=3,
        api_duration=1.25,
        finish_reason="stop",
        message_count=12,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 20,
            "reasoning_tokens": 0,
            "request_count": 1,
            "prompt_tokens": 1120,
            "total_tokens": 1170,
        },
        telemetry_schema_version="hermes.observer.v1",
    )
    kwargs.update(overrides)
    return kwargs


def test_records_api_event():
    perf_monitor.on_post_api_request(**_api_kwargs())

    conn = sqlite3.connect(perf_monitor._db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM api_events").fetchone()
    conn.close()

    assert row["session_id"] == "sess-1"
    assert row["duration_ms"] == 1250
    assert row["prompt_tokens"] == 1120
    assert row["total_tokens"] == 1170
    assert row["origin"] == "interactive"


def test_api_event_with_no_usage_is_recorded():
    perf_monitor.on_post_api_request(**_api_kwargs(usage=None))

    conn = sqlite3.connect(perf_monitor._db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM api_events").fetchone()
    conn.close()

    assert row is not None
    assert row["total_tokens"] == 0


def test_cron_origin_detected(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    perf_monitor.on_post_api_request(**_api_kwargs())

    conn = sqlite3.connect(perf_monitor._db_path())
    origin = conn.execute("SELECT origin FROM api_events").fetchone()[0]
    conn.close()
    assert origin == "cron"


def test_records_tool_event():
    perf_monitor.on_post_tool_call(
        tool_name="terminal",
        session_id="sess-1",
        turn_id="turn-1",
        duration_ms=340,
        status="success",
        error_type=None,
        result="x" * 500,
        telemetry_schema_version="hermes.observer.v1",
    )

    conn = sqlite3.connect(perf_monitor._db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tool_events").fetchone()
    conn.close()

    assert row["tool_name"] == "terminal"
    assert row["duration_ms"] == 340
    assert row["output_chars"] == 500


def test_callbacks_never_raise_on_garbage():
    # Fail-open contract: junk payloads must not raise.
    perf_monitor.on_post_api_request(usage="not-a-dict", api_duration="nan?")
    perf_monitor.on_post_tool_call(duration_ms=object())


def test_sample_gauges_records_state_db_size():
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "state.db").write_bytes(b"0" * 2048)

    perf_monitor.sample_gauges()

    conn = sqlite3.connect(perf_monitor._db_path())
    conn.row_factory = sqlite3.Row
    rows = {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM gauges")}
    conn.close()
    assert rows.get("state_db_bytes") == 2048.0


def test_report_math_and_formatting():
    for i, duration in enumerate((1.0, 2.0, 3.0, 4.0, 10.0)):
        perf_monitor.on_post_api_request(
            **_api_kwargs(api_duration=duration, api_call_count=i + 1)
        )
    perf_monitor.on_post_tool_call(
        tool_name="terminal", duration_ms=1000, status="error",
        error_type="RuntimeError", result="boom",
    )

    report = perf_monitor.generate_report(days=1)
    assert report["api"]["calls"] == 5
    assert report["api"]["median_ms"] == 3000.0
    assert report["api"]["max_ms"] == 10000.0
    assert report["api"]["total_tokens"] == 5 * 1170
    # non-cache-read tokens: (1170 - 1000) per call
    assert report["api"]["uncached_tokens"] == 5 * 170
    assert report["tools"][0]["tool_name"] == "terminal"
    assert report["tools"][0]["errors"] == 1

    text = perf_monitor.format_report(report)
    assert "Model requests: 5" in text
    assert "terminal" in text


def test_empty_report_says_no_material_usage():
    text = perf_monitor.format_report(perf_monitor.generate_report(days=1))
    assert "No material usage" in text


def test_install_registers_hooks_and_is_idempotent():
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    before_api = list(manager._hooks.get("post_api_request", []))

    assert perf_monitor.install() is True
    assert perf_monitor.install() is True  # idempotent

    after_api = manager._hooks.get("post_api_request", [])
    added = [cb for cb in after_api if cb not in before_api]
    assert added == [perf_monitor.on_post_api_request]
    assert manager.has_hook("post_tool_call")

    # Clean up the process-global manager for other tests.
    manager._hooks["post_api_request"].remove(perf_monitor.on_post_api_request)
    manager._hooks["post_tool_call"].remove(perf_monitor.on_post_tool_call)


class TestIterationBudgetExhaust:
    def test_exhaust_zeroes_remaining_and_blocks_consume(self):
        budget = IterationBudget(10)
        assert budget.consume()
        budget.exhaust()
        assert budget.remaining == 0
        assert not budget.consume()

    def test_refund_cannot_revive_exhausted_budget(self):
        budget = IterationBudget(10)
        budget.consume()
        budget.exhaust()
        budget.refund()
        assert budget.remaining == 0
        assert not budget.consume()


class TestCronMaxRunTokens:
    def test_job_override_wins(self):
        from cron.scheduler import _resolve_cron_max_run_tokens

        assert _resolve_cron_max_run_tokens(
            {"max_run_tokens": 5000}, {"cron": {"max_run_tokens": 100}}
        ) == 5000

    def test_config_default_and_unlimited(self):
        from cron.scheduler import _resolve_cron_max_run_tokens

        assert _resolve_cron_max_run_tokens({}, {"cron": {"max_run_tokens": 250_000}}) == 250_000
        assert _resolve_cron_max_run_tokens({}, {}) == 0

    def test_invalid_values_disable_ceiling(self):
        from cron.scheduler import _resolve_cron_max_run_tokens

        assert _resolve_cron_max_run_tokens({"max_run_tokens": "junk"}, {}) == 0
        assert _resolve_cron_max_run_tokens({"max_run_tokens": -5}, {}) == 0
