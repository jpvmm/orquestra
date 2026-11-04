# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - unreleased

### Fixed
- `fleet-retro` / `fleet-retro-cycle`: abandoned batches no longer block generation. `collect_pending` treats a missing/broken
  status plus a final `result` event as final; expiry of a reservation retires its batch (finalized as failed, task state not
  advanced, before the entry is dropped); an uncollected batch without an entry is retired after `budget.outstanding_max_age_hours`.
- `fleet-retro-cycle`: budget reservations are crash-safe and idempotent. The outstanding entry is written before the
  generation run starts (`charged: 0`); the debit becomes durable in the end-of-cycle state write, and a charge and the
  removal of its entry are one atomic retro-state write. A missing or invalid status file keeps the reservation charged
  until `budget.outstanding_max_age_hours` (default 168), then it expires without refund (`expired_reservations` in
  retro-state, last 20, and in `usage.json`).
- `fleet-retro-shadow`: the source policy lookup refuses a symlinked `fleet/` or `skill/` ancestor.

### Added
- Retro loop v0.2.0: `fleet-retro-cycle` (cycle runner, `prompts/retro-routine.md`; empty cycle = 0 model calls),
  `fleet-skill-judge` (paired behavioural check, judge memory in `skill/judge-log.jsonl`), `fleet-skill-eval` (probes),
  `fleet-retro-shadow`, rules index (`skill/rules-index.json`, overlap/contradiction check), merge-guard, probation with
  automatic revert, and `skill/retro-policy.json` (owner name, quotas, budgets, cycle, judge memory, eval, probation).
- Token budget: `budget.tokens_per_cycle` hard cap (default 25000), reserved before each call, usage read from the
  stream-json result. Typical cycle estimate <= ~25k tokens.
- `install.sh` installs all loop tools and the policy; records `skill/.install-baseline.json`.
- `tests/test_oss_port.py` and the other v0.2.0 test files.
- Shadow-run numbers (T8, dry run on a copy of a real home, no model call): 20 tasks / 3 projects / 1037 files copied
  (no Vigia folder, no `.env` files), 10 tasks selected, 1 skipped as active, estimated 18,428 tokens (fits the 25k cap).

### Changed
- Re-install never overwrites a skill/config/agent file that differs from its recorded baseline (written as
  `<file>.upstream` and reported) nor any bookkeeping file (ledgers, archive, evals, judge log, rules index, retro state,
  `LESSONS.md`, `learnings.md`).
- One path resolver (`ORQ_ROOT`, then `ORQUESTRA_HOME`, then the folder above `fleet/`) for every loop tool and `fleet-run`; no `/workspace`, `/home/` or
  personal names in shipped files. `docs/fleet.md` retro section rewritten.

### Deferred (not in 0.2.0)
DSPy/GEPA, embeddings, full replay in worktrees, autonomous edits of the Orquestra profile, per-run retro, per-rule usage
counters, pruning by inactivity, 3 samples per probe, turning `judge_memory.near_suppress` on.

## [0.1.0] - 2026-11-03

### Added
- **Fleet** (`fleet/`, `docs/fleet.md`): spec-to-code runner. `fleet-run` launches a headless Claude team lead (M/G)
  or a dev + cross-reviewer pair (P) under Vigia; `fleet-wt` handles worktrees, review packages, commit-free patches,
  integration and DAG waves; `codex-dev`/`codex-reviewer` (`fleet-codex`) run Codex roles under Vigia and wait for
  them; `fleet-wait` waits for runs. Agent definitions (team-lead, dev, reviewer-claude, security) and the
  `fleet-sdd` skill (flow, briefs, templates, references). Prompts use an `@FLEET_HOME@` placeholder resolved at run
  time; every Claude run gets `--disallowedTools` for `git commit/push/merge/rebase`, `gh` and `rm -rf`.
- `prompts/vigia-routine.md`: instruction for the "Vigia" webhook routine, including the fleet follow-up.
- Vigia records what each agent received and answered: `<run>.prompt.md`, `<run>.conversation.md` and a structured
  `<run>.conversation.json` (`schema_version` 1); `--task` and `--prompt-file`; `started`/`alert`/`detected`/`finished`
  notifications sent in order from a background thread.
- Vigia watchdog (`vigia_lib.watch_active`, run by `vigia-status` and `vigia-run`): the supervisor touches its
  `status.json` every poll; a run whose supervisor stopped watching is alerted on and, past its caps, claimed under a
  per-run lock, killed (PID-reuse safe) and finished by the watchdog. Tunables `VIGIA_HB_STALE_SEC`,
  `VIGIA_WD_GRACE_SEC`, `VIGIA_WD_RECHECK_SEC`.
- Final state `incomplete`: a fleet lead (`fleet-*-lead-*`) that exits 0 without a fresh `T/FINAL-SUMMARY.md`.
- Alerts from the agent's stderr (`Background tasks still running`; auth/rate limit for Claude), a "box suspended"
  alert, and a "finished after the cap" alert; alerts that appear at the end are notified before `finished`.
- Per-run webhook: `VIGIA_WEBHOOK_ENV` at launch is stored in the status as `webhook_env` (`""` mutes the run) and
  `vigia-notify` honors it before its own environment and the default file.
- `CHANGELOG.md`; tests `tests/test_vigia_notify.py`, `tests/test_vigia_watchdog.py`, `tests/test_fleet.py`.

### Changed
- `--max-min` is wall-clock time and counts time the machine was suspended (`suspended_sec`; `duration_sec` is wall
  clock, `active_sec` awake time). `--stall-min` stays monotonic, but no new bytes for `VIGIA_STALL_CPU_MULT` (3) ×
  stall-min is `stalled` even while the process burns CPU.
- `started` is only recorded in `events.jsonl`, not POSTed, unless `VIGIA_NOTIFY_STARTED=1`.
- An empty `VIGIA_WEBHOOK_ENV` now means "no webhook" instead of falling back to the default file.
- `vigia-status` runs the watchdog first (messages on stderr); the state column is 10 wide.
- Bot prompt rewritten around the fleet, with an **autonomous** profile: the bot commits, pushes, opens PRs, merges
  and deploys after its own check; hard stops remain for spending, external messages, history rewrites on main,
  deleting what it did not create, and secrets. The header says how to restore the approval gate.
- `install.sh` also installs the fleet into `$ORQUESTRA_HOME/fleet/` and `vigia-routine.md`; the setup skill
  smoke-tests the fleet with `fleet-run --dry-run` and documents per-bot webhooks.

[Unreleased]: https://github.com/jpvmm/orquestra/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jpvmm/orquestra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jpvmm/orquestra/releases/tag/v0.1.0
