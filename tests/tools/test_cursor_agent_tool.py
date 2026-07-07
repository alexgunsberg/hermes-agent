import os

from tools import cursor_agent_tool as mod
from tools.registry import registry


def test_cursor_agent_registered():
    entry = registry.get_entry("cursor_agent")
    assert entry is not None
    assert entry.toolset == "cursor"
    assert "create" in mod.CURSOR_AGENT_SCHEMA["parameters"]["properties"]["action"]["enum"]


def test_build_repos_requires_repo_for_pr():
    try:
        mod._build_repos(pr_url="https://github.com/org/repo/pull/1")
    except mod.CursorAgentError as e:
        assert "repo_url is required" in str(e)
    else:
        raise AssertionError("expected CursorAgentError")


def test_cursor_api_key_falls_back_to_active_profile_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("CURSOR_API_KEY=profile-key\n", encoding="utf-8")

    assert mod._cursor_api_key() == "profile-key"


def test_create_payload_minimal(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"agent": {"id": "bc-1"}, "run": {"id": "run-1"}}

    monkeypatch.setattr(mod, "_request", fake_request)
    out = mod._handle_cursor_agent({"action": "create", "prompt": "do it", "model": "composer-2.5"})
    assert out["agent"]["id"] == "bc-1"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/v1/agents"
    assert calls[0][2]["prompt"] == {"text": "do it"}
    assert calls[0][2]["model"] == {"id": "composer-2.5"}
    assert "repos" not in calls[0][2]


def test_public_call_cursor_agent_delegates_to_create(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"agent": {"id": "bc-1"}, "run": {"id": "run-1"}}

    monkeypatch.setattr(mod, "_request", fake_request)
    out = mod.call_cursor_agent({"action": "create", "prompt": "do it"})
    assert out["run"]["id"] == "run-1"
    assert calls[0][0] == "POST"


def test_cancel_run_endpoint(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"id": "run-1"}

    monkeypatch.setattr(mod, "_request", fake_request)
    out = mod.call_cursor_agent({"action": "cancel_run", "agent_id": "bc-1", "run_id": "run-1"})
    assert out["id"] == "run-1"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/v1/agents/bc-1/runs/run-1/cancel"


def test_followup_payload(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"run": {"id": "run-2"}}

    monkeypatch.setattr(mod, "_request", fake_request)
    out = mod._handle_cursor_agent({"action": "followup", "agent_id": "bc-1", "prompt": "continue", "mode": "plan"})
    assert out["run"]["id"] == "run-2"
    assert calls[0][1] == "/v1/agents/bc-1/runs"
    assert calls[0][2] == {"prompt": {"text": "continue"}, "mode": "plan"}


def test_parse_sse_json_events():
    lines = [
        "event: status\n",
        'data: {"status":"RUNNING"}\n',
        "\n",
        "id: 1\n",
        "event: assistant\n",
        'data: {"text":"hi"}\n',
        "\n",
    ]
    out = mod._parse_sse(lines, max_events=10)
    assert out["events"][0]["event"] == "status"
    assert out["events"][0]["data"]["status"] == "RUNNING"
    assert out["events"][1]["id"] == "1"
    assert out["events"][1]["data"]["text"] == "hi"
