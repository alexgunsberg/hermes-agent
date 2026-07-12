"""Pure tool-call guardrail primitive tests."""

import json

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
    retained_tool_result_matches,
    toolguard_suppressed_result,
)


def _retained_skill_messages(args: dict, result: str, call_id: str = "skill-call-1") -> list[dict]:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "skill_view",
                        "arguments": json.dumps(args),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "skill_view",
            "tool_call_id": call_id,
            "content": result,
        },
    ]


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)


def test_default_config_is_soft_warning_only_with_hard_stop_disabled():
    cfg = ToolCallGuardrailConfig()

    assert cfg.warnings_enabled is True
    assert cfg.hard_stop_enabled is False
    assert cfg.exact_failure_warn_after == 2
    assert cfg.same_tool_failure_warn_after == 3
    assert cfg.no_progress_warn_after == 2
    assert cfg.exact_failure_block_after == 5
    assert cfg.same_tool_failure_halt_after == 8
    assert cfg.no_progress_block_after == 5


def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2


def test_success_resets_exact_signature_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, same_tool_failure_halt_after=99)
    )
    args = {"query": "same"}

    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", args, '{"ok":true}', failed=False)

    assert controller.before_call("web_search", args).action == "allow"
    controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert controller.before_call("web_search", args).action == "allow"


def test_file_mutation_lint_error_result_is_not_a_tool_failure():
    write_result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })
    patch_result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert classify_tool_failure("write_file", write_result) == (False, "")
    assert classify_tool_failure("patch", patch_result) == (False, "")


def test_same_tool_varying_args_warns_by_default_without_halting():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(same_tool_failure_warn_after=2, same_tool_failure_halt_after=3)
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    fourth = controller.after_call("terminal", {"command": "cmd-4"}, '{"exit_code":1}', failed=True)

    assert first.action == "allow"
    assert [second.action, third.action, fourth.action] == ["warn", "warn", "warn"]
    assert {second.code, third.code, fourth.code} == {"same_tool_failure_warning"}
    assert "Do not switch to text-only replies" in second.message
    assert "keep using tools" in second.message
    assert "diagnose before retrying" in second.message
    assert "different tool" in second.message
    assert controller.halt_decision is None


def test_hard_stop_enabled_halts_same_tool_varying_args_failure_streak():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_block_after=99,
            same_tool_failure_warn_after=2,
            same_tool_failure_halt_after=3,
        )
    )

    first = controller.after_call("terminal", {"command": "cmd-1"}, '{"exit_code":1}', failed=True)
    assert first.action == "allow"
    second = controller.after_call("terminal", {"command": "cmd-2"}, '{"exit_code":1}', failed=True)
    assert second.action == "warn"
    assert second.code == "same_tool_failure_warning"
    third = controller.after_call("terminal", {"command": "cmd-3"}, '{"exit_code":1}', failed=True)
    assert third.action == "halt"
    assert third.code == "same_tool_failure_halt"
    assert third.count == 3


def test_idempotent_no_progress_repeated_result_warns_without_blocking_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    for _ in range(4):
        assert controller.before_call("read_file", args).action == "allow"
        decision = controller.after_call("read_file", args, result, failed=False)

    assert decision.action == "warn"
    assert decision.code == "idempotent_no_progress_warning"
    assert controller.before_call("read_file", args).action == "allow"
    assert controller.halt_decision is None


def test_unchanged_skill_view_result_is_suppressed_only_after_fresh_comparison():
    controller = ToolCallGuardrailController()
    args = {"name": "software-development-workflows"}
    original = json.dumps({"success": True, "content": "large instructions"})

    first = controller.after_call("skill_view", args, original, failed=False)
    assert first.action == "allow"
    assert controller.before_call("skill_view", args).action == "allow"

    repeated = controller.after_call(
        "skill_view",
        args,
        original,
        failed=False,
        retained_messages=_retained_skill_messages(args, original),
    )
    assert repeated.action == "warn"
    assert repeated.code == "unchanged_result_suppressed"

    compact = json.loads(toolguard_suppressed_result(repeated))
    assert compact["success"] is True
    assert compact["unchanged"] is True
    assert compact["guardrail"]["count"] == 2
    assert "large instructions" not in json.dumps(compact)


def test_skill_view_is_never_suppressed_without_retained_history_even_at_threshold_one():
    # warn_after=1 must not let the very first call be suppressed: the stub
    # claims the content is in retained history, so suppression is licensed
    # by the retention proof, never by the repeat threshold alone.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=1, no_progress_block_after=1)
    )
    args = {"name": "software-development-workflows"}
    result = json.dumps({"success": True, "content": "only copy"})

    first = controller.after_call("skill_view", args, result, failed=False)
    assert first.action == "allow"

    retained = controller.after_call(
        "skill_view",
        args,
        result,
        failed=False,
        retained_messages=_retained_skill_messages(args, result),
    )
    assert retained.code == "unchanged_result_suppressed"


