#!/usr/bin/env python3
"""Loop integration tests (T8): the pieces of the self-improvement loop working TOGETHER, plus the shadow-cycle harness.
Fake VIGIA_BIN (a stub vigia-run writing a status.json and a stream-json events file with a canned `result` and
`usage`), temporary roots only, no network, no real model.
Run: python3 tests/test_loop_integration.py   or   pytest -q tests/test_loop_integration.py"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

warnings.simplefilter("ignore", ResourceWarning)
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "fleet", "bin")
sys.path.insert(0, BIN)
import fleet_eval_lib as E  # noqa: E402
import fleet_judge_lib as J  # noqa: E402
import fleet_probation as P  # noqa: E402
import fleet_rules_index as R  # noqa: E402
import fleet_skill_lib as L  # noqa: E402

CYCLE = os.path.join(BIN, "fleet-retro-cycle")
APPLY = os.path.join(BIN, "fleet-skill-apply")
SHADOW = os.path.join(BIN, "fleet-retro-shadow")
SKILL_REL = "skill/fleet-sdd/SKILL.md"
REF_REL = "skill/fleet-sdd/references/tests.md"
SKILL = (
    "# fleet-sdd\n\n## Rules\n- Never touch a test without saying so in the report.\n"
)
REF = "# Tests\n- Keep fixtures small and local to the test.\n"
DAY = 86400.0
W = 48 * 3600.0  # policy cycle.due_hours = probation window
CANARY = (
    "CANARY-" + "d34db33f" * 4
)  # obviously fake secret used to prove nothing sensitive is copied
ENV_KEYS = (
    "FLEET_ROOT",
    "ORQ_ROOT",
    "ORQUESTRA_HOME",
    "FLEET_RETRO_POLICY",
    "FLEET_LESSONS_NAME",
    "FLEET_LESSONS",
    "VIGIA_ATIVOS",
    "VIGIA_BASE",
    "VIGIA_BIN",
    "VIGIA_WEBHOOK_ENV",
)

FAKE_VIGIA = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
td = a[a.index("--task-dir") + 1]
name = a[a.index("--name") + 1]
cmd = a[a.index("--") + 1:]
prompt = cmd[cmd.index("-p") + 1]
judge = cmd[cmd.index("--tools") + 1] == ""
kind = "judge" if judge else "gen"
d = os.environ["FAKE_DIR"]
cp = os.path.join(d, "calls-" + kind)
n = (int(open(cp).read()) if os.path.exists(cp) else 0) + 1
open(cp, "w").write(str(n))
open(os.path.join(d, "prompt-%s-%d.txt" % (kind, n)), "w").write(prompt)
ap = os.path.join(d, "answer-%s-%d.txt" % (kind, n))
if not os.path.exists(ap):
    ap = os.path.join(d, "answer-" + kind + ".txt")
answer = open(ap).read()
up = os.path.join(d, "usage-" + kind + ".json")
usage = json.load(open(up)) if os.path.exists(up) else {"input_tokens": 1000, "output_tokens": 200}
runs = os.path.join(td, "runs")
os.makedirs(runs, exist_ok=True)
sp, ep = os.path.join(runs, name + ".status.json"), os.path.join(runs, name + ".events.jsonl")
with open(ep, "w") as f:
    tools = [] if judge else ["Read", "Grep", "Glob"]
    f.write(json.dumps({"type": "system", "subtype": "init", "tools": tools}) + "\n")
    f.write(json.dumps({"type": "result", "subtype": "success", "result": answer, "usage": usage}) + "\n")
with open(sp, "w") as f:
    json.dump({"name": name, "state": "done"}, f)
print(json.dumps({"name": name, "status_path": sp, "events_path": ep}))
"""


