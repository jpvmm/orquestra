# Fleet — Orquestra's spec-to-code runner

Orquestra writes the best possible spec (plan.md, tasks.json with a DAG, criteria.json with the command that
proves each criterion, critical tests), classifies the work as P/M/G and launches the **fleet** under the Vigia.
The fleet is the superpowers `subagent-driven-development` skill adapted: headless, parallel, with
Claude × Codex cross review. Files and prompts are in English; Orquestra still talks to the user in the user's language.

## Layout
```
$ORQUESTRA_HOME/fleet/            (installed by install.sh from the repo's fleet/; this file is its README.md)
  .claude/agents/                team-lead.md dev.md reviewer-claude.md security.md  (model, tools, maxTurns)
  skill/fleet-sdd/SKILL.md       the flow (waves, worktrees, review, integration, fixed summary)
  skill/fleet-sdd/brief-*.md     templates: implementer, task-reviewer, final-reviewer, security, fixer
  skill/fleet-sdd/final-summary.md, templates/{progress.md,tasks.json,criteria.json}
  skill/fleet-sdd/references/    topical depth (claude-headless, codex-sandbox, docker-without-systemd)
  bin/fleet-run                  launches the fleet (below)
  bin/fleet-wt                   worktrees, review packages, patches, integration, DAG waves
  bin/codex-dev, codex-reviewer → fleet-codex   Codex role under the Vigia, waits for the end
  bin/fleet-wait                 waits for Vigia runs (≤ 540 s per call)
```
Tests live in the repo: `tests/test_fleet.py` (LLM-free tests of fleet-wt and `fleet-run --dry-run`).

Paths: `fleet-run`/`fleet-codex` find the Vigia in `$VIGIA_BIN` (default: the `bin/` next to this `fleet/` folder,
i.e. `$ORQUESTRA_HOME/bin`) and its state in `$VIGIA_BASE` (default `$ORQUESTRA_HOME/vigia`). Agent prompts use the
placeholder `@FLEET_HOME@`, which `fleet-run` replaces with the real fleet folder when it builds `T/.fleet/agents.json`,
so nothing is rendered at install time.

## Task folder T (file names code depends on)
`plan.md`, `criteria.json` `{"criteria":[{"id","description","command","expect","passes":false}],"critical_tests":[...]}`,
`tasks.json` `{"suite":"...","tasks":[{"id","title","text","files":[...],"depends_on":[...],"criteria":[...],"judgment":bool,"security":bool}]}`,
then the fleet writes `fleet.json`, `fleet-state.json`, `progress.md`, `briefs/`, `reports/`, `review/`, `patches/`,
`wt/integration`, `wt/task-ID`, `FINAL-SUMMARY.md`.

## Usage
```
export PATH="$HOME/.orquestra/fleet/bin:$HOME/.local/bin:$PATH"     # or $ORQUESTRA_HOME/fleet/bin
fleet-wt --task-dir T waves                                            # validate the DAG before launching
fleet-run --task-dir T --complexity M --lead claude --task "short description" --repo /path/to/repo
fleet-run --task-dir T --complexity P --lead claude --task "..." --repo R                 # P: dev
fleet-run --task-dir T --complexity P --lead claude --task "..." --stage review           # P: cross reviewer, after the dev finishes
fleet-run ... --dry-run                                                # print the command without launching
```
- **P**: no lead. 1 dev run + 1 reviewer run of the other tool (2 Orquestra wake-ups). One task (`task-1`) per folder.
- **M/G**: 1 team-lead run (Claude opus). It creates worktrees, dispatches devs/reviewers in parallel,
  integrates and writes `T/FINAL-SUMMARY.md`. Orquestra sleeps until the notification.
- **`--lead codex`** for M/G: EXPERIMENTAL, not tested end to end.
- Security: `--security auto` (default) enables the security reviewer on G, on a task with `"security": true`,
  or when the plan mentions auth/secret/input/network. `yes|no` forces it.
- A fleet lead that exits without a fresh `T/FINAL-SUMMARY.md` ends as `incomplete` in the Vigia (with a `detected`
  notification), not `done`.

