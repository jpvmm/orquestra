"""Shared helpers for the fleet retro loops (fleet-retro, fleet-skill-apply, fleet-skill-lint). Python 3 stdlib only.

Two loops, two kinds of target:
- generic: fleet skill files (paths relative to the fleet root: skill/..., .claude/agents/...) and the Orquestra
  lessons file (learnings.md by default, FLEET_LESSONS_NAME to rename; relative to the Orquestra root). Ledger: FLEET_ROOT/skill/ledger.jsonl.
- project: ORQ_ROOT/<project>/LESSONS.md (diff path "<project>/LESSONS.md", relative to the Orquestra root).
  Ledger: ORQ_ROOT/<project>/lessons-ledger.jsonl, backups ORQ_ROOT/<project>/lessons-backups/.
FLEET_ROOT env (default: the folder above bin/); ORQ_ROOT env (default: ORQUESTRA_HOME, else the folder above the
fleet root). Vigia active-run registry: VIGIA_ATIVOS env, else VIGIA_BASE/active, else ORQ_ROOT/vigia/active."""
import copy, difflib, fcntl, hashlib, json, os, re, stat, subprocess, tempfile, time

BIN = os.path.dirname(os.path.realpath(__file__))
EDITABLE = ("skill/", ".claude/agents/")
# bookkeeping under skill/: never a patch target (history, lock, backups, proposals, archive, lint output)
RESERVED = ("skill/ledger", "skill/ledger-backups/", "skill/proposals/", "skill/archive/", "skill/lint-report.md",
            "skill/retro-state.json", "skill/retro-batches/", "skill/rules-index.json", "skill/judge-log.jsonl",
            "skill/evals/", "skill/retro-policy.json", "skill/usage", "skill/.install-baseline.json")
NOT_PROJECTS = {"fleet", "bin", "vigia", "skill", "tmp"}   # default of policy ignore_dirs (see valid_project)
LESSONS_FILE = "LESSONS.md"
LESSONS_CAP = 4000
_PROJECT_RX = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}")
_LESSONS_RX = re.compile(r"[A-Za-z0-9._-]+\.md")
HARD_IGNORE = frozenset({"fleet", "vigia", "skill"})   # never projects, whatever the policy says


def ignore_set(policy):
    return set(policy["ignore_dirs"]) | HARD_IGNORE


# ------------------------------------------------------------------ policy (retro-policy.json)
DEFAULTS = {
    "owner_name": "the owner",
    "lessons_file": "learnings.md",
    "ignore_dirs": ["fleet", "bin", "vigia", "skill", "tmp"],
    "quota": {"project_additions_per_project": 3, "generic_additions": 3, "shrink_ops_per_scope": 3,
              "read_cap_multiplier": 3},
    "budgets": {"lessons_chars": 4000, "learnings_chars": 4000, "skill_md_chars": 12000,
                "references_total_chars": 40000},
    "budget": {"tokens_per_cycle": 25000, "output_allowance_tokens": 4000, "outstanding_max_age_hours": 168},
    "cycle": {"due_hours": 48, "due_slack_hours": 6, "late_hours": 72, "late_notice": True, "max_batch_dirs": 10,
              "first_batch_hours": 48},
    "judge_memory": {"block_days": 14, "cooldown_days": [30, 60, 90], "cooldown_window_days": 60,
                     "cooldown_reset_days": 90, "log_days": 90, "log_lines": 500, "max_precedents": 3,
                     "max_rejections_in_retro_prompt": 5, "near_suppress": False, "near_jaccard": 0.5,
                     "near_ratio": 0.88},
    "eval": {"project": "lite", "max_success_probes": 2, "success_window_days": 7, "probe_max_chars": 1500},
    "index": {"related_max": 8, "new_overlap_refuse": 0.6},
    "merge_guard": {"min_ratio": 0.8},
    "probation": {"min_runs": 3, "windows_to_active": 2, "max_per_always_loaded_file": 1,
                  "regression_threshold": 0.25},
    "pending_expire_batches": 3,
    "always_loaded": ["skill/fleet-sdd/SKILL.md", ".claude/agents/team-lead.md"],
}


