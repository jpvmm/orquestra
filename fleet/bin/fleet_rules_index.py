"""Rules index and deterministic rule comparison for the retro loop (PLAN F1 + F3.1). Python 3 stdlib only.

Sidecar <fleet root>/skill/rules-index.json (bookkeeping, in fleet_skill_lib.RESERVED; IDs never go into Markdown):
  {"version": 1, "rules": {<rule hash>: entry}}
  rule hash = sha256(normalize_rule(rule text))                      (rule_hash)
  entry     = {file, anchor, scope, status, added_ledger_id, source_runs, last_seen_in_retro,
               superseded_by?, ledger_id?, invalidated_at?, others?}
  status    = active | probation | archived | superseded
  scope     = "generic" | "project:<name>";  file = diff path ("skill/...", "learnings.md", "<project>/LESSONS.md")
  anchor    = text of the nearest Markdown heading above the rule ("" when none)
  others    = the same hash at other places (other files/scopes): [{file, anchor, scope, status, ...}], sorted
The Markdown files + the ledgers are the source of truth: rebuild_index() recomputes everything from them and is
idempotent (byte-identical JSON on a second run). Fields that only other steps set are carried over from the
previous index: status "probation" of a live rule, "archived" entries for rules no longer in any file, and
last_seen_in_retro. Superseded history comes from ledger apply entries with relation "supersedes:<id>": only the
rule that relation names (full hash in the entry's relation_hash) becomes status "superseded" (superseded_by = hash
of the first rule the diff added, ledger_id, invalidated_at = entry ts); other rules the same diff changes are not
touched. A reverted apply entry releases the hash.

Pure helpers used by fleet-retro and the judge (T5): normalize_rule, rule_hash, rules_of_file, related_rules,
overlap, polarity/contradicts, near_match, cand_fp, ctx_hash."""
import contextlib, difflib, fcntl, hashlib, json, os, re, stat

import fleet_skill_lib as L

INDEX_NAME = "rules-index.json"
STATUSES = ("active", "probation", "archived", "superseded")
RELATION_RX = re.compile(r"(new|refines:[0-9a-f]{6,64}|supersedes:[0-9a-f]{6,64})")
normalize_rule = L.normalize_rule


