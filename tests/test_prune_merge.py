#!/usr/bin/env python3
"""Pruning, merge, archive, budgets and the merge-guard (PLAN F2) + ledger decision_id/cand_fp. No model, no network;
temporary roots only. Run: python3 tests/test_prune_merge.py (or pytest)."""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "fleet", "bin")
sys.path.insert(0, BIN)
import fleet_rules_index as R  # noqa: E402
import fleet_skill_lib as L  # noqa: E402

RETRO, APPLY, LINT = (
    os.path.join(BIN, n)
    for n in ("fleet-retro", "fleet-skill-apply", "fleet-skill-lint")
)
ENV_KEYS = (
    "FLEET_ROOT",
    "ORQ_ROOT",
    "FLEET_RETRO_POLICY",
    "FLEET_LESSONS_NAME",
    "FLEET_LESSONS",
    "ORQUESTRA_HOME",
)

A1 = "- Always run `make all` before the tests so generated files exist when the suite starts."
A2 = "- Run `make all` before the tests so that generated files exist when the suite starts."
B1 = "- Pin the Node version in `.nvmrc` so CI and laptops build alike."
B2 = "- Pin the Node version in the `.nvmrc` file so that CI and laptops build alike."
C1 = "- Keep database migrations idempotent so a retry after a crash cannot corrupt data."
C2 = "- Keep every database migration idempotent so that a retry after a crash cannot corrupt data."
D1 = "- Tag release candidates with `git tag -s` before publishing the package archive."
D2 = (
    "- Tag release candidates using `git tag -s` before publishing the package archive."
)
REF = "skill/fleet-sdd/references/tests.md"


def merged(a):
    """A merged block: the first version, reworded a little (a block equal to a source would only be a deletion)."""
    return a.rstrip(".") + ", in every project."


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


def prop(
    rule,
    rel=REF,
    scope="generic",
    old="",
    new="- A perfectly valid rule that is long enough here.",
    **kw,
):
    p = {
        "scope": scope,
        "rule": rule,
        "why": "w",
        "skill_step": "s",
        "target": rel,
        "edit": {"old": old, "new": new},
    }
    if scope == "project":
        p["project"] = "acme"
    p.update(kw)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.mkdtemp(prefix="fleet-prune-test-")
        self.root = os.path.join(self.tmp, "fleet")
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["FLEET_ROOT"], os.environ["ORQ_ROOT"] = self.root, self.tmp
        write(
            os.path.join(self.root, "skill", "fleet-sdd", "SKILL.md"),
            "# fleet-sdd\n\n## Rules\n- Never touch a test without saying so.\n",
        )
        self.ref = os.path.join(self.root, REF)
        self.T = os.path.join(self.tmp, "T")
        write(os.path.join(self.T, "progress.md"), "# Progress\n")
        self.retro = load(os.path.join(BIN, "fleet-retro"), "fleet_retro_prune_t")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, args, **env):
        e = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            VIGIA_ATIVOS=os.path.join(self.tmp, "ativos"),
        )
        e.update(env)
        return subprocess.run(args, capture_output=True, text=True, env=e)

    def validate(self, props, projects=()):
        os.makedirs(os.path.join(self.tmp, "acme"), exist_ok=True)
        return self.retro.validate(props, self.root, projects)

    def collect(self, props, projects=()):
        """The real retro collect (validate + diffs + retro.md) → (result, retro.md text)."""
        os.makedirs(os.path.join(self.tmp, "acme"), exist_ok=True)
        ans = (
            "```json\n"
            + json.dumps({"verdict": "proposals", "proposals": props})
            + "\n```"
        )
        res = self.retro.collect(
            self.T, "fleet-T-lead-1", ans, None, self.root, list(projects)
        )
        return res, open(res["retro"]).read()

    def apply_line(self, retro_md, n=0):
        """The suggested `fleet-skill-apply` command of the n-th diff in retro.md, as an argv list."""
        lines = [
            x.strip()[len("apply (after review): ") :]
            for x in retro_md.splitlines()
            if x.strip().startswith("apply (after review)")
        ]
        return [w.replace("<why>", "test") for w in shlex.split(lines[n])]

    def ledger(self, path=None):
        p = path or os.path.join(self.root, "skill", "ledger.jsonl")
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []

    def diff_file(self, rel, new_text, name="m.diff"):
        old = open(os.path.join(self.root, rel)).read()
        p = os.path.join(self.tmp, name)
        write(p, L.unified_diff(rel, old, new_text))
        return p


