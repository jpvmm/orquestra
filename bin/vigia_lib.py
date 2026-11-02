"""Shared library for Vigia (supervisor for headless Claude Code / Codex CLI runs).
Python 3 standard library only. Used by vigia-run, vigia-status, vigia-notify and vigia-stop.

Configuration (environment variables):
  ORQUESTRA_HOME   base folder for Orquestra (default: the install folder created by install.sh,
                   detected via its .orquestra-home marker; otherwise ~/.orquestra)
  VIGIA_BASE       Vigia state folder: registry, event log, webhook config (default: $ORQUESTRA_HOME/vigia)
  VIGIA_MAX_ACTIVE maximum number of concurrently supervised runs (default: 5)
  VIGIA_TZ         IANA time zone for timestamps, e.g. "Europe/Lisbon" (default: system local time)
"""
import json, os, re, fcntl, time, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

BIN = os.path.dirname(os.path.abspath(__file__))


def _home_padrao():
    # an install made by install.sh drops a marker next to bin/, so the installed copy finds its own home
    pai = os.path.dirname(BIN)
    return pai if os.path.exists(os.path.join(pai, ".orquestra-home")) else "~/.orquestra"


HOME = os.path.expanduser(os.environ.get("ORQUESTRA_HOME") or _home_padrao())
BASE = os.path.expanduser(os.environ.get("VIGIA_BASE") or os.path.join(HOME, "vigia"))
ATIVOS = os.path.join(BASE, "active")
EVENTOS = os.path.join(BASE, "events.jsonl")
MAX_ATIVOS = int(os.environ.get("VIGIA_MAX_ACTIVE", "5"))
TZ = ZoneInfo(os.environ["VIGIA_TZ"]) if os.environ.get("VIGIA_TZ") else None
TERMINAIS = ("done", "failed", "stalled", "timeout", "loop", "killed")


def agora():
    """Timezone-aware 'now' (VIGIA_TZ if set, otherwise the system local zone)."""
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


def agora_iso():
    return agora().isoformat(timespec="seconds")


def iso_de(ts):
    if not ts:
        return None
    d = datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts).astimezone()
    return d.isoformat(timespec="seconds")