## What the fleet may not do (and why)
The fleet never publishes. `fleet-run` launches every Claude run (lead, dev, reviewer) with
`--disallowedTools "Bash(git commit:*)" "Bash(git push:*)" "Bash(git merge:*)" "Bash(git rebase:*)" "Bash(gh:*)" "Bash(rm -rf *)"`.
Each task becomes a patch (git *tree* snapshots, no commits) and the result stays in `T/wt/integration`
(detached at BASE, changes in index/tree). Orquestra checks the integration (criteria commands, suite) and only
then commits, pushes and opens/merges the PR itself, per the AUTONOMY section of its prompt. Codex devs run in the
`workspace-write` sandbox (no network, cannot commit); Codex reviewers are read-only.

## fleet.json (written to T by fleet-run)
`roles` → `{tool, model}` for team-lead, dev (+ `judgment_model`), reviewer, final_reviewer,
security (`enabled`); `max_parallel`; `waves`; `base`/`integration`; `webhook_env`; launched `runs`.
Default with `--lead claude`: lead opus · dev sonnet (opus on `"judgment": true`) · reviewer Codex mid model ·
final reviewer Codex top model · security opus (see `MODELS` in `fleet-run`). If T/fleet.json already has
`roles`/`max_parallel`, they win (that is how you change models).

### max_parallel (default 4) and the Vigia cap
The Vigia accepts **5 supervised active runs in total, across all bots** (`$VIGIA_BASE/active` is shared;
`VIGIA_MAX_ACTIVE` changes it). The lead run counts 1; **each Codex run (dev or reviewer) counts 1**; Claude subagents
inside the lead do NOT count (practical limit ~20). Hence `max_parallel=4`: lead + 4 fit. If other runs are active,
`codex-*` waits up to 15 min for a slot (vigia-run exits 3) and then gives up (`not_launched`).

## Webhook
- **Default**: fleet-run does NOT set `VIGIA_WEBHOOK_ENV`, so `vigia-run` stores `webhook_env: null` and
  `vigia-notify` uses `$VIGIA_BASE/webhook.env` → Orquestra's "Vigia" routine wakes up.
- **Per-bot option**: `FLEET_WEBHOOK_ENV=/abs/path/webhook.env fleet-run ...` exports `VIGIA_WEBHOOK_ENV` to that file,
  so notifications go to another bot's routine. `VIGIA_WEBHOOK_ENV=""` inherited from the caller = muted (tests).
  `vigia-notify` order: status `webhook_env` (if not null) → `$VIGIA_WEBHOOK_ENV` → default file; `""` or a missing file
  = no POST (the event is still recorded in `events.jsonl`).
- Codex sub-runs launched by the lead are **muted** (`VIGIA_WEBHOOK_ENV=""`): the lead run is what wakes Orquestra.
  `FLEET_NOTIFY_SUBRUNS=1` enables them (P uses it: dev and reviewer are top-level runs).
- Never read or print any webhook.env.

