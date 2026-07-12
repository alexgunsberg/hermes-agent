# Event-Driven Performance & Usage Monitoring

> **Status:** design proposal
> **Audience:** Hermes operators and contributors working on the agent loop, gateway, and cron
> **Related:** `docs/observability/README.md` (observer hook contract),
> `docs/chronos-managed-cron-contract.md` (scale-to-zero precedent),
> `agent/insights.py` (existing usage analytics)

## Problem

Hermes deployments degrade gradually and invisibly. By the time the operator
notices, median platform turns take minutes instead of seconds. A 24-hour
diagnostic on a live deployment found the raw model path healthy (1.3–1.5s raw
API latency on a probe; 9.1s median across 4,175 real calls) while real
Telegram turns took a **median of 2m53s** and a p90 of **24m32s**. The gap was
not the provider — it was accumulated state and unbounded behavior:

1. **Oversized per-route context.** 14 Telegram routes above 50k tokens, the
   largest at ~247k; mean API input 115.6k tokens; 4,135 of 4,175 calls tripped
   the large-context TTFB watchdog.
2. **Blocking lifecycle hooks.** A synchronous hook to an unreachable bridge
   timed out 271 times in a day (5s timeout each, ~23 min cumulative), and its
   audit log grew to 845 MB because it logged full payloads on every event —
   including failures.
3. **Tool over-calling and loop amplification.** 6,921 tool calls with a 6.5%
   error rate; failed terminal/patch/skill operations trigger more model turns,
   which grow the context, which slows the next call.
4. **Compression failing exactly when needed.** Auxiliary compression routed
   through a rate-limited quota failed at ~300k-token contexts, so oversized
   sessions kept operating oversized instead of shrinking.
5. **Request overhead.** 30–49 tool schemas per request (~12k tokens mean,
   ~20k max) plus repeated skill loads (541/day; one skill loaded 171 times)
   and unbounded tool output (~59M chars/day; a single `search_files` result
   returned ~10 MB).
6. **Storage-layer decay** (from code inspection, not the field report):
   FTS5 segment fragmentation in `state.db` (`hermes_state.py:893-902`), WAL
   write-lock convoys across gateway/CLI/cron processes
   (`hermes_state.py:880`), and an uncapped skills index in the system prompt
   (`agent/prompt_builder.py:1417`).

None of these is a single acute failure. They are slow trends, which is why
they surface as "woke up and it's unusable." Hermes records per-session token
and cost totals (`sessions` table, `hermes_state.py:718-737`) and can report
on them (`agent/insights.py`), but it does not record **latency**, does not
track **size trends**, and enforces only an **iteration** budget — never a
token, context-size, or hook-time budget.

## Design principles

- **Event-driven, never clock-driven.** No polling process, no periodic
  ticker. When Hermes is idle, the monitor consumes nothing. This matches the
  Chronos managed-cron philosophy (scale to zero, wake only on a genuine
  event).
- **Instrument at the existing seam.** The observer hook contract
  (`pre/post_api_request`, `pre/post_tool_call`, session lifecycle hooks)
  already carries durations, token usage, request character counts, message
  counts, and tool counts. The monitor is a first-party, always-on consumer of
  hooks that already fire — not a new instrumentation layer.
- **Enforce bounds inline, in-process.** Budget checks run inside the agent
  loop at the moment the event completes. No separate watchdog process.
- **Summarize lazily.** Reports are queries over locally persisted events,
  generated on demand or on next use after idleness. An idle fortnight
  produces "no material usage," not fourteen days of empty reports.
- **Detection without repair is half a fix.** For decay-type problems
  (fragmentation, log growth, stale route pins), the monitor pairs each alert
  with a maintenance action it can run or propose.
- **All data stays local and operator-owned.** Everything lands in the SQLite
  store Hermes already owns. External telemetry (Langfuse, OTel) remains an
  optional plugin consuming the same hooks.

The architecture in one line:

> **Instrument usage events → enforce bounds immediately → run maintenance
> when trends degrade → summarize lazily on next use.**

