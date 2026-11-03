"""vigia_comms: what Orquestra sends to each agent and what the agent sends back.

- redact(): masks secret-looking strings before anything is written or sent.
- extract_prompt(): reads the prompt, system-prompt additions and model from a claude/codex command line.
- render_prompt_md(): the readable <run>.prompt.md.
- Transcript: the readable <run>.conversation.md (prompt, agent messages, one line per tool call,
  subagent handoffs, final reply) and its structured twin <run>.conversation.json (schema_version 1).
- Notifier: sends vigia notifications in order from a background thread (never blocks the supervisor).

Python 3 standard library only. The file is identical in the live and the open-source copy except LANG.
"""
import json, os, queue, re, shlex, subprocess, threading, time
from datetime import datetime

LANG = os.environ.get("VIGIA_LANG", "en")
PREVIEW = 500

# ---------------------------------------------------------------- redaction

_ENV_NAME = r"(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key|private[_-]?key|key|token|secret|password|passwd|pwd|pass|passphrase|credentials?|auth)"
_ASSIGN_RE = re.compile(r"\b(" + _ENV_NAME + r")(\b[\"']?\s*(?:=|:)\s*)([\"']?)([^\s\"',;]+)", re.I)
_SECRET_RES = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----"),
    (re.compile(r"\b(Bearer)\s+(?:(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}|[A-Za-z0-9._~+/=-]{20,})", re.I), r"\1 ***"),
    (re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"), r"\1***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"\b(xox[abposre]-)[A-Za-z0-9-]{8,}"), r"\1***"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***jwt***"),
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+):[^@\s/]+@", re.I), r"\1:***@"),
]


def _looks_secret(v):
    if v.startswith("***") or v.startswith("$") or v.startswith("<"):
        return False
    if len(v) >= 16 and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", v):
        return True
    return len(v) >= 6 and bool(re.search(r"\d", v)) and bool(re.search(r"[A-Za-z]", v))


def _assign(m):
    name, sep, q, val = m.group(1), m.group(2), m.group(3), m.group(4)
    env_style = name.isupper() and "=" in sep  # FOO_KEY=..., TOKEN=...: always masked
    if val.startswith("***") or not (env_style or _looks_secret(val)):
        return m.group(0)
    return f"{name}{sep}{q}***"


def redact(text):
    """Masks secret-looking substrings with ***. Accepts any value; returns str (or the value untouched if not str)."""
    if not isinstance(text, str) or not text:
        return text
    for rx, rep in _SECRET_RES:
        text = rx.sub(rep, text)
    return _ASSIGN_RE.sub(_assign, text)


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def preview(text, n=PREVIEW):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


# ---------------------------------------------------------------- prompt extraction

AGENTS = ("claude", "codex")
CLAUDE_VALUE_OPTS = {"--output-format", "--input-format", "--model", "--settings", "--append-system-prompt",
                     "--system-prompt", "--system-prompt-file", "--append-system-prompt-file", "--max-turns",
                     "--permission-mode", "--session-id", "--resume", "-r", "--fallback-model", "--agents", "--agent",
                     "--setting-sources", "--permission-prompt-tool", "--max-budget-usd", "--json-schema",
                     "--plugin-dir", "--debug-file", "--max-thinking-tokens", "--effort"}
CLAUDE_VARIADIC_OPTS = {"--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--add-dir",
                        "--mcp-config", "--tools", "--betas", "--file"}
CODEX_VALUE_OPTS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
                    "-i", "--image", "--enable", "--disable", "-a", "--ask-for-approval", "--add-dir",
                    "--remote", "--local-provider", "--color", "-o", "--output-last-message", "--output-schema",
                    "--oss-provider"}
CODEX_INSTR_KEYS = ("developer_instructions", "instructions", "base_instructions", "model_instructions_file",
                    "experimental_instructions_file")


def _agent_of(tok):
    b = os.path.basename(tok)
    for ext in (".js", ".mjs", ".cjs"):
        b = b.removesuffix(ext)
    return b if b in AGENTS else None


