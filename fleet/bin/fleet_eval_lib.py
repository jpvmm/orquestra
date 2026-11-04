"""Decision-probe store for the retro loop (PLAN F3 / F3.1 rule 1). Standard library only.

A probe is DATA: a sanitized situation, the expected and the forbidden behaviour, the run it came from and a canonical
hash. Nothing in a probe is ever executed or interpreted as a command. Probes live in <fleet root>/skill/evals/<id>.json
(bookkeeping, listed in fleet_skill_lib.RESERVED), are written atomically under a flock, and are FROZEN: a failure probe
is never edited, a correction is a new probe with `corrected_from`.

Public API (all take `root=None` -> fleet_root(), and an injectable `now`: epoch float, datetime or ISO string):
  sanitize(text, max_chars)                      -> str, or raises ProbeRefused when a secret survives redaction
  canonical_json(obj) / probe_hash(kind, scope, situation, expected, forbidden)
  record_failure(situation, expected, forbidden, source_run, scope, area, tags, corrected_from, root, now)
  record_success(task_dir, scope, area, tags, run, root, now)
  success_from_task_dir(T, run=None)             -> dict, or raises NotSuccess (status not done / criteria not all passing)
  load_probe(id, root) / list_probes(root, kind, scope) / update_probe(...) (always refused: probes are frozen)
  select(candidate_scope, area, now, root, tags, failure_id) -> {"failure": probe|None, "successes": [probe, ...]}
  is_new_evidence(probe, cited_probes, batch_runs) -> "new" | "probe_corrected" | "" (falsy = not new evidence)
"""

import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time

BIN = os.path.dirname(os.path.realpath(__file__))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
import fleet_skill_lib as L  # noqa: E402

_ID_RX = re.compile(r"(?:fail|ok)-[0-9a-f]{16}")
_ANSI_RX = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CTRL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKEN_RX = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset({"the", "and", "for", "with", "that", "this", "from", "run", "task", "fleet"})
_FALLBACK_RX = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[a-z]-[A-Za-z0-9-]{8,}|Bearer\s+\S{8,}|AKIA[0-9A-Z]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)")
_SECRETISH = re.compile(
    r"(?i:\b(?:api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S{6,})|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    # base64 with '/' (AWS secret keys): needs a digit and an uppercase letter so plain long paths are not refused
    r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}"
)
# long base64-ish runs; a pure lowercase hex run of exactly 40 or 64 chars (git SHA-1 / SHA-256) is not a secret
_LONGRUN = re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}")
_HEX_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
CAPS = {"progress": 1200, "summary": 600}
JSON_CAP = 1_000_000   # bytes read from status/criteria JSON written by agents
_RUN_RX = re.compile(r"[A-Za-z0-9._-]+")


class ProbeError(Exception):
    """Base class: the CLI turns these into {"error": ...} with exit 2."""


class ProbeRefused(ProbeError):
    """Probe content refused (secret-like text left after redaction, empty fields, bad scope)."""


class ProbeFrozen(ProbeError):
    """Probes are never edited; record a correction (corrected_from) instead."""


class NotSuccess(ProbeError):
    """Task folder is not a successful run (state not done or a criterion not passing)."""


# ------------------------------------------------------------------ redaction (same as fleet-retro)
def _load_redact():
    """vigia_comms.redact when importable (VIGIA_BIN like fleet-retro), always followed by the local token regex: vigia's
    heuristics skip low-entropy keys such as sk-test..., and a probe must never keep one."""
    vbin = os.path.expanduser(os.environ.get("VIGIA_BIN") or os.path.join(os.path.dirname(L.fleet_root()), "bin"))
    base = None
    try:
        if vbin not in sys.path:
            sys.path.insert(0, vbin)
        from vigia_comms import redact as base
    except Exception:   # vigia not installed: only the local regex (same as fleet-retro's fallback)
        base = None

    def red(t):
        if not isinstance(t, str):
            return t
        if base:
            t = base(t)
        return _FALLBACK_RX.sub("***", t)

    return red