## Architecture

```
model request completes ──┐
tool call completes ──────┤   observer hooks      ┌─ bound checks (inline, same turn)
cron run executes ────────┼──► monitor recorder ──┼─ turn_metrics row (async write)
hook/compression event ───┤   (fail-open)         └─ alert queue (on crossing)
session start ────────────┘
        │
        └─► startup gauges (sampled once per process start, not polled)
                 │
                 └─► catch-up report + maintenance proposals (lazy, on next use)
```

### 1. Event recording (`turn_metrics`)

A new table in `state.db`, written asynchronously off the hot path (batched
with the existing write-retry/checkpoint machinery in `hermes_state.py`):

| Column | Source |
| --- | --- |
| `ts`, `session_id`, `turn_id`, `profile`, `platform`, `origin` (`interactive` \| `cron` \| `subagent`) | hook correlation IDs |
| `api_duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `context_tokens` | `post_api_request` |
| `tool_name`, `tool_duration_ms`, `tool_status`, `tool_output_chars` | `post_tool_call` |
| `hook_name`, `hook_duration_ms`, `hook_timed_out` | lifecycle hook dispatch wrapper |
| `compression_attempted`, `compression_ok`, `tokens_before`, `tokens_after` | compression pipeline |
| `schema_bytes`, `tool_count`, `system_prompt_chars` | `pre_api_request` |

This is the piece Hermes is missing today: token **totals** exist, but
latency, context size per call, hook cost, and compression outcomes are
observable only through optional plugins. Every finding in the field report
above was reconstructed by hand from logs; each becomes a one-line SQL query
over `turn_metrics`.

Retention is bounded by design: raw rows are rolled up into daily aggregates
after N days (default 14) and deleted, so the monitor cannot itself become a
growth vector.

### 2. Inline bound enforcement

Checked in-process at event boundaries. Each bound has a **soft** threshold
(record + queue an alert) and a **hard** threshold (stop, with different
semantics by origin):

| Bound | Checked at | Hard-stop behavior |
| --- | --- | --- |
| Per-turn token ceiling | each `post_api_request` | interactive: alert only; cron/subagent: terminate run |
| Per-session token ceiling | each `post_api_request` | alert + force compression; refuse further turns only if compression fails |
| Per-cron-run token + iteration ceiling | each loop iteration | terminate run, queue report (extends the existing `cron.max_iterations`) |
| Per-profile rolling usage ceiling | each turn end | alert only |
| Context-size ceiling | `pre_api_request` | force compression before send; alert if compression fails or is skipped |
| Tool runtime / error-rate threshold | `post_tool_call` | alert; optionally block repeat calls of a tool failing >X% in a window |
| Hook time budget | hook dispatch | demote hook to async/fail-fast after K consecutive timeouts |
| Cumulative usage since last checkpoint | turn end | alert only |

Two deliberate asymmetries:

- **Interactive sessions never hard-stop mid-task on a token bound.** A hard
  stop while the operator is mid-conversation is a quality regression; the
  bound alerts and forces compression instead. Hard stops are reserved for
  unattended work (cron, subagents), where the field report showed the real
  runaway risk — a single cron owner burned ~42M tokens across 261 calls,
  repeatedly hitting the 90-iteration cap, which bounded call *count* but not
  cost. Token ceilings close that gap.
- **The context-size ceiling is enforced *before* the request, not after.**
  The field data shows the dominant latency cause was sending 115k–297k-token
  contexts on nearly every call. Preventing the oversized send (by forcing
  compression, or alerting when compression cannot run) attacks the cause;
  a post-hoc token alert only describes it.

### 3. Health gauges (sampled at startup, not polled)

Cheap measurements taken once per process start and appended to a `gauges`
table, so trends are visible across restarts with zero idle cost:

- `state.db` size and FTS segment count (the self-documented decay mode at
  `hermes_state.py:893`)
- system prompt size and skills-index entry count (the one uncapped prompt
  component, `agent/prompt_builder.py:1417`)
- per-route pinned context sizes (the #1 field finding)
- hook audit / log file sizes (the 845 MB finding)
- memories directory size, per memory layer (see below)
- mean tool-schema bytes per request from the last aggregate window

### 4. Maintenance actions

Each gauge or trend crossing maps to a concrete action. Cheap, safe actions
run automatically; anything destructive or behavior-visible is proposed in
the catch-up report and requires operator confirmation:

| Signal | Automatic | Proposed |
| --- | --- | --- |
| FTS segment count above threshold | `INSERT INTO messages_fts VALUES('optimize')` + WAL checkpoint at idle moment | — |
| Hook audit / log file oversized | rotate + compact | — |
| Route pin above context ceiling | — | prune the route pin (backup first; route entry only, never transcripts) |
| Compression failure streak | — | reroute auxiliary compression to a fallback model |
| Skills index above size threshold | — | curation pass (archive unused skills; keep relevance, don't blind-cap) |
| Tool schema payload above threshold | — | trim MCP tool exposure for that surface |
| Repeated same-skill loads in one session | dedupe: serve from session cache | — |

The pruning/rerouting actions stay proposals because the field report's own
remediation showed they need judgment (backups, external gateway restarts,
`/new` on affected topics).

### 5. Catch-up report (lazy)

Generated on the first turn after an idle gap exceeding a threshold, or on
demand (an `insights`-style command). Contents: usage since last report, top
token consumers, longest-running tools, largest contexts, failed/repeated
loops, budget crossings, latency trend deltas vs. the previous comparable
period, gauge trends, and pending maintenance proposals. If nothing ran, the
report is a single line: no material usage. Background work is the one
exception: a cron run that fires while the operator is away enforces its own
bounds and queues its alerts/report for this same catch-up channel.

## Memory layers and ownership

The monitor treats memory as three layers with different owners and different
performance characteristics, and instruments each without ever holding the
content:

1. **Built-in memory** — `MEMORY.md` / `USER.md`, hard-capped
   (2200/1375 chars, `tools/memory_tool.py:130`) and injected into the system
   prompt. Already bounded; the gauge just confirms it stays that way.
2. **Holographic memory (PMB)** — the operator's primary memory bank, living
   in a separate ops repository outside this codebase. From this repo's
   perspective it is an external memory provider: what the monitor owns is the
   `prefetch()` path (`agent/memory_provider.py:94`), which runs **before
   every API call** and is therefore on the latency-critical path. The monitor
   records per-layer prefetch latency and injected-context size, so a slow or
   bloated provider shows up as a named line in the report rather than as
   diffuse turn slowness.
3. **Accumulating cross-service memory** — memory aggregated across all AI
   services the operator uses. Same treatment as layer 2 at the seam: latency
   and injected size per call, growth gauge at startup.

The ownership invariant: all monitor data, like all memory content, stays in
operator-owned local storage. Instrumentation records sizes, durations, and
counts — never memory content — so the monitor can be exported or wiped with
the rest of the store.

## What this design does *not* do

- No 15-minute (or any) polling timer; no always-on watchdog process.
- No token cost during inactive periods; the monitor itself makes no model
  calls except optionally when composing the catch-up summary text.
- No replacement of provider requests, tool arguments, or execution semantics
  — the recorder is a read-only observer, consistent with the observer-hook
  contract's fail-open rule.
- No external telemetry dependency; OTel/Langfuse remain optional plugins on
  the same hooks.

## Phasing

1. **Record + report** — `turn_metrics` recorder, startup gauges, catch-up
   report. Pure observation; no behavior change. This alone would have
   surfaced every finding in the field report as it developed.
2. **Bounds** — token/context/hook budgets with soft alerts everywhere and
   hard stops for cron/subagents. Extends the existing iteration budget and
   the cron caps merged in the latency-remediation work.
3. **Maintenance** — automatic FTS optimize/log rotation; proposal flow for
   route pruning, compression rerouting, and skills curation.

Each phase is independently shippable and independently valuable; phase 1
carries no behavioral risk at all.
