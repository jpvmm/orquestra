#!/usr/bin/env python3
"""Probation, auto-revert, txn recovery and ledger-derived cooldown (PLAN F4, F3.1 rules 3 and 5). No model, no
network; temporary roots only. Run: python3 tests/test_probation.py (or pytest)."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

APPLY, LINT = (os.path.join(BIN, n) for n in ("fleet-skill-apply", "fleet-skill-lint"))
ENV_KEYS = (
    "FLEET_ROOT",
    "ORQ_ROOT",
    "FLEET_RETRO_POLICY",
    "FLEET_LESSONS_NAME",
    "FLEET_LESSONS",
    "ORQUESTRA_HOME",
    "VIGIA_ATIVOS",
    "VIGIA_BIN",
)
SKILL_REL = "skill/fleet-sdd/SKILL.md"
REF_REL = "skill/fleet-sdd/references/tests.md"
SKILL = "# fleet-sdd\n\n## Rules\n- Never touch a test without saying so.\n"
RULE_A = (
    "- Always rerun a flaky test once before reporting it as a failure in the summary."
)
RULE_B = "- Write the integration branch name in progress.md before dispatching the first wave."
DAY = 86400.0
W = 48 * 3600.0  # policy cycle.due_hours


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


def rbytes(p):
    with open(p, "rb") as f:
        return f.read()


def rtext(p):
    with open(p) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.mkdtemp(prefix="fleet-probation-test-")
        self.root = os.path.join(self.tmp, "fleet")
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.fakebin = os.path.join(
            self.tmp, "fakebin"
        )  # a test must never launch a real vigia run
        write(os.path.join(self.fakebin, "vigia-run"), "#!/bin/sh\nexit 99\n")
        os.chmod(os.path.join(self.fakebin, "vigia-run"), 0o755)
        os.environ.update(
            FLEET_ROOT=self.root,
            ORQ_ROOT=self.tmp,
            VIGIA_ATIVOS=os.path.join(self.tmp, "ativos"),
            VIGIA_BIN=self.fakebin,
        )
        self.skill = os.path.join(self.root, SKILL_REL)
        write(self.skill, SKILL)
        write(
            os.path.join(self.root, REF_REL),
            "# Tests\n- Keep fixtures small and local to the test.\n",
        )
        self.n = 0

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def cli(self, args):
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )

    def diff_add(self, rel, line, base=None):
        base = base or self.root
        old = rtext(os.path.join(base, rel))
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
                "test",
            ]
            + list(extra)
        )
        return r, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else {})

    def apply_probation(self, line=RULE_A, rel=SKILL_REL, *extra):
        r, out = self.apply(rel, line, "--probation", "--cand-fp", "a" * 64, *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return out["ledger"]

    def ledger(self, scope=None):
        return L.read_ledger(scope or L.generic_scope())

    def ops(self, op):
        return [e for e in self.ledger() if e.get("op") == op]

    def add_run(
        self, project, task, name, ended, state="done", progress=None, criteria=None
    ):
        T = os.path.join(self.tmp, project, task)
        write(
            os.path.join(T, "runs", name + ".status.json"),
            json.dumps({"name": name, "state": state, "ended_at": P._iso(ended)}),
        )
        if progress is not None:
            write(os.path.join(T, "progress.md"), progress)
        if criteria is not None:
            write(os.path.join(T, "criteria.json"), json.dumps({"criteria": criteria}))
        return T

    def runs(self, start, offsets, state="done", project="alpha", tag="t"):
        """One task folder per run (ended at start + offset hours)."""
        for i, h in enumerate(offsets):
            self.add_run(
                project,
                f"{tag}{i}-{int(h * 10)}",
                f"fleet-{tag}{i}-lead-{int(h * 10)}",
                start + h * 3600,
                state,
            )

    def status(self, h):
        return P._index_status(R.load_index(), h)


# ==================================================================== CT7: regression → auto-revert, drift, recovery
class TestAutoRevert(Base):
    def setUp(self):
        super().setUp()
        self.before = rbytes(self.skill)
        self.probe = E.record_failure(
            "The lead reported a flaky test as a failure without rerunning it.",
            "Rerun the flaky test once before reporting.",
            "Report a single flaky failure as a regression.",
            "fleet-old-lead-1",
            area="tests",
            tags=["flaky-tests"],
            root=self.root,
        )["probe"]
        self.e = self.apply_probation(
            RULE_A, SKILL_REL, "--failure-probe", self.probe["id"]
        )
        self.start = P._epoch(self.e["ts"])
        self.h = R.rule_hash(RULE_A)

    def test_ct7_synthetic_regression_auto_reverts_with_ledger_entry_and_counterexample(
        self,
    ):
        self.assertEqual(self.status(self.h), "probation")
        self.runs(self.start - 2 * W, [1, 5, 9, 13])  # baseline: all done
        self.runs(
            self.start, [1, 2, 3], state="failed", tag="after"
        )  # after the rule: every run fails
        with mock.patch.object(P.subprocess, "run", wraps=subprocess.run) as spy:
            rep = P.run(self.start + W + 3600)
        self.assertEqual([r["status"] for r in rep["reverted"]], ["reverted"], rep)
        argv = [c.args[0] for c in spy.call_args_list][0]
        self.assertEqual(argv[1:3], ["--revert", self.e["id"]])
        self.assertNotIn("--force", argv)
        self.assertEqual(rbytes(self.skill), self.before)  # exact bytes back
        rev = self.ops("revert")
        self.assertEqual([e["reverts"] for e in rev], [self.e["id"]])
        (ar,) = self.ops("auto-revert")
        self.assertEqual(ar["reverted"], self.e["id"])
        self.assertEqual(ar["revert_entry"], rev[0]["id"])
        self.assertEqual(ar["cand_fp"], "a" * 64)
        self.assertEqual(ar["metric"]["regressed"][0]["metric"], "not_done_rate")
        self.assertIn("regression not_done_rate", ar["metric_text"])
        self.assertEqual(ar["counterexample_probe"], self.probe["id"])
        self.assertEqual(
            E.load_probe(self.probe["id"], self.root)["id"], self.probe["id"]
        )  # kept
        self.assertEqual(P.probation_entries(), [])
        # idempotent: a second pass changes nothing
        n = len(self.ledger())
        rep2 = P.run(self.start + W + 7200)
        self.assertEqual((rep2["reverted"], len(self.ledger())), ([], n))
        # the judge gets the precedent "applied and auto-reverted by <metric>" + the counterexample probe
        cand = {
            "scope": "generic",
            "target": SKILL_REL,
            "anchor": "Rules",
            "cand_fp": "b" * 64,
            "relation": "new",
            "rule": RULE_A.replace("Always", "Please always"),
            "new": RULE_A.replace("Always", "Please always"),
        }
        precs = J.precedents(cand, root=self.root, log=[])
        self.assertEqual(precs[0]["kind"], "auto_revert")
        self.assertIn("applied and auto-reverted by regression", precs[0]["reason"])
        self.assertIn(self.probe["id"], precs[0]["reason"])
        self.assertEqual(J.precedents(cand, root=self.root, log=[], extra=[]), [])

    def test_recurrence_of_the_target_failure_auto_reverts(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], tag="after")  # all done: no regression
        again = E.record_failure(
            "Again the lead reported a flaky test as failed, no rerun at all.",
            "Rerun the flaky test once.",
            "",
            "fleet-after1-lead-20",
            area="tests",
            tags=["flaky-tests", "lead"],
            root=self.root,
            now=self.start + 7200,
        )["probe"]
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "revert", ev)
        self.assertEqual(ev["metric"]["recurrence"], [again["id"]])
        rep = P.run(self.start + W + 60)
        self.assertEqual(
            rep["reverted"][0]["entry"]["counterexample_probe"], again["id"]
        )

    def test_exact_repeat_of_the_target_failure_in_a_later_run_reverts(self):
        # the probe store dedupes identical content (the frozen probe keeps its first source_run), so an exact repeat
        # is only visible through the occurrence log
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], tag="after")  # all done: no regression
        same = E.record_failure(
            self.probe["situation"],
            self.probe["expected"],
            self.probe["forbidden"],
            "fleet-after1-lead-20",
            area="tests",
            tags=["flaky-tests"],
            root=self.root,
        )
        self.assertEqual(
            (same["status"], same["probe"]["source_run"]),
            ("exists", "fleet-old-lead-1"),
        )
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "extend", ev)  # invisible without the log
        occ = P.record_occurrence(
            self.probe["id"], "fleet-after1-lead-20", self.start + 7200
        )
        self.assertTrue(occ["created"])
        again = P.record_occurrence(self.probe["id"], "fleet-after1-lead-20")
        self.assertFalse(again["created"])  # idempotent per (probe, run)
        self.assertEqual(len(P.read_occurrences()[0]), 1)
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "revert", ev)
        self.assertEqual(ev["metric"]["recurrence"], [self.probe["id"]])
        # an occurrence from a run BEFORE the rule never counts
        self.runs(self.start - 2 * W, [20], tag="pre")
        pre = P.record_occurrence(self.probe["id"], "fleet-pre0-lead-200")
        rt = P.run_end_times()
        only_pre = [pre["occurrence"]]
        self.assertEqual(
            P.recurrences(self.probe, self.start, self.start + W, rt, None, only_pre),
            [],
        )
        self.assertEqual(
            [
                p["id"]
                for p in P.recurrences(
                    self.probe, self.start - 2 * W, self.start, rt, None, only_pre
                )
            ],
            [self.probe["id"]],
        )

    def test_occurrence_log_validates_and_fails_closed(self):
        with self.assertRaises(ValueError):
            P.record_occurrence("fail-" + "9" * 16, "fleet-x-lead-1")  # unknown probe
        with self.assertRaises(ValueError):
            P.record_occurrence(self.probe["id"], "../evil")
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], tag="after")
        write(P.occurrences_path(), "{not json\n")
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "extend")
        self.assertIn("occurrence log", ev["reason"])
        with self.assertRaises(ValueError):  # never rewritten over a corrupt log
            P.record_occurrence(self.probe["id"], "fleet-after0-lead-10")
        self.assertEqual(rtext(P.occurrences_path()), "{not json\n")

    def test_task_counted_once_in_the_window_of_its_last_run(self):
        # task X ran clean before the rule, then again after it and its progress.md now holds BLOCKED entries:
        # the current progress.md belongs to the window of its LAST run (after), never to the baseline
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.add_run("alpha", "X", "fleet-X-lead-1", self.start - W, progress="# P\n")
        self.add_run(
            "alpha",
            "X",
            "fleet-X-lead-2",
            self.start + 3600,
            progress="# P\n## BLOCKED\n- one\n- two\n- three\n",
        )
        self.runs(self.start, [2, 3], tag="after")
        dirs = P.task_dirs()
        base = P.collect_metrics(dirs, self.start - 2 * W, self.start)
        self.assertEqual(
            (base["tasks"], base["runs"], base["blocked"]), (3, 3, 0)
        )  # X not in the baseline
        after = P.collect_metrics(dirs, self.start, self.start + W)
        self.assertEqual((after["tasks"], after["runs"], after["blocked"]), (3, 3, 3))
        self.assertNotIn("fleet-X-lead-1", after["run_names"])
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "revert", ev)
        self.assertEqual(
            [r["metric"] for r in ev["metric"]["regressed"]], ["blocked_per_task"]
        )
        # a task with a lead run still running is open: counted in no window
        T = self.add_run("alpha", "Y", "fleet-Y-lead-1", self.start + 7200)
        write(
            os.path.join(T, "runs", "fleet-Y-lead-2.status.json"),
            json.dumps({"state": "running"}),
        )
        m = P.collect_metrics(P.task_dirs(), self.start, self.start + W)
        self.assertEqual((m["tasks"], m["open"]), (3, ["alpha/Y"]))

    def test_too_many_status_files_fail_closed(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], tag="after")
        T = os.path.join(self.tmp, "alpha", "after0-10", "runs")
        for i in range(P.STATUS_CAP):
            write(os.path.join(T, f"fleet-z{i}-lead-1.status.json"), "{}")
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "extend", ev)
        self.assertIn("more than", ev["errors"][0]["error"])

    def test_ct7_drift_refuses_the_revert_and_is_reported_not_counted(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], state="failed", tag="after")
        with open(self.skill, "a") as f:
            f.write("- A manual edit made after the probation started.\n")
        drifted = rbytes(self.skill)
        rep = P.run(self.start + W + 60)
        self.assertEqual(rep["reverted"], [])
        self.assertEqual(rep["drift"][0]["status"], "drift")
        self.assertEqual(rep["drift"][0]["files"], [SKILL_REL])
        self.assertEqual(rbytes(self.skill), drifted)
        self.assertEqual((self.ops("revert"), self.ops("auto-revert")), ([], []))
        self.assertFalse(
            P.cooldown_state("a" * 64, None, self.start + W + 120)["cooldown"]
        )
        # the CLI itself refuses without --force (auto_revert never passes it)
        r = self.cli([APPLY, "--revert", self.e["id"], "--reason", "x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed after", json.loads(r.stdout)["error"])

    def test_drift_refusal_by_the_apply_tool_is_reported_and_writes_nothing(self):
        # the file moves between the pre-check and the revert: fleet-skill-apply refuses (it is never given --force)
        stub = os.path.join(self.tmp, "fake-apply")
        write(
            stub,
            '#!/bin/sh\necho \'{"ok": false, "error": "files changed after this entry", "files": ["x"]}\'\n'
            "exit 1\n",
        )
        os.chmod(stub, 0o755)
        r = P.auto_revert(self.e["id"], {"text": "regression"}, apply_bin=stub)
        self.assertEqual((r["status"], r["files"]), ("drift", ["x"]))
        self.assertEqual((self.ops("revert"), self.ops("auto-revert")), ([], []))
        self.assertEqual(P.probation_holders(SKILL_REL), [self.e["id"]])

    def test_crash_between_revert_and_auto_revert_entry_is_finished_once(self):
        reason = P._auto_reason(
            self.e["id"], {"text": "regression x"}, self.probe["id"]
        )
        r = self.cli(
            [
                APPLY,
                "--revert",
                self.e["id"],
                "--auto-revert-of",
                self.e["id"],
                "--reason",
                reason,
            ]
        )  # the revert done (as auto_revert's argv does it), then "crash"
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.ops("auto-revert"), [])
        rep = P.run(self.start + 60)
        self.assertEqual([x["status"] for x in rep["recovered"]], ["reverted"])
        (ar,) = self.ops("auto-revert")
        self.assertEqual(
            (ar["reverted"], ar["counterexample_probe"]),
            (self.e["id"], self.probe["id"]),
        )
        P.run(self.start + 120)
        self.assertEqual(len(self.ops("auto-revert")), 1)
        self.assertEqual(P.auto_revert(self.e["id"], {})["status"], "already")

    def test_auto_revert_is_marked_structurally_never_by_the_reason(self):
        # a human reason that imitates the auto-revert prefix is refused without the structural flag
        fake = P._auto_reason(self.e["id"], {"text": "regression x"}, self.probe["id"])
        r = self.cli([APPLY, "--revert", self.e["id"], "--reason", fake])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("reserved", json.loads(r.stdout)["error"])
        self.assertEqual(self.ops("revert"), [])
        # the flag must name the reverted entry, and only a probation entry can be auto-reverted
        r = self.cli([APPLY, "--revert", self.e["id"], "--auto-revert-of", "999"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        r2, plain = self.apply(REF_REL, RULE_B)
        self.assertEqual(r2.returncode, 0, r2.stdout)
        pid = plain["ledger"]["id"]
        r = self.cli([APPLY, "--revert", pid, "--auto-revert-of", pid])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("not an apply entry in probation", json.loads(r.stdout)["error"])
        # auto_revert's own revert entry carries the marker
        res = P.auto_revert(self.e["id"], {"text": "regression x"})
        self.assertEqual(res["status"], "reverted", res)
        (rev,) = self.ops("revert")
        self.assertEqual(
            (rev["auto_revert"], rev["auto_revert_of"]), (True, self.e["id"])
        )

    def test_manual_revert_is_not_an_auto_revert(self):
        r = self.cli([APPLY, "--revert", self.e["id"], "--reason", "human said no"])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(
            P.auto_revert(self.e["id"], {"text": "x"})["status"], "manually_reverted"
        )
        P.run(self.start + 60)
        self.assertEqual(self.ops("auto-revert"), [])

    def test_insufficient_sample_extends_probation(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(
            self.start, [1, 2], state="failed", tag="after"
        )  # 2 < min_runs 3, even after 3 windows
        ev = P.evaluate_probation(self.e, self.start + 3 * W + 60)
        self.assertEqual(ev["decision"], "extend", ev)
        self.assertIn("insufficient sample", ev["reason"])
        rep = P.run(self.start + 3 * W + 60)
        self.assertEqual((rep["reverted"], rep["activated"]), ([], []))
        self.assertEqual(self.status(self.h), "probation")
        self.assertEqual(rbytes(self.skill), self.before + RULE_A.encode() + b"\n")

    def test_small_window_grows_until_min_runs(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(
            self.start, [1, W / 3600 + 1, W / 3600 + 2], state="failed", tag="after"
        )
        ev = P.evaluate_probation(self.e, self.start + 2 * W + 60)
        self.assertEqual(
            ev["decision"], "revert", ev
        )  # 1 run in window 1 + 2 in window 2 = one sample of 3

    def test_two_clean_windows_activate(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], tag="w1")
        self.assertEqual(
            P.evaluate_probation(self.e, self.start + W + 60)["decision"], "extend"
        )
        self.runs(self.start + W, [1, 2, 3], tag="w2")
        rep = P.run(self.start + 2 * W + 60)
        self.assertEqual(len(rep["activated"]), 1, rep)
        (end,) = self.ops("probation-end")
        self.assertEqual((end["of"], end["result"]), (self.e["id"], "active"))
        self.assertEqual(self.status(self.h), "active")
        self.assertEqual(P.probation_entries(), [])
        P.run(self.start + 2 * W + 120)
        self.assertEqual(len(self.ops("probation-end")), 1)

    def test_unreadable_task_file_in_window_fails_closed(self):
        self.runs(self.start - 2 * W, [1, 5, 9])
        self.runs(self.start, [1, 2, 3], state="failed", tag="after")
        T = os.path.join(self.tmp, "alpha", "after0-10")
        write(os.path.join(T, "criteria.json"), "{not json")
        ev = P.evaluate_probation(self.e, self.start + W + 60)
        self.assertEqual(ev["decision"], "extend")
        self.assertTrue(ev["errors"])
        # a symlinked task dir is skipped, never followed
        os.symlink(
            os.path.join(self.tmp, "alpha", "after1-20"),
            os.path.join(self.tmp, "alpha", "link"),
        )
        self.assertNotIn(os.path.join(self.tmp, "alpha", "link"), P.task_dirs())

    def test_index_status_is_repaired_from_the_ledger(self):
        R.set_status(
            self.h, "active", scope="generic"
        )  # e.g. a crash before the status was written
        P.sync_index()
        self.assertEqual(self.status(self.h), "probation")


# ==================================================================== CT8: cooldown from the ledger only
class TestCooldown(Base):
    FP = "c" * 64

    def put(self, op, t, **kw):
        lid = L.next_id()
        e = dict({"id": lid, "ts": P._iso(t), "op": op, "scope": "generic"}, **kw)
        with L.LedgerLock():
            L.append_ledger(e)
        return e

    def revert_at(self, day, **kw):
        kw.setdefault("cand_fp", self.FP)
        return self.put("auto-revert", day * DAY, reverted="1", **kw)

    def st(self, day, **kw):
        return P.cooldown_state(
            kw.pop("fp", self.FP), kw.pop("lineage", None), day * DAY, **kw
        )

    def test_ct8_cooldown_30_60_90_cap_and_reset_after_90_clean_days(self):
        self.revert_at(0)
        self.assertFalse(self.st(1)["cooldown"])  # one auto-revert: no cooldown
        self.revert_at(10)  # 2nd within 60 days
        s = self.st(11)
        self.assertEqual((s["cooldown"], s["days"], s["level"]), (True, 30.0, 1))
        self.assertEqual(s["until"], P._iso(40 * DAY))
        self.assertFalse(self.st(40.5)["cooldown"])  # expired
        self.revert_at(45)  # 3rd → 60
        self.assertEqual(
            (self.st(46)["days"], self.st(104)["cooldown"], self.st(106)["cooldown"]),
            (60.0, True, False),
        )
        self.revert_at(110)  # 4th → 90
        self.assertEqual(self.st(111)["days"], 90.0)
        self.revert_at(150)  # 5th → still 90 (cap)
        self.assertEqual(self.st(151)["days"], 90.0)
        self.assertEqual(self.st(241)["level"], 0)  # 90 clean days: reset
        self.revert_at(241)  # first after the reset: no cooldown
        s = self.st(242)
        self.assertEqual((s["cooldown"], s["level"]), (False, 0))
        self.assertTrue(
            self.st(242, fp="d" * 64)["cooldown"] is False
        )  # other candidate

    def test_lineage_counts_and_manual_reverts_or_drift_do_not(self):
        for d in (0, 5, 9):
            self.put(
                "revert", d * DAY, reverts="1", reason="human"
            )  # manual reverts: never counted
        self.assertFalse(self.st(10)["cooldown"])
        self.revert_at(20, cand_fp="e" * 64, lineage="abcdef12")
        self.revert_at(30, cand_fp="f" * 64, lineage="abcdef1234")
        s = self.st(31, fp="0" * 64, lineage="abcdef12")
        self.assertTrue(s["cooldown"], s)
        self.assertFalse(self.st(31, fp="0" * 64, lineage="99999999")["cooldown"])

    def probe(self, situation, run):
        return E.record_failure(
            situation, "Do the expected thing.", "", run, root=self.root
        )["probe"]

    def test_early_exit_only_with_a_probe_from_a_run_after_the_last_revert(self):
        self.revert_at(0)
        self.revert_at(10)
        old = self.probe(
            "The lead skipped the review of the second wave.", "fleet-a-lead-1"
        )
        new = self.probe(
            "The lead merged before the integration test passed.", "fleet-b-lead-2"
        )
        times = {"fleet-a-lead-1": 9 * DAY, "fleet-b-lead-2": 12 * DAY}
        self.assertTrue(self.st(13, probes=[old["id"]], run_times=times)["cooldown"])
        s = self.st(13, probes=[old["id"], new["id"]], run_times=times)
        self.assertEqual((s["cooldown"], s["early_exit"]), (False, new["id"]))
        # caller-supplied fields are ignored: only the stored, hash-verified probe counts
        forged = [
            {"id": old["id"], "source_run": "fleet-b-lead-2"},
            {"id": "fail-" + "9" * 16, "source_run": "fleet-b-lead-2"},
            "fail-" + "8" * 16,
        ]
        self.assertTrue(self.st(13, probes=forged, run_times=times)["cooldown"])
        # run times read from the task folders
        self.add_run("alpha", "t", "fleet-b-lead-2", 12 * DAY)
        self.assertFalse(self.st(13, probes=[new])["cooldown"])
        # a logged occurrence of the old probe in a run after the revert lifts it too
        P.record_occurrence(old["id"], "fleet-b-lead-2", 12 * DAY)
        s = self.st(13, probes=[old["id"]], run_times=times)
        self.assertEqual((s["cooldown"], s["early_exit"]), (False, old["id"]))

    def test_original_rule_and_its_refinement_share_one_lineage(self):
        # rule A applied in probation and auto-reverted; later A is back (by hand) and a refinement of A is
        # auto-reverted too: 2 auto-reverts of the same lineage → cooldown, from the ledger only
        fp_a, fp_b = "a" * 64, "b" * 64
        h0 = R.rule_hash(RULE_A)
        r, out = self.apply(SKILL_REL, RULE_A, "--probation", "--cand-fp", fp_a)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(
            P.auto_revert(out["ledger"]["id"], {"text": "regression x"})["status"],
            "reverted",
        )
        r, _ = self.apply(SKILL_REL, RULE_A)  # A back, outside probation
        self.assertEqual(r.returncode, 0, r.stdout)
        old = rtext(self.skill)
        refined = RULE_A.rstrip(".") + " in the same session."
        self.n += 1
        d = os.path.join(self.tmp, f"c{self.n}.diff")
        write(d, L.unified_diff(SKILL_REL, old, old.replace(RULE_A, refined)))
        r = self.cli(
            [APPLY, d, "--run", "fleet-x-lead-1", "--reason", "refine", "--op", "edit"]
            + ["--relation", f"refines:{h0[:12]}", "--probation", "--cand-fp", fp_b]
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        e1 = json.loads(r.stdout)["ledger"]
        self.assertEqual(
            P.auto_revert(e1["id"], {"text": "regression y"})["status"], "reverted"
        )
        ars = self.ops("auto-revert")
        self.assertEqual(
            [(a["lineage"], a["rule_hashes"]) for a in ars],
            [(None, [h0]), (h0[:12], [R.rule_hash(refined)])],
        )
        self.assertEqual(P.lineage_root(R.rule_hash(refined)), h0)
        for fp, lineage in ((None, h0[:12]), (fp_a, None), (fp_b, None)):
            s = P.cooldown_state(fp, lineage)
            self.assertTrue(s["cooldown"], (fp, lineage, s))
            self.assertEqual(len(s["reverts"]), 2)
        self.assertFalse(P.cooldown_state("0" * 64, None)["cooldown"])

    def test_gate_holds_candidates_in_cooldown_without_judge(self):
        self.revert_at(0)
        self.revert_at(1)
        c = {
            "scope": "generic",
            "target": REF_REL,
            "cand_fp": self.FP,
            "relation": "new",
        }
        to_judge, held = P.gate_candidates([c], now=2 * DAY)
        self.assertEqual((to_judge, held[0]["status"]), ([], "cooldown"))


# ==================================================================== CT8: one probation per always-loaded file
class TestProbationSlots(Base):
    def test_ct8_one_probation_per_always_loaded_file_second_waits_without_judge(self):
        e = self.apply_probation(RULE_A)
        self.assertEqual(P.probation_holders(SKILL_REL), [e["id"]])
        self.assertFalse(P.probation_slot_free(SKILL_REL))
        self.assertTrue(P.probation_slot_free(REF_REL))  # not always loaded
        before = rbytes(self.skill)
        r, out = self.apply(
            SKILL_REL, RULE_B, "--probation"
        )  # 2nd probation on the same file: refused
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("in probation", out["error"])
        r, out = self.apply(
            SKILL_REL, RULE_B
        )  # any other change to it waits too (its revert would drift)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(rbytes(self.skill), before)
        r, _ = self.apply(REF_REL, RULE_B, "--probation")  # other files are free
        self.assertEqual(r.returncode, 0, r.stdout)
        cands = [
            {
                "n": 1,
                "scope": "generic",
                "target": SKILL_REL,
                "rule": RULE_B,
                "new": RULE_B,
                "cand_fp": "1" * 64,
            },
            {
                "n": 2,
                "scope": "project:alpha",
                "target": "alpha/LESSONS.md",
                "rule": RULE_B,
                "new": RULE_B,
            },
        ]
        calls = []

        def fake_judge(cs, **kw):
            calls.append([c["n"] for c in cs])
            return {"verdicts": [], "usage": {}, "prompt_chars": 0, "dry_run": False}

        with mock.patch.object(J, "judge", fake_judge):
            res = P.judge_gated(
                cands, batch_stamp="b1", batch_runs=[], budget_left=None
            )
            self.assertEqual(
                calls, [[2]]
            )  # the waiting candidate never reaches the judge
            self.assertEqual(
                [(h["n"], h["status"]) for h in res["held"]], [(1, "waiting")]
            )
            calls.clear()
            res = P.judge_gated(
                cands[:1], batch_stamp="b2", batch_runs=[], budget_left=None
            )
            self.assertEqual(
                (calls, res["held"][0]["status"]), ([], "waiting")
            )  # no judge call at all
        # once the probation ends the slot is free again: the first of two candidates goes, the second waits
        P.activate(e)
        two = [dict(cands[0]), dict(cands[0], n=3, cand_fp="3" * 64)]
        to_judge, held = P.gate_candidates(two)
        self.assertEqual(
            ([c["n"] for c in to_judge], [h["n"] for h in held]), ([1], [3])
        )
        r, _ = self.apply(SKILL_REL, RULE_B, "--probation")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_probation_needs_a_generic_single_file_diff_and_stores_txn(self):
        r, out = self.apply(SKILL_REL, RULE_A, "--probation", "--txn", "bad id!")
        self.assertEqual(r.returncode, 2)
        e = self.apply_probation(
            RULE_A, SKILL_REL, "--txn", "promo-1", "--failure-probe", "fail-" + "0" * 16
        )
        self.assertEqual(
            (e["probation"], e["txn"], e["failure_probe"]),
            (True, "promo-1", "fail-" + "0" * 16),
        )

    def test_collect_emits_one_diff_per_generic_candidate_for_an_always_loaded_file(
        self,
    ):
        T = os.path.join(self.tmp, "T")
        write(os.path.join(T, "progress.md"), "# Progress\n")
        retro = load(os.path.join(BIN, "fleet-retro"), "fleet_retro_probation_t")
        props = [
            {
                "scope": "generic",
                "rule": "r",
                "why": "w",
                "skill_step": "s",
                "target": SKILL_REL,
                "edit": {"old": "", "new": x},
            }
            for x in (RULE_A, RULE_B)
        ]
        ans = (
            "```json\n"
            + json.dumps({"verdict": "proposals", "proposals": props})
            + "\n```"
        )
        res = retro.collect(T, "fleet-T-lead-1", ans, None, self.root, [])
        self.assertEqual((res["proposals"], len(res["diffs"])), (2, 2))
        self.assertEqual([c["behavioural"] for c in res["candidates"]], [True, True])
        for d, rule in zip(res["diffs"], (RULE_A, RULE_B)):
            added = [
                x
                for x in rtext(d).splitlines()
                if x.startswith("+") and not x.startswith("+++")
            ]
            self.assertEqual(added, ["+" + rule])

    def test_collect_splits_generic_candidates_for_any_rule_file(self):
        # a generic target that is NOT always loaded: still one diff (one ledger entry) per behavioural candidate
        T = os.path.join(self.tmp, "T2")
        write(os.path.join(T, "progress.md"), "# Progress\n")
        retro = load(os.path.join(BIN, "fleet-retro"), "fleet_retro_probation_t2")
        props = [
            {
                "scope": "generic",
                "rule": "r",
                "why": "w",
                "skill_step": "s",
                "target": REF_REL,
                "edit": {"old": "", "new": x},
            }
            for x in (RULE_A, RULE_B)
        ]
        ans = (
            "```json\n"
            + json.dumps({"verdict": "proposals", "proposals": props})
            + "\n```"
        )
        res = retro.collect(T, "fleet-T-lead-2", ans, None, self.root, [])
        self.assertEqual((res["proposals"], len(res["diffs"])), (2, 2), res)
        for d, rule in zip(res["diffs"], (RULE_A, RULE_B)):
            added = [
                x
                for x in rtext(d).splitlines()
                if x.startswith("+") and not x.startswith("+++")
            ]
            self.assertEqual(added, ["+" + rule])
        # what is NOT behavioural: project scope, archives, a new reference file without rules
        g = {"scope": "generic", "target": REF_REL}
        self.assertFalse(
            retro.is_behavioural(dict(g, scope="project:alpha"), "", RULE_A)
        )
        self.assertFalse(
            retro.is_behavioural(dict(g, declared_op="archive"), RULE_A, "")
        )
        self.assertFalse(retro.is_behavioural(g, None, "# Notes\n"))
        self.assertTrue(retro.is_behavioural(g, None, "# Notes\n" + RULE_A + "\n"))


# ==================================================================== CT7: promotion txn, crash recovery
class TestPromotionTxn(Base):
    LESSON = "- Run the database migrations in a transaction so a failed step rolls everything back."

    def setUp(self):
        super().setUp()
        own = {
            "alpha": "- Alpha deploys go through the staging cluster first.",
            "beta": "- Beta keeps its fixtures in JSON.",
        }
        for p in ("alpha", "beta"):
            write(
                os.path.join(self.tmp, p, "LESSONS.md"),
                f"# Lessons\n{own[p]}\n{self.LESSON}\n",
            )

    def lessons(self, p):
        return rtext(os.path.join(self.tmp, p, "LESSONS.md"))

    def archives(self, p):
        return [
            e for e in L.read_ledger(L.project_scope(p)) if e.get("op") == "archive"
        ]

    def test_ct7_crash_between_generic_apply_and_project_archive_recovers_idempotently(
        self,
    ):
        (plan,) = P.promotion_plan()
        self.assertEqual(plan["projects"], ["alpha", "beta"])
        self.assertEqual(len(plan["copies"]), 2)
        rec = P.begin_txn(plan)
        self.assertEqual(P.begin_txn(plan), rec)  # idempotent intent
        self.assertEqual(
            json.loads(self.cli([LINT, "--json", "--no-report"]).stdout)[
                "promote_to_generic"
            ],
            1,
        )
        g = self.apply_probation(self.LESSON, REF_REL, "--txn", plan["txn"])
        res = P.recover_txns()
        self.assertEqual(
            res[0]["state"], "probation"
        )  # never archive the copies before the generic is active
        self.assertIn(self.LESSON, self.lessons("alpha"))
        P.activate(g)
        # crash after archiving the first copy only
        first = P._archive_copy(rec, rec["copies"][0], g["id"])
        self.assertEqual(first["status"], "archived")
        self.assertNotIn(self.LESSON, self.lessons("alpha"))
        self.assertIn(self.LESSON, self.lessons("beta"))
        res = P.recover_txns()
        self.assertEqual(res[0]["state"], "done", res)
        self.assertEqual(
            {c["project"]: c.get("already", False) for c in res[0]["copies"]},
            {"alpha": True, "beta": False},
        )
        for p in ("alpha", "beta"):
            self.assertNotIn(self.LESSON, self.lessons(p))
            self.assertIn(p.capitalize(), self.lessons(p))
            (a,) = self.archives(p)
            self.assertEqual(a["txn"], plan["txn"])
            arch = os.path.join(self.tmp, a["archive_file"])
            self.assertIn(self.LESSON, rtext(arch))  # archive moves, never deletes
        res = P.recover_txns()  # a second run is a no-op
        self.assertEqual(res[0]["state"], "done")
        self.assertEqual([len(self.archives(p)) for p in ("alpha", "beta")], [1, 1])
        self.assertIn(self.LESSON, rtext(os.path.join(self.root, REF_REL)))

    def test_two_copies_in_one_project_file_are_each_archived_after_a_crash(self):
        variant = self.LESSON.rstrip(".") + "!"
        write(
            os.path.join(self.tmp, "alpha", "LESSONS.md"),
            f"# Lessons\n- Alpha deploys go through the staging cluster first.\n"
            f"{self.LESSON}\n{variant}\n",
        )
        (plan,) = P.promotion_plan()
        alpha = [c for c in plan["copies"] if c["project"] == "alpha"]
        self.assertEqual(
            sorted(c["block"] for c in alpha), sorted([self.LESSON, variant])
        )
        rec = P.begin_txn(plan)
        g = self.apply_probation(self.LESSON, REF_REL, "--txn", plan["txn"])
        P.activate(g)
        # crash after archiving the first alpha copy only
        first = next(c for c in rec["copies"] if c["project"] == "alpha")
        self.assertEqual(P._archive_copy(rec, first, g["id"])["status"], "archived")
        self.assertEqual(len(self.archives("alpha")), 1)
        res = P.recover_txns()
        self.assertEqual(res[0]["state"], "done", res)
        by_block = {(c["project"], c["block_sha256"]): c for c in res[0]["copies"]}
        self.assertEqual(len(by_block), 3)
        self.assertEqual(
            sorted(c.get("already", False) for c in res[0]["copies"]),
            [False, False, True],
        )
        for p in ("alpha", "beta"):
            self.assertNotIn(self.LESSON, self.lessons(p))
            self.assertIn(p.capitalize(), self.lessons(p))
        self.assertNotIn(variant, self.lessons("alpha"))
        arch = self.archives("alpha")
        self.assertEqual(len(arch), 2)
        self.assertEqual(len({a["block_sha256"] for a in arch}), 2)
        self.assertEqual(
            sorted(
                rtext(os.path.join(self.tmp, a["archive_file"])).splitlines()[-1]
                for a in arch
            ),
            sorted([self.LESSON, variant]),
        )
        res = P.recover_txns()  # idempotent: nothing archived twice
        self.assertEqual(res[0]["state"], "done")
        self.assertTrue(all(c.get("already") for c in res[0]["copies"]), res)
        self.assertEqual([len(self.archives(p)) for p in ("alpha", "beta")], [2, 1])

    def test_an_archive_of_another_block_does_not_mark_a_copy_done(self):
        variant = self.LESSON.rstrip(".") + "!"
        write(
            os.path.join(self.tmp, "alpha", "LESSONS.md"),
            f"# Lessons\n{self.LESSON}\n{variant}\n",
        )
        (plan,) = P.promotion_plan()
        rec = P.begin_txn(plan)
        g = self.apply_probation(self.LESSON, REF_REL, "--txn", plan["txn"])
        P.activate(g)
        second = [c for c in rec["copies"] if c["project"] == "alpha"][1]
        self.assertEqual(P._archive_copy(rec, second, g["id"])["status"], "archived")
        # the other alpha copy is still in the file: it is not "already archived"
        first = [c for c in rec["copies"] if c["project"] == "alpha"][0]
        r = P._archive_copy(rec, first, g["id"])
        self.assertEqual(r["status"], "archived")
        self.assertFalse(r.get("already", False))
        self.assertNotIn(first["block"], self.lessons("alpha"))

    def test_reverted_generic_aborts_the_txn_and_keeps_the_copies(self):
        (plan,) = P.promotion_plan()
        P.begin_txn(plan)
        g = self.apply_probation(self.LESSON, REF_REL, "--txn", plan["txn"])
        r = self.cli([APPLY, "--revert", g["id"], "--reason", "x"])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(P.recover_txns()[0]["state"], "aborted")
        self.assertIn(self.LESSON, self.lessons("alpha"))
        self.assertEqual(self.archives("alpha"), [])

    def test_two_runs_in_the_same_project_are_not_two_projects(self):
        write(
            os.path.join(self.tmp, "beta", "LESSONS.md"),
            "# Lessons\n- Unrelated beta lesson about logging.\n",
        )
        # the same lesson added by two runs of project alpha
        write(
            os.path.join(self.tmp, "alpha", "LESSONS.md"),
            f"# Lessons\n{self.LESSON}\n{self.LESSON.rstrip('.')}!\n",
        )
        self.assertEqual(P.promotion_plan(), [])
        t1 = self.add_run("alpha", "task1", "fleet-task1-lead-1", 1000)
        t2 = self.add_run("alpha", "task2", "fleet-task2-lead-1", 2000)
        self.assertEqual(P.distinct_projects([t1, t2]), ["alpha"])
        t3 = self.add_run("beta", "task3", "fleet-task3-lead-1", 3000)
        self.assertEqual(P.distinct_projects([t1, t2, t3]), ["alpha", "beta"])


# ==================================================================== metrics
class TestMetrics(Base):
    def test_progress_and_criteria_metrics(self):
        prog = (
            "# Progress\n| id | status | review round |\n|----|----|----|\n| P1 | DONE | r2 |\n| P2 | BLOCKED | r3 |\n"
            "## Events\n- nothing\n## BLOCKED\n- P2: needs a product decision\n"
        )
        T = self.add_run(
            "alpha",
            "t",
            "fleet-t-lead-1",
            100,
            state="failed",
            progress=prog,
            criteria=[{"id": "C1", "passes": True}, {"id": "C2", "passes": False}],
        )
        self.add_run("alpha", "t", "retro-fleet-t-lead-1", 100)  # not a lead run
        m = P.collect_metrics([T], 0, 200)
        self.assertEqual(
            (m["runs"], m["not_done"], m["blocked"], m["rounds"], m["round_rows"]),
            (1, 1, 2, 5, 2),
        )
        self.assertEqual((m["failed_criteria"], m["criteria"]), (1, 2))
        self.assertEqual(m["rates"]["rounds_per_task"], 2.5)
        self.assertEqual(P.collect_metrics([T], 200, 300)["runs"], 0)


if __name__ == "__main__":
    unittest.main()
