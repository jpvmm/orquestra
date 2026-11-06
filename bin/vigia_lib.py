"""Shared library for Vigia (supervisor for headless Claude Code / Codex CLI runs).
Python 3 standard library only. Used by vigia-run, vigia-status, vigia-notify and vigia-stop.

Configuration (environment variables):
  ORQUESTRA_HOME   base folder for Orquestra (default: the install folder created by install.sh,
                   detected via its .orquestra-home marker; otherwise ~/.orquestra)
  VIGIA_BASE       Vigia state folder: registry, event log, webhook config (default: $ORQUESTRA_HOME/vigia)
  VIGIA_MAX_ACTIVE maximum number of concurrently supervised runs (default: 5)
  VIGIA_TZ         IANA time zone for timestamps, e.g. "Europe/Lisbon" (default: system local time)
  VIGIA_HB_STALE_SEC    watchdog: a running run whose status.json (heartbeat) is older than this is suspect (default: 180)
  VIGIA_WD_GRACE_SEC    watchdog: grace after the wall-clock --max-min before alerting (default: 120)
  VIGIA_WD_RECHECK_SEC  watchdog: awake seconds to wait for a fresh heartbeat before acting (default: 20)
"""
import contextlib, json, os, re, fcntl, time, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

BIN = os.path.dirname(os.path.abspath(__file__))


def _default_home():
    # an install made by install.sh drops a marker next to bin/, so the installed copy finds its own home
    parent = os.path.dirname(BIN)
    return parent if os.path.exists(os.path.join(parent, ".orquestra-home")) else "~/.orquestra"


HOME = os.path.expanduser(os.environ.get("ORQUESTRA_HOME") or _default_home())
BASE = os.path.expanduser(os.environ.get("VIGIA_BASE") or os.path.join(HOME, "vigia"))
ACTIVE_DIR = os.path.join(BASE, "active")
EVENTS_FILE = os.path.join(BASE, "events.jsonl")
MAX_ACTIVE = int(os.environ.get("VIGIA_MAX_ACTIVE", "5"))
TZ = ZoneInfo(os.environ["VIGIA_TZ"]) if os.environ.get("VIGIA_TZ") else None
FINAL_STATES = ("done", "failed", "stalled", "timeout", "loop", "killed", "incomplete")
# watchdog (watch_active): heartbeat = mtime of status.json (the supervisor touches it every poll)
HB_STALE = float(os.environ.get("VIGIA_HB_STALE_SEC", "180"))
WD_GRACE = float(os.environ.get("VIGIA_WD_GRACE_SEC", "120"))
WD_RECHECK = float(os.environ.get("VIGIA_WD_RECHECK_SEC", "20"))


def now_local():
    """Timezone-aware 'now' (VIGIA_TZ if set, otherwise the system local zone)."""
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


def now_iso():
    return now_local().isoformat(timespec="seconds")


def iso_from(ts):
    if not ts:
        return None
    d = datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts).astimezone()
    return d.isoformat(timespec="seconds")


