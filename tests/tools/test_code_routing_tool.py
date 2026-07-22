"""Tests for the route_code_task Hermes tool registration."""

import json

import pytest

import tools.code_routing_tool as tool_mod
from tools import code_routing
from tools.registry import registry


def test_route_code_task_is_registered_in_delegation_toolset():
    entry = registry.get_entry("route_code_task")
    assert entry is not None
    assert entry.toolset == "delegation"
    props = entry.schema["parameters"]["properties"]
    for key in ("name", "prompt", "verify", "repo", "base_sha",
                "allowed_paths", "task_class", "risk", "protected_files"):
        assert key in props, key
    assert entry.schema["parameters"]["required"] == ["name", "prompt", "verify"]


def test_handler_rejects_invalid_packet_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    out = json.loads(tool_mod._route_code_task_handler(
        {"name": "x", "prompt": "y", "verify": ["true"],
         "files": {"../escape.txt": "boom"}}))
    assert "invalid packet" in out["error"]
    out = json.loads(tool_mod._route_code_task_handler({"name": "x"}))
    assert "invalid packet" in out["error"]


def test_handler_routes_and_logs_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    solver = tmp_path / "solver.py"
    solver.write_text("open('hello.txt', 'w').write('hi\\n')\n",
                      encoding="utf-8")
    import sys
    stub = code_routing.stub_profile(
        "stub", f"{sys.executable} {solver} {{prompt}}")
    monkeypatch.setattr(tool_mod.code_routing, "builtin_profiles",
                        lambda: {"stub": stub})
    monkeypatch.setitem(code_routing.ROUTING_TABLE, "default",
                        ["stub", "stub"])
    out = json.loads(tool_mod._route_code_task_handler({
        "name": "hello", "prompt": "make hello.txt say hi",
        "verify": ["grep -q hi hello.txt"],
    }))
    assert out["accepted"] is True
    log = tmp_path / "router" / "routes.jsonl"
    assert log.exists()
    records = [json.loads(l) for l in log.read_text().splitlines()]
    assert records[-1]["type"] == "summary"
    assert out["log_path"] == str(log)


def test_check_fn_requires_a_delegate_binary(monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda b: None)
    assert tool_mod.check_code_routing_requirements() is False
    monkeypatch.setattr(_shutil, "which",
                        lambda b: "/usr/bin/grok" if b == "grok" else None)
    assert tool_mod.check_code_routing_requirements() is True
