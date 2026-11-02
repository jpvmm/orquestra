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
Confirm `~/.orquestra/bin/vigia-run` exists.

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
- prompt: "A Vigia run finished. Read the JSON received (run, state, exit_code, summary) and its status_path, then continue the task according to your Orquestra rules: verify the result, launch the next step or tell the user."

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
Wait about 30 s, then check `~/.orquestra/vigia/notify.log` for an HTTP 2xx and that the Vigia routine woke you. If it fails, recheck the URL/key (step 5) and retry once. Delete `~/.orquestra/setup-test` afterwards.

## 7. Give the bot its rules
The working rules are in `~/.orquestra/orquestra.md` (the part after `---`).
- If the user wants a dedicated bot, offer to create one named "Orquestra" with that text as its instructions, then do steps 4–6 on that bot instead of yourself.
- Otherwise, follow those rules yourself when the user asks for software work.
Keep the prompt's hard stops: no merge, deploy, publishing or spending without the user's OK in chat.

## 8. Report
Tell the user in two or three sentences: what's installed, which CLIs work, that the Vigia test woke the bot, and how to start ("ask for a coding task in a repo"). Mention that Codex runs use `--dangerously-bypass-hook-trust` for Vigia's per-run hooks (`VIGIA_CODEX_HOOKS=0` turns it off).
