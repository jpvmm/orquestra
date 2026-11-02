#!/usr/bin/env sh
# Install Orquestra/Vigia into $ORQUESTRA_HOME (default: ~/.orquestra).
# Safe to re-run: it refreshes bin/ and docs, and never touches webhook.env or run state.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
ORQUESTRA_HOME=${ORQUESTRA_HOME:-$HOME/.orquestra}
VIGIA_BASE=${VIGIA_BASE:-$ORQUESTRA_HOME/vigia}
BIN="$ORQUESTRA_HOME/bin"

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 (3.9+) is required" >&2; exit 1; }
command -v pgrep >/dev/null 2>&1 || { echo "install.sh: pgrep (procps) is required" >&2; exit 1; }
[ -d /proc ] || { echo "install.sh: Vigia needs Linux (/proc)" >&2; exit 1; }

mkdir -p "$BIN" "$VIGIA_BASE/active"
touch "$ORQUESTRA_HOME/.orquestra-home"

for f in vigia-run vigia-status vigia-stop vigia-notify vigia-claude-hook vigia-codex-hook vigia_lib.py; do
  cp "$REPO/bin/$f" "$BIN/$f"
done
chmod 755 "$BIN"/vigia-*
chmod 644 "$BIN/vigia_lib.py"

# Claude Code needs absolute hook paths: render the settings file for this install.
sed "s|@VIGIA_BIN@|$BIN|g" "$REPO/bin/claude-hooks.json.template" > "$BIN/claude-hooks.json"

cp "$REPO/docs/vigia.md" "$VIGIA_BASE/README.md"
cp "$REPO/prompts/orquestra.md" "$ORQUESTRA_HOME/orquestra.md"
[ -e "$VIGIA_BASE/webhook.env.example" ] || cp "$REPO/webhook.env.example" "$VIGIA_BASE/webhook.env.example"
[ -e "$ORQUESTRA_HOME/learnings.md" ] || printf '# Learnings\n\nWhat worked (and did not) with each tool and model.\n' > "$ORQUESTRA_HOME/learnings.md"

cat <<MSG
Installed Orquestra/Vigia:
  scripts:        $BIN
  Claude hooks:   $BIN/claude-hooks.json
  Vigia state:    $VIGIA_BASE
  Vigia docs:     $VIGIA_BASE/README.md
  bot prompt:     $ORQUESTRA_HOME/orquestra.md

Optional:
  export PATH="$BIN:\$PATH"
  # wake-up webhook (see docs/vigia.md):
  cp "$VIGIA_BASE/webhook.env.example" "$VIGIA_BASE/webhook.env" && chmod 600 "$VIGIA_BASE/webhook.env"
MSG
