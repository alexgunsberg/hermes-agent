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


class TestAlerts:
    def test_context_alert_fires_and_dedups(self):
        big = dict(_api_kwargs()["usage"], prompt_tokens=200_000)
        perf_monitor.on_post_api_request(**_api_kwargs(usage=big))
        perf_monitor.on_post_api_request(**_api_kwargs(usage=big))

        conn = sqlite3.connect(perf_monitor._db_path())
        rows = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE kind = 'context_size'"
        ).fetchone()[0]
        conn.close()
        assert rows == 1  # second one deduped within the hour

    def test_session_token_alert(self):
        usage = dict(_api_kwargs()["usage"], total_tokens=3_000_000)
        perf_monitor.on_post_api_request(**_api_kwargs(usage=usage))

        conn = sqlite3.connect(perf_monitor._db_path())
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
        conn.close()
        assert "session_tokens" in kinds

    def test_slow_tool_alert(self):
        perf_monitor.on_post_tool_call(
            tool_name="cursor_agent", session_id="s", duration_ms=300_000,
            status="success", result="",
        )
        conn = sqlite3.connect(perf_monitor._db_path())
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
        conn.close()
        assert "slow_tool" in kinds

    def test_tool_error_rate_alert_needs_min_calls(self):
        for _ in range(9):
            perf_monitor.on_post_tool_call(
                tool_name="patch", duration_ms=10, status="error",
                error_type="E", result="",
            )
        conn = sqlite3.connect(perf_monitor._db_path())
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
        assert "tool_error_rate" not in kinds  # only 9 calls
        conn.close()
        perf_monitor.on_post_tool_call(
            tool_name="patch", duration_ms=10, status="error",
            error_type="E", result="",
        )
        conn = sqlite3.connect(perf_monitor._db_path())
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
        conn.close()
        assert "tool_error_rate" in kinds


class TestCompressionEvents:
    def test_failure_streak_raises_alert(self):
        perf_monitor.record_compression("s", ok=True, tokens_before=300_000,
                                        tokens_after=50_000)
        for _ in range(3):
            perf_monitor.record_compression("s", ok=False, tokens_before=300_000)

        conn = sqlite3.connect(perf_monitor._db_path())
        conn.row_factory = sqlite3.Row
        events = conn.execute("SELECT COUNT(*) FROM compression_events").fetchone()[0]
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM alerts")]
        conn.close()
        assert events == 4
        assert "compression_failures" in kinds

    def test_report_includes_compression_stats(self):
        perf_monitor.record_compression("s", ok=True, tokens_before=100_000,
                                        tokens_after=20_000)
        report = perf_monitor.generate_report(days=1)
        assert report["compression"]["attempts"] == 1
        assert report["compression"]["successes"] == 1


class TestHookEvents:
    def test_recorded_and_reported(self):
        for _ in range(6):
            perf_monitor.record_hook_event(
                "post_tool_call", "notify-bridge.sh", 5000, timed_out=True,
            )
        report = perf_monitor.generate_report(days=1)
        assert report["hooks"][0]["command"] == "notify-bridge.sh"
        assert report["hooks"][0]["timeouts"] == 6
        assert any("circuit breaker" in p for p in report["proposals"])


class TestMaintenance:
    def test_runs_once_per_day_and_fails_open(self):
        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        # A plain sqlite file without FTS tables: optimize statements must
        # fail open, the checkpoint succeeds, and the meta stamp is written.
        sqlite3.connect(home / "state.db").close()

        perf_monitor._run_maintenance()

        conn = sqlite3.connect(perf_monitor._db_path())
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_maintenance_at'"
        ).fetchone()
        conn.close()
        assert stamp is not None
        perf_monitor._run_maintenance()  # within a day: early return, no error

    def test_catchup_alert_after_idle_gap(self):
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = perf_monitor._connect()
        conn.execute(
            "INSERT INTO api_events (created_at, session_id) VALUES (?, 's')",
            (old,),
        )
        conn.commit()
        conn.close()

        perf_monitor._check_catchup()

        conn = sqlite3.connect(perf_monitor._db_path())
        kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts")]
        conn.close()
        assert "catchup" in kinds


