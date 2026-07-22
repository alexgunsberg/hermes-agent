"""Offline tests for scripts/coding_agent_bench.py.

The real delegate CLIs (grok, cursor-agent) need paid auth, so these tests
drive the harness with a stub agent: a tiny script that either solves, ignores,
or crashes on the task. That validates seeding, diff accounting, verification,
availability gating, and report rendering without any network or credentials.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.coding_agent_bench as bench


def _stub_agent(tmp_path, body):
    script = tmp_path / "solver.py"
    script.write_text(body, encoding="utf-8")
    return bench.Agent(
        name="stub",
        binary=sys.executable,
        build_cmd=lambda prompt: [sys.executable, str(script), prompt],
        auth_check=lambda: None,
        timeout=60,
    )


TASK = bench.Task(
    name="make-hello",
    prompt="Create hello.txt containing the word hi.",
    files={"seed.txt": "seed\n"},
    verify="grep -q hi hello.txt",
)


def test_default_tasks_are_well_formed():
    names = [t.name for t in bench.DEFAULT_TASKS]
    assert len(names) == len(set(names))
    for task in bench.DEFAULT_TASKS:
        assert task.prompt.strip()
        assert task.verify.strip()
        assert task.files, task.name


def test_task_from_dict_rejects_missing_keys():
    with pytest.raises(ValueError):
        bench.Task.from_dict({"name": "x", "prompt": "y", "files": {}})


def test_solver_stub_passes_and_diff_is_counted(tmp_path):
    agent = _stub_agent(
        tmp_path,
        "open('hello.txt', 'w').write('hi\\n')\n",
    )
    result = bench.run_task(agent, TASK, tmp_path / "runs")
    assert result.status == "passed"
    assert result.files_changed == 1
    assert result.lines_changed >= 1
    # The scratch repo was seeded and committed before the agent ran.
    repo = tmp_path / "runs" / "stub" / TASK.name
    assert (repo / "seed.txt").read_text() == "seed\n"


def test_noop_stub_fails_verification(tmp_path):
    agent = _stub_agent(tmp_path, "pass\n")
    result = bench.run_task(agent, TASK, tmp_path / "runs")
    assert result.status == "failed"
    assert result.files_changed == 0


def test_crashing_stub_is_agent_error(tmp_path):
    agent = _stub_agent(tmp_path, "import sys; sys.exit(3)\n")
    result = bench.run_task(agent, TASK, tmp_path / "runs")
    assert result.status == "agent-error"
    assert "exit 3" in result.detail


def test_missing_binary_reported_unavailable():
    agent = bench.Agent(
        name="ghost",
        binary="definitely-not-a-real-binary-xyz",
        build_cmd=lambda prompt: ["definitely-not-a-real-binary-xyz", prompt],
        auth_check=lambda: None,
    )
    reason = agent.availability()
    assert reason and "not found" in reason


def test_grok_auth_check(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert bench._grok_auth() is not None
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert bench._grok_auth() is None


def test_markdown_report_includes_unavailable_and_summary():
    results = [
        bench.Result("stub", TASK.name, "passed", duration_s=1.2,
                     files_changed=1, lines_changed=2),
    ]
    md = bench.render_markdown(results, {"cursor": "no auth"}, [TASK])
    assert "Unavailable agents" in md
    assert "**cursor**: no auth" in md
    assert "| stub | 1/1 |" in md
    assert "| make-hello | stub | passed |" in md


def test_cli_end_to_end_with_stub(tmp_path):
    solver = tmp_path / "solver.py"
    solver.write_text("open('hello.txt', 'w').write('hi\\n')\n", encoding="utf-8")
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps([{
        "name": TASK.name,
        "prompt": TASK.prompt,
        "files": TASK.files,
        "verify": TASK.verify,
    }]), encoding="utf-8")
    out = tmp_path / "out"
    rc = bench.main([
        "--agents", "stub",
        "--stub-cmd", f"{sys.executable} {solver} {{prompt}}",
        "--tasks", str(tasks_file),
        "--out", str(out),
    ])
    assert rc == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["unavailable"] == {}
    assert [r["status"] for r in report["results"]] == ["passed"]
    assert "Coding-agent routing benchmark" in (out / "report.md").read_text(
        encoding="utf-8")


def test_cli_rejects_unknown_agent(tmp_path):
    with pytest.raises(SystemExit):
        bench.main(["--agents", "nonexistent-agent"])


def test_headless_invocations_use_safe_flags():
    registry = bench.builtin_agents()
    grok_cmd = registry["grok"].build_cmd("do it")
    assert "--no-auto-update" in grok_cmd
    assert "--always-approve" in grok_cmd
    assert grok_cmd[grok_cmd.index("--output-format") + 1] == "json"
    cursor_cmd = registry["cursor"].build_cmd("do it")
    assert "--force" in cursor_cmd
    assert "--output-format" in cursor_cmd  # stream-json default must be overridden


def test_agents_pin_explicit_models_never_plain_defaults():
    registry = bench.builtin_agents(grok_model="grok-lean", grok_effort="high",
                                    cursor_model="composer")
    grok_cmd = registry["grok"].build_cmd("do it")
    assert grok_cmd[grok_cmd.index("-m") + 1] == "grok-lean"
    assert grok_cmd[grok_cmd.index("--reasoning-effort") + 1] == "high"
    # Bounded autonomy: turn cap + no inherited context.
    for flag in ("--max-turns", "--no-subagents", "--no-memory",
                 "--disable-web-search"):
        assert flag in grok_cmd, flag
    # Removed in grok 0.2.109 — must never be emitted.
    assert "--check" not in grok_cmd and "--best-of-n" not in grok_cmd
    cursor_cmd = registry["cursor"].build_cmd("do it")
    assert cursor_cmd[cursor_cmd.index("-m") + 1] == "composer"
    assert registry["grok"].model == "grok-lean[effort=high]"
