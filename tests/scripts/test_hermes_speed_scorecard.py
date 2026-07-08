from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hermes_speed_scorecard.py"


def _load_scorecard_module():
    spec = importlib.util.spec_from_file_location("hermes_speed_scorecard", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_scorecard_counts_without_leaking_cron_or_log_contents(tmp_path, monkeypatch):
    scorecard = _load_scorecard_module()
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        scorecard,
        "collect_tool_schema_metrics",
        lambda **_: {
            "current_environment_has_kanban_task": False,
            "normal_chat_baseline": {"count": 3, "schema_bytes": 120, "approx_schema_tokens": 30},
        },
    )

    (hermes_home / "cron").mkdir(parents=True)
    (hermes_home / "logs").mkdir(parents=True)
    (hermes_home / "memory_store.db").write_bytes(b"memory-bytes")
    (hermes_home / "logs" / "agent.log").write_text(
        "2026-01-01 Preflight compression: SECRET_LOG_TEXT\n",
        encoding="utf-8",
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": {
                    "a": {"enabled": True, "prompt": "SECRET_CRON_PROMPT"},
                    "b": {"enabled": False, "prompt": "another hidden prompt"},
                }
            }
        ),
        encoding="utf-8",
    )
    pmb_packet = tmp_path / "pmb_packet.md"
    pmb_packet.write_text("SECRET_PMB_CONTENT", encoding="utf-8")

    report = scorecard.collect_scorecard(run_commands=False, pmb_paths=[pmb_packet])

    assert report["cron_jobs"]["total"] == 2
    assert report["cron_jobs"]["enabled"] == 1
    assert report["cron_jobs"]["disabled"] == 1
    assert report["active_memory"]["total_bytes"] == len(b"memory-bytes")
    assert report["pmb_packet"]["total_bytes"] == len("SECRET_PMB_CONTENT")
    assert report["compression_warnings"]["total_matches"] >= 1

    serialized = json.dumps(report, sort_keys=True)
    assert "SECRET_CRON_PROMPT" not in serialized
    assert "SECRET_LOG_TEXT" not in serialized
    assert "SECRET_PMB_CONTENT" not in serialized


def test_collect_scorecard_reports_skill_frequency_and_route_bloat_without_leaking_text(tmp_path, monkeypatch):
    scorecard = _load_scorecard_module()
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(scorecard, "collect_process_timer_metrics", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        scorecard,
        "collect_tool_schema_metrics",
        lambda **_: {
            "normal_chat_baseline": {
                "count": 1,
                "schema_bytes": 40,
                "approx_schema_tokens": 10,
                "top_heavy_tools": [{"name": "safe_tool", "schema_bytes": 40}],
            }
        },
    )

    skill_dir = hermes_home / "skills" / "devops" / "large-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Large\n" + ("x" * 200_000), encoding="utf-8")

    hermes_home.mkdir(exist_ok=True)
    conn = sqlite3.connect(hermes_home / "state.db")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            system_prompt TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER,
            tool_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            archived INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            content TEXT
        );
        CREATE TABLE gateway_routing (
            scope TEXT,
            session_key TEXT,
            entry_json TEXT,
            updated_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "s_active",
            "telegram",
            '[IMPORTANT: The user launched this CLI session with the "devops/large-skill" skill preloaded.] SECRET_PROMPT_TEXT',
            2_000_000_000,
            None,
            None,
            2,
            0,
            123,
            45,
            0,
            0,
            0,
        ),
    )
    conn.execute(
        "INSERT INTO messages (session_id, content) VALUES (?, ?)",
        ("s_active", 'SECRET_MESSAGE_TEXT [IMPORTANT: The user has invoked the "devops/large-skill" skill.]'),
    )
    conn.execute(
        "INSERT INTO gateway_routing VALUES (?, ?, ?, ?)",
        (
            "scope",
            "SECRET_ROUTE_KEY",
            json.dumps({"session_id": "s_active", "last_prompt_tokens": 120000, "total_tokens": 9, "suspended": True}),
            300,
        ),
    )
    conn.commit()
    conn.close()

    report = scorecard.collect_scorecard(run_commands=False)

    assert any(item["name"] == "devops/large-skill" for item in report["skills"]["top_prompt_heavy_skills"])
    assert report["skills"]["loaded_frequency_top"][0] == {"name": "devops/large-skill", "count": 2}
    assert report["session_routes"]["active_session_count"] == 1
    assert report["session_routes"]["high_prompt_route_pins_100k"] == 1
    assert report["session_routes"]["suspended_route_pins"] == 1

    serialized = json.dumps(report, sort_keys=True)
    assert "SECRET_PROMPT_TEXT" not in serialized
    assert "SECRET_MESSAGE_TEXT" not in serialized
    assert "SECRET_ROUTE_KEY" not in serialized


