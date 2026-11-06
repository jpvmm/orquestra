# Vigia: supervisor for headless Claude Code and Codex CLI runs

Vigia ("watchman" in Portuguese) replaces "wake the orchestrator every 10 minutes to check whether a run got stuck".
Every `claude -p` or `codex exec` run executes inside a detached supervisor that outlives the shell that launched it.
The supervisor:

- writes the CLI's events to files;
- detects stalls, wall-clock timeouts (suspension counts) and loops, plus a fleet lead that ended without its summary (`incomplete`);
- leaves a heartbeat that an independent watchdog (in `vigia-status`/`vigia-run`) checks, so a supervisor that stopped watching is caught;
- freezes evidence, kills the whole process group and verifies with `pgrep`;
- records **what the orchestrator sent** (`<name>.prompt.md`: prompt, system prompt, model, rule files) and **what the agent answered** (`<name>.conversation.md`: turn by turn, including tool calls and subagents), with secrets masked;
- calls `vigia-notify` on every state change (started, alert, detected, finished), which records it and, if a webhook is configured for the run, wakes the orchestrator (`started` is only recorded).

Scripts live in `$ORQUESTRA_HOME/bin/` and state (registry, logs, configuration) in `$VIGIA_BASE`.
Defaults: `ORQUESTRA_HOME=~/.orquestra`, `VIGIA_BASE=$ORQUESTRA_HOME/vigia`.
Python 3 standard library only. Linux only (uses `/proc`, process groups and `PR_SET_CHILD_SUBREAPER`).

## Commands

### Launch a run

```bash
B=${ORQUESTRA_HOME:-$HOME/.orquestra}/bin

# Claude (stream-json for live events; the hooks apply only to this run, via --settings)
$B/vigia-run --tool claude --task-dir /path/to/task --name t1-claude --task "fix the login bug" \
  --cwd /path/to/repo [--stall-min 10] [--max-min 20] [--max-turns-hint 15] [--protect-tests] -- \
  claude -p "PROMPT HERE" --model sonnet --max-turns 15 \
  --output-format stream-json --verbose --settings $B/claude-hooks.json

# Codex (default sandbox is read-only; to implement, add --sandbox workspace-write)
# Vigia's hooks are added to this run automatically (see "Codex hooks"); you pass nothing extra
$B/vigia-run --tool codex --task-dir /path/to/task --name t1-codex --task "review the login fix" --cwd /path/to/repo -- \
  codex exec --json -o /path/to/task/runs/t1-codex.last.txt "PROMPT HERE"
```

- `vigia-run` returns immediately and prints a JSON object with `name`, `pid`, `pgid`, `task`, `model`, `status_path`, `events_path`, `prompt_path` and `conversation_path`. The run keeps going on its own.
- **Always pass `--task "short description"`.** It goes into the status, every notification and `prompt.md`, so a person can tell runs apart without reading the prompt.
- The command's stdin is always `/dev/null`. You do not need `< /dev/null`.
- Do not use `claude --bare` (it ignores your subscription login).
- **Gotcha (tested):** with `claude`, put the prompt **right after `-p`**. Options such as `--allowedTools`/`--disallowedTools` take multiple values and "swallow" a prompt that comes after them. The symptom is `Input must be provided...`.
- `--output-format json` also works, but it only writes at the end. Signs of life then come only from hooks and CPU.
- `--cwd` sets the directory the command runs in. Default: the current directory.
- Names (`--name`) use only `A-Z a-z 0-9 . _ -`. A name cannot be reused within the same `--task-dir`; trying exits with code 4.
- `--tool other` supervises any command (no event parsing, just stall/timeout/kill).

`vigia-run` options:

| option | default | what it does |
|---|---|---|
| `--stall-min N` | 10 | stalled = N minutes with no new bytes (events, hooks or stderr) **and** no CPU in the group |
| `--max-min N` | 20 | maximum wall-clock time |
| `--max-turns-hint N` | none | more than N tool calls: alert; more than 2N: loop (growing scope) |
| `--max-files N` | none | more than N distinct files edited: loop (growing scope) |
| `--protect-tests` | off | any write to a test file is a loop; the hook also **denies** the edit (Edit/Write in Claude, `apply_patch` in Codex) |
| `--loop-repeat N` | 3 | how many repetitions count as a loop |
| `--task TEXT` | none | short task description shown in status, notifications and `vigia-status` |
| `--role ROLE` | none | the run's real role (`plan`, `review`, `implement`, `lead`); decides the Claude usage-limit follow-up |
| `--output PATH` | none | expected result file; a Codex fallback of a Claude review writes its reply there |
| `--prompt-file PATH` | none | file holding the prompt, when the command reads it from stdin (`-`); otherwise only noted |
| `--foreground` | off | do not detach (debugging) |

