---
name: cursor
description: "Delegate coding to Cursor Agent CLI (features, PRs, reviews)."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Cursor, Code-Review, Refactoring, Automation]
    related_skills: [grok, codex, claude-code, coding-agent-routing]
---

# Cursor Agent CLI — Hermes Orchestration Guide

Delegate coding tasks to [Cursor Agent](https://cursor.com/docs/cli/overview)
(Anysphere's coding agent CLI, the `cursor-agent` command) via the Hermes
terminal. Cursor Agent can read files, edit code, run shell commands, and use
MCP servers, with access to frontier models from multiple labs (Anthropic,
OpenAI, Google, xAI, and Cursor's own Composer models) behind one CLI.

This is a sibling to `grok`, `codex`, and `claude-code`. The orchestration
pattern is the same — **prefer headless print mode for one-shots**, use a PTY
for interactive sessions. See the `coding-agent-routing` skill for when to pick
Cursor over its siblings.

## When to use

- Building features and refactoring, especially when you want to pick the
  underlying model per task (`--model gpt-5`, `--model sonnet-4.5`, Composer
  for speed)
- PR reviews and batch issue fixing
- Repos that already carry `.cursor/rules` or Cursor project config — the CLI
  picks these up natively (it also reads `AGENTS.md`)

## Prerequisites

- **Install:** `curl https://cursor.com/install -fsS | bash` → installs
  `cursor-agent` (with an `agent` alias in recent versions) into `~/.local/bin`.
  - The `cursor.com` / `downloads.cursor.com` hosts are Cloudflare-fronted and
    may be unreachable in locked-down or proxied environments. Unlike Grok,
    there is **no official npm fallback** — the npm packages named
    `cursor-agent` are unrelated third-party projects; do not install them.
- **Auth — two paths:**
  - Interactive: `cursor-agent login` (browser OAuth against your Cursor
    subscription). Check state with `cursor-agent status`.
  - Headless/CI: set the `CURSOR_API_KEY` environment variable (a Cursor User
    API key from cursor.com dashboard). This is the path for automation.
- Usage is billed against the user's Cursor plan; heavy autonomous runs burn
  plan credits. Surface this before launching large batches.

## Two Orchestration Modes

### Mode 1: Headless print mode (PREFERRED)

Runs a one-shot task, prints the result, and exits. The prompt is a positional
argument; `-p` / `--print` enables non-interactive mode (it is also inferred
when stdout is not a TTY).

```
terminal(command="cursor-agent -p 'Add a dark mode toggle to settings' --force --output-format text", workdir="/path/to/project", timeout=300)
```

**When to use headless:**
- One-shot coding tasks (fix a bug, add a feature, refactor)
- CI/CD automation and scripting
- Structured output parsing with `--output-format json`

### Mode 2: Interactive PTY — Multi-Turn TUI Sessions

The TUI is a fullscreen app. Drive it with `pty=true`; for robust
monitoring/input use tmux, exactly like the `claude-code` / `codex` / `grok`
skills:

```
terminal(command="tmux new-session -d -s cursor-work -x 140 -y 40")
terminal(command="tmux send-keys -t cursor-work 'cd /path/to/project && cursor-agent' Enter")
terminal(command="sleep 5 && tmux send-keys -t cursor-work 'Refactor the auth module to use JWT' Enter")
terminal(command="sleep 15 && tmux capture-pane -t cursor-work -p -S -50")
terminal(command="tmux kill-session -t cursor-work")
```

## Headless Deep Dive

### Common Flags

| Flag | Effect |
|------|--------|
| `-p, --print` | Non-interactive: print the response and exit |
| `--force` | Allow unattended file modifications (the `--always-approve` / `--full-auto` equivalent) |
| `-m, --model <MODEL>` | Choose a model (e.g. `gpt-5`, `sonnet-4.5`, `opus`, Composer) |
| `--output-format <FMT>` | `text`, `json`, or `stream-json` (in print mode the default is `stream-json`) |
| `--resume <ID>` | Resume a specific session |
| `--continue` | Continue the most recent session in this directory |
| `-a, --api-key <KEY>` | Pass an API key explicitly (prefer `CURSOR_API_KEY` env) |

### Output Formats

- `text` — final response text only; cleanest for logs and note pipelines
- `json` — exactly one JSON object on completion; parse the result cleanly
- `stream-json` — newline-delimited JSON events as they arrive (default in
  print mode — pass `--output-format text` explicitly if you want plain logs)

```
# Structured single result for parsing
terminal(command="cursor-agent -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="cursor-agent -p 'Refactor the database layer and run the tests' --force --output-format text", workdir="/project", timeout=600)
```

### Background Mode (Long Tasks)

```
terminal(command="cursor-agent -p 'Refactor the auth module' --force --output-format text", workdir="/project", background=true, notify_on_complete=true)
# Returns session_id

process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")
process(action="kill", session_id="<id>")
```

### Session Continuation

```
# List past sessions
terminal(command="cursor-agent ls", workdir="/project")

# Continue the most recent session in this directory
terminal(command="cursor-agent --continue -p 'What did you change last time?' --output-format text", workdir="/project", timeout=120)

# Resume a specific session
terminal(command="cursor-agent --resume <chat-id> -p 'Now add connection pooling' --force", workdir="/project", timeout=300)
```

## PR Review Patterns

### Quick Review (Headless, read-only)

Omit `--force` so the agent cannot write, and demand markdown-only output:

```
terminal(command="cd /path/to/repo && git diff main...feature-branch | cursor-agent -p 'Review this diff for bugs, security issues, and race conditions. Markdown only, no preamble.' --output-format text", timeout=300)
```

### Post the review

```
terminal(command="gh pr comment 42 --body '<review text>'", workdir="/path/to/repo")
```

## Parallel Issue Fixing with Worktrees

Cursor has no built-in worktree flag — create worktrees with git, then launch
one headless agent per worktree in the background:

```
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="cursor-agent -p 'Fix issue #78: <description>. Commit when done.' --force --output-format text", workdir="/tmp/issue-78", background=true, notify_on_complete=true)

process(action="list")

terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Useful Subcommands

| Command | Purpose |
|---------|---------|
| `cursor-agent` | Start the interactive TUI |
| `cursor-agent -p "query"` | Headless one-shot |
| `cursor-agent login` / `logout` | Sign in / out |
| `cursor-agent status` | Show auth/account state |
| `cursor-agent models` | List available models |
| `cursor-agent ls` | List sessions |
| `cursor-agent resume` | Resume the latest session |
| `cursor-agent mcp` | Manage MCP server configuration |
| `cursor-agent update` | Update the CLI (needs `cursor.com`; skip in automation) |

## Rules & Project Context

- The CLI reads `.cursor/rules` (project rules) and `AGENTS.md` automatically —
  existing Cursor-project context carries over with zero config.
- Permissions can be pinned in Cursor's CLI config (allow/deny lists for tools
  and commands), which is safer than blanket `--force` for semi-trusted tasks.

## Pitfalls & Gotchas

1. **Auth is plan-gated.** Headless use wants `CURSOR_API_KEY`; a logged-in IDE
   on the same machine does NOT authenticate the CLI. Verify with
   `cursor-agent status` before relying on it.
2. **`stream-json` is the print-mode default** — if you expect plain text and
   parse stdout naively you'll get NDJSON events. Pass `--output-format text`
   or `json` explicitly in every automated invocation.
3. **`--force` is the autonomous-build switch.** Without it, headless runs may
   stall or skip writes. Omit it deliberately for read-only review/audit work.
4. **No `--cwd` flag** — always set `workdir` on the Hermes `terminal` call so
   the agent targets the right project.
5. **Install host may be walled.** `cursor.com` is Cloudflare-fronted; in
   proxied environments the installer 403s and there is no npm fallback. Check
   reachability before promising Cursor delegation.
6. **Don't install npm packages named `cursor-agent`** — they are unrelated
   third-party squatters, not the official CLI.
7. **Clean up tmux sessions** with `tmux kill-session -t <name>` when done.

## Rules for Hermes Agents

1. **Prefer headless `-p`** with an explicit `--output-format` for single
   tasks.
2. **Always set `workdir`** — there is no `--cwd` flag.
3. **Use `--force` only when Cursor should write autonomously**; omit it for
   read-only reviews and audits.
4. **Background long tasks** with `background=true, notify_on_complete=true`
   and monitor via the `process` tool.
5. **Verify auth before relying on it** — `cursor-agent status`, or confirm
   `CURSOR_API_KEY` is set.
6. **Pick the model per task** with `-m` — Composer models for fast iteration,
   frontier reasoning models for hard refactors.
7. **Report results to the user** — summarize what Cursor changed and what's
   left.
