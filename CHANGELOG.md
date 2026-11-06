# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Fleet criteria template ships a `C-UI` visual-proof criterion (screenshot or Playwright journey log) for UI tasks.
- fleet-run criteria gate: refuses manual, vacuous (`true`, `echo`, `test 1`), unknown or failure-masking commands
  (`||`, `;`, `|`, `&`, subshells, `$(...)`), commands that may run nothing (`xargs`, `find -exec`, a bare interpreter, `--version`, no-op inline code like `python3 -c pass`), long commands (→ `T/checks/<id>.sh`), missing/non-executable scripts,
  and UI criteria whose command does not check their `artifact`. The skill states criteria never change in a run.


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

[Unreleased]: https://github.com/jpvmm/orquestra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jpvmm/orquestra/releases/tag/v0.1.0
