"""Regression tests for semantic session-title fallbacks."""

from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import (
    auto_title_session,
    derive_fallback_title,
    maybe_auto_title,
)
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _enable_auto_titles():
    with patch("agent.title_generator._auto_title_enabled", return_value=True):
        yield


class TestDeriveFallbackTitle:
    def test_strips_desktop_transport_metadata(self):
        message = """[Workspace::v1: /Users/alexgunsberg]
Why are some chats named so uninformatively and others not?

[Attached files: /tmp/sidebar.jpg]

<memory-context>
[System note: recalled context]
</memory-context>
"""
        assert derive_fallback_title(message) == (
            "Why are some chats named so uninformatively and others not"
        )

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Reply exactly: FABLE_OAUTH_OK", "FABLE_OAUTH_OK"),
            (
                "Reply with exactly this token and nothing else: MENTIONTEST_OK_default",
                "MENTIONTEST_OK_default",
            ),
            ("work kanban task t_a03989cd", None),
            (
                "work kanban task t_a03989cd — Repair semantic session titles",
                "Repair semantic session titles",
            ),
        ],
    )
    def test_removes_known_nonsemantic_boilerplate(self, message, expected):
        assert derive_fallback_title(message) == expected

    def test_skill_scaffold_uses_typed_invocation_not_skill_body(self):
        message = (
            '[IMPORTANT: The user has invoked the "work" skill. '
            'The full skill content is loaded below.]\n'
            "Use this skill for unrelated opening prose.\n"
            "The user has provided the following instruction alongside the skill invocation: "
            "Fix title generation fallback"
        )
        assert derive_fallback_title(message) == (
            "/work — Fix title generation fallback"
        )

    def test_strips_nested_gateway_owned_context_prefixes(self):
        message = (
            '[Replying to your previous message: "quoted text"]\n\n'
            '[Triggering message id: `123` — use as `message_id` for '
            'reply/react/pin via the discord tools.]\n\n'
            "[The user sent a document: 'report.pdf'. It is saved at: /tmp/report.pdf. "
            "Its text is not inlined here (it's a binary format such as PDF or DOCX). "
            "To read it, extract the document's text yourself — for example with the "
            "terminal tool or the ocr-and-documents skill — before answering, instead "
            "of asking the user to paste the contents.]\n\n"
            "Compare lender prediction errors"
        )
        assert derive_fallback_title(message) == "Compare lender prediction errors"

    def test_keeps_literal_reply_like_user_prose(self):
        message = '[Replying to: "literal example"] please compare both estimates'
        assert derive_fallback_title(message) == message

    def test_preserves_language_and_truncates_on_word_boundary(self):
        message = (
            "Revisa de forma independiente la reparación de títulos de sesión "
            "y confirma que nunca dependa por completo del proveedor auxiliar"
        )
        title = derive_fallback_title(message)
        assert title is not None
        assert title.startswith("Revisa de forma independiente")
        assert title.endswith("...")
        assert len(title) <= 80


    def test_removes_control_and_bidi_characters_without_damaging_urls(self):
        message = "Fix\x00 v1.2.3 at https://example.com/a?b=1\u202e now"
        assert derive_fallback_title(message) == (
            "Fix v1.2.3 at https://example.com/a?b=1 now"
        )

    def test_cjk_without_spaces_uses_character_fallback(self):
        title = derive_fallback_title("会話タイトルを改善する" * 12)
        assert title is not None
        assert len(title) == 80
        assert title.endswith("...")

    def test_never_cuts_inside_a_word_when_a_boundary_exists(self):
        assert derive_fallback_title("Fix " + "x" * 100) == "Fix..."