def test_collect_process_timer_metrics_reports_measured_bottlenecks_and_owner_gaps(tmp_path, monkeypatch):
    scorecard = _load_scorecard_module()
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT,
            created_at INTEGER,
            started_at INTEGER,
            completed_at INTEGER,
            last_heartbeat_at INTEGER,
            result TEXT,
            goal_mode INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            status TEXT,
            started_at INTEGER,
            ended_at INTEGER,
            outcome TEXT
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            body TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_ready",
            "implement SECRET_TITLE",
            "Ownership:\n- Task owner: default\nHandoff: SECRET_BODY next_action: review evidence: diff",
            "default",
            "ready",
            100,
            None,
            None,
            None,
            None,
            0,
        ),
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_blocked",
            "review work",
            "Ownership:\n- Task owner: default\n- Project owner: p\n- Kanban owner: k\n- Stage owner: reviewer\n- Escalation target: Alex",
            "reviewer",
            "blocked",
            50,
            60,
            None,
            None,
            "review-required: SECRET_RESULT",
            1,
        ),
    )
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)", ("t_ready", "promoted", None, 200))
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        ("t_blocked", "blocked", json.dumps({"reason": "review-required: SECRET_PAYLOAD"}), 300),
    )
    conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)", ("t_blocked", "blocked", 60, 180, "blocked"))
    conn.execute("INSERT INTO task_comments (task_id, body) VALUES (?, ?)", ("t_ready", "handoff SECRET_COMMENT handoff_from a handoff_to b"))
    conn.commit()
    conn.close()

    report = scorecard.collect_process_timer_metrics(hermes_home, now_ts=500)

    assert report["active_tasks"] == 2
    assert report["timers"]["ready_queue_age"]["total_seconds"] == 300
    assert report["timers"]["blocked_age"]["total_seconds"] == 200
    assert report["timers"]["review_required_age"]["observation_count"] == 1
    assert report["ownership_gaps"]["tasks_with_missing_owner_fields"] == 1
    assert "project_owner" in report["ownership_gaps"]["missing_by_field"]
    assert report["ownership_gaps"]["non_goal_work_cards"] == 1
    assert report["ownership_gaps"]["handoffs_with_missing_contract_fields"] >= 1
    assert report["top_bottlenecks"][0]["name"] == "ready_queue_age"

    serialized = json.dumps(report, sort_keys=True)
    assert "SECRET_TITLE" not in serialized
    assert "SECRET_BODY" not in serialized
    assert "SECRET_RESULT" not in serialized
    assert "SECRET_PAYLOAD" not in serialized
    assert "SECRET_COMMENT" not in serialized


def test_write_reports_creates_timestamped_and_latest_files(tmp_path):
    scorecard = _load_scorecard_module()
    report = {
        "generated_at": "2026-01-01T00:00:00Z",
        "repo_root": "/repo",
        "hermes_home": "/home/.hermes",
        "scorecard_runtime_s": 0.1,
        "timings": {"skipped": True, "reason": "test"},
        "tool_schemas": {
            "normal_chat_baseline": {"count": 2, "schema_bytes": 80, "approx_schema_tokens": 20}
        },
        "active_memory": {"human_total": "10 B", "file_count": 1},
        "pmb_packet": {"human_total": "20 B", "file_count": 1},
        "compression_warnings": {"total_matches": 0},
        "cron_jobs": {"total": 1, "enabled": 1, "disabled": 0},
    }

    json_path, md_path = scorecard.write_reports(report, tmp_path, "score")

    assert json_path.exists()
    assert md_path.exists()
    assert (tmp_path / "score_latest.json").exists()
    assert (tmp_path / "score_latest.md").exists()
    assert json.loads(json_path.read_text(encoding="utf-8"))["generated_at"] == report["generated_at"]
    markdown = md_path.read_text(encoding="utf-8")
    assert "Hermes speed scorecard" in markdown
    assert "normal_chat_tool_schema_count" in markdown
