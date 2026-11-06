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
    r"please run /login|unauthorized|\b401\b|oauth token|login required|credit balance|"
    r"hit your .+ limit|5-hour limit|resets \d{1,2}:\d{2}\s*(am|pm)?",
    re.I,
)

# Claude CLI subscription cap, e.g.:
# "You've hit your 5-hour limit · resets 11:50pm (America/Sao_Paulo)"
CLAUDE_LIMIT_RE = re.compile(
    r"you'?ve hit your .+ limit|hit your 5-hour limit|5-hour limit\s*[·•].*resets",
    re.I,
)


def detect_claude_limit(text):
    """If text is the Claude CLI usage-limit error, return dict with resets; else None."""
    if not text:
        return None
    m = CLAUDE_LIMIT_RE.search(str(text))
    if not m:
        return None
    resets = None
    rm = re.search(r"resets\s+([^\n(]+)", str(text), re.I)
    if rm:
        resets = rm.group(1).strip(" ·•")
    return {"kind": "claude_usage_limit", "match": m.group(0), "resets": resets}


def limit_fallback_plan(role):
    """What to do when Claude hits the usage limit, by run role.
    review → fall back to Codex; lead/plan/implement → pause with a clear state (no loop)."""
    role = (role or "").lower()
    if role in ("review", "reviewer", "final_reviewer", "security"):
        return {"action": "fallback_tool", "tool": "codex", "state": "limit_fallback",
                "summary": "Claude usage limit: review falls back to Codex"}
    return {"action": "pause", "tool": None, "state": "limit_paused",
            "summary": "Claude usage limit: lead/run paused — do not loop; wait for reset or switch tool"}


def append_usage_progress(task_dir, *, name, tool, model, usage=None, limit_info=None):
    """Append a short usage line to task_dir/progress.md when the file exists."""
    if not task_dir:
        return False
    path = os.path.join(task_dir, "progress.md")
    if not os.path.isfile(path):
        return False
    usage = usage or {}
    parts = [f"- usage {now_iso()}: run={name} tool={tool} model={model or '?'}"]
    bits = []
    for k in ("input_tokens", "output_tokens", "total_tokens", "total_cost_usd", "num_turns", "duration_ms"):
        if usage.get(k) is not None:
            bits.append(f"{k}={usage[k]}")
    if bits:
        parts.append(" ".join(bits))
    if limit_info:
        parts.append(f"LIMIT {limit_info.get('kind')} resets={limit_info.get('resets')}")
        fb = limit_fallback_plan(usage.get("role"))
        parts.append(f"fallback={fb['action']}:{fb.get('tool') or fb['state']}")
    line = " ".join(parts) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False


# agent stderr: this pattern always applies; ALERT_RE only on Claude's stderr (Codex echoes the whole conversation: noise)
STDERR_ALERT_RE = re.compile(r"Background tasks still running", re.I)
LEAD_RE = re.compile(r"fleet-.+-lead-.+")  # fleet lead: must leave T/FINAL-SUMMARY.md


def is_test(path):
    return bool(path) and bool(TEST_RE.search(str(path)))


def shell_test_writes(cmd):
    """Heuristic: test-file paths that a shell command appears to write or delete."""
    found = []
    for m in SHELL_WRITE_RE.finditer(cmd or ""):
        target = m.group(1) or m.group(2) or ""
        for tok in target.split():
            tok = tok.strip("'\"")
            if tok.startswith("-") or tok == "/dev/null":
                continue
            if is_test(tok):
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