def unwrap(cmd):
    """Finds the claude/codex call inside wrappers (env, timeout, nice, sh -c '...'). Returns (agent, argv)."""
    cmd = list(cmd or [])
    for i, t in enumerate(cmd):
        ag = _agent_of(t)
        if ag:
            return ag, cmd[i:]
        if os.path.basename(t) in ("sh", "bash", "zsh", "dash"):
            for j in range(i + 1, len(cmd) - 1):
                if re.fullmatch(r"-[a-z]*c[a-z]*", cmd[j]):
                    try:
                        inner = shlex.split(cmd[j + 1])
                    except ValueError:
                        inner = cmd[j + 1].split()
                    return unwrap(inner)
            break
    return None, cmd


def _read_file(path, cwd, cap=200_000):
    try:
        p = path if os.path.isabs(path) else os.path.join(cwd or ".", path)
        with open(p, errors="replace") as f:
            return f.read(cap)
    except OSError as e:
        return f"[{type(e).__name__}: {path}]"


def _opt(tok):
    """'--model=x' -> ('--model', 'x'); '--model' -> ('--model', None)."""
    if tok.startswith("--") and "=" in tok:
        k, v = tok.split("=", 1)
        return k, v
    return tok, None


def extract_prompt(tool, cmd, cwd=None, prompt_file=None):
    """Returns dict: agent, model, prompt, prompt_source, system (list of [label, text]), reply_file, notes."""
    agent, argv = unwrap(cmd)
    agent = agent or (tool if tool in AGENTS else None)
    out = {"agent": agent, "model": None, "prompt": None, "prompt_source": None, "system": [],
           "reply_file": None, "notes": []}
    if agent == "claude":
        _claude(argv[1:] if argv and _agent_of(argv[0]) else argv, out, cwd)
    elif agent == "codex":
        _codex(argv[1:] if argv and _agent_of(argv[0]) else argv, out, cwd)
    if out["prompt"] is not None:
        out["prompt_source"] = "argv"
    if (out["prompt"] in (None, "-")) and prompt_file:
        out["prompt"] = _read_file(prompt_file, cwd)
        out["prompt_source"] = f"--prompt-file {prompt_file}"
    elif prompt_file:
        out["notes"].append(f"--prompt-file {prompt_file} ignored: the command already has a prompt")
    if out["prompt"] == "-":
        out["prompt"] = None
        out["notes"].append("prompt comes from stdin and no --prompt-file was given")
    return out


def _claude(args, out, cwd):
    after_p, positional, i = None, [], 0
    while i < len(args):
        k, v = _opt(args[i])
        if k in ("-p", "--print"):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is not None and (nxt == "-" or not nxt.startswith("-")):
                after_p = args[i + 1]
                i += 2
                continue
        elif k in CLAUDE_VALUE_OPTS:
            if v is None:
                v = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if k == "--model":
                out["model"] = v
            elif k in ("--system-prompt", "--append-system-prompt"):
                out["system"].append([k, v])
            elif k in ("--system-prompt-file", "--append-system-prompt-file"):
                out["system"].append([f"{k} {v}", _read_file(v, cwd)])
        elif k in CLAUDE_VARIADIC_OPTS:
            if v is None:
                while i + 1 < len(args) and not args[i + 1].startswith("-"):
                    i += 1
        elif not k.startswith("-") or k == "-":
            positional.append(k)
        i += 1
    out["prompt"] = after_p if after_p is not None else (positional[0] if positional else None)


