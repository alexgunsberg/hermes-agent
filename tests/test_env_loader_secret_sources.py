"""Tests for the secret-source tracking in ``hermes_cli.env_loader``.

These cover the small public surface that lets `hermes model` / `hermes setup`
label detected credentials with their origin ("from Bitwarden") so users
don't see an unexplained "credentials ✓" line when their .env is empty.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import env_loader  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sources():
    """Each test starts with a clean source map and applied-home guard."""
    from agent import secret_scope
    from agent.secret_sources import registry
    from hermes_cli import plugins

    env_loader._SECRET_SOURCES.clear()
    env_loader.reset_secret_source_cache()
    registry._reset_registry_for_tests()
    plugins._plugin_manager = None
    secret_scope.set_multiplex_active(False)
    yield
    env_loader._SECRET_SOURCES.clear()
    env_loader.reset_secret_source_cache()
    registry._reset_registry_for_tests()
    plugins._plugin_manager = None
    secret_scope.set_multiplex_active(False)


def _write_secret_source_plugin(
    home: Path,
    *,
    name: str,
    env_var: str,
    secret: str,
    import_marker: Path | None = None,
    fetch_marker: Path | None = None,
) -> None:
    """Write and enable a real user plugin backed by a fake secret source."""
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {name}\nversion: 0.1.0\ndescription: test secret source\n",
        encoding="utf-8",
    )
    import_side_effect = (
        f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        if import_marker is not None
        else ""
    )
    fetch_side_effect = (
        f"        Path({str(fetch_marker)!r}).write_text('fetched', encoding='utf-8')\n"
        if fetch_marker is not None
        else ""
    )
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "from agent.secret_sources.base import FetchResult, SecretSource\n"
        f"{import_side_effect}"
        "\n"
        "class FakeSource(SecretSource):\n"
        f"    name = {name!r}\n"
        "    label = 'Fake Secret Source'\n"
        "    shape = 'mapped'\n"
        "\n"
        "    def fetch(self, cfg, home_path):\n"
        f"{fetch_side_effect}"
        "        result = FetchResult()\n"
        f"        result.secrets = {{{env_var!r}: {secret!r}}}\n"
        "        return result\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_secret_source(FakeSource())\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        f"  - {name}\n"
        "secrets:\n"
        "  sources:\n"
        f"  - {name}\n"
        f"  {name}:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )


def test_get_secret_source_returns_none_for_untracked_var():
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_get_secret_source_returns_label_for_tracked_var():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"


def test_format_secret_source_suffix_empty_for_untracked():
    # Credentials from .env or the shell shouldn't add noise — the
    # implicit case stays unlabeled.
    assert env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY") == ""


def test_format_secret_source_suffix_bitwarden_uses_proper_name():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_format_secret_source_suffix_generic_label_for_future_sources():
    # Future-proofing: a new secret source (e.g. "vault") should still
    # produce a sensible label without needing to edit every call site.
    env_loader._SECRET_SOURCES["OPENAI_API_KEY"] = "vault"
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from vault)"
    )


def test_format_secret_source_suffix_onepassword_uses_proper_name():
    env_loader._SECRET_SOURCES["OPENAI_API_KEY"] = "onepassword"
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_records_bitwarden_origin(tmp_path, monkeypatch):
    """End-to-end: when the Bitwarden source fetches keys, applied vars
    end up in ``_SECRET_SOURCES`` so the UI can label them."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    # Stub the fetch layer under the SecretSource adapter.
    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(
        bw_module,
        "fetch_bitwarden_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_apply_external_secret_sources_noop_when_disabled(tmp_path, monkeypatch):
    """Disabled Bitwarden config must not touch the source map."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_apply_external_secret_sources_dedupes_within_process(tmp_path, monkeypatch):
    """``load_hermes_dotenv()`` is called at module-import time from several
    hot modules (cli.py, hermes_cli/main.py, run_agent.py, ...).  The
    Bitwarden status line previously printed once per call — 3-5x per
    startup.  The applied-home guard must short-circuit subsequent calls
    so the heavy work (config re-parse, Bitwarden lookup, status print)
    runs exactly once per HERMES_HOME per process.
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    call_count = {"n": 0}

    def _fake_fetch(**_kwargs):
        call_count["n"] += 1
        return {"ANTHROPIC_API_KEY": "sk-ant-test"}, []

    import agent.secret_sources.bitwarden as bw_module
    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(bw_module, "fetch_bitwarden_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    # Five calls in a row, simulating module-import-time invocations from
    # cli.py, hermes_cli/main.py, run_agent.py, trajectory_compressor.py,
    # gateway/run.py.  Only the first should actually call the backend.
    for _ in range(5):
        env_loader._apply_external_secret_sources(tmp_path)

    assert call_count["n"] == 1, (
        "Bitwarden backend was called {} time(s); expected exactly 1 — "
        "the applied-home guard is broken.".format(call_count["n"])
    )

    # Source tracking still works after dedup.
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"

    # reset_secret_source_cache() forces a fresh pull on the next call.
    env_loader.reset_secret_source_cache()
    env_loader._apply_external_secret_sources(tmp_path)
    assert call_count["n"] == 2


def test_apply_external_secret_sources_records_onepassword_origin(tmp_path, monkeypatch):
    """When the 1Password source resolves refs, applied vars end up in
    ``_SECRET_SOURCES`` labeled ``onepassword``."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    env:\n"
        "      ANTHROPIC_API_KEY: 'op://Private/Anthropic/credential'\n",
        encoding="utf-8",
    )

    import agent.secret_sources.onepassword as op_module

    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(
        op_module,
        "fetch_onepassword_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "onepassword"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_survives_non_dict_section(tmp_path, monkeypatch):
    """A malformed `secrets:` section must not abort startup (fail-open).

    Both `onepassword: true` (non-dict) and a bad bitwarden section must be
    coerced to empty config instead of raising AttributeError up through
    load_hermes_dotenv().
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden: true\n"
        "  onepassword: true\n",
        encoding="utf-8",
    )

    # Must not raise and must not record anything.
    env_loader._apply_external_secret_sources(tmp_path)
    assert env_loader.get_secret_source("ANYTHING") is None


def test_apply_external_secret_sources_bad_ttl_does_not_crash(tmp_path, monkeypatch):
    """A non-numeric cache_ttl_seconds must be coerced, not crash startup."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    cache_ttl_seconds: not-a-number\n"
        "    env:\n"
        "      K: 'op://V/I/F'\n",
        encoding="utf-8",
    )

    captured = {}

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}, []

    import agent.secret_sources.onepassword as op_module
    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(op_module, "fetch_onepassword_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    # Coerced to the 300s default rather than raising ValueError.
    assert captured["cache_ttl_seconds"] == 300


def test_config_requests_plugin_secret_sources_detects_non_bundled_name():
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry

    assert env_loader._config_requests_plugin_secret_sources(
        {"sources": ["protonpass"], "protonpass": {"enabled": True}}
    )
    assert env_loader._config_requests_plugin_secret_sources(
        {"myvault": {"enabled": True}}
    )
    assert not env_loader._config_requests_plugin_secret_sources(
        {"sources": ["bitwarden"], "bitwarden": {"enabled": True}}
    )
    assert not env_loader._config_requests_plugin_secret_sources({})
    assert not env_loader._config_requests_plugin_secret_sources(
        {"bitwarden": {"enabled": False}, "onepassword": {"enabled": False}}
    )

    class _AlreadyKnownSource(SecretSource):
        name = "alreadyknown"
        shape = "mapped"

        def fetch(self, cfg, home_path):
            return FetchResult()

    registry.register_source(_AlreadyKnownSource())
    assert not env_loader._config_requests_plugin_secret_sources(
        {"sources": ["alreadyknown"], "alreadyknown": {"enabled": True}}
    )


def test_apply_discovers_plugins_for_non_bundled_source(tmp_path, monkeypatch):
    """Plugin backends named in secrets.sources must be discovered before apply.

    Without this, first-process env load warns "unknown source" and skips the
    plugin backend entirely because discover_plugins() usually runs later.
    """
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry as reg

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("PLUGIN_TEST_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  sources:\n"
        "  - plugintest\n"
        "  plugintest:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    class _PluginTestSource(SecretSource):
        name = "plugintest"
        label = "Plugin Test"
        shape = "mapped"

        def fetch(self, cfg, home_path):
            del cfg, home_path
            result = FetchResult()
            result.secrets = {"PLUGIN_TEST_API_KEY": "from-plugin"}
            return result

    discovered = {"count": 0}

    def _fake_discover():
        discovered["count"] += 1
        reg.register_source(_PluginTestSource(), replace=True)

    reg._reset_registry_for_tests()
    monkeypatch.setattr(
        "hermes_cli.plugins.discover_plugins",
        _fake_discover,
        raising=False,
    )
    # Ensure import path used by env_loader resolves to our fake.
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "discover_plugins", _fake_discover)

    env_loader.reset_secret_source_cache()
    env_loader._apply_external_secret_sources(tmp_path)

    assert discovered["count"] == 1
    assert env_loader.get_secret_source("PLUGIN_TEST_API_KEY") == "plugintest"
    import os

    assert os.environ.get("PLUGIN_TEST_API_KEY") == "from-plugin"


def test_dotenv_bootstrap_can_defer_plugin_fetch_until_consumer(
    tmp_path, monkeypatch, capsys
):
    """Administrative startup must not pay remote plugin fetch latency.

    A later consumer must still discover the source and resolve its mapped
    credential before reading it.
    """
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry as reg

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("DEFERRED_TEST_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  sources:\n"
        "  - deferredtest\n"
        "  deferredtest:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    class _DeferredSource(SecretSource):
        name = "deferredtest"
        label = "Deferred Test"
        shape = "mapped"

        def fetch(self, cfg, home_path):
            del cfg, home_path
            result = FetchResult()
            result.secrets = {"DEFERRED_TEST_API_KEY": "resolved-at-consumer"}
            return result

    discovered = {"count": 0}

    def _fake_discover():
        discovered["count"] += 1
        reg.register_source(_DeferredSource(), replace=True)

    import hermes_cli.plugins as plugins_mod

    reg._reset_registry_for_tests()
    monkeypatch.setattr(plugins_mod, "discover_plugins", _fake_discover)

    env_loader.load_hermes_dotenv(
        hermes_home=tmp_path,
        resolve_external_secrets=False,
    )

    assert discovered["count"] == 0
    assert "DEFERRED_TEST_API_KEY" not in os.environ
    assert str(tmp_path.resolve()) not in env_loader._APPLIED_HOMES

    env_loader.ensure_external_secret_sources_loaded(hermes_home=tmp_path)

    assert discovered["count"] == 1
    assert os.environ["DEFERRED_TEST_API_KEY"] == "resolved-at-consumer"
    assert env_loader.get_secret_source("DEFERRED_TEST_API_KEY") == "deferredtest"
    captured = capsys.readouterr()
    assert "resolved-at-consumer" not in captured.out
    assert "resolved-at-consumer" not in captured.err


def test_apply_skips_plugin_discovery_for_bundled_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  sources:\n"
        "  - bitwarden\n"
        "  bitwarden:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    calls = {"count": 0}

    def _fake_discover():
        calls["count"] += 1

    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "discover_plugins", _fake_discover)
    env_loader.reset_secret_source_cache()
    env_loader._apply_external_secret_sources(tmp_path)
    assert calls["count"] == 0


def test_default_home_resolution_honors_context_local_profile(
    tmp_path, monkeypatch
):
    """An omitted home must follow the task-local profile, not os.environ."""
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    env_home = tmp_path / "process-home"
    profile_home = tmp_path / "profile-home"
    env_home.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(env_home))
    monkeypatch.delenv("CONTEXT_PROFILE_API_KEY", raising=False)
    monkeypatch.delenv("CONTEXT_PROFILE_DOTENV_TOKEN", raising=False)
    (profile_home / ".env").write_text(
        "CONTEXT_PROFILE_DOTENV_TOKEN=profile-dotenv\n", encoding="utf-8"
    )
    (profile_home / "config.yaml").write_text(
        "secrets:\n"
        "  sources: [contextsource]\n"
        "  contextsource:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    seen_homes: list[Path] = []

    class _ContextSource(SecretSource):
        name = "contextsource"
        shape = "mapped"

        def fetch(self, cfg, home_path):
            seen_homes.append(home_path)
            result = FetchResult()
            result.secrets = {"CONTEXT_PROFILE_API_KEY": "profile-secret"}
            return result

    registry.register_source(_ContextSource())
    token = set_hermes_home_override(profile_home)
    try:
        loaded = env_loader.load_hermes_dotenv(resolve_external_secrets=False)
        env_loader.ensure_external_secret_sources_loaded()
    finally:
        reset_hermes_home_override(token)

    assert loaded == [profile_home / ".env"]
    assert os.environ["CONTEXT_PROFILE_DOTENV_TOKEN"] == "profile-dotenv"
    assert seen_homes == [profile_home]
    assert os.environ["CONTEXT_PROFILE_API_KEY"] == "profile-secret"
    assert str(profile_home.resolve()) in env_loader._APPLIED_HOMES
    assert str(env_home.resolve()) not in env_loader._APPLIED_HOMES


def test_safe_mode_never_imports_discovers_fetches_or_warns_for_plugin_source(
    tmp_path, monkeypatch, caplog
):
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "safe-home"
    import_marker = tmp_path / "safe-imported"
    fetch_marker = tmp_path / "safe-fetched"
    _write_secret_source_plugin(
        home,
        name="safevault",
        env_var="SAFE_VAULT_API_KEY",
        secret="safe-mode-must-not-read-this",
        import_marker=import_marker,
        fetch_marker=fetch_marker,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SAFE_MODE", "1")
    monkeypatch.delenv("SAFE_VAULT_API_KEY", raising=False)
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    caplog.set_level(logging.WARNING)

    env_loader.ensure_external_secret_sources_loaded()

    assert not import_marker.exists()
    assert not fetch_marker.exists()
    assert "SAFE_VAULT_API_KEY" not in os.environ
    assert "unknown source" not in caplog.text.lower()
    assert str(home.resolve()) not in env_loader._APPLIED_HOMES


def test_multiplex_mode_does_not_apply_process_global_plugin_secrets(
    tmp_path, monkeypatch, caplog
):
    from agent import secret_scope
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "multiplex-home"
    import_marker = tmp_path / "multiplex-imported"
    fetch_marker = tmp_path / "multiplex-fetched"
    _write_secret_source_plugin(
        home,
        name="multiplexvault",
        env_var="MULTIPLEX_VAULT_API_KEY",
        secret="cross-profile-secret",
        import_marker=import_marker,
        fetch_marker=fetch_marker,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MULTIPLEX_VAULT_API_KEY", "preexisting-global")
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    secret_scope.set_multiplex_active(True)
    caplog.set_level(logging.WARNING)

    env_loader.ensure_external_secret_sources_loaded()

    assert not import_marker.exists()
    assert not fetch_marker.exists()
    assert os.environ["MULTIPLEX_VAULT_API_KEY"] == "preexisting-global"
    assert "unknown source" not in caplog.text.lower()
    assert str(home.resolve()) not in env_loader._APPLIED_HOMES


def test_real_temp_plugin_first_consumer_registers_and_applies_without_warning(
    tmp_path, monkeypatch, caplog, capsys
):
    """Real scanner/loader E2E for the first secret-consuming call."""
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "plugin-home"
    empty_bundled = tmp_path / "empty-bundled"
    empty_bundled.mkdir()
    secret = "fake-secret-value-that-must-not-be-printed"
    _write_secret_source_plugin(
        home,
        name="e2evault",
        env_var="E2E_VAULT_API_KEY",
        secret=secret,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("E2E_VAULT_API_KEY", raising=False)
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    monkeypatch.setattr(
        plugins_mod.PluginManager, "_scan_entry_points", lambda self: []
    )
    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    caplog.set_level(logging.WARNING)

    env_loader.ensure_external_secret_sources_loaded()

    assert os.environ["E2E_VAULT_API_KEY"] == secret
    assert env_loader.get_secret_source("E2E_VAULT_API_KEY") == "e2evault"
    assert plugins_mod.get_plugin_manager()._plugins["e2evault"].enabled is True
    assert "unknown source" not in caplog.text.lower()
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in caplog.text


def test_plugin_discovery_reentrancy_keeps_outer_apply_authoritative(
    tmp_path, monkeypatch, caplog
):
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry
    from hermes_cli import plugins as plugins_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("REENTRANT_VAULT_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  sources: [reentrantvault]\n"
        "  reentrantvault:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    calls = {"discover": 0, "fetch": 0}

    class _ReentrantSource(SecretSource):
        name = "reentrantvault"
        shape = "mapped"

        def fetch(self, cfg, home_path):
            calls["fetch"] += 1
            result = FetchResult()
            result.secrets = {"REENTRANT_VAULT_API_KEY": "reentrant-secret"}
            return result

    def _reentrant_discover():
        calls["discover"] += 1
        env_loader.ensure_external_secret_sources_loaded(hermes_home=tmp_path)
        registry.register_source(_ReentrantSource())

    monkeypatch.setattr(plugins_mod, "discover_plugins", _reentrant_discover)
    caplog.set_level(logging.WARNING)

    env_loader.ensure_external_secret_sources_loaded(hermes_home=tmp_path)

    assert calls == {"discover": 1, "fetch": 1}
    assert os.environ["REENTRANT_VAULT_API_KEY"] == "reentrant-secret"
    assert "unknown source" not in caplog.text.lower()
    assert not env_loader._APPLYING_HOMES


def test_administrative_config_command_does_not_import_or_fetch_plugin_source(
    tmp_path
):
    home = tmp_path / "admin-home"
    import_marker = tmp_path / "admin-imported"
    fetch_marker = tmp_path / "admin-fetched"
    secret = "admin-command-must-not-print-or-fetch-this"
    _write_secret_source_plugin(
        home,
        name="adminvault",
        env_var="ADMIN_VAULT_API_KEY",
        secret=secret,
        import_marker=import_marker,
        fetch_marker=fetch_marker,
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env.pop("HERMES_SAFE_MODE", None)
    env.pop("ADMIN_VAULT_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "config", "path"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert not import_marker.exists()
    assert not fetch_marker.exists()
    assert secret not in proc.stdout
    assert secret not in proc.stderr