redact = _load_redact()


def has_secret(text):
    """True when redaction would change the text or a secret-looking assignment/blob remains."""
    if not text:
        return False
    if redact(text) != text or _SECRETISH.search(text) or _FALLBACK_RX.search(text):
        return True
    return any(not _HEX_ID.fullmatch(m.group(0)) for m in _LONGRUN.finditer(text))


def normalize(text):
    """Text with ANSI sequences and control characters removed (newlines/tabs kept). Always runs BEFORE redaction, so a
    token split by an escape sequence is whole again when the redactor sees it."""
    t = _ANSI_RX.sub("", str(text if text is not None else "")).replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RX.sub("", t)


def sanitize(text, max_chars=None):
    """Probe-safe text: ANSI and control chars stripped, secrets redacted, capped. Command-looking lines are kept as
    inert text. Raises ProbeRefused when something secret-like is still there after redaction."""
    if max_chars is None:
        max_chars = int(L.load_policy()["eval"]["probe_max_chars"])
    t = normalize(text)
    t = "\n".join(ln.rstrip() for ln in t.split("\n")).strip()
    t = redact(t)
    if has_secret(t):
        raise ProbeRefused("secret-like content remains after redaction; probe refused")
    if len(t) > max_chars:
        mark = "\n[... cut ...]"
        max_chars = max(0, int(max_chars))
        if max_chars <= len(mark):   # no room for the marker: plain cut, never above the cap
            return t[:max_chars]
        t = t[: max_chars - len(mark)].rstrip() + mark
    return t