def _codex(args, out, cwd):
    i, sub, positional, last = 0, None, [], False
    while i < len(args):
        k, v = _opt(args[i])
        if k in CODEX_VALUE_OPTS or (k.startswith("-") and v is not None):
            if v is None:
                v = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if k in ("-m", "--model"):
                out["model"] = v
            elif k in ("-c", "--config"):
                key, _, val = v.partition("=")
                key, val = key.strip(), val.strip().strip("'\"")
                if key == "model":
                    out["model"] = out["model"] or val
                elif key in CODEX_INSTR_KEYS:
                    out["system"].append([f"-c {key}", _read_file(val, cwd) if key.endswith("_file") else val])
            elif k in ("-o", "--output-last-message"):
                out["reply_file"] = v if os.path.isabs(v) else os.path.join(cwd or ".", v)
            elif k in ("-p", "--profile"):
                out["notes"].append(f"codex profile: {v}")
            elif k in ("-i", "--image"):
                out["notes"].append(f"image attached: {v}")
        elif k == "--last":
            last = True
        elif k.startswith("-") and k != "-":
            pass
        elif sub is None and k in ("exec", "e"):
            sub = "exec"
        elif sub == "exec" and not positional and k == "resume":
            sub = "resume"
        else:
            positional.append(k)
        i += 1
    if sub == "resume":
        out["notes"].append("codex exec resume")
        positional = positional if last else positional[1:]
    out["prompt"] = positional[0] if positional else None


def rules_files(cwd):
    """Instruction files the agent will load on its own (existing ones only)."""
    found, d = [], os.path.abspath(cwd or ".")
    for _ in range(10):
        for n in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", ".claude"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                found.append(p + ("/" if os.path.isdir(p) else ""))
        if os.path.exists(os.path.join(d, ".git")) or d in ("/", os.path.expanduser("~")):
            break
        d = os.path.dirname(d)
    for p in ("~/.claude/CLAUDE.md", "~/.codex/AGENTS.md"):
        p = os.path.expanduser(p)
        if os.path.exists(p) and p not in found:
            found.append(p + " (global)")
    return found


# ---------------------------------------------------------------- rendering

T = {
    "pt": {"prompt_title": "Prompt do run", "task": "Tarefa", "tool": "Ferramenta", "model": "Modelo",
           "cwd": "Pasta (cwd)", "started": "Iniciado em", "source": "Origem do prompt", "rules": "Arquivos de regras que o agente carrega sozinho",
           "none": "(nenhum)", "unknown": "(não informado)", "sys": "Acréscimos ao system prompt", "full": "Prompt completo (Orquestra → agente)",
           "noprompt": "(não consegui extrair o prompt do comando; passe --prompt-file)", "cmd": "Comando (prompt omitido)",
           "notes": "Observações", "conv_title": "Conversa do run", "fullprompt": "prompt completo",
           "orq": "Orquestra", "sub": "subagente", "final": "resposta final", "tools": "ferramentas",
           "omitted": "entradas antigas omitidas", "err": "erro", "cut": "cortado; completo em"},
    "en": {"prompt_title": "Prompt for run", "task": "Task", "tool": "Tool", "model": "Model",
           "cwd": "Folder (cwd)", "started": "Started at", "source": "Prompt source", "rules": "Rule files the agent loads on its own",
           "none": "(none)", "unknown": "(not given)", "sys": "System prompt additions", "full": "Full prompt (Orquestra → agent)",
           "noprompt": "(could not extract the prompt from the command; pass --prompt-file)", "cmd": "Command (prompt omitted)",
           "notes": "Notes", "conv_title": "Conversation for run", "fullprompt": "full prompt",
           "orq": "Orquestra", "sub": "subagent", "final": "final reply", "tools": "tools",
           "omitted": "older entries omitted", "err": "error", "cut": "cut; full text in"},
}[LANG if LANG in ("pt", "en") else "en"]


def _fence(text):
    run = max([len(m) for m in re.findall(r"~{3,}|`{3,}", text or "")] + [3])
    return "~" * (run + 1)


def _block(text, lang="text"):
    f = _fence(text)
    return f"{f}{lang}\n{text.rstrip()}\n{f}\n"


