"""Shared helpers for the fleet retro loops (fleet-retro, fleet-skill-apply, fleet-skill-lint). Python 3 stdlib only.

Two loops, two kinds of target:
- generic: fleet skill files (paths relative to the fleet root: skill/..., .claude/agents/...) and the Orquestra
  lessons file (learnings.md by default, FLEET_LESSONS_NAME to rename; relative to the Orquestra root). Ledger: FLEET_ROOT/skill/ledger.jsonl.
- project: ORQ_ROOT/<project>/LESSONS.md (diff path "<project>/LESSONS.md", relative to the Orquestra root).
  Ledger: ORQ_ROOT/<project>/lessons-ledger.jsonl, backups ORQ_ROOT/<project>/lessons-backups/.
FLEET_ROOT env (default: the folder above bin/); ORQ_ROOT env (default: ORQUESTRA_HOME, else the folder above the
fleet root). Vigia active-run registry: VIGIA_ATIVOS env, else VIGIA_BASE/active, else ORQ_ROOT/vigia/active."""
import difflib, fcntl, hashlib, json, os, re, stat, subprocess, tempfile, time

BIN = os.path.dirname(os.path.realpath(__file__))
EDITABLE = ("skill/", ".claude/agents/")
# bookkeeping under skill/: never a patch target (history, lock, backups, proposals, archive, lint output)
RESERVED = ("skill/ledger", "skill/ledger-backups/", "skill/proposals/", "skill/archive/", "skill/lint-report.md",
            "skill/retro-state.json", "skill/retro-batches/")
NOT_PROJECTS = {"fleet", "bin", "vigia", "skill", "tmp"}
APRENDIZADOS = os.environ.get("FLEET_LESSONS_NAME") or "learnings.md"   # generic Orquestra lessons file
LESSONS_FILE = "LESSONS.md"
LESSONS_CAP = 4000
_PROJECT_RX = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}")


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
    return os.environ.get("FLEET_LESSONS") or os.path.join(orq_root(), APRENDIZADOS)


def skill_dir(root=None):
    return os.path.join(root or fleet_root(), "skill")


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
    return bool(name) and bool(_PROJECT_RX.fullmatch(name)) and name not in NOT_PROJECTS and not name.startswith(".")


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
    if norm == APRENDIZADOS:
        return generic_scope(orq_root()), norm
    parts = norm.split("/")
    if len(parts) == 2 and parts[1] == LESSONS_FILE and valid_project(parts[0]):
        if not os.path.isdir(os.path.join(orq_root(), parts[0])):
            return None, f"no project folder {parts[0]}"
        return project_scope(parts[0]), norm
    return None, (f"path not allowed: {rel} (allowed: skill/ or .claude/agents/ except bookkeeping, "
                  f"{APRENDIZADOS}, <project>/{LESSONS_FILE})")


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