def rule_hash(text):
    """sha256 hex of normalize_rule(text): the rule's stable id (prefixes of it are used in relation refs)."""
    return hashlib.sha256(normalize_rule(text).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ rules of files / scopes
def _heading(raw):
    return re.sub(r"^#+\s*", "", raw.strip()).strip()


def anchor_at(text, pos):
    """Heading text of the section containing character offset pos of text ("" before the first heading)."""
    head, fence = "", False
    for line in (text or "")[:max(0, pos)].splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        elif not fence and re.match(r"#{1,6}\s", line.lstrip()):
            head = _heading(line)
    return head


def rules_of_text(text):
    """[{line, raw, norm, hash, anchor}] using the lint's rule splitter (fleet_skill_lib.rules_of_text)."""
    lines = (text or "").splitlines()
    heads, head, fence = {}, "", False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            fence = not fence
        elif not fence and re.match(r"#{1,6}\s", line.lstrip()):
            head = _heading(line)
        heads[i] = head
    return [{"line": i, "raw": raw, "norm": n, "hash": hashlib.sha256(n.encode("utf-8")).hexdigest(),
             "anchor": heads.get(i, "")} for i, raw, n in L.rules_of_text(text or "")]


def _read_nofollow(base, rel):
    """Text of base/rel read without following any symlink below base (directories walked by fd with O_NOFOLLOW,
    like fleet_skill_lib.safe_write). None when missing, symlinked, not a regular file or unreadable."""
    try:
        dfd = L._open_dir(base, os.path.dirname(rel), create=False)
    except OSError:
        return None
    try:
        fd = os.open(os.path.basename(rel), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def rules_of_file(path, base=None):
    """rules_of_text() of a file ([] when missing, symlinked or unreadable). With base (a directory above path), no
    path component below base may be a symlink; without it, only the file's own directory and the file are checked."""
    path = os.path.abspath(path)
    base = os.path.abspath(base) if base else os.path.dirname(path)
    rel = os.path.relpath(path, base)
    if rel.startswith(".." + os.sep) or rel in (".", "..") or os.path.isabs(rel):
        return []
    text = _read_nofollow(base, rel)
    return [] if text is None else rules_of_text(text)


_SKIP_GENERIC = ("skill/archive/", "skill/proposals/", "skill/retro-batches/", "skill/ledger-backups/", "skill/evals/")


def scope_files(scope, root=None):
    """[(diff path, absolute path)] of the rule files of a scope, sorted. generic: skill/**/*.md and
    .claude/agents/*.md under the fleet root (bookkeeping excluded) + the Orquestra lessons file;
    project:<p>: <p>/LESSONS.md."""
    return [(rel, path) for rel, path, _ in _scope_files_based(scope, root)]


def _scope_files_based(scope, root=None):
    """scope_files() with the base each diff path is relative to: [(diff path, absolute path, base)], sorted."""
    root = root or L.fleet_root()
    out = []
    if scope == "generic":
        for top in ("skill", os.path.join(".claude", "agents")):
            base = os.path.join(root, top)
            for d, dirs, files in os.walk(base):
                dirs.sort()
                for f in sorted(files):
                    if not f.endswith(".md"):
                        continue
                    rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                    if rel.startswith(_SKIP_GENERIC) or not L.safe_rel(rel) or rel.endswith("lint-report.md"):
                        continue
                    out.append((rel, os.path.join(root, rel), root))
        out.append((L.lessons_name(), os.path.join(L.orq_root(), L.lessons_name()), L.orq_root()))
    elif scope.startswith("project:"):
        p = scope[len("project:"):]
        out.append((f"{p}/{L.LESSONS_FILE}", os.path.join(L.orq_root(), p, L.LESSONS_FILE), L.orq_root()))
    return sorted(out)


def scope_rules(scope, root=None):
    """Rules currently in the files of a scope: [{line, raw, norm, hash, anchor, file, scope}]."""
    out = []
    for rel, path, base in _scope_files_based(scope, root):
        for r in rules_of_file(path, base):
            out.append(dict(r, file=rel, scope=scope))
    return out


def scope_obj(scope):
    """fleet_skill_lib.Scope (ledger location) for a scope label."""
    return L.generic_scope() if scope == "generic" else L.project_scope(scope[len("project:"):])


def known_scopes():
    """generic + every project that has a LESSONS.md or a lessons ledger, sorted."""
    names = set(L.project_lessons_files())
    try:
        for n in os.listdir(L.orq_root()):
            if L.valid_project(n) and os.path.isfile(os.path.join(L.orq_root(), n, "lessons-ledger.jsonl")):
                names.add(n)
    except OSError:
        pass
    return ["generic"] + [f"project:{n}" for n in sorted(names)]


# ------------------------------------------------------------------ history from the ledgers
def _diff_sections(diff_text):
    """{path: (removed lines, added lines)} from a unified diff (content lines without the +/- marker)."""
    out, cur = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].split("\t")[0].strip()
            cur = p[2:] if p.startswith(("a/", "b/")) else p
            out.setdefault(cur, ([], []))
        elif line.startswith("--- ") or line.startswith("diff --git ") or line.startswith("@@"):
            continue
        elif cur is not None and line.startswith("-"):
            out[cur][0].append(line[1:])
        elif cur is not None and line.startswith("+"):
            out[cur][1].append(line[1:])
    return out


def _rule_hashes(lines):
    return [r["hash"] for r in rules_of_text("\n".join(lines))]


LEDGER_ID_RX = re.compile(r"[1-9][0-9]{0,9}")   # fleet_skill_lib.next_id format


def removed_rule_hashes(diff_text):
    """{hash: path} of the rules a unified diff removes (removed lines whose rule is not re-added in the same file)."""
    out = {}
    for path, (rem, add) in sorted(_diff_sections(diff_text).items()):
        add_h = set(_rule_hashes(add))
        for h in _rule_hashes(rem):
            if h not in add_h:
                out.setdefault(h, path)
    return out


def resolve_relation_hash(relation, diff_text):
    """Full hash of the rule a refines/supersedes relation names, resolved against the rules the diff removes.
    → (hash, None) | (None, None) for "new"/missing | (None, reason) when the prefix matches no removed rule or
    several."""
    rel = parse_relation(relation)
    if rel is None:
        return None, "invalid relation"
    kind, ref = rel
    if kind == "new":
        return None, None
    hits = sorted(h for h in removed_rule_hashes(diff_text) if h.startswith(ref))
    if len(hits) != 1:
        return None, (f"relation {kind}:{ref} " + ("is ambiguous among the rules the diff removes" if hits
                                                   else "matches no rule the diff removes or changes"))
    return hits[0], None


def ledger_history(scope):
    """(added {hash: [(ledger id, run)]}, superseded {hash: {file, superseded_by, ledger_id, invalidated_at}})
    from the non-reverted apply entries of a scope's ledger and their backed-up change.diff. Only the rule a
    "supersedes" entry names becomes superseded: its full hash from the entry's relation_hash (written by
    fleet-skill-apply), else its relation prefix resolved against the rules the diff removes (must be unique).
    Entries whose id is not a ledger id (digits) are ignored (never joined into a path)."""
    sc = scope_obj(scope)
    led = L.read_ledger(sc)
    reverted = {str(e.get("reverts")) for e in led if e.get("op") == "revert"}
    added, superseded = {}, {}
    for e in led:
        lid = str(e.get("id"))
        if e.get("op") != "apply" or lid in reverted or not LEDGER_ID_RX.fullmatch(lid):
            continue
        try:
            dfd = L._open_dir(sc.anchor, os.path.join(sc.rel(sc.backups), lid), create=False)
        except OSError:
            continue
        try:
            fd = os.open("change.diff", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError:
            continue
        finally:
            os.close(dfd)
        with os.fdopen(fd, "rb") as f:
            diff_text = f.read().decode("utf-8", errors="replace")
        secs = _diff_sections(diff_text)
        rel = parse_relation(e.get("relation")) or ("new", None)
        target = None
        if rel[0] == "supersedes":
            full = str(e.get("relation_hash") or "")
            target = full if re.fullmatch(r"[0-9a-f]{64}", full) else resolve_relation_hash(e.get("relation"),
                                                                                          diff_text)[0]
        for path, (rem, add) in sorted(secs.items()):
            add_h, rem_h = _rule_hashes(add), _rule_hashes(rem)
            for h in add_h:
                added.setdefault(h, []).append((lid, e.get("run")))
            if target and target in rem_h and target not in add_h:
                new_h = [h for h in add_h if h not in rem_h]
                superseded[target] = {"file": path, "superseded_by": (new_h or add_h or [None])[0], "ledger_id": lid,
                                      "invalidated_at": e.get("ts")}
            for h in add_h:   # re-added later by a non-reverted apply: no longer superseded
                if h in superseded and superseded[h]["ledger_id"] != lid:
                    superseded.pop(h)
    return added, superseded


# ------------------------------------------------------------------ build / load / save
def index_path(root=None):
    return os.path.join(L.skill_dir(root), INDEX_NAME)


def load_index(root=None):
    """The saved index ({"version": 1, "rules": {}} when missing or unreadable)."""
    p = index_path(root)
    try:
        if not os.path.islink(p):
            with open(p, encoding="utf-8") as f:
                idx = json.load(f)
            if isinstance(idx, dict) and isinstance(idx.get("rules"), dict):
                return idx
    except (OSError, ValueError):
        pass
    return {"version": 1, "rules": {}}


@contextlib.contextmanager
def index_lock(root=None):
    """Exclusive flock on skill/rules-index.json.lock (no symlink followed)."""
    root = root or L.fleet_root()
    dfd = L._open_dir(root, "skill", create=True)
    try:
        fd = os.open(INDEX_NAME + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def dump_index(idx):
    """Canonical JSON text of an index (sorted keys: same content → same bytes)."""
    return json.dumps(idx, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write(idx, root):
    root = root or L.fleet_root()
    L.safe_write(root, os.path.join("skill", INDEX_NAME), dump_index(idx))


def save_index(idx, root=None):
    """Atomic write of the index under its lock."""
    with index_lock(root):
        _write(idx, root)


def _places(idx):
    """[(hash, place dict)] for every primary entry and every `others` item of an index."""
    out = []
    for h, e in idx.get("rules", {}).items():
        out.append((h, {k: v for k, v in e.items() if k != "others"}))
        out += [(h, dict(o)) for o in e.get("others") or []]
    return out


def _place_key(p):
    order = {"active": 0, "probation": 0, "superseded": 1, "archived": 2}
    return (order.get(p.get("status"), 3), p.get("scope", ""), p.get("file", ""), p.get("anchor", ""))


def build_index(scopes=None, root=None, previous=None):
    """The index computed from the current files and ledgers (pure: writes nothing). scopes=None → known_scopes();
    with a subset, the places of other scopes are kept from `previous` unchanged."""
    root = root or L.fleet_root()
    prev = previous if previous is not None else load_index(root)
    scopes = list(scopes) if scopes else known_scopes()
    old = {}
    for h, p in _places(prev):
        old.setdefault((h, p.get("scope"), p.get("file")), p)
        old.setdefault((h, p.get("scope"), None), p)
    places = [(h, p) for h, p in _places(prev) if p.get("scope") not in scopes]
    for scope in scopes:
        added, superseded = ledger_history(scope)
        live = set()
        for r in scope_rules(scope, root):
            key = (r["hash"], r["file"])
            if key in live:
                continue
            live.add(key)
            was = old.get((r["hash"], scope, r["file"])) or {}
            runs = sorted({str(run) for _, run in added.get(r["hash"], []) if run})
            ids = [lid for lid, _ in added.get(r["hash"], [])]
            places.append((r["hash"], {
                "file": r["file"], "anchor": r["anchor"], "scope": scope,
                "status": "probation" if was.get("status") == "probation" else "active",
                "added_ledger_id": ids[-1] if ids else None, "source_runs": runs,
                "last_seen_in_retro": was.get("last_seen_in_retro")}))
        live_h = {h for h, _ in live}
        for h, s in sorted(superseded.items()):
            if h in live_h:
                continue
            was = old.get((h, scope, s["file"])) or old.get((h, scope, None)) or {}
            places.append((h, {"file": s["file"], "anchor": was.get("anchor", ""), "scope": scope,
                               "status": "superseded", "added_ledger_id": was.get("added_ledger_id"),
                               "source_runs": was.get("source_runs") or [],
                               "last_seen_in_retro": was.get("last_seen_in_retro"),
                               "superseded_by": s["superseded_by"], "ledger_id": s["ledger_id"],
                               "invalidated_at": s["invalidated_at"]}))
        done = live_h | set(superseded)
        for h, p in _places(prev):   # archived rules are history kept by the archive step
            if p.get("scope") == scope and p.get("status") == "archived" and h not in done:
                places.append((h, p))
    return _regroup(places)


def _regroup(places):
    """Index dict from [(hash, place)]: the first place of a hash (sorted) is the entry, the rest are its `others`."""
    rules = {}
    for h, p in sorted(places, key=lambda hp: (hp[0], _place_key(hp[1]))):
        if h not in rules:
            rules[h] = dict(p)
        else:
            rules[h].setdefault("others", []).append(p)
    return {"version": 1, "rules": rules}


def rebuild_index(scopes=None, root=None):
    """Recompute the index from files + ledgers and save it (atomic, under the lock). Idempotent."""
    with index_lock(root):
        idx = build_index(scopes, root, load_index(root))
        _write(idx, root)
    return idx


def mark_archived(scope, block_text, file, anchor, ledger_id, archive_file, source_runs=(), root=None):
    """Record the rules of an archived block with status "archived" (call after rebuild_index: the rules left the
    files). Each place keeps file, anchor, archived_ledger_id and archive_file (relative to the scope's anchor dir);
    a later revert + rebuild_index turns the rule back to "active". Idempotent. → the hashes recorded."""
    rules = rules_of_text(block_text) or [{"hash": rule_hash(block_text)}]
    done = []
    with index_lock(root):
        idx = load_index(root)
        places = _places(idx)
        for r in rules:
            h = r["hash"]
            if any(x == h and p.get("scope") == scope and p.get("file") == file and p.get("status") == "archived"
                   and p.get("archived_ledger_id") == ledger_id for x, p in places):
                continue
            was = next((p for x, p in places if x == h and p.get("scope") == scope), {})
            places.append((h, {"file": file, "anchor": anchor, "scope": scope, "status": "archived",
                               "added_ledger_id": was.get("added_ledger_id"), "source_runs": sorted(set(source_runs)),
                               "last_seen_in_retro": was.get("last_seen_in_retro"),
                               "archived_ledger_id": ledger_id, "archive_file": archive_file}))
            done.append(h)
        _write(_regroup(places), root)
    return done


def set_status(h, status, root=None, scope=None, **fields):
    """Set status (+ extra fields) of rule hash h (the place in `scope` when given) and save. False if unknown."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with index_lock(root):
        idx = load_index(root)
        e = idx["rules"].get(h)
        if e is None:
            return False
        target = e if scope in (None, e.get("scope")) else next(
            (o for o in e.get("others") or [] if o.get("scope") == scope), None)
        if target is None:
            return False
        target.update(fields, status=status)
        _write(idx, root)
    return True


def superseded_hashes(scope, root=None):
    """{hash: info} superseded in a scope, computed from the ledger (not from the saved file) and minus live rules."""
    live = {r["hash"] for r in scope_rules(scope, root)}
    return {h: s for h, s in ledger_history(scope)[1].items() if h not in live}


def is_superseded(rule_text, scope, root=None):
    """True when the exact normalized rule is superseded in that scope (ledger is the source of truth)."""
    return rule_hash(rule_text) in superseded_hashes(scope, root)


# ------------------------------------------------------------------ comparison
_STOP = set("""a an the and or of to in on for with by is are be been it its this that these those as at from you your
any every each all into than then so if when do does did not no never always also only just own same such there their
them they we our us will would should must can could may might has have had was were being about via per over under
o os um uma uns umas de da do das dos e em no na nos nas para por com que se ou ao aos à às é ser sempre nunca não nao
jamais evite evitar avoid don dont t before after antes depois except unless exceto salvo menos""".split())
_NEG_RX = re.compile(r"\b(never|nunca|jamais|avoid|evite|evitar|don t|dont|do not|does not|did not|must not|mustn t|"
                     r"should not|shouldn t|cannot|can t|não|nao|no longer)\b")
_BEFORE_RX = re.compile(r"\b(before|antes)\b")
_AFTER_RX = re.compile(r"\b(after|depois)\b")
_EXCEPT_RX = re.compile(r"\b(except|unless|exceto|salvo|a menos que|only in|somente em|apenas em)\b")
_TICK_RX = re.compile(r"`([^`]+)`")
_NUM_RX = re.compile(r"\d+(?:[.,]\d+)?")


def _stem(w):
    for suf in ("ing", "ed", "es", "s", "e"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content_tokens(text):
    """Stemmed content words of a rule (stopwords and polarity/order/exception words removed)."""
    return {_stem(w) for w in re.findall(r"\w+", normalize_rule(text)) if w not in _STOP and len(w) > 1}


def overlap(a, b):
    """Token-set overlap (Jaccard of content_tokens) of two rule texts, 0..1. Polarity words do not count, so
    'always X' vs 'never X' overlap fully (a hidden conflict), and a paraphrase overlaps highly."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def polarity(text):
    """'neg' when the rule forbids (never/não/don't/avoid/must not ...), else 'pos'."""
    return "neg" if _NEG_RX.search(normalize_rule(text)) else "pos"


def order_markers(text):
    """frozenset of 'before'/'after' markers (en/pt) in the rule."""
    n = normalize_rule(text)
    return frozenset(m for m, rx in (("before", _BEFORE_RX), ("after", _AFTER_RX)) if rx.search(n))


def has_exception(text):
    """True for a scoped exception clause (except/unless/exceto/salvo/a menos que/only in ...)."""
    return bool(_EXCEPT_RX.search(normalize_rule(text)))


def literals(text):
    return frozenset(x.strip() for x in _TICK_RX.findall(text or ""))


def numbers(text):
    return frozenset(_NUM_RX.findall(_TICK_RX.sub(" ", text or "")))


def contradicts(a, b, policy=None):
    """(bool, reason): same subject (overlap >= judge_memory.near_jaccard) with a polarity flip or a before/after flip.
    A rule carrying a scoped exception clause is a narrowing, not a contradiction."""
    pol = policy or L.load_policy()
    ov = overlap(a, b)
    if ov < pol["judge_memory"]["near_jaccard"]:
        return False, f"different subject (overlap {ov:.2f})"
    if has_exception(a) or has_exception(b):
        return False, "scoped exception"
    if polarity(a) != polarity(b):
        return True, f"polarity flip on the same subject (overlap {ov:.2f})"
    oa, ob = order_markers(a), order_markers(b)
    if oa and ob and oa != ob:
        return True, f"before/after flip on the same subject (overlap {ov:.2f})"
    return False, "compatible"


def _edit_new(c):
    e = c.get("edit") if isinstance(c.get("edit"), dict) else {}
    return str(e.get("new", "") or "")


def near_match(a, b, policy=None):
    """(bool, reason) for two candidates {scope, target, anchor, rule, edit: {new}} (judge memory rule 2): same
    scope+target+anchor; on `rule` and on `edit.new`: token Jaccard >= near_jaccard then difflib >= near_ratio; AND
    same polarity, same before/after markers, same backtick literals and same numbers — each guard checked
    independently on `rule` and on `edit.new` (a difference in either field → not a near-match)."""
    jm = (policy or L.load_policy())["judge_memory"]
    for k in ("scope", "target", "anchor"):
        if str(a.get(k, "") or "").strip() != str(b.get(k, "") or "").strip():
            return False, f"different {k}"
    fields = (("rule", str(a.get("rule", "") or ""), str(b.get("rule", "") or "")),
              ("edit.new", _edit_new(a), _edit_new(b)))
    for field, ta, tb in fields:
        na, nb = normalize_rule(ta), normalize_rule(tb)
        if na == nb:
            continue
        r = L.near_dup_ratio(na, nb, jm["near_jaccard"])
        if r is None:
            return False, f"{field}: token Jaccard below {jm['near_jaccard']}"
        if r < jm["near_ratio"]:
            return False, f"{field}: difflib {r:.2f} below {jm['near_ratio']}"
    for _, ta, tb in fields:   # guards on rule AND on edit.new, independently
        if polarity(ta) != polarity(tb):
            return False, "different polarity"
        if order_markers(ta) != order_markers(tb):
            return False, "before/after flip"
        if literals(ta) != literals(tb):
            return False, "different backtick literals"
        if numbers(ta) != numbers(tb):
            return False, "different numbers"
    return True, "near-identical (same scope/target/anchor, polarity, literals and numbers)"


def related_rules(candidate, scope, k=None, root=None, rules=None, policy=None):
    """Deterministic pre-filter: the <= k (policy index.related_max, never more than 8) existing rules most related to
    `candidate` (text, or a dict with rule/edit.new). Score = content-token overlap + difflib ratio of the normalized
    texts; only rules sharing at least one content token; ties broken by hash. A project scope also sees the generic
    rules (they apply to every project). `rules` overrides the rules read from the files (tests, callers with a cache).
    → [{hash, text, file, anchor, scope, score}]"""
    pol = policy or L.load_policy(root)
    k = min(8, int(pol["index"]["related_max"] if k is None else k))
    text = candidate if isinstance(candidate, str) else (str(candidate.get("rule", "")) + "\n" + _edit_new(candidate))
    if rules is None:
        rules = scope_rules(scope, root) + (scope_rules("generic", root) if scope != "generic" else [])
    tc, nc = content_tokens(text), normalize_rule(text)
    best = {}
    for r in rules:
        tr = content_tokens(r["raw"])
        if not tc or not (tc & tr):
            continue
        s = round(len(tc & tr) / len(tc | tr) + difflib.SequenceMatcher(None, nc, r["norm"]).ratio(), 6)
        if r["hash"] not in best or s > best[r["hash"]]["score"]:
            best[r["hash"]] = {"hash": r["hash"], "text": r["raw"], "file": r["file"], "anchor": r.get("anchor", ""),
                               "scope": r.get("scope", scope), "score": s}
    return sorted(best.values(), key=lambda x: (-x["score"], x["hash"]))[:max(0, k)]


def _canon(x):
    return str(x if x is not None else "").strip()


def cand_fp(op, scope, target, anchor, old, new, drops=()):
    """sha256 over a canonical JSON join of the candidate. Normalization is conservative: only outer whitespace of
    each field is stripped (case, punctuation, commands and inner code whitespace are kept); drops are sorted."""
    body = [_canon(op), _canon(scope), _canon(target), _canon(anchor), _canon(old), _canon(new),
            sorted(_canon(d) for d in (drops or ()))]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _related_key(r):
    """(scope, rule hash) of one related rule: a related_rules() dict, a (scope, hash-or-text) pair, or a bare hash /
    rule text (scope "")."""
    if isinstance(r, dict):
        scope, h = r.get("scope", ""), r.get("hash") or rule_hash(str(r.get("text", "")))
    elif isinstance(r, (tuple, list)) and len(r) == 2:
        scope, h = r
    else:
        scope, h = "", r
    h = str(h)
    return _canon(scope), (h if re.fullmatch(r"[0-9a-f]{64}", h) else rule_hash(h))


def ctx_hash(related_rules_sent, target, anchor, block_text, policy_v):
    """sha256 of the judging context: the set of related rules sent, identified by sorted (scope, rule hash) pairs
    (related_rules() dicts, (scope, hash) pairs, or bare hashes/texts with scope ""), target, anchor, the target
    block text and the policy version. The same rule in another scope is another context."""
    pairs = sorted({_related_key(r) for r in related_rules_sent or ()})
    body = [[list(x) for x in pairs], _canon(target), _canon(anchor), _canon(block_text), _canon(policy_v)]
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_relation(value):
    """(kind, ref) from a proposal's relation: ('new', None) | ('refines', prefix) | ('supersedes', prefix);
    None when invalid. A missing/empty relation means 'new' (backwards compatible with older answers)."""
    v = str(value or "new").strip().lower()
    if not RELATION_RX.fullmatch(v):
        return None
    kind, _, ref = v.partition(":")
    return kind, (ref or None)


def resolve_ref(prefix, rules):
    """Rules (from scope_rules) whose hash starts with prefix, de-duplicated by hash: [] unknown, >1 ambiguous."""
    seen = {}
    for r in rules:
        if r["hash"].startswith(prefix):
            seen.setdefault(r["hash"], r)
    return list(seen.values())
