"""_check_gateway_running must use the short-lived PID cache on the read path."""

from pathlib import Path


def test_check_gateway_running_uses_cached_pid_probe(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles_mod
    import gateway.status as status_mod

    pid_path = tmp_path / "gateway.pid"
    pid_path.write_text("1\n", encoding="utf-8")
    calls = {"cached": 0, "uncached": 0}

    def cached(path=None, *args, **kwargs):
        calls["cached"] += 1
        assert path == pid_path
        assert kwargs.get("cleanup_stale") is False
        return 1234

    def uncached(path=None, *args, **kwargs):
        calls["uncached"] += 1
        return None

    monkeypatch.setattr(status_mod, "get_running_pid_cached", cached)
    monkeypatch.setattr(status_mod, "get_running_pid", uncached)

    assert profiles_mod._check_gateway_running(tmp_path) is True
    assert calls["cached"] == 1
    assert calls["uncached"] == 0
