# orq-launch

Optional helper that builds a Claude or Codex argv for a fleet **role** and
holds a **task-dir + role** lock so two implementers (or two reviews) do not
run in the same task folder at once.

`fleet-run` and `fleet-codex` work without it. Callers that want the shared
role defaults can invoke `orq-launch` (it prints JSON with `cmd` and
`vigia_args`) and feed those into `vigia-run`.

Roles: `plan`, `review` (aliases: `reviewer`, `final_reviewer`, `security`),
`implement` (alias: `dev`), `lead` (alias: `team-lead`).

Lock file: `$TASK_DIR/.orq-launch-<role>.lock`. Stale if the pid is dead and
Vigia's `active/` registry has no matching running entry. Exit `3` if the lock
is held (`--force-lock` skips acquisition for dry runs / tests).

Safety flags it always adds:

- Claude: `--disallowedTools` for `git commit/push/merge/rebase`, `gh`, `curl`, `wget`, `rm -rf`, and
  Read/Edit/Write of `.env*` / `webhook.env`; plus `--strict-mcp-config` (no MCP servers). The `plan` role
  gets only `Read,Grep,Glob`.
- Codex `workspace-write` roles: `-c sandbox_workspace_write.network_access=false`, so a user config cannot
  turn network on. `--output-schema FILE` is passed only to read-only roles (plan/review).
- The `lead` role runs with `--protect-tests` (its subagents edit inside the same process).
