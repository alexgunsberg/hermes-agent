# Hermes Agent — Agent Contract

Instructions for AI coding assistants and developers working on Hermes Agent.

**Never give up on the right solution.** Verify the premise, preserve the
feature's intent, and leave evidence for every claimed result.

## Shared memory and knowledge base

Durable cross-service knowledge for this project lives in the private repo
`alexgunsberg/hermes-ops` (start with `docs/memory-system.md` and `AGENTS.md`).
Treat it as the git-distributed part of layer 3: the accumulated,
version-controlled source of truth for sanitized contracts, workflows,
runbooks, and prompts. Project-scoped source evidence, facts, decisions, and
tasks live in local Project Memory Bundles (PMB); use the read-only `pmb` MCP
retrieval tools when available.

- Accepted PMB records and `hermes-ops` override anything merely remembered
  from a session. Automatically captured PMB L0 sources are evidence, not
  accepted facts.
- Propose promoting durable decisions, runbooks, workflows, and config patterns
  to `hermes-ops` in sanitized form.
- Never write secrets, transcripts, runtime state, live memory databases, or
  logs there. Hermes runtime memory stays in `~/.hermes` on the Mac mini.
- If `hermes-ops` is inaccessible, say so and ask the owner to relay or grant
  access; do not guess its contents.

## Hosted automation and release cadence

For Alex-owned repositories, the durable cross-tool policy is
`alexgunsberg/hermes-ops/docs/github-actions-governance.md`. Hermes, Codex,
Cursor, Claude Code, and delegated workers must use that same policy instead of
inventing tool-specific CI habits.

- Keep normal iterations local or draft; only the release worker admits hosted
  CI and deployment candidates.
- Batch normal work into at most one candidate per active Europe/Helsinki date;
  idle days run nothing.
- A referenced urgent regression, security issue, data-integrity risk, or
  outage may bypass the daily wait, never review, required CI, protected
  integration, or smoke verification.
- Never duplicate an identical workflow/SHA/input run or create an empty commit
  to force one.

This repository is public, so standard GitHub-hosted runners do not consume the
private-repository minute allowance. The batching discipline still applies to
engineering churn and deployments; do not weaken upstream-required checks.

## Product model

Hermes runs the same agent core across CLI, messaging gateways, TUI, Electron
desktop, dashboard, and ACP integrations. It learns across sessions, delegates,
runs scheduled jobs, and operates terminals and browsers.

Two invariants govern almost every change:

1. **Per-conversation prompt caching is sacred.** Keep the system prompt and
   tool array byte-stable for a conversation. Never mutate past context, swap
   toolsets mid-session, inject synthetic user messages, or break strict role
   alternation. Context compression is the sole intentional prefix rewrite.
2. **The core is a narrow waist.** Every core model tool is paid for on every
   API call. Capability normally belongs in existing code, a CLI command plus
   skill, a service-gated tool, a plugin, or an MCP server.

Hermes grows aggressively at the product edges—platforms, providers, models,
desktop/TUI/dashboard features—while keeping the core agent and model-tool
schema deliberately small.

## Non-negotiable engineering rules

- Reproduce bugs on current `main`, identify the exact failing line/path, and
  fix the whole bug class, including sibling call paths.
- Read original intent with `git log -p -S '<symbol>'` before changing an
  apparent omission, isolation boundary, cooldown, or security restriction.
- Preserve features while securing them. A mitigation that destroys the
  feature is not an acceptable fix.
- Extend existing infrastructure instead of creating duplicate managers,
  hooks, or abstractions. Do not add speculative extension points.
- Behavioral configuration belongs in `config.yaml`. `.env` is for secrets
  only. Do not introduce user-facing `HERMES_*` variables for non-secret
  settings.
- Prefer behavior contracts and invariants over snapshots of changing values
  such as model lists, config versions, or enumeration counts.
- Exercise real imports and real I/O against a temporary `HERMES_HOME` for
  resolution, config, security, remote-backend, and file/network changes.