**Where the prompt comes from.** `vigia-run` parses the command (also through `env`/`timeout`/`nice` wrappers):

- Claude: the argument after `-p`/`--print` (or the first positional), `--system-prompt`, `--append-system-prompt` and their `-file` variants, `--model`.
- Codex: the `exec` positional prompt (also `exec resume`), `-m`/`--model`, `-c model=...`, `-c` instruction keys, and `-o` (the final-reply file).
- Rule files the agent loads by itself (`CLAUDE.md`, `AGENTS.md`, `.claude/`, global ones) are listed by path, not copied.

Exit codes: 0 launched, 2 bad usage, 3 active-run cap reached, 4 name already used, 5 command not found.

### Watch and stop

```bash
$B/vigia-status            # table: active runs + the last 15 finished, with task, prompt and last reply lines
$B/vigia-status --short    # table only
$B/vigia-status -n 30 --json
$B/vigia-stop /path/to/task/runs/t1-claude.status.json   # final state: killed
```

`vigia-status` also cleans up entries whose supervisor died (for example after a `kill -9`).
It kills whatever is left of the group, marks the run `killed` and sends the notification.

## Per-run files (in `<task-dir>/runs/`)

| file | content |
|---|---|
| `<name>.events.jsonl` | CLI stdout (JSON events) |
| `<name>.stderr.log` | stderr |
| `<name>.hooks.jsonl` | heartbeats from the Claude or Codex hooks (SessionStart, Pre/PostToolUse, Stop...; Codex lines carry `"origin": "codex"`) |
| `<name>.status.json` | current state (rewritten every 15 s or when events arrive; its mtime is touched every poll as the supervisor's heartbeat) |
| `<name>.prompt.md` | what the agent received: task, tool, model, prompt, system prompt, rule files, full command (redacted) |
| `<name>.conversation.md` | the exchange, turn by turn: Orquestra → agent, agent → Orquestra, tool calls, subagent calls and replies, final reply (redacted) |
| `<name>.conversation.json` | the same entries, structured (`schema_version` 1; `entries` with `ts`, `kind`, `who`, `text`) for programs |
| `<name>.status.json.lock` | per-run lock shared by the supervisor and the watchdog |
| `<name>.notified.json` | which notifications were already sent (dedupe) |
| `<name>.evidence/` | only on stalled/timeout/loop: tails of events, stderr and hooks, a `ps` of the tree and the status at capture time |
| `<name>.vigia.log` | the supervisor's own log (should stay empty) |

`status.json` contains:

- `state`, `pid`, `pgid`, `supervisor_pid`;
- `started_at`, `ended_at` and `last_event_at` (ISO 8601 with offset; local time, or `VIGIA_TZ`);
- `exit_code`, `summary` (last answer or error), `alerts`;
- `task`, `model`, `prompt_preview` and `reply_preview` (~500 chars each, redacted), `prompt_path`, `conversation_path`, `rules_files`;
- `stats`: turns, tool calls, files edited, CPU seconds, idle seconds;
- `duration_sec` (wall clock), `active_sec` (time the machine was awake), `suspended_sec` (if it was suspended);
- `webhook_env`: the `VIGIA_WEBHOOK_ENV` given at launch (`null` = default, `""` = muted);
- `incomplete_reason` and `watchdog` when those apply (see below);
- Claude's `result`: cost, turns, `is_error`;
- `kill`: what was killed, whether SIGKILL was needed, the output of `pgrep -g`, and `ok`.

## States

| state | meaning | what the orchestrator does |
|---|---|---|
| `running` | running | nothing |
| `done` | exited 0 (and, for Claude, `is_error` false) | read the result and review it |
| `failed` | non-zero exit, or Claude returned `is_error` | read `summary` and stderr; on auth or rate-limit errors, stop and tell the user |
| `stalled` | `--stall-min` with no new bytes and no CPU, or no new bytes for 3× `--stall-min` even with CPU; evidence frozen, group killed | read `evidence/` before relaunching |
| `timeout` | exceeded `--max-min` of **wall-clock** time (time the machine was suspended counts) | same |
| `loop` | same error N times, identical edit/test cycle N times, protected test file touched, or growing scope (`loop_reason`) | do not relaunch as-is; change the approach |
| `killed` | stopped by `vigia-stop`, by an external signal, or because the supervisor died | check who stopped it |
| `incomplete` | exited 0 but did not deliver: a fleet lead (`fleet-*-lead-*`) without a fresh, non-empty `T/FINAL-SUMMARY.md` (`incomplete_reason`) | treat as a failure, not `done`: read the alert and decide whether to resume |
| `limit_paused` | a Claude run (`--tool claude`) hit the subscription usage limit and its role is lead/implement/plan/unknown | wait for the reset in `limit`; relaunch `resume_command` from `T/.fleet/limit-resume.json` (the exact launcher argv, from `ORQ_RELAUNCH_ARGV`) |
| `limit_fallback` | a Claude **review** hit the limit and was relaunched on Codex (`fleet-codex reviewer`); `limit_fallback.relaunch` has the new run's `name`/`status_path`/`output` | wait on the new run; if the relaunch failed, the state stays as it was and `summary` says `fallback FAILED` |

Killing: SIGTERM to the whole group (and to descendants that changed group), wait 10 s (`VIGIA_KILL_GRACE`), then SIGKILL.
Then verify with `pgrep -g <pgid>`.
The result is in `kill.ok`. If `false`, `survivors` lists what stayed alive.

**Alerts.** Rate-limit, 429, 401 or auth text in events or stderr, and a Claude `rate_limit_event` whose status is not `allowed`, become `ALERT:` in `summary`.
Orquestra's rule: stop and tell the user.
The agent's stderr is scanned too: `Background tasks still running` (any tool; headless Claude killing pending subagents)
and, for Claude only, the auth/rate-limit patterns become `stderr: ...` alerts. A run that ends on its own after its
`--max-min` (e.g. while the machine was suspended) gets a `finished after the cap` alert. Alerts that only show up at
the end are still notified before `finished`.

### Suspension, heartbeat and watchdog

If the machine (VM) is paused mid-run, the monotonic clock stops, so a monotonic cap would never fire.
- **`--max-min` is wall-clock time.** If the wall clock jumps ahead of the monotonic clock by more than
  `VIGIA_SUSPEND_SEC` (60 s) between polls, the run gets a `box suspended ~N min during the run` alert and
  `suspended_sec` in its status.
- **`--stall-min` stays monotonic**, so resuming from suspension does not cause a false stall. Extra cap: no new bytes
  for `VIGIA_STALL_CPU_MULT` × stall-min (3×) is `stalled` even while burning CPU.
- **Heartbeat**: the supervisor touches `status.json` every poll.
- **Watchdog** (`vigia_lib.watch_active`, run by `vigia-status` before printing and by every `vigia-run` at launch; its
  actions go to stderr): for a `running` run whose supervisor is alive, if the heartbeat is older than
  `VIGIA_HB_STALE_SEC` (180 s) and stays unchanged for `VIGIA_WD_RECHECK_SEC` (20 s) of awake time, it sends an
  `alert` (`watchdog: supervisor has no heartbeat`). If the run is also past its wall-clock `--max-min` (→ `timeout`) or
  the heartbeat is older than `--stall-min` (→ `stalled`), it claims the run under `<status>.lock` (state + `watchdog`
  mark written before the kill; the supervisor never overwrites a run with that mark), sends `detected`, kills the group
  (checking the recorded `pid_start` so a reused PID is never killed) and sends `finished`. A fresh heartbeat but past
  `--max-min` + `VIGIA_WD_GRACE_SEC` (120 s) only raises an `alert`. Everything is deduplicated by `notified.json`.
- Limit: while the machine is paused nothing runs, not even the watchdog; it acts as soon as the machine resumes and
  something calls `vigia-status` or `vigia-run`. For long fleets, a periodic `vigia-status` (cron or a routine) helps.
- Tests: `python3 tests/test_vigia_watchdog.py` (temp `VIGIA_BASE`, `VIGIA_WEBHOOK_ENV=""`).

## Active-run cap

The registry lives in `$VIGIA_BASE/active/` (one file per active run, guarded by `flock`).
Default cap: 5 (`VIGIA_MAX_ACTIVE`). The next `vigia-run` is refused with a clear message and exit code 3, and nothing is created.
Entries of dead supervisors are cleaned up automatically.

## Notifications (`vigia-notify`)

The supervisor calls `vigia-notify <status.json> --event E --state S --prev P [--detail TXT]` in the background (it never blocks supervision):

| event | when | `state` |
|---|---|---|
| `started` | the agent process is up (recorded only; not POSTed unless `VIGIA_NOTIFY_STARTED=1`) | `running` |
| `alert` | a new alert (rate limit, 429, auth...) | `running` |
| `detected` | loop/stall/timeout/signal detected, right before the kill (`detail` says why); also `incomplete` for a fleet lead | `running` (or `incomplete`) |
| `finished` | final state reached; sent **exactly once**, also if the supervisor crashes (a `finally` block) or dies (`vigia-status` cleanup) | final state |

Each call:

1. Is deduplicated per run in `<name>.notified.json` (under `flock`): the same event/transition/detail is never sent twice, and `finished` only once.
2. Appends a line to `$VIGIA_BASE/events.jsonl`.
3. Skips the POST for `started` (it would wake the bot for nothing) unless `VIGIA_NOTIFY_STARTED=1`.
4. Picks the run's webhook file (below) and, if it exists, POSTs:
   ```json
   {"event","ts","run","tool","model","task","previous_state","state","transition","detail",
    "exit_code","summary","prompt_preview","reply_preview","prompt_path","conversation_path",
    "task_dir","status_path","evidence_path"}
   ```
   `exit_code` is set only on `finished`; `summary` only on `detected` and `finished`. The `finished` payload carries the final reply preview.
   The POST has a 10 s timeout and up to 4 attempts (after 0, 2, 5 and 15 s) on network errors, 5xx, 408, 425 or 429. Other 4xx responses are not retried.
   The outcome goes to `$VIGIA_BASE/notify.log`, with the host only. The key and the full URL are never logged.

Without `webhook.env`, nothing is sent and that is not an error.

**Per-run webhook (one bot per run).** `vigia-run` stores `VIGIA_WEBHOOK_ENV` from its environment in the status as
`webhook_env` (absolute path; `""` if set but empty; `null` if unset). `vigia-notify` uses, in order: the status's
`webhook_env` (if not `null`), then its own `$VIGIA_WEBHOOK_ENV`, then `$VIGIA_BASE/webhook.env`. An empty value or a
missing file means no POST (the event is still recorded in `events.jsonl`). So a run launched for another bot keeps
notifying that bot even when a different process (e.g. your `vigia-status` cleaning up) sends the notification, and
`VIGIA_WEBHOOK_ENV=""` mutes a run entirely (tests, sub-runs). Test: `python3 tests/test_vigia_notify.py`.

Example (`finished` after `vigia-stop`):

```json
{"event": "finished", "run": "t1-claude", "tool": "claude", "model": "sonnet", "task": "fix the login bug",
 "previous_state": "running", "state": "killed", "transition": "running -> killed", "exit_code": -15,
 "prompt_preview": "Fix login. key sk-*** GITHUB_TOKEN=***", "reply_preview": "step 32",
 "prompt_path": ".../runs/t1-claude.prompt.md", "conversation_path": ".../runs/t1-claude.conversation.md", "...": "..."}
```

**Redaction.** Before anything is written to `prompt.md`, `conversation.md`, `status.json`, `events.jsonl` or the webhook payload, likely secrets are masked: API keys (`sk-...`, `ghp_...`, `xox...`, `AKIA...`, JWTs...), `Bearer` tokens, private-key blocks, and `NAME=value` pairs whose name looks like a secret (`*_KEY`, `*TOKEN*`, `PASSWORD`...). Test: `python3 tests/test_vigia_comms.py`.
The raw CLI files (`<name>.events.jsonl`, `stderr.log`, `hooks.jsonl`) and `vigia.log` are **not** redacted; treat them as sensitive.

### Set up the wake-up webhook

1. Create something that runs your orchestrator when a webhook is called. In Grok Bot: a routine with a **webhook** trigger on the Orquestra bot, named e.g. "Vigia", with an instruction like "a Vigia run changed state: tell the user in 1-2 lines (task, tool/model, transition, prompt summary, prompt.md path); if event=finished or detected, read the status_path and follow your rules".
2. Copy the URL and key it gives you.
3. Create the file and protect it:
   ```bash
   cat > "$VIGIA_BASE/webhook.env" <<'X'
   VIGIA_WEBHOOK_URL=<your webhook URL>
   VIGIA_WEBHOOK_KEY=<your key>
   # VIGIA_WEBHOOK_HEADER=Authorization   (default; the value becomes "Bearer <key>")
   X
   chmod 600 "$VIGIA_BASE/webhook.env"
   ```

The sender uses `Authorization: Bearer <key>`.
If the key already starts with `Bearer `, the prefix is not duplicated.
With a different `VIGIA_WEBHOOK_HEADER`, the raw key is sent in that header.

The file is parsed as `KEY=value` and never executed. To test without touching the real one, set `VIGIA_WEBHOOK_ENV=/other/file` (or `""` to send nothing).

**Webhook caveats:**

- HTTP 200 only means the routine **started**, not that it finished.
- Routines usually have execution limits. Every run sends at least 1 notification (finished), more with alerts or kills; many runs together can produce 429s. The notifier retries but may drop one.
- `events.jsonl` + `vigia-status` are the source of truth. The webhook is only the alarm clock.

## Claude hooks (`claude-hooks.json` + `bin/vigia-claude-hook`)

- `install.sh` renders `$ORQUESTRA_HOME/bin/claude-hooks.json` from `bin/claude-hooks.json.template` with absolute paths.
- They apply only to the run that receives `--settings $ORQUESTRA_HOME/bin/claude-hooks.json`. They do not touch `~/.claude` or the repo, and they add to hooks that already exist.
- Events enabled: SessionStart, PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStop and StopFailure.
- Each one writes a line to `<name>.hooks.jsonl`, through the `VIGIA_HEARTBEAT` variable that `vigia-run` sets.
- Outside Vigia, without that variable, the hook does nothing.
- The hook writes nothing to stdout and always exits 0. The only exception is `--protect-tests`: it then returns `permissionDecision: deny` on PreToolUse for Edit, Write, MultiEdit or NotebookEdit on a test file.
- Test files: `tests/`, `test/`, `__tests__/`, `spec/`, `test_*`, `*_test.*`, `*.spec.*`, `*.test.*` and `conftest.py`.
- Warning: in `-p` mode an invalid settings file is silently ignored. If `hooks.jsonl` stays empty, be suspicious.
- Tip: if a global PreToolUse hook in `~/.claude/settings.json` points to a script that does not exist and fails closed, every tool call in `claude -p` gets blocked. Check your global settings if runs fail that way.

## Codex hooks (`bin/vigia-codex-hook`, tested with Codex CLI 0.166.0)

- `vigia-run --tool codex` adds, right after `exec`, `--dangerously-bypass-hook-trust` and a `-c hooks.<Event>=[...]` for SessionStart, PreToolUse, PostToolUse, Stop and SubagentStop.
  The caller changes nothing. `status.json` shows `"codex_hooks": true`; `cmd` stays the original command.
- It applies to this run only. It does not create or modify `~/.codex/config.toml` (or `hooks.json`), and it does not use another `CODEX_HOME` (your login stays the same).
- **Why the bypass:** Codex only runs "trusted" hooks (reviewed in `/hooks`, stored by hash in the user config). Without trust it **silently skips the hook**. `--dangerously-bypass-hook-trust` applies to this invocation only.
  - Side effect: it also enables hooks already configured for the user or for a trusted project. Review those before relying on this.
  - Codex reports this in `--json` as `item.type=error`. Vigia ignores that warning.
- Payload on stdin: `session_id`, `turn_id`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `tool_name` (`Bash`, `apply_patch`, `mcp__...`), `tool_use_id`, `tool_input.command` (for `apply_patch` it is the patch text) and, on PostToolUse, `tool_response` (for Bash it is only the output text, **without the exit code**).
- With `--protect-tests`, the hook denies (`permissionDecision: deny`) an `apply_patch` that touches a test file. Tested: the file does not change and the run becomes `loop`.
  Writing to tests through the shell is still only caught afterwards, by the heuristic.
- `--json` stays the main source (exit code, file_change, errors). From the hook, Vigia uses what `--json` does not show:
  - a denied `apply_patch`, which does not appear in `--json`;
  - commands that vanish from `--json`: in 0.166, a command blocked by the sandbox (for example a write in read-only mode) shows up in the hook but never becomes a `command_execution`.
    If no `--json` item matches the PostToolUse within 10 s, Vigia counts it as a call and as an error (`command-hook`), which feeds the "same error N times" rule.
  - Without `--json`, the hooks become the source of calls, edits and test cycles.
- To disable for a run: `VIGIA_CODEX_HOOKS=0` in `vigia-run`'s environment.
- Codex's `notify` (`-c notify=[...]`) is another option, but it only fires at the end of a turn. It does not see tools and cannot block anything.

## Observed event formats

**Claude Code 2.1.x** (`--output-format stream-json --verbose`), one JSON line per event:

- `system`/`hook_started` and `system`/`hook_response` (hooks from settings);
- `system`/`init` (`session_id`, `model`, `cwd`, `tools`...);
- `assistant` (`message.content[]` with `text` or `tool_use{id,name,input}`);
- `user` (`tool_result{tool_use_id, content, is_error}`);
- `rate_limit_event` (`rate_limit_info.status`: `allowed`...);
- `result` (`subtype`, `is_error`, `result`, `num_turns`, `total_cost_usd`, `duration_ms`, `session_id`, `permission_denials`).

**Codex 0.166.0** (`exec --json`):

- `thread.started{thread_id}`, `turn.started`;
- `item.started` and `item.completed` with `item.type`:
  - `agent_message{text}`;
  - `command_execution{command, aggregated_output, exit_code, status}`;
  - `file_change{changes[{path,kind}]}` (documented format; not yet observed live);
- `turn.completed{usage}`;
- `turn.failed` / `error` on failure.

Codex writes `Reading additional input from stdin...` to stderr. That is normal.

## Configuration reference

| variable | default | meaning |
|---|---|---|
| `ORQUESTRA_HOME` | install dir (marker `.orquestra-home`), else `~/.orquestra` | base folder |
| `VIGIA_BASE` | `$ORQUESTRA_HOME/vigia` | registry, `events.jsonl`, `notify.log`, `webhook.env` |
| `VIGIA_WEBHOOK_ENV` | unset (= `$VIGIA_BASE/webhook.env`) | per-run webhook config file, stored in the status at launch; `""` = muted |
| `VIGIA_NOTIFY_STARTED` | 0 | set to 1 to also POST `started` |
| `VIGIA_STALL_CPU_MULT` | 3 | no new bytes for this × `--stall-min` = stalled even with CPU |
| `VIGIA_SUSPEND_SEC` | 60 | wall clock ahead of monotonic by this much between polls = suspension |
| `VIGIA_HB_STALE_SEC` | 180 | watchdog: heartbeat age that makes a run suspect |
| `VIGIA_WD_RECHECK_SEC` | 20 | watchdog: awake seconds to wait for a fresh heartbeat before acting |
| `VIGIA_WD_GRACE_SEC` | 120 | watchdog: grace after `--max-min` before alerting on a live supervisor |
| `VIGIA_MAX_ACTIVE` | 5 | cap on concurrent supervised runs |
| `VIGIA_TZ` | system local | IANA zone for timestamps |
| `VIGIA_POLL_SEC` | 3 | supervisor poll interval |
| `VIGIA_CPU_TICKS` | 15 | CPU ticks per poll that count as "working" |
| `VIGIA_KILL_GRACE` | 10 | seconds between SIGTERM and SIGKILL |
| `VIGIA_CODEX_HOOKS` | 1 | set to 0 to skip Codex hook injection |
| `VIGIA_LANG` | `en` | language of the `prompt.md`/`conversation.md` headings (`en` or `pt`) |

## Known limitations

- **Test writes through the shell are heuristic.** Vigia looks for redirection, `tee`, `sed -i`, `rm`, `mv`, `cp` and `git checkout`/`restore` in the command. A script that writes to a test internally (Python, for example) goes unnoticed.
  - In Claude, Edit/Write on tests is truly blocked by the hook.
  - In Codex, `apply_patch` on tests is blocked by the hook; shell writes are only detected after the fact.
- **"No CPU" uses a threshold** of 15 ticks (about 0.15 s) per check, summed over the group and its descendants.
  - A process that burns CPU without producing any output is `stalled` after 3× `--stall-min` (`VIGIA_STALL_CPU_MULT`); otherwise `--max-min` catches it.
  - A long network call with no output (a slow model response) counts as idle. That is why the default is 10 min.
- **"Same error" loops** normalize numbers, hashes and `/tmp` paths before comparing. Similar errors with different text do not add up.
- If the supervisor is killed hard or stops watching, only the next `vigia-run` or `vigia-status` notices (cleanup / watchdog) and notifies.
- Processes that leave the group **and** detach from the tree (daemons with their own double fork) are not seen.
  - The supervisor is a *subreaper*, so orphaned descendants stay its children and are almost always caught.
- `--output-format json` in Claude does not stream. Use `stream-json --verbose`.
- With `stream-json`, Claude's hooks are ignored for liveness/alerts; subagents still show up in `conversation.md` through the `Task`/`Agent` tool calls.
- Redaction is pattern-based: a secret with no recognizable shape (a bare random string with no `NAME=`) can get through.