def test_skill_view_suppression_is_a_token_bound_and_ignores_warnings_enabled():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(warnings_enabled=False, hard_stop_enabled=False)
    )
    args = {"name": "software-development-workflows"}
    result = json.dumps({"success": True, "content": "large instructions"})

    controller.after_call("skill_view", args, result, failed=False)
    repeated = controller.after_call(
        "skill_view",
        args,
        result,
        failed=False,
        retained_messages=_retained_skill_messages(args, result),
    )

    assert repeated.action == "warn"
    assert repeated.code == "unchanged_result_suppressed"


def test_varying_skill_view_content_with_same_args_warns_after_repeated_reads():
    # A skill whose inline rendering changes per call can never prove
    # retention, so suppression stays off — but the loop must still be
    # called out once the same signature keeps re-emitting full documents.
    controller = ToolCallGuardrailController()
    args = {"name": "nondeterministic-skill"}

    first = controller.after_call("skill_view", args, "render 1", failed=False)
    second = controller.after_call("skill_view", args, "render 2", failed=False)
    third = controller.after_call("skill_view", args, "render 3", failed=False)

    assert first.action == "allow"
    assert second.action == "allow"
    assert third.action == "warn"
    assert third.code == "repeated_instructional_read"
    # Warning only: execution and full content are never withheld.
    assert controller.before_call("skill_view", args).action == "allow"
    assert controller.halt_decision is None


def test_changed_skill_view_content_is_returned_and_starts_a_new_streak():
    controller = ToolCallGuardrailController()
    args = {"name": "software-development-workflows"}
    original = json.dumps({"success": True, "content": "version one"})
    changed = json.dumps({"success": True, "content": "version two"})

    controller.after_call("skill_view", args, original, failed=False)
    assert controller.after_call(
        "skill_view",
        args,
        original,
        failed=False,
        retained_messages=_retained_skill_messages(args, original),
    ).action == "warn"

    assert controller.before_call("skill_view", args).action == "allow"
    changed_decision = controller.after_call("skill_view", args, changed, failed=False)
    assert changed_decision.action == "allow"
    assert changed_decision.count == 1


def test_skill_view_hard_stop_happens_after_latest_content_is_compared():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"name": "software-development-workflows"}
    result = json.dumps({"success": True, "content": "same"})

    controller.after_call("skill_view", args, result, failed=False)
    # Unlike ordinary idempotent reads, skill_view executes again so an edited
    # SKILL.md can never be hidden behind stale guardrail state.
    assert controller.before_call("skill_view", args).action == "allow"
    halted = controller.after_call(
        "skill_view",
        args,
        result,
        failed=False,
        retained_messages=_retained_skill_messages(args, result),
    )

    assert halted.action == "halt"
    assert halted.code == "unchanged_result_halt"
    assert controller.halt_decision == halted


def test_retained_skill_result_requires_matching_call_args_id_and_full_content():
    args = {"name": "software-development-workflows"}
    result = json.dumps({"success": True, "content": "full instructions"})
    retained = _retained_skill_messages(args, result)

    assert retained_tool_result_matches(retained, "skill_view", args, result) is True
    assert retained_tool_result_matches(
        retained,
        "skill_view",
        {"name": "another-skill"},
        result,
    ) is False

    retained[1]["tool_call_id"] = "different-call"
    assert retained_tool_result_matches(retained, "skill_view", args, result) is False

    retained = _retained_skill_messages(args, result)
    retained[1]["content"] = json.dumps({"success": True, "unchanged": True})
    assert retained_tool_result_matches(retained, "skill_view", args, result) is False


def test_hard_stop_enabled_blocks_idempotent_no_progress_future_repeat():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    )
    args = {"path": "/tmp/same.txt"}
    result = "same file contents"

    assert controller.before_call("read_file", args).action == "allow"
    assert controller.after_call("read_file", args, result, failed=False).action == "allow"
    assert controller.before_call("read_file", args).action == "allow"
    warn = controller.after_call("read_file", args, result, failed=False)
    assert warn.action == "warn"
    assert warn.code == "idempotent_no_progress_warning"

    blocked = controller.before_call("read_file", args)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_reset_for_turn_clears_bounded_guardrail_state():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2, no_progress_block_after=2)
    )
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("web_search", {"query": "same"}, '{"error":"boom"}', failed=True)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)
    controller.after_call("read_file", {"path": "/tmp/x"}, "same", failed=False)

    assert controller.before_call("web_search", {"query": "same"}).action == "block"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "block"

    controller.reset_for_turn()

    assert controller.before_call("web_search", {"query": "same"}).action == "allow"
    assert controller.before_call("read_file", {"path": "/tmp/x"}).action == "allow"