## Known limits
- Headless lead (`claude -p`): a reply without a tool call ENDS the session, and background subagents left running are
  killed ~10 min later ("Background tasks still running ... stopping them"; the Vigia raises a stderr alert for it).
  So the lead dispatches every Agent/Bash in the foreground (`run_in_background: false`, several calls in one
  message = parallel) and replies text only once, after FINAL-SUMMARY.md; fleet-run also sets
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` for the lead.
- Resume: there is no `--resume` flag. Relaunch the same fleet-run command; the lead skips what `fleet-state.json`
  lists as `integrated` (progress.md exists = resuming). Before relaunching, remove dead task worktrees with
  `git worktree remove --force T/wt/task-ID` and drop their entries from `fleet-state.json` `tasks` (fleet-wt has no remove).
- `tasks.json` `files` must list EVERY file a task may edit (including "only if needed" ones): waves only keep
  same-file tasks apart through that list.
- Codex dev has no network: HTTP/socket tests fail there; the lead runs those in the integration.
- The lead does NOT use the Vigia's `--protect-tests` (devs write tests first on purpose).
- Vigia secret redaction may turn text like `pwd: ...` into `***` in conversation.md (false positive).

## Retro loop (v0.2.0)
Both loops (per project, generic) run **once every two days in one batch**, no end-of-run hook. Suggested cron
(not created by the install): `0 6 */2 * * fleet-retro-cycle`. Full operator guide: [retro-cycle.md](retro-cycle.md).
- **Cycle runner** `fleet-retro-cycle` (prompt `retro-routine.md`): decides whether a cycle is due (`cycle.due_hours`,
  `late_hours`); an empty cycle makes 0 model calls. Otherwise it runs `fleet-retro --batch` (task folders
  `$ORQUESTRA_HOME/<project>/<task>/` finished since the last batch, at most `cycle.max_batch_dirs`, none with an active
  run in `$VIGIA_BASE/active`), judges, applies, then lints. `--dry-run` changes nothing; exit 5 = lock held, 4 = pending.
- The model only answers JSON; the script validates it and writes diffs to `skill/proposals/`.
- **Loop 1, per project** → `$ORQUESTRA_HOME/<p>/LESSONS.md` (~4000 chars), ledger `<p>/lessons-ledger.jsonl`; fleet-run
  copies it to `T/.fleet/project-lessons.md`. **Loop 2, generic** → `skill/fleet-sdd/SKILL.md`, `references/<topic>.md`,
  `.claude/agents/*.md`, `$ORQUESTRA_HOME/learnings.md`; ledger `skill/ledger.jsonl`.
- **Gates** before a generic rule is applied: per-scope quotas, overlap/contradiction check against `skill/rules-index.json`
  (`index.new_overlap_refuse`), merge-guard (`merge_guard.min_ratio`: a merge must not lose content), and a cheap paired
  behavioural check by `fleet-skill-judge` (read-only, no tools, through vigia-run) on probes from `fleet-skill-eval`.
- **Judge memory** (`skill/judge-log.jsonl`): rejected rules are blocked for `block_days`, then re-admitted with growing
  cooldowns; recent precedents and rejections are shown to the judge and retro prompts. `near_suppress` stays off.
- **Probation and auto-revert**: an applied generic rule is `probation` for `probation.min_runs` runs over
  `windows_to_active` windows (at most one per always-loaded file); a regression above `regression_threshold` reverts it
  automatically with the exact bytes (`fleet-skill-apply --revert ID` does it by hand). Archive moves, never deletes.
- **Budget**: `budget.tokens_per_cycle` (default 25000) is a hard cap, reserved before each call (chars/4 estimate plus
  `output_allowance_tokens`) and never exceeded; usage is read from the stream-json `result`.
- **Shadow**: `fleet-retro-shadow` replays the gates without applying anything, to measure a policy before enabling it.
- **Policy** `skill/retro-policy.json` (override with `FLEET_RETRO_POLICY`): `owner_name`, `lessons_file`, `ignore_dirs`,
  `quota`, `budgets`, `budget`, `cycle`, `judge_memory`, `eval`, `index`, `merge_guard`, `probation`,
  `pending_expire_batches`, `always_loaded`. Never a patch target.
- Paths: one resolver (`fleet_skill_lib`): `ORQUESTRA_HOME` (default `~/.orquestra`) → `ORQ_ROOT`, `FLEET_ROOT`,
  `VIGIA_BIN`, `VIGIA_BASE` (`$VIGIA_BASE/active`), all overridable.
- **Install / upgrade**: `install.sh` copies every loop tool, `retro-policy.json` and `prompts/retro-routine.md`
  (`$ORQUESTRA_HOME/retro-routine.md`). Hashes of installed skill/config/agent files are kept in
  `skill/.install-baseline.json`; a re-install updates untouched files, but a file whose hash differs (edited by you or
  learned by the loop) is kept and the new version written to `<file>.upstream`. Ledgers, archive, evals, judge log,
  rules index, retro state/batches, proposals, `LESSONS.md` and `learnings.md` are never overwritten.

## Tests
`python3 tests/test_fleet.py`, `python3 tests/test_retro_loop.py`, `python3 tests/test_oss_port.py` (plus the Vigia tests in `tests/`). Tested with Claude Code 2.1.x · codex-cli 0.166 ·
Python 3 stdlib.
