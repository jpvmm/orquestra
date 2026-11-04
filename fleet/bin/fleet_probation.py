"""Probation, auto-revert, ledger-derived cooldown and promotion transactions for generic rules (PLAN F4, F3.1 rules
3 and 5). Python 3.9+ standard library only. Pure functions over the generic ledger, the rules index, the probe store
and the task folders; 0 tokens.

State (no duplicated state; the ledger is the source of truth):
- `fleet-skill-apply DIFF --probation [--txn ID] [--failure-probe ID]` writes an apply entry with `probation: true`
  (generic scope only, one file) and marks the rules it adds with index status "probation". An entry is IN PROBATION
  while it is not reverted and has no `op=probation-end` entry. sync_index() re-derives the index statuses from that
  (crash between the ledger append and the index update is repaired on the next run).
- `op=probation-end` {of, result: "active", windows}: written by activate() after policy probation.windows_to_active
  clean windows; the rules go to status "active".
- `op=auto-revert` {reverted, revert_entry, cand_fp, lineage, target, decision_id, metric, metric_text,
  counterexample_probe, near_text, rule_hashes}: written by auto_revert() only AFTER `fleet-skill-apply --revert <id>`
  (never --force) succeeded. A drift refusal is reported and writes nothing; a manual revert is not an auto-revert.
  These entries are the ONLY input of cooldown_state() (judge memory rule 3) and of precedent_for() (the judge's
  "applied and auto-reverted by <metric>" precedent with the counterexample probe).
- Promotion transactions: begin_txn() writes the intent <fleet root>/skill/ledger-txns/<txn>.json (bookkeeping: under
  the RESERVED "skill/ledger" prefix) BEFORE the generic rule is applied with --txn; recover_txns() archives the project
  copies (fleet-skill-apply --op archive --txn, never deletes) only once the generic entry is active, and is idempotent:
  a copy whose archive entry (same txn + file + block, verified via block_sha256) is already in the project ledger is
  never archived twice; two copies in one file are two distinct copies.

Metrics (collect_metrics): lead runs (runs/fleet-*-lead-*.status.json in a final state) of every task folder
<orq root>/<project>/<task>/. WINDOW ATTRIBUTION: progress.md / criteria.json are the task's CURRENT state, so a
task folder's facts (and its final state = state of its last lead run) are counted ONCE, in the window that holds
the end time of its LAST lead run — never in an earlier window; a task whose last run ended after the rule was
applied belongs to "after", never to the baseline; a task with a lead run not yet final is open and counted nowhere.
At most STATUS_CAP status files are read per runs/ dir (more = error → extend). Per window: runs, not_done (tasks
whose final state != done), BLOCKED
entries (bullets under a "## BLOCKED" heading + table rows with a BLOCKED cell in progress.md), review rounds (table
cells "rN" in progress.md, max per row), failed criteria (criteria.json items without passes: true). Agent-written
files are opened with O_NOFOLLOW, regular files only, bounded; symlinked task/runs dirs are skipped. An unreadable or
invalid file in the window is an error and the decision is "extend" (fail closed), never a verdict.
Window length = policy cycle.due_hours. Rates compared after vs the 2 windows before the rule:
  delta = (after - baseline) / max(baseline, 1.0); regression when delta > probation.regression_threshold
(rates in [0, 1] are compared in absolute terms, rounds per task relatively). A window with fewer than
probation.min_runs lead runs grows by one more window length until it has them; without them the probation extends.
A baseline with fewer than min_runs runs gives no regression comparison (recurrence is still checked).
Recurrence: an occurrence of the entry's target failure probe from a run that ENDED in the window: the occurrence
log <fleet root>/skill/evals/occurrences.jsonl (record_occurrence, called by the cycle runner every time a failure
probe is recorded, new or already stored — the probe store dedupes identical content) plus each stored probe's own
source_run; it matches when it is the target itself (same id/hash), corrected_from it, carries all of its tags, or
has a near-identical situation text. An unreadable occurrence log → extend (fail closed).
Auto-revert marking is structural: the revert entry auto_revert() makes carries auto_revert: true (fleet-skill-apply
--revert ID --auto-revert-of ID); fleet-skill-apply refuses a human --reason starting with "auto-revert " without it."""
import datetime, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, time

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import fleet_eval_lib as E  # noqa: E402
import fleet_judge_lib as J  # noqa: E402
import fleet_rules_index as R  # noqa: E402
import fleet_skill_lib as L  # noqa: E402

APPLY = os.path.join(BIN, "fleet-skill-apply")
PROBATION_END = "probation-end"
AUTO_REVERT = "auto-revert"
TXN_DIR = os.path.join("skill", "ledger-txns")
TXN_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PROBE_RX = re.compile(r"fail-[0-9a-f]{16}")
LEAD_RX = re.compile(r"fleet-.*-lead-")
FINAL_STATES = J.FINAL_STATES
FILE_CAP = 1_000_000            # bytes read from one agent-written file
STATUS_CAP = 200                # status files read per runs/ dir; more = error (fail closed)
OCCURRENCES = "occurrences.jsonl"   # <fleet root>/skill/evals/occurrences.jsonl (RESERVED via skill/evals/)
OCC_CAP = 8_000_000             # bytes of the occurrence log
RUN_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
RATES = ("not_done_rate", "blocked_per_task", "rounds_per_task", "failed_criteria_rate")
_ROUND_RX = re.compile(r"r(\d{1,3})\b")
APPLY_TIMEOUT = 300


# ------------------------------------------------------------------ small helpers
def _epoch(v):
    """Epoch seconds of None (now) / number / datetime / ISO text (also the +HHMM offsets of fleet_skill_lib.now_iso).
    ValueError when it cannot be parsed."""
    if v is None:
        return time.time()
    if isinstance(v, bool):
        raise ValueError("bad time")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)).timestamp()
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", str(v).strip().replace("Z", "+00:00"))
    d = datetime.datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()


def _iso(ts):
    return None if ts is None else datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_or_none(v):
    try:
        return _epoch(v) if v is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def lineage_of(relation):
    """The rule id a refines:/supersedes: relation names (hex prefix), else None."""
    kind, _, ref = str(relation or "").strip().lower().partition(":")
    return ref if kind in ("refines", "supersedes") and re.fullmatch(r"[0-9a-f]{6,64}", ref) else None


def _same_lineage(a, b):
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def _ledger(ledger=None):
    return list(ledger) if ledger is not None else L.read_ledger(L.generic_scope())


def entry_files(e):
    return [f.get("path") for f in (e.get("files") or []) if isinstance(f, dict)]


def _reverted_ids(led):
    return {str(e.get("reverts")) for e in led if e.get("op") == "revert"}


def _ended_ids(led):
    return {str(e.get("of")) for e in led if e.get("op") == PROBATION_END}


def auto_revert_entries(ledger=None):
    """The op=auto-revert entries of the generic ledger (completed auto-reverts)."""
    return [e for e in _ledger(ledger) if e.get("op") == AUTO_REVERT]


# ------------------------------------------------------------------ probation slots (F3.1 rule 5)
def probation_entries(ledger=None):
    """Generic apply entries applied with --probation that are still in probation (not reverted, not ended)."""
    led = _ledger(ledger)
    gone = _reverted_ids(led) | _ended_ids(led)
    return [e for e in led if e.get("op") == "apply" and e.get("probation") is True and str(e.get("id")) not in gone]


