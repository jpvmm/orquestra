#!/usr/bin/env sh
# Install Orquestra/Vigia and the fleet kit into $ORQUESTRA_HOME (default: ~/.orquestra).
# Safe to re-run: it refreshes bin/, fleet/ and docs, and never touches webhook.env or run state.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
ORQUESTRA_HOME=${ORQUESTRA_HOME:-$HOME/.orquestra}
VIGIA_BASE=${VIGIA_BASE:-$ORQUESTRA_HOME/vigia}
BIN="$ORQUESTRA_HOME/bin"
FLEET="$ORQUESTRA_HOME/fleet"

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 (3.9+) is required" >&2; exit 1; }
command -v pgrep >/dev/null 2>&1 || { echo "install.sh: pgrep (procps) is required" >&2; exit 1; }
[ -d /proc ] || { echo "install.sh: Vigia needs Linux (/proc)" >&2; exit 1; }

mkdir -p "$BIN" "$VIGIA_BASE/active"
touch "$ORQUESTRA_HOME/.orquestra-home"

for f in vigia-run vigia-status vigia-stop vigia-notify vigia-claude-hook vigia-codex-hook vigia_lib.py vigia_comms.py orq-launch; do
  cp "$REPO/bin/$f" "$BIN/$f"
done
chmod 755 "$BIN"/vigia-* "$BIN/orq-launch"
chmod 644 "$BIN/vigia_lib.py" "$BIN/vigia_comms.py"

# Claude Code needs absolute hook paths: render the settings file for this install.
sed "s|@VIGIA_BIN@|$BIN|g" "$REPO/bin/claude-hooks.json.template" > "$BIN/claude-hooks.json"

# Fleet kit: scripts, agent definitions and the fleet-sdd skill (prompts resolve @FLEET_HOME@ at run time).
mkdir -p "$FLEET/bin" "$FLEET/.claude/agents" "$FLEET/skill/fleet-sdd"
for f in fleet-run fleet-wt fleet-codex codex-dev codex-reviewer fleet-wait; do
  cp "$REPO/fleet/bin/$f" "$FLEET/bin/$f"
  chmod 755 "$FLEET/bin/$f"
done
cp "$REPO"/fleet/.claude/agents/*.md "$FLEET/.claude/agents/"
cp -R "$REPO/fleet/skill/fleet-sdd/." "$FLEET/skill/fleet-sdd/"
cp "$REPO/docs/fleet.md" "$FLEET/README.md"

cp "$REPO/docs/vigia.md" "$VIGIA_BASE/README.md"
cp "$REPO/prompts/orquestra.md" "$ORQUESTRA_HOME/orquestra.md"
cp "$REPO/prompts/vigia-routine.md" "$ORQUESTRA_HOME/vigia-routine.md"
[ -e "$VIGIA_BASE/webhook.env.example" ] || cp "$REPO/webhook.env.example" "$VIGIA_BASE/webhook.env.example"
[ -e "$ORQUESTRA_HOME/learnings.md" ] || printf '# Learnings\n\nWhat worked (and did not) with each tool and model.\n' > "$ORQUESTRA_HOME/learnings.md"

cat <<MSG
Installed Orquestra/Vigia:
  scripts:        $BIN
  Claude hooks:   $BIN/claude-hooks.json
  Vigia state:    $VIGIA_BASE
  Vigia docs:     $VIGIA_BASE/README.md
  bot prompt:     $ORQUESTRA_HOME/orquestra.md
  Vigia routine:  $ORQUESTRA_HOME/vigia-routine.md
  fleet:          $FLEET (docs: $FLEET/README.md)

Optional:
  export PATH="$BIN:$FLEET/bin:\$PATH"
  # wake-up webhook (see docs/vigia.md):
  cp "$VIGIA_BASE/webhook.env.example" "$VIGIA_BASE/webhook.env" && chmod 600 "$VIGIA_BASE/webhook.env"
MSG
