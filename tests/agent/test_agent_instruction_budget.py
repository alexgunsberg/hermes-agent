from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
MAX_INSTRUCTION_CHARS = 20_000


def test_root_agent_contract_and_routed_references_fit_full_read_budget():
    contract = ROOT / "AGENTS.md"
    text = contract.read_text(encoding="utf-8")
    assert len(text) <= MAX_INSTRUCTION_CHARS

    routed = re.findall(r"`(docs/agent-guide/[^`]+\.md)`", text)
    assert routed, "root contract must route task-specific instructions"
    for relative in set(routed):
        reference = ROOT / relative
        assert reference.is_file(), f"missing routed instruction: {relative}"
        assert len(reference.read_text(encoding="utf-8")) <= MAX_INSTRUCTION_CHARS, relative


def test_root_contract_keeps_cross_cutting_invariants_always_visible():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required = (
        "prompt caching is sacred",
        "core is a narrow waist",
        "config.yaml",
        "Instructional files",
        "Profiles are isolated",
        "temporary `HERMES_HOME`",
    )
    for invariant in required:
        assert invariant in text
