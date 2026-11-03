```
status: DONE | DONE_WITH_CAVEATS | BLOCKED | FAILED
summary: <2-4 lines: what was delivered>
files: <list of files changed in the integration>
tests: <suite command> → <result>
criteria:
- <id>: pass|fail — <command> — <short output>
blocked: <each BLOCKED entry from progress.md, or "none">
tests_changed: <which existing tests changed and why, or "none">
concerns: <or "none">
integration: <path of T/wt/integration> (no commit; applying/committing is up to Orquestra)
models: <role → tool/model used>
```
