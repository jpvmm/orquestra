# Fix — {SCOPE} (round {N})
You are a fresh implementer. Worktree: {WT} (only here). Report: {REPORT}.
Fix ALL Critical and Important findings below; Minor only if trivial. Do not touch tests without saying so.
General solution, never test-specific. Run the suite: `{SUITE_COMMAND}`.

## Findings
{FINDINGS}

## Report (fixed format)
```
status: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
summary:
files:
tests: <command> → <result>
findings: <id/finding: fixed|not fixed (reason)>
concerns:
```
