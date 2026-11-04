---
name: Orquestra setup
description: Install and configure Orquestra (Vigia supervisor + bot prompt for orchestrating Claude Code and Codex) on this bot's computer, end to end, when the user asks to install, set up, update or repair Orquestra or Vigia.
---
# Orquestra setup

Goal: Orquestra installed in `~/.orquestra`, Claude Code and/or Codex ready, a webhook routine named **Vigia** waking the bot that runs the work, and one test run proving the wake-up works. Do every step yourself; stop for the user only where marked **USER**. Re-running is safe (the installer never touches `webhook.env` or run state).

Repo: https://github.com/jpvmm/orquestra (Apache 2.0).

## 1. Check the machine
Run in your shell:
```bash
uname -s; python3 --version; command -v pgrep git
command -v claude || ls ~/.local/bin/claude; command -v codex || ls ~/.local/bin/codex
```
- Needs Linux, Python 3.9+, `pgrep`, `git`. Install what's missing with the system package manager.
- Needs at least one of `claude` (Claude Code) or `codex` (Codex CLI). If missing, install the official CLI (`npm i -g @anthropic-ai/claude-code`, `npm i -g @openai/codex`).

## 2. Install or update
```bash
if [ -d ~/src/orquestra/.git ]; then git -C ~/src/orquestra pull --ff-only; else git clone https://github.com/jpvmm/orquestra.git ~/src/orquestra; fi
~/src/orquestra/install.sh
```
Confirm `~/.orquestra/bin/vigia-run` and `~/.orquestra/fleet/bin/fleet-run` exist.

The install also ships the retro loop tools (`fleet-retro-cycle`, `fleet-skill-*`) and `fleet/skill/retro-policy.json`. It is safe to re-run to upgrade: skill/config/agent files you or the loop edited are kept (the new version lands next to them as `<file>.upstream`; tell the user if any are reported), and ledgers, archive, judge log, `LESSONS.md` and `learnings.md` are never touched. No scheduler is created; the cron is documented in `docs/fleet.md`.

Smoke-test the fleet without launching anything (a temp git repo, `--dry-run`):
```bash
d=$(mktemp -d) && git -C "$d" init -q && echo 'x = 1' > "$d/a.py" && git -C "$d" add . && git -C "$d" -c user.name=t -c user.email=t@t commit -qm base
mkdir -p "$d/T" && echo 'Smoke test.' > "$d/T/plan.md" && echo '{"criteria":[{"id":"C1","command":"true"}]}' > "$d/T/criteria.json"
echo '{"tasks":[{"id":"1","files":["a.py"]}]}' > "$d/T/tasks.json"
VIGIA_WEBHOOK_ENV="" ~/.orquestra/fleet/bin/fleet-run --task-dir "$d/T" --complexity M --lead claude --task smoke --repo "$d" --dry-run
rm -rf "$d"
```
It must print JSON with `"dry_run": true` and a `vigia-run` command.

## 3. Make sure the CLIs are logged in
Test each installed CLI with a trivial, cheap call:
```bash
claude -p "reply ok" --max-turns 1            # Claude Code
codex exec "reply ok" --skip-git-repo-check   # Codex
```
If one says it is not logged in: **USER** — the login is theirs. Start the CLI's login (`claude` then `/login`, or `codex login`), and hand them the screen or the login link so they finish it. Never ask for passwords in chat. Skip a CLI the user doesn't want.

## 4. Create the Vigia routine
On the bot that will run the work (usually yourself), create a routine with a **webhook** trigger (read the `routines` skill first):
- name: `Vigia`
- trigger: `{ "type": "webhook" }`
- prompt: the text after `---` in `~/.orquestra/vigia-routine.md` (relay rules + the fleet addendum).

One routine per bot: by default every run notifies `~/.orquestra/vigia/webhook.env`. To send a run's notifications to a different bot, launch it with `VIGIA_WEBHOOK_ENV=/abs/path/other-webhook.env` (stored per run; `""` mutes it), or for the fleet `FLEET_WEBHOOK_ENV=/abs/path/other-webhook.env fleet-run ...`. Codex sub-runs of a fleet lead are muted unless `FLEET_NOTIFY_SUBRUNS=1`.

## 5. Connect the webhook
**USER**: the URL and key appear only in the routine's panel. Tell the user, in one short message: open the bot's info pane (click the bot's name in the chat header), open the **Vigia** routine, and copy its webhook URL and key. Then request each value with a masked secret request (never in plain chat):
- `VIGIA_WEBHOOK_URL`
- `VIGIA_WEBHOOK_KEY`

Then write the file without printing it:
```bash
umask 077; printf 'VIGIA_WEBHOOK_URL=%s\nVIGIA_WEBHOOK_KEY=%s\n' "$VIGIA_WEBHOOK_URL" "$VIGIA_WEBHOOK_KEY" > ~/.orquestra/vigia/webhook.env
chmod 600 ~/.orquestra/vigia/webhook.env
```
Never cat, echo or log this file.

## 6. Test the wake-up
```bash
~/.orquestra/bin/vigia-run --tool other --task-dir ~/.orquestra/setup-test --name wake-test --max-min 1 -- sh -c 'echo hi; exit 1'
```
Wait about 30 s, then check `~/.orquestra/vigia/notify.log` for an HTTP 2xx and that the Vigia routine woke you (`started` is only recorded, not sent; the `finished` notification is what wakes you). If it fails, recheck the URL/key (step 5) and retry once. Delete `~/.orquestra/setup-test` afterwards.

## 7. Give the bot its rules
The working rules are in `~/.orquestra/orquestra.md` (the part after `---`).
- If the user wants a dedicated bot, offer to create one named "Orquestra" with that text as its instructions, then do steps 4–6 on that bot instead of yourself.
- Otherwise, follow those rules yourself when the user asks for software work.
The prompt is autonomous: the bot commits, pushes, opens PRs, merges and deploys itself after its own check. Keep its hard stops: never spend money or send external messages for the user, never force-push or rewrite main, never delete repos/branches/data it did not create, never touch secrets, `~/.ssh` or `~/.claude`. If the user wants an approval gate, apply the one-sentence change described in the prompt's header.

## 8. Report
Tell the user in two or three sentences: what's installed (Vigia + fleet), which CLIs work, that the Vigia test woke the bot, and how to start ("ask for a coding task in a repo"). Mention that Codex runs use `--dangerously-bypass-hook-trust` for Vigia's per-run hooks (`VIGIA_CODEX_HOOKS=0` turns it off).
