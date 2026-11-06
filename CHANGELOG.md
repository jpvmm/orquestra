# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Detect Claude CLI 5-hour usage limit (Claude runs only); a review is really relaunched on Codex (`fleet-codex reviewer`, verified, state `limit_fallback`), a lead/implement/plan/unknown role pauses as `limit_paused` with `T/.fleet/limit-resume.json` holding the exact launcher command (`ORQ_RELAUNCH_ARGV`). The role comes from `vigia-run --role` (set by fleet-run), else the run's own `fleet.json` entry, else its name. Usage lines append to `progress.md` when present. fleet-wait/fleet-codex treat both states as terminal. The limit is also found in the final result text (stdout, exit 0); while the Codex relaunch is decided the run shows the non-final `limit_handoff`, so waiters never see a final state before the new run exists. A handoff interrupted by a dead supervisor is finished from a marker in `$VIGIA_BASE/handoffs/` (by `vigia-status`, the next `vigia-run`, `fleet-wait` or `fleet-codex`): `limit_fallback` if the replacement run exists, else `failed`. A relaunch that fails or raises ends the run as `failed`. A plan/review run with `--output` moves an older file to `<output>.prev` before it starts and is `failed` unless it wrote the file itself (`output_sha256` in the status).
- Fleet criteria template ships a `C-UI` visual-proof criterion (screenshot or Playwright journey log) for UI tasks.
- fleet-run criteria gate is a whitelist: a criterion command may only run a test runner (`pytest`,
  `python3 -m pytest|unittest`, `npm|pnpm|yarn test`, `go test`, `cargo test`) or an existing text script under
  `T/checks/`, joined by `&&` with optional `cd DIR` / `VAR=value`; a UI criterion must name its `artifact` and check
  it explicitly with `test -s ARTIFACT`. Runner arguments are literal (no redirection except `2>&1`, no quoted operator characters) and whitelisted per runner, with value shapes (`-count` ≥ 1...); runners by path must be the real executable, and environment prefixes that swap the runner (`PATH`, `PYTEST_*`, `GOFLAGS`...) or modules that shadow it are refused. Everything else is refused: manual text, inline code
  (`python3 -c`, `node -e`, `bash -c`), other programs, wrappers (`env`, `exec`, `timeout`, `xargs`, `find`), shell
  options, failure-masking operators, help/collect-only/no-run flags and assertion-disabling Python settings. The skill states criteria never change in a run.
- `orq-launch`: optional per-role Claude/Codex flag builder and task-dir+role concurrency lock (fleet runners stay unchanged).
- `orq-launch` safety: Claude gets `--disallowedTools` (git commit/push/merge/rebase, gh, curl, wget, rm -rf, secret files) and `--strict-mcp-config`; Codex write roles get `network_access=false` (fleet-run now refuses a Codex M/G lead instead of giving its sandbox network); plan is read-only (no Bash); lead protects tests; `--output-schema` for read-only Codex roles.
### Fixed
- `--protect-tests` only counts test paths inside the run cwd/worktree (scratch under `/tmp` no longer trips the loop detector).
- `--protect-tests` protects only the tests that exist on the base commit (snapshot by fleet-run in `$VIGIA_BASE/protected/`, outside the agent-writable task dir; relative `../` paths are resolved against the run cwd; shell writes are denied too): new test files are allowed, so TDD works. The M/G lead now runs with it too; a subagent's denied attempt is an alert, not a kill of the lead, and the loop/scope detector counts only the run's own calls.
- End-of-run gate for `--protect-tests`: fleet-run passes the base commit by argv (`vigia-run --protect-base`, also recorded for Codex devs); when the run ends, every base test in the cwd and `T/wt/*` is compared by raw content with the base (renames, index flags, clean filters, `git replace` or any shell trick cannot hide a change; symlinks by target; a symlinked parent directory or a changed exec bit counts as a change; a CRLF checkout of the same text does not; expected blob ids/modes come from a manifest fleet-run writes before launch (`protected/<hash>.manifest.json`), not from the agent-writable git objects; non-UTF-8 test names are handled; tests listed in fleet-run's snapshot but missing from the base tree are reported). A changed/deleted base test, or one that cannot be verified, turns `done` into `loop`. Codex devs and the P Claude dev now run with `--protect-tests` too.
- `install.sh` no longer overwrites a customized `orquestra.md` on reinstall; writes `orquestra.md.new` instead.


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
