# Codex CLI (`codex exec`) — sandbox limits

Read when choosing who runs a step. Binary: `~/.local/bin/codex` (put `~/.local/bin` on PATH before `vigia-run`).

- Outside a git repo (cwd not in a repo), pass `--skip-git-repo-check`, or it fails at start ("Not inside a trusted directory").
- `--sandbox workspace-write` cannot open sockets (PermissionError): tests that start a server or call HTTP fail there; the lead or Orquestra runs them.
- `--sandbox workspace-write` cannot commit in a worktree (`index.lock` read-only even with `--add-dir` of the gitdir) and cannot reach Docker: tests that need Postgres/Docker and every commit stay with Claude Bash in the integration or with Orquestra.
- Default sandbox is read-only: right for reviewers; add `--sandbox workspace-write` only for devs.
