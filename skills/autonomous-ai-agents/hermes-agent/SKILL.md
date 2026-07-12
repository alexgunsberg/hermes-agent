---
name: hermes-agent
description: "Configure, extend, or contribute to Hermes Agent."
version: 2.4.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

Hermes is an open-source personal agent by Nous Research. The same core runs
through the CLI, Ink TUI, Electron desktop app, messaging gateway, web
dashboard, and ACP integrations. Capability belongs in tools, skills, plugins,
platform adapters, and MCP servers; the core tool surface stays deliberately
narrow. It supports many model providers, persistent memory, profiles,
scheduling, delegation, and full terminal/browser execution.

Official docs are authoritative: https://hermes-agent.nousresearch.com/docs/
Verify live behavior with `hermes --help`, `hermes <command> --help`, the
current repository, and the relevant reference below. Absence from this
overview is not evidence that a feature does not exist.

## Required reference routing

Read this file completely, then load every reference relevant to the request:

- CLI commands, slash commands, config keys, providers, credentials, toolsets,
  profiles, sessions, cron, or project context:
  `references/cli-and-configuration.md`
- Security/privacy, voice, spawning Hermes processes, delegation, cron,
  curator, kanban, surfaces, Windows behavior, or troubleshooting:
  `references/operations-and-troubleshooting.md`
- Source layout, adding tools or slash commands, agent loop, tests, commits,
  and contribution rules: `references/contributor-guide.md`
- Webhook configuration and lifecycle: `references/webhooks.md`
- Native MCP development: `references/native-mcp.md`

Do not load unrelated references merely because they exist.

## Operating rules

1. Inspect the real config and runtime before changing anything.
2. Put behavioral settings in `config.yaml`; `.env` is for credentials only.
3. Prefer existing code, then CLI+skill, service-gated tools, plugins, and MCP
   before adding a permanent core model tool.
4. Preserve the system prompt and tool schemas byte-for-byte during a
   conversation so provider prompt caching remains warm.
5. Never inject synthetic user messages mid-loop or break role alternation.
6. Use profile-aware paths and commands; profiles are isolated by design.
7. Verify changes through the real integration path, not only mocked units.

## Quick start

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup
hermes model
hermes doctor
hermes                         # interactive chat
hermes chat -q "What is the capital of France?"
hermes desktop
hermes dashboard
hermes gateway
```

When troubleshooting, collect the exact command, active profile, selected
provider/model, `hermes doctor` result, and relevant logs before proposing a
fix. When changing Hermes itself, read the repository's `AGENTS.md` first.