def probation_holders(target_file, ledger=None):
    """Ids of the entries in probation that touch target_file (a diff path such as skill/fleet-sdd/SKILL.md)."""
    return [str(e.get("id")) for e in probation_entries(ledger) if target_file in entry_files(e)]


def probation_slot_free(target_file, root=None, policy=None, ledger=None):
    """True when one more rule may enter probation on target_file: always for a file that is not always loaded
    (policy always_loaded), else fewer than probation.max_per_always_loaded_file entries in probation on it."""
    pol = policy or L.load_policy(root)
    if target_file not in pol["always_loaded"]:
        return True
    return len(probation_holders(target_file, ledger)) < int(pol["probation"]["max_per_always_loaded_file"])


def take_slot(c, slots, *, root=None, policy=None, ledger=None):
    """Reserve a probation slot for candidate c in the shared map `slots` {always-loaded file: free slots} (filled lazily
    from the ledger). → True when c needs none (not generic / not an always-loaded file) or one was free and is now
    taken; False when the file has no free slot left. release_slot gives it back."""
    pol = policy or L.load_policy(root)
    tgt, scope = str(c.get("target") or ""), str(c.get("scope") or "generic")
    if scope != "generic" or tgt not in pol["always_loaded"]:
        return True
    if tgt not in slots:
        slots[tgt] = int(pol["probation"]["max_per_always_loaded_file"]) - len(probation_holders(tgt, _ledger(ledger)))
    if slots[tgt] <= 0:
        return False
    slots[tgt] -= 1
    return True


def release_slot(c, slots, *, policy=None, root=None):
    """Give back a slot taken by take_slot for a candidate that will not be applied."""
    pol = policy or L.load_policy(root)
    tgt = str(c.get("target") or "")
    if str(c.get("scope") or "generic") == "generic" and tgt in pol["always_loaded"] and tgt in slots:
        slots[tgt] += 1


def gate_candidates(candidates, *, now=None, root=None, policy=None, ledger=None, task_dirs=None, slots=None):
    """Code gate BEFORE the judge (0 tokens) → (to_judge, held). A generic candidate in cooldown (cooldown_state of its
    cand_fp / lineage) is held with status "cooldown"; a generic candidate for an always-loaded file is held with status
    "waiting" when the file has no free probation slot, counting the slots already taken in the ledger and the earlier
    candidates of this list for the same file. Held candidates are never sent to the judge.
    `slots` (optional, mutable {file: free slots}) is shared by several calls of one cycle (several origin batches): a slot
    reserved by an earlier call is not free for a later one.
    held items: dict(candidate, status, reason[, cooldown])."""
    pol = policy or L.load_policy(root)
    led = _ledger(ledger)
    free = slots if slots is not None else {}
    to_judge, held = [], []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        tgt, scope = str(c.get("target") or ""), str(c.get("scope") or "generic")
        if scope == "generic":
            cd = cooldown_state(c.get("cand_fp"), lineage_of(c.get("relation")), now, root=root, policy=pol,
                                ledger=led, probes=c.get("probes"), task_dirs=task_dirs)
            if cd["cooldown"]:
                held.append(dict(c, status="cooldown", reason=cd["reason"], cooldown=cd))
                continue
        if not take_slot(c, free, root=root, policy=pol, ledger=led):
            held.append(dict(c, status="waiting", reason=f"{tgt} already has a rule in probation (max "
                             f"{pol['probation']['max_per_always_loaded_file']} per always-loaded file): waits"))
            continue
        to_judge.append(c)
    return to_judge, held


def judge_gated(candidates, **judge_kwargs):
    """gate_candidates() then fleet_judge_lib.judge() on the candidates that passed (no call at all when none did).
    → judge result + "held" (the waiting / cooldown candidates, never judged)."""
    gate_kw = {k: judge_kwargs.get(k) for k in ("now", "root", "policy")}
    to_judge, held = gate_candidates(candidates, **gate_kw)
    if to_judge:
        res = J.judge(to_judge, **judge_kwargs)
    else:
        res = {"verdicts": [], "usage": J._usage0(), "prompt_chars": 0, "dry_run": bool(judge_kwargs.get("dry_run"))}
    res["held"] = held
    return res


# ------------------------------------------------------------------ rule hashes of an entry / index status
def added_rule_hashes(diff_text):
    """Hashes of the rules a unified diff adds (added lines whose rule is not also removed in the same file)."""
    out = []
    for _path, (rem, add) in sorted(R._diff_sections(diff_text or "").items()):
        rem_h = set(R._rule_hashes(rem))
        out += [h for h in R._rule_hashes(add) if h not in rem_h and h not in out]
    return out


def _backup_diff(e, scope=None):
    """Text of the backed-up change.diff of a ledger entry (no symlink followed); None when missing."""
    sc = scope or L.generic_scope()
    lid = str(e.get("id"))
    if not R.LEDGER_ID_RX.fullmatch(lid):
        return None
    return R._read_nofollow(sc.anchor, os.path.join(sc.rel(sc.backups), lid, "change.diff"))


def entry_rule_hashes(e, scope=None):
    return added_rule_hashes(_backup_diff(e, scope) or "")


def _index_status(idx, h):
    e = (idx.get("rules") or {}).get(h)
    if not e:
        return None
    for p in [e] + list(e.get("others") or []):
        if p.get("scope") == "generic":
            return p.get("status")
    return None


def mark_probation(hashes, ledger_id, root=None):
    """Index status "probation" for these rule hashes (generic places). → hashes changed."""
    done = []
    idx = R.load_index(root)
    for h in hashes:
        if _index_status(idx, h) == "active" and R.set_status(h, "probation", root=root, scope="generic",
                                                                            probation_ledger_id=str(ledger_id)):
            done.append(h)
    return done


def sync_index(root=None, ledger=None):
    """Re-derive the index statuses from the ledger (crash repair, idempotent): rules of entries in probation →
    "probation"; rules of ended entries still marked probation → "active". → {"probation": [...], "active": [...]}"""
    led = _ledger(ledger)
    out = {"probation": [], "active": []}
    for e in probation_entries(led):
        out["probation"] += mark_probation(entry_rule_hashes(e), e.get("id"), root)
    idx = R.load_index(root)
    ended = _ended_ids(led)
    for e in led:
        if e.get("op") == "apply" and e.get("probation") is True and str(e.get("id")) in ended:
            for h in entry_rule_hashes(e):
                if _index_status(idx, h) == "probation" and R.set_status(h, "active", root=root, scope="generic"):
                    out["active"].append(h)
    return out


# ------------------------------------------------------------------ task folders and metrics
def task_dirs(orq=None):
    """<orq root>/<project>/<task> folders that have a runs/ dir (projects per fleet_skill_lib.valid_project);
    symlinked project / task / runs dirs are skipped."""
    orq = orq or L.orq_root()
    out = []
    try:
        names = sorted(os.listdir(orq))
    except OSError:
        return out
    for p in names:
        pd = os.path.join(orq, p)
        if not L.valid_project(p) or os.path.islink(pd) or not os.path.isdir(pd):
            continue
        try:
            tasks = sorted(os.listdir(pd))
        except OSError:
            continue
        for t in tasks:
            td = os.path.join(pd, t)
            rd = os.path.join(td, "runs")
            if os.path.islink(td) or not os.path.isdir(td) or os.path.islink(rd) or not os.path.isdir(rd):
                continue
            out.append(td)
    return out


