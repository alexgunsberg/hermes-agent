from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_plugin_secret_source_applies_during_initial_dotenv_load(tmp_path, monkeypatch):
    """Plugin SecretSources are discovered before the initial secret-source pass."""

    hermes_home = tmp_path
    plugin_dir = hermes_home / "plugins" / "fake-secret-source"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "fake-secret-source",
                "version": "0.1.0",
                "kind": "standalone",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from agent.secret_sources.base import FetchResult, SecretSource\n"
        "\n"
        "class FakeSource(SecretSource):\n"
        "    name = 'fakevault'\n"
        "    label = 'Fake Vault'\n"
        "    shape = 'mapped'\n"
        "    scheme = 'fake'\n"
        "\n"
        "    def fetch(self, cfg, home_path):\n"
        "        return FetchResult(secrets={'PLUGIN_SECRET_KEY': 'from-plugin'})\n"
        "\n"
        "def register(ctx):\n"
        "    ctx.register_secret_source(FakeSource())\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["fake-secret-source"]},
                "secrets": {"fakevault": {"enabled": True, "override_existing": True}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("PLUGIN_SECRET_KEY", raising=False)

    from agent.secret_sources import registry as secret_registry
    from hermes_cli import env_loader
    import hermes_cli.plugins as plugins_mod

    secret_registry._reset_registry_for_tests()
    env_loader._SECRET_SOURCES.clear()
    env_loader.reset_secret_source_cache()
    plugins_mod._plugin_manager = plugins_mod.PluginManager()

    env_loader.load_hermes_dotenv(hermes_home=hermes_home)

    assert os.environ["PLUGIN_SECRET_KEY"] == "from-plugin"
    assert env_loader.get_secret_source("PLUGIN_SECRET_KEY") == "fakevault"
