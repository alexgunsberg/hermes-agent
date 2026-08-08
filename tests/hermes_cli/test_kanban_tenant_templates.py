"""Tests for SL local-first Kanban body templates.

Proves:
- Suomen Liittokunta implementation cards receive the five local-first steps
- Unrelated tenants are unchanged
- Pure review cards are not polluted with shipping steps
- create_task / specify_triage_task / decompose_triage_task apply the template
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_tenant_templates as tpl


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_sl_scope_aliases():
    assert tpl.is_suomen_liittokunta_scope(tenant="suomenliittokunta")
    assert tpl.is_suomen_liittokunta_scope(tenant="Suomen-Liittokunta")
    assert tpl.is_suomen_liittokunta_scope(tenant="sl")
    assert tpl.is_suomen_liittokunta_scope(project_slug="suomenliittokunta")
    assert not tpl.is_suomen_liittokunta_scope(tenant="other-tenant")
    assert not tpl.is_suomen_liittokunta_scope(tenant=None, project_slug=None)


def test_apply_includes_five_steps_for_sl_implementation():
    body = (
        "**Goal** — ship the pension nav fix.\n"
        "**Approach** — edit registry + tests.\n"
        "**Acceptance criteria** — build passes.\n"
    )
    out = tpl.apply_local_first_checklist(
        body,
        tenant="suomenliittokunta",
        title="Implement pension registry navigation",
    )
    assert out is not None
    assert tpl.LOCAL_FIRST_SECTION_MARKER in out
    assert "draft PR while iterating" in out
    assert "npm run gates" in out
    assert "Mark ready once" in out
    assert "npm run review:request -- --pr <n> --wait-gates" in out
    assert "Never ask Alex for routine review" in out
    # Original content preserved
    assert "pension nav fix" in out


def test_apply_skips_unrelated_tenant():
    body = "Implement something for another product."
    out = tpl.apply_local_first_checklist(
        body,
        tenant="acme-corp",
        title="Implement feature X",
    )
    assert out == body
    assert tpl.LOCAL_FIRST_SECTION_MARKER not in (out or "")


def test_apply_skips_when_no_tenant():
    body = "Implement SL-looking work without tenant."
    out = tpl.apply_local_first_checklist(
        body,
        tenant=None,
        title="Implement feature",
    )
    assert out == body


def test_apply_skips_review_only_cards():
    title = "Read-only exact-head review: PR #171"
    body = (
        "Labels: review:exact-head, review:read-only\n"
        "Read-only gate. Do not edit, commit, push.\n"
        "Reviewer mutation audit required.\n"
    )
    out = tpl.apply_local_first_checklist(
        body,
        tenant="suomenliittokunta",
        title=title,
    )
    assert out == body
    assert "npm run gates" not in (out or "")


def test_apply_is_idempotent():
    body = "Implement the landing page fix."
    once = tpl.apply_local_first_checklist(
        body, tenant="suomenliittokunta", title="Implement landing page"
    )
    twice = tpl.apply_local_first_checklist(
        once, tenant="suomenliittokunta", title="Implement landing page"
    )
    assert once == twice
    assert once.count(tpl.LOCAL_FIRST_SECTION_MARKER) == 1


def test_prompt_addendum_only_for_sl():
    addendum = tpl.local_first_prompt_addendum(tenant="suomenliittokunta")
    assert "local-first" in addendum.casefold()
    assert "never ask alex" in addendum.casefold()
    assert "npm run gates" in addendum
    assert tpl.local_first_prompt_addendum(tenant="other") == ""
    assert tpl.local_first_prompt_addendum(tenant=None) == ""


def test_project_slug_scope_without_tenant():
    out = tpl.apply_local_first_checklist(
        "Fix the route copy.",
        tenant=None,
        project_slug="suomen-liittokunta",
        title="Fix digital euro framing",
    )
    assert tpl.LOCAL_FIRST_SECTION_MARKER in (out or "")


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


def test_create_task_injects_for_sl_tenant(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Implement pension family navigation",
            body="**Goal** — derive nav from registry.",
            assignee="websites",
            tenant="suomenliittokunta",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.body is not None
    assert tpl.LOCAL_FIRST_SECTION_MARKER in task.body
    assert "npm run review:request -- --pr <n> --wait-gates" in task.body
    assert "Never ask Alex for routine review" in task.body


def test_create_task_skips_unrelated_tenant(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Implement feature X",
            body="Ship it.",
            assignee="websites",
            tenant="unrelated-product",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.body == "Ship it."
    assert tpl.LOCAL_FIRST_SECTION_MARKER not in (task.body or "")


def test_create_task_skips_sl_review_card(kanban_home):
    body = (
        "Labels: review:exact-head, review:read-only\n"
        "Read-only gate. Do not edit, commit, push.\n"
        "Reviewer mutation audit: required.\n"
    )
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Review: pension navigation PR",
            body=body,
            assignee="websites",
            tenant="suomenliittokunta",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.body == body
    assert "npm run gates" not in (task.body or "")


def test_specify_triage_injects_for_sl(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Pension nav",
            body="rough idea",
            assignee="websites",
            tenant="suomenliittokunta",
            triage=True,
        )
        # create_task already injects for triage implementation-ish titles;
        # clear body back to a pre-template state to isolate specify path.
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            ("rough idea only", tid),
        )
        conn.commit()
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Implement pension registry navigation",
            body=(
                "**Goal** — derive nav from registry.\n"
                "**Approach** — edit site registry.\n"
                "**Acceptance criteria** — typecheck passes.\n"
            ),
            author="specifier",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify promotes triage -> todo; recompute_ready may flip parent-free
    # todos to ready immediately.
    assert task.status in {"todo", "ready"}
    assert tpl.LOCAL_FIRST_SECTION_MARKER in (task.body or "")
    assert "draft PR while iterating" in (task.body or "")


def test_decompose_children_inject_for_sl(kanban_home):
    with kb.connect_closing() as conn:
        root = kb.create_task(
            conn,
            title="Ship pension nav batch",
            body="umbrella",
            assignee="websites",
            tenant="suomenliittokunta",
            triage=True,
        )
        # Avoid root body injection noise for this assert path.
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("umbrella", root))
        conn.commit()
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            children=[
                {
                    "title": "Implement registry navigation",
                    "body": "**Goal** — derive nav.\n**Acceptance criteria** — tests pass.",
                    "assignee": "websites",
                    "parents": [],
                },
                {
                    "title": "Review: registry navigation",
                    "body": (
                        "Labels: review:exact-head\n"
                        "Read-only gate. Do not edit, commit, push.\n"
                        "Reviewer mutation audit required.\n"
                    ),
                    "assignee": "websites",
                    "parents": [0],
                },
            ],
            root_assignee="websites",
            author="decomposer",
            auto_promote=False,
        )
        assert len(child_ids) == 2
        impl = kb.get_task(conn, child_ids[0])
        rev = kb.get_task(conn, child_ids[1])
    assert impl is not None and rev is not None
    assert tpl.LOCAL_FIRST_SECTION_MARKER in (impl.body or "")
    assert "npm run gates" in (impl.body or "")
    assert tpl.LOCAL_FIRST_SECTION_MARKER not in (rev.body or "")
    assert "npm run gates" not in (rev.body or "")
