---
name: coding-agent-routing
description: "Route coding tasks to the right delegate CLI (Grok Build, Cursor, Codex, Claude Code) with optimized pipelines."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Routing, Orchestration, Grok, Cursor, Automation]
    related_skills: [grok, cursor, codex, claude-code]
---

# Coding-Agent Routing — Grok Build vs Cursor (vs siblings) through Hermes

Hermes can delegate coding work to several external agent CLIs. This skill is
the decision layer: which delegate to pick, how to invoke it for maximum
capability, and how to measure the choice instead of guessing.

Capability facts below: the Grok column was verified live against
`grok 0.2.109` (`--help` inspection); the Cursor column reflects Cursor's
published CLI docs. **Version-gate every capability**: re-verify with
`grok --help` / `cursor-agent --help` before emitting a flag — Grok removed
`--check` and `--best-of-n` between 0.2.106 and 0.2.109, so any cached
"maximum profile" can silently go stale. Record the resolved CLI version and
model in every result.

## Capability Matrix

| Capability | Grok Build (`grok`) | Cursor Agent (`cursor-agent`) |
|---|---|---|
| Headless one-shot | `-p` / `--prompt-file` | `-p` (prompt positional; inferred on non-TTY) |
| Auto-approve writes | `--always-approve`, or graded `--permission-mode` | `--force` (binary) |
| Output formats | `plain`, `json`, `streaming-json` | `text`, `json`, `stream-json` (**default in print mode: stream-json**) |
| Schema-constrained output | `--json-schema '<schema>'` | not available |
| Best-of-N parallel attempts | removed in 0.2.109 (`--best-of-n` was 0.2.106-only); emulate via parallel `-w` worktrees as an opt-in fan-out | not available |
| Built-in self-verification | removed in 0.2.109 (`--check` was 0.2.106-only); use external verification + one same-session repair (`-s`/`-r`) | not available (same external pattern) |
| Git worktree isolation | built-in `-w/--worktree`, `grok worktree` | manual `git worktree` + `workdir` |
| Turn cap | `--max-turns N` | not available |
| Sandbox profiles | `--sandbox <profile>` | permissions allow/deny in CLI config |
| Tool allow/deny | `--allow/--deny`, `--tools/--disallowed-tools` | config-file permissions |
| Model choice | xAI models only (`-m`, `--reasoning-effort`) | multi-lab: GPT, Claude, Gemini, Grok, Composer (`-m`) |
| Subagents | yes; inline `--agents` JSON | no first-class flag |
| Session resume | `-r/-c`, named `-s`, `--fork-session`, `--restore-code` | `--resume`, `--continue`, `cursor-agent ls` |
| Project context | reads `CLAUDE.md`, `.claude/`, `AGENTS.md`, `.grok/` | reads `.cursor/rules`, `AGENTS.md` |
| MCP | `grok mcp` | `cursor-agent mcp` |
| Auth | SuperGrok / X Premium+ OAuth (`grok login`, `--device-code`) or `XAI_API_KEY` | Cursor plan OAuth (`cursor-agent login`) or `CURSOR_API_KEY` |
| Install without vendor host | `npm i -g @xai-official/grok` (npm registry only) | no — installer requires `cursor.com` (npm `cursor-agent` is a squatter) |
| ACP / embedding | `grok agent stdio` (ACP over JSON-RPC) | no ACP mode in the CLI proper |

## Routing Rules

Route by task shape first, then by environment constraints:

1. **High-assurance autonomous builds → Grok with bounded autonomy.** Pin the
   model and effort, cap turns, strip inherited context, and verify
   externally: `-m grok-lean --reasoning-effort high --max-turns 40
   --no-subagents --no-memory --disable-web-search --output-format json`.
   Self-verification is YOUR check plus one same-session repair (`-s <uuid>`
   then `-r <uuid>` with the exact failure evidence) — do not rely on
   `--check`/`--best-of-n`, which were removed in 0.2.109.
2. **Pipelines that parse agent output → Grok.** `--json-schema` yields
   validated JSON; Cursor can emit `json` but cannot constrain its shape.
3. **Model-sensitive tasks → Cursor.** When the task benefits from a specific
   lab's model (Claude for careful refactors, GPT for breadth, Composer for
   fast cheap iteration), Cursor is the only delegate that routes to all of
   them behind one CLI.
4. **Repos already configured for Cursor → Cursor.** `.cursor/rules` and team
   Cursor config carry over natively.
5. **Repos configured for Claude Code → Grok or Claude Code.** Grok reads
   `CLAUDE.md` / `.claude/` with zero config, so it can substitute when Claude
   Code quota is exhausted.
6. **Locked-down / proxied environments → Grok.** It installs from the npm
   registry alone; Cursor's installer needs the Cloudflare-fronted
   `cursor.com`. Check reachability before promising Cursor.
7. **Parallel fan-out (N issues at once) → Grok first.** Built-in `-w`
   worktrees keep the main checkout clean without manual git plumbing; with
   Cursor, create worktrees yourself.
