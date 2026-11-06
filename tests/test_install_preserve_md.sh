#!/usr/bin/env bash
# Behavior: reinstall must not clobber a customized orquestra.md.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export ORQUESTRA_HOME="$TMP/home"
mkdir -p "$ORQUESTRA_HOME/vigia"
"$ROOT/install.sh" >/dev/null
echo "custom by user" > "$ORQUESTRA_HOME/orquestra.md"
"$ROOT/install.sh" >/tmp/install-out.txt 2>/tmp/install-err.txt || true
grep -q "custom by user" "$ORQUESTRA_HOME/orquestra.md"
test -f "$ORQUESTRA_HOME/orquestra.md.new"
grep -q "kept customized" /tmp/install-err.txt
# Identical case: no .new when content matches shipped
cp "$ROOT/prompts/orquestra.md" "$ORQUESTRA_HOME/orquestra.md"
rm -f "$ORQUESTRA_HOME/orquestra.md.new"
"$ROOT/install.sh" >/dev/null 2>/tmp/install-err2.txt
test ! -f "$ORQUESTRA_HOME/orquestra.md.new"
cmp -s "$ROOT/prompts/orquestra.md" "$ORQUESTRA_HOME/orquestra.md"
echo OK