def render_prompt_md(name, task, tool, info, cwd, cmd, rules, started_at):
    L = T
    prompt = info.get("prompt")
    cmd_show = [("<prompt>" if (prompt and c == prompt) else c) for c in (cmd or [])]
    cmd_show = [(c if len(c) <= 400 else c[:400] + "…") for c in cmd_show]
    lines = [f"# {L['prompt_title']} `{name}`", "",
             f"- **{L['task']}:** {task or L['unknown']}",
             f"- **{L['tool']}:** {tool}" + (f" · **{L['model']}:** {info.get('model')}" if info.get("model") else f" · **{L['model']}:** {L['unknown']}"),
             f"- **{L['cwd']}:** `{cwd}`",
             f"- **{L['started']}:** {started_at}",
             f"- **{L['source']}:** {info.get('prompt_source') or L['unknown']}",
             f"- **{L['rules']}:** " + (", ".join(f"`{r}`" for r in rules) if rules else L["none"]), ""]
    if info.get("notes"):
        lines += [f"**{L['notes']}:** " + "; ".join(info["notes"]), ""]
    if info.get("system"):
        lines += [f"## {L['sys']}", ""]
        for label, txt in info["system"]:
            lines += [f"### `{label}`", "", _block(txt or "")]
    lines += [f"## {L['full']}", "", _block(prompt) if prompt else L["noprompt"] + "\n", f"## {L['cmd']}", "",
              _block(shlex.join(cmd_show), "sh")]
    return redact("\n".join(lines))


