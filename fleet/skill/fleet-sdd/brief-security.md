# Security review — {SCOPE}
Package: {PACKAGE}. Worktree: {WT}. Only read. Never print a secret you find (say where, not the value).
Focus: user input, injection, auth, secrets, network/SSRF, file permissions, new dependencies.
Reply:
```
verdict: APPROVED | REJECTED
findings:
- [Critical|Important|Minor] file:line — one-line attack — fix
```
