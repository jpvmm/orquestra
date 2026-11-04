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

## Retro loop (v0.1.x, ported from the live install)
Both loops run **once every two days in one batch** from the owner's scheduler (suggested cron `0 6 */2 * *`). There is
no end-of-run hook.
- `fleet-retro --batch` finds task folders `$ORQUESTRA_HOME/<project>/<task>/` whose runs finished since the last batch
  (`skill/retro-state.json`), skips folders with a run still active (`$VIGIA_BASE/active`), takes at most 10 and analyzes
  them in ONE read-only Claude call under vigia-run (muted, tools limited to Read/Grep/Glob). Then it runs
  `fleet-skill-lint`. `--dry-run` changes nothing; exit 5 = another batch holds the lock, exit 4 = collect pending.
- The model only answers JSON; the script validates it and writes diffs to `skill/proposals/`. Nothing is applied.
- **Loop 1, per project** → `$ORQUESTRA_HOME/<p>/LESSONS.md` (~4000 chars), ledger `<p>/lessons-ledger.jsonl`. fleet-run
  copies it to `T/.fleet/project-lessons.md`; the lead pastes the relevant rules into every brief.
- **Loop 2, generic** → `skill/fleet-sdd/SKILL.md`, `references/<topic>.md`, `$ORQUESTRA_HOME/learnings.md`
  (`FLEET_LESSONS_NAME` renames it); ledger `skill/ledger.jsonl`.
- `fleet-skill-apply <diff> --run R --reason "..."` applies with a backup and a ledger entry;
  `fleet-skill-apply --revert ID` restores the exact bytes. `fleet-skill-lint` writes `skill/lint-report.md` (never deletes).
- Paths: `FLEET_ROOT` (default the fleet folder), `ORQ_ROOT` (default `$ORQUESTRA_HOME`), `VIGIA_BIN`, `VIGIA_BASE`.

## Tests
`python3 tests/test_fleet.py`, `python3 tests/test_retro_loop.py` (plus the Vigia tests in `tests/`). Tested with Claude Code 2.1.x · codex-cli 0.166 ·
Python 3 stdlib.