class TestMerge(Base):
    def setUp(self):
        super().setUp()
        self.text = f"# Tests\n\n{A1}\n{A2}\n- Keep fixtures small and local.\n"
        write(self.ref, self.text)

    def test_merging_near_dups_reduces_lines_through_retro_and_apply(self):
        p = prop("merge dup", op="merge", sources=[A1, A2], new=merged(A1))
        res, md = self.collect([p])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        self.assertIn("- op: merge", md)
        cmd = self.apply_line(md)
        self.assertIn("--op", cmd)
        self.assertEqual(cmd[cmd.index("--op") + 1], "merge")
        r = self.run_cli(cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = open(self.ref).read()
        self.assertLess(len(after.splitlines()), len(self.text.splitlines()))
        self.assertEqual(
            after, f"# Tests\n\n{merged(A1)}\n- Keep fixtures small and local.\n"
        )
        self.assertEqual(self.ledger()[-1]["declared_op"], "merge")

    def test_ct3_ratio_below_08_refused_in_retro_and_in_apply(self):
        short = "- Run `make all` first."
        p = prop("collapse", op="merge", sources=[A1, A2], new=short)
        acc, rej, _ = self.validate([p])
        self.assertEqual(acc, [])
        self.assertIn("merge-guard", rej[0][1])
        self.assertIn("minimum is 0.8x", rej[0][1])
        diff = self.diff_file(
            REF, f"# Tests\n\n{short}\n- Keep fixtures small and local.\n"
        )
        before = rbytes(self.ref)
        for extra in (
            ["--op", "merge"],
            [],
        ):  # explicit op and the default are both guarded
            r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"] + extra)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("merge-guard", json.loads(r.stdout)["error"])
        self.assertEqual(rbytes(self.ref), before)
        self.assertEqual(self.ledger(), [])

    def test_ct3_dropped_backtick_literal_refused_in_retro_and_apply_unless_in_drops(
        self,
    ):
        new = "- Always run the whole suite before the tests so that generated files exist when everything starts."
        p = prop("lose cmd", op="merge", sources=[A1, A2], new=new)
        acc, rej, _ = self.validate([p])
        self.assertEqual(acc, [])
        self.assertIn("`make all`", rej[0][1])  # the refusal names the missing literal
        diff = self.diff_file(
            REF, f"# Tests\n\n{new}\n- Keep fixtures small and local.\n"
        )
        before = rbytes(self.ref)
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x", "--op", "merge"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("`make all`", json.loads(r.stdout)["error"])
        self.assertEqual(rbytes(self.ref), before)
        # with an explicit drops the same change passes in retro (visible in the report and the command) and in apply
        p["drops"] = ["make all"]
        res, md = self.collect([p])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        self.assertIn("drops (literals removed on purpose): `make all`", md)
        cmd = self.apply_line(md)
        self.assertIn("--drops", cmd)
        r = self.run_cli(cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.ledger()[-1]["drops"], ["make all"])

    def test_edit_without_op_is_an_edit_and_guarded_in_retro_and_apply(self):
        write(self.ref, f"# Tests\n\n{A1}\n- Keep fixtures small and local.\n")
        for p in (
            prop("shrink", op="edit", old=A1, new="- Run `make all`."),
            prop("shrink", old=A1, new="- Run `make all`."),  # no op: still an edit
            prop("lose", old=A1, new=A1.replace("`make all`", "the build target")),
        ):
            acc, rej, _ = self.validate([p])
            self.assertEqual((acc, len(rej)), ([], 1))
            self.assertIn("merge-guard", rej[0][1])
        ok = prop("reword", old=A1, new=A1.replace("Always run", "Run"))
        res, md = self.collect([ok])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        self.assertNotIn("--op legacy", md)  # retro never emits legacy
        cmd = self.apply_line(md)
        self.assertEqual(cmd[cmd.index("--op") + 1], "edit")
        self.assertIn("- op: edit (none declared", md)
        r = self.run_cli(cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the same shrink as a hand-made diff: refused in apply unless a human passes --op legacy (recorded)
        write(self.ref, self.text)
        diff = self.diff_file(REF, self.text.replace(A1, "- Run `make all`."))
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("merge-guard", json.loads(r.stdout)["error"])
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x", "--op", "legacy"])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.ledger()[-1]["declared_op"], "legacy")

    def test_merge_equal_to_a_source_passes_in_retro_and_apply(self):
        p = prop("x", op="merge", sources=[A1, A2], new=A1)
        res, md = self.collect([p])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        cmd = self.apply_line(md)
        diff = cmd[1]
        self.assertTrue(os.path.exists(diff + ".ops.json"))
        # the diff alone is a pure deletion: without the proposal metadata apply refuses it
        bare = os.path.join(self.tmp, "bare.diff")
        shutil.copy(diff, bare)
        r = self.run_cli([APPLY, bare, "--run", "r", "--reason", "x", "--op", "merge"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("only deletes", json.loads(r.stdout)["error"])
        # metadata that does not reproduce the diff is ignored (diff-derived guard)
        fake = os.path.join(self.tmp, "fake.ops.json")
        write(
            fake,
            json.dumps({"ops": [{"file": REF, "op": "edit", "old": A2, "new": A1}]}),
        )
        r = self.run_cli(
            [APPLY, bare, "--run", "r", "--reason", "x", "--op", "merge", "--ops", fake]
        )
        self.assertEqual(r.returncode, 1, r.stdout)
        r = self.run_cli(cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            open(self.ref).read(),
            f"# Tests\n\n{A1}\n- Keep fixtures small and local.\n",
        )

    def test_merge_equal_to_a_source_still_needs_the_other_sources_literals(self):
        other = "- Run `make check` before the tests so that generated files exist when it starts."
        write(self.ref, f"# Tests\n\n{A1}\n{other}\n")
        p = prop("x", op="merge", sources=[A1, other], new=A1)
        acc, rej, _ = self.validate([p])
        self.assertEqual(acc, [])
        self.assertIn("`make check`", rej[0][1])

    def test_guard_is_per_change_region_in_retro_and_apply(self):
        text = f"# Tests\n\n{A1}\n\n{C1}\n"
        write(self.ref, text)
        big = (
            C1 + " Also keep each migration reversible and documented in the changelog."
        )
        shrink = prop("s", old=A1, new="- Run `make all` first.")
        grow = prop("g", old=C1, new=big)
        acc, rej, _ = self.validate([shrink, grow])
        self.assertEqual([p["rule"] for p, _ in acc], ["g"])
        self.assertIn("merge-guard", rej[0][1])
        # apply: the growth of the second block does not compensate the over-shortened first one
        diff = self.diff_file(
            REF, text.replace(A1, "- Run `make all` first.").replace(C1, big)
        )
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x", "--op", "edit"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("minimum is 0.8x", json.loads(r.stdout)["error"])
        self.assertEqual(open(self.ref).read(), text)

    def test_guard_is_per_file_in_apply(self):
        other_rel = "skill/fleet-sdd/references/build.md"
        other = os.path.join(self.root, other_rel)
        write(other, "# Build\n")
        lost = A1.replace("`make all`", "the build target")
        d1 = L.unified_diff(REF, self.text, self.text.replace(A1, lost))
        d2 = L.unified_diff(
            other_rel, "# Build\n", "# Build\n- Use `make all` to build.\n"
        )
        diff = os.path.join(self.tmp, "two.diff")
        write(diff, d1 + d2)
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"])
        self.assertEqual(r.returncode, 1, r.stdout)
        out = json.loads(r.stdout)
        self.assertIn("`make all`", out["error"])
        self.assertEqual(out["file"], REF)
        self.assertEqual(open(other).read(), "# Build\n")

    def test_secret_like_drops_evidence_and_reason_are_refused(self):
        sec = "api_key=ABCDEF1234567890"
        p = prop("d", op="merge", sources=[A1, A2], new=merged(A1), drops=[sec])
        acc, rej, _ = self.validate([p])
        self.assertEqual(acc, [])
        self.assertIn("secret-like", rej[0][1])
        diff = self.diff_file(REF, self.text.replace(A2 + "\n", ""))
        base = [APPLY, diff, "--run", "r", "--op", "archive", "--archive-kind", "error"]
        for extra in (
            ["--reason", "x", "--evidence", sec],
            ["--reason", "why " + sec, "--evidence", "e"],
        ):
            r = self.run_cli(base + extra)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("secret-like", json.loads(r.stdout)["error"])
        self.assertEqual(open(self.ref).read(), self.text)
        self.assertEqual(self.ledger(), [])

    def test_op_schema_errors(self):
        bad = [
            prop("x", op="rename"),
            prop("x", op="add", old=A1, new=A2),
            prop("x", op="edit", old="", new=A2),
            prop(
                "x",
                op="merge",
                sources=[A1, "- not in the file at all, nowhere."],
                new=A1,
            ),
            prop(
                "x", op="merge", sources=[A1, A2], new=A1, relation="supersedes:abcdef"
            ),
            prop("x", op="merge", sources="nope", new=A1),
        ]
        acc, rej, _ = self.validate(bad)
        self.assertEqual((acc, len(rej)), ([], len(bad)))

    def test_merge_guard_pure_function(self):
        ok, _ = L.merge_guard(
            ["- a `x` rule of some size here"],
            "- a `x` rule of some size here",
            [],
            0.8,
        )
        self.assertTrue(ok)
        ok, why = L.merge_guard(
            ["- a `x` rule of some size here"], "- a rule of some size here", [], 0.8
        )
        self.assertFalse(ok)
        self.assertIn("`x`", why)
        ok, _ = L.merge_guard(
            ["- a `x` rule of some size here"], "- a rule of some size here", ["x"], 0.8
        )
        self.assertTrue(ok)
        self.assertTrue(L.merge_guard([], "", [], 0.8)[0])


class TestGuardPerBlock(Base):
    """The guard works per rule block, on the diff (apply) and on the original text (retro); the ops sidecar can only
    relax the verified-merge case."""

    GROW = " Also keep each migration reversible and documented in the changelog."

    def setUp(self):
        super().setUp()
        self.text = f"# Tests\n\n{A1}\n{C1}\n- Keep fixtures small and local.\n"
        write(self.ref, self.text)

    def refused_in_retro_and_apply(self, new_pair, needle):
        old = f"{A1}\n{C1}"
        for p in (
            prop("both", old=old, new=new_pair),
            prop("both", op="edit", old=old, new=new_pair),
        ):
            acc, rej, _ = self.validate([p])
            self.assertEqual(acc, [], rej)
            self.assertIn(needle, rej[0][1])
        diff = self.diff_file(REF, self.text.replace(old, new_pair))
        for extra in ([], ["--op", "edit"]):
            r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"] + extra)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn(needle, json.loads(r.stdout)["error"])
        self.assertEqual(open(self.ref).read(), self.text)
        self.assertEqual(self.ledger(), [])

    def test_adjacent_bullet_growth_does_not_hide_a_stubbed_rule(self):
        self.refused_in_retro_and_apply(f"- Stub.\n{C1}{self.GROW}", "minimum is 0.8x")

    def test_literal_moved_into_the_adjacent_bullet_is_refused(self):
        lost = A1.replace("`make all`", "the full build")
        self.refused_in_retro_and_apply(
            f"{lost}\n{C1} Run `make all` first.", "`make all`"
        )

    def test_adjacent_bullets_each_kept_pass(self):
        new_pair = f"{A1.replace('Always run', 'Run')}\n{C1}{self.GROW}"
        acc, rej, _ = self.validate([prop("both", old=f"{A1}\n{C1}", new=new_pair)])
        self.assertEqual((len(acc), rej), (1, []))
        diff = self.diff_file(REF, self.text.replace(f"{A1}\n{C1}", new_pair))
        r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"])
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_tampered_ops_file_cannot_weaken_the_apply_guard(self):
        big = (
            "- Before every release run `make check` on a clean checkout, read the whole failure log, fix the "
            "first error before the others, rerun the suite twice to catch flaky tests, compare the coverage "
            "report with the previous release, update the changelog with every user-visible change, and only "
            "then tag the release candidate and ask a second reviewer to sign it off."
        )
        write(self.ref, f"# Tests\n\n{big}\n")
        small = "- Before every release run `make check` on a clean checkout and fix what fails."
        self.assertLess(len(small), 0.3 * len(big))
        after = f"# Tests\n\n{small}\n"
        diff = self.diff_file(REF, after)
        # 12 small edits that replay to the same result, each one shrinking a piece by < 20%
        cut = [big[i * len(big) // 12 : (i + 1) * len(big) // 12] for i in range(12)]
        ops = [
            {"file": REF, "op": "edit", "old": c, "new": c[: len(c) * 9 // 10]}
            for c in cut
        ]
        ops += [
            {"file": REF, "op": "merge", "sources": [big], "new": small}
        ]  # a merge into a too-short block
        for tampered in (ops, ops[:-1]):
            write(diff + ".ops.json", json.dumps({"version": 1, "ops": tampered}))
            for extra in ([], ["--op", "merge"], ["--ops", diff + ".ops.json"]):
                r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"] + extra)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("minimum is 0.8x", json.loads(r.stdout)["error"])
        self.assertEqual(open(self.ref).read(), f"# Tests\n\n{big}\n")
        # a merge claim whose sources the diff does not touch cannot license a deletion either
        diff2 = self.diff_file(REF, "# Tests\n", "d2.diff")
        write(
            diff2 + ".ops.json",
            json.dumps(
                {
                    "ops": [
                        {"file": REF, "op": "merge", "sources": [big], "new": "# Tests"}
                    ]
                }
            ),
        )
        r = self.run_cli([APPLY, diff2, "--run", "r", "--reason", "x", "--op", "merge"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("merge-guard", json.loads(r.stdout)["error"])

    def test_retro_guards_against_the_original_text_and_refuses_overlaps(self):
        write(self.ref, f"# Tests\n\n{A1}\n\n{C1}\n")
        a1b = A1.replace("Always run", "Run").replace(
            " so generated files exist", " so files exist"
        )
        self.assertGreater(len(a1b), 0.8 * len(A1))
        a1c = a1b.replace(" when the suite starts", " when it starts")
        self.assertGreater(len(a1c), 0.8 * len(a1b))
        self.assertLess(len(a1c), 0.8 * len(A1))
        # compounding: the second shrink edits the output of the first one
        acc, rej, _ = self.validate(
            [prop("s1", old=A1, new=a1b), prop("s2", old=a1b, new=a1c)]
        )
        self.assertEqual([p["rule"] for p, _ in acc], ["s1"])
        self.assertIn("original target", rej[0][1])
        # overlap: the second rewrites lines the first one changed (its old text is still in both versions)
        part = "`make all` before the tests"
        acc, rej, _ = self.validate(
            [prop("s1", old=A1, new=a1b), prop("s2", old=part, new=part + " always")]
        )
        self.assertEqual([p["rule"] for p, _ in acc], ["s1"])
        self.assertIn("overlaps candidate 1", rej[0][1])
        # separate blocks: both are guarded independently and pass
        acc, rej, _ = self.validate(
            [prop("s1", old=A1, new=a1b), prop("s2", old=C1, new=C2)]
        )
        self.assertEqual((len(acc), rej), (2, []))

    def test_archives_are_checked_against_the_original_text_and_for_overlaps(self):
        write(self.ref, f"# Tests\n\n{A1}\n\n{C1}\n")
        a1b = A1.replace("Always run", "Run")
        arch = dict(op="archive", new="", archive_reason="error", evidence="wrong")
        # an archive of the output of an earlier edit of the batch
        acc, rej, _ = self.validate(
            [prop("e", old=A1, new=a1b), prop("a", old=a1b, **arch)]
        )
        self.assertEqual([p["rule"] for p, _ in acc], ["e"])
        self.assertIn("original target", rej[0][1])
        # an archive of a block an earlier edit rewrote (its text is unchanged in the current text)
        both = f"{A1}\n\n{C1}"
        e = prop("e", old=both, new=f"{a1b}\n\n{C1}")
        acc, rej, _ = self.validate([e, prop("a", old=C1, **arch)])
        self.assertEqual([p["rule"] for p, _ in acc], ["e"])
        self.assertIn("overlaps candidate 1", rej[0][1])
        # an edit of a block an earlier archive removed
        part = "`make all` before the tests"
        acc, rej, _ = self.validate(
            [prop("a", old=A1, **arch), prop("e", old=part, new=part + " always")]
        )
        self.assertEqual([p["rule"] for p, _ in acc], ["a"])
        # an archive of another block passes
        acc, rej, _ = self.validate(
            [prop("e", old=A1, new=a1b), prop("a", old=C1, **arch)]
        )
        self.assertEqual((len(acc), rej), (2, []))

    def test_two_edits_of_one_multi_line_block_are_refused(self):
        l1 = "- Before a release run `make check` on a clean checkout and read the whole failure log."
        l2 = "  Then fix the first error before the others and rerun the suite twice to catch flaky tests."
        write(self.ref, f"# Tests\n\n{l1}\n{l2}\n\n{C1}\n")
        n1 = "- Before a release run `make check` on a clean checkout, read the log."
        n2 = "  Then fix the first error first and rerun the suite twice for flakes."
        block = len(l1) + len(l2) + 1
        self.assertGreaterEqual(len(n1) + len(l2) + 1, 0.8 * block)
        self.assertGreaterEqual(len(l1) + len(n2) + 1, 0.8 * block)
        self.assertLess(len(n1) + len(n2) + 1, 0.8 * block)
        p1, p2 = prop("p1", old=l1, new=n1), prop("p2", old=l2, new=n2)
        acc, rej, _ = self.validate([p1, p2])
        self.assertEqual([p["rule"] for p, _ in acc], ["p1"])
        self.assertIn("overlaps candidate 1", rej[0][1])
        # the end-of-validate check of the combined change per file catches it on its own too
        saved = self.retro._batch_conflict
        self.retro._batch_conflict = lambda *a, **k: None
        try:
            acc, rej, _ = self.validate([p1, p2])
        finally:
            self.retro._batch_conflict = saved
        self.assertEqual([p["rule"] for p, _ in acc], ["p1"])
        self.assertIn("together with the earlier proposals", rej[0][1])
        self.assertIn("minimum is 0.8x", rej[0][1])
        # edits of different blocks still pass together
        acc, rej, _ = self.validate([p1, prop("c", old=C1, new=C2)])
        self.assertEqual((len(acc), rej), (2, []))

    def test_ops_sidecar_merges_are_honoured_only_with_op_merge_and_recorded(self):
        write(self.ref, f"# Tests\n\n{A1}\n{A2}\n")
        res, md = self.collect([prop("x", op="merge", sources=[A1, A2], new=A1)])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        cmd = self.apply_line(md)
        diff = cmd[1]
        sidecar = rbytes(diff + ".ops.json")
        base = [APPLY, diff, "--run", "r", "--reason", "x"]
        for extra in ([], ["--op", "edit"], ["--op", "add"]):
            r = self.run_cli(base + extra)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("only deletes", json.loads(r.stdout)["error"])
        self.assertEqual(self.ledger(), [])
        r = self.run_cli(cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        e = self.ledger()[-1]
        self.assertEqual(e["ops_sha256"], hashlib.sha256(sidecar).hexdigest())
        self.assertEqual(len(e["guard_merges"]), 1)
        self.assertEqual(e["guard_merges"][0]["file"], REF)
        self.assertEqual(
            e["guard_merges"][0]["new_sha256"],
            hashlib.sha256(A1.encode()).hexdigest(),
        )

    def test_cand_fp_exemption_is_only_a_full_sha256(self):
        diff = self.diff_file(REF, self.text + "- Name fixtures after what they pin.\n")
        for fp in ("0f" * 20, "0f" * 32 + "0", "0F" * 32):
            r = self.run_cli(
                [APPLY, diff, "--run", "r", "--reason", "x", "--cand-fp", fp]
            )
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("secret-like", json.loads(r.stdout)["error"])
        self.assertEqual(self.ledger(), [])

    def test_secret_like_drops_and_cand_fp_are_refused_in_apply(self):
        diff = self.diff_file(REF, self.text + "- Name fixtures after what they pin.\n")
        sec = "api_key=ABCDEF1234567890"
        for extra in (
            ["--drops", json.dumps([sec])],
            ["--cand-fp", sec],
            ["--cand-fp", "ghp_" + "a" * 36],
        ):
            r = self.run_cli([APPLY, diff, "--run", "r", "--reason", "x"] + extra)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("secret-like", json.loads(r.stdout)["error"])
        self.assertEqual(self.ledger(), [])
        r = self.run_cli(
            [APPLY, diff, "--run", "r", "--reason", "x", "--cand-fp", "0f" * 32]
        )
        self.assertEqual(r.returncode, 0, r.stdout)


class TestBudgets(Base):
    LINE = "- Filler lesson number %03d keeps the file near its budget size.\n"

    def lessons(self, n):
        return "# acme\n" + "".join(self.LINE % i for i in range(n))

    def setUp(self):
        super().setUp()
        self.les = os.path.join(self.tmp, "acme", "LESSONS.md")

    def test_policy_budgets_are_the_source_of_the_limits(self):
        pol = L.load_policy()
        self.assertEqual(
            L.budget_of("acme/LESSONS.md", pol), ("file:acme/LESSONS.md", 4000)
        )
        self.assertEqual(L.budget_of("skill/fleet-sdd/SKILL.md", pol)[1], 12000)
        self.assertEqual(L.budget_of(REF, pol)[0], "references")
        self.assertEqual(L.budget_of("skill/fleet-sdd/brief-x.md", pol), (None, None))

    def test_shrinking_an_oversized_legacy_file_passes_and_growing_it_fails(self):
        write(self.les, self.lessons(70))
        self.assertGreater(len(open(self.les).read()), 4000)
        shrink = prop(  # an op-less edit would be merge-guarded: the shrink archives a wrong line
            "s",
            "LESSONS.md",
            "project",
            op="archive",
            old=(self.LINE % 3).rstrip("\n"),
            new="",
            archive_reason="error",
            evidence="line 003 states a wrong size",
        )
        grow = prop("g", "LESSONS.md", "project", new=B1)
        acc, rej, _ = self.validate([shrink], ("acme",))
        self.assertEqual((len(acc), rej), (1, []))
        acc, rej, _ = self.validate([grow], ("acme",))
        self.assertEqual(acc, [])
        self.assertIn("over its ~4000-char cap", rej[0][1])

    def test_growth_over_budget_passes_when_the_same_set_also_shrinks_the_file(self):
        n = next(
            k
            for k in range(40, 80)
            if len(self.lessons(k)) + len(B1) + 1
            > 4000
            >= len(self.lessons(k)) + len(B1) + 1 - len(self.LINE % 1)
        )
        write(self.les, self.lessons(n))
        grow = prop("g", "LESSONS.md", "project", new=B1)
        arch = prop(
            "a",
            "LESSONS.md",
            "project",
            op="archive",
            old=(self.LINE % 5).rstrip("\n"),
            new="",
            archive_reason="redundancy",
            evidence="rule 006 says the same",
        )
        acc, rej, _ = self.validate([grow], ("acme",))
        self.assertEqual(acc, [])
        acc, rej, ch = self.validate([grow, arch], ("acme",))
        self.assertEqual((len(acc), rej), (2, []))
        ((_, new_text),) = [v for v in ch.values()]
        self.assertLessEqual(len(new_text), 4000)

    def test_shrink_ops_do_not_consume_the_addition_quota(self):
        pairs = [(A1, A2), (B1, B2), (C1, C2), (D1, D2)]
        write(self.les, "# acme\n\n" + "".join(f"{a}\n{b}\n" for a, b in pairs))
        merges = [
            prop(
                f"m{i}",
                "LESSONS.md",
                "project",
                op="merge",
                sources=[a, b],
                new=merged(a),
            )
            for i, (a, b) in enumerate(pairs)
        ]
        adds = [
            prop(
                "n1",
                "LESSONS.md",
                "project",
                new="- Review the lockfile diff before approving generated dependency bumps.",
            ),
            prop(
                "n2",
                "LESSONS.md",
                "project",
                new="- Seed the random generator in every snapshot test to keep output stable.",
            ),
            prop(
                "n3",
                "LESSONS.md",
                "project",
                new="- Document each environment variable where the service reads it first.",
            ),
        ]
        acc, rej, _ = self.validate(merges + adds, ("acme",))
        self.assertEqual(
            len(acc), 6, [r for _, r in rej]
        )  # 3 shrinks (their own cap) + 3 additions
        self.assertEqual([p["rule"] for p, _ in rej], ["m3"])
        self.assertIn("shrink ops", rej[0][1])


class TestArchive(Base):
    NOTES = f"# Notes\n\n## Build\n{A1}\n\n## Old\n{A2}\n- Name feature flags after the area, never after a person.\n"

    def setUp(self):
        super().setUp()
        self.notes = os.path.join(
            self.root, "skill", "fleet-sdd", "references", "notes.md"
        )
        self.rel = "skill/fleet-sdd/references/notes.md"
        write(self.notes, self.NOTES)

    def archive_prop(self, **kw):
        base = dict(
            op="archive",
            old=A2,
            new="",
            archive_reason="redundancy",
            evidence="the Build rule says the same",
        )
        base.update(kw)
        return prop("archive dup", self.rel, **base)

    def archive_cmd(self):
        res, md = self.collect([self.archive_prop()])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        cmd = self.apply_line(md)
        self.assertEqual(cmd[cmd.index("--op") + 1], "archive")
        return cmd

    def archived_files(self):
        d = os.path.join(self.root, "skill", "archive")
        return (
            sorted(f for f in os.listdir(d) if f.endswith(".md"))
            if os.path.isdir(d)
            else []
        )

    def test_ct4_archive_then_revert_restores_exact_bytes(self):
        before = rbytes(self.notes)
        h = R.rule_hash(A2)
        r = self.run_cli(self.archive_cmd())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertNotIn("index_error", out)
        self.assertNotIn(A2, open(self.notes).read())
        files = self.archived_files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^\d{8}-\d{6}-1-.*\.md$")  # <stamp>-<slug>.md
        meta, block = L.archive_unpack(
            rbytes(os.path.join(self.root, "skill", "archive", files[0]))
        )
        self.assertEqual(block, (A2 + "\n").encode())  # the exact bytes
        self.assertEqual(
            (meta["reason_kind"], meta["file"], meta["anchor"], meta["ledger_id"]),
            ("redundancy", self.rel, "Old", "1"),
        )
        self.assertTrue(meta["evidence"] and meta["run"])
        e = self.ledger()[-1]
        self.assertEqual((e["op"], e["archive_kind"]), ("archive", "redundancy"))
        self.assertEqual(R.load_index(self.root)["rules"][h]["status"], "archived")
        # revert
        r = self.run_cli([APPLY, "--revert", e["id"], "--reason", "undo"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rbytes(self.notes), before)
        self.assertEqual(self.archived_files(), [])
        restored = os.path.join(self.root, "skill", "archive", "restored")
        self.assertEqual(len(os.listdir(restored)), 1)  # moved, never deleted
        self.assertEqual(
            L.archive_unpack(rbytes(os.path.join(restored, os.listdir(restored)[0])))[
                1
            ],
            block,
        )
        led = self.ledger()
        self.assertEqual((led[-1]["op"], led[-1]["reverts"]), ("revert", e["id"]))
        self.assertEqual(R.load_index(self.root)["rules"][h]["status"], "active")
        r = self.run_cli([APPLY, "--revert", e["id"]])
        self.assertEqual(r.returncode, 1)
        self.assertIn("already reverted", r.stdout)

    def test_revert_interrupted_after_the_restore_finishes_without_reinserting(self):
        before = rbytes(self.notes)
        r = self.run_cli(self.archive_cmd())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lid = self.ledger()[-1]["id"]
        # crash simulation: the restore step wrote the file, then the process died before moving the archive
        with open(self.notes, "wb") as f:
            f.write(before)
        self.assertEqual(len(self.archived_files()), 1)
        r = self.run_cli([APPLY, "--revert", lid])  # the plain retry finishes the job
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rbytes(self.notes), before)  # not inserted twice
        self.assertEqual(self.archived_files(), [])
        restored = os.path.join(self.root, "skill", "archive", "restored")
        self.assertEqual(len(os.listdir(restored)), 1)
        e = self.ledger()[-1]
        self.assertEqual(
            (e["op"], e["reverts"], e["placement"]), ("revert", lid, "already-restored")
        )
        self.assertEqual(
            R.load_index(self.root)["rules"][R.rule_hash(A2)]["status"], "active"
        )
        r = self.run_cli([APPLY, "--revert", lid, "--force"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(rbytes(self.notes), before)

    def test_revert_after_drift_needs_force_and_places_the_block_by_context(self):
        r = self.run_cli(self.archive_cmd())
        self.assertEqual(r.returncode, 0, r.stdout)
        write(
            self.notes,
            open(self.notes)
            .read()
            .replace("# Notes\n", "# Notes\n\nIntro line that was added later.\n", 1),
        )
        lid = self.ledger()[-1]["id"]
        r = self.run_cli([APPLY, "--revert", lid])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.archived_files()), 1)
        r = self.run_cli([APPLY, "--revert", lid, "--force"])
        self.assertEqual(r.returncode, 0, r.stdout)
        txt = open(self.notes).read()
        self.assertIn("Intro line that was added later.", txt)
        self.assertEqual(txt.count(A2), 1)
        self.assertEqual(txt.index("## Old\n") + len("## Old\n"), txt.index(A2))

    def test_project_scope_archive_and_revert(self):
        les = os.path.join(self.tmp, "acme", "LESSONS.md")
        write(
            les,
            f"# acme\n\n{A1}\n{A2}\n- Keep migrations idempotent so retries are safe after a crash.\n",
        )
        before = rbytes(les)
        p = prop(
            "dup",
            "LESSONS.md",
            "project",
            op="archive",
            old=A2,
            new="",
            archive_reason="redundancy",
            evidence="same as the first",
        )
        res, md = self.collect([p], ("acme",))
        self.assertEqual(res["proposals"], 1, md)
        r = self.run_cli(self.apply_line(md))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        adir = os.path.join(self.tmp, "acme", "lessons-archive")
        self.assertEqual(len([f for f in os.listdir(adir) if f.endswith(".md")]), 1)
        led = self.ledger(os.path.join(self.tmp, "acme", "lessons-ledger.jsonl"))
        self.assertEqual(led[-1]["op"], "archive")
        r = self.run_cli([APPLY, "--revert", led[-1]["id"], "--scope", "project:acme"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(rbytes(les), before)
        self.assertEqual(len(os.listdir(os.path.join(adir, "restored"))), 1)

    def test_old_rule_never_contradicted_is_not_archived(self):
        keep = "- Name feature flags after the area, never after a person."
        os.utime(self.notes, (time.time() - 400 * 86400,) * 2)  # very old
        cases = [
            self.archive_prop(
                old=keep, archive_reason="age", evidence="untouched for a year"
            ),
            self.archive_prop(old=keep, archive_reason="", evidence="old"),
            self.archive_prop(old=keep, evidence=""),
            self.archive_prop(old=keep, archive_reason="error", evidence="   "),
            self.archive_prop(
                old=keep
            ),  # redundancy claimed, but no other rule overlaps it
            self.archive_prop(new="- replacement"),  # an archive never writes text
        ]
        acc, rej, _ = self.validate(cases)
        self.assertEqual((acc, len(rej)), ([], len(cases)), rej)
        self.assertIn("age only names a candidate", rej[0][1])
        self.assertIn("no other active rule overlaps", rej[4][1])
        # the lint only reports old entries (it names candidates); without --archive it moves nothing
        self.assertEqual(self.run_cli([LINT, "--days", "30"]).returncode, 0)
        self.assertEqual(rbytes(self.notes), self.NOTES.encode())
        # with supersession/error + evidence it is allowed (the model owns the claim, the ledger keeps the evidence)
        ok = self.archive_prop(
            old=keep,
            archive_reason="error",
            evidence="the rule caused a wrong flag name in 2 projects",
        )
        acc, rej, _ = self.validate([ok])
        self.assertEqual((len(acc), rej), (1, []))

    def test_retro_refuses_an_archive_that_is_not_whole_rule_blocks(self):
        multi = f"# Notes\n\n{A1}\n  Continued: the suite reads them at start.\n{B1}\n"
        cases = [
            (self.NOTES, "so that generated"),  # a fragment inside a line
            (self.NOTES, A2[: len(A2) // 2]),  # the start of a line
            (multi, A1),  # the first line of a multi-line block
            (multi, "  Continued: the suite reads them at start."),  # its continuation
        ]
        for text, old in cases:
            write(self.notes, text)
            self.assertEqual(text.count(old), 1, old)
            acc, rej, _ = self.validate(
                [self.archive_prop(old=old, archive_reason="error")]
            )
            self.assertEqual(acc, [], old)
            self.assertIn("whole rule block", rej[0][1])
        # the whole multi-line block is accepted and its diff applies cleanly
        write(self.notes, multi)
        whole = f"{A1}\n  Continued: the suite reads them at start."
        res, md = self.collect([self.archive_prop(old=whole, archive_reason="error")])
        self.assertEqual((res["proposals"], res["rejected"]), (1, 0), md)
        r = self.run_cli(self.apply_line(md))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(open(self.notes).read(), f"# Notes\n\n{B1}\n")
        self.assertEqual(len(self.archived_files()), 1)

    def test_archive_apply_needs_kind_and_evidence_and_a_pure_deletion(self):
        diff = self.diff_file(self.rel, self.NOTES.replace(A2 + "\n", ""))
        r = self.run_cli(
            [APPLY, diff, "--run", "r", "--reason", "x", "--op", "archive"]
        )
        self.assertEqual(r.returncode, 2)
        edit = self.diff_file(self.rel, self.NOTES.replace(A2, "- changed"), "e.diff")
        r = self.run_cli(
            [
                APPLY,
                edit,
                "--run",
                "r",
                "--reason",
                "x",
                "--op",
                "archive",
                "--archive-kind",
                "error",
                "--evidence",
                "e",
            ]
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("delete exactly one contiguous block", r.stdout)
        self.assertEqual(self.archived_files(), [])
        self.assertEqual(rbytes(self.notes), self.NOTES.encode())


class TestLint(Base):
    def test_lint_json_shape_and_no_report(self):
        sdd = os.path.join(self.root, "skill", "fleet-sdd")
        write(os.path.join(sdd, "references", "a.md"), f"# A\n{A1}\n{A2}\n")
        old = os.path.join(sdd, "references", "old-topic.md")
        write(
            old,
            "# Old\n- Something nobody references from the skill any more at all.\n",
        )
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        r = self.run_cli([LINT, "--json", "--no-report", "--skill-cap", "20"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIsNone(out["report"])
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "skill", "lint-report.md"))
        )
        f = out["findings"]
        self.assertEqual(
            set(f),
            {
                "near_duplicates",
                "duplicates",
                "over_budget",
                "old_entries",
                "promote_to_generic",
            },
        )
        nd = f["near_duplicates"][0]
        self.assertEqual(set(nd), {"ratio", "a", "b"})
        self.assertEqual(set(nd["a"]), {"file", "line", "text"})
        self.assertGreaterEqual(nd["ratio"], 0.88)
        self.assertEqual(f["over_budget"][0]["file"], "skill/fleet-sdd/SKILL.md")
        self.assertEqual(set(f["over_budget"][0]), {"file", "chars", "budget"})
        self.assertEqual(f["over_budget"][0]["budget"], 20)
        self.assertEqual(set(f["old_entries"][0]), {"file", "days", "kind"})
        self.assertTrue(f["old_entries"][0]["file"].endswith("old-topic.md"))
        # plain run: same summary without findings, report written (unchanged behaviour)
        out = json.loads(self.run_cli([LINT]).stdout)
        self.assertNotIn("findings", out)
        self.assertTrue(
            os.path.exists(os.path.join(self.root, "skill", "lint-report.md"))
        )

    def test_lint_budget_defaults_come_from_the_policy(self):
        write(
            os.path.join(self.root, "skill", "retro-policy.json"),
            json.dumps({"budgets": {"skill_md_chars": 10}}),
        )
        out = json.loads(self.run_cli([LINT, "--json", "--no-report"]).stdout)
        self.assertEqual(out["findings"]["over_budget"][0]["budget"], 10)

    def test_batch_prompt_carries_the_lint_findings_only_when_there_are_some(self):
        T = os.path.join(self.tmp, "acme", "t1")
        write(os.path.join(T, "FINAL-SUMMARY.md"), "status: DONE\n")
        write(
            os.path.join(T, "runs", "fleet-t-lead-1.status.json"),
            json.dumps(
                {"state": "done", "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            ),
        )
        r = self.run_cli([RETRO, "--batch", "--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("acme/t1", r.stdout)
        self.assertNotIn("Housekeeping findings", r.stdout)
        write(self.ref, f"# T\n{A1}\n{A2}\n")
        r = self.run_cli([RETRO, "--batch", "--dry-run"])
        self.assertIn("Housekeeping findings", r.stdout)
        self.assertIn("near-duplicate", r.stdout)
        self.assertIn("op merge", r.stdout)
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "skill", "lint-report.md")),
            "dry-run writes nothing",
        )

    def test_prompt_documents_the_ops(self):
        txt = self.retro.render(self.retro.RULES + self.retro.FORMAT)
        for needle in (
            '"op"',
            "archive_reason",
            "evidence",
            "sources",
            "Age only names a candidate",
        ):
            self.assertIn(needle, txt)


class TestLedgerFields(Base):
    def test_decision_id_and_cand_fp_are_stored_and_optional(self):
        write(self.ref, f"# Tests\n{A1}\n")
        d1 = self.diff_file(
            REF, f"# Tests\n{A1}\n- Keep fixtures small and local.\n", "d1.diff"
        )
        r = self.run_cli(
            [
                APPLY,
                d1,
                "--run",
                "r",
                "--reason",
                "x",
                "--decision",
                "dec-123",
                "--cand-fp",
                "ab" * 32,
            ]
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        e = self.ledger()[-1]
        self.assertEqual((e["decision_id"], e["cand_fp"]), ("dec-123", "ab" * 32))
        d2 = self.diff_file(
            REF,
            open(self.ref).read()
            + "- Name fixtures after the behaviour they pin down.\n",
            "d2.diff",
        )
        r = self.run_cli([APPLY, d2, "--run", "r", "--reason", "x"])
        self.assertEqual(r.returncode, 0, r.stdout)
        e2 = self.ledger()[-1]
        self.assertNotIn("decision_id", e2)
        self.assertNotIn("cand_fp", e2)
        self.assertEqual(len(L.read_ledger()), 2)  # old-style entries still read
        r = self.run_cli([APPLY, "--revert", e2["id"]])
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
