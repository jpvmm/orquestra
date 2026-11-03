# Claude Code headless (`claude -p`) — launch flags and permissions

Read when launching or debugging a `claude -p` run (lead, gate, ad-hoc). Binary: `~/.local/bin/claude` (put `~/.local/bin` on PATH).

## Flags
- Put the prompt right after `-p`: `--allowedTools`, `--disallowedTools` and `--add-dir` are variadic and swallow a later positional prompt ("Input must be provided").
- A run that writes needs an explicit `--allowedTools "Read,Edit,Write,Grep,Glob,Bash"`: `--dangerously-skip-permissions` alone leaves permissionMode at default and Write is denied.
- Path-scoped file rules are unreliable in `-p` (`Write(/abs/**)` is resolved relative to the settings file; `Write(//abs/**)` was still denied): grant bare tool names and limit scope with `--cwd`/`--add-dir` and the prompt.
- A denied Write is not lost: its full content is in the `tool_use` input in `runs/<name>.events.jsonl`; recover it instead of relaunching.
- Launch runs that touch a repo with the cwd in the worktree (`vigia-run --cwd <wt>`) and `--add-dir <wt>`: from the task folder the agent reaches for `git -C`/`uv run --directory`, which fall outside the allowlist, or has no repo access at all.
- `fleet-run` passes `--disallowedTools` with `Bash(git commit:*)`, `git push`, `git merge`, `git rebase`, `gh` and `rm -rf` on purpose: never launch a Claude run just to commit; Orquestra commits after the gate passes.

## Bash allowlists (`Bash(cmd:*)`)
- Compound commands (`;`, `|`, `&&`, `$(...)`), `git -C` and any command outside the list need an approval nobody gives headless, and the run loops: require ONE operation per Bash call and Read/Grep instead of cat/tail.
- `VAR=x python3 ...` does not match `Bash(python3:*)`: add `Bash(env:*)` or set the variable via subprocess.
- Add `Bash(git log:*)` when the run needs history, and `Bash(python3 -m json.tool:*)` to read-only runs (or tell them not to validate JSON via Bash): they otherwise improvise a validation command and loop.

## Budget
- Large planning/reading tasks burn the whole `--max-min` reading code: give an explicit reading budget ("at most N reads, then write") or a larger `--max-min`.
- When a run is cut before answering, `--resume <session>` asking only for the final answer works better than relaunching.
