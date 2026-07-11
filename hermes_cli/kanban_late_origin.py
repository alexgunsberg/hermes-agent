"""Canonical-target late-origin subscription suppression for Kanban.

Telegram (and other gateway) command-mode ``/kanban create`` paths late-
subscribe the originating chat after the CLI returns. When a *canonical
report target* already exists for that platform — the configured home
channel, and/or an existing notify-sub that is not the origin — creating
an additional origin subscription duplicates / misroutes terminal
notifications.

This module is the single decision chokepoint for that suppress gate.
Destination selection, source labels, delivery confirmations, and the
notifier's terminal / terminal-race behaviour are intentionally untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


def _norm_thread(thread_id: Optional[str]) -> str:
    return str(thread_id or "").strip()


def _norm_chat(chat_id: Optional[str]) -> str:
    return str(chat_id or "").strip()


def resolve_canonical_report_target(platform: str) -> Optional[dict[str, Optional[str]]]:
    """Return the platform home channel as the canonical report target, if set.

    Home channels are the first-class gateway destination for cron/report and
    kanban terminal notifications (see dashboard home-subscribe). Missing or
    unloadable config yields ``None`` (no canonical target → do not suppress).
    """
    platform_key = (platform or "").strip().lower()
    if not platform_key:
        return None
    try:
        from gateway.config import Platform, load_gateway_config

        cfg = load_gateway_config()
        try:
            plat = Platform(platform_key)
        except ValueError:
            return None
        home = cfg.get_home_channel(plat)
        if home is None or not home.chat_id:
            return None
        return {
            "platform": platform_key,
            "chat_id": _norm_chat(home.chat_id),
            "thread_id": _norm_thread(home.thread_id) or None,
        }
    except Exception as exc:
        logger.debug(
            "resolve_canonical_report_target(%r) failed: %s", platform_key, exc
        )
        return None


def _target_key(
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> tuple[str, str, str]:
    return (
        (platform or "").strip().lower(),
        _norm_chat(chat_id),
        _norm_thread(thread_id),
    )


def _sub_key(sub: Mapping[str, Any]) -> tuple[str, str, str]:
    return _target_key(
        str(sub.get("platform") or ""),
        str(sub.get("chat_id") or ""),
        sub.get("thread_id"),
    )


def origin_matches_target(
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    target: Mapping[str, Any],
) -> bool:
    """True when the origin chat is the same destination as *target*."""
    return _target_key(platform, chat_id, thread_id) == _sub_key(target)


def existing_non_origin_subscription(
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    existing_subs: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Return the first existing notify-sub that is not the origin chat."""
    origin = _target_key(platform, chat_id, thread_id)
    for sub in existing_subs or ():
        if _sub_key(sub) != origin:
            return sub
    return None


def should_suppress_late_origin_subscription(
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    existing_subs: Optional[Sequence[Mapping[str, Any]]] = None,
    canonical_target: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return True when late origin auto-subscribe must be skipped.

    Suppression fires when a canonical report target exists and the origin
    chat is *not* that target:

    1. Configured platform home channel (canonical report destination), or
    2. An existing notify-sub on the task that is not the origin itself
       (explicit / home-subscribe / inherited report target).

    When the origin *is* the canonical home, subscription is allowed
    (idempotent write to the correct destination). When no canonical target
    exists, the legacy late-origin subscribe behaviour is preserved.
    """
    platform_key = (platform or "").strip().lower()
    chat = _norm_chat(chat_id)
    if not platform_key or not chat:
        return False

    canonical = (
        dict(canonical_target)
        if canonical_target is not None
        else resolve_canonical_report_target(platform_key)
    )

    # Existing non-origin subscription ⇒ a report target was already chosen.
    other = existing_non_origin_subscription(
        platform=platform_key,
        chat_id=chat,
        thread_id=thread_id,
        existing_subs=existing_subs or (),
    )
    if other is not None:
        return True

    if not canonical or not canonical.get("chat_id"):
        return False

    # Home channel configured: suppress only when origin is a different lane.
    return not origin_matches_target(platform_key, chat, thread_id, canonical)
