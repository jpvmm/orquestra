# Orquestra

Orquestra is a setup for an AI assistant bot that acts as an expert software engineer and **orchestrates
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex CLI](https://github.com/openai/codex)**
to plan, evaluate, architect, test and develop software. The bot is the conductor: it writes the plan,
splits the work, picks the tool and the model, writes each task, checks the results and integrates them.
Claude and Codex write the code, and each one reviews the other.

It has two parts:

- **The bot prompt** ([`prompts/orquestra.md`](prompts/orquestra.md)): the working rules. Risk-sized ceremony,
  tests written by whoever does not implement, clean-context cross review, verifiable criteria, at most 3 fix rounds,
  and hard stops (no merge, deploy or spending without the user's OK). Written for a [Grok Bot](#using-the-bot-prompt),
  usable by any agent that has a shell.
- **Vigia** ([`bin/`](bin), [`docs/vigia.md`](docs/vigia.md)): a small supervisor for headless `claude -p` and
  `codex exec` runs. It detects stalls, timeouts and loops, kills the whole process tree, keeps evidence,
  and wakes the bot through a webhook when a run ends. The bot never has to poll.

> Status: early, used daily by its author on a single Linux box. Expect rough edges.

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
  hooks or stderr **and** no CPU in the group.
- **Timeout.** Wall-clock cap `--max-min` (default 20).
- **Loop detection.** The same error N times (normalized), an identical edit/test cycle N times with no progress,
  editing a protected test file (`--protect-tests`), or growing scope (`--max-files`, `--max-turns-hint`).
- **Evidence.** On stalled/timeout/loop, it freezes the tails of events, stderr and hooks, a `ps --forest` of the
  tree and the status at that moment into `<task-dir>/runs/<name>.evidence/`.
- **Kill and verify.** SIGTERM to the group and to stray descendants, 10 s grace, SIGKILL, then `pgrep -g` to
  prove nothing survived (`kill.ok` in `status.json`). Leftover background processes are killed even on success.
- **Cap.** At most 5 concurrent supervised runs (`VIGIA_MAX_ACTIVE`), with a `flock`-guarded registry and
  automatic cleanup of dead supervisors.
- **Alerts.** Rate-limit and auth errors are surfaced as `ALERT:` in the summary so the bot can stop and tell you.

### Claude Code and Codex hooks

- **Claude Code:** `claude-hooks.json` is passed per run with `--settings`. It does not touch `~/.claude`.
  The hook appends a heartbeat line per event (SessionStart, Pre/PostToolUse, PostToolUseFailure, Stop,
  SubagentStop, StopFailure) and, with `--protect-tests`, **denies** Edit/Write on test files.
- **Codex CLI:** `vigia-run --tool codex` injects `-c hooks.*` plus `--dangerously-bypass-hook-trust` right after
  `exec`, for that run only (nothing written to `~/.codex`). With `--protect-tests` the hook denies `apply_patch`
  on test files. It also catches commands blocked by the sandbox, which `--json` omits.

### Webhook wake-up

When a run reaches a final state, `vigia-notify` appends a line to `events.jsonl` and, if
`$VIGIA_BASE/webhook.env` exists, POSTs
`{"run","tool","state","exit_code","summary","task_dir","status_path","evidence_path"}`
with `Authorization: Bearer <key>` (retries on network errors, 5xx, 408, 425 and 429). In Grok Bot, point it at a
routine with a webhook trigger: the bot launches a run, ends its turn, and is woken when the run finishes.
Any webhook receiver works. The key and full URL are never logged.

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
creates `$ORQUESTRA_HOME/vigia/`, and copies the docs and the bot prompt. Re-run it to update. It never touches
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
$B/vigia-run --tool claude --task-dir $T --name impl-1 --cwd ~/src/myproject --protect-tests -- \
  claude -p "Implement criterion C1 from $T/criteria.json ..." --model opus --max-turns 30 \
  --output-format stream-json --verbose --settings $B/claude-hooks.json

# Codex reviewing the diff, read-only
$B/vigia-run --tool codex --task-dir $T --name review-1 --cwd ~/src/myproject -- \
  codex exec --json -o $T/runs/review-1.last.txt "Review the diff on this branch against $T/plan.md ..."

# Any other command (stall/timeout/kill only)
$B/vigia-run --tool other --task-dir $T --name build-1 --max-min 5 -- make test

$B/vigia-status                                   # active + recent runs
$B/vigia-status --json -n 30
$B/vigia-stop $T/runs/impl-1.status.json          # cancel a run
```

`vigia-run` returns immediately with JSON such as
`{"name": "impl-1", "state": "running", "pid": 1234, "pgid": 1234, "status_path": ".../runs/impl-1.status.json", ...}`.
Final states: `done`, `failed`, `stalled`, `timeout`, `loop`, `killed`.

| command | purpose |
|---|---|
| `vigia-run` | launch a supervised run (exit 0 started, 2 usage, 3 cap reached, 4 name in use, 5 command not found) |
| `vigia-status` | table or JSON of active and recent runs; cleans up dead supervisors |
| `vigia-stop <status.json>` | stop a run (state `killed`) and confirm the group is gone |
| `vigia-notify <status.json>` | record the end of a run and POST the webhook (called by the supervisor) |
| `vigia-claude-hook`, `vigia-codex-hook` | per-run hooks (called by the CLIs) |

## Using the bot prompt

[`prompts/orquestra.md`](prompts/orquestra.md) is the full instruction set.

**In Grok Bot**

1. Create a bot (e.g. "Orquestra") and paste the prompt (the part after the `---`) as its instructions.
2. On the machine the bot uses, install Claude Code and Codex CLI, log in, then run `./install.sh`.
3. Create a routine named **Vigia** with a **webhook** trigger on that bot, with an instruction such as
   "A Vigia run finished: read the JSON received and its status_path, and follow your rules."
   Put its URL and key into `~/.orquestra/vigia/webhook.env` (`chmod 600`). The prompt allows the bot to write
   that file itself.
4. Ask for work: "Add rate limiting to the API in ~/src/myproject". The bot plans, launches runs under Vigia,
   tells you when to expect news, and picks up when the routine wakes it.

**In any other agent** (Claude Code itself, a custom agent, another assistant): use the prompt as the system
prompt. Replace the "routine named Vigia" part with whatever your platform offers for webhook-triggered runs,
or drop it and have the agent run `vigia-status` at the time it promised.

The prompt assumes: a shared machine where the CLIs are logged in with your accounts; `~/.orquestra` paths
(edit them if you installed elsewhere); the model aliases `opus`, `sonnet`, `haiku`; and that the user approves
merges, deploys and spending in chat.

## Repository layout

```
bin/            vigia-run, vigia-status, vigia-stop, vigia-notify, hooks, vigia_lib.py,
                claude-hooks.json.template
docs/vigia.md   full Vigia reference
prompts/        orquestra.md (the bot instructions)
install.sh      installer (into $ORQUESTRA_HOME, default ~/.orquestra)
webhook.env.example
```

## Security notes

- `webhook.env` holds a secret: keep it `chmod 600` and out of git (it is in `.gitignore`).
- Codex runs use `--dangerously-bypass-hook-trust` so Vigia's per-run hooks fire. That also enables any hooks
  already configured for your user or trusted projects. Review them, or set `VIGIA_CODEX_HOOKS=0`.
- Vigia does not sandbox anything. Permissions come from the CLIs' own flags (`--allowedTools`, `--sandbox`, ...).
- Test-file protection through the shell is heuristic (see [limitations](docs/vigia.md#known-limitations)).

## License

[Apache License 2.0](LICENSE). Copyright 2026 jpvmm.
