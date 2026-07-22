---
name: grok
description: "Delegate coding to xAI Grok Build CLI (features, PRs)."
version: 0.2.0
author: Matt Maximo (MattMaximo), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Grok, xAI, Code-Review, Refactoring, Automation]
    related_skills: [codex, claude-code, cursor, coding-agent-routing, hermes-agent]
---

# Grok Build CLI — Hermes Orchestration Guide

Delegate coding tasks to [Grok Build](https://docs.x.ai/build/overview) (xAI's
autonomous coding agent CLI, the `grok` command) via the Hermes terminal. Grok
can read files, write code, run shell commands, spawn subagents, and manage git
workflows. It runs three ways: an interactive TUI, **headless** (`-p`), and as
an **ACP agent** over JSON-RPC.

This is the third sibling to `codex` and `claude-code`. The orchestration
pattern is nearly identical — **prefer headless `-p` for one-shots**, use a PTY
for interactive sessions.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing
- Any task where you'd otherwise reach for Codex / Claude Code but want Grok

## Prerequisites

- **Install (preferred):** `npm install -g @xai-official/grok`
  - The official installer `curl -fsSL https://x.ai/cli/install.sh | bash` also
    works, but the `x.ai` host is Cloudflare-walled in some environments. The
    npm path avoids that dependency entirely.
- **Auth — SuperGrok / X Premium+ subscription (primary path):**
  - Run `grok login` once → opens a browser for OAuth → token cached in
    `~/.grok/auth.json`. This uses your **SuperGrok or X Premium+** subscription
    (no per-token API billing). On a headless box use `grok login --device-code`
    (prints a code to enter on another device).
  - Check sign-in state by looking for `~/.grok/auth.json`, or run a cheap
    headless smoke test: `grok --no-auto-update -p "Say ok."`
  - In the TUI, `/logout` signs out and `/login` (or relaunching) signs back in.
- **No git repo required** — unlike Codex, Grok runs fine outside a git
  directory (good for scratch/throwaway tasks).
- **Claude Code / AGENTS.md compatible with zero config** — Grok auto-reads
  `CLAUDE.md`, `.claude/` (skills, agents, MCPs, hooks, rules), and the
  `AGENTS.md` family. Existing project context just works.

> **API-key fallback (not the default for this user):** Grok also supports
> setting the `XAI_API_KEY` environment variable for pay-as-you-go billing
> via `api.x.ai`. Only use
> this if `grok login` / SuperGrok auth is unavailable. The subscription path
> (`grok login`) is the intended setup here.

## Two Orchestration Modes

### Mode 1: Headless (`-p`) — Non-Interactive (PREFERRED)

Runs a one-shot task, prints the result, and exits. No PTY, no interactive
dialogs to navigate. This is the cleanest integration path — the analog of
`claude -p` and `codex exec`.

```
terminal(command="grok --no-auto-update -p 'Add a dark mode toggle to settings'", workdir="/path/to/project", timeout=180)
```

Always pass `--no-auto-update` in automation to skip background update checks.

**When to use headless:**
- One-shot coding tasks (fix a bug, add a feature, refactor)
- CI/CD automation and scripting
- Structured output parsing with `--output-format json`
- Any task that doesn't need multi-turn conversation

### Mode 2: Interactive PTY — Multi-Turn TUI Sessions

The TUI is a fullscreen, mouse-interactive app. Drive it with `pty=true`. For
robust monitoring/input use tmux (same pattern as the `claude-code` skill).

```
# Launch in a tmux session for capture-pane monitoring
terminal(command="tmux new-session -d -s grok-work -x 140 -y 40")
terminal(command="tmux send-keys -t grok-work 'cd /path/to/project && grok' Enter")

# Wait for startup, then send a task
terminal(command="sleep 5 && tmux send-keys -t grok-work 'Refactor the auth module to use JWT' Enter")

# Monitor progress
terminal(command="sleep 15 && tmux capture-pane -t grok-work -p -S -50")

# Exit when done
terminal(command="tmux send-keys -t grok-work '/quit' Enter && sleep 1 && tmux kill-session -t grok-work")
```

**Tip for headless-but-inline output:** if you want TUI-style output without the
fullscreen alt-screen takeover (e.g. for cleaner logs), add `--no-alt-screen`.
For pure automation, headless `-p` is still cleaner than the TUI.

## Headless Deep Dive

### Common Flags

