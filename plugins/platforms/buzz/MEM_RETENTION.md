# Buzz `mem` (engrams): retention, tombstone, and cleanup norms

Worker-facing runbook for the `buzz mem` subcommand — persistent cross-session
scratch on a Buzz relay (Nostr NIP-AE engrams). This covers *how long entries
live*, *when to tombstone*, and *cleanup hygiene*. The companion deny-list
(what must never be written) lives in the parent task and is linked at the
bottom.

## What `buzz mem` is

`buzz mem` is a community-relay-backed key/value store keyed by **slug**
(e.g. `sl/last-green-sha`). It is shared across sessions and across workers on
the same relay identity, so it is safe for non-secret ops state that several
agents read. It is **not** end-to-end encrypted and is **not** a substitute for
Hermes memory (PMB) — keep substantive knowledge in Hermes memory; use mem for
cheap, disposable, shared markers.

```bash
buzz mem ls                         # list non-tombstoned entries
buzz mem get <slug>                 # print a slug's current value
buzz mem set <slug> <value>         # overwrite a slug (or `-` to read stdin)
buzz mem patch <slug>               # apply a unified diff (safer than set)
buzz mem rm <slug>                  # publish a tombstone (mark deprecated)
buzz mem hash <slug>                # sha256 of current value (for patch --base-hash)
```

Scoping: every subcommand accepts `--owner <hex>` and `get/ls/hash` also accept
`--agent <hex>` to read as a key's owner/agent. Use these when acting on behalf
of a shared ops identity rather than your own key.

## Retention — how long entries persist

- **There is no TTL or expiry in the tool.** `mem set`/`mem patch` have no
  lifetime flag. An entry lives **indefinitely** from the tool's perspective.
- Entries persist until one of:
  1. **Tombstoned** — `buzz mem rm <slug>` publishes a tombstone event
     (see below). The value is no longer returned by `mem ls`/`get`, but the
     tombstone marker itself stays on the relay.
  2. **Pruned by the relay** — the relay operator's retention policy can drop
     events (Nostr relays are not guaranteed permanent storage). Treat mem as
     *best-effort, not archival*. Do not put anything here you cannot
     reconstruct.
- **The tombstone is permanent and one-way.** `mem rm` *publishes* a tombstone;
  it does not delete bytes. You cannot un-tombstone. `core` is protected — `mem
  rm core` is rejected, so the canonical anchor slug can never be tombstoned.
- **Non-secret ops slugs persist until you tombstone them.** Examples from the
  parent task — `sl/last-green-sha`, `sl/last-deploy-class`,
  `fw/actions-budget-note` — stay live across sessions until explicitly
  deprecated. Set a value, forget it, and it keeps returning that value
  forever; that is the trap.

## When to tombstone

Tombstone (deprecate) a slug with `buzz mem rm <slug>` when:

- It is **superseded** — a newer value replaced it under a different slug, or
  the same slug now means something else and stale readers would be misled.
- It is **wrong** — the stored value is incorrect and you do not want it
  consulted again.
- It is **stale / no longer used** — e.g. `sl/last-green-sha` points at a
  branch you have since force-pushed or abandoned. A stale SHA is worse than no
  SHA: a worker that trusts it will check out the wrong commit.
- You want it **gone from `mem ls`** without relying on a relay prune — the
  tombstone removes it from the readable set immediately.

Do **not** tombstone:

- `core` — protected by the CLI.
- An entry another worker is actively relying on, without announcing it. Mem is
  shared; tombstoning mid-flight can break a teammate's run.

## Cleanup norms

1. **Tombstone, don't zero.** Prefer `buzz mem rm <slug>` over `buzz mem set
   <slug> ""`. A tombstone clearly signals "deprecated" and drops the slug from
   `mem ls`; an empty value leaves a live, misleading entry that still shows up
   and reads as "no info" rather than "don't use."
2. **Namespace your slugs.** Use a stable prefix so tombstones stay scannable:
   `sl/` for source/CI state, `fw/` for ForgeWOD ops notes, `relay/` for
   relay-specific markers. Avoid bare, ambiguous slugs.
3. **Rotate, don't accumulate.** Mem is scratch, not a log. When a value is
   replaced, tombstone the prior slug if it is no longer referenced. Periodically
   run `buzz mem ls` and tombstone anything you would not want a fresh worker to
   trust.
4. **Read before you write blindly.** `buzz mem set` overwrites with no
   conflict check. If another worker may have updated the slug since you last
   read it, use the safe-edit path instead (below) so you don't clobber their
   change.
5. **Never store a secret as a value.** See the deny-list. If a slug that
   *should* be secret ever lands in mem, treat it as compromised: tombstone it
   and rotate the credential. Tombstoning does not redact history, so assume the
   value leaked.

## Safe concurrent editing (avoid clobber)

`mem set` is a blind overwrite. When multiple workers may touch the same slug,
use the diff path:

```bash
buzz mem hash <slug> > base.sha     # capture sha256 of current value
# edit a local copy of `buzz mem get <slug>` into next.txt
diff -u <(buzz mem get <slug>) next.txt | buzz mem patch <slug> --base-hash "$(cat base.sha)"
```

`patch` refuses to apply if the slug changed since `base-hash` was captured, or
if a hunk's context doesn't match verbatim — so concurrent edits conflict
loudly instead of silently overwriting. Only pass `--no-base-hash` for
single-writer, fire-and-forget updates; never under concurrency.

## Deny-list — what must never go on Buzz mem

Buzz mem is a **hosted community relay, not end-to-end encrypted**. The
following are forbidden as slug values (or as any part of a value):

- Secret material: nsec, private keys, API tokens, OAuth/client secrets.
- Relay or gateway credentials of any kind.
- Thesis drafts, unpublished research, or anything under embargo.
- Private personal data (addresses, contacts, health, finances).
- Anything whose exposure would let another community member impersonate or
  pivot into Alex's systems.

Allowed: non-secret ops markers only — build SHAs, deploy class, budget notes,
last-known-good references, shared runbook pointers. When in doubt, it belongs
in Hermes memory (PMB) or a private file, not on the relay.

Full deny-list and the search-before-claiming runbook: parent task
**t_f10c18d0** ("Easy win: Buzz search + mem as agent tools (non-secret)").
