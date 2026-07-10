from hermes_cli.model_switch import apply_model_picker_policy


def _rows():
    return [
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "models": ["claude-opus-old", "claude-opus-new", "claude-sonnet-new"],
            "total_models": 3,
        },
        {
            "slug": "openai-codex",
            "name": "OpenAI Codex",
            "models": ["gpt-old", "gpt-new"],
            "total_models": 2,
        },
    ]


def test_picker_policy_filters_and_orders_providers_and_models():
    result = apply_model_picker_policy(
        _rows(),
        {
            "providers": {
                "openai-codex": ["gpt-new", "missing"],
                "anthropic": ["claude-sonnet-new", "claude-opus-new"],
                "not-authenticated": ["anything"],
            }
        },
    )

    assert [row["slug"] for row in result] == ["openai-codex", "anthropic"]
    assert result[0]["models"] == ["gpt-new"]
    assert result[0]["total_models"] == 1
    assert result[1]["models"] == ["claude-sonnet-new", "claude-opus-new"]


def test_picker_policy_keeps_current_model_by_default():
    result = apply_model_picker_policy(
        _rows(),
        {"providers": {"anthropic": ["claude-opus-new"]}},
        current_provider="Anthropic",
        current_model="claude-opus-old",
    )

    assert result[0]["models"] == ["claude-opus-old", "claude-opus-new"]
    assert result[0]["total_models"] == 2


def test_picker_policy_can_exclude_current_model():
    result = apply_model_picker_policy(
        _rows(),
        {
            "include_current": False,
            "providers": {"anthropic": ["claude-opus-new"]},
        },
        current_provider="anthropic",
        current_model="claude-opus-old",
    )

    assert result[0]["models"] == ["claude-opus-new"]


def test_picker_policy_malformed_values_are_noop():
    rows = _rows()

    for policy in (None, [], "not-json", {"providers": []}):
        assert apply_model_picker_policy(rows, policy) is rows


def test_picker_policy_accepts_json_string_config():
    result = apply_model_picker_policy(
        _rows(),
        '{"providers":{"openai-codex":["gpt-new"]}}',
    )

    assert [row["slug"] for row in result] == ["openai-codex"]
    assert result[0]["models"] == ["gpt-new"]
