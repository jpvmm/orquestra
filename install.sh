#!/usr/bin/env sh
# Install Orquestra/Vigia and the fleet kit into $ORQUESTRA_HOME (default: ~/.orquestra).
# Safe to re-run: it refreshes bin/, fleet/ and docs, and never touches webhook.env, run state, the retro loop's
# bookkeeping (ledgers, archive, evals, judge log, LESSONS.md, learnings) nor any skill/config file you or the loop edited
# (those get a <file>.upstream next to them instead).
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

for f in vigia-run vigia-status vigia-stop vigia-notify vigia-claude-hook vigia-codex-hook vigia_lib.py vigia_comms.py; do
  cp "$REPO/bin/$f" "$BIN/$f"
done
chmod 755 "$BIN"/vigia-*
chmod 644 "$BIN/vigia_lib.py" "$BIN/vigia_comms.py"

# Claude Code needs absolute hook paths: render the settings file for this install.
sed "s|@VIGIA_BIN@|$BIN|g" "$REPO/bin/claude-hooks.json.template" > "$BIN/claude-hooks.json"

# Fleet kit: scripts, agent definitions and the fleet-sdd skill (prompts resolve @FLEET_HOME@ at run time).
mkdir -p "$FLEET/bin" "$FLEET/.claude/agents" "$FLEET/skill/fleet-sdd"
for f in fleet-run fleet-wt fleet-codex codex-dev codex-reviewer fleet-wait \
         fleet-retro fleet-retro-cycle fleet-retro-shadow fleet-skill-apply fleet-skill-lint fleet-skill-judge fleet-skill-eval \
         fleet_skill_lib.py fleet_judge_lib.py fleet_eval_lib.py fleet_probation.py fleet_rules_index.py; do
  [ -f "$REPO/fleet/bin/$f" ] || continue   # fleet-retro-shadow may be absent
  cp "$REPO/fleet/bin/$f" "$FLEET/bin/$f"
  case "$f" in *.py) chmod 644 "$FLEET/bin/$f" ;; *) chmod 755 "$FLEET/bin/$f" ;; esac
done

# Skill + config + agent files are the ones the retro loop (or you) may edit: install them against a recorded baseline.
# Reserved files (ledgers, archive, evals, judge-log, rules-index, retro-state, batches, proposals, LESSONS.md, learnings)
# are never shipped, so never overwritten.
python3 - "$REPO/fleet" "$FLEET" <<'PY'
import hashlib, json, os, shutil, sys

src_root, dst_root = sys.argv[1], sys.argv[2]
RESERVED = ("skill/ledger", "skill/ledger-backups/", "skill/proposals/", "skill/archive/", "skill/lint-report.md",
            "skill/retro-state.json", "skill/retro-batches/", "skill/rules-index.json", "skill/judge-log.jsonl",
            "skill/evals/", "skill/usage", "skill/.install-baseline.json")
NEVER = {"LESSONS.md", "learnings.md", "lessons-ledger.jsonl"}   # by file name, anywhere in the source tree
try:
    with open(os.path.join(src_root, "skill", "retro-policy.json")) as f:
        NEVER.add(str(json.load(f).get("lessons_file") or "learnings.md"))
except (OSError, ValueError):
    pass
base_path = os.path.join(dst_root, "skill", ".install-baseline.json")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


try:
    with open(base_path) as f:
        base = json.load(f)
    if not isinstance(base, dict):
        base = {}
except (OSError, ValueError):
    base = {}

rels = []
for top in ("skill", ".claude/agents"):
    for d, _, files in os.walk(os.path.join(src_root, top)):
        for fn in files:
            rels.append(os.path.relpath(os.path.join(d, fn), src_root).replace(os.sep, "/"))
kept = []
for rel in sorted(rels):
    if os.path.basename(rel) in NEVER or rel.endswith(".upstream") or any(rel == r.rstrip("/") or rel.startswith(r) for r in RESERVED):
        continue
    s, d = os.path.join(src_root, rel), os.path.join(dst_root, rel)
    new = sha(s)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    if not os.path.exists(d):
        shutil.copyfile(s, d)
        base[rel] = new
        continue
    cur = sha(d)
    if cur == new:
        base[rel] = new
        if os.path.exists(d + ".upstream"):
            os.remove(d + ".upstream")
    elif base.get(rel) == cur:          # untouched since we installed it: safe to update
        shutil.copyfile(s, d)
        base[rel] = new
        if os.path.exists(d + ".upstream"):
            os.remove(d + ".upstream")
    else:                               # edited locally / by the loop (or unknown history): keep it
        shutil.copyfile(s, d + ".upstream")
        kept.append(rel)

tmp = base_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(base, f, indent=1, sort_keys=True)
    f.write("\n")
os.replace(tmp, base_path)
for rel in kept:
    print("kept local edits: %s (upstream version: %s.upstream)" % (os.path.join(dst_root, rel), os.path.join(dst_root, rel)))
PY
cp "$REPO/docs/fleet.md" "$FLEET/README.md"

cp "$REPO/docs/vigia.md" "$VIGIA_BASE/README.md"
cp "$REPO/prompts/orquestra.md" "$ORQUESTRA_HOME/orquestra.md"
cp "$REPO/prompts/vigia-routine.md" "$ORQUESTRA_HOME/vigia-routine.md"
cp "$REPO/prompts/retro-routine.md" "$ORQUESTRA_HOME/retro-routine.md"
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
  retro routine:  $ORQUESTRA_HOME/retro-routine.md
  retro policy:   $FLEET/skill/retro-policy.json
  fleet:          $FLEET (docs: $FLEET/README.md)

Optional:
  export PATH="$BIN:$FLEET/bin:\$PATH"
  # wake-up webhook (see docs/vigia.md):
  cp "$VIGIA_BASE/webhook.env.example" "$VIGIA_BASE/webhook.env" && chmod 600 "$VIGIA_BASE/webhook.env"
MSG