def _read_in(dfd, name, cap=FILE_CAP):
    """Bytes of the REGULAR file `name` in directory fd dfd, opened O_NOFOLLOW (never blocks on a FIFO), at most cap
    bytes. None when missing; ValueError when symlinked, not regular, unreadable or larger than cap."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{name}: refused (symlink or unreadable: {e.__class__.__name__})")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{name}: not a regular file")
        chunks, n = [], 0
        while n <= cap:
            c = os.read(fd, min(65536, cap + 1 - n))
            if not c:
                break
            chunks.append(c)
            n += len(c)
        if n > cap:
            raise ValueError(f"{name}: larger than {cap} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _label(T):
    return "/".join(os.path.abspath(T).split(os.sep)[-2:])


def _task_runs(T, lead_only=True):
    """→ (runs [{name, state, ended, final}], errors [{file, error, mtime}], skipped reason | None). A run that is not
    in a final state is listed with final False and ended None (its task is still open). More than STATUS_CAP status
    files in runs/ (agent-written) → nothing is read and one error with mtime None (fails closed in every window)."""
    try:
        dfd = L._open_dir(T, "runs", create=False)
    except OSError:
        return [], [], f"{_label(T)}: runs/ missing or symlinked (skipped)"
    runs, errors = [], []
    try:
        names = sorted(n for n in os.listdir(dfd) if n.endswith(".status.json"))
        if len(names) > STATUS_CAP:
            return [], [{"file": f"{_label(T)}/runs", "error": f"{len(names)} status files (more than {STATUS_CAP}): "
                         "not read", "mtime": None}], None
        for name in names:
            rn = name[: -len(".status.json")]
            if lead_only and not LEAD_RX.match(rn):
                continue
            try:
                mtime = os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mtime
            except OSError:
                mtime = None
            try:
                raw = _read_in(dfd, name)
                st = json.loads((raw or b"").decode("utf-8"))
                if not isinstance(st, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError) as e:
                errors.append({"file": f"{_label(T)}/runs/{name}", "error": str(e)[:200], "mtime": mtime})
                continue
            state = st.get("state")
            if state not in FINAL_STATES:
                runs.append({"name": rn, "state": state, "ended": None, "final": False})
                continue
            ended = _ts_or_none(st.get("ended_at"))
            if ended is None:
                if mtime is None:
                    errors.append({"file": f"{_label(T)}/runs/{name}", "error": "no ended_at", "mtime": None})
                    continue
                ended = mtime
            runs.append({"name": rn, "state": state, "ended": ended, "final": True})
    finally:
        os.close(dfd)
    return runs, errors, None


def progress_facts(text):
    """(BLOCKED count, [review rounds per table row]) of a progress.md: bullets under a "## BLOCKED" heading plus table
    rows with a cell starting with BLOCKED; a table row's rounds = the largest "rN" cell."""
    blocked, rounds, section = 0, [], ""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = re.sub(r"^#+\s*", "", s).strip().upper()
            continue
        if section.startswith("BLOCKED") and re.match(r"[-*]\s+\S", s):
            blocked += 1
            continue
        if s.startswith("|") and not re.fullmatch(r"[|:\-\s]+", s):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if any(c.upper().startswith("BLOCKED") for c in cells):
                blocked += 1
            rs = [int(m.group(1)) for m in (_ROUND_RX.match(c) for c in cells) if m]
            if rs:
                rounds.append(max(rs))
    return blocked, rounds


def _task_facts(T):
    """{blocked, rounds, round_rows, failed_criteria, criteria} of a task folder → (facts, errors)."""
    facts = {"blocked": 0, "rounds": 0, "round_rows": 0, "failed_criteria": 0, "criteria": 0}
    try:
        dfd = L._open_dir(T, "", create=False)
    except OSError as e:
        return None, [{"file": _label(T), "error": f"task dir refused: {e.__class__.__name__}"}]
    errors = []
    try:
        try:
            raw = _read_in(dfd, "progress.md")
            b, rs = progress_facts((raw or b"").decode("utf-8", errors="replace"))
            facts.update(blocked=b, rounds=sum(rs), round_rows=len(rs))
        except ValueError as e:
            errors.append({"file": f"{_label(T)}/progress.md", "error": str(e)[:200]})
        try:
            raw = _read_in(dfd, "criteria.json")
            if raw is not None:
                crit = json.loads(raw.decode("utf-8"))
                items = crit.get("criteria") if isinstance(crit, dict) else crit
                if not isinstance(items, list):
                    raise ValueError("criteria.json: no criteria list")
                facts["criteria"] = len(items)
                facts["failed_criteria"] = sum(1 for c in items if not (isinstance(c, dict) and c.get("passes") is True))
        except (ValueError, UnicodeDecodeError) as e:
            errors.append({"file": f"{_label(T)}/criteria.json", "error": str(e)[:200]})
    finally:
        os.close(dfd)
    return (None if errors else facts), errors


def collect_metrics(task_dirs, since, until):
    """Deterministic metrics of the window [since, until) over the given task folders (see the module doc).
    Window attribution: a task folder's facts (progress.md, criteria.json, final state = state of its last lead run)
    are counted ONCE, in the window holding the end time of its LAST lead run; "runs" = that task's lead runs ending
    in the window. A task with a lead run not yet in a final state is open: counted nowhere (listed in "open").
    → {since, until, runs, run_names, tasks, not_done, blocked, rounds, round_rows, failed_criteria, criteria,
    rates: {not_done_rate (per task), blocked_per_task, rounds_per_task, failed_criteria_rate}, errors, skipped, open}"""
    since, until = _epoch(since), _epoch(until)
    m = {"since": _iso(since), "until": _iso(until), "runs": 0, "run_names": [], "tasks": 0, "not_done": 0,
         "blocked": 0, "rounds": 0, "round_rows": 0, "failed_criteria": 0, "criteria": 0, "errors": [], "skipped": [],
         "open": []}
    for T in task_dirs or []:
        runs, errs, skipped = _task_runs(T)
        if skipped:
            m["skipped"].append(skipped)
            continue
        m["errors"] += [{"file": x["file"], "error": x["error"]} for x in errs
                        if x.get("mtime") is None or since <= x["mtime"] < until]
        if any(not r["final"] for r in runs):
            m["open"].append(_label(T))
            continue
        if not runs:
            continue
        last = max(runs, key=lambda r: (r["ended"], r["name"]))
        if not since <= last["ended"] < until:
            continue   # the task belongs to the window of its last lead run only
        inwin = [r for r in runs if since <= r["ended"] < until]
        m["runs"] += len(inwin)
        m["run_names"] += [r["name"] for r in inwin]
        facts, ferrs = _task_facts(T)
        m["errors"] += ferrs
        if facts:
            m["tasks"] += 1
            m["not_done"] += int(last["state"] != "done")
            for k in ("blocked", "rounds", "round_rows", "failed_criteria", "criteria"):
                m[k] += facts[k]
    m["run_names"].sort()
    m["rates"] = {
        "not_done_rate": m["not_done"] / m["tasks"] if m["tasks"] else 0.0,
        "blocked_per_task": m["blocked"] / m["tasks"] if m["tasks"] else 0.0,
        "rounds_per_task": m["rounds"] / m["round_rows"] if m["round_rows"] else 0.0,
        "failed_criteria_rate": m["failed_criteria"] / m["criteria"] if m["criteria"] else 0.0}
    return m


