from __future__ import annotations

import json
import sqlite3

from hermes_cli import performance_metrics as pm


def _record_series(store, area: str, values: list[float], start: int = 1) -> None:
    for offset, value in enumerate(values):
        pm.record_observation(
            area,
            elapsed_s=value,
            status="measured",
            details={"sample_index": offset},
            observed_at=start + offset,
            store_path=store,
        )


def test_recording_redacts_sensitive_details_and_covers_scoped_areas(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    store = tmp_path / "metrics.db"

    pm.record_observation(
        "python_startup",
        elapsed_s=0.123,
        details={
            "prompt": "SECRET_PROMPT_TEXT",
            "safe_counter": 7,
            "note": "token=SECRET_TOKEN_VALUE",
            "nested": {"api_key": "sk-SECRETKEYVALUE123456789"},
        },
        source="unit-test",
        store_path=store,
    )
    pm.record_observation(
        "live_chat_no_tools",
        status="skipped",
        skip_reason="disabled in tests; bearer SECRET_BEARER_VALUE",
        store_path=store,
    )

    observations = pm.load_observations(store_path=store)
    serialized = json.dumps(observations, sort_keys=True)

    assert "SECRET_PROMPT_TEXT" not in serialized
    assert "SECRET_TOKEN_VALUE" not in serialized
    assert "SECRETKEYVALUE" not in serialized
    assert "SECRET_BEARER_VALUE" not in serialized
    assert "safe_counter" in serialized

    coverage = pm.build_area_coverage(observations)
    assert set(coverage) == set(pm.AREA_NAMES)
    assert len(coverage) == 12
    assert coverage["python_startup"]["status"] == "measured"
    assert coverage["live_chat_no_tools"]["status"] == "skipped"


def test_baseline_trend_logic_flags_regression_and_suppresses_below_threshold(tmp_path):
    store = tmp_path / "metrics.db"
    _record_series(store, "python_startup", [1.0, 1.0, 1.0, 2.4, 2.5, 2.6])
    _record_series(store, "hermes_cli_startup", [4.0, 4.1, 4.0, 4.2, 4.1, 4.2], start=20)

    report = pm.compute_baseline_and_trends(
        store_path=store,
        threshold_ratio=1.5,
        min_delta_s=0.5,
        min_baseline=3,
        recent_window=3,
    )

    assert report["trends"]["python_startup"]["trend_status"] == "regression"
    assert report["trends"]["python_startup"]["baseline_median_s"] == 1.0
    assert report["trends"]["python_startup"]["recent_median_s"] == 2.5
    assert report["trends"]["hermes_cli_startup"]["trend_status"] == "below_threshold"
    assert [item["area"] for item in report["regressions"]] == ["python_startup"]


def test_synthetic_regression_task_creation_dedupes_by_fingerprint(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    db_path = tmp_path / "kanban.db"
    store = tmp_path / "metrics.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    _record_series(store, "python_startup", [1.0, 1.0, 1.0, 3.0, 3.1, 3.2])
    report = pm.compute_baseline_and_trends(
        store_path=store,
        threshold_ratio=1.5,
        min_delta_s=0.5,
        min_baseline=3,
        recent_window=3,
    )

    first = pm.create_synthetic_regression_tasks(report, store_path=store, assignee="default")
    second = pm.create_synthetic_regression_tasks(report, store_path=store, assignee="default")

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["task_id"] == second[0]["task_id"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT title, body, assignee, goal_mode, idempotency_key FROM tasks").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Investigate Hermes performance regression: python_startup"
    assert row["assignee"] == "default"
    assert row["goal_mode"] == 1
    assert row["idempotency_key"].startswith("perf-regression:python_startup:")
    assert "Ownership:" in row["body"]
    assert "Task owner: default" in row["body"]
    assert "Metrics evidence citation:" in row["body"]
    assert "Source: performance_metrics.compute_baseline_and_trends" in row["body"]
    assert "Threshold: ratio >= 1.5 and delta >= 0.5s" in row["body"]


def test_below_threshold_suppresses_synthetic_task_creation(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    db_path = tmp_path / "kanban.db"
    store = tmp_path / "metrics.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    _record_series(store, "gateway_status", [2.0, 2.0, 2.1, 2.2, 2.1, 2.2])
    report = pm.compute_baseline_and_trends(
        store_path=store,
        threshold_ratio=1.5,
        min_delta_s=0.5,
        min_baseline=3,
        recent_window=3,
    )
    actions = pm.create_synthetic_regression_tasks(report, store_path=store, assignee="default")

    assert report["regression_count"] == 0
    assert actions == []
    assert not db_path.exists()