def escrever_json_atomico(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def ler_json(path):
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


def todos_procs():
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            s = proc_stat(int(d))
            if s:
                out[s["pid"]] = s
    return out


def vivo(pid, start=None):
    """Process exists (not a zombie) and, if given, has the same start time (guards against PID reuse)."""
    if not pid:
        return False
    s = proc_stat(int(pid))
    if not s or s["state"] in ("Z", "X"):
        return False
    return start is None or s["start"] == start


def membros(pgid, raiz_pids=(), procs=None):
    """Live PIDs in process group pgid plus descendants of the root PIDs (even if they left the group)."""
    procs = procs or todos_procs()
    sel = {p for p, s in procs.items() if s["pgid"] == pgid and s["state"] not in ("Z", "X")}
    filhos = {}
    for p, s in procs.items():
        filhos.setdefault(s["ppid"], []).append(p)
    pilha = list(raiz_pids) + list(sel)
    vistos = set()
    while pilha:
        p = pilha.pop()
        if p in vistos:
            continue
        vistos.add(p)
        for c in filhos.get(p, []):
            if procs[c]["state"] not in ("Z", "X"):
                sel.add(c)
            pilha.append(c)
    return sel, procs


# ---------- registry of active runs ----------

class Trava:
    """Exclusive flock on the active-runs registry."""

    def __init__(self):
        os.makedirs(ATIVOS, exist_ok=True)
        self.f = open(os.path.join(ATIVOS, ".lock"), "a+")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def entradas_ativas():
    out = []
    for n in sorted(os.listdir(ATIVOS)):
        if n.endswith(".json"):
            e = ler_json(os.path.join(ATIVOS, n))
            if e:
                e["_file"] = os.path.join(ATIVOS, n)
                out.append(e)
    return out


def limpar_mortos():
    """Remove registry entries whose supervisor died. If the supervised command is still
    alive (supervisor was killed hard), kill its group and mark the status as killed.
    Call while holding Trava. Returns the list of cleaned keys."""
    limpos = []
    for e in entradas_ativas():
        if vivo(e.get("supervisor_pid"), e.get("supervisor_start")):
            continue
        st = ler_json(e.get("status_path", "")) or {}
        if st.get("state") == "running":
            import signal, subprocess, time as _t
            pgid = st.get("pgid")
            restos = membros(pgid, [st["pid"]] if st.get("pid") else [])[0] if pgid else set()
            for alvo in ([("g", pgid)] if pgid else []) + [("p", x) for x in restos]:
                try:
                    (os.killpg if alvo[0] == "g" else os.kill)(alvo[1], signal.SIGKILL)
                except Exception:
                    pass
            _t.sleep(0.5)
            pg = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True) if pgid else None
            st["kill"] = {"targets": sorted(restos), "kill_sent": bool(restos),
                          "pgrep_g": pg.stdout.split() if pg else [], "ok": (pg is None or pg.returncode == 1),
                          "verified_at": agora_iso(), "reason": "supervisor died; registry cleanup"}
            st["state"] = "killed"
            st["ended_at"] = agora_iso()
            st["summary"] = "supervisor died; registry cleanup killed what was left of the group"
            try:
                escrever_json_atomico(e["status_path"], st)
                subprocess.Popen([os.path.join(BIN, "vigia-notify"), e["status_path"]], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
        try:
            os.unlink(e["_file"])
        except FileNotFoundError:
            pass
        limpos.append(e.get("key"))
    return limpos


# ---------- patterns ----------

TESTE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|(^|/)[^/]*_test\.[^/]+$|\.spec\.[^/]+$|\.test\.[^/]+$|(^|/)conftest\.py$"
)
CMD_TESTE_RE = re.compile(
    r"\b(pytest|py\.test|python3? -m (pytest|unittest)|nose2|tox|jest|vitest|mocha|go test|cargo test|"
    r"(npm|pnpm|yarn|bun)( run)? test|make (test|check)|phpunit|rspec|ctest|mvn test|gradle\w* test|deno test)\b"
)
ESCRITA_SHELL_RE = re.compile(
    r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?([^\s;|&'\"]+)|\b(?:sed\s+-i\S*|perl\s+-p?i\S*|rm(?:\s+-\S+)*|mv|cp|truncate(?:\s+-\S+)*|"
    r"git\s+(?:checkout|restore)(?:\s+--)?)\s+((?:[^\s;|&]+\s*)+)"
)
ALERTA_RE = re.compile(
    r"rate.?limit|usage limit|too many requests|\b429\b|quota|authentication_failed|invalid api key|not logged in|"
    r"please run /login|unauthorized|\b401\b|oauth token|login required|credit balance",
    re.I,
)


def eh_teste(path):
    return bool(path) and bool(TESTE_RE.search(str(path)))


def escritas_em_teste_por_shell(cmd):
    """Heuristic: test-file paths that a shell command appears to write or delete."""
    achados = []
    for m in ESCRITA_SHELL_RE.finditer(cmd or ""):
        alvo = m.group(1) or m.group(2) or ""
        for tok in alvo.split():
            tok = tok.strip("'\"")
            if tok.startswith("-") or tok == "/dev/null":
                continue
            if eh_teste(tok):
                achados.append(tok)
    return achados


def normalizar(txt, n=400):
    t = str(txt or "").lower()
    t = re.sub(r"0x[0-9a-f]+|[0-9a-f]{8,}", "<h>", t)
    t = re.sub(r"\d+(\.\d+)?", "#", t)
    t = re.sub(r"/tmp/[^\s'\"]+", "<tmp>", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[-n:]


def h(*partes):
    return hashlib.sha1("\x1f".join(map(str, partes)).encode()).hexdigest()[:16]
