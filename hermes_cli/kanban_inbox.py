"""Zero-prefix Kanban inbox bindings for messaging platforms.

A binding maps an exact messaging topic/thread (currently Telegram) to a
Kanban board + default card routing.  Plain messages in that thread become
triage cards before they enter the normal LLM session path.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home
from utils import atomic_replace


STORE_FILENAME = "kanban_inbox_bindings.json"


@dataclass(frozen=True)
class KanbanInboxBinding:
    name: str
    platform: str
    chat_id: str
    thread_id: str = ""
    board: str = "default"
    assignee: str = "default"
    project: Optional[str] = None
    workspace: str = "scratch"
    tenant: Optional[str] = None
    priority: int = 0
    enabled: bool = True
    created_at: int = 0
    updated_at: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_profile(profile: Optional[str]) -> Optional[str]:
    if profile is None:
        return None
    value = str(profile).strip()
    if not value or value == "default":
        return "default"
    return value


def profile_home(profile: Optional[str] = None) -> Path:
    """Return the HERMES_HOME that owns an inbox binding store.

    ``profile=None`` means the currently-active process home.  A named profile
    writes to ``<root>/profiles/<name>`` so a multiplexed default gateway can
    manage secondary-profile bindings without mutating its own store.
    """

    norm = _normalize_profile(profile)
    if norm is None:
        return get_hermes_home()
    root = get_default_hermes_root()
    if norm == "default":
        return root
    return root / "profiles" / norm


def store_path(profile: Optional[str] = None) -> Path:
    return profile_home(profile) / STORE_FILENAME


def _binding_from_raw(raw: dict[str, Any]) -> KanbanInboxBinding:
    now = int(time.time())
    name = str(raw.get("name") or raw.get("board") or "inbox").strip() or "inbox"
    platform = str(raw.get("platform") or "telegram").strip().lower()
    chat_id = str(raw.get("chat_id") or "").strip()
    thread_id = str(raw.get("thread_id") or "").strip()
    board = str(raw.get("board") or name or "default").strip() or "default"
    assignee = str(raw.get("assignee") or ("default" if board in {"default", "general"} else board)).strip() or "default"
    workspace = str(raw.get("workspace") or "scratch").strip() or "scratch"
    return KanbanInboxBinding(
        name=name,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        board=board,
        assignee=assignee,
        project=(str(raw.get("project")).strip() or None) if raw.get("project") is not None else None,
        workspace=workspace,
        tenant=(str(raw.get("tenant")).strip() or None) if raw.get("tenant") is not None else None,
        priority=int(raw.get("priority") or 0),
        enabled=bool(raw.get("enabled", True)),
        created_at=int(raw.get("created_at") or now),
        updated_at=int(raw.get("updated_at") or raw.get("created_at") or now),
    )


def load_bindings(profile: Optional[str] = None) -> list[KanbanInboxBinding]:
    path = store_path(profile)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    rows = raw.get("bindings", []) if isinstance(raw, dict) else []
    out: list[KanbanInboxBinding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(_binding_from_raw(row))
        except Exception:
            continue
    return out


def save_bindings(bindings: Iterable[KanbanInboxBinding], profile: Optional[str] = None) -> Path:
    path = store_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "bindings": [b.as_dict() for b in bindings],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    atomic_replace(tmp, path)
    return path


def upsert_binding(
    *,
    name: str,
    platform: str = "telegram",
    chat_id: str,
    thread_id: Optional[str] = None,
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    project: Optional[str] = None,
    workspace: str = "scratch",
    tenant: Optional[str] = None,
    priority: int = 0,
    enabled: bool = True,
    profile: Optional[str] = None,
) -> KanbanInboxBinding:
    now = int(time.time())
    clean_name = (name or board or "inbox").strip()
    if not clean_name:
        raise ValueError("binding name is required")
    clean_platform = (platform or "telegram").strip().lower()
    clean_chat = str(chat_id or "").strip()
    if not clean_chat:
        raise ValueError("chat_id is required")
    clean_thread = str(thread_id or "").strip()
    clean_board = (board or clean_name or "default").strip() or "default"
    clean_assignee = (assignee or ("default" if clean_board in {"default", "general"} else clean_board)).strip() or "default"
    binding = KanbanInboxBinding(
        name=clean_name,
        platform=clean_platform,
        chat_id=clean_chat,
        thread_id=clean_thread,
        board=clean_board,
        assignee=clean_assignee,
        project=(project.strip() or None) if isinstance(project, str) else project,
        workspace=(workspace or "scratch").strip() or "scratch",
        tenant=(tenant.strip() or None) if isinstance(tenant, str) else tenant,
        priority=int(priority or 0),
        enabled=bool(enabled),
        created_at=now,
        updated_at=now,
    )
    bindings = load_bindings(profile)
    replaced = False
    merged: list[KanbanInboxBinding] = []
    for existing in bindings:
        same_name = existing.name == binding.name
        same_route = (
            existing.platform == binding.platform
            and existing.chat_id == binding.chat_id
            and existing.thread_id == binding.thread_id
        )
        if same_name or same_route:
            binding = KanbanInboxBinding(**{**binding.as_dict(), "created_at": existing.created_at or now, "updated_at": now})
            if not replaced:
                merged.append(binding)
                replaced = True
            continue
        merged.append(existing)
    if not replaced:
        merged.append(binding)
    save_bindings(merged, profile)
    return binding


def remove_binding(
    *,
    name: Optional[str] = None,
    platform: str = "telegram",
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> bool:
    bindings = load_bindings(profile)
    clean_platform = (platform or "telegram").strip().lower()
    clean_chat = str(chat_id or "").strip()
    clean_thread = str(thread_id or "").strip()
    clean_name = (name or "").strip()
    kept: list[KanbanInboxBinding] = []
    removed = False
    for binding in bindings:
        by_name = bool(clean_name and binding.name == clean_name)
        by_route = bool(
            clean_chat
            and binding.platform == clean_platform
            and binding.chat_id == clean_chat
            and binding.thread_id == clean_thread
        )
        if by_name or by_route:
            removed = True
            continue
        kept.append(binding)
    if removed:
        save_bindings(kept, profile)
    return removed


def match_binding(source: Any, *, profile: Optional[str] = None) -> Optional[KanbanInboxBinding]:
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
    platform = str(platform or "").lower()
    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    effective_profile = profile if profile is not None else getattr(source, "profile", None)
    for binding in load_bindings(effective_profile):
        if not binding.enabled:
            continue
        if binding.platform != platform:
            continue
        if binding.chat_id != chat_id:
            continue
        if binding.thread_id != thread_id:
            continue
        return binding
    return None


def infer_kind(text: str) -> str:
    lowered = (text or "").lower()
    bug_terms = ("bug", "broken", "crash", "error", "fail", "freeze", "stuck", "doesn't work", "not working")
    idea_terms = ("idea", "suggest", "could", "maybe", "improve", "feature")
    task_terms = ("todo", "task", "please", "need to", "remind", "fix", "build", "add")
    if any(term in lowered for term in bug_terms):
        return "bug"
    if any(term in lowered for term in idea_terms):
        return "idea"
    if any(term in lowered for term in task_terms):
        return "task"
    return "triage"


def _title_from_text(text: str, kind: str) -> str:
    first = " ".join((text or "").strip().split())
    if not first:
        first = "Captured Telegram inbox item"
    if len(first) > 96:
        first = first[:93].rstrip() + "..."
    return f"[{kind}] {first}"


def _message_identity(event: Any, source: Any, text: str) -> str:
    mid = getattr(event, "message_id", None) or getattr(source, "message_id", None) or getattr(event, "platform_update_id", None)
    if mid is not None:
        return str(mid)
    stamp = getattr(event, "timestamp", None)
    raw = f"{getattr(source, 'chat_id', '')}:{getattr(source, 'thread_id', '')}:{stamp}:{text}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def capture_inbox_message(
    event: Any,
    *,
    profile: Optional[str] = None,
    notifier_profile: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Create a Kanban triage task for a bound plain message.

    Returns a dict with ``task_id``/``receipt`` when captured, or ``None`` when
    the event is not eligible or not bound.
    """

    if getattr(event, "internal", False):
        return None
    text = (getattr(event, "text", "") or "").strip()
    if not text or text.startswith("/"):
        return None
    source = getattr(event, "source", None)
    if source is None:
        return None
    binding = match_binding(source, profile=profile)
    if binding is None:
        return None

    from hermes_cli import kanban_db as kb

    kind = infer_kind(text)
    title = _title_from_text(text, kind)
    source_platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
    identity = _message_identity(event, source, text)
    idempotency_key = f"kanban-inbox:{source_platform}:{binding.chat_id}:{binding.thread_id}:{identity}"
    media = list(getattr(event, "media_urls", None) or [])
    body_lines = [
        "Captured from a Telegram zero-prefix Kanban inbox.",
        "",
        "Original message:",
        text,
        "",
        "Routing:",
        f"- Inbox: {binding.name}",
        f"- Board: {binding.board}",
        f"- Assignee: {binding.assignee}",
        f"- Kind: {kind}",
        f"- Source: {source_platform}:{binding.chat_id}:{binding.thread_id or '-'} message {identity}",
    ]
    if binding.project:
        body_lines.append(f"- Project: {binding.project}")
    if binding.tenant:
        body_lines.append(f"- Tenant: {binding.tenant}")
    if media:
        body_lines.extend(["", "Attached media paths:"] + [f"- {p}" for p in media])
    body = "\n".join(body_lines)

    with kb.connect_closing(board=binding.board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=binding.assignee,
            created_by=f"{source_platform}-inbox",
            workspace_kind=binding.workspace,
            project_id=binding.project,
            tenant=binding.tenant,
            priority=binding.priority,
            triage=True,
            idempotency_key=idempotency_key,
            goal_mode=False,
            board=binding.board,
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=str(source_platform or "").lower(),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            thread_id=str(getattr(source, "thread_id", "") or "") or None,
            user_id=(str(getattr(source, "user_id", "") or "") or None),
            notifier_profile=notifier_profile or profile,
        )

    preview = " ".join(text.split())
    if len(preview) > 160:
        preview = preview[:157].rstrip() + "..."
    receipt = f"Queued `{task_id}` → `{binding.board}` · `{kind}` · triage\n{preview}"
    return {
        "task_id": task_id,
        "receipt": receipt,
        "binding": binding.as_dict(),
        "kind": kind,
        "idempotency_key": idempotency_key,
    }