class TestProposals:
    def test_oversized_gauges_produce_proposals(self):
        conn = perf_monitor._connect()
        conn.executemany(
            "INSERT INTO gauges (created_at, name, value) VALUES (?, ?, ?)",
            [
                (perf_monitor._utc_now(), "logs_dir_bytes", 900e6),
                (perf_monitor._utc_now(), "state_db_bytes", 2e9),
                (perf_monitor._utc_now(), "skills_count", 300),
            ],
        )
        conn.commit()
        conn.close()

        proposals = perf_monitor.generate_report(days=1)["proposals"]
        text = "\n".join(proposals)
        assert "logs directory" in text
        assert "state.db" in text
        assert "curation" in text


class TestShellHookCircuitBreaker:
    @staticmethod
    def _make_observer_spec():
        from agent import shell_hooks

        return shell_hooks.ShellHookSpec(
            event="post_tool_call", command="/bin/false-bridge",
            matcher=None, timeout=1, blocking=False,
        )

    def _run_synchronously(self, monkeypatch, spawn_results):
        from agent import shell_hooks

        calls = {"n": 0}

        def fake_spawn(spec, stdin_json):
            calls["n"] += 1
            return spawn_results(calls["n"])

        class SyncThread:
            def __init__(self, target=None, args=(), **kwargs):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        monkeypatch.setattr(shell_hooks, "_spawn", fake_spawn)
        monkeypatch.setattr(shell_hooks.threading, "Thread", SyncThread)
        cb = shell_hooks._make_callback(self._make_observer_spec())
        return cb, calls

    def test_opens_after_three_consecutive_failures(self, monkeypatch):
        timeout_result = {
            "returncode": None, "stdout": "", "stderr": "",
            "timed_out": True, "elapsed_seconds": 1.0, "error": None,
        }
        cb, calls = self._run_synchronously(monkeypatch, lambda n: dict(timeout_result))
        for _ in range(6):
            cb(tool_name="terminal")
        # Three real spawns, then the circuit opens and skips the rest.
        assert calls["n"] == 3

    def test_success_resets_failure_count(self, monkeypatch):
        ok_result = {
            "returncode": 0, "stdout": "", "stderr": "",
            "timed_out": False, "elapsed_seconds": 0.1, "error": None,
        }
        timeout_result = dict(ok_result, timed_out=True)

        def results(n):
            return dict(timeout_result if n % 3 else ok_result)  # every 3rd ok

        cb, calls = self._run_synchronously(monkeypatch, results)
        for _ in range(9):
            cb(tool_name="terminal")
        assert calls["n"] == 9  # breaker never opens: streak keeps resetting

    def test_blocking_hooks_are_never_skipped(self, monkeypatch):
        from agent import shell_hooks

        spec = shell_hooks.ShellHookSpec(
            event="pre_tool_call", command="/bin/policy",
            matcher=None, timeout=1, blocking=True,
        )
        calls = {"n": 0}

        def fake_spawn(_spec, _stdin):
            calls["n"] += 1
            return {
                "returncode": None, "stdout": "", "stderr": "",
                "timed_out": True, "elapsed_seconds": 1.0, "error": None,
            }

        monkeypatch.setattr(shell_hooks, "_spawn", fake_spawn)
        cb = shell_hooks._make_callback(spec)
        for _ in range(6):
            cb(tool_name="terminal")
        assert calls["n"] == 6  # policy hooks always run


class TestDelegationMaxRunTokens:
    def test_resolver(self, monkeypatch):
        import tools.delegate_tool as dt

        monkeypatch.setattr(dt, "_load_config", lambda: {"max_run_tokens": 400_000})
        assert dt._get_subagent_max_run_tokens() == 400_000
        monkeypatch.setattr(dt, "_load_config", lambda: {})
        assert dt._get_subagent_max_run_tokens() == 0
        monkeypatch.setattr(dt, "_load_config", lambda: {"max_run_tokens": "junk"})
        assert dt._get_subagent_max_run_tokens() == 0
