---
title: Performance metrics evolution loop
sidebar_position: 22
---

# Performance metrics evolution loop

Hermes includes a local, low-overhead performance metrics path for tracking whether routine agent operations are getting slower over time.

It is intentionally implemented as a normal Python module plus script, not as a model tool. That keeps the always-on prompt/tool schema unchanged.

## What it records

The store lives at:

```bash
~/.hermes/performance_metrics/metrics.db
```

It stores counts, timings, statuses, and byte sizes only. It redacts sensitive-looking fields before writing durable details and does not store prompts, task bodies, comments, logs, environment values, or memory contents.

Each run covers these twelve scoped timing areas as either measured, skipped with a reason, or pending instrumentation:

1. `python_startup`
2. `hermes_cli_startup`
3. `gateway_status`
4. `simple_chat_help`
5. `live_chat_no_tools`
6. `tool_schema_collection`
7. `active_memory_scan`
8. `pmb_packet_scan`
9. `compression_warning_scan`
10. `cron_job_scan`
11. `kanban_process_timer_scan`
12. `kanban_ownership_handoff_scan`

`live_chat_no_tools` is skipped by default because it spends a model call. Pass `--live-chat` only when you deliberately want that measurement.

## Manual run

From the Hermes source checkout:

```bash
scripts/hermes_performance_watchdog.py --print-report
```

For a one-shot context/token/tool-schema scorecard:

```bash
scripts/hermes_speed_scorecard.py --kanban-board <board-slug>
```

The scorecard writes Markdown and JSON under `~/.hermes/reports/`. It captures
the same non-secret timing proxies plus active memory bytes, PMB packet bytes,
loaded-skill frequency, top prompt-heavy skill/tool schema contributors, active
session counts, gateway route-pin counts, high `last_prompt_tokens` route pins,
compression-warning counts, cron counts, and Kanban ownership/process-timer
counters. It also emits low-risk fast-path recommendations: scoped toolsets,
linked-file/on-demand skill loading, PMB pointers instead of always-injected
memory, and fresh-session/preflight packet policy for high-token route pins.
It does not print prompts, logs, memory contents, card bodies, routing keys,
environment variables, or secret values.

For JSON:

```bash
scripts/hermes_performance_watchdog.py --json
```

The script is silent when no regression crosses the configured threshold unless `--json` or `--print-report` is passed. This makes it safe for no-agent cron/watchdog use.

## Regression detection

The analyzer computes per-area medians from earlier observations and compares them to the recent window. A regression is actionable only when both conditions hold:

- recent median / baseline median is at least `--threshold-ratio` (default `1.5`), and
- recent median minus baseline median is at least `--min-delta-s` (default `1.0`).

Below-threshold changes are suppressed; they do not create cards and produce no cron output in silent mode.

## Kanban follow-up creation

To create deduped goal-mode Kanban cards for above-threshold regressions:

```bash
scripts/hermes_performance_watchdog.py \
  --create-kanban-tasks \
  --board hermes-process-mastery \
  --assignee default
```

The synthetic cards include ownership markers and use an idempotency key derived from the area and regression fingerprint, so repeated watchdog runs do not duplicate the same regression card.

Use `--dry-run --print-report` first if you want to inspect the planned actions without mutating Kanban.

## Citing metrics evidence in hardening cards

Future hardening cards created from this loop should cite the metrics evidence directly instead of saying only that Hermes “felt slow.” Include:

- the timing area name,
- the source (`performance_metrics.compute_baseline_and_trends` or the JSON report path),
- the report `generated_at` timestamp,
- baseline median, recent median, delta, ratio, and threshold settings, and
- whether the card came from the synthetic watchdog or a manual review.

The synthetic regression cards include this citation block automatically. Manually-created performance hardening cards should copy the same fields from `scripts/hermes_performance_watchdog.py --json` or from the local metrics DB report.

## Cron/watchdog setup

For a quiet script-only cron job, copy or symlink the script into the Hermes cron scripts directory, then create a no-agent job that runs it periodically. Example:

```bash
mkdir -p ~/.hermes/scripts
ln -sf ~/.hermes/hermes-agent/scripts/hermes_performance_watchdog.py \
  ~/.hermes/scripts/hermes_performance_watchdog.py
hermes cron create 'every 6h' \
  --name hermes-performance-watchdog \
  --script hermes_performance_watchdog.py \
  --no-agent \
  --deliver local
```

Keep `--deliver local` unless you explicitly want a configured messaging gateway to receive regression alerts. Empty stdout means no notification.

## Rollback / disable

Disable the watchdog without deleting history:

```bash
hermes cron list
hermes cron pause <job_id>
```

Remove it entirely:

```bash
hermes cron remove <job_id>
```

Remove only the metrics history:

```bash
rm -rf ~/.hermes/performance_metrics
```

Remove the script symlink:

```bash
rm -f ~/.hermes/scripts/hermes_performance_watchdog.py
```

No gateway restart is required for any of these rollback steps unless you separately changed gateway configuration.