def compare(after, baseline, threshold):
    """Rates of `after` that regressed vs `baseline`: [{metric, after, baseline, delta}], delta = (a - b) / max(b, 1)."""
    out = []
    for k in RATES:
        a, b = float(after["rates"][k]), float(baseline["rates"][k])
        delta = (a - b) / max(b, 1.0)
        if delta > threshold:
            out.append({"metric": k, "after": round(a, 4), "baseline": round(b, 4), "delta": round(delta, 4)})
    return out


def run_end_times(task_dirs_=None):
    """{run name: ended epoch} of every final run (lead or not) of the task folders."""
    out = {}
    for T in task_dirs(None) if task_dirs_ is None else task_dirs_:
        runs, _, _ = _task_runs(T, lead_only=False)
        for r in (r for r in runs if r["final"]):
            out[r["name"]] = max(r["ended"], out.get(r["name"], r["ended"]))
    return out


# ------------------------------------------------------------------ target failure / recurrence
def target_failure_probe(entry, root=None):
    """The failure probe a probation entry targets: its failure_probe field (fleet-skill-apply --failure-probe), else
    the failure probe the judge cited for its decision_id in the judge log. None when unknown or unreadable."""
    pid = entry.get("failure_probe")
    if not (isinstance(pid, str) and PROBE_RX.fullmatch(pid)) and entry.get("decision_id"):
        pid = None
        for line in J.read_log(root):
            if line.get("decision_id") == entry.get("decision_id"):
                pid = next((p.get("id") for p in line.get("probes") or [] if isinstance(p, dict)
                            and PROBE_RX.fullmatch(str(p.get("id") or ""))), None)
    if not (isinstance(pid, str) and PROBE_RX.fullmatch(pid)):
        return None
    try:
        return E.load_probe(pid, root)
    except (E.ProbeError, OSError, ValueError):
        return None


def _probe_matches(p, target):
    if p.get("corrected_from") == target.get("id"):
        return True
    tags = set(target.get("tags") or [])
    if tags and tags <= set(p.get("tags") or []):
        return True
    r = L.near_dup_ratio(L.normalize_rule(p.get("situation", "")), L.normalize_rule(target.get("situation", "")))
    return r is not None and r >= L.NEAR


def occurrences_path(root=None):
    return os.path.join(E.evals_dir(root), OCCURRENCES)


def _occ_raw(root=None):
    """Bytes of the occurrence log (no symlink followed, regular file, bounded); None when absent. ValueError."""
    try:
        raw = E._read_bytes(occurrences_path(root), OCC_CAP + 1)
    except (E.ProbeError, OSError) as e:
        raise ValueError(f"{OCCURRENCES}: refused ({e.__class__.__name__})")
    if raw is not None and len(raw) > OCC_CAP:
        raise ValueError(f"{OCCURRENCES}: larger than {OCC_CAP} bytes")
    return raw


