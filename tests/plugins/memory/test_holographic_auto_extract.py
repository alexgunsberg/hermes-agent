from plugins.memory.holographic import HolographicMemoryProvider


def _provider(tmp_path):
    provider = HolographicMemoryProvider({
        "db_path": str(tmp_path / "memory.db"),
        "auto_extract": True,
        "hrr_weight": 0,
    })
    provider.initialize("test-session")
    return provider


def test_auto_extract_captures_explicit_preferences_without_manual_tool(tmp_path):
    provider = _provider(tmp_path)
    try:
        provider.on_session_end([
            {"role": "user", "content": "I prefer concise answers. Can you fix this?"},
            {"role": "user", "content": "My default editor is Neovim."},
            {"role": "user", "content": "I never commit secrets!"},
        ])
        facts = provider._store.list_facts(category="user_pref")
        contents = {fact["content"] for fact in facts}
        assert "User prefers concise answers" in contents
        assert "User's default editor is Neovim" in contents
        assert "User never commit secrets" in contents
    finally:
        provider.shutdown()


def test_auto_extract_rejects_transient_requests_and_project_decisions(tmp_path):
    provider = _provider(tmp_path)
    try:
        provider.on_session_end([
            {"role": "user", "content": "I want you to run the tests."},
            {"role": "user", "content": "I need this fixed today."},
            {"role": "user", "content": "We decided to use PostgreSQL."},
            {"role": "assistant", "content": "I prefer verbose output."},
        ])
        assert provider._store.list_facts() == []
    finally:
        provider.shutdown()
