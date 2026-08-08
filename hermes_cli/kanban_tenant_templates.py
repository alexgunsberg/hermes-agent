"""Tenant/project-scoped Kanban body templates.

Small pure helpers used when creating or specifying tasks so certain
tenants get deterministic checklist text in newly generated card bodies
without changing generic board behavior.

Currently ships one template: Suomen Liittokunta (SL) local-first delivery
steps for implementation cards. Keep this file dependency-light so unit
tests can exercise it without a live board DB.
"""

from __future__ import annotations

import re
from typing import Optional

# Stable section marker — also used for idempotent re-injection.
LOCAL_FIRST_SECTION_MARKER = "## Local-first delivery (Suomen Liittokunta)"

# Exact five steps required by the SL local-first Kanban policy.
LOCAL_FIRST_CHECKLIST = """\
## Local-first delivery (Suomen Liittokunta)

Do these on every implementation card (do not skip or replace with Alex ping):

1. Open/update a **draft PR while iterating** (do not wait for a perfect final commit first).
2. Run `npm run gates` on the **final SHA** before requesting review.
3. **Mark ready once** when the work is complete and evidence is attached — routine review is a separate child card, not a block on this one.
4. Request independent review with `npm run review:request -- --pr <n> --wait-gates`.
5. **Never ask Alex for routine review.** Independent exact-head review is the gate; Alex is not in the loop.
"""

# Tenant / project identifiers that opt into the SL template.
_SL_TENANT_ALIASES = frozenset(
    {
        "suomenliittokunta",
        "suomen-liittokunta",
        "suomen_liittokunta",
        "sl",
    }
)

_SL_PROJECT_ALIASES = frozenset(
    {
        "suomenliittokunta",
        "suomen-liittokunta",
        "suomen_liittokunta",
        "suomenliittokunta-2026",
        "sl",
    }
)

# Cards that are review/gate/meta only — never inject local-first shipping steps.
_REVIEW_ONLY_TITLE = re.compile(
    r"(?i)^\s*("
    r"review\b|"
    r"read-only\b|"
    r"exact-head\b|"
    r"re-review\b|"
    r"frozen\b.*\breview\b|"
    r"semantic review\b|"
    r"code review\b"
    r")"
)

_REVIEW_ONLY_BODY = re.compile(
    r"(?i)("
    r"labels:\s*review:exact-head|"
    r"read-only gate\.|"
    r"reviewer mutation audit|"
    r"do not edit,\s*commit,\s*push"
    r")"
)

# Positive signals that this is an implementation / shipping card.
_IMPLEMENTATION_SIGNAL = re.compile(
    r"(?i)\b("
    r"implement|implementation|fix|repair|feature|refactor|"
    r"add |update |build |ship |deploy|pr\b|pull request|"
    r"npm run|website|route|page|content|code change|"
    r"acceptance criteria|approach"
    r")\b"
)


def normalize_tenant_key(value: Optional[str]) -> str:
    """Lowercase, strip, collapse separators for stable alias matching."""
    if not value:
        return ""
    key = value.strip().casefold()
    key = key.replace(" ", "").replace("_", "").replace("-", "")
    return key


def is_suomen_liittokunta_scope(
    *,
    tenant: Optional[str] = None,
    project_slug: Optional[str] = None,
) -> bool:
    """True when tenant or project slug is Suomen Liittokunta."""
    t = (tenant or "").strip().casefold()
    if t in _SL_TENANT_ALIASES:
        return True
    # Also accept compacted forms (suomen liittokunta → suomenliittokunta).
    if normalize_tenant_key(tenant) in {
        normalize_tenant_key(a) for a in _SL_TENANT_ALIASES
    }:
        return True

    p = (project_slug or "").strip().casefold()
    if p in _SL_PROJECT_ALIASES:
        return True
    if normalize_tenant_key(project_slug) in {
        normalize_tenant_key(a) for a in _SL_PROJECT_ALIASES
    }:
        return True
    return False


def is_implementation_card(
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> bool:
    """Heuristic: implementation/shipping work, not a pure review card.

    Review-only titles/bodies are excluded. Otherwise include when the
    text looks like implementation work, or when the body is empty/thin
    (freshly generated cards often start as a title + short goal).
    """
    title_s = (title or "").strip()
    body_s = (body or "").strip()
    combined = f"{title_s}\n{body_s}".strip()

    if not combined:
        return False

    if _REVIEW_ONLY_TITLE.search(title_s):
        return False
    if body_s and _REVIEW_ONLY_BODY.search(body_s) and not _IMPLEMENTATION_SIGNAL.search(
        title_s
    ):
        # Pure review-child body fragment without an implementation title.
        return False

    if _IMPLEMENTATION_SIGNAL.search(combined):
        return True

    # Fresh generated cards: short non-review body still gets the checklist
    # so workers see it before the first edit cycle.
    if title_s and not _REVIEW_ONLY_TITLE.search(title_s):
        return True
    return False


def already_has_local_first_checklist(body: Optional[str]) -> bool:
    if not body:
        return False
    return LOCAL_FIRST_SECTION_MARKER in body


def apply_local_first_checklist(
    body: Optional[str],
    *,
    tenant: Optional[str] = None,
    project_slug: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    """Append the SL local-first checklist when in scope and applicable.

    Returns the (possibly unchanged) body. ``None`` stays ``None`` only when
    out of scope or not an implementation card; when injection applies and
    body was empty/None, returns the checklist alone.
    """
    if not is_suomen_liittokunta_scope(tenant=tenant, project_slug=project_slug):
        return body
    if not is_implementation_card(title=title, body=body):
        return body
    if already_has_local_first_checklist(body):
        return body

    base = (body or "").rstrip()
    block = LOCAL_FIRST_CHECKLIST.strip()
    if base:
        return f"{base}\n\n{block}\n"
    return f"{block}\n"


def local_first_prompt_addendum(
    *,
    tenant: Optional[str] = None,
    project_slug: Optional[str] = None,
) -> str:
    """Extra system-prompt text for specify/decompose when SL-scoped.

    Empty string when out of scope so callers can append unconditionally.
    """
    if not is_suomen_liittokunta_scope(tenant=tenant, project_slug=project_slug):
        return ""
    return (
        "\n\nTenant policy (Suomen Liittokunta): every *implementation* child "
        "task body MUST include a section titled "
        f"\"{LOCAL_FIRST_SECTION_MARKER.lstrip('# ').strip()}\" with these five "
        "steps verbatim: (1) draft PR while iterating; (2) npm run gates on "
        "final SHA; (3) mark ready once; (4) npm run review:request -- --pr "
        "<n> --wait-gates; (5) never ask Alex for routine review. Do NOT put "
        "this section on pure read-only/exact-head review cards. Do not "
        "weaken deploy or review policy."
    )