def read_occurrences(root=None):
    """The failure occurrence log → (occurrences [{probe_id, hash, source_run, ts}], errors [text]). An unreadable
    log or an invalid line is an error (callers fail closed: the probation extends)."""
    try:
        raw = _occ_raw(root)
    except ValueError as e:
        return [], [str(e)[:200]]
    out, errs = [], []
    for i, line in enumerate((raw or b"").decode("utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            o = None
        if not (isinstance(o, dict) and PROBE_RX.fullmatch(str(o.get("probe_id")))
                and RUN_RX.fullmatch(str(o.get("source_run")))):
            errs.append(f"{OCCURRENCES}:{i}: invalid occurrence")
            continue
        out.append(o)
    return out, errs


def record_occurrence(probe_id, source_run, ts=None, root=None):
    """Record that failure probe `probe_id` occurred in run `source_run` (at ts; default now) in
    <fleet root>/skill/evals/occurrences.jsonl. The probe store dedupes identical content (the frozen probe keeps its
    FIRST source_run), so this log is what makes an exact repeat visible to the probation recurrence check.
    Contract (T7): call it EVERY time a failure probe is recorded — E.record_failure status created, corrected OR
    exists — with the run the failure was seen in. Idempotent per (probe_id, source_run); atomic rewrite under the
    probe store lock. The probe must exist (hash-verified) and be a failure probe. → {created, occurrence}.
    ValueError on a bad id/run/ts, an unknown probe or an unreadable log (never rewritten over)."""
    if not (isinstance(probe_id, str) and PROBE_RX.fullmatch(probe_id)):
        raise ValueError(f"bad failure probe id {probe_id!r}")
    if not (isinstance(source_run, str) and RUN_RX.fullmatch(source_run)):
        raise ValueError(f"bad run name {source_run!r}")
    t = _epoch(ts)
    try:
        p = E.load_probe(probe_id, root)
    except E.ProbeError as e:
        raise ValueError(str(e)) from e
    if p.get("kind") != "failure":
        raise ValueError(f"{probe_id} is not a failure probe")
    with E._Lock(root):
        raw = _occ_raw(root)
        occ, errs = read_occurrences(root)
        if errs:
            raise ValueError("occurrence log invalid: " + errs[0])
        found = next((o for o in occ if o["probe_id"] == probe_id and o["source_run"] == source_run), None)
        if found:
            return {"created": False, "occurrence": found}
        rec = {"probe_id": probe_id, "hash": p["hash"], "source_run": source_run, "ts": _iso(t)}
        text = (raw or b"").decode("utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        E._atomic_write(occurrences_path(root), text + json.dumps(rec, sort_keys=True) + "\n")
    return {"created": True, "occurrence": rec}


def recurrences(target, since, until, run_times, root=None, occurrences=None):
    """Probes that show the target failure again from a run that ENDED in [since, until) (run_times {run: epoch}).
    Sources: the occurrence log (record_occurrence; `occurrences` = read_occurrences()[0], read when None) and every
    stored failure probe's own source_run. A probe matches when it IS the target (same id or hash: an exact repeat),
    is corrected_from it, carries all of its tags, or has a near-identical situation. Unknown runs never count."""
    if not target:
        return []
    probes = {p["id"]: p for p in E.list_probes(root, kind="failure")}
    if occurrences is None:
        occurrences = read_occurrences(root)[0]
    pairs = {(o["probe_id"], o["source_run"]) for o in occurrences}
    pairs |= {(pid, p.get("source_run")) for pid, p in probes.items()}
    out = []
    for pid, run in sorted(pairs, key=lambda x: (str(x[0]), str(x[1]))):
        end = run_times.get(run)
        if end is None or not since <= end < until or any(x["id"] == pid for x in out):
            continue
        p = probes.get(pid)
        if p is None:
            continue   # not a valid stored failure probe (hash-verified by list_probes): ignored
        if pid == target.get("id") or p.get("hash") == target.get("hash") or _probe_matches(p, target):
            out.append(p)
    return out


# ------------------------------------------------------------------ evaluation
def metric_text(metric):
    """One short line for the ledger / judge precedent: what triggered the auto-revert."""
    if not metric:
        return "probation metrics"
    if metric.get("text"):
        return str(metric["text"])[:300]
    if metric.get("recurrence"):
        return (f"recurrence of the target failure (probe {', '.join(metric['recurrence'][:3])}) in "
                f"{metric.get('runs', '?')} run(s) after the rule")
    parts = [f"{r['metric']} {r['baseline']:.2f}->{r['after']:.2f} (+{r['delta']:.2f} > {metric.get('threshold')})"
             for r in metric.get("regressed") or []]
    return "regression " + "; ".join(parts) + f" over {metric.get('runs', '?')} run(s)"


def evaluate_probation(entry, now=None, *, task_dirs_=None, root=None, policy=None):
    """Decision for one entry in probation (pure: writes nothing).
    → {decision: extend|revert|activate, reason, metric?, windows, baseline, counterexample_probe?, errors?}"""
    pol = policy or L.load_policy(root)
    pr = pol["probation"]
    W = float(pol["cycle"]["due_hours"]) * 3600
    min_runs, threshold = int(pr["min_runs"]), float(pr["regression_threshold"])
    out = {"id": str(entry.get("id")), "windows": [], "baseline": None}
    start, now = _ts_or_none(entry.get("ts")), _epoch(now)
    if start is None or W <= 0:
        return dict(out, decision="extend", reason="entry has no valid ts (or window length): cannot evaluate",
                    errors=["bad ts"])
    dirs = task_dirs(None) if task_dirs_ is None else list(task_dirs_)
    base = collect_metrics(dirs, start - 2 * W, start)
    out["baseline"] = {k: base[k] for k in ("since", "until", "runs", "rates")}
    if base["errors"]:
        return dict(out, decision="extend", reason="unreadable task files in the baseline windows (fail closed)",
                    errors=base["errors"][:10])
    target = target_failure_probe(entry, root)
    occ, occ_errs = read_occurrences(root) if target else ([], [])
    if occ_errs:
        return dict(out, decision="extend", reason="unreadable failure occurrence log (fail closed)",
                    errors=occ_errs[:10])
    rt = run_end_times(dirs) if target else {}
    ws, clean = start, 0
    while ws + W <= now:
        we = ws + W
        m = collect_metrics(dirs, ws, we)
        while m["runs"] < min_runs and not m["errors"] and we + W <= now:   # small sample: the window grows
            we += W
            m = collect_metrics(dirs, ws, we)
        if m["errors"]:
            return dict(out, decision="extend", reason="unreadable task files in a probation window (fail closed)",
                        errors=m["errors"][:10])
        if m["runs"] < min_runs:
            break
        regressed = compare(m, base, threshold) if base["runs"] >= min_runs else []
        rec = recurrences(target, ws, we, rt, root, occ)
        w = {"since": m["since"], "until": m["until"], "runs": m["runs"], "rates": m["rates"],
             "regressed": regressed, "recurrence": [p["id"] for p in rec],
             "baseline": "ok" if base["runs"] >= min_runs else f"insufficient ({base['runs']} < {min_runs} runs)"}
        out["windows"].append(w)
        if regressed or rec:
            metric = {"window": [m["since"], m["until"]], "runs": m["runs"], "run_names": m["run_names"][:20],
                      "regressed": regressed, "recurrence": [p["id"] for p in rec], "threshold": threshold,
                      "baseline_runs": base["runs"]}
            metric["text"] = metric_text(metric)
            cx = rec[0]["id"] if rec else (target or {}).get("id")
            return dict(out, decision="revert", reason=metric["text"], metric=metric, counterexample_probe=cx)
        clean += 1
        if clean >= int(pr["windows_to_active"]):
            return dict(out, decision="activate", reason=f"{clean} clean window(s)")
        ws = we
    open_runs = collect_metrics(dirs, ws, now)["runs"] if ws < now else 0
    return dict(out, decision="extend", reason=(f"{clean} clean window(s) of {pr['windows_to_active']}; insufficient "
                                                f"sample in the open window ({open_runs} < {min_runs} lead runs)"))


# ------------------------------------------------------------------ actions
def _append(entry_fields, scope=None, dedupe=None):
    """Append a bookkeeping entry under the ledger lock unless dedupe(ledger) finds it already → (entry, created)."""
    sc = scope or L.generic_scope()
    with L.LedgerLock(sc):
        led = L.read_ledger(sc)
        found = dedupe(led) if dedupe else None
        if found:
            return found, False
        entry = dict({"id": L.next_id(sc), "ts": L.now_iso(), "scope": sc.label()}, **entry_fields)
        L.append_ledger(entry, sc)
    return entry, True


def activate(entry, evaluation=None, root=None):
    """End the probation of an entry: ledger op=probation-end (result active, once) then index status active."""
    lid = str(entry.get("id"))
    ended, created = _append({"op": PROBATION_END, "of": lid, "result": "active",
                              "windows": len((evaluation or {}).get("windows") or [])},
                             dedupe=lambda led: next((e for e in led if e.get("op") == PROBATION_END
                                                      and str(e.get("of")) == lid), None))
    try:
        sync_index(root)
    except (OSError, ValueError) as e:
        return {"status": "activated", "entry": ended, "created": created, "index_error": str(e)[:200]}
    return {"status": "activated", "entry": ended, "created": created}


def _auto_reason(lid, metric, counterexample):
    txt = f"{AUTO_REVERT} {lid}: {metric_text(metric)}"
    if counterexample:
        txt += f" [counterexample {counterexample}]"
    return txt[:500]


def _auto_entry_fields(target, revert_entry, metric, counterexample, root=None):
    near = ""
    try:
        diff = _backup_diff(target) or ""
        added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        near = J.safe_text("\n".join(x for x in added if x.strip()), J.NEAR_TEXT_MAX)
    except (OSError, ValueError):
        near = ""
    return {"op": AUTO_REVERT, "reverted": str(target.get("id")), "revert_entry": str(revert_entry.get("id")),
            "cand_fp": target.get("cand_fp"), "decision_id": target.get("decision_id"),
            "lineage": lineage_of(target.get("relation")), "relation": target.get("relation"),
            "lineage_hash": target.get("relation_hash"),
            "target": (entry_files(target) or [None])[0], "txn": target.get("txn"),
            "metric": metric or {}, "metric_text": metric_text(metric),
            "counterexample_probe": counterexample, "near_text": near,
            "rule_hashes": entry_rule_hashes(target)}


def auto_revert(ledger_id, metric, *, counterexample=None, root=None, apply_bin=None):
    """Revert a probation entry through `fleet-skill-apply --revert <id>` (argv list, NEVER --force), then append the
    op=auto-revert ledger entry. Idempotent / crash-safe: an existing auto-revert entry → "already"; a revert entry
    written by an earlier auto_revert (structural marker auto_revert: true from `--auto-revert-of <id>`) without its
    auto-revert entry → the bookkeeping is finished; any other revert is manual → "manually_reverted" (no entry: it
    does not count for the cooldown).
    → {status: reverted|already|manually_reverted|drift|error, ...}. drift and error write nothing."""
    lid = str(ledger_id)
    sc = L.generic_scope()
    led = L.read_ledger(sc)
    target = next((e for e in led if str(e.get("id")) == lid and e.get("op") == "apply"), None)
    if target is None:
        return {"status": "error", "id": lid, "error": f"no apply entry {lid} in the generic ledger"}
    done = next((e for e in led if e.get("op") == AUTO_REVERT and str(e.get("reverted")) == lid), None)
    if done:
        return {"status": "already", "id": lid, "entry": done}
    rev = next((e for e in led if e.get("op") == "revert" and str(e.get("reverts")) == lid), None)
    if rev is not None and not _is_auto_revert_entry(rev, lid):
        return {"status": "manually_reverted", "id": lid, "revert_entry": rev.get("id")}
    if rev is None:
        base = target.get("base") or sc.base
        drift = [p for f, p in ((f, f.get("path")) for f in target.get("files") or [])
                 if not isinstance(p, str) or L.sha256_file(os.path.join(base, p)) != f.get("sha256_after")]
        if drift:
            return {"status": "drift", "id": lid, "files": drift,
                    "error": "files changed after the probation entry: revert refused (reported, never --force)"}
        cmd = [apply_bin or APPLY, "--revert", lid, "--auto-revert-of", lid, "--scope", "generic", "--reason",
               _auto_reason(lid, metric, counterexample)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"status": "error", "id": lid, "error": f"fleet-skill-apply --revert failed: {e.__class__.__name__}"}
        try:
            res = json.loads(r.stdout)
        except ValueError:
            res = {}
        if r.returncode != 0 or not res.get("ok"):
            err = str(res.get("error") or (r.stderr or "")[-300:])
            kind = "drift" if "changed after" in err else "error"
            return {"status": kind, "id": lid, "error": err[:300], "files": res.get("files")}
        rev = res.get("ledger") or {}
    entry, created = _append(_auto_entry_fields(target, rev, metric, counterexample, root),
                             dedupe=lambda led2: next((e for e in led2 if e.get("op") == AUTO_REVERT
                                                       and str(e.get("reverted")) == lid), None))
    return {"status": "reverted" if created else "already", "id": lid, "entry": entry}


def _is_auto_revert_entry(rev, lid):
    """A revert entry written by auto_revert: the STRUCTURAL marker fleet-skill-apply --auto-revert-of <id> writes
    (auto_revert: true, auto_revert_of: id). The free-text reason never decides it."""
    return rev.get("op") == "revert" and rev.get("auto_revert") is True and str(rev.get("auto_revert_of")) == str(lid) \
        and str(rev.get("reverts")) == str(lid)


def _pending_auto_reverts(led):
    """(target entry, revert entry, counterexample) of auto-reverts interrupted before their op=auto-revert entry."""
    done = {str(e.get("reverted")) for e in led if e.get("op") == AUTO_REVERT}
    by_id = {str(e.get("id")): e for e in led}
    out = []
    for e in led:
        tid = str(e.get("reverts"))
        if e.get("op") != "revert" or tid in done or not _is_auto_revert_entry(e, tid):
            continue
        t = by_id.get(tid)
        if t and t.get("op") == "apply" and t.get("probation") is True:
            m = re.search(r"\[counterexample (fail-[0-9a-f]{16})\]", str(e.get("reason")))
            out.append((t, e, m.group(1) if m else None))
    return out


# ------------------------------------------------------------------ cooldown (judge memory rule 3) and precedents
_HEX_RX = re.compile(r"[0-9a-f]{6,64}")


def _hex(v):
    return isinstance(v, str) and bool(_HEX_RX.fullmatch(v))


def _ancestry(led):
    """[(rule hashes, parent rule ref | None)] of the ledger entries that link rules: op=auto-revert entries
    (rule_hashes + the rule their entry refined/superseded) and generic apply entries with a refines:/supersedes:
    relation (rules they add, from their backed-up diff + relation_hash or the relation's id)."""
    out = []
    for e in led:
        if e.get("op") == AUTO_REVERT:
            hs = [h for h in e.get("rule_hashes") or [] if _hex(h)]
            par = e.get("lineage_hash") if _hex(e.get("lineage_hash")) else e.get("lineage")
        elif e.get("op") == "apply" and lineage_of(e.get("relation")):
            try:
                hs = entry_rule_hashes(e)
            except (OSError, ValueError):
                hs = []
            par = e.get("relation_hash") if _hex(e.get("relation_hash")) else lineage_of(e.get("relation"))
        else:
            continue
        out.append((hs, par if _hex(par) else None))
    return out


def lineage_root(ref, ledger=None, ancestry=None):
    """The root rule of the refines/supersedes chain that rule `ref` (hash or hex prefix) belongs to, following the
    ledger ancestry (an entry whose rule hashes hold ref and that names a parent rule → that parent, recursively; at
    most 32 steps, cycles stop). → the full root hash when known, else the deepest ref; None for no ref."""
    if not _hex(ref):
        return None
    anc = ancestry if ancestry is not None else _ancestry(_ledger(ledger))
    cur, seen = ref, set()
    while len(seen) < 32 and cur not in seen:
        seen.add(cur)
        full, parent = None, None
        for hs, par in anc:
            hit = next((h for h in hs if _same_lineage(h, cur)), None)
            if hit:
                full = full or hit
                if par and not _same_lineage(par, cur):
                    parent = par
                    break
        if not parent:
            return full or cur
        cur = parent
    return cur


def _entry_roots(e, anc):
    """Lineage roots of an op=auto-revert entry: of the rules it reverted (rule_hashes), else of its lineage."""
    refs = [h for h in e.get("rule_hashes") or [] if _hex(h)]
    if not refs:
        refs = [r for r in (e.get("lineage_hash"), e.get("lineage")) if _hex(r)][:1]
    return {r for r in (lineage_root(x, ancestry=anc) for x in refs) if r}


def _matching_auto_reverts(cand_fp, lineage, led):
    """op=auto-revert entries of the same candidate: same cand_fp, or the same lineage ROOT (resolved through the
    reverted rules' hashes + ledger ancestry, so an original rule and its refinements share one history)."""
    anc = _ancestry(led)
    ars = auto_revert_entries(led)
    roots = {lineage_root(lineage, ancestry=anc)} - {None}
    for e in ars:
        if cand_fp and e.get("cand_fp") == cand_fp:
            roots |= _entry_roots(e, anc)
    out = []
    for e in ars:
        if (cand_fp and e.get("cand_fp") == cand_fp) or \
                any(_same_lineage(r, x) for r in _entry_roots(e, anc) for x in roots):
            t = _ts_or_none(e.get("ts"))
            if t is not None:
                out.append((t, e))
    return sorted(out, key=lambda x: (x[0], str(x[1].get("id"))))


def cooldown_state(cand_fp, lineage, now=None, *, root=None, policy=None, ledger=None, probes=None, run_times=None,
                   task_dirs=None):
    """Cooldown of a candidate derived ONLY from the generic ledger's op=auto-revert entries for the same cand_fp or the
    same lineage (refines:/supersedes: rule id). Manual reverts and drift refusals are not auto-revert entries, so they
    never count. 2 auto-reverts within judge_memory.cooldown_window_days → first cooldown (cooldown_days[0]); every
    further auto-revert before the reset is a new case and takes the next duration (capped at the last); after
    cooldown_reset_days without an auto-revert the level resets to 0. Matching: same cand_fp, or the same lineage root
    (lineage_root: through the reverted rules' hashes and the ledger's refines/supersedes ancestry). Early exit:
    `probes` = failure probe ids (a dict contributes only its "id"); each is reloaded from the store (hash-verified,
    kind failure, unknown ids ignored) and lifts the cooldown when its STORED source_run or one of its logged
    occurrences (record_occurrence) is a run that ENDED after the last auto-revert (run_times {run: epoch}, else read
    from task_dirs / all task folders); probes from earlier runs never lift it.
    → {cooldown, until, level, days, cases, reverts, last_revert, early_exit, reason}"""
    pol = policy or L.load_policy(root)
    jm = pol["judge_memory"]
    days = [float(d) for d in jm["cooldown_days"]] or [30.0]
    win, reset = float(jm["cooldown_window_days"]) * 86400, float(jm["cooldown_reset_days"]) * 86400
    now = _epoch(now)
    evs = [(t, e) for t, e in _matching_auto_reverts(cand_fp, lineage, _ledger(ledger)) if t <= now]
    level, until, last, prev, cur_days, cases = 0, None, None, None, None, []
    times = []
    for t, e in evs:
        if prev is not None and t - prev >= reset:
            level = 0
        times.append(t)
        recent = [x for x in times if t - x <= win]
        if level > 0 or len(recent) >= 2:
            level += 1
            cur_days = days[min(level, len(days)) - 1]
            until = t + cur_days * 86400
            cases.append({"revert": str(e.get("id")), "days": cur_days, "until": _iso(until)})
        prev = last = t
    if last is not None and now - last >= reset:
        level = 0
    active = until is not None and now < until
    st = {"cooldown": active, "until": _iso(until) if active else None, "level": level, "days": cur_days if active else None,
          "cases": cases, "reverts": [str(e.get("id")) for _, e in evs], "last_revert": _iso(last), "early_exit": None,
          "reason": (f"cooldown until {_iso(until)} after {len(evs)} auto-revert(s) (case {level}, {cur_days:g} days)"
                     if active else "no cooldown")}
    if active and probes:
        rt = run_times if run_times is not None else run_end_times(task_dirs)
        occ = None
        for ref in probes:
            pid = ref.get("id") if isinstance(ref, dict) else ref   # ids only: every other caller field is ignored
            try:
                p = E.load_probe(pid, root)   # hash-verified store copy
            except (E.ProbeError, OSError, ValueError):
                continue
            if p.get("kind") != "failure":
                continue
            if occ is None:
                occ = read_occurrences(root)[0]
            runs = [p.get("source_run")] + sorted({o["source_run"] for o in occ if o["probe_id"] == p["id"]})
            run = next((r for r in runs if rt.get(r) is not None and rt[r] > last), None)
            if run:
                st.update(cooldown=False, until=None, early_exit=p["id"],
                          reason=f"cooldown lifted early: probe {p['id']} from run {run} after the last auto-revert")
                break
    return st


def precedent_for(candidate, *, root=None, policy=None, ledger=None):
    """Judge precedents (fleet_judge_lib.precedents `extra`) for a generic candidate: every op=auto-revert entry of the
    same target with the same cand_fp, the same lineage root (as cooldown_state), or a near paraphrase (content-token overlap >=
    judge_memory.near_jaccard with the reverted text): "applied and auto-reverted by <metric>" + the counterexample
    probe. Newest first, <= judge_memory.max_precedents. [] for project candidates."""
    if str(candidate.get("scope") or "generic") != "generic":
        return []
    pol = policy or L.load_policy(root)
    jm = pol["judge_memory"]
    text = "\n".join(str(candidate.get(k) or "") for k in ("rule", "new")).strip()
    my_lineage = lineage_of(candidate.get("relation"))
    out = []
    led = _ledger(ledger)
    same = {id(e) for _, e in _matching_auto_reverts(candidate.get("cand_fp"), my_lineage, led)}
    for e in sorted(auto_revert_entries(led), key=lambda e: _ts_or_none(e.get("ts")) or 0, reverse=True):
        if e.get("target") and candidate.get("target") and e.get("target") != candidate.get("target"):
            continue
        hit = id(e) in same or \
            (text and e.get("near_text") and R.overlap(text, e["near_text"]) >= float(jm["near_jaccard"]))
        if not hit:
            continue
        cx = e.get("counterexample_probe")
        reason = f"applied and auto-reverted by {e.get('metric_text') or 'probation metrics'}"
        probes = []
        if cx:
            reason += f"; counterexample probe {cx}"
            try:
                p = E.load_probe(cx, root)
                reason += ": " + J.safe_text(p.get("situation", ""), 200)
                probes = [{"id": p["id"], "hash": p["hash"], "source_run": p.get("source_run")}]
            except (E.ProbeError, OSError, ValueError):
                probes = [{"id": cx}]
        out.append({"decision_id": None, "ts": e.get("ts"), "kind": "auto_revert", "verdict": "reverted",
                    "reason_code": "auto_revert", "reason": J.safe_text(reason, 600),
                    "near_text": J.safe_text(e.get("near_text") or "", J.NEAR_TEXT_MAX), "ctx": None, "probes": probes,
                    "ledger_id": e.get("reverted"), "fallible": True})
    return out[: max(0, int(jm["max_precedents"]))]


# ------------------------------------------------------------------ promotion (2+ projects → generic) with a txn
def distinct_projects(task_dirs_, orq=None):
    """Sorted distinct projects of task folders: two runs (or tasks) of the same project are ONE project."""
    return sorted({p for p in (L.project_of(T, orq) for T in task_dirs_ or []) if p})


def promotion_plan(per_project=None, root=None):
    """Lessons present in 2+ DISTINCT projects (fleet_skill_lib.find_promotions over {project: rules_of(LESSONS.md)};
    the same lesson twice in one project is one project) → [{txn, projects, text, copies: [{project, file, block}]}].
    txn = "promo-" + sha256(normalized text)[:16] (deterministic: re-planning gives the same id)."""
    if per_project is None:
        per_project = {p: L.rules_of(path) for p, path in L.project_lessons_files().items()}
    plans = []
    for projs, raw in L.find_promotions(per_project):
        if len(set(projs)) < 2:
            continue
        n = L.normalize_rule(raw)
        copies = []
        for p in projs:
            for _, r, rn in per_project.get(p, []):
                ratio = L.near_dup_ratio(rn, n)
                if rn == n or (ratio is not None and ratio >= L.NEAR):
                    copies.append({"project": p, "file": f"{p}/{L.LESSONS_FILE}", "block": r})
        plans.append({"txn": "promo-" + hashlib.sha256(n.encode("utf-8")).hexdigest()[:16], "projects": list(projs),
                      "text": raw, "copies": copies})
    return plans


def _txn_rel(txn):
    if not (isinstance(txn, str) and TXN_RX.fullmatch(txn)):
        raise ValueError(f"bad txn id {txn!r}")
    return os.path.join(TXN_DIR, txn + ".json")


def begin_txn(plan, root=None):
    """Write the txn intent skill/ledger-txns/<txn>.json BEFORE the generic apply (atomic; an existing intent is kept
    as is: idempotent). → the txn record."""
    root = root or L.fleet_root()
    rel = _txn_rel(plan.get("txn"))
    cur = load_txn(plan["txn"], root)
    if cur is not None:
        return cur
    for c in plan.get("copies") or []:
        if not (isinstance(c, dict) and L.valid_project(str(c.get("project")))
                and c.get("file") == f"{c.get('project')}/{L.LESSONS_FILE}" and str(c.get("block") or "").strip()):
            raise ValueError("bad promotion copy")
        if L.has_secret(str(c["block"])):
            raise ValueError("promotion copy holds a secret-like string")
    rec = {"txn": plan["txn"], "kind": "promotion", "created": L.now_iso(), "projects": list(plan.get("projects") or []),
           "text": str(plan.get("text") or ""), "copies": [{"project": c["project"], "file": c["file"],
                                                            "block": str(c["block"]).strip()}
                                                           for c in plan.get("copies") or []]}
    L.safe_write(root, rel, json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return rec


def load_txn(txn, root=None):
    text = R._read_nofollow(root or L.fleet_root(), _txn_rel(txn))
    if text is None:
        return None
    rec = json.loads(text)
    return rec if isinstance(rec, dict) and rec.get("txn") == txn else None


def list_txns(root=None):
    d = os.path.join(root or L.fleet_root(), TXN_DIR)
    try:
        names = sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json") and TXN_RX.fullmatch(n[:-5]))
    except OSError:
        return []
    out = []
    for n in names:
        try:
            rec = load_txn(n, root)
        except (OSError, ValueError):
            rec = None
        if rec:
            out.append(rec)
    return out


def _copy_sha(block):
    """Identity of one planned copy inside its file: sha256 of its stripped block text."""
    return hashlib.sha256(str(block or "").strip().encode("utf-8")).hexdigest()


SHA_RX = re.compile(r"[0-9a-f]{64}")


def _archived_block(sc, entry):
    """Stripped text of the block an op=archive entry archived, read from its archive file (or the restored/ copy
    after a revert) and verified against the entry's block_sha256; None when it cannot be verified."""
    want = entry.get("block_sha256")
    norm = L._norm(entry.get("archive_file") or "")
    adir = sc.rel(L.archive_dir(sc))
    if not (isinstance(want, str) and SHA_RX.fullmatch(want) and norm and os.path.dirname(norm) == adir
            and norm.endswith(".md")):
        return None
    for rel in (norm, os.path.join(adir, "restored", os.path.basename(norm))):
        text = R._read_nofollow(sc.anchor, rel)
        if text is None:
            continue
        try:
            _, block = L.archive_unpack(text.encode("utf-8"))
        except ValueError:
            continue
        if hashlib.sha256(block).hexdigest() == want:
            return block.decode("utf-8", errors="replace").strip()
    return None


def _archive_copy(rec, copy, gid, apply_bin=None, used=None):
    """Archive one project copy of a promoted rule (idempotent) → {project, file, copy_sha256, status, ...}.
    A copy is identified by (txn, file, block): it is already archived only when an archive entry of this txn for
    this file holds a block (verified against its block_sha256) equal to this copy's block. `used` (a set of
    (project, archive entry id)) makes one archive entry count for one copy only."""
    txn, proj, rel = rec["txn"], copy.get("project"), copy.get("file")
    csha = _copy_sha(copy.get("block"))
    res = {"project": proj, "file": rel, "copy_sha256": csha}
    if not (L.valid_project(str(proj)) and rel == f"{proj}/{L.LESSONS_FILE}"):
        return dict(res, status="error", error="bad copy record")
    used = set() if used is None else used
    sc = L.project_scope(proj)
    for e in L.read_ledger(sc):
        eid = (proj, str(e.get("id")))   # ledger ids are per project
        if (e.get("op") == "archive" and e.get("txn") == txn and rel in entry_files(e) and eid not in used
                and (blk := _archived_block(sc, e)) is not None and _copy_sha(blk) == csha):
            used.add(eid)
            return dict(res, status="archived", already=True, ledger_id=eid[1], block_sha256=e.get("block_sha256"))
    text = R._read_nofollow(L.orq_root(), rel)
    if text is None:
        return dict(res, status="absent")
    lines = text.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if ln.strip() == str(copy.get("block") or "").strip()]
    if not hits:
        return dict(res, status="absent")
    if len(hits) > 1:
        return dict(res, status="error", error="the copy occurs more than once: archive it by hand")
    new = "".join(lines[: hits[0]] + lines[hits[0] + 1:])
    tmp = tempfile.mkdtemp(prefix="fleet-txn-")
    try:
        dpath = os.path.join(tmp, "archive.diff")
        with open(dpath, "w", encoding="utf-8") as f:
            f.write(L.unified_diff(rel, text, new))
        cmd = [apply_bin or APPLY, dpath, "--run", f"txn-{txn}", "--reason",
               f"promoted to generic (txn {txn}, generic ledger entry {gid})", "--op", "archive", "--archive-kind",
               "supersession", "--evidence", f"promoted to the generic rule of ledger entry {gid}, active after "
               f"probation (txn {txn})", "--txn", txn, "--scope", f"project:{proj}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return dict(res, status="error", error=e.__class__.__name__)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            out = {}
        if r.returncode != 0 or not out.get("ok"):
            return dict(res, status="error", error=str(out.get("error") or r.stderr[-300:])[:300])
        led = out.get("ledger") or {}
        if led.get("id") is not None:
            used.add((proj, str(led.get("id"))))
        return dict(res, status="archived", ledger_id=led.get("id"), block_sha256=led.get("block_sha256"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def txn_state(rec, ledger=None):
    """(state, generic apply entry | None): pending (no generic apply with this txn yet), probation, aborted (the
    generic entry was reverted: project copies are kept), active."""
    led = _ledger(ledger)
    apps = [e for e in led if e.get("op") == "apply" and e.get("txn") == rec.get("txn")]
    if not apps:
        return "pending", None
    g = apps[-1]
    gid = str(g.get("id"))
    if gid in _reverted_ids(led):
        return "aborted", g
    if g.get("probation") is True and gid not in _ended_ids(led):
        return "probation", g
    return "active", g


def recover_txns(root=None, apply_bin=None):
    """Drive every promotion txn forward, idempotently (safe after a crash at any step): the project copies are
    archived (fleet-skill-apply --op archive --txn) only when the generic entry is active; an already archived copy
    (project ledger archive entry with the same txn + file whose verified block is this copy's block) is skipped; a
    copy no longer in its file is "absent"; the txn is "done" only when every planned copy is archived or absent.
    → [{txn, state, generic_id, copies: [...]}]"""
    out = []
    led = _ledger()
    for rec in list_txns(root):
        state, g = txn_state(rec, led)
        item = {"txn": rec["txn"], "state": state, "generic_id": g and str(g.get("id")), "copies": []}
        if state == "active":
            used = set()   # per txn: one archive entry stands for one copy
            item["copies"] = [_archive_copy(rec, c, item["generic_id"], apply_bin, used) for c in rec.get("copies") or []]
            if all(c["status"] in ("archived", "absent") for c in item["copies"]):
                item["state"] = "done"
        out.append(item)
    return out


# ------------------------------------------------------------------ one probation pass (called by the cycle runner)
def run(now=None, *, root=None, policy=None, task_dirs_=None, apply_bin=None):
    """One probation pass: finish interrupted auto-reverts, repair index statuses, evaluate every entry in probation
    (revert / activate / extend), then drive the promotion txns. → report dict (lists of ids and results)."""
    pol = policy or L.load_policy(root)
    rep = {"reverted": [], "drift": [], "activated": [], "extended": [], "errors": [], "recovered": [], "txns": []}
    for t, rev, cx in _pending_auto_reverts(L.read_ledger(L.generic_scope())):
        why = str(rev.get("reason") or "").split(": ", 1)[-1]
        m = {"recovered": True, "text": re.sub(r"\s*\[counterexample [^\]]*\]$", "", why)[:300]}
        r = auto_revert(t["id"], m, counterexample=cx, root=root, apply_bin=apply_bin)
        rep["recovered"].append(r)
    try:
        sync_index(root)
    except (OSError, ValueError) as e:
        rep["errors"].append({"index": str(e)[:200]})
    for e in probation_entries():
        ev = evaluate_probation(e, now, task_dirs_=task_dirs_, root=root, policy=pol)
        if ev["decision"] == "revert":
            r = auto_revert(e["id"], ev["metric"], counterexample=ev.get("counterexample_probe"), root=root,
                            apply_bin=apply_bin)
            key = {"reverted": "reverted", "already": "reverted", "drift": "drift"}.get(r["status"], "errors")
            rep[key].append(dict(r, evaluation=ev))
        elif ev["decision"] == "activate":
            rep["activated"].append(dict(activate(e, ev, root), evaluation=ev))
        else:
            rep["extended"].append(ev)
    rep["txns"] = recover_txns(root, apply_bin)
    return rep
