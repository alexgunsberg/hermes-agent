"""Invariants for the coding-agent delegation skills (grok / cursor / routing).

These pin the operational safety notes that keep autonomous delegation sane:
explicit output formats, write-switch discipline, auth verification, and the
npm-squatter warning for Cursor. They also keep the three skills cross-linked
so Hermes surfaces the routing layer next to the delegates.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "optional-skills" / "autonomous-ai-agents"

GROK = (AGENTS_DIR / "grok" / "SKILL.md").read_text(encoding="utf-8")
CURSOR = (AGENTS_DIR / "cursor" / "SKILL.md").read_text(encoding="utf-8")
ROUTING = (AGENTS_DIR / "coding-agent-routing" / "SKILL.md").read_text(
    encoding="utf-8")


def test_grok_documents_pipeline_maximizers():
    for flag in ("--best-of-n", "--check", "--json-schema", "--worktree",
                 "--permission-mode", "--max-turns", "--device-code"):
        assert flag in GROK, flag
    # Automation hygiene must survive edits.
    assert "--no-auto-update" in GROK
    assert "--always-approve" in GROK


def test_cursor_skill_operational_safety():
    assert "name: cursor" in CURSOR
    assert "CURSOR_API_KEY" in CURSOR
    assert "cursor-agent status" in CURSOR
    # stream-json is the print-mode default; automation must pin a format.
    assert "stream-json" in CURSOR
    assert "--output-format" in CURSOR
    # --force discipline: autonomous switch, omitted for read-only review.
    assert "--force" in CURSOR
    assert "read-only" in CURSOR.lower()
    # The npm packages named cursor-agent are NOT the official CLI.
    assert "squatter" in CURSOR.lower()


def test_routing_skill_covers_both_delegates_and_bench():
    assert "grok" in ROUTING.lower()
    assert "cursor" in ROUTING.lower()
    assert "scripts/coding_agent_bench.py" in ROUTING
    # The auth-decides-last rule keeps delegation from stalling on login.
    assert "cursor-agent status" in ROUTING
    assert "XAI_API_KEY" in ROUTING


def test_skills_are_cross_linked():
    assert "coding-agent-routing" in GROK
    assert "coding-agent-routing" in CURSOR
    for sibling in ("grok", "cursor", "codex", "claude-code"):
        assert sibling in ROUTING


def test_frontmatter_versions_present():
    for text in (GROK, CURSOR, ROUTING):
        head = text.split("---")[1]
        assert "version:" in head
        assert "license: MIT" in head
