"""Prompt-size diagnostic: ``hermes prompt-size``.

Reports a byte/char breakdown of the system prompt the agent would build for
a fresh session — system prompt total, the ``<available_skills>`` index,
memory + user profile, and tool-schema JSON. Lets users see where their fixed
prompt budget goes (issue #34667) without parsing a saved session JSON by hand.

The diagnostic builds a real inspection agent (so the numbers match what
actually ships on the wire) but never makes a network call: it passes dummy
credentials so ``AIAgent.__init__`` takes the direct-construction path, then
calls ``build_system_prompt_parts`` / inspects ``agent.tools`` offline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

# The skills index is wrapped in this tag pair inside the stable tier.
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)

# Budgets cover the fixed, fresh-session payload. They deliberately exclude
# conversation history and provider-specific JSON framing. ``--check`` turns
# these from diagnostics into a CI/operator guard.
DEFAULT_TOKEN_BUDGETS = {
    "stable": 4_000,
    "context": 5_000,
    "tool_schemas": 6_000,
    "base_prefix": 10_000,
    "fresh_prefix": 15_000,
}


def _token_counter():
    """Return ``(counter, method)`` without making tiktoken a dependency.

    o200k_base is a useful common-denominator approximation for current large
    models. Minimal installs fall back to the same four-chars-per-token
    estimator used by tool-search and context-budget code.
    """
    try:
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("o200k_base")
        return (lambda text: len(encoding.encode(text))), "tiktoken:o200k_base"
    except Exception:
        return (lambda text: (len(text) + 3) // 4), "estimated:chars/4"


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _build_inspection_agent(platform: str) -> Any:
    """Construct an offline AIAgent for prompt inspection.

    Dummy ``api_key`` + ``base_url`` force the direct-construction path in
    ``run_agent.py`` (no provider auto-detection, no network). Toolsets and
    platform come from the caller so the breakdown matches a real session.
    """
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    cfg = load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    model = model_cfg.get("default") or model_cfg.get("model") or ""

    # Resolve platform-specific toolsets the same way the gateway does.
    enabled_toolsets = sorted(_get_platform_tools(cfg, platform))
    agent_cfg = cfg.get("agent") or {}
    disabled_toolsets = agent_cfg.get("disabled_toolsets") or None

    return AIAgent(
        model=model,
        api_key="inspect-only",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        save_trajectories=False,
        platform=platform,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
    )


def compute_prompt_breakdown(platform: str = "cli") -> Dict[str, Any]:
    """Return a dict of prompt-size measurements for a fresh session.

    Keys: ``system_prompt`` (chars/bytes), ``skills_index``, ``memory``,
    ``user_profile``, ``tools`` (count + json bytes), and ``sections`` (a list
    of (label, chars, bytes) for the three prompt tiers).
    """
    from agent.system_prompt import build_system_prompt_parts

    agent = _build_inspection_agent(platform)

    parts = build_system_prompt_parts(agent)

    stable = parts.get("stable", "")
    context = parts.get("context", "")
    volatile = parts.get("volatile", "")
    full = "\n\n".join(part for part in (stable, context, volatile) if part)

    # Skills index — the <available_skills> block (the largest single block
    # when many skills are installed). Measured inside the stable tier.
    skills_match = _SKILLS_BLOCK_RE.search(stable)
    skills_index = skills_match.group(0) if skills_match else ""

    # Memory + user profile live in the volatile tier. We re-derive their
    # blocks directly from the memory store so the numbers are attributable
    # even though they're joined into ``volatile``.
    memory_block = ""
    user_block = ""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        try:
            if getattr(agent, "_memory_enabled", True):
                memory_block = store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_user_profile_enabled", True):
                user_block = store.format_for_system_prompt("user") or ""
        except Exception:
            pass

    # Tool-schema JSON — the other half of the fixed per-call payload.
    tools = getattr(agent, "tools", None) or []
    tools_json = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    count_tokens, token_method = _token_counter()

    tool_items = []
    for tool in tools:
        encoded = json.dumps(tool, ensure_ascii=False, separators=(",", ":"))
        tool_items.append({
            "name": str((tool.get("function") or {}).get("name") or ""),
            "chars": len(encoded),
            "bytes": _bytes(encoded),
            "tokens": count_tokens(encoded),
        })
    tool_items.sort(key=lambda item: (-item["tokens"], item["name"]))

    sections: List[Tuple[str, int, int, int]] = [
        ("stable (identity/guidance/skills)", len(stable), _bytes(stable), count_tokens(stable)),
        ("context (AGENTS.md/cwd files)", len(context), _bytes(context), count_tokens(context)),
        ("volatile (memory/profile/timestamp)", len(volatile), _bytes(volatile), count_tokens(volatile)),
    ]

    stable_tokens = count_tokens(stable)
    context_tokens = count_tokens(context)
    volatile_tokens = count_tokens(volatile)
    tools_tokens = count_tokens(tools_json)
    base_prefix_tokens = stable_tokens + volatile_tokens + tools_tokens
    budget_values = {
        "stable": stable_tokens,
        "context": context_tokens,
        "tool_schemas": tools_tokens,
        "base_prefix": base_prefix_tokens,
        "fresh_prefix": base_prefix_tokens + context_tokens,
    }
    budgets = {
        name: {
            "tokens": budget_values[name],
            "limit": limit,
            "over": max(0, budget_values[name] - limit),
            "ok": budget_values[name] <= limit,
        }
        for name, limit in DEFAULT_TOKEN_BUDGETS.items()
    }

    return {
        "platform": platform,
        "model": getattr(agent, "model", "") or "",
        "token_method": token_method,
        "system_prompt": {"chars": len(full), "bytes": _bytes(full), "tokens": count_tokens(full)},
        "skills_index": {"chars": len(skills_index), "bytes": _bytes(skills_index), "tokens": count_tokens(skills_index)},
        "memory": {"chars": len(memory_block), "bytes": _bytes(memory_block), "tokens": count_tokens(memory_block)},
        "user_profile": {"chars": len(user_block), "bytes": _bytes(user_block), "tokens": count_tokens(user_block)},
        "tools": {
            "count": len(tools),
            "json_bytes": _bytes(tools_json),
            "tokens": tools_tokens,
            "items": tool_items,
        },
        "base_prefix_tokens": base_prefix_tokens,
        "fresh_prefix_tokens": base_prefix_tokens + context_tokens,
        "budgets": budgets,
        "over_budget": [name for name, result in budgets.items() if not result["ok"]],
        "sections": sections,
    }


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def render_breakdown(data: Dict[str, Any]) -> str:
    """Render the breakdown as plain text suitable for a terminal."""
    lines: List[str] = []
    sp = data["system_prompt"]
    lines.append(f"Prompt-size breakdown (platform={data['platform']}, model={data['model'] or 'unset'})")
    lines.append("")
    lines.append(
        f"  System prompt total : {sp['bytes']:>8,} B  "
        f"({_fmt_kb(sp['bytes'])}, {sp['tokens']:,} tokens)"
    )
    lines.append(f"  Fresh fixed prefix  : {data['fresh_prefix_tokens']:>8,} tokens")
    lines.append(f"  Base prefix         : {data['base_prefix_tokens']:>8,} tokens (without project context)")
    lines.append(f"  Token method        : {data['token_method']}")
    lines.append("")
    lines.append("  Major blocks:")
    si = data["skills_index"]
    mem = data["memory"]
    up = data["user_profile"]
    lines.append(f"    skills index       : {si['bytes']:>8,} B  ({_fmt_kb(si['bytes'])})")
    lines.append(f"    memory             : {mem['bytes']:>8,} B  ({_fmt_kb(mem['bytes'])})")
    lines.append(f"    user profile       : {up['bytes']:>8,} B  ({_fmt_kb(up['bytes'])})")
    lines.append("")
    lines.append("  Prompt tiers:")
    for label, chars, byts, tokens in data["sections"]:
        lines.append(f"    {label:<36}: {byts:>8,} B  ({tokens:>7,} tokens)")
    lines.append("")
    tools = data["tools"]
    lines.append(
        f"  Tool schemas         : {tools['json_bytes']:>8,} B  "
        f"({tools['tokens']:,} tokens, {tools['count']} tools)"
    )
    for item in tools.get("items", [])[:10]:
        lines.append(f"    {item['name']:<34}: {item['tokens']:>7,} tokens")
    lines.append("")
    lines.append("  Token budgets:")
    for name, result in data["budgets"].items():
        marker = "OK" if result["ok"] else f"OVER by {result['over']:,}"
        lines.append(
            f"    {name:<20}: {result['tokens']:>7,} / {result['limit']:>7,}  {marker}"
        )
    return "\n".join(lines)


def cmd_prompt_size(args: Any) -> None:
    """Entry point for ``hermes prompt-size``."""
    platform = getattr(args, "platform", "cli") or "cli"
    as_json = getattr(args, "json", False)
    try:
        data = compute_prompt_breakdown(platform)
    except Exception as e:
        print(f"Could not compute prompt-size breakdown: {e}")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_breakdown(data))
    if getattr(args, "check", False) and data.get("over_budget"):
        raise SystemExit(1)