def write_json_atomic(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


# ---------- processes (Linux /proc) ----------

def proc_stat(pid):
    """Return a dict with ppid, pgid, sid, state, cpu ticks and start time, or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except Exception:
        return None
    r = data.rfind(")")
    rest = data[r + 2:].split()
    # rest[0]=state(3) rest[1]=ppid(4) rest[2]=pgrp(5) rest[3]=session(6) ... utime(14)=rest[11] stime(15)=rest[12] starttime(22)=rest[19]
    return {
        "pid": pid,
        "state": rest[0],
        "ppid": int(rest[1]),
        "pgid": int(rest[2]),
        "sid": int(rest[3]),
        "cpu": int(rest[11]) + int(rest[12]),
        "start": int(rest[19]),
    }


def all_procs():
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            s = proc_stat(int(d))
            if s:
                out[s["pid"]] = s
    return out


def alive(pid, start=None):
    """Process exists (not a zombie) and, if given, has the same start time (guards against PID reuse)."""
    if not pid:
        return False
    s = proc_stat(int(pid))
    if not s or s["state"] in ("Z", "X"):
        return False
    return start is None or s["start"] == start


def members(pgid, root_pids=(), procs=None):
    """Live PIDs in process group pgid plus descendants of the root PIDs (even if they left the group)."""
    procs = procs or all_procs()
    sel = {p for p, s in procs.items() if s["pgid"] == pgid and s["state"] not in ("Z", "X")}
    children = {}
    for p, s in procs.items():
        children.setdefault(s["ppid"], []).append(p)
    stack = list(root_pids) + list(sel)
    seen = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        for c in children.get(p, []):
            if procs[c]["state"] not in ("Z", "X"):
                sel.add(c)
            stack.append(c)
    return sel, procs


# ---------- registry of active runs ----------

class Lock:
    """Exclusive flock on the active-runs registry."""

    def __init__(self):
        os.makedirs(ACTIVE_DIR, exist_ok=True)
        self.f = open(os.path.join(ACTIVE_DIR, ".lock"), "a+")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def active_entries():
    out = []
    for n in sorted(os.listdir(ACTIVE_DIR)):
        if n.endswith(".json"):
            e = read_json(os.path.join(ACTIVE_DIR, n))
            if e:
                e["_file"] = os.path.join(ACTIVE_DIR, n)
                out.append(e)
    return out


def cleanup_dead():
    """Remove registry entries whose supervisor died. If the supervised command is still
    alive (supervisor was killed hard), kill its group and mark the status as killed.
    Call while holding Lock. Returns the list of cleaned keys."""
    cleaned = []
    for e in active_entries():
        if alive(e.get("supervisor_pid"), e.get("supervisor_start")):
            continue
        st = read_json(e.get("status_path", "")) or {}
        if st.get("state") == "running":
            import signal, subprocess, time as _t
            pgid = st.get("pgid")
            leftovers = members(pgid, [st["pid"]] if st.get("pid") else [])[0] if pgid else set()
            for target in ([("g", pgid)] if pgid else []) + [("p", x) for x in leftovers]:
                try:
                    (os.killpg if target[0] == "g" else os.kill)(target[1], signal.SIGKILL)
                except Exception:
                    pass
            _t.sleep(0.5)
            pg = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True) if pgid else None
            st["kill"] = {"targets": sorted(leftovers), "kill_sent": bool(leftovers),
                          "pgrep_g": pg.stdout.split() if pg else [], "ok": (pg is None or pg.returncode == 1),
                          "verified_at": now_iso(), "reason": "supervisor died; registry cleanup"}
            st["state"] = "killed"
            st["ended_at"] = now_iso()
            st["summary"] = "supervisor died; registry cleanup killed what was left of the group"
            try:
                write_json_atomic(e["status_path"], st)
                subprocess.Popen([os.path.join(BIN, "vigia-notify"), e["status_path"], "--event", "finished",
                                  "--state", "killed", "--prev", "running",
                                  "--detail", "supervisor died; registry cleanup"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
        try:
            os.unlink(e["_file"])
        except FileNotFoundError:
            pass
        cleaned.append(e.get("key"))
    return cleaned


@contextlib.contextmanager
def run_lock(sp, wait=None):
    """Per-run flock (<status>.lock) shared by the supervisor (save) and the watchdog. wait=None blocks; otherwise
    tries for 'wait' seconds and yields False on failure (a stopped supervisor holding the lock: the watchdog retries later)."""
    fd = os.open(sp + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        ok = False
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            ok = True
        else:
            end = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    ok = True
                    break
                except OSError:
                    if time.monotonic() >= end:
                        break
                    time.sleep(0.1)
        yield ok
    finally:
        os.close(fd)


def _notify_cmd(sp, event, state, prev, detail=None):
    cmd = [os.path.join(BIN, "vigia-notify"), sp, "--event", event, "--state", state, "--prev", prev]
    if detail:
        cmd += ["--detail", detail]
    return cmd


def _notify_bg(sp, event, state, prev, detail=None):
    import subprocess
    try:
        subprocess.Popen(_notify_cmd(sp, event, state, prev, detail), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _notify(sp, event, state, prev, detail=None, timeout=30):
    """Synchronous (keeps ordering: detected before finished)."""
    import subprocess
    try:
        subprocess.run(_notify_cmd(sp, event, state, prev, detail), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        pass


def _valid_targets(pgid, pid, pid_start):
    """Live members of the run's group, without killing a stranger after PID reuse: if the run's pid exists with a
    different start time (zombie or not), it is no longer ours (nor its children) and, if it was the group leader
    (pgid == pid), neither is the group. A zombie leader with the same start time is still ours (its group and
    children remain targets)."""
    s = proc_stat(int(pid)) if pid else None
    reused = s is not None and pid_start is not None and s["start"] != pid_start
    if reused and pgid == pid:
        return None, set()
    sel = members(pgid, [pid] if s is not None and not reused else [])[0] if pgid else set()
    return pgid, sel


def _kill_pg(pgid, pid, pid_start=None):
    """SIGTERM then SIGKILL the run's group (revalidated before each signal). Returns a kill report."""
    import signal, subprocess
    g, leftovers = _valid_targets(pgid, pid, pid_start)
    targets0 = sorted(leftovers)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        g, leftovers = _valid_targets(pgid, pid, pid_start)  # revalidate before each signal
        for target in ([("g", g)] if g and leftovers else []) + [("p", x) for x in leftovers]:
            try:
                (os.killpg if target[0] == "g" else os.kill)(target[1], sig)
            except Exception:
                pass
        end = time.monotonic() + 5
        while time.monotonic() < end and _valid_targets(pgid, pid, pid_start)[1]:
            time.sleep(0.25)
    pg = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True) if pgid else None
    survivors = sorted(_valid_targets(pgid, pid, pid_start)[1])  # no zombies: the (stopped) supervisor reaps them
    return {"targets": targets0, "kill_sent": bool(targets0), "pgrep_g": pg.stdout.split() if pg else [],
            "survivors": survivors, "ok": not survivors, "verified_at": now_iso()}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def watch_active():
    """Check independent of the supervisor (vigia-status and vigia-run call it, without Lock; may wait up to
    WD_RECHECK s). For a 'running' run whose supervisor is alive:
      - heartbeat (status.json mtime) older than HB_STALE s, confirmed after re-reading for up to WD_RECHECK s
        (the box may have just resumed from suspension: a healthy supervisor updates within seconds) => the
        supervisor is not watching: notify 'alert' once; if it is also past the wall-clock max-min or stall-min,
        kill the group, mark timeout/stalled and notify detected + finished;
      - heartbeat ok but past the wall-clock max-min + WD_GRACE => the supervisor did not enforce the cap: 'alert'.
    Events are deduplicated by <run>.notified.json (fixed detail). Returns [(name, action)]."""
    now = time.time()
    suspects = []
    for e in active_entries():
        if not alive(e.get("supervisor_pid"), e.get("supervisor_start")):
            continue  # dead supervisor: cleanup_dead handles it
        sp = e.get("status_path", "")
        st = read_json(sp) or {}
        if st.get("state") != "running" or not st.get("started_at"):
            continue
        try:
            hb = os.path.getmtime(sp)
            dur = now - datetime.fromisoformat(st["started_at"]).timestamp()
        except Exception:
            continue
        mx = float((st.get("limits") or {}).get("max_min") or 0)
        if now - hb > HB_STALE or (mx and dur > mx * 60 + WD_GRACE):
            suspects.append((e, sp, hb))
    if not suspects:
        return []
    # recheck for WD_RECHECK s of AWAKE time (monotonic): if the box pauses midway the deadline does not run, and a
    # healthy supervisor (touches status.json every poll) has time to show a heartbeat after resuming.
    w0, m0 = time.time(), time.monotonic()
    while time.monotonic() - m0 < WD_RECHECK:
        if all(_mtime(sp) != hb for _e, sp, hb in suspects):
            break
        time.sleep(1)
    if (time.time() - w0) - (time.monotonic() - m0) > 30:
        return []  # the box was suspended during the recheck: decide on the next call
    done = []
    for e, sp, hb in suspects:
        st = read_json(sp) or {}
        if st.get("state") != "running":
            continue
        now = time.time()
        hb2 = _mtime(sp)
        lim = st.get("limits") or {}
        mx, sm = float(lim.get("max_min") or 0), float(lim.get("stall_min") or 0)
        dur = now - datetime.fromisoformat(st["started_at"]).timestamp()
        stale = now - hb2 if hb2 == hb else 0
        name = st.get("name") or e.get("name")
        if stale > HB_STALE:
            _notify_bg(sp, "alert", "running", "running",
                       f"watchdog: supervisor has no heartbeat (status.json unchanged > {HB_STALE:g} s)")
            reason = "timeout" if mx and dur > mx * 60 else ("stalled" if sm and stale > sm * 60 else None)
            if not reason:
                done.append((name, "alert:no-heartbeat"))
                continue
            summary = (f"watchdog: past {mx:g} min of wall clock and the supervisor was not watching" if reason == "timeout"
                       else f"watchdog: supervisor without heartbeat for {int(stale // 60)} min (> stall {sm:g} min)")
            # claim the run under its lock: the supervisor only saves status under the same lock and honors the mark;
            # a second watchdog sees the mark and gives up. Final state + mark BEFORE the kill.
            with run_lock(sp, wait=5) as ok:
                if not ok:
                    continue
                st = read_json(sp) or {}
                if st.get("state") != "running" or st.get("watchdog") or _mtime(sp) != hb:
                    continue  # the supervisor came back, or another watchdog took over
                st.update(stopping=st.get("stopping") or {"reason": reason, "detail": summary, "at": now_iso()},
                          watchdog=summary, state=reason, ended_at=now_iso(), summary=summary)
                write_json_atomic(sp, st)
            sto = st["stopping"]
            if sto.get("reason") == reason and sto.get("detail") == summary:
                det = f"{reason}: watchdog (supervisor without heartbeat)"
            else:  # the supervisor had already detected it: same detail => vigia-notify dedups if it was already sent
                det = f"{sto.get('reason')}: {sto.get('detail')}"
            _notify(sp, "detected", "running", "running", det)
            st["kill"] = dict(_kill_pg(st.get("pgid"), st.get("pid"), st.get("pid_start")),
                              reason="watchdog: supervisor without heartbeat")
            write_json_atomic(sp, st)
            try:
                os.unlink(e["_file"])
            except OSError:
                pass
            _notify_bg(sp, "finished", reason, "running", "watchdog")
            done.append((name, reason))
        elif mx and dur > mx * 60 + WD_GRACE:
            _notify_bg(sp, "alert", "running", "running",
                       f"watchdog: past {mx:g} min of wall clock and the supervisor did not stop it (old supervisor?)")
            done.append((name, "alert:cap"))
    return done