def _type_ok(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):   # integer settings: no floats (they crash slicing/ranges), no bools
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _merge(base, over, path, warns, defaults):
    """Deep-merge `over` into `base` in place: unknown keys are kept; a wrong type for a known key restores the IN-CODE
    default of that key (not the previous layer's value) and warns."""
    for k, v in over.items():
        here = f"{path}{k}"
        if k not in defaults:
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v, here + ".", warns, {})   # unknown objects merge recursively across layers
            else:
                base[k] = copy.deepcopy(v)
        elif isinstance(defaults[k], dict):
            if isinstance(v, dict):
                if not isinstance(base.get(k), dict):
                    base[k] = copy.deepcopy(defaults[k])
                _merge(base[k], v, here + ".", warns, defaults[k])
            else:
                base[k] = copy.deepcopy(defaults[k])
                warns.append(f"{here}: expected an object, using the default")
        elif _type_ok(defaults[k], v):
            base[k] = copy.deepcopy(v)
        else:
            base[k] = copy.deepcopy(defaults[k])
            warns.append(f"{here}: expected {type(defaults[k]).__name__}, got {type(v).__name__}; using the default")


def _policy_files(root=None):
    files = [os.path.join(skill_dir(root), "retro-policy.json")]
    if os.environ.get("FLEET_RETRO_POLICY"):
        files.append(os.path.expanduser(os.environ["FLEET_RETRO_POLICY"]))
    return files


def load_policy(root=None):
    """Effective policy: DEFAULTS <- <fleet root>/skill/retro-policy.json (optional) <- $FLEET_RETRO_POLICY (optional).
    A fresh deep copy every call. Problems never raise: they are listed in policy["_warnings"]."""
    pol, warns = copy.deepcopy(DEFAULTS), []
    for i, path in enumerate(_policy_files(root)):
        if not os.path.isfile(path):
            if i == 1:
                warns.append(f"{path}: policy file not found")
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            warns.append(f"{path}: unreadable policy ({e.__class__.__name__}); ignored")
            continue
        if isinstance(data, dict):
            _merge(pol, data, "", warns, DEFAULTS)
        else:
            warns.append(f"{path}: policy must be a JSON object; ignored")
    if not _LESSONS_RX.fullmatch(str(pol["lessons_file"])):
        warns.append(f"lessons_file: {str(pol['lessons_file'])[:60]!r} is not a plain *.md file name; using the default")
        pol["lessons_file"] = DEFAULTS["lessons_file"]
    pol["_warnings"] = warns
    return pol


def policy_version(policy):
    """Short sha256 of the canonical JSON of the effective policy (without _warnings)."""
    body = {k: v for k, v in policy.items() if k != "_warnings"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:12]


_POLICY_CACHE = {}


def _cached_policy():
    """load_policy() for hot paths (valid_project): re-read only when a policy file changes."""
    key = []
    for p in _policy_files():
        try:
            st = os.stat(p)
            key.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            key.append((p, None, None))
    key = tuple(key)
    if key not in _POLICY_CACHE:
        _POLICY_CACHE.clear()
        _POLICY_CACHE[key] = load_policy()
    return _POLICY_CACHE[key]


def lessons_name():
    """Generic lessons file name: FLEET_LESSONS_NAME env, else policy lessons_file."""
    return os.environ.get("FLEET_LESSONS_NAME") or _cached_policy()["lessons_file"]


def owner_name(policy=None):
    return str((policy or _cached_policy())["owner_name"])


def fleet_root():
    return os.path.abspath(os.environ.get("FLEET_ROOT") or os.path.dirname(BIN))


def orq_root():
    return os.path.abspath(os.path.expanduser(os.environ.get("ORQ_ROOT") or os.environ.get("ORQUESTRA_HOME")
                                              or os.path.dirname(fleet_root())))


def vigia_active_dir():
    """Vigia registry of active runs (one JSON per run)."""
    if os.environ.get("VIGIA_ATIVOS"):
        return os.environ["VIGIA_ATIVOS"]
    base = os.environ.get("VIGIA_BASE") or os.path.join(orq_root(), "vigia")
    return os.path.join(os.path.expanduser(base), "active")


def lessons_default():
    return os.environ.get("FLEET_LESSONS") or os.path.join(orq_root(), lessons_name())


def skill_dir(root=None):
    return os.path.join(root or fleet_root(), "skill")


APRENDIZADOS = lessons_name()   # import-time value kept for callers; functions here use lessons_name()