- Instructional files—skills, prompts, contracts, and playbooks—must be read
  completely. Do not add offset/limit escape hatches that encourage reading
  only page one. Split large instructions into complete routed references.
- Preserve contributor authorship by cherry-picking/rebase-merging external
  work when building on it.
- Do not add outbound telemetry, analytics, attribution, or third-party
  identifiers without a generic user opt-in gate.
- Profiles are isolated by design. Use profile-aware paths and never hardcode
  `~/.hermes` where `get_hermes_home()` or its display helper belongs.
- Plugins remain in their own directory and use generic plugin surfaces. Do
  not special-case a plugin in core files. Third-party product integrations
  ship as standalone plugin repositories, not under this core tree.
- Never let tests write to the real `~/.hermes`.

## Capability footprint ladder

Choose the first rung that correctly solves the problem:

1. Extend existing code.
2. Add a CLI command plus skill.
3. Add a prerequisite-gated structured tool (`check_fn`).
4. Build a standalone plugin.
5. Build/catalog an MCP server.
6. Add a core model tool only when it is fundamental, broadly useful, and
   unreachable through terminal, files, or MCP.

When three or more contributions integrate the same category, design a shared
ABC plus orchestrator, wrap the built-in implementation first, and move other
providers behind that interface.

## Required reference routing

Read this file completely. Then read every reference relevant to the task;
each reference is intentionally below the 20,000-character instructional-file
limit.

- Product intent, contribution acceptance/rejection, premise verification,
  and the detailed footprint ladder:
  `docs/agent-guide/contribution-and-design.md`
- Development environment, repository structure, TypeScript rules, core agent
  loop, CLI, TUI, dashboard, and desktop architecture:
  `docs/agent-guide/architecture-and-surfaces.md`
- Adding tools/configuration, dependency policy, skins, and plugin systems:
  `docs/agent-guide/configuration-and-extensions.md`
- Skills, toolsets, delegation, curator, cron, and Kanban:
  `docs/agent-guide/skills-and-operations.md`
- Prompt-cache rules, gateway notifications, profiles, known pitfalls, and
  testing standards:
  `docs/agent-guide/policies-and-testing.md`

For any code change, read `contribution-and-design.md` and
`policies-and-testing.md`. Add the architecture or extension reference that
owns the files being changed. Do not load unrelated references.

## Working defaults

```bash
source .venv/bin/activate  # fall back to venv when necessary
scripts/run_tests.sh tests/path/to/affected_test.py
```

- The filesystem is canonical; do not rely on stale file-count documentation.
- Use `rg` / `rg --files` for discovery.
- Preserve unrelated dirty-worktree changes and avoid destructive Git commands.
- Keep changes narrow. Record unrelated findings as follow-up work.
- Report changed files, actual verification commands/results, and the exact
  commit/PR/push state. Never claim an unavailable check passed.

## Load-bearing entry points

- `run_agent.py`: `AIAgent` and top-level conversation integration.
- `agent/conversation_loop.py`: synchronous model/tool loop.
- `agent/prompt_builder.py` and `agent/system_prompt.py`: cache-stable prompt
  construction.
- `model_tools.py`, `toolsets.py`, `tools/registry.py`: model tool discovery,
  schemas, routing, and registration.
- `cli.py`, `hermes_cli/commands.py`: classic CLI and central slash-command
  registry.
- `gateway/run.py`, `gateway/session.py`, `gateway/platforms/`: messaging.
- `ui-tui/` plus `tui_gateway/`: Ink TUI and Python JSON-RPC backend.
- `apps/desktop/`: separate Electron/React desktop chat surface.
- `hermes_cli/pty_bridge.py`, `hermes_cli/web_server.py`, `web/`: dashboard;
  its primary chat embeds the TUI rather than reimplementing it.
- `plugins/`, `skills/`, `optional-skills/`: edge capability.
- `tests/` and `scripts/run_tests.sh`: verification.

When a detailed reference conflicts with this root contract, this contract
wins.