class TestLateUntitledRepair:
    def test_repairs_from_earliest_meaningful_request_without_llm(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True
        history = [
            {"role": "user", "content": "work kanban task t_a03989cd"},
            {"role": "assistant", "content": "started"},
            {"role": "user", "content": "Repair session title persistence"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "Verify it now"},
            {"role": "assistant", "content": "done"},
        ]
        seen = []

        with patch("agent.title_generator.auto_title_session") as generate:
            maybe_auto_title(
                db,
                "sess-1",
                "Verify it now",
                "done",
                history,
                title_callback=seen.append,
            )

        generate.assert_not_called()
        db.set_auto_title_if_empty.assert_called_once_with(
            "sess-1", "Repair session title persistence"
        )
        assert seen == ["Repair session title persistence"]

    def test_disabled_setting_prevents_late_repair(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [{"role": "user", "content": f"request {i}"} for i in range(3)]

        with patch("agent.title_generator._auto_title_enabled", return_value=False):
            maybe_auto_title(db, "sess-1", "request 2", "done", history)

        db.set_auto_title_if_empty.assert_not_called()

    def test_existing_manual_title_prevents_late_repair(self):
        db = MagicMock()
        db.get_session_title.return_value = "Manual title"
        history = [{"role": "user", "content": f"request {i}"} for i in range(3)]

        maybe_auto_title(db, "sess-1", "request 2", "done", history)

        db.set_auto_title_if_empty.assert_not_called()


class TestAutoTitleFallback:
    def test_persists_local_fallback_when_auxiliary_title_is_unavailable(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True
        seen = []

        with patch("agent.title_generator.generate_title", return_value=None):
            auto_title_session(
                db,
                "sess-1",
                "Independently review the Proton Pass secret-source repair. Focus on security.",
                "Review complete",
                title_callback=seen.append,
            )

        fallback = "Independently review the Proton Pass secret-source repair"
        db.set_auto_title_if_empty.assert_called_once_with("sess-1", fallback)
        assert seen == [fallback]

    def test_successful_fallback_suppresses_auxiliary_failure_warning(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True
        surfaced = []
        failure = RuntimeError("provider rate limited")

        def unavailable(*_args, failure_callback=None, **_kwargs):
            assert failure_callback is not None
            failure_callback("title generation", failure)
            return None

        with patch("agent.title_generator.generate_title", side_effect=unavailable):
            auto_title_session(
                db,
                "sess-1",
                "Repair semantic session titles",
                "Working on it",
                failure_callback=lambda task, exc: surfaced.append((task, exc)),
            )

        assert surfaced == []
        db.set_auto_title_if_empty.assert_called_once_with(
            "sess-1", "Repair semantic session titles"
        )

    def test_auxiliary_failure_is_surfaced_if_no_safe_fallback_exists(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        surfaced = []
        failure = RuntimeError("provider rate limited")

        def unavailable(*_args, failure_callback=None, **_kwargs):
            assert failure_callback is not None
            failure_callback("title generation", failure)
            return None

        with patch("agent.title_generator.generate_title", side_effect=unavailable):
            auto_title_session(
                db,
                "sess-1",
                "work kanban task t_a03989cd",
                "Working on it",
                failure_callback=lambda task, exc: surfaced.append((task, exc)),
            )

        assert surfaced == [("title generation", failure)]
        db.set_auto_title_if_empty.assert_not_called()

    def test_empty_model_output_falls_back_to_user_intent(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = ""

        with patch("agent.title_generator.call_llm", return_value=response):
            auto_title_session(db, "sess-1", "Fix session title reliability", "Done")

        db.set_auto_title_if_empty.assert_called_once_with(
            "sess-1", "Fix session title reliability"
        )

    def test_disabled_auto_title_skips_llm_and_fallback(self):
        db = MagicMock()
        with patch("agent.title_generator._auto_title_enabled", return_value=False), patch(
            "agent.title_generator.generate_title"
        ) as generate:
            auto_title_session(db, "sess-1", "Fix titles", "Done")

        generate.assert_not_called()
        db.get_session_title.assert_not_called()
        db.set_auto_title_if_empty.assert_not_called()

    def test_runtime_change_skips_llm_but_still_uses_local_fallback(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.return_value = True

        with patch("agent.title_generator.call_llm") as call:
            auto_title_session(
                db,
                "sess-1",
                "Keep session names informative",
                "Done",
                runtime_validator=lambda: False,
            )

        call.assert_not_called()
        db.set_auto_title_if_empty.assert_called_once_with(
            "sess-1", "Keep session names informative"
        )

    def test_fallback_respects_manual_title_race(self, tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="desktop")

        def fail_after_manual_title(*_args, **_kwargs):
            db.set_session_title("sess-1", "Manual Title")
            return None

        with patch("agent.title_generator.generate_title", side_effect=fail_after_manual_title):
            auto_title_session(db, "sess-1", "Fallback title", "Done")

        assert db.get_session_title("sess-1") == "Manual Title"

    def test_duplicate_fallback_uses_existing_lineage_dedupe(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        db.set_auto_title_if_empty.side_effect = [ValueError("in use"), True]
        db.get_next_title_in_lineage.return_value = "Repair titles #2"

        with patch("agent.title_generator.generate_title", return_value=None):
            auto_title_session(db, "sess-1", "Repair titles", "Done")

        assert db.set_auto_title_if_empty.call_args_list[-1].args == (
            "sess-1",
            "Repair titles #2",
        )