def _hm(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S")


def write_atomic(path, text):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def tool_line(name, inp):
    """One line per tool call, no output noise."""
    inp = inp if isinstance(inp, dict) else {}
    for k in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(k):
            v = re.sub(r"\s+", " ", str(inp[k])).strip()
            return f"{name}: {v[:160]}" + ("…" if len(v) > 160 else "")
    return str(name)


class Transcript:
    """Builds <run>.conversation.md. All text is redacted on the way in."""
    MAX_TEXT = 8000
    MAX_ENTRIES = 800

    def __init__(self, path, name, task, tool, model, prompt, prompt_path):
        self.path, self.name, self.task, self.tool, self.model = path, name, task, tool, model
        self.prompt_path = prompt_path
        self.entries = []      # [ts, kind, text, who]
        self.omitted = 0
        self.last_reply = None
        self.final = None
        self.dirty = True
        self._seen_sub = set()
        self.add("prompt", prompt or T["noprompt"])

    def add(self, kind, text, who=None):
        text = redact(str(text or "")).strip()
        if not text:
            return
        if kind == "final":
            self.final = text
            # the final reply usually repeats the last agent message: promote it instead of duplicating
            for e in reversed(self.entries):
                if e[1] == "tool":
                    continue
                if e[1] == "assistant" and e[3] is None and e[2].strip() == text[:self.MAX_TEXT].strip():
                    e[1] = "final"
                    self.last_reply, self.dirty = text, True
                    return
                break
        if kind in ("assistant", "final") and who is None:
            self.last_reply = text
        if len(text) > self.MAX_TEXT:
            text = text[:self.MAX_TEXT] + f"\n… [{T['cut']} {os.path.basename(self.path).replace('.conversation.md', '.events.jsonl')}]"
        self.entries.append([time.time(), kind, text, who])
        if len(self.entries) > self.MAX_ENTRIES:
            del self.entries[1:101]
            self.omitted += 100
        self.dirty = True

    def subagent_call(self, key, agent_type, description, prompt):
        if key in self._seen_sub:
            return
        self._seen_sub.add(key)
        head = f"{agent_type or T['sub']}" + (f" — {description}" if description else "")
        self.add("sub_call", head + ("\n\n" + str(prompt)[:3000] if prompt else ""))

    def render(self):
        L, ag = T, self.tool
        out = [f"# {L['conv_title']} `{self.name}`", "",
               f"**{L['task']}:** {self.task or L['unknown']} · **{L['tool']}:** {ag}"
               + (f" ({self.model})" if self.model else "")
               + (f" · {L['fullprompt']}: `{self.prompt_path}`" if self.prompt_path else ""), ""]
        if self.omitted:
            out += [f"_({self.omitted} {L['omitted']})_", ""]
        tools = []

        def flush_tools():
            if tools:
                out.append(f"**{L['tools']}:**")
                out.extend(f"- {_hm(t)} `{x.replace('`', chr(39))}`" for t, x in tools)
                out.append("")
                tools.clear()
        for ts, kind, text, who in self.entries:
            if kind == "tool":
                tools.append((ts, text if who is None else f"[{who}] {text}"))
                continue
            flush_tools()
            src = who or ag
            if kind == "prompt":
                body = text if len(text) <= 3000 else text[:3000] + f"\n… [{L['cut']} {self.prompt_path}]"
                out += [f"## {_hm(ts)} · {L['orq']} → {ag}", "", _block(body)]
            elif kind == "assistant":
                out += [f"## {_hm(ts)} · {src} → " + (ag if who else L['orq']), "", text, ""]
            elif kind == "final":
                out += [f"## {_hm(ts)} · {ag} → {L['orq']} ({L['final']})", "", text, ""]
            elif kind == "sub_call":
                out += [f"## {_hm(ts)} · {ag} → {L['sub']}: {text.splitlines()[0]}", ""]
                rest = "\n".join(text.splitlines()[1:]).strip()
                out += ([_block(rest)] if rest else [])
            elif kind == "sub_result":
                nm = L['sub'] if not who or who == L['sub'] or who in ("subagente", "subagent") else f"{L['sub']} {who}"
                out += [f"## {_hm(ts)} · {nm} → {ag}", "", text, ""]
            elif kind == "error":
                out += [f"> **{L['err']}** {_hm(ts)}: {text[:600]}", ""]
            else:
                out += [f"_{_hm(ts)} {text}_", ""]
        flush_tools()
        return "\n".join(out)

    def json_path(self):
        base = self.path[:-len(".conversation.md")] if self.path.endswith(".conversation.md") else self.path
        return base + ".conversation.json"

    def to_json(self):
        """Structured twin of conversation.md (same redacted entries) for programmatic readers (dashboards, bots)."""
        iso = lambda ts: datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
        return {"schema_version": 1, "run": self.name, "task": self.task, "tool": self.tool, "model": self.model,
                "prompt_path": self.prompt_path, "omitted": self.omitted, "updated_at": iso(time.time()),
                "entries": [{"ts": iso(ts), "kind": kind, "who": who, "text": text}
                            for ts, kind, text, who in self.entries]}

    def flush(self):
        if not self.dirty:
            return
        try:
            write_atomic(self.path, self.render())
            write_atomic(self.json_path(), json.dumps(self.to_json(), ensure_ascii=False))
            self.dirty = False
        except (OSError, TypeError, ValueError):
            pass


# ---------------------------------------------------------------- notifications

class Notifier:
    """Queues `<script> <status> --event ... --state ... --prev ...` calls and runs them one at a time in a
    daemon thread, so notifications keep their order and never block or crash the supervisor."""

    def __init__(self, script, status_path, timeout=120):
        self.script, self.status_path, self.timeout = script, status_path, timeout
        self.q = queue.Queue()
        self.sent = set()
        threading.Thread(target=self._loop, daemon=True).start()

    def args(self, event, state, prev=None, detail=None):
        a = [self.script, self.status_path, "--event", event, "--state", str(state)]
        if prev:
            a += ["--prev", str(prev)]
        if detail:
            a += ["--detail", redact(str(detail))[:500]]
        return a

    def send(self, event, state, prev=None, detail=None, key=None):
        key = key or f"{event}|{prev}|{state}|{detail or ''}"
        if key in self.sent:
            return False
        self.sent.add(key)
        self.q.put(self.args(event, state, prev, detail))
        return True

    def _loop(self):
        while True:
            a = self.q.get()
            try:
                subprocess.run(a, timeout=self.timeout, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            finally:
                self.q.task_done()

    def drain(self, timeout=60):
        end = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.2)


def status_side_path(status_path, suffix):
    base = status_path[:-len(".status.json")] if status_path.endswith(".status.json") else status_path
    return base + suffix