# ------------------------------------------------------------------ hash, ids, time
def canonical_json(obj):
    """Deterministic JSON: sorted keys, no whitespace, utf-8 characters kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def probe_hash(kind, scope, situation, expected, forbidden):
    body = {"kind": kind, "scope": scope, "situation": situation, "expected": expected, "forbidden": forbidden}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def probe_id(kind, h):
    return ("fail-" if kind == "failure" else "ok-") + h[:16]


def _epoch(now):
    if now is None:
        return time.time()
    if isinstance(now, datetime.datetime):
        return (now if now.tzinfo else now.replace(tzinfo=datetime.timezone.utc)).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    s = str(now).strip().replace("Z", "+00:00")
    d = datetime.datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ store
def evals_dir(root=None):
    return os.path.join(L.skill_dir(root), "evals")


def _check_scope(scope):
    scope = scope or "generic"
    if scope != "generic" and not (scope.startswith("project:") and L.valid_project(scope[8:])):
        raise ProbeRefused(f"scope must be generic or project:<name>, got {scope!r}")
    return scope


def _tags(*parts):
    out = []
    for p in parts:
        for x in (p if isinstance(p, (list, tuple)) else [p]):
            x = str(x or "").strip().lower()
            if x and x not in out:
                out.append(x)
    return out


def _field(value, max_chars):
    """Sanitized string for expected/forbidden (a list is joined line by line)."""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(v) for v in value)
    return sanitize(value, max_chars)


def _atomic_write(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".probe-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class _Lock:
    def __init__(self, root):
        os.makedirs(evals_dir(root), exist_ok=True)
        self.path = os.path.join(evals_dir(root), ".lock")

    def __enter__(self):
        self.f = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644), "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _read_file(path):
    raw = _read_bytes(path, JSON_CAP + 1)   # no-follow, regular files only, bounded
    if raw is None or len(raw) > JSON_CAP:
        raise ProbeError(f"{os.path.basename(path)}: missing or too large")
    try:
        p = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise ProbeError(f"{os.path.basename(path)}: invalid JSON") from e
    if not isinstance(p, dict):
        raise ProbeError(f"{path}: not a probe object")
    want = probe_hash(p.get("kind"), p.get("scope"), p.get("situation"), p.get("expected"), p.get("forbidden"))
    if p.get("hash") != want or p.get("id") != probe_id(p.get("kind"), want):
        raise ProbeError(f"{os.path.basename(path)}: hash/id mismatch (probe was edited or is corrupt)")
    return p


def load_probe(pid, root=None):
    """The probe with this id; ProbeError if missing, malformed or its hash does not match its content."""
    if not isinstance(pid, str) or not _ID_RX.fullmatch(pid):
        raise ProbeError(f"bad probe id: {pid!r}")
    path = os.path.join(evals_dir(root), pid + ".json")
    if not os.path.isfile(path):
        raise ProbeError(f"no such probe: {pid}")
    return _read_file(path)


def list_probes(root=None, kind=None, scope=None):
    """All valid probes (corrupt files are skipped), ordered by (created_at, id)."""
    out = []
    for path in sorted(glob.glob(os.path.join(evals_dir(root), "*.json"))):
        try:
            p = _read_file(path)
        except (OSError, ValueError, ProbeError):
            continue
        if (kind and p.get("kind") != kind) or (scope and p.get("scope") != scope):
            continue
        out.append(p)
    return sorted(out, key=lambda p: (p.get("created_at", ""), p["id"]))


def _store(kind, scope, area, tags, situation, expected, forbidden, source_run, corrected_from, root, now, policy):
    pol = policy or L.load_policy(root)
    cap = int(pol["eval"]["probe_max_chars"])
    scope = _check_scope(scope)
    situation = sanitize(situation, cap)
    expected = _field(expected, cap)
    forbidden = _field(forbidden, cap)
    source_run = sanitize(source_run, 200).replace("\n", " ")
    if not situation or not expected or not source_run:
        raise ProbeRefused("situation, expected and source_run are required")
    h = probe_hash(kind, scope, situation, expected, forbidden)
    pid = probe_id(kind, h)
    probe = {
        "id": pid, "kind": kind, "scope": scope, "area": sanitize(area or "", 120).replace("\n", " "),
        "tags": [sanitize(t, 60).replace("\n", " ") for t in _tags(tags)],
        "situation": situation, "expected": expected, "forbidden": forbidden,
        "source_run": source_run, "created_at": _iso(_epoch(now)), "hash": h,
    }
    if scope != "generic":
        probe["project"] = scope[8:]
    with _Lock(root):
        if corrected_from:
            orig = load_probe(corrected_from, root)
            if orig["kind"] != kind:
                raise ProbeRefused("a correction must have the same kind as the probe it corrects")
            if orig["id"] != pid:
                probe["corrected_from"] = orig["id"]
        path = os.path.join(evals_dir(root), pid + ".json")
        if os.path.exists(path):   # same content = same id: idempotent no-op, the stored (frozen) probe wins
            return {"status": "exists", "created": False, "probe_corrected": False, "probe": _read_file(path)}
        _atomic_write(path, json.dumps(probe, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    corrected = "corrected_from" in probe
    return {"status": "corrected" if corrected else "created", "created": True, "probe_corrected": corrected, "probe": probe}


def record_failure(situation, expected, forbidden, source_run, scope="generic", area="", tags=None,
                   corrected_from=None, root=None, now=None, policy=None):
    """Freeze a failure probe (call it when the failure is recorded, before any rule). Returns
    {status: created|exists|corrected, created, probe_corrected, probe}. Re-recording identical content is a no-op;
    different content for a known failure must pass corrected_from=<old id> and gets a NEW id (flag probe_corrected)."""
    return _store("failure", scope, area, tags, situation, expected, forbidden, source_run, corrected_from, root, now, policy)


def update_probe(pid, **fields):
    """Probes are frozen. Always refused; record a correction with record_failure(..., corrected_from=pid)."""
    raise ProbeFrozen(f"probe {pid} is frozen: record a correction with corrected_from instead of editing")


# ------------------------------------------------------------------ success bank
def _read_bytes(path, limit):
    """At most `limit` bytes of a REGULAR, non-symlink file (task folders are agent-written). Missing file -> None;
    symlink, FIFO, device or any other non-regular file -> NotSuccess. Never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise NotSuccess(f"{os.path.basename(path)}: refused (symlink or unreadable)") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotSuccess(f"{os.path.basename(path)}: not a regular file")
        chunks, n = [], 0
        while n < limit:
            c = os.read(fd, min(65536, limit - n))
            if not c:
                break
            chunks.append(c)
            n += len(c)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_json(path):
    """Parsed JSON from a regular file of at most JSON_CAP bytes, else NotSuccess."""
    raw = _read_bytes(path, JSON_CAP + 1)
    if raw is None or len(raw) > JSON_CAP:
        raise NotSuccess(f"{os.path.basename(path)} missing or too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise NotSuccess(f"{os.path.basename(path)} invalid") from e


def _read(path, cap):
    """Capped text of an agent-written file. Reads at most cap*4 bytes, redacts and secret-checks the whole bounded
    text FIRST and only then cuts it (a token straddling the cut must not leak its suffix)."""
    raw = _read_bytes(path, cap * 4)
    if raw is None:
        return ""
    txt = redact(normalize(raw.decode("utf-8", errors="replace")).strip())
    if has_secret(txt):
        raise ProbeRefused(f"{os.path.basename(path)}: secret-like content remains after redaction")
    if len(txt) > cap:
        txt = txt[: cap // 3] + "\n[... cut ...]\n" + txt[-(2 * cap) // 3:]
    return txt


def _pick_run(T, run=None):
    runs = os.path.join(T, "runs")
    if os.path.islink(runs):
        raise ProbeRefused("runs/ is a symlink; refused")
    if run is not None:
        if not (isinstance(run, str) and _RUN_RX.fullmatch(run)) or ".." in run:
            raise ProbeRefused(f"bad run name: {run!r}")
        path = os.path.join(runs, run + ".status.json")
        if os.path.dirname(os.path.realpath(path)) != os.path.realpath(runs):
            raise ProbeRefused("run path escapes runs/")
        return run if os.path.isfile(path) and not os.path.islink(path) else None
    st = [p for p in glob.glob(os.path.join(runs, "fleet-*.status.json")) if not os.path.islink(p)]
    st.sort(key=lambda p: (os.path.getmtime(p), p))
    pick = [p for p in st if "-lead-" in os.path.basename(p)] or st
    return os.path.basename(pick[-1])[: -len(".status.json")] if pick else None


def success_from_task_dir(T, run=None):
    """Material for a success probe from a task folder: {situation, expected, forbidden, source_run, area}.
    Raises NotSuccess unless the (lead) run state is `done` and criteria.json has criteria that ALL have passes: true.
    progress.md / FINAL-SUMMARY.md are read capped; sanitizing happens when the probe is stored."""
    name = _pick_run(T, run)
    if not name:
        raise NotSuccess("no fleet run status in runs/")
    st = _read_json(os.path.join(T, "runs", name + ".status.json"))
    state = st.get("state") if isinstance(st, dict) else None
    if state != "done":
        raise NotSuccess(f"run {name} state is {state!r}, not done")
    crit = _read_json(os.path.join(T, "criteria.json"))
    items = crit.get("criteria") if isinstance(crit, dict) else crit
    if not isinstance(items, list) or not items or not all(isinstance(c, dict) and c.get("passes") is True for c in items):
        raise NotSuccess("not every criterion has passes: true")
    summary = _read(os.path.join(T, "FINAL-SUMMARY.md"), CAPS["summary"])
    progress = _read(os.path.join(T, "progress.md"), CAPS["progress"])
    if not (summary or progress):
        raise NotSuccess("no progress.md or FINAL-SUMMARY.md to build the situation from")
    situation = "\n".join(x for x in (f"FINAL-SUMMARY:\n{summary}" if summary else "", f"progress:\n{progress}" if progress else "") if x)
    return {
        "situation": situation,
        "expected": "Keep the approach that produced this passing run; do not regress the decisions it records.",
        "forbidden": "",
        "source_run": name,
        "area": os.path.basename(os.path.realpath(T)),
    }


def record_success(task_dir, scope="generic", area=None, tags=None, run=None, root=None, now=None, policy=None):
    """Bank a success probe from a done run whose criteria all pass (NotSuccess otherwise). Same result shape as
    record_failure; idempotent."""
    m = success_from_task_dir(task_dir, run)
    return _store("success", scope, area or m["area"], tags, m["situation"], m["expected"], m["forbidden"],
                  m["source_run"], None, root, now, policy)


# ------------------------------------------------------------------ selection and new evidence
def _toks(*parts):
    out = set()
    for p in parts:
        for x in (p if isinstance(p, (list, tuple)) else [p]):
            out |= set(_TOKEN_RX.findall(str(x or "").lower()))
    return out - _STOP


def _ptoks(p):
    return _toks(p.get("area"), p.get("tags") or [], p.get("project"))


def select(candidate_scope, area, now=None, root=None, tags=None, failure_id=None, policy=None):
    """Probes for one candidate: {"failure": frozen failure probe or None, "successes": [<= max_success_probes]}.
    The failure probe is `failure_id` when given, else the latest uncorrected failure probe of the same scope sharing area
    tokens (a correction replaces the probe it corrects). Successes: kind success, created within
    eval.success_window_days of `now`, scope compatible, sharing area/tag tokens; order is (overlap desc, newest, id)."""
    pol = policy or L.load_policy(root)
    ev = pol["eval"]
    scope = _check_scope(candidate_scope)
    want = _toks(area, tags or [])
    probes = list_probes(root)
    failure = None
    if failure_id:
        failure = load_probe(failure_id, root)
        if failure.get("kind") != "failure":
            raise ProbeRefused(f"{failure_id} is not a failure probe")
        if failure.get("scope") != scope:   # same scope only: no cross-project (or project<->generic) borrowing
            raise ProbeRefused(f"{failure_id} has scope {failure.get('scope')!r}, not the candidate scope {scope!r}")
    else:
        corrected = {p["corrected_from"] for p in probes if p.get("corrected_from")}
        cands = [p for p in probes if p["kind"] == "failure" and p["scope"] == scope and p["id"] not in corrected
                 and want & _ptoks(p)]
        cands.sort(key=lambda p: (-len(want & _ptoks(p)), p["created_at"], p["id"]))   # best overlap, then newest
        if cands:
            top = len(want & _ptoks(cands[0]))
            failure = sorted((p for p in cands if len(want & _ptoks(p)) == top), key=lambda p: (p["created_at"], p["id"]))[-1]
    cutoff = _epoch(now) - float(ev["success_window_days"]) * 86400
    pool = []
    for p in probes:
        if p["kind"] != "success" or p["id"] == (failure or {}).get("id"):
            continue
        if not (scope == "generic" or p["scope"] in (scope, "generic")):
            continue
        ts = _epoch(p["created_at"])
        ov = len(want & _ptoks(p))
        if ts >= cutoff and ov > 0:
            pool.append((-ov, -ts, p["id"], p))
    pool.sort(key=lambda x: x[:3])
    return {"failure": failure, "successes": [x[3] for x in pool[: max(0, int(ev["max_success_probes"]))]]}


def is_new_evidence(probe, cited_probes, batch_runs):
    """F3.1 rule 1. `cited_probes`: probes (dicts with id/hash/source_run) cited by the earlier reject; `batch_runs`:
    source runs of the current batch. Returns "new" when the hash was never cited, the source_run differs from every
    cited one and is in the batch; "probe_corrected" when it is a correction (corrected_from a cited probe) with a new
    hash from a batch run, even from the same run as the cited probe; "" otherwise (falsy)."""
    cited = [c for c in (cited_probes or []) if isinstance(c, dict)]
    if not probe or probe.get("source_run") not in set(batch_runs or ()):
        return ""
    if probe.get("hash") in {c.get("hash") for c in cited}:
        return ""
    if probe.get("source_run") not in {c.get("source_run") for c in cited}:
        return "new"
    if probe.get("corrected_from") in {c.get("id") for c in cited}:
        return "probe_corrected"
    return ""