def sha256_file(path):
    if not os.path.lexists(path) or os.path.islink(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ------------------------------------------------------------------ paths and scopes
def _norm(path):
    if not path or os.path.isabs(path) or "\\" in path or "\0" in path:
        return None
    norm = os.path.normpath(path)
    if norm.startswith("..") or norm == "." or norm.startswith("/"):
        return None
    return norm


def safe_rel(path):
    """Fleet-relative skill path; None if it escapes, is not editable, or is bookkeeping."""
    norm = _norm(path)
    if not norm or not any(norm.startswith(p) for p in EDITABLE):
        return None
    if any(norm == r.rstrip("/") or norm.startswith(r) for r in RESERVED):
        return None
    return norm


def valid_project(name):
    return bool(name) and bool(_PROJECT_RX.fullmatch(name)) and name not in ignore_set(_cached_policy()) and not name.startswith(".")


def project_of(task_dir, orq=None):
    """<project> for a task dir ORQ_ROOT/<project>/<task>[/...]; None otherwise."""
    orq = os.path.realpath(orq or orq_root())
    t = os.path.realpath(task_dir)
    rel = os.path.relpath(t, orq)
    parts = rel.split(os.sep)
    if rel.startswith("..") or len(parts) < 2 or not valid_project(parts[0]):
        return None
    return parts[0]


class Scope:
    """Where a diff applies and which ledger records it."""
    def __init__(self, kind, base, ledger_dir, ledger_name, backups_dir, project=None):
        self.kind, self.base, self.project = kind, base, project
        self.ledger_dir = ledger_dir
        self.ledger = os.path.join(ledger_dir, ledger_name)
        self.backups = backups_dir
        self.anchor = fleet_root() if kind == "generic" else orq_root()   # symlink checks start here

    def rel(self, path):
        return os.path.relpath(path, self.anchor)

    @property
    def key(self):
        return self.ledger

    def label(self):
        return f"project:{self.project}" if self.kind == "project" else "generic"


def generic_scope(base=None):
    root = fleet_root()
    return Scope("generic", base or root, skill_dir(root), "ledger.jsonl", os.path.join(skill_dir(root), "ledger-backups"))


def project_scope(project):
    pdir = os.path.join(orq_root(), project)
    return Scope("project", orq_root(), pdir, "lessons-ledger.jsonl", os.path.join(pdir, "lessons-backups"), project)


def scope_for_path(rel):
    """(Scope, normalized rel) for a diff path, or (None, reason)."""
    norm = _norm(rel)
    if not norm:
        return None, f"path escapes the root: {rel}"
    s = safe_rel(norm)
    if s:
        return generic_scope(), s
    if norm == lessons_name():
        return generic_scope(orq_root()), norm
    parts = norm.split("/")
    if len(parts) == 2 and parts[1] == LESSONS_FILE and valid_project(parts[0]):
        if not os.path.isdir(os.path.join(orq_root(), parts[0])):
            return None, f"no project folder {parts[0]}"
        return project_scope(parts[0]), norm
    return None, (f"path not allowed: {rel} (allowed: skill/ or .claude/agents/ except bookkeeping, "
                  f"{lessons_name()}, <project>/{LESSONS_FILE})")


def parse_scope_flag(value):
    if not value or value == "generic":
        return generic_scope()
    if value.startswith("project:") and valid_project(value[8:]):
        return project_scope(value[8:])
    raise ValueError(f"--scope must be generic or project:<name>, got {value!r}")


def symlink_in_path(base, rel):
    """First symlink among base/<rel components> (including the leaf), or None. Missing components are fine."""
    cur = base
    if os.path.islink(base.rstrip("/")) or os.path.realpath(base) != os.path.abspath(base):
        return base
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return cur
        if not os.path.exists(cur):
            break
    return None


def _parts(rel):
    return [x for x in _norm(rel).split("/") if x] if rel else []


def _open_dir(base, rel_dir, create):
    """fd of base/rel_dir walked one component at a time with O_NOFOLLOW (a symlinked component raises; no
    check-then-use gap). base itself must have no symlinked ancestor."""
    base = os.path.abspath(base)
    if os.path.realpath(base) != base:
        raise OSError(f"refusing base with a symlinked ancestor: {base}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(base, flags)
    try:
        for part in _parts(rel_dir):
            if part in (".", ".."):
                raise OSError(f"bad path component in {rel_dir}")
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError as e:
                raise OSError(f"refusing symlinked or non-directory path component {part} in {base}/{rel_dir}: {e}")
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_dir(base, rel_dir):
    """mkdir -p base/rel_dir refusing any symlinked component (and any symlinked ancestor of base)."""
    os.close(_open_dir(base, rel_dir, create=True))
    return os.path.join(os.path.abspath(base), rel_dir) if rel_dir else os.path.abspath(base)


def safe_write(base, rel, data):
    """Write base/rel atomically, never through a symlink: directories are walked by fd with O_NOFOLLOW, the temp file
    is created O_EXCL|O_NOFOLLOW in that directory and renamed relative to the same fd. Keeps an existing file's mode.
    data: str or bytes."""
    rel = _norm(rel)
    if not rel or rel.endswith("/"):
        raise OSError("bad path")
    name = os.path.basename(rel)
    dfd = _open_dir(base, os.path.dirname(rel), create=True)
    tmp = f".tmp-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        mode = 0o644
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise OSError(f"refusing symlinked path component {os.path.join(base, rel)}")
            mode = stat.S_IMODE(st.st_mode)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
        try:
            os.fchmod(fd, mode)   # exact mode, independent of the umask
            with os.fdopen(fd, "wb") as f:
                f.write(data.encode("utf-8") if isinstance(data, str) else data)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


# ------------------------------------------------------------------ diffs
def unified_diff(rel, old_text, new_text):
    """git-applicable unified diff for one file (old_text None = new file). Handles a missing final newline."""
    a = [] if old_text is None else old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)
    fa = "/dev/null" if old_text is None else f"a/{rel}"
    out = []
    for line in difflib.unified_diff(a, b, fa, f"b/{rel}", n=3):
        if line.startswith(("---", "+++")):
            out.append(line.rstrip("\n") + "\n")
        elif line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    if out and old_text is None:
        out.insert(0, f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n")
    elif out:
        out.insert(0, f"diff --git a/{rel} b/{rel}\n")
    return "".join(out)


def _strip_ab(p):
    return p[2:] if p.startswith(("a/", "b/")) else p


def diff_files(diff_text):
    """[(rel_path, is_new, is_delete)] from a unified diff's ---/+++ headers."""
    files, old = [], None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old = line[4:].split("\t")[0].strip()
        elif line.startswith("+++ ") and old is not None:
            new = line[4:].split("\t")[0].strip()
            rel = _strip_ab(new) if new != "/dev/null" else _strip_ab(old)
            files.append((rel, old == "/dev/null", new == "/dev/null"))
            old = None
    return files


_GIT_EXT_BAD = ("old mode", "new mode", "deleted file mode", "rename from", "rename to", "copy from", "copy to",
                "similarity index", "dissimilarity index", "GIT binary patch", "Binary files", "literal ", "delta ")


def patch_problems(diff_text):
    """Reasons a patch is not a plain text add/modify patch we can back up and ledger (empty = fine)."""
    probs = []
    lines = diff_text.splitlines()
    git_paths, hdr = [], set()
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            m = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not m or m.group(1) != m.group(2):
                probs.append(f"unsupported git header: {line[:120]}")
            else:
                git_paths.append(m.group(1))
        elif line.startswith("new file mode") and line.strip() != "new file mode 100644":
            probs.append(f"unsupported file mode: {line.strip()}")
        elif any(line.startswith(b) for b in _GIT_EXT_BAD):
            probs.append(f"unsupported patch operation (rename/copy/mode/binary/delete): {line[:80]}")
        elif line.startswith("--- ") or line.startswith("+++ "):
            p = line[4:].split("\t")[0].strip()
            if p != "/dev/null":
                if not p.startswith(("a/", "b/")):
                    probs.append(f"header path without a/ b/ prefix: {p}")
                hdr.add(_strip_ab(p))
    files = diff_files(diff_text)
    if not files:
        probs.append("no ---/+++ file headers")
    for gp in git_paths:
        if gp not in hdr:
            probs.append(f"git section without ---/+++ headers (headerless change): {gp}")
    return probs


def git_env(cwd):
    return dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(os.path.abspath(cwd)))


def git_apply(diff_path, cwd, check_only):
    cmd = ["git", "apply", "--whitespace=nowarn"] + (["--check"] if check_only else []) + [os.path.abspath(diff_path)]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=git_env(cwd))
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def git_patch_paths(diff_path, cwd):
    """What git itself would touch: (paths from --numstat, summary lines). Binary numstat rows are flagged."""
    r = subprocess.run(["git", "apply", "--numstat", "--summary", "-z", os.path.abspath(diff_path)], cwd=cwd,
                       capture_output=True, text=True, env=git_env(cwd))
    if r.returncode != 0:
        return None, [r.stderr.strip() or "git apply --numstat failed"]
    paths, summary = [], []
    for chunk in r.stdout.split("\0"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        for line in chunk.split("\n"):
            if not line.strip():
                continue
            m = re.fullmatch(r"(-|\d+)\t(-|\d+)\t(.*)", line)
            if m:
                if m.group(1) == "-":
                    summary.append(f"binary change: {m.group(3)}")
                if m.group(3):
                    paths.append(m.group(3))
            else:
                summary.append(line.strip())
    return paths, summary


# ------------------------------------------------------------------ ledgers
def read_ledger(scope=None):
    p = (scope or generic_scope()).ledger
    if not os.path.isfile(p) or os.path.islink(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


class LedgerLock:
    """Exclusive lock on <ledger>.lock (refuses a symlinked lock or ledger dir)."""
    def __init__(self, scope=None):
        self.scope = scope or generic_scope()
        d = self.scope.ledger_dir
        if os.path.islink(d):
            raise OSError(f"refusing symlinked ledger dir {d}")
        os.makedirs(d, exist_ok=True)
        self.path = self.scope.ledger + ".lock"

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        self.f = os.fdopen(fd, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def append_ledger(entry, scope=None):
    p = (scope or generic_scope()).ledger
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_id(scope=None):
    ids = [int(e["id"]) for e in read_ledger(scope) if str(e.get("id", "")).isdigit()]
    return str(max(ids, default=0) + 1)


def active_leads():
    d = vigia_active_dir()
    try:
        return sorted(n for n in os.listdir(d) if re.match(r"fleet-.*-lead-", n))
    except OSError:
        return []


def project_lessons_files(orq=None):
    """{project: path} for every ORQ_ROOT/<project>/LESSONS.md (read-only scan)."""
    orq = orq or orq_root()
    out = {}
    try:
        names = sorted(os.listdir(orq))
    except OSError:
        return out
    for n in names:
        p = os.path.join(orq, n, LESSONS_FILE)
        if valid_project(n) and os.path.isfile(p) and not os.path.islink(p):
            out[n] = p
    return out


_PUNCT = re.compile(r"[`*_>#\[\]()\"'“”‘’:;,.!?]+")


def normalize_rule(text):
    t = text.strip().lower()
    t = re.sub(r"^(\d+[.)]|[-*+]|\[[ x]\])\s+", "", t)
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ rule splitter and near-duplicates (shared by
# fleet-skill-lint and fleet_rules_index; moved here from fleet-skill-lint, behaviour unchanged)
NEAR = 0.88          # difflib ratio for a near-duplicate
NEAR_JACCARD = 0.5   # cheap token-set prefilter before difflib
RULE_MIN_CHARS = 25  # a normalized line shorter than this is not a rule


def rules_of_text(text):
    """[(line_no, raw, normalized)] for bullet / numbered lines and plain sentences long enough to be a rule.
    Skips fenced code, headings, tables and rulers."""
    out, fence = [], False
    for i, raw in enumerate(text.splitlines(), 1):
        if raw.strip().startswith("```"):
            fence = not fence; continue
        if fence or raw.lstrip().startswith(("#", "|", "---")):
            continue
        n = normalize_rule(raw)
        if len(n) >= RULE_MIN_CHARS:
            out.append((i, raw.strip(), n))
    return out


def rules_of(path):
    """rules_of_text() of a file ([] when it cannot be read)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return rules_of_text(f.read())
    except OSError:
        return []


def token_jaccard(a_norm, b_norm):
    """Jaccard of the whitespace token sets of two normalized rules (0.0 when either is empty)."""
    ta, tb = set(a_norm.split()), set(b_norm.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def near_dup_ratio(a_norm, b_norm, jaccard=NEAR_JACCARD):
    """difflib ratio of two normalized rules, or None when the token-set prefilter (Jaccard < jaccard) rules them out."""
    if token_jaccard(a_norm, b_norm) < jaccard:
        return None
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


def find_dups(files):
    """(exact groups [[(file, line, raw, norm)]], near [(ratio, item_a, item_b)] sorted by ratio desc)."""
    items = [(f, i, raw, n) for f in files for (i, raw, n) in rules_of(f)]
    exact, near, seen = [], [], {}
    for it in items:
        seen.setdefault(it[3], []).append(it)
    for n, group in seen.items():
        if len(group) > 1:
            exact.append(group)
    uniq = [g[0] for g in seen.values()]
    for a in range(len(uniq)):
        for b in range(a + 1, len(uniq)):
            r = near_dup_ratio(uniq[a][3], uniq[b][3])
            if r is not None and r >= NEAR:
                near.append((r, uniq[a], uniq[b]))
    return exact, sorted(near, key=lambda x: -x[0])


def find_promotions(per_project):
    """[(sorted projects, sample text)] for lessons that appear (normalized or near-duplicate) in 2+ projects."""
    items = [(proj, raw, n) for proj, rules in per_project.items() for (_, raw, n) in rules]
    groups = []   # [ [set(projects), representative normalized, sample raw] ]
    for proj, raw, n in items:
        for g in groups:
            r = near_dup_ratio(n, g[1])
            if n == g[1] or (r is not None and r >= NEAR):
                g[0].add(proj)
                break
        else:
            groups.append([{proj}, n, raw])
    return [(sorted(g[0]), g[2]) for g in groups if len(g[0]) >= 2]


# ------------------------------------------------------------------ budgets, merge-guard, archive (PLAN F2)
def budget_of(rel, policy=None):
    """(budget key, char limit) of a fleet/project file under policy["budgets"], or (None, None) when it has none.
    LESSONS.md of a project and the generic lessons file have a limit each, every skill/*/SKILL.md has one, and the
    files of skill/fleet-sdd/references/ share ONE total (key "references")."""
    b = (policy or load_policy())["budgets"]
    if rel == lessons_name():
        return "file:" + rel, b["learnings_chars"]
    if rel.endswith("/" + LESSONS_FILE) and rel.count("/") == 1:
        return "file:" + rel, b["lessons_chars"]
    if rel.startswith("skill/") and rel.endswith("/SKILL.md"):
        return "file:" + rel, b["skill_md_chars"]
    if rel.startswith("skill/fleet-sdd/references/") and rel.endswith(".md") and rel.count("/") == 3:
        return "references", b["references_total_chars"]
    return None, None


def references_sizes(root=None):
    """{diff path: chars} of the files in skill/fleet-sdd/references/ (the files sharing the references budget)."""
    d = os.path.join(skill_dir(root), "fleet-sdd", "references")
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".md") and os.path.isfile(p) and not os.path.islink(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                out[f"skill/fleet-sdd/references/{n}"] = len(f.read())
    return out


_BLOCK_START = re.compile(r"(?:[-*+]|\d+[.)])\s")
_LITERAL_RX = re.compile(r"`([^`\n]+)`")


def blocks_of(text):
    """Blocks of a text: paragraphs separated by blank lines, where every unindented bullet / numbered item starts a
    new block (its indented continuation lines belong to it)."""
    blocks, cur = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            if cur and _BLOCK_START.match(line):
                blocks.append("\n".join(cur))
                cur = []
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def block_spans(text):
    """[(start, end)] character spans of the blocks_of() blocks of a text, in order: each from the start of its first
    line to the end of its last line (line break included)."""
    spans, start, prev_end, pos = [], None, 0, 0
    for line in (text or "").splitlines(keepends=True):
        if not line.strip():
            if start is not None:
                spans.append((start, prev_end))
                start = None
        else:
            if start is not None and _BLOCK_START.match(line):
                spans.append((start, prev_end))
                start = None
            if start is None:
                start = pos
            prev_end = pos + len(line)
        pos += len(line)
    if start is not None:
        spans.append((start, prev_end))
    return spans


def widen_to_blocks(text, s, e):
    """(s, e) widened to cover every blocks_of() block of `text` it touches."""
    for bs, be in block_spans(text):
        if bs < e and s < be:
            s, e = min(s, bs), max(e, be)
    return s, e


def whole_blocks(text, start, end):
    """True when text[start:end] is made of whole lines that are whole blocks_of() blocks (with blank lines): no line
    is cut and no block is split, so removing it neither leaves a fragment nor glues a continuation to another rule."""
    if not (start == 0 or text[start - 1] == "\n") or not (end == len(text) or text[end - 1] == "\n"
                                                           or text[end] == "\n"):
        return False
    return blocks_of(text[:start]) + blocks_of(text[start:end]) + blocks_of(text[end:]) == blocks_of(text)


def change_blocks(before, after):
    """(input blocks, result text) of a change, derived from the file text before/after it (line diff): the blocks of
    every removed or replaced region, and the concatenation of every added or replacing region (pooled over the whole
    text, informative only: the merge-guard callers use guard_change, which checks every old block on its own)."""
    a = (before or "").splitlines(keepends=True)
    b = (after or "").splitlines(keepends=True)
    inputs, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            inputs += blocks_of("".join(a[i1:i2]))
        if tag in ("replace", "insert"):
            added.append("".join(b[j1:j2]))
    return inputs, "".join(added)


def line_span(text, start, end):
    """(s, e): [start, end) of text widened to whole lines (a span ending right after a newline stays there)."""
    s = text.rfind("\n", 0, start) + 1
    if end > start and text[end - 1] == "\n":
        return s, end
    e = text.find("\n", end)
    return s, (len(text) if e < 0 else e + 1)


def merge_sources(cur, sources, new):
    """cur with the first (in file order) source replaced by `new` and the other sources removed (whole lines with
    their line break) → (updated, start of `new` in updated, source spans sorted). ValueError when a source does not
    occur exactly once or two sources overlap."""
    spans = []
    for s in sources:
        if cur.count(s) != 1:
            raise ValueError(f"merge source found {cur.count(s)} times in the target (must be exactly once): "
                             f"{s.strip()[:60]}")
        i = cur.index(s)
        spans.append((i, i + len(s)))
    spans.sort()
    if any(spans[k][1] > spans[k + 1][0] for k in range(len(spans) - 1)):
        raise ValueError("merge sources overlap")
    out = cur
    for k, (i, j) in reversed(list(enumerate(spans))):
        out = out[:i] + new + out[j:] if k == 0 else remove_block(out, i, j)
    return out, spans[0][0], spans


def _verified_merges(before, after, merges, added):
    """{old block: [merge result texts]} of the declared merges (fleet-retro proposal metadata) that the diff itself
    shows: every source occurs exactly once in `before`, edit.new exactly once in `after`, and the result (the whole
    lines holding edit.new) is either touched by the diff (one of its blocks is an added block) or is verbatim one of
    the sources (the "merged block equals a source" case). Anything else is ignored: the metadata can only RELAX the
    one case of an old block whose replacement is that verified merge result, never replace the diff check."""
    out = {}
    for op in merges or ():
        if not isinstance(op, dict) or op.get("op") != "merge":
            continue
        sources, new = op.get("sources"), op.get("new")
        if (not isinstance(sources, list) or not sources or not isinstance(new, str) or not new.strip()
                or any(not isinstance(s, str) or not s.strip() or before.count(s) != 1 for s in sources)
                or after.count(new) != 1):
            continue
        at = after.index(new)
        result = after[slice(*line_span(after, at, at + len(new)))]
        src_texts = [before[slice(*line_span(before, before.index(s), before.index(s) + len(s)))] for s in sources]
        if not (any(b in added for b in blocks_of(result)) or result in src_texts):
            continue
        for t in src_texts:
            for blk in blocks_of(t):
                out.setdefault(blk, []).append(result)
    return out


def guard_change(before, after, drops=(), min_ratio=0.8, merges=()):
    """The merge-guard of one file change (code, 0 tokens) → (ok, reason). The one function fleet-retro validate (each
    proposal against the original file text) and fleet-skill-apply (each file of the diff) both use.
    Granularity is the rule block (blocks_of): the block sequences before/after are diffed; inside every changed region
    each removed old block is paired with its replacement, the most similar added block of the same region (difflib
    ratio, greedy, each added block used at most once), and merge_guard([old], replacement) must pass (>= min_ratio x
    the old block, every backtick literal of the old block kept unless in drops). An old block with no replacement is a
    deletion and is refused (op archive is the way to remove a block) unless it is a source of a verified declared
    merge (`merges`: [{"op": "merge", "sources": [...], "new": ...}], see _verified_merges) whose result passes the same
    check; that result may also stand in for a source whose paired block fails. Nothing else in `merges` is used, so
    the check is never weaker than on the diff alone except for that verified-merge case."""
    a, b = blocks_of(before or ""), blocks_of(after or "")
    regions, added = [], set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            regions.append((a[i1:i2], b[j1:j2]))
            added.update(b[j1:j2])
    results = _verified_merges(before or "", after or "", merges, added) if merges else {}
    for olds, news in regions:
        scored = sorted((-difflib.SequenceMatcher(None, o, n, autojunk=False).ratio(), oi, ni)
                        for oi, o in enumerate(olds) for ni, n in enumerate(news))
        pair, used = {}, set()
        for _, oi, ni in scored:
            if oi not in pair and ni not in used:
                pair[oi] = ni
                used.add(ni)
        for oi, old in enumerate(olds):
            if not old.strip():
                continue
            ok, why = merge_guard([old], news[pair[oi]] if oi in pair else "", drops, min_ratio)
            for r in (results.get(old, ()) if not ok else ()):   # a verified merge source: the merged block replaces it
                ok, rwhy = merge_guard([old], r, drops, min_ratio)
                if ok:
                    break
                why = rwhy if oi not in pair else why   # a deleted source: the merged block's failure is the reason
            if not ok:
                return False, f"{why} (old block: {old.strip()[:80]!r})"
    return True, "ok"


def backtick_literals(text):
    """Ordered unique `backtick` literals (commands, paths) of a text, stripped."""
    seen = []
    for m in _LITERAL_RX.findall(text or ""):
        m = m.strip()
        if m and m not in seen:
            seen.append(m)
    return seen


def merge_guard(inputs, result, drops=(), min_ratio=0.8):
    """PLAN F2 merge-guard, pure (0 tokens) → (ok, reason). The merged/edited `result` must be at least min_ratio x the
    largest input block, and every backtick literal of the inputs must survive in the result unless it is listed in
    `drops`. The reason names the offending literal. No input blocks (a pure addition) is always fine."""
    inputs = [i for i in (inputs or []) if i and i.strip()]
    if not inputs:
        return True, "no input blocks"
    big, got = max(len(i.strip()) for i in inputs), len((result or "").strip())
    if not got:
        return False, ("merge-guard: the change only deletes text (the result is empty): use op archive to keep the text, "
                       "or write a merged block that differs from every source")
    if got < min_ratio * big:
        return False, (f"merge-guard: result has {got} chars = {got / big:.2f}x the largest input block ({big} chars); "
                       f"minimum is {min_ratio}x")
    dropped = {str(d).strip() for d in (drops or ())}
    have = set(backtick_literals(result))
    for lit in (x for i in inputs for x in backtick_literals(i)):
        if lit not in have and lit not in dropped:
            return False, (f"merge-guard: backtick literal `{lit}` of the inputs is missing from the result "
                           f"(keep it or list it in drops)")
    return True, "ok"


_TOKEN_RX = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[a-z]-[A-Za-z0-9-]{8,}|Bearer\s+\S{8,})")
_SECRET_RX = re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*\S{6,}|"
                        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b[A-Za-z0-9+]{40,}={0,2}")
_REDACT = []


def has_secret(text):
    """True when a text looks like it holds a secret (same test as fleet-retro: vigia's redactor when installed, else
    a token pattern, plus key=value / private key / long base64 patterns). For human-supplied ledger text
    (fleet-skill-apply --reason / --evidence): refuse it instead of persisting it."""
    if not text or not isinstance(text, str):
        return False
    if not _REDACT:
        fn = None
        try:
            vb = os.path.expanduser(os.environ.get("VIGIA_BIN") or os.path.join(os.path.dirname(fleet_root()), "bin"))
            import sys
            if vb not in sys.path:
                sys.path.insert(0, vb)
            from vigia_comms import redact as fn
        except Exception:
            fn = None
        _REDACT.append(fn)
    fn = _REDACT[0]
    try:
        if fn is not None and fn(text) != text:
            return True
    except Exception:
        pass
    return bool(_TOKEN_RX.search(text) or _SECRET_RX.search(text))


def remove_block(text, start, end):
    """text without text[start:end]; when the block is whole lines, its line break goes with it."""
    if (start == 0 or text[start - 1] == "\n") and text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


ARCHIVE_KINDS = ("redundancy", "supersession", "error")
_ARCH_HEAD, _ARCH_TAIL = b"<!-- fleet-archive\n", b"\n-->\n"


def archive_dir(scope):
    """Directory (absolute) receiving block archives: generic -> <fleet root>/skill/archive, project ->
    ORQ_ROOT/<project>/lessons-archive. Nothing in it is ever deleted; restored blocks move to its restored/ folder."""
    return (os.path.join(skill_dir(fleet_root()), "archive") if scope.kind == "generic"
            else os.path.join(orq_root(), scope.project, "lessons-archive"))


def archive_slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", normalize_rule(text)).strip("-")[:n].strip("-")
    return s or "block"


def archive_pack(meta, block):
    """Bytes of an archive file: a JSON header (reason, provenance) in a comment, then the exact archived bytes."""
    return _ARCH_HEAD + json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8") + _ARCH_TAIL + block


def archive_unpack(data):
    """(meta dict, exact block bytes) of archive_pack() output; ValueError when it is not one."""
    if not data.startswith(_ARCH_HEAD):
        raise ValueError("not a fleet archive file")
    i = data.find(_ARCH_TAIL, len(_ARCH_HEAD))
    if i < 0:
        raise ValueError("archive header not closed")
    return json.loads(data[len(_ARCH_HEAD):i].decode("utf-8")), data[i + len(_ARCH_TAIL):]


def deleted_block(before, after):
    """(byte offset, exact bytes) of the ONE contiguous run of whole lines that `after` lacks compared with `before`
    (both bytes); None when the change is anything else. before == after[:offset] + block + after[offset:]."""
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != "equal"]
    if len(ops) != 1 or ops[0][0] != "delete":
        return None
    _, i1, i2, _, _ = ops[0]
    return sum(map(len, a[:i1])), b"".join(a[i1:i2])