def load_mod(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


def rd(p):
    with open(p) as f:
        return f.read()


def rb(p):
    with open(p, "rb") as f:
        return f.read()


def fence(obj):
    return "Here it is.\n```json\n" + json.dumps(obj) + "\n```\n"


def judgment(key="c1", cur=False, new=True):
    return {
        "candidate": key,
        "relation": "compatible",
        "failure_probe": {
            "with_current_rules": {"answer": "merges", "fixed": cur},
            "with_candidate": {"answer": "reviews first", "fixed": new},
        },
        "success_probes": [],
        "consistent_with": None,
        "changed_because": None,
        "reason": "canned",
    }


def generic_proposal(rule, new, situation, target=SKILL_REL):
    return {
        "scope": "generic",
        "rule": rule,
        "why": "unreviewed waves hide regressions until the final integration",
        "skill_step": "Rules",
        "target": target,
        "relation": "new",
        "failure": {
            "situation": situation,
            "expected": "dispatch the reviewer on the wave before merging",
            "forbidden": "",
            "source_run": "fleet-t1-lead-010101",
        },
        "edit": {"old": "", "new": new},
    }


GENERIC = generic_proposal(
    "Run the reviewer before merging a wave",
    "- Run the reviewer on every wave before merging it; unreviewed waves hide regressions.",
    "the lead merged wave 2 without dispatching the reviewer and a regression reached main",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="fleet-integ-")
        self.root = os.path.join(self.tmp, "fleet")
        self.skill = os.path.join(self.root, SKILL_REL)
        write(self.skill, SKILL)
        write(os.path.join(self.root, REF_REL), REF)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        write(os.path.join(self.fakebin, "vigia-run"), FAKE_VIGIA)
        os.chmod(os.path.join(self.fakebin, "vigia-run"), 0o755)
        self.fdir = os.path.join(self.tmp, "fake")
        os.makedirs(self.fdir)
        self.ativos = os.path.join(self.tmp, "ativos")
        os.makedirs(self.ativos)
        write(os.path.join(self.tmp, "learnings.md"), "# Lessons\n- Short file.\n")
        os.environ.update(
            FLEET_ROOT=self.root,
            ORQ_ROOT=self.tmp,
            VIGIA_ATIVOS=self.ativos,
            VIGIA_BIN=self.fakebin,
            FAKE_DIR=self.fdir,
            FLEET_LESSONS=os.path.join(self.tmp, "learnings.md"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.answer("gen", {"verdict": "nothing_to_save", "proposals": []})
        self.n = 0

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("FAKE_DIR", None)
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def answer(self, kind, obj, n=None):
        write(
            os.path.join(self.fdir, f"answer-{kind}{'-' + str(n) if n else ''}.txt"),
            fence(obj),
        )

    def calls(self, kind=None):
        def one(k):
            p = os.path.join(self.fdir, "calls-" + k)
            return int(rd(p)) if os.path.exists(p) else 0

        return one(kind) if kind else one("gen") + one("judge")

    def cycle(self, *args):
        return subprocess.run(
            [CYCLE, "--force"] + list(args),
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )

    def cli(self, args):
        return subprocess.run(
            args, capture_output=True, text=True, env=dict(os.environ)
        )

    def mk(self, project="acme", task="t1", ended=None):
        T = os.path.join(self.tmp, project, task)
        write(os.path.join(T, "FINAL-SUMMARY.md"), f"status: DONE\nsummary: {task}\n")
        write(os.path.join(T, "progress.md"), "# Progress\nwave 2 merged unreviewed\n")
        write(
            os.path.join(T, "criteria.json"),
            json.dumps({"criteria": [{"id": "C1", "passes": True}]}),
        )
        write(
            os.path.join(T, "runs", f"fleet-{task}-lead-010101.status.json"),
            json.dumps(
                {
                    "state": "done",
                    "ended_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime(ended or time.time())
                    ),
                }
            ),
        )
        return T

    def add_run(self, project, task, name, ended, state="done"):
        T = os.path.join(self.tmp, project, task)
        write(
            os.path.join(T, "runs", name + ".status.json"),
            json.dumps({"name": name, "state": state, "ended_at": P._iso(ended)}),
        )
        write(os.path.join(T, "progress.md"), "# Progress\n")
        return T

    def runs(self, start, offsets, state="done", tag="t", project="alpha"):
        for i, h in enumerate(offsets):
            self.add_run(
                project,
                f"{tag}{i}-{int(h * 10)}",
                f"fleet-{tag}{i}-lead-{int(h * 10)}",
                start + h * 3600,
                state,
            )

    def diff_add(self, rel, line):
        old = rd(os.path.join(self.root, rel))
        self.n += 1
        p = os.path.join(self.tmp, f"c{self.n}.diff")
        write(p, L.unified_diff(rel, old, old + line + "\n"))
        return p

    def apply(self, rel, line, *extra):
        r = self.cli(
            [
                APPLY,
                self.diff_add(rel, line),
                "--run",
                "fleet-x-lead-0",
                "--reason",
                "t",
            ]
            + list(extra)
        )
        out = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        return r, out

    def ledger(self):
        return L.read_ledger(L.generic_scope())

    def ops(self, op):
        return [e for e in self.ledger() if e.get("op") == op]

    def put_auto_revert(self, t, fp, **kw):
        e = dict(
            {
                "id": L.next_id(),
                "ts": P._iso(t),
                "op": "auto-revert",
                "scope": "generic",
                "reverted": "1",
                "cand_fp": fp,
            },
            **kw,
        )
        with L.LedgerLock():
            L.append_ledger(e)
        return e

    def state(self):
        return json.loads(rd(os.path.join(self.root, "skill", "retro-state.json")))

    def batch_dirs(self):
        d = os.path.join(self.root, "skill", "retro-batches")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def usage_json(self):
        b = self.batch_dirs()[-1]
        return json.loads(
            rd(os.path.join(self.root, "skill", "retro-batches", b, "usage.json"))
        )

    def failure_probe(self, run, situation):
        return E.record_failure(
            situation,
            expected="run the reviewer before merging",
            forbidden="",
            source_run=run,
            area="review gate",
            root=self.root,
        )["probe"]


# ==================================================================== semantic collision: before X vs after X
class TestSemanticCollision(Base):
    BEFORE = "- Always run the reviewer agent before merging a wave."
    AFTER = "- Always run the reviewer agent after merging a wave."
    PARAPHRASE = "- Always run the reviewer agent before merging each wave."

    def cand(self, new, n=1):
        return {
            "n": n,
            "scope": "generic",
            "target": SKILL_REL,
            "anchor": "Rules",
            "relation": "new",
            "drops": [],
            "op": "append",
            "rule": new.lstrip("- "),
            "old": "",
            "new": new,
            "area": "review gate",
            "needs_judge": True,
        }

    def test_before_x_and_after_x_are_not_near_and_are_judged_separately(self):
        pol = L.load_policy(self.root)
        self.failure_probe("run-a", "the lead merged a wave without the reviewer")
        self.answer("judge", {"judgments": [judgment(cur=True, new=True)]})
        first = J.judge(
            [self.cand(self.BEFORE)],
            batch_stamp="b1",
            batch_runs=["run-a"],
            budget_left=25000,
            root=self.root,
            policy=pol,
            wait_sec=5,
        )
        self.assertEqual(self.calls("judge"), 1)
        entry = J.read_log(self.root)[0]
        a = J.normalize_candidate(self.cand(self.BEFORE), self.root, pol)
        b = J.normalize_candidate(self.cand(self.AFTER), self.root, pol)
        c = J.normalize_candidate(self.cand(self.PARAPHRASE), self.root, pol)
        self.assertNotEqual(a["cand_fp"], b["cand_fp"])
        ok = J.near_identical(b, entry, pol)[0]
        self.assertFalse(ok, "before/after flips the meaning: never near-identical")
        self.assertTrue(
            J.near_identical(c, entry, pol)[0],
            "control: a real paraphrase IS near-identical (the check discriminates)",
        )
        precs = J.precedents(b, root=self.root, policy=pol, extra=[])
        self.assertEqual(
            [p["kind"] for p in precs], [], "the opposite rule is not a precedent"
        )
        precs = J.precedents(c, root=self.root, policy=pol, extra=[])
        self.assertEqual([p["kind"] for p in precs], ["near"])
        # the exact-repeat block of the first verdict does not touch the opposite rule: it gets its own call
        self.assertFalse(
            J.exact_block(
                b, probes=[], batch_runs=["run-a"], root=self.root, policy=pol
            )["blocked"]
        )
        self.answer("judge", {"judgments": [judgment(cur=False, new=True)]})
        second = J.judge(
            [self.cand(self.AFTER)],
            batch_stamp="b2",
            batch_runs=["run-a"],
            budget_left=25000,
            root=self.root,
            policy=pol,
            wait_sec=5,
        )
        self.assertEqual(self.calls("judge"), 2, "judged separately, not suppressed")
        self.assertEqual(second["usage"]["would_suppress"], 0)
        self.assertNotEqual(
            first["verdicts"][0]["decision_id"], second["verdicts"][0]["decision_id"]
        )
        log = J.read_log(self.root)
        self.assertEqual(len(log), 2)
        self.assertEqual(len({e["cand_fp"] for e in log}), 2)


# ==================================================================== ledger link survives judge-log rotation
class TestLedgerSurvivesRotation(Base):
    FP = "c" * 64

    def test_cooldown_is_derived_from_the_ledger_after_the_judge_log_is_rotated_away(
        self,
    ):
        now = time.time()
        pol = L.load_policy(self.root)
        old = [
            {
                "decision_id": f"d{i}",
                "ts": J._iso(now - 120 * DAY),
                "verdict": "reject",
                "reason_code": "harms_success",
                "scope": "generic",
                "target": SKILL_REL,
                "anchor": "Rules",
                "cand_fp": self.FP,
                "ctx": "x",
            }
            for i in range(3)
        ]
        J.append_verdicts(old, self.root)
        self.put_auto_revert(now - 5 * DAY, self.FP)
        self.put_auto_revert(now - 1 * DAY, self.FP)
        cand = {
            "n": 1,
            "scope": "generic",
            "target": REF_REL,
            "cand_fp": self.FP,
            "relation": "new",
        }
        before = P.cooldown_state(self.FP, None, now)
        self.assertTrue(before["cooldown"], before)
        rot = J.rotate(self.root, now, pol)
        self.assertEqual((rot["kept"], rot["archived"]), (0, 3))
        self.assertEqual(
            J.read_log(self.root), [], "the judge log no longer has the case"
        )
        self.assertTrue(os.path.exists(rot["archive"]), "archived, never deleted")
        after = P.cooldown_state(self.FP, None, now)
        self.assertTrue(
            after["cooldown"], "cooldown comes from the ledger, not from the log"
        )
        self.assertEqual(
            (after["until"], after["reverts"]), (before["until"], before["reverts"])
        )
        to_judge, held = P.gate_candidates([cand], now=now)
        self.assertEqual((to_judge, [h["status"] for h in held]), ([], ["cooldown"]))
        res = P.judge_gated(
            [cand], batch_stamp="b9", batch_runs=[], budget_left=25000, now=now
        )
        self.assertEqual(self.calls("judge"), 0, "held by the gate: no model call")
        self.assertEqual(res["held"][0]["status"], "cooldown")


# ==================================================================== revert interference
class TestRevertInterference(Base):
    RULE_A = "- Always rerun a flaky test once before reporting it as a failure in the summary."
    RULE_B = (
        "- Keep every fixture under fifty lines so a reviewer can read it in one go."
    )

    def test_reverting_one_probation_rule_leaves_the_other_file_and_rule_alone(self):
        ra, a = self.apply(SKILL_REL, self.RULE_A, "--probation", "--cand-fp", "a" * 64)
        rb, b = self.apply(REF_REL, self.RULE_B, "--probation", "--cand-fp", "b" * 64)
        self.assertEqual((ra.returncode, rb.returncode), (0, 0), ra.stdout + rb.stdout)
        ida, idb = a["ledger"]["id"], b["ledger"]["id"]
        self.assertEqual(
            {str(e["id"]) for e in P.probation_entries()}, {str(ida), str(idb)}
        )
        ref_before = rd(os.path.join(self.root, REF_REL))
        r = self.cli([APPLY, "--revert", str(ida), "--reason", "manual undo"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            rd(self.skill), SKILL, "the reverted file is back byte for byte"
        )
        self.assertEqual(rd(os.path.join(self.root, REF_REL)), ref_before)
        self.assertIn(self.RULE_B, ref_before)
        self.assertEqual([str(e["id"]) for e in P.probation_entries()], [str(idb)])
        self.assertEqual(
            P._index_status(R.load_index(), R.rule_hash(self.RULE_B)), "probation"
        )
        # the other one is still revertible on its own (no drift: nothing else touched its file)
        r = self.cli([APPLY, "--revert", str(idb), "--reason", "manual undo"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rd(os.path.join(self.root, REF_REL)), REF)
        self.assertEqual(P.probation_entries(), [])

    def test_same_always_loaded_file_is_blocked_by_a_one_probation_rule(self):
        r, a = self.apply(SKILL_REL, self.RULE_A, "--probation", "--cand-fp", "a" * 64)
        self.assertEqual(r.returncode, 0, r.stdout)
        before = rd(self.skill)
        r, out = self.apply(
            SKILL_REL, self.RULE_B, "--probation", "--cand-fp", "b" * 64
        )
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("in probation", out["error"])
        self.assertEqual(rd(self.skill), before, "nothing written")
        cand = {
            "n": 1,
            "scope": "generic",
            "target": SKILL_REL,
            "rule": self.RULE_B,
            "new": self.RULE_B,
            "cand_fp": "b" * 64,
            "relation": "new",
        }
        res = P.judge_gated([cand], batch_stamp="b1", batch_runs=[], budget_left=25000)
        self.assertEqual([h["status"] for h in res["held"]], ["waiting"])
        self.assertEqual(
            self.calls("judge"), 0, "waiting candidates never reach the judge"
        )
        # the revert of the holder frees the slot
        self.cli([APPLY, "--revert", str(a["ledger"]["id"]), "--reason", "undo"])
        to_judge, held = P.gate_candidates([cand])
        self.assertEqual((len(to_judge), held), (1, []))


# ==================================================================== cooldown exit with a post-revert probe
class TestCooldownExit(Base):
    FP = "e" * 64

    def test_cooldown_lifts_only_for_a_probe_from_a_run_after_the_last_revert(self):
        t0 = time.time() - 20 * DAY
        self.put_auto_revert(t0, self.FP)
        last = self.put_auto_revert(t0 + 10 * DAY, self.FP)
        now = t0 + 13 * DAY
        self.assertTrue(P.cooldown_state(self.FP, None, now)["cooldown"])
        before = self.failure_probe(
            "fleet-a-lead-1", "the lead skipped the review of wave two"
        )
        after = self.failure_probe(
            "fleet-b-lead-2", "the lead merged before the integration test passed"
        )
        self.add_run("alpha", "a", "fleet-a-lead-1", t0 + 9 * DAY)
        self.add_run("alpha", "b", "fleet-b-lead-2", t0 + 12 * DAY)
        s = P.cooldown_state(self.FP, None, now, probes=[before["id"]])
        self.assertTrue(
            s["cooldown"], "a probe from before the last revert does not lift it"
        )
        s = P.cooldown_state(self.FP, None, now, probes=[before["id"], after["id"]])
        self.assertEqual((s["cooldown"], s["early_exit"]), (False, after["id"]))
        self.assertEqual(s["last_revert"], last["ts"])
        # through the gate: the candidate carrying the post-revert probe goes to the judge, the other is held
        cand = {
            "n": 1,
            "scope": "generic",
            "target": REF_REL,
            "cand_fp": self.FP,
            "relation": "new",
        }
        to_judge, held = P.gate_candidates([dict(cand, probes=[after["id"]])], now=now)
        self.assertEqual((len(to_judge), held), (1, []))
        to_judge, held = P.gate_candidates([dict(cand, probes=[before["id"]])], now=now)
        self.assertEqual((to_judge, [h["status"] for h in held]), ([], ["cooldown"]))
        # the cooldown simply expires too (30 days after the 2nd auto-revert)
        self.assertFalse(
            P.cooldown_state(self.FP, None, t0 + 10 * DAY + 31 * DAY)["cooldown"]
        )


# ==================================================================== per-project quota, end to end
class TestProjectQuota(Base):
    WORDS = {
        "acme": [
            "Seed the database with `make seed` before the integration suite runs.",
            "Keep migrations reversible: every up step needs a matching down step.",
            "Pin the Node version in `.nvmrc` so CI and laptops build alike.",
            "Name feature flags after the ticket area, never after a person.",
        ],
        "beta": [
            "Generate the protobuf stubs with `make proto` before compiling the services.",
            "Run the linter in the pre-commit hook, not only in the pipeline.",
            "Store golden files next to the test that reads them.",
            "Tag every release candidate with the date in the tag name.",
        ],
    }

    def proposals(self):
        out = []
        for proj, texts in self.WORDS.items():
            for i, t in enumerate(texts):
                out.append(
                    {
                        "scope": "project",
                        "project": proj,
                        "rule": f"{proj} rule {i}",
                        "why": "learned in the last task",
                        "skill_step": "Build",
                        "target": "LESSONS.md",
                        "relation": "new",
                        "edit": {"old": "", "new": "- " + t},
                    }
                )
        return out

    def test_three_per_project_applied_and_the_fourth_is_not(self):
        self.mk("acme", "t1")
        self.mk("beta", "t2")
        self.answer("gen", {"verdict": "proposals", "proposals": self.proposals()})
        r = self.cycle()
        self.assertEqual(r.returncode, 0, r.stderr)
        for proj, texts in self.WORDS.items():
            les = rd(os.path.join(self.tmp, proj, "LESSONS.md"))
            for t in texts[:3]:
                self.assertIn(t, les)
            self.assertNotIn(
                texts[3], les, f"4th addition for {proj} is over the quota"
            )
        self.assertEqual(
            self.calls("judge"), 0, "project rules are lite: no judge call"
        )
        u = self.usage_json()
        self.assertEqual(u["applied"], 6)
        self.assertEqual(u["apply_failed"], 0)
        self.assertIn("applied 6 change", r.stdout)
        texts = "".join(
            rd(os.path.join(self.root, "skill", "retro-batches", b, "retro.md"))
            for b in self.batch_dirs()
            if os.path.exists(
                os.path.join(self.root, "skill", "retro-batches", b, "retro.md")
            )
        )
        self.assertIn("over the limit of 3 proposals for project:acme", texts)
        self.assertIn("over the limit of 3 proposals for project:beta", texts)


# ==================================================================== the full loop
class TestFullCycle(Base):
    def apply_generic_via_cycle(self, task):
        self.mk(task=task)
        self.answer("gen", {"verdict": "proposals", "proposals": [GENERIC]})
        self.answer("judge", {"judgments": [judgment()]})
        r = self.cycle()
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def regress_and_probe(self, start):
        """Baseline runs that were fine, then runs that all fail after `start`; evaluate the probation at start+W+1h."""
        self.runs(start - 2 * W, [1, 5, 9, 13], tag=f"b{int(start)}")
        self.runs(start, [1, 2, 3], state="failed", tag=f"f{int(start)}")
        return P.run(start + W + 3600)

    def test_retro_judge_apply_probation_auto_revert_cooldown_all_consistent(self):
        # 1. retro -> judge -> apply (in probation)
        r = self.apply_generic_via_cycle("t1")
        self.assertIn("generic 1", r.stdout)
        self.assertEqual((self.calls("gen"), self.calls("judge")), (1, 1))
        self.assertIn("unreviewed waves", rd(self.skill))
        (app,) = self.ops("apply")
        self.assertTrue(app.get("probation"))
        fp, did = app["cand_fp"], app["decision_id"]
        log = J.read_log(self.root)
        self.assertEqual(
            [(e["decision_id"], e["cand_fp"], e["verdict"]) for e in log],
            [(did, fp, "accept")],
        )
        h = R.rule_hash(GENERIC["edit"]["new"])
        self.assertEqual(P._index_status(R.load_index(), h), "probation")
        self.assertEqual(self.state()["last_batch"].startswith("retro-batch-"), True)
        u1 = self.usage_json()
        self.assertEqual((u1["applied"], u1["judged"], u1["calls"]["judge"]), (1, 1, 1))
        # 2. probation -> auto-revert (regression over the window)
        rep = self.regress_and_probe(P._epoch(app["ts"]))
        self.assertEqual([x["status"] for x in rep["reverted"]], ["reverted"], rep)
        self.assertEqual(rd(self.skill), SKILL, "exact bytes back")
        (rev,) = self.ops("revert")
        (ar,) = self.ops("auto-revert")
        self.assertEqual((rev["reverts"], rev.get("auto_revert")), (app["id"], True))
        self.assertEqual(
            (ar["reverted"], ar["revert_entry"], ar["cand_fp"], ar["decision_id"]),
            (app["id"], rev["id"], fp, did),
        )
        self.assertEqual(P.probation_entries(), [])
        self.assertNotEqual(P._index_status(R.load_index(), h), "probation")
        self.assertFalse(
            P.cooldown_state(fp, None)["cooldown"], "one auto-revert: no cooldown yet"
        )
        # 3. the same rule comes back, is judged again (with the auto-revert as precedent) and reverted again
        time.sleep(1.1)  # ended_at has second resolution
        self.apply_generic_via_cycle("t2")
        self.assertEqual(self.calls("judge"), 2)
        self.assertIn(
            "auto-reverted", rd(os.path.join(self.fdir, "prompt-judge-2.txt"))
        )
        apps = self.ops("apply")
        self.assertEqual(len(apps), 2)
        self.assertEqual(apps[1]["cand_fp"], fp)
        rep = self.regress_and_probe(P._epoch(apps[1]["ts"]) + 5 * DAY)
        self.assertEqual([x["status"] for x in rep["reverted"]], ["reverted"], rep)
        self.assertEqual(len(self.ops("auto-revert")), 2)
        # 4. cooldown: a third attempt is held by the gate, no judge call, nothing applied
        cd = P.cooldown_state(fp, None)
        self.assertTrue(cd["cooldown"], cd)
        self.assertEqual(cd["days"], 30.0)
        time.sleep(1.1)
        self.mk(task="t3")
        self.answer("gen", {"verdict": "proposals", "proposals": [GENERIC]})
        r = self.cycle()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.calls("judge"), 2, "the cooldown holds it before the judge"
        )
        self.assertEqual(rd(self.skill), SKILL)
        self.assertEqual(len(self.ops("apply")), 2)
        cj = [
            json.loads(
                rd(
                    os.path.join(
                        self.root, "skill", "retro-batches", b, "candidates.json"
                    )
                )
            )
            for b in self.batch_dirs()
            if os.path.exists(
                os.path.join(self.root, "skill", "retro-batches", b, "candidates.json")
            )
        ]
        statuses = [
            c["status"]
            for cj_ in cj[-1:]
            for s in cj_["segments"]
            for c in s["candidates"]
        ]
        self.assertEqual(statuses, ["cooldown"])
        # all bookkeeping agrees: ledger chain apply -> revert -> auto-revert (x2), one log line per judgment
        ids = [e["id"] for e in self.ledger()]
        self.assertEqual(len(ids), len(set(ids)), "ledger ids are unique")
        self.assertEqual(len(J.read_log(self.root)), 2)
        self.assertTrue(all(e["cand_fp"] == fp for e in J.read_log(self.root)))
        self.assertEqual(P.probation_entries(), [])
        self.assertEqual(
            sorted(e["reverts"] for e in self.ops("revert")),
            sorted(str(a["id"]) for a in apps),
        )


# ==================================================================== shadow-cycle harness
def tree_hash(root):
    h = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(base, f)
            h.update(p.encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            else:
                h.update(rb(p))
                h.update(str(os.stat(p).st_mtime_ns).encode())
    return h.hexdigest()


class TestShadow(Base):
    def make_source(self):
        src = os.path.join(self.tmp, "source")
        os.makedirs(src)
        T = os.path.join(src, "acme", "t1")
        write(os.path.join(T, "FINAL-SUMMARY.md"), "status: DONE\n")
        write(os.path.join(T, "progress.md"), "# Progress\nwave merged unreviewed\n")
        write(os.path.join(T, "plan.md"), "# plan\n")
        write(
            os.path.join(T, "criteria.json"),
            json.dumps({"criteria": [{"id": "C1", "passes": True}]}),
        )
        write(os.path.join(T, "notes-private.md"), "not an allowed task file\n")
        write(
            os.path.join(T, "runs", "fleet-t1-lead-010101.status.json"),
            json.dumps(
                {"state": "done", "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            ),
        )
        write(
            os.path.join(T, "runs", "fleet-t1-lead-010101.events.jsonl"),
            '{"type":"result"}\n',
        )
        write(os.path.join(T, "runs", "fleet-t1-lead-010101.stderr.log"), "warn\n")
        write(
            os.path.join(T, "runs", "fleet-t1-lead-010101.prompt.txt"), "prompt text\n"
        )
        write(os.path.join(src, "acme", "LESSONS.md"), "# acme\n- project lesson\n")
        write(os.path.join(src, "learnings.md"), "# Lessons\n- generic lesson\n")
        write(os.path.join(src, "fleet", "skill", "fleet-sdd", "SKILL.md"), SKILL)
        write(os.path.join(src, "fleet", "skill", "ledger.jsonl"), "")
        # things that must NEVER be copied, each holding the canary
        write(os.path.join(src, "vigia", "webhook.env"), f"WEBHOOK_URL={CANARY}\n")
        write(os.path.join(src, "vigia", "active", "r.json"), f'{{"token":"{CANARY}"}}')
        write(os.path.join(src, "acme", "t1", "webhook.env"), f"X={CANARY}\n")
        write(os.path.join(src, "acme", "t1", "runs", "secrets.env"), f"X={CANARY}\n")
        write(os.path.join(src, "acme", "vigia", "note.txt"), CANARY)
        write(os.path.join(src, "fleet", "skill", "webhook.env"), f"X={CANARY}\n")
        write(os.path.join(src, "fleet", "skill", "vigia", "x.json"), CANARY)
        write(os.path.join(src, "fleet", "skill", "prod.env"), f"X={CANARY}\n")
        os.symlink(
            os.path.join(src, "vigia", "webhook.env"),
            os.path.join(src, "acme", "t1", "link.md"),
        )
        os.symlink(
            os.path.join(src, "vigia", "webhook.env"),
            os.path.join(src, "fleet", "skill", "plan.md"),
        )
        # a large archive stays out
        big = os.path.join(src, "fleet", "skill", "archive", "big.jsonl")
        os.makedirs(os.path.dirname(big))
        with open(big, "wb") as f:
            f.truncate(6 * 1024 * 1024)
        return src

    def dest_files(self, dest):
        out = []
        for base, dirs, files in os.walk(dest):
            for f in files:
                out.append(os.path.join(base, f))
            out += [os.path.join(base, d) for d in dirs]
        return out

    def run_shadow(self, src, dest, *args):
        return subprocess.run(
            [SHADOW, "--source", src, "--dest", dest] + list(args),
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )

    def test_ct10_shadow_never_copies_vigia_or_env_files_and_leaves_the_source_untouched(
        self,
    ):
        src = self.make_source()
        dest = os.path.join(self.tmp, "dest")
        before = tree_hash(src)
        r = self.run_shadow(src, dest, "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            tree_hash(src), before, "the source tree is byte-identical (and mtimes)"
        )
        for p in self.dest_files(dest):
            self.assertNotIn("vigia", os.path.relpath(p, dest).split(os.sep))
            self.assertFalse(p.lower().endswith(".env"), p)
            self.assertFalse(os.path.islink(p), p)
            if os.path.isfile(p):
                self.assertNotIn(CANARY.encode(), rb(p), p)
        # the allowed files made it
        for rel in (
            "acme/t1/FINAL-SUMMARY.md",
            "acme/t1/progress.md",
            "acme/t1/plan.md",
            "acme/t1/criteria.json",
            "acme/t1/runs/fleet-t1-lead-010101.status.json",
            "acme/t1/runs/fleet-t1-lead-010101.events.jsonl",
            "acme/t1/runs/fleet-t1-lead-010101.stderr.log",
            "acme/LESSONS.md",
            "learnings.md",
            "fleet/skill/fleet-sdd/SKILL.md",
        ):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        # and the not-allowed ones did not
        for rel in (
            "acme/t1/notes-private.md",
            "acme/t1/runs/fleet-t1-lead-010101.prompt.txt",
            "acme/t1/link.md",
            "fleet/skill/archive/big.jsonl",
        ):
            self.assertFalse(os.path.lexists(os.path.join(dest, rel)), rel)
        out = json.loads(r.stdout)
        self.assertEqual(out["selected"], [os.path.join(dest, "acme", "t1")])
        self.assertEqual(self.calls(), 0, "no model call without --with-model")
        self.assertIn("estimated_tokens", out)
        self.assertGreater(out["estimated_tokens"], 0)

    def test_the_forbidden_files_are_never_opened(self):
        src = self.make_source()
        dest = os.path.join(self.tmp, "dest2")
        mod = load_mod(SHADOW, "fleet_retro_shadow_t")
        opened = []
        real_open, real_os_open = open, os.open

        def spy_open(f, *a, **k):
            opened.append(str(f))
            return real_open(f, *a, **k)

        def spy_os_open(f, *a, **k):
            opened.append(str(f))
            return real_os_open(f, *a, **k)

        with mock.patch("builtins.open", spy_open), mock.patch("os.open", spy_os_open):
            stats = mod.build_shadow(src, dest)
        self.assertGreater(stats["files"], 5)
        self.assertTrue(opened)
        for p in opened:
            parts = p.split(os.sep)
            self.assertNotIn("vigia", parts, p)
            self.assertFalse(p.lower().endswith(".env"), p)
            self.assertNotIn("archive", parts, p)
            self.assertFalse(os.path.basename(p) == "link.md", p)
        self.assertEqual(
            stats["skipped_large"], [os.path.join(src, "fleet", "skill", "archive")]
        )

    def test_with_model_makes_one_call_inside_dest_and_prints_the_numbers(self):
        src = self.make_source()
        dest = os.path.join(self.tmp, "dest3")
        self.answer("gen", {"verdict": "proposals", "proposals": [GENERIC]})
        # a past batch's usage.json travels with fleet/skill/**: its tokens and would_suppress are summed
        write(
            os.path.join(
                src,
                "fleet",
                "skill",
                "retro-batches",
                "20250101-000000-1aa",
                "usage.json",
            ),
            json.dumps(
                {
                    "tokens": {"input_tokens": 1000, "output_tokens": 200},
                    "would_suppress": 2,
                    "blocked": 1,
                    "calls": {"total": 2},
                }
            ),
        )
        before = tree_hash(src)
        r = self.run_shadow(src, dest, "--with-model")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree_hash(src), before)
        self.assertEqual(self.calls("gen"), 1)
        self.assertEqual(self.calls("judge"), 0)
        self.assertIn("real tokens 1200", r.stdout)
        self.assertIn("would judge 1", r.stdout)
        self.assertIn("would_suppress 2", r.stdout)
        self.assertIn("tokens 1200", r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(dest, "shadow-out", "gen", "runs")))
        self.assertFalse(os.path.exists(os.path.join(dest, "acme", "LESSONS.md.new")))
        self.assertFalse(
            os.path.exists(os.path.join(dest, "fleet", "skill", "retro-state.json")),
            "a dry run keeps no state, not even in the copy",
        )

    def test_symlinks_are_never_followed(self):
        src = self.make_source()
        vig = os.path.join(src, "vigia")
        # vigia/runs holds look-alike run files with the canary
        write(
            os.path.join(vig, "runs", "evil.status.json"), json.dumps({"state": CANARY})
        )
        write(os.path.join(vig, "runs", "evil.events.jsonl"), CANARY + "\n")
        # 1. symlinked runs dir pointing into vigia/
        T2 = os.path.join(src, "acme", "t2")
        write(os.path.join(T2, "plan.md"), "# plan t2\n")
        os.symlink(os.path.join(vig, "runs"), os.path.join(T2, "runs"))
        # 2. symlinked *.status.json files pointing to the webhook.env canary
        T3 = os.path.join(src, "acme", "t3")
        write(os.path.join(T3, "plan.md"), "# plan t3\n")
        os.makedirs(os.path.join(T3, "runs"))
        os.symlink(
            os.path.join(vig, "webhook.env"), os.path.join(T3, "runs", "x.status.json")
        )
        os.symlink(os.path.join(vig, "webhook.env"), os.path.join(T3, "progress.md"))
        # 3. symlinked project dir (whole project elsewhere) and symlinked task dir
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "t9", "plan.md"), CANARY)
        os.symlink(outside, os.path.join(src, "linkproj"))
        os.symlink(os.path.join(outside, "t9"), os.path.join(src, "acme", "linktask"))
        # 4. symlinked skill dir contents
        os.symlink(vig, os.path.join(src, "fleet", "skill", "linkdir"))
        dest = os.path.join(self.tmp, "dest-links")
        before = tree_hash(src)
        mod = load_mod(SHADOW, "fleet_retro_shadow_links")
        opened = []
        real_open = open

        def spy_open(f, *a, **k):
            opened.append(str(f))
            return real_open(f, *a, **k)

        with mock.patch("builtins.open", spy_open):
            mod.build_shadow(src, dest)
        for p in opened:
            self.assertNotIn(os.path.join(src, "vigia"), p)
            self.assertNotIn(os.path.join("acme", "t2", "runs"), p)
            self.assertNotIn(os.path.join("acme", "t3", "runs", "x.status.json"), p)
            self.assertNotIn("linkproj", p)
            self.assertNotIn("linktask", p)
        for p in self.dest_files(dest):
            self.assertFalse(os.path.islink(p), p)
            if os.path.isfile(p):
                self.assertNotIn(CANARY.encode(), rb(p), p)
        for rel in (
            "acme/t2/runs",
            "acme/t3/runs/x.status.json",
            "acme/t3/progress.md",
            "linkproj",
            "acme/linktask",
            "fleet/skill/linkdir",
        ):
            self.assertFalse(os.path.lexists(os.path.join(dest, rel)), rel)
        self.assertTrue(os.path.isfile(os.path.join(dest, "acme", "t2", "plan.md")))
        self.assertEqual(tree_hash(src), before)

    def test_refuses_nested_or_non_empty_dest(self):
        src = self.make_source()
        r = self.run_shadow(src, os.path.join(src, "inside"))
        self.assertNotEqual(r.returncode, 0)
        d = os.path.join(self.tmp, "full")
        write(os.path.join(d, "x"), "x")
        r = self.run_shadow(src, d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("empty", r.stderr)

    def test_refuses_symlinked_dest_or_ancestor_aliasing_source(self):
        src = self.make_source()
        before = sorted(os.path.relpath(os.path.join(dp, f), src)
                        for dp, _, fs in os.walk(src) for f in fs)
        os.makedirs(os.path.join(src, "emptyin"))
        # 1. dest itself is a symlink to an empty folder inside SOURCE
        link = os.path.join(self.tmp, "destlink")
        os.symlink(os.path.join(src, "emptyin"), link)
        r = self.run_shadow(src, link)
        self.assertNotEqual(r.returncode, 0)
        # 2. dest's ancestor is a symlink to SOURCE
        anc = os.path.join(self.tmp, "anclink")
        os.symlink(src, anc)
        r = self.run_shadow(src, os.path.join(anc, "newdest"))
        self.assertNotEqual(r.returncode, 0)
        after = sorted(os.path.relpath(os.path.join(dp, f), src)
                       for dp, _, fs in os.walk(src) for f in fs)
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(src, "newdest")))
        self.assertEqual(os.listdir(os.path.join(src, "emptyin")), [])

    def test_source_custom_lessons_name_is_copied_and_passed_to_the_child(self):
        src = self.make_source()
        write(os.path.join(src, "fleet", "skill", "retro-policy.json"),
              json.dumps({"lessons_file": "CUSTOM-LESSONS.md"}))
        write(os.path.join(src, "CUSTOM-LESSONS.md"), "# lessons\n")
        mod = load_mod(SHADOW, "fleet_retro_shadow_custom")
        dest = os.path.join(self.tmp, "dest-custom")
        os.makedirs(dest)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_LESSONS_NAME", None)
            name = mod.source_lessons_name(src)
            self.assertEqual(name, "CUSTOM-LESSONS.md")
            mod.build_shadow(src, dest, name)
            self.assertTrue(os.path.isfile(os.path.join(dest, "CUSTOM-LESSONS.md")))
            env = mod.shadow_env(dest, name)
        self.assertEqual(env["FLEET_LESSONS"], os.path.join(dest, "CUSTOM-LESSONS.md"))

    def test_vigia_bin_is_resolved_before_roots_are_redirected(self):
        mod = load_mod(SHADOW, "fleet_retro_shadow_vbin")
        dest = os.path.join(self.tmp, "dest-vbin")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIGIA_BIN", None)
            expected = os.path.join(os.path.dirname(mod.L.fleet_root()), "bin")
            env = mod.shadow_env(dest)
        self.assertEqual(env["VIGIA_BIN"], expected)
        self.assertFalse(env["VIGIA_BIN"].startswith(dest))


if __name__ == "__main__":
    unittest.main()
