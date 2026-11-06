# Orquestra

Orquestra is a setup for an AI assistant bot that acts as an expert software engineer and **orchestrates
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex CLI](https://github.com/openai/codex)**
to plan, evaluate, architect, test and develop software. The bot is the conductor: it writes the plan,
splits the work, picks the tool and the model, writes each task, checks the results and integrates them.
Claude and Codex write the code, and each one reviews the other.

It has three parts:

- **The bot prompt** ([`prompts/orquestra.md`](prompts/orquestra.md)): the working rules. Risk-sized ceremony,
  tests written by whoever does not implement, clean-context cross review, verifiable criteria, at most 3 fix rounds.
  The profile is **autonomous** (see [Autonomy](#autonomy)): the bot commits, pushes, merges and deploys after its own
  check, with hard stops on spending, external messages, history rewrites and secrets. Written for a
  [Grok Bot](#using-the-bot-prompt), usable by any agent that has a shell.
- **Vigia** ([`bin/`](bin), [`docs/vigia.md`](docs/vigia.md)): a small supervisor for headless `claude -p` and
  `codex exec` runs. It detects stalls, timeouts and loops, kills the whole process tree, keeps evidence,
  and wakes the bot through a webhook when a run ends. The bot never has to poll.
- **The fleet** ([`fleet/`](fleet), [`docs/fleet.md`](docs/fleet.md)): a spec-to-code runner. From a plan, a task DAG
  and criteria with proving commands, a headless team lead dispatches devs and Claude × Codex cross reviewers in
  parallel waves, one git worktree per task, and integrates the patches without committing. Everything runs under Vigia.

> Status: early, used daily by its author on a single Linux box. Expect rough edges.

## Why Orquestra

- **Event-driven, not polling.** The bot launches a Claude or Codex run, ends its turn, and is woken by a webhook
  when the run finishes. No "is it done yet?" loops burning tokens, and news arrives right away.
- **Catches problems on its own.** Vigia notices stalled, timed-out and looping runs (the same error over and over,
  the same edit/test cycle with no progress) and stops them before they waste quota.
- **Leaves the machine clean.** When a run is stopped, the whole process group is killed and verified gone.
  No orphaned processes or leftover containers.
- **Keeps evidence.** On failure it freezes log tails, the process tree and the status at that moment, so you can
  see what happened without re-running.
- **Really protects tests.** Claude Code and Codex hooks stop the implementing agent from editing test files to
  make them pass.
- **Same rules for both agents.** Claude Code and Codex get the same states, alerts and limits, and each can review
  the other's work.
- **Small and portable.** A few standard-library Python scripts: no Docker, no services. Hooks apply only to
  supervised runs, never to your global CLI config. A Grok Bot can install the whole thing by itself with the
  bundled skill.
- **One bot, clear rules.** Instead of a stack of agents talking to each other, a single orchestrator with
  explicit working rules and a tiny supervisor that tells it when there is something to do.

## How Vigia works

```
bot ──► vigia-run ──(detached supervisor)──► claude -p … / codex exec …   (own process group)
                        │  reads events (stream-json / --json) + hook heartbeats + CPU
                        │  stall? timeout? loop?  ──► freeze evidence ──► TERM, then KILL the group ──► verify with pgrep
                        ▼
                  vigia-notify ──► events.jsonl  (+ POST to webhook ──► bot wakes up)
```

- **Detached supervisor, own process group.** `vigia-run` double-forks a supervisor that outlives the shell or
  turn that launched it, starts the CLI in a new session/process group (stdin `/dev/null`), and marks itself a
  Linux *child subreaper* so orphaned grandchildren still belong to it.
- **Stall detection.** A run is `stalled` after `--stall-min` minutes (default 10) with no new bytes in events,
  hooks or stderr **and** no CPU in the group, or after 3× that with no new bytes even if it burns CPU.
- **Timeout.** Wall-clock cap `--max-min` (default 20); time the machine spent suspended counts (and raises an alert).
- **Watchdog.** The supervisor touches its `status.json` every poll; `vigia-status` and `vigia-run` check that
  heartbeat and, if a supervisor stopped watching past its caps, kill the run themselves (PID-reuse safe).
- **Incomplete.** A fleet lead that exits 0 without writing its `FINAL-SUMMARY.md` ends as `incomplete`, not `done`.
- **Loop detection.** The same error N times (normalized), an identical edit/test cycle N times with no progress,
  editing a protected test file (`--protect-tests`), or growing scope (`--max-files`, `--max-turns-hint`).
- **Evidence.** On stalled/timeout/loop, it freezes the tails of events, stderr and hooks, a `ps --forest` of the
  tree and the status at that moment into `<task-dir>/runs/<name>.evidence/`.
- **Kill and verify.** SIGTERM to the group and to stray descendants, 10 s grace, SIGKILL, then `pgrep -g` to
  prove nothing survived (`kill.ok` in `status.json`). Leftover background processes are killed even on success.
- **Cap.** At most 5 concurrent supervised runs (`VIGIA_MAX_ACTIVE`), with a `flock`-guarded registry and
  automatic cleanup of dead supervisors.
- **Alerts.** Rate-limit and auth errors (events and stderr) and `Background tasks still running` on stderr are
  surfaced as `ALERT:` in the summary so the bot can stop and tell you.

### Claude Code and Codex hooks

- **Claude Code:** `claude-hooks.json` is passed per run with `--settings`. It does not touch `~/.claude`.
  The hook appends a heartbeat line per event (SessionStart, Pre/PostToolUse, PostToolUseFailure, Stop,
  SubagentStop, StopFailure) and, with `--protect-tests`, **denies** Edit/Write on test files.
- **Codex CLI:** `vigia-run --tool codex` injects `-c hooks.*` plus `--dangerously-bypass-hook-trust` right after
  `exec`, for that run only (nothing written to `~/.codex`). With `--protect-tests` the hook denies `apply_patch`
  on test files. It also catches commands blocked by the sandbox, which `--json` omits.

### Webhook wake-up

Each run also records what the orchestrator sent (`<run>.prompt.md`) and what the agent answered, turn by turn
(`<run>.conversation.md`, plus a structured `<run>.conversation.json`), with secrets masked.
On every state change (`started`, `alert`, `detected`, `finished`), `vigia-notify` appends a line to `events.jsonl`
and, except for `started` (recorded only, unless `VIGIA_NOTIFY_STARTED=1`), POSTs to the run's webhook a redacted payload with the event, transition, task, model,
prompt and reply previews and file paths, with `Authorization: Bearer <key>` (retries on network errors, 5xx, 408,
425 and 429). Duplicates are dropped and `finished` is sent exactly once. In Grok Bot, point it at a routine with a
webhook trigger: the bot launches a run, ends its turn, and is woken when the run changes state.
Any webhook receiver works. The key and full URL are never logged.
The webhook is per run: `VIGIA_WEBHOOK_ENV` at launch is stored in the run's status (default
`$VIGIA_BASE/webhook.env`; another file routes to another bot; `""` mutes the run).

Details: [`docs/vigia.md`](docs/vigia.md).

## Requirements

- Linux (Vigia reads `/proc` and uses process groups and `prctl(PR_SET_CHILD_SUBREAPER)`)
- Python 3.9+ (standard library only), `pgrep`/`ps` (procps)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) and/or [Codex CLI](https://github.com/openai/codex) (`codex`), logged in.
  Tested with Claude Code 2.1.x and Codex CLI 0.166.0. Codex hook injection needs a Codex version with hooks support.
- For the bot: an agent that can run shell commands on that machine. Wake-ups need webhook-triggered runs
  (e.g. Grok Bot routines). Without them the bot checks `vigia-status` instead.

## Install

```bash
git clone https://github.com/jpvmm/orquestra.git
cd orquestra
./install.sh                          # installs to ~/.orquestra
# or: ORQUESTRA_HOME=/opt/orquestra ./install.sh
export PATH="$HOME/.orquestra/bin:$PATH"   # optional
```

`install.sh` copies the scripts to `$ORQUESTRA_HOME/bin`, renders `claude-hooks.json` with absolute paths,
creates `$ORQUESTRA_HOME/vigia/`, installs the fleet kit into `$ORQUESTRA_HOME/fleet/` (add `fleet/bin` to `PATH`),
and copies the docs, the bot prompt and the Vigia routine instruction (`vigia-routine.md`). Re-run it to update. It never touches
`webhook.env` or run state. An installed copy finds its own home through a `.orquestra-home` marker, so
`ORQUESTRA_HOME` only needs to be exported if you want to override it.

Optional webhook:

```bash
cp ~/.orquestra/vigia/webhook.env.example ~/.orquestra/vigia/webhook.env
chmod 600 ~/.orquestra/vigia/webhook.env
$EDITOR ~/.orquestra/vigia/webhook.env   # VIGIA_WEBHOOK_URL, VIGIA_WEBHOOK_KEY
```

## Usage

```bash
B=~/.orquestra/bin
T=~/.orquestra/myproject/task-1          # task folder: plan.md, criteria.json, progress.md, runs/

# Claude Code implementing a slice, tests protected (prompt must come right after -p)
$B/vigia-run --tool claude --task-dir $T --name impl-1 --task "implement C1" --cwd ~/src/myproject --protect-tests -- \
  claude -p "Implement criterion C1 from $T/criteria.json ..." --model opus --max-turns 30 \
  --output-format stream-json --verbose --settings $B/claude-hooks.json

# Codex reviewing the diff, read-only
$B/vigia-run --tool codex --task-dir $T --name review-1 --task "review C1" --cwd ~/src/myproject -- \
  codex exec --json -o $T/runs/review-1.last.txt "Review the diff on this branch against $T/plan.md ..."

# Any other command (stall/timeout/kill only)
$B/vigia-run --tool other --task-dir $T --name build-1 --max-min 5 -- make test

$B/vigia-status                                   # active + recent runs, with task, prompt and reply previews
$B/vigia-status --json -n 30
$B/vigia-stop $T/runs/impl-1.status.json          # cancel a run
```

`vigia-run` returns immediately with JSON such as
`{"name": "impl-1", "state": "running", "pid": 1234, "pgid": 1234, "status_path": ".../runs/impl-1.status.json", ...}`.
Final states: `done`, `failed`, `stalled`, `timeout`, `loop`, `killed`, `incomplete`.

| command | purpose |
|---|---|
| `vigia-run` | launch a supervised run (exit 0 started, 2 usage, 3 cap reached, 4 name in use, 5 command not found) |
| `vigia-status` | table or JSON of active and recent runs; runs the watchdog and cleans up dead supervisors |
| `vigia-stop <status.json>` | stop a run (state `killed`) and confirm the group is gone |
| `vigia-notify <status.json> [--event E]` | record a state change and POST the webhook (called by the supervisor) |
| `vigia-claude-hook`, `vigia-codex-hook` | per-run hooks (called by the CLIs) |

## Fleet

For anything bigger than a one-file fix, the bot writes a task folder (`plan.md`, `tasks.json` with a DAG,
`criteria.json` where every criterion has the command that proves it) and launches the fleet:

```bash
export PATH="$HOME/.orquestra/fleet/bin:$PATH"
fleet-wt --task-dir $T waves                                    # validate the DAG
fleet-run --task-dir $T --complexity M --lead claude --task "add rate limiting" --repo ~/src/myproject
fleet-run ... --dry-run                                         # print the command, launch nothing
```

- **P** (trivial): one dev run, then `--stage review` for a reviewer of the other tool.
- **M/G**: one Claude team-lead run that creates a worktree per task, dispatches devs and cross reviewers in
  parallel waves (same file never in the same wave), integrates the patches into `T/wt/integration`, runs a final
  review by the strongest model of the other tool, and writes `T/FINAL-SUMMARY.md`.
- The fleet never publishes: `fleet-run` passes `--disallowedTools` for `git commit/push/merge/rebase`, `gh`,
  `rm -rf`, `curl`/`wget` and secret files, plus `--strict-mcp-config`, to every Claude run it launches. The bot verifies the integration and commits itself.
- Webhook: lead runs notify the default routine; `FLEET_WEBHOOK_ENV` routes to another bot; Codex sub-runs are muted
  unless `FLEET_NOTIFY_SUBRUNS=1`.

Details: [`docs/fleet.md`](docs/fleet.md).

## Autonomy

The bundled prompt gives the bot full autonomy over software delivery: after its own check (criteria commands,
suite, diff) it commits, pushes, opens PRs, merges and deploys without asking. It still never spends money, sends
external messages on the user's behalf, force-pushes or rewrites `main`, deletes repos/branches/data it did not
create, or touches secrets, `~/.ssh` or `~/.claude`. To get an approval gate back, replace the AUTONOMY section of the
prompt as described in its header.

## Using the bot prompt

[`prompts/orquestra.md`](prompts/orquestra.md) is the full instruction set.

**In Grok Bot**

1. Create a bot (e.g. "Orquestra") and paste the prompt (the part after the `---`) as its instructions.
2. On the machine the bot uses, install Claude Code and Codex CLI, log in, then run `./install.sh`.
3. Create a routine named **Vigia** with a **webhook** trigger on that bot, using the instruction in
   [`prompts/vigia-routine.md`](prompts/vigia-routine.md) (relay rules + fleet follow-up).
   Put its URL and key into `~/.orquestra/vigia/webhook.env` (`chmod 600`). The prompt allows the bot to write
   that file itself.
4. Ask for work: "Add rate limiting to the API in ~/src/myproject". The bot plans, launches runs under Vigia,
   tells you when to expect news, and picks up when the routine wakes it.

**Automatic setup in Grok Bot:** give your bot the skill in [`skills/orquestra-setup/SKILL.md`](skills/orquestra-setup/SKILL.md)
and ask "install Orquestra". It installs everything, creates the Vigia routine, asks you only for logins and the
webhook URL/key (through a masked field), and tests the wake-up.

**In any other agent** (Claude Code itself, a custom agent, another assistant): use the prompt as the system
prompt. Replace the "routine named Vigia" part with whatever your platform offers for webhook-triggered runs,
or drop it and have the agent run `vigia-status` at the time it promised.

The prompt assumes: a shared machine where the CLIs are logged in with your accounts; `~/.orquestra` paths
(edit them if you installed elsewhere); the model aliases `opus`, `sonnet`, `haiku`; and the autonomy policy above.

## Repository layout

```
bin/            vigia-run, vigia-status, vigia-stop, vigia-notify, hooks, vigia_lib.py, vigia_comms.py,
                claude-hooks.json.template
fleet/          bin/ (fleet-run, fleet-wt, fleet-codex, codex-dev, codex-reviewer, fleet-wait),
                .claude/agents/ (team-lead, dev, reviewer-claude, security), skill/fleet-sdd/ (flow, briefs, templates)
docs/           vigia.md (Vigia reference), fleet.md (fleet reference)
prompts/        orquestra.md (the bot instructions), vigia-routine.md (webhook routine instruction)
skills/         orquestra-setup/SKILL.md (self-install skill for Grok Bot)
tests/          unittest scripts (no LLM, temp dirs only)
install.sh      installer (into $ORQUESTRA_HOME, default ~/.orquestra)
CHANGELOG.md
webhook.env.example
```

## Tests

Plain `unittest` scripts, standard library only; they use temp dirs, a temp `VIGIA_BASE` and never POST to a real webhook:

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

- `test_vigia_comms.py`: redaction, prompt extraction, conversation.md/.json
- `test_vigia_notify.py`: per-run webhook, muting, `started` not POSTed, dedupe
- `test_vigia_watchdog.py`: wall-clock cap across suspension, stall with CPU, `incomplete`, watchdog, PID reuse
- `test_fleet.py`: fleet-wt waves/integration/conflicts and `fleet-run --dry-run`
- `test_orq_launch.py`: role flags and task-dir+role lock for `orq-launch`

## Security notes

- `webhook.env` holds a secret: keep it `chmod 600` and out of git (it is in `.gitignore`).
- Codex runs use `--dangerously-bypass-hook-trust` so Vigia's per-run hooks fire. That also enables any hooks
  already configured for your user or trusted projects. Review them, or set `VIGIA_CODEX_HOOKS=0`.
- The fleet blocks publishing commands for the Claude runs it launches (`--disallowedTools`), but the bot itself is
  autonomous: review [`prompts/orquestra.md`](prompts/orquestra.md) before giving it access to repos you care about.
- Vigia does not sandbox anything. Permissions come from the CLIs' own flags (`--allowedTools`, `--sandbox`, ...).
- Test-file protection through the shell is heuristic (see [limitations](docs/vigia.md#known-limitations)).

## License

[Apache License 2.0](LICENSE). Copyright 2026 jpvmm.
