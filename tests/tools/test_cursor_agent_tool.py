import os

import model_tools
from tools import cursor_agent_tool as mod
from tools.registry import _check_fn_cache, _check_fn_last_good, registry


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
    assert calls[0][2]["autoCreatePR"] is False
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
    assert calls[0][2]["model"] == {"id": "composer-2.5"}
    assert calls[0][2]["autoCreatePR"] is False


def test_create_payload_preserves_explicit_model_and_pr_choice(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"agent": {"id": "bc-1"}, "run": {"id": "run-1"}}

    monkeypatch.setattr(mod, "_request", fake_request)
    mod.call_cursor_agent({
        "action": "create",
        "prompt": "do it",
        "model": "gpt-5.5",
        "auto_create_pr": True,
    })
    assert calls[0][2]["model"] == {"id": "gpt-5.5"}
    assert calls[0][2]["autoCreatePR"] is True


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


def test_cursor_api_key_uses_configured_secret_sources(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(mod, "_ENV_SOURCES_ATTEMPTED", False)
    monkeypatch.setattr(mod, "_load_env_file", lambda path: None)

    def fake_load_sources():
        os.environ["CURSOR_API_KEY"] = "cursor-from-secret-source"

    monkeypatch.setattr(mod, "_load_configured_secret_sources", fake_load_sources)

    assert mod._cursor_api_key() == "cursor-from-secret-source"


def test_configured_secret_sources_retry_after_failed_attempt(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(mod, "_ENV_SOURCES_ATTEMPTED", False)
    monkeypatch.setattr(mod, "_ENV_SOURCES_LAST_ATTEMPT", 0.0)
    monkeypatch.setattr(mod, "_ENV_SOURCES_RETRY_SECONDS", 30.0)

    import hermes_cli.env_loader as env_loader
    import hermes_cli.plugins as plugins

    now = {"value": 100.0}
    calls = []

    monkeypatch.setattr(mod.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(env_loader, "reset_secret_source_cache", lambda: None)

    def fake_load_dotenv(*, hermes_home):
        calls.append(hermes_home)
        if len(calls) == 2:
            os.environ["CURSOR_API_KEY"] = "cursor-after-retry"

    monkeypatch.setattr(env_loader, "load_hermes_dotenv", fake_load_dotenv)

    mod._load_configured_secret_sources()
    assert len(calls) == 1
    assert "CURSOR_API_KEY" not in os.environ

    now["value"] = 110.0
    mod._load_configured_secret_sources()
    assert len(calls) == 1

    now["value"] = 131.0
    mod._load_configured_secret_sources()
    assert os.environ["CURSOR_API_KEY"] == "cursor-after-retry"
    assert len(calls) == 2


def test_quiet_tool_defs_cache_refreshes_check_fn_after_ttl(monkeypatch):
    entry = registry.get_entry("cursor_agent")
    assert entry is not None
    model_tools._clear_tool_defs_cache()
    _check_fn_cache.clear()
    _check_fn_last_good.clear()

    now = {"value": 100.0}
    state = {"available": False}

    def fake_check():
        return state["available"]

    monkeypatch.setattr(entry, "check_fn", fake_check)
    monkeypatch.setattr(model_tools, "_TOOL_DEFS_CHECK_FN_REFRESH_SECONDS", 30.0)
    monkeypatch.setattr(model_tools.time, "monotonic", lambda: now["value"])

    first = model_tools.get_tool_definitions(["cursor"], quiet_mode=True)
    assert "cursor_agent" not in [t["function"]["name"] for t in first]

    state["available"] = True
    now["value"] = 110.0
    same_bucket = model_tools.get_tool_definitions(["cursor"], quiet_mode=True)
    assert "cursor_agent" not in [t["function"]["name"] for t in same_bucket]

    now["value"] = 131.0
    refreshed = model_tools.get_tool_definitions(["cursor"], quiet_mode=True)
    assert "cursor_agent" in [t["function"]["name"] for t in refreshed]

    model_tools._clear_tool_defs_cache()
    _check_fn_cache.clear()
    _check_fn_last_good.clear()


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