| Flag | Effect |
|------|--------|
| `-p, --single <PROMPT>` | Send one prompt, run headless, exit |
| `-m, --model <MODEL>` | Choose a model |
| `-s, --session-id <ID>` | Create or resume a named headless session |
| `-r, --resume <ID>` | Resume an existing session |
| `-c, --continue` | Continue the most recent session in the current directory |
| `--cwd <PATH>` | Set the working directory |
| `--output-format <FMT>` | `plain` (default), `json`, or `streaming-json` |
| `--always-approve` | Auto-approve all tool executions (the `--full-auto` / `--yolo` equivalent) |
| `--permission-mode <MODE>` | Finer-grained than `--always-approve`: `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan` |
| `--no-alt-screen` | Run inline, no fullscreen TUI takeover |
| `--no-auto-update` | Skip background update checks (use in all automation) |
| `--prompt-file <PATH>` | Single-turn prompt read from a file (safer than shell-quoting a long prompt) |
| `--max-turns <N>` | Cap agent turns (bound runaway autonomous runs) |
| `--rules <RULES>` | Append extra rules to the system prompt for this run |
| `--reasoning-effort <EFFORT>` | Reasoning effort for reasoning models |
| `--sandbox <PROFILE>` | Sandbox profile for filesystem/network access (also `GROK_SANDBOX` env) |
| `--allow <RULE>` / `--deny <RULE>` | Permission allow/deny rules (compat: `--allowedTools` / `--disallowedTools`) |
| `--tools` / `--disallowed-tools` | Comma-separated allow/remove lists for built-in tools |
| `--disable-web-search` | Turn off web search/fetch tools for hermetic runs |
| `-w, --worktree [<NAME>]` | Run the session in a fresh git worktree (`--worktree-ref` picks the base) |
| `--best-of-n <N>` | **0.2.106 only — removed in 0.2.109.** Ran the task N ways in parallel, picked the best |
| `--check` | **0.2.106 only — removed in 0.2.109.** Appended a self-verification loop to the prompt |
| `--json-schema <SCHEMA>` | Constrain output to a JSON Schema (implies `--output-format json`) |
| `--agents <JSON>` / `--agent <NAME>` | Inline subagent definitions / named agent profile |
| `--no-subagents` / `--no-plan` / `--no-memory` | Disable subagent spawning / plan mode / cross-session memory |

### Output Formats

- `plain` — human-readable text (default)
- `json` — one JSON object at the end of the run (parse the result cleanly)
- `streaming-json` — newline-delimited JSON events as they arrive

```
# Structured result for parsing
terminal(command="grok --no-auto-update -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the database layer and run the tests'", workdir="/project", timeout=300)
```

### Pipeline Maximizers

> **Version-gate before using any of these** — Grok's flag surface moves
> fast. Run `grok --help` and check the flag exists in the installed version
> before emitting it. Notably, `--best-of-n` and `--check` existed in 0.2.106
> but were **removed by 0.2.109**; on current versions, self-verification is
> your own external check plus one same-session repair (`-s <uuid>` to name
> the session, then `-r <uuid>` with the failure evidence), and best-of-N is
> an opt-in fan-out you build with multiple `-w` worktrees.

Verified on grok 0.2.109 (all flags below still present):

```
# Isolation: run in a throwaway git worktree so main checkout is never touched
terminal(command="grok --no-auto-update --always-approve -w fix-retry -p 'Fix issue #78 and commit'", workdir="/project", timeout=600)

# Structured pipeline output: schema-constrained JSON you can parse blindly
terminal(command="grok --no-auto-update -p 'Audit src/ for TODOs' --json-schema '{\"type\":\"object\",\"properties\":{\"todos\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}},\"required\":[\"todos\"]}'", workdir="/project", timeout=300)

# Bounded autonomy: cap turns, deny dangerous tools, no web access
terminal(command="grok --no-auto-update --always-approve --max-turns 30 --disable-web-search --deny 'Bash(rm *)' -p 'Refactor the auth module'", workdir="/project", timeout=900)
```

### Background Mode (Long Tasks)

```
# Start headless in background
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the auth module'", workdir="/project", background=true, notify_on_complete=true)
# Returns session_id

# Monitor
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

For an interactive (TUI) background session, use `pty=true` + tmux and monitor
with `tmux capture-pane`, exactly like the `claude-code` / `codex` skills.

### Session Continuation

```
# Start a named session
terminal(command="grok --no-auto-update -s refactor-db -p 'Start refactoring the database layer' --always-approve", workdir="/project", timeout=240)

# Resume it later
terminal(command="grok --no-auto-update -r refactor-db -p 'Now add connection pooling' --always-approve", workdir="/project", timeout=180)

# Or continue the most recent session in this directory
terminal(command="grok --no-auto-update -c -p 'What did you change last time?'", workdir="/project", timeout=60)
```

## Read-Only Audit → Markdown Note Pattern

To have Grok review local artifacts and return a clean markdown note (for
Obsidian or a repo) without mutating anything:

1. Prepare stable input files first with Hermes tools (`read_file`,
   `write_file`). Snapshot only the relevant context into a temp file rather
   than dumping raw paths.
2. Run Grok headless **without** `--always-approve` so it cannot auto-write, and
   demand `markdown only, no preamble`.
3. Save Grok's stdout straight into the destination note with `write_file()`.

```
grok --no-auto-update -p "Read /tmp/current.md and /tmp/inventory.md. Produce markdown only, no preamble. Output a clean note titled 'Cleanup Review'." --output-format plain
```

**Pitfall (same as Claude Code):** for document rewrites, a loose "rewrite this"
prompt may return a change summary instead of the full file. Instead: pipe the
file in, and demand `Return ONLY the full revised markdown document. No intro,
no explanation, no code fences. Start immediately with '# Title'.` Verify the
first lines with `read_file()` before overwriting the destination.

## PR Review Patterns

### Quick Review (Headless)

```
terminal(command="cd /path/to/repo && git diff main...feature-branch | grok --no-auto-update -p 'Review this diff for bugs, security issues, and style problems. Be thorough.'", timeout=120)
```

### Clone-to-temp Review (safe, no repo mutation)

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && grok --no-auto-update -p 'Review the changes vs origin/main. Check bugs, security, race conditions, missing tests.'", pty=true, timeout=300)
```

