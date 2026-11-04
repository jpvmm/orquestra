# The retro cycle: scheduling, proof and shadow run

`fleet-retro-cycle` is the one command of the self-improvement loop: due check → lint → generation (one model call) →
judge (one paired call) → apply → probation pass → quiet notice. An empty cycle costs 0 model calls. This page covers
how it is scheduled, how to PROVE the scheduler exists, and how to rehearse a cycle on a copy of your real data.

Nothing in this repository creates a routine or a cron entry. You (or your agent) do that once, by hand.

## Scheduling the 2-day batch

The batch runs every two days, early in the morning, in the owner's time zone:

```
0 6 */2 * *     fleet-retro-cycle
```

(`0 6 */2 * *` = 06:00 on every second day of the month.) Pick ONE of these ways to fire it:

* **A routine of your agent** (for example a Grok Bot / agent routine) with that schedule, in your time zone, whose
  action is to run `fleet-retro-cycle` and forward its stdout (the notice) to you. A quiet cycle prints nothing.
* **System cron** on the machine that holds the Orquestra home:
  `0 6 */2 * * /path/to/fleet/bin/fleet-retro-cycle >> "$HOME/.orquestra-retro-cycle.log" 2>&1`
  (set `ORQUESTRA_HOME`/`FLEET_ROOT` in the crontab if they are not the defaults; cron has a minimal `PATH`, so use
  absolute paths and make sure `vigia-run` is reachable through `VIGIA_BIN`).

Why `*/2` is fine even though it is uneven: it fires on day 31 and on day 1 back to back at the turn of a 31-day month.
The cycle's due check (`cycle.due_hours`, default 48 h since `last_cycle` in `skill/retro-state.json`) absorbs it: the
second invocation prints `not due: last cycle 6.0h ago (< 48h)` and does nothing. A second invocation while one is still
running exits 5 (the batch lock). `--force` skips the due check (manual runs).

If the scheduler dies, the next real cycle prints `retro: scheduler late: Nh` once the last cycle is older than
`cycle.late_hours` (default 60 h), so a dead scheduler becomes visible.

## PROOF that the scheduler exists

A cron line written in a note is not a scheduler. After the FIRST scheduled time has passed, check:

1. `skill/retro-state.json` exists in the fleet root and its `last_cycle` is a timestamp from that run;
2. when there was work, `skill/retro-batches/<stamp>/usage.json` is NEW (its `<stamp>` is after the scheduled time);
3. a Vigia event `retro-batch-*` (the generation run's name is `retro-batch-<stamp>`) shows up in the Vigia log/events.

```
ls -l "$FLEET_ROOT/skill/retro-state.json"; grep last_cycle "$FLEET_ROOT/skill/retro-state.json"
ls -t "$FLEET_ROOT"/skill/retro-batches | head -3
```

An empty cycle (nothing finished since the last one) writes only `retro-state.json` — no batch folder, no model call, no
Vigia event — so for the proof, run it when at least one task folder has finished since the previous cycle, or accept
`last_cycle` moving as the sign of life of an idle period. **If `retro-state.json` does not appear (or `last_cycle` does
not move) after the first scheduled time, the scheduler does not exist**: fix the routine/cron before trusting the loop.

## Shadow run: rehearse a cycle on a copy

```
fleet-retro-shadow --source "$ORQUESTRA_HOME" --dest "$(mktemp -d)"            # no model call at all
fleet-retro-shadow --source "$ORQUESTRA_HOME" --dest "$(mktemp -d)" --with-model  # the ONE generation call
```

It copies only the task files the cycle reads (`plan.md`, `progress.md`, `criteria.json`, `FINAL-SUMMARY.md`,
`runs/*.status.json`, `runs/*.events.jsonl`, `runs/*.stderr.log`), `fleet/skill/**` (the `archive/` and `backups/` folders
are left out when larger than 5 MB) and the lessons files. It never copies and never opens a `vigia/` folder, a
`webhook.env` or any `*.env` file, a symlink or a lock file. It then runs
`fleet-retro-cycle --dry-run --force --json` with `FLEET_ROOT`/`ORQ_ROOT` pointing at the copy, so nothing is applied to
the source (the source tree is only read). To skip the runs that are still going, point `VIGIA_ATIVOS` at the real active
registry before calling it (it is only read).

Reading the numbers it prints:

* `N folder(s) selected, estimated T tokens (budget left B)`: what a real cycle would send now; `T` is the reservation
  (chars/4 × 1.25 + the output allowance) and must be ≤ `budget.tokens_per_cycle` (default 25 000).
* `model calls`, `real tokens`: 0 without `--with-model`; with it, the generation call's real usage from the stream-json
  `result` (input + output + cache). `would judge N` counts the proposals that would reach the judge.
* `history (K past batch(es) in usage.json)`: the sums over the copied `skill/retro-batches/*/usage.json`: total tokens and
  calls of the past cycles, `would_suppress` (near-identical candidates the judge memory WOULD have suppressed if
  `judge_memory.near_suppress` were on — it is off) and `blocked` (exact repeats blocked with 0 calls). A high
  `would_suppress` is the evidence for or against turning that switch on.

`--json` prints the same numbers as a JSON object. The dry-run report and its model run stay inside
`<dest>/shadow-out`. Remove `<dest>` when you are done.

## Budget reservations are crash-safe

Every generation call is reserved first and tracked in `skill/retro-state.json` → `outstanding_reservations`
(run name, batch, `reserved` = the estimate, `charged`, `created`). The entry is written **before** the run is launched,
with `charged: 0` (nothing is durably debited yet), so a crash or kill during the wait never loses the reservation.

The debit becomes durable only in the cycle's end-of-cycle state write (ONE atomic write: temp file + `os.replace`):
a run that finished inside its cycle has its entry dropped there (an overshoot is in `budget_carry`); a run still running
gets `charged` = what the cycle charged. A crash before that write leaves `charged: 0`.

At the start of every real cycle (never in `--dry-run`) leftovers are reconciled; the charge and the removal of the entry
are again ONE atomic write, so a rerun after a crash at any point never double-charges and never loses a charge:

* status final → real usage (events `result`; the reserved amount when there is none) minus `charged` is carried
  (`late_reconciled_tokens` in `usage.json`);
* status file missing or invalid JSON → the entry is **not** treated as final: it stays outstanding (counted at its
  reserved amount), unless the events log has a final `result` with usage, which is then used;
* an entry older than `budget.outstanding_max_age_hours` (default `168`, in `retro-policy.json`) is dropped; `reserved -
  charged` is charged and nothing is ever refunded. The record `{name, batch, reserved, charged_now, expired_at}` is
  written to retro-state `expired_reservations` (last 20) in the same write, and to `usage.json` when one is written.
  Expiry also **retires the entry's batch**: it is finalized as failed (exactly like a generation error), the task state is
  not advanced and its task dirs are selected again; the batch is retired before the entry is dropped, so a crash in between
  is harmless on rerun. A status file that is missing/broken but whose events log has a final `result` counts as final, and
  `fleet-retro --batch --collect` / the cycle collect the batch from its events. An uncollected batch with no outstanding
  entry (older version) and a bad/missing status is retired by the same age limit, from the manifest `started`.
  An otherwise empty cycle writes only retro-state (and carries the charge in `budget_carry`).