8. **Untrusted or destructive-prone tasks → Grok.** Graded `--permission-mode`,
   `--sandbox`, `--deny` rules and `--max-turns` bound the blast radius;
   Cursor's `--force` is all-or-nothing (use its config-file permissions to
   compensate).
9. **Read-only reviews/audits → either**, invoked WITHOUT the write switch
   (omit `--always-approve` / `--force`) and with "markdown only, no preamble"
   in the prompt.
10. **Auth decides last.** A delegate with no working auth is not a candidate:
    check `~/.grok/auth.json` or `XAI_API_KEY` for Grok; `cursor-agent status`
    or `CURSOR_API_KEY` for Cursor. Fall back to the sibling rather than
    stalling on a login prompt.

## Optimized Pipeline Recipes

```
# Grok: bounded-autonomy build (the `grok-lean-high` router profile)
grok --no-auto-update --always-approve -m grok-lean --reasoning-effort high \
  --max-turns 40 --no-subagents --no-memory --disable-web-search \
  --output-format json -s <uuid> -p 'Fix issue #123; run the test suite'
# ...verify externally; on failure, repair once in the SAME session:
grok --no-auto-update --always-approve -r <uuid> --output-format json \
  -p 'Acceptance failed with: <evidence>. Fix it.'

# Grok: structured audit feeding a Hermes pipeline
grok --no-auto-update -p 'List dead code in src/' \
  --json-schema '{"type":"object","properties":{"findings":{"type":"array","items":{"type":"string"}}},"required":["findings"]}'

# Cursor: model-routed build with parseable single-object result
cursor-agent -p 'Implement the settings page per AGENTS.md conventions' \
  --force -m sonnet-4.5 --output-format json

# Cursor: cheap fast iteration loop on a small fix
cursor-agent -p 'Fix the failing test in tests/test_config.py' --force -m composer --output-format text
```

Always: set `workdir`, pass `--no-auto-update` (Grok) and an explicit
`--output-format` (Cursor), background anything long with
`background=true, notify_on_complete=true`, and verify the delegate's diff
yourself (run the repo's tests) before reporting success.

## Route Production Work: the `route_code_task` Tool

The general routing path is the engine in `tools/code_routing.py`, exposed to
Hermes as the **`route_code_task` tool** (toolset `delegation`) and to
operators as `scripts/coding_agent_router.py`. A work packet targets a real
repository (`repo`: local path or URL, `base_sha`: the exact commit to build
on) or seeds a synthetic scratch repo from `files` (benchmarks). The engine:

1. clones an isolated workspace at the exact base SHA (the original checkout
   is never touched); packet file paths are strictly validated — no absolute
   paths, no `..`, no symlink escapes;
2. runs the selected profile headless under process-group supervision plus a
   descendant tracker (no orphaned processes, even detached ones);
3. verifies OUTSIDE candidate control: protected files are restored from the
   packet before acceptance, and an `allowed_paths` scope contract rejects
   changes outside the packet's declared surface;
4. performs at most one same-session repair with the exact failure evidence,
   reroutes the identical packet once to the fallback profile, then stops;
5. appends every attempt to `HERMES_HOME/router/routes.jsonl` with the
   packet hash (covering all semantic fields), per-phase run/repair outcomes
   and usage, exact model/profile/resolved CLI version, and failure class.

```
python scripts/coding_agent_router.py --packet packet.json
```

Named profiles (all env-configurable, never plain defaults):

- `grok-high` — `ROUTER_GROK_BIN` (default `grok`), `ROUTER_GROK_MODEL`
  (default `grok-4.5`, catalog-valid on 0.2.109), `ROUTER_GROK_EFFORT`,
  `ROUTER_GROK_SANDBOX` (when set, `--sandbox` is an argv invariant and the
  profile fails closed if the binary can't enforce it).
- `cursor-grok-high-fast-off` — `ROUTER_CURSOR_GROK_MODEL`.
- `cursor-composer` — `ROUTER_CURSOR_COMPOSER_MODEL`.
- `cursor-frontier` — **fails closed** unless `ROUTER_CURSOR_FRONTIER_MODEL`
  names a model from the live authenticated catalog; a moving alias must
  never silently pin a stale model.

Risk policy: packets with `risk: high` route only to sandbox-enforcing
profiles. The routing table (task class → [primary, fallback]) is
provisional — the JSONL production record is what promotes or demotes a
harness per task class, not synthetic samples.

## Measure, Don't Guess

Routing rules decay as CLIs evolve. `scripts/coding_agent_bench.py` runs an
identical task suite through any subset of delegates in isolated scratch git
repos, verifies each result with the task's own check command, and emits a
JSON + Markdown comparison (wall time, verify pass rate, diff size):

```
python scripts/coding_agent_bench.py --agents grok,cursor --out /tmp/bench
```

Agents without working auth are reported as unavailable rather than failing
the run. Re-run the bench when a CLI ships a new version and update the rules
above from the data.