### Post the review

```
terminal(command="gh pr comment 42 --body '<review text>'", workdir="/path/to/repo")
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# Launch Grok headless in each (background)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, notify_on_complete=true)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, notify_on_complete=true)

# Monitor
process(action="list")

# After completion: push and open PRs
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Useful Subcommands & TUI Commands

| Command | Purpose |
|---------|---------|
| `grok` | Start the interactive TUI |
| `grok -p "query"` | Headless one-shot |
| `grok login` / `grok logout` | Sign in / out (SuperGrok / X Premium+ OAuth) |
| `grok inspect` | Show what Grok discovered in cwd: config sources, instructions, skills, plugins, hooks, MCP servers |
| `grok agent stdio` | Run as an ACP agent over JSON-RPC (for IDE/tool integration) |
| `grok models` | List available models and exit |
| `grok mcp` | Manage MCP server configurations |
| `grok sessions` | List, search, or restore sessions |
| `grok export` | Export a session transcript as Markdown |
| `grok worktree` | Manage git worktrees created with `-w` |
| `grok update` | Update the CLI (needs the `x.ai` host; skip in automation) |

TUI slash commands (interactive only): `/model <name>`, `/always-approve`,
`/plan`, `/context`, `/compact`, `/resume`, `/sessions`, `/fork`, `/usage`,
`/quit`. `Shift+Tab` cycles session modes (including Plan mode, which blocks
write tools except the session plan file).

## Config (`~/.grok/config.toml`)

```toml
[cli]
auto_update = false          # skip background update checks persistently

[ui]
permission_mode = "ask"      # or "always-approve" to skip tool prompts by default

[models]
default = "grok-build-0.1"
```

Put global preferences in `~/.grok/config.toml` (not project-scoped
`.grok/config.toml`). `permission_mode` supersedes the legacy `approval_mode` /
`yolo = true` keys.

## Pitfalls & Gotchas

1. **Auth is subscription-gated.** `grok login` requires a SuperGrok or X
   Premium+ subscription. If login fails or there's no `~/.grok/auth.json`,
   confirm the subscription is active before falling back to `XAI_API_KEY`.
2. **Don't conflate Hermes' xAI auth with the `grok` CLI's auth.** Hermes'
   `x_search` runs on its own xAI OAuth; the standalone `grok` CLI has a
   separate token in `~/.grok/auth.json`. A working `x_search` does NOT mean
   `grok` is logged in.
3. **Always pass `--no-auto-update` in automation** — otherwise Grok phones home
   for update checks (and `x.ai`/`storage.googleapis.com` may be unreachable).
4. **Prefer npm install over the curl installer** — `npm install -g
   @xai-official/grok` avoids the Cloudflare-walled `x.ai` host.
5. **`--always-approve` is the autonomous-build switch.** Without it, headless
   runs may stall waiting on tool-approval prompts. Omit it deliberately for
   read-only review/audit work so Grok can't mutate files.
6. **Headless `-p` skips TUI dialogs**; the TUI needs `pty=true` (+ tmux for
   monitoring), just like Claude Code.
7. **Use `--no-alt-screen`** if you run the TUI inline and the fullscreen
   alt-screen takeover garbles captured output.
8. **No git repo needed**, but for PR/commit workflows you still want one — use
   `mktemp -d && git init` for scratch commit tasks.
9. **Clean up tmux sessions** with `tmux kill-session -t <name>` when done.

## Rules for Hermes Agents

1. **Prefer headless `-p`** for single tasks — cleanest integration, structured
   output via `--output-format json`.
2. **Always set `workdir`** (or `--cwd`) so Grok targets the right project.
3. **Pass `--no-auto-update`** in every automated invocation.
4. **Use `--always-approve` only when Grok should write autonomously**; omit it
   for read-only reviews and audits.
5. **Background long tasks** with `background=true, notify_on_complete=true` and
   monitor via the `process` tool.
6. **Use tmux for multi-turn interactive work** and monitor with
   `tmux capture-pane -t <session> -p -S -50`.
7. **Verify auth before relying on it** — check `~/.grok/auth.json` or run a
   cheap `grok -p "Say ok."` smoke test; don't assume Hermes' xAI auth carries
   over.
8. **Report results to the user** — summarize what Grok changed and what's left.