# ---------- patterns ----------

TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|(^|/)[^/]*_test\.[^/]+$|\.spec\.[^/]+$|\.test\.[^/]+$|(^|/)conftest\.py$"
)
TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|python3? -m (pytest|unittest)|nose2|tox|jest|vitest|mocha|go test|cargo test|"
    r"(npm|pnpm|yarn|bun)( run)? test|make (test|check)|phpunit|rspec|ctest|mvn test|gradle\w* test|deno test)\b"
)
SHELL_WRITE_RE = re.compile(
    r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?([^\s;|&'\"]+)|\b(?:sed\s+-i\S*|perl\s+-p?i\S*|rm(?:\s+-\S+)*|mv|cp|truncate(?:\s+-\S+)*|"
    r"git\s+(?:checkout|restore)(?:\s+--)?)\s+((?:[^\s;|&]+\s*)+)"
)
ALERT_RE = re.compile(
    r"rate.?limit|usage limit|too many requests|\b429\b|quota|authentication_failed|invalid api key|not logged in|"
    r"please run /login|unauthorized|\b401\b|oauth token|login required|credit balance",
    re.I,
)

# agent stderr: this pattern always applies; ALERT_RE only on Claude's stderr (Codex echoes the whole conversation: noise)
STDERR_ALERT_RE = re.compile(r"Background tasks still running", re.I)
LEAD_RE = re.compile(r"fleet-.+-lead-.+")  # fleet lead: must leave T/FINAL-SUMMARY.md


def _under_roots(path, roots):
    """True if path (abs or relative) falls under one of the absolute roots.
    Relatives count as inside (resolved against the run cwd). Absolute paths
    outside the roots (e.g. /tmp/test_foo.py) do NOT — avoids false positives
    from --protect-tests on scratch outside the worktree."""
    if not roots:
        return True
    p = str(path)
    if not os.path.isabs(p):
        return True
    abs_p = os.path.abspath(p)
    for r in roots:
        if not r:
            continue
        root = os.path.abspath(r)
        if root == os.sep:
            return True  # filesystem root contains everything
        if abs_p == root or abs_p.startswith(root + os.sep):
            return True
    return False


def git_plain(repo, *args, **kw):
    """git for protection checks: replacement refs (`git replace`) ignored, so an agent cannot swap the base."""
    import subprocess
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(k, None)
    r = subprocess.run(["git", "--no-replace-objects", "-C", repo, *args], capture_output=True, env=env, **kw)
    # paths are bytes: surrogateescape keeps any non-UTF-8 name usable with os.path (never raises)
    r.stdout = r.stdout.decode("utf-8", "surrogateescape")
    r.stderr = r.stderr.decode("utf-8", "replace")
    return r


def rev_sha(repo, rev):
    """Full commit sha of rev in repo (raises CalledProcessError if it is not a commit)."""
    import subprocess
    r = git_plain(repo, "rev-parse", "--verify", rev + "^{commit}")
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, "git rev-parse", r.stdout, r.stderr)
    return r.stdout.strip()


class ProtectedTests:
    """--protect-tests protects the test files that already exist on the base commit.

    fleet-run snapshots them into the Vigia's protected/<hash of T>.txt (one repo-relative path per line). With the
    snapshot, only those paths are protected, so TDD can add NEW test files. Without it (older task folders,
    raw vigia-run), every test-named path is protected, as before."""

    @staticmethod
    def path(task_dir):
        """Kept in the Vigia's folder, not in T: agents write T (Codex workspace-write, Claude --add-dir)."""
        if not task_dir:
            return None
        return os.path.join(BASE, "protected", h(os.path.abspath(task_dir)) + ".txt")

    @staticmethod
    def snapshot(repo, rev, task_dir):
        """Write the base commit's test files to the protected list of task_dir; return (path, count)."""
        import subprocess
        r = git_plain(repo, "ls-tree", "-r", "-z", rev)
        if r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, "git ls-tree", r.stdout, r.stderr)
        manifest = ProtectedTests.parse_tree(r.stdout)
        tests = sorted(manifest)
        dest = ProtectedTests.path(task_dir)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        for path, txt in ((dest, "".join(t + "\n" for t in tests)), (ProtectedTests.base_path(task_dir), rev_sha(repo, rev) + "\n")):
            with open(path + ".tmp", "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(txt)
            os.replace(path + ".tmp", path)
        mp = ProtectedTests.manifest_path(task_dir)   # trusted blob ids/modes, taken before any agent runs
        with open(mp + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"base": rev_sha(repo, rev), "tests": manifest}, f)
        os.replace(mp + ".tmp", mp)
        return dest, len(tests)

    @staticmethod
    def parse_tree(out):
        """{path: [mode, blob id]} of the test files in `git ls-tree -r -z` output."""
        blobs = {}
        for ent in out.split("\0"):
            if "\t" in ent:
                meta, path = ent.split("\t", 1)
                parts = meta.split()
                if len(parts) == 3 and parts[1] == "blob" and TEST_RE.search(path):
                    blobs[path] = [parts[0], parts[2]]
        return blobs

    @staticmethod
    def manifest_path(task_dir):
        return ProtectedTests.path(task_dir)[:-4] + ".manifest.json"

    @staticmethod
    def manifest_for(task_dir, repo, base):
        """The trusted manifest when it was taken at this same base commit, else None."""
        import subprocess
        try:
            with open(ProtectedTests.manifest_path(task_dir), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
        try:
            same = m.get("base") == rev_sha(repo, base)
        except (OSError, subprocess.SubprocessError):
            same = str(m.get("base") or "").startswith(base) and len(base) >= 7
        return {p: tuple(v) for p, v in (m.get("tests") or {}).items()} if same else None

    @staticmethod
    def base_path(task_dir):
        """Sidecar with the base commit (Vigia folder): Codex devs launched by the lead get their --protect-base here."""
        return ProtectedTests.path(task_dir)[:-4] + ".base"

    @staticmethod
    def recorded_base(task_dir):
        try:
            with open(ProtectedTests.base_path(task_dir), encoding="utf-8") as f:
                return f.read().strip() or None
        except (OSError, TypeError):
            return None

    @staticmethod
    def from_git(repo, rev):
        """Test files of commit rev, or None when git cannot read it."""
        import subprocess
        try:
            out = git_plain(repo, "ls-tree", "-r", "-z", "--name-only", rev, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return frozenset(p for p in out.stdout.split("\0") if p and TEST_RE.search(p))

    @staticmethod
    def load(task_dir):
        """frozenset of protected repo-relative paths, or None (= protect every test-named path).
        The list always comes from git at the base commit: VIGIA_PROTECT_BASE (vigia-run argv, out of the agent's
        reach) or the base recorded by fleet-run. Without a base, the snapshot file; an empty one is not trusted."""
        base = os.environ.get("VIGIA_PROTECT_BASE") or ProtectedTests.recorded_base(task_dir)
        repo = os.environ.get("VIGIA_CWD")
        if base and repo:
            got = ProtectedTests.from_git(repo, base)   # None (git failed) → fail closed
            if got is None:
                return None
            return got | ProtectedTests.snapshot_for(task_dir, repo, base)
        listed = ProtectedTests.listed(task_dir)
        return listed or None

    @staticmethod
    def listed(task_dir):
        """Paths in the snapshot file written by fleet-run before any agent ran (empty set if none)."""
        p = ProtectedTests.path(task_dir)
        try:
            with open(p, encoding="utf-8", errors="surrogateescape") as f:
                return frozenset(ln.rstrip("\n") for ln in f if ln.strip())
        except (OSError, TypeError):
            return frozenset()

    @staticmethod
    def snapshot_for(task_dir, repo, base):
        """The snapshot list when it was taken at this same base commit (kept outside the repo, so rewriting git
        objects or refs in the repo cannot shrink it); else an empty set."""
        import subprocess
        rec = ProtectedTests.recorded_base(task_dir)
        if not rec:
            return frozenset()
        try:
            same = rec == rev_sha(repo, base)
        except (OSError, subprocess.SubprocessError):
            same = rec.startswith(base) and len(base) >= 7
        return ProtectedTests.listed(task_dir) if same else frozenset()

    @staticmethod
    def blob_id(path, hexlen=40):
        """Git blob id of the raw file bytes, computed here: no git filters/attributes/config from the worktree run."""
        with open(path, "rb") as f:
            return ProtectedTests.blob_id_bytes(f.read(), hexlen)

    @staticmethod
    def same_blob(path, oid):
        """File bytes match the base blob, as checked out (raw) or with CRLF line endings back to LF (autocrlf /
        eol=crlf checkouts). Only line endings are forgiven; no git filter or config is run."""
        with open(path, "rb") as f:
            data = f.read()
        if ProtectedTests.blob_id_bytes(data, len(oid)) == oid:
            return True
        return b"\r\n" in data and ProtectedTests.blob_id_bytes(data.replace(b"\r\n", b"\n"), len(oid)) == oid

    @staticmethod
    def blob_id_bytes(data, hexlen=40):
        import hashlib
        algo = hashlib.sha256 if hexlen == 64 else hashlib.sha1
        return algo(b"blob %d\0" % len(data) + data).hexdigest()

    @staticmethod
    def modified(base, dirs, expected=frozenset(), manifest=None):
        """Problems with the base tests in the git worktrees dirs, as 'dir: path' (changed or deleted) or
        'dir: cannot verify ...'. Compares the raw file bytes with the base blobs (hashed here, no git filters, no
        replace refs), so renames, index flags, clean filters or any shell trick cannot hide a change; symlinks are
        compared by target, a symlinked parent directory or a changed exec bit is reported. Fails closed: anything that cannot be checked is reported. expected = paths that must be in the base (fleet-run's snapshot); one
        missing from the base tree is reported too. manifest = fleet-run's {path: (mode, blob)} taken before launch:
        when given, git objects in the (agent-writable) repo are not consulted at all. vigia-run runs it at run end."""
        import subprocess
        out = []
        for d in dirs:
            try:
                if manifest is not None:
                    blobs = dict(manifest)
                else:
                    ls = git_plain(d, "ls-tree", "-r", "-z", base, timeout=60)
                    if ls.returncode != 0:
                        out.append(f"{d}: cannot verify protected tests (git ls-tree {base[:12]} failed)")
                        continue
                    blobs = {p: tuple(v) for p, v in ProtectedTests.parse_tree(ls.stdout).items()}
                out += [f"{d}: {p} (not in the base tree: cannot verify)" for p in sorted(set(expected) - set(blobs))]
                for p in sorted(blobs):
                    mode, oid = blobs[p]
                    f = os.path.join(d, p)
                    parts = p.split("/")[:-1]
                    if any(os.path.islink(os.path.join(d, *parts[:i])) for i in range(1, len(parts) + 1)):
                        out.append(f"{d}: {p} (parent directory is a symlink)")
                        continue
                    if mode in ("100644", "100755") and os.path.isfile(f) and not os.path.islink(f) \
                            and (mode == "100755") != bool(os.stat(f).st_mode & 0o111):
                        out.append(f"{d}: {p} (mode changed)")
                        continue
                    if mode == "120000":     # symlink: the blob is the link target
                        if not os.path.islink(f):
                            out.append(f"{d}: {p} (deleted)" if not os.path.lexists(f) else f"{d}: {p}")
                        elif ProtectedTests.blob_id_bytes(os.fsencode(os.readlink(f)), len(oid)) != oid:
                            out.append(f"{d}: {p}")
                    elif os.path.islink(f) or not os.path.isfile(f):
                        out.append(f"{d}: {p} (deleted)" if not os.path.lexists(f) else f"{d}: {p}")
                    elif not ProtectedTests.same_blob(f, oid):
                        out.append(f"{d}: {p}")
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                out.append(f"{d}: cannot verify protected tests ({type(e).__name__})")
        return out

    @staticmethod
    def candidates(path, roots, task_dir):
        """Repo-relative forms of path: after T/wt/<worktree>/, or relative to a root. A relative path is resolved
        against the run cwd (first root) first, so ../task-1/tests/x.py is seen as that worktree's file."""
        p = str(path)
        if not os.path.isabs(p):
            if not roots:
                return {os.path.normpath(p)}
            p = os.path.join(roots[0], p)
        p = os.path.abspath(p)
        out = set()
        if task_dir:
            wt = os.path.join(os.path.abspath(task_dir), "wt") + os.sep
            if p.startswith(wt):
                rest = p[len(wt):].split(os.sep, 1)
                if len(rest) == 2:
                    out.add(rest[1])
        for r in roots or []:
            r = os.path.abspath(r)
            if p.startswith(r + os.sep):
                out.add(os.path.relpath(p, r))
        return out

    @staticmethod
    def is_protected(path, roots=None, task_dir=None):
        if not path:
            return False
        if roots and not os.path.isabs(str(path)):
            path = os.path.abspath(os.path.join(roots[0], str(path)))
        if not _under_roots(str(path), roots):
            return False
        listed = ProtectedTests.load(task_dir)
        if listed is None:
            return bool(TEST_RE.search(str(path)))
        return bool(ProtectedTests.candidates(path, roots, task_dir) & listed)


SECRET_RE = re.compile(r"(^|[\s/'\"=<>:])(\.env(\.(?!example\b|sample\b|template\b)[\w.-]+)?|webhook\.env)(?=$|[\s'\";|&)<>])"
                        r"|(^|[\s/'\"=<>:])[.*?\[\]]*\.?e[*?\[\]nv]*[*?\[\]][\w*?\[\].-]*(?=$|[\s'\";|&)<>])"   # globs: .env*, .e?v
                        r"|webhook[*?\[]")


def touches_secret(text):
    """Command/path that names a .env (not .env.example/.sample/.template) or webhook.env file."""
    return bool(SECRET_RE.search(str(text or "")))


class HookPolicy:
    """PreToolUse decisions shared by vigia-claude-hook and vigia-codex-hook (one place for the rules)."""

    EDIT = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
    SHELL = ("Bash", "shell", "local_shell", "exec_command")
    TEST = "Test file protected by Vigia (it exists on the base): do not edit it; add a new test file if needed."
    SECRET = "Secret file (.env / webhook.env) blocked by Vigia: do not read or write it."
    SECRET_SEARCH = ("Search blocked by Vigia: it would read secret files (.env / webhook.env) in this folder. "
                     "Narrow it with a glob/type that excludes them (e.g. glob='*.py' or type='py').")
    SECRET_NAMES = (".env", ".env.local", ".env.production", ".env.dev", "webhook.env")
    SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
             ".next", "target"}
    MAX_ENTRIES = 50000
    # ripgrep types checked to have no glob that matches .env / *.env / webhook.env (`sh`, `config`... do or may)
    SAFE_TYPES = {"py", "python", "js", "ts", "typescript", "go", "rust", "java", "c", "cpp", "md", "markdown",
                  "json", "yaml", "toml", "html", "css", "ruby", "php", "kotlin", "swift", "csharp", "sql", "rst",
                  "txt", "vue", "svelte", "scala", "lua", "elixir", "haskell", "dart"}

    @staticmethod
    def _globs(g):
        """Expand one level of {a,b} braces (ripgrep glob syntax)."""
        m = re.search(r"\{([^{}]*)\}", g)
        if not m:
            return [g]
        return [g[:m.start()] + alt + g[m.end():] for alt in m.group(1).split(",")]

    @staticmethod
    def search_reaches_secret(inp, cwd):
        """Grep whose files could include a secret file: no type/glob that excludes them, and one exists under its
        path (bounded walk; too many files to check counts as yes)."""
        import fnmatch
        t = str(inp.get("type") or "")
        if t in HookPolicy.SAFE_TYPES:
            return False                      # rg --type of a language whose globs never match a .env file
        # any other type (e.g. `sh` includes *.env / .env) is treated like no filter
        g = str(inp.get("glob") or "")
        if g and not g.startswith("!"):
            pats = [x.rsplit("/", 1)[-1] for x in HookPolicy._globs(g)]
            if not any(fnmatch.fnmatch(n, pt) for n in HookPolicy.SECRET_NAMES for pt in pats):
                return False
        root = str(inp.get("path") or cwd or "")
        if not root:
            return True
        root = root if os.path.isabs(root) else os.path.join(cwd or "", root)
        if os.path.isfile(root):
            return touches_secret(root)
        n = 0
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in HookPolicy.SKIP_DIRS]
            n += len(files) + len(dirs)
            if any(touches_secret(f) for f in files) or n > HookPolicy.MAX_ENTRIES:
                return True
        return False


    @staticmethod
    def deny_reason(event, tool, inp, files=(), env=None):
        """Reason to deny this tool call, or None. files = paths an apply_patch touches."""
        if event != "PreToolUse":
            return None
        env = os.environ if env is None else env
        inp = inp if isinstance(inp, dict) else {}
        cmd = str(inp.get("command") or inp.get("cmd") or "") if tool in HookPolicy.SHELL else ""
        paths = [p for p in (inp.get("file_path"), inp.get("notebook_path"), inp.get("path")) if p] + list(files)
        if any(touches_secret(p) for p in paths) or (cmd and touches_secret(cmd)):   # always on, any tool
            return HookPolicy.SECRET
        if tool in ("Grep", "Glob") and touches_secret(inp.get("glob") or ""):
            return HookPolicy.SECRET
        if tool == "Grep" and HookPolicy.search_reaches_secret(inp, env.get("VIGIA_CWD") or os.getcwd()):
            return HookPolicy.SECRET_SEARCH
        if env.get("VIGIA_PROTECT_TESTS") != "1":
            return None
        task_dir = env.get("VIGIA_TASK_DIR") or None
        roots = [x for x in (env.get("VIGIA_CWD"), task_dir) if x] or None
        if tool in HookPolicy.EDIT and any(is_test(p, roots=roots, task_dir=task_dir) for p in paths):
            return HookPolicy.TEST
        if cmd and shell_test_writes(cmd, roots=roots, task_dir=task_dir):
            return HookPolicy.TEST
        return None


def is_test(path, roots=None, task_dir=None):
    """Protected test file? Only inside the cwd/worktree when roots is given; with fleet-run's snapshot
    only the base's existing tests count (new test files are allowed)."""
    return ProtectedTests.is_protected(path, roots, task_dir)


def shell_test_writes(cmd, roots=None, task_dir=None):
    """Heuristic: protected test paths that a shell command appears to write or delete."""
    found = []
    for m in SHELL_WRITE_RE.finditer(cmd or ""):
        target = m.group(1) or m.group(2) or ""
        for tok in target.split():
            tok = tok.strip("'\"")
            if tok.startswith("-") or tok == "/dev/null":
                continue
            if is_test(tok, roots=roots, task_dir=task_dir):
                found.append(tok)
    return found


def normalize(txt, n=400):
    t = str(txt or "").lower()
    t = re.sub(r"0x[0-9a-f]+|[0-9a-f]{8,}", "<h>", t)
    t = re.sub(r"\d+(\.\d+)?", "#", t)
    t = re.sub(r"/tmp/[^\s'\"]+", "<tmp>", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[-n:]


def h(*parts):
    return hashlib.sha1("\x1f".join(map(str, parts)).encode()).hexdigest()[:16]
