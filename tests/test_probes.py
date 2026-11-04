#!/usr/bin/env python3
"""Decision-probe store tests: canonical hash, idempotence, frozen failure probes, sanitization, success bank,
selection and new-evidence rules. No model, no network, temp roots only. Run: python3 tests/test_probes.py (or pytest)."""

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

NOW = 1_700_000_000.0
DAY = 86400
FAKE_TOKEN = "sk-" + "test" * 8  # obviously fake


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-probes-")
        self.root = os.path.join(self.tmp, "fleet")
        os.makedirs(os.path.join(self.root, "skill"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkfail(self, situation="lead skipped the review gate", run="run-a", **kw):
        kw.setdefault("expected", "run the reviewer before merging")
        kw.setdefault("forbidden", "merge without review")
        kw.setdefault("area", "review gate")
        kw.setdefault("now", NOW)
        return E.record_failure(situation, source_run=run, root=self.root, **kw)

    def task(
        self,
        name,
        state="done",
        passes=(True,),
        summary="all green",
        run="fleet-x-lead-010101",
    ):
        T = os.path.join(self.tmp, name)
        write(
            os.path.join(T, "runs", run + ".status.json"), json.dumps({"state": state})
        )
        write(
            os.path.join(T, "criteria.json"),
            json.dumps(
                {
                    "criteria": [
                        {"id": f"C{i}", "passes": p} for i, p in enumerate(passes)
                    ]
                }
            ),
        )
        write(os.path.join(T, "FINAL-SUMMARY.md"), summary)
        write(os.path.join(T, "progress.md"), "# Progress\nreview gate respected\n")
        return T


class TestHash(Base):
    def test_canonical_hash_ignores_key_order_and_whitespace(self):
        a = E.canonical_json({"b": 1, "a": [1, 2], "c": {"y": 1, "x": 2}})
        b = E.canonical_json(
            json.loads('{ "c" : {"x":2,\n "y":1}, "a":[1,2],   "b":1 }')
        )
        self.assertEqual(a, b)
        self.assertNotIn(" ", a)
        h = E.probe_hash("failure", "generic", "s", "e", "f")
        self.assertEqual(h, E.probe_hash("failure", "generic", "s", "e", "f"))
        self.assertNotEqual(h, E.probe_hash("success", "generic", "s", "e", "f"))
        self.assertNotEqual(h, E.probe_hash("failure", "project:acme", "s", "e", "f"))

    def test_stored_hash_matches_content_and_id_is_deterministic(self):
        r = self.mkfail()
        p = r["probe"]
        self.assertEqual(
            p["hash"],
            E.probe_hash(
                p["kind"], p["scope"], p["situation"], p["expected"], p["forbidden"]
            ),
        )
        self.assertEqual(p["id"], "fail-" + p["hash"][:16])
        other = os.path.join(self.tmp, "fleet2")
        os.makedirs(os.path.join(other, "skill"))
        r2 = E.record_failure(
            "lead skipped the review gate",
            "run the reviewer before merging",
            "merge without review",
            "run-a",
            root=other,
            now=NOW + 5,
        )
        self.assertEqual(r2["probe"]["id"], p["id"])


class TestFrozen(Base):
    def test_rerecord_is_idempotent(self):
        r1 = self.mkfail()
        path = os.path.join(self.root, "skill", "evals", r1["probe"]["id"] + ".json")
        before = open(path, "rb").read()
        r2 = self.mkfail(now=NOW + 9999)
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(r2["status"], "exists")
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(len(E.list_probes(self.root)), 1)
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(path))), [".lock", os.path.basename(path)]
        )

    def test_failure_probe_is_frozen_and_correction_is_new(self):
        orig = self.mkfail()["probe"]
        with self.assertRaises(E.ProbeFrozen):
            E.update_probe(orig["id"], expected="something else")
        fixed = self.mkfail(
            expected="run the reviewer and wait for its verdict",
            corrected_from=orig["id"],
        )
        self.assertTrue(fixed["probe_corrected"])
        self.assertNotEqual(fixed["probe"]["id"], orig["id"])
        self.assertEqual(fixed["probe"]["corrected_from"], orig["id"])
        self.assertEqual(
            E.load_probe(orig["id"], self.root), orig
        )  # original untouched
        self.assertEqual(len(E.list_probes(self.root)), 2)
        with self.assertRaises(E.ProbeError):  # correcting an unknown probe is refused
            self.mkfail(expected="x", corrected_from="fail-0123456789abcdef")

    def test_edited_file_is_detected(self):
        p = self.mkfail()["probe"]
        path = os.path.join(self.root, "skill", "evals", p["id"] + ".json")
        d = json.load(open(path))
        d["expected"] = "tampered"
        write(path, json.dumps(d))
        with self.assertRaises(E.ProbeError):
            E.load_probe(p["id"], self.root)
        self.assertEqual(E.list_probes(self.root), [])


class TestSanitize(Base):
    def test_fake_tokens_and_ansi_removed_and_capped(self):
        text = f"\x1b[31mERROR\x1b[0m used {FAKE_TOKEN} and Bearer abcdefgh12345678\r\nnext\x00 line"
        out = E.sanitize(text, 1500)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("abcdefgh12345678", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x00", out)
        self.assertIn("ERROR", out)
        long = E.sanitize("word " * 2000, 200)
        self.assertLessEqual(len(long), 200)
        r = self.mkfail(situation=f"token leak {FAKE_TOKEN}")
        self.assertNotIn(FAKE_TOKEN, json.dumps(r["probe"]))

    def test_policy_cap_applies(self):
        r = self.mkfail(situation="x " * 4000)
        self.assertLessEqual(len(r["probe"]["situation"]), 1500)

    def test_unredactable_secret_is_refused(self):
        with self.assertRaises(E.ProbeRefused):
            self.mkfail(situation="saw -----BEGIN RSA PRIVATE KEY----- in the log")
        with (
            mock.patch.object(E, "redact", lambda t: t),
            self.assertRaises(E.ProbeRefused),
        ):
            E.sanitize("password: hunter2hunter2xyz", 1500)
        blob = "A1b2" * 12  # 48-char opaque blob
        with self.assertRaises(E.ProbeRefused):
            self.mkfail(situation=f"saw {blob} in the log")
        with mock.patch.object(
            E, "redact", lambda t: t
        ):  # redaction that cannot help: secret survives -> refused
            with self.assertRaises(E.ProbeRefused):
                E.sanitize(f"key {FAKE_TOKEN}", 1500)
        self.assertEqual(E.list_probes(self.root), [])

    def test_command_text_is_inert_data_never_executed(self):
        marker = os.path.join(self.tmp, "pwned")
        cmd = f"$ touch {marker}; rm -rf / `touch {marker}` $(touch {marker})"
        boom = mock.Mock(side_effect=AssertionError("must not execute anything"))
        with (
            mock.patch.object(subprocess, "run", boom),
            mock.patch.object(subprocess, "Popen", boom),
            mock.patch.object(subprocess, "check_output", boom),
            mock.patch.object(os, "system", boom),
            mock.patch.object(os, "popen", boom),
        ):
            r = self.mkfail(situation=cmd, forbidden=f"touch {marker}")
            E.select("generic", "review gate", NOW, root=self.root)
        self.assertIn("touch", r["probe"]["situation"])
        self.assertFalse(os.path.exists(marker))
        boom.assert_not_called()
        self.assertNotIn(
            "subprocess", open(E.__file__).read().replace("no subprocess", "")
        )

    def test_scope_validated(self):
        with self.assertRaises(E.ProbeRefused):
            self.mkfail(scope="../etc")
        r = self.mkfail(scope="project:acme")
        self.assertEqual(r["probe"]["project"], "acme")


class TestSuccessBank(Base):
    def test_success_only_from_done_with_all_criteria_passing(self):
        ok = E.record_success(self.task("t-ok"), root=self.root, now=NOW)
        self.assertEqual(ok["probe"]["kind"], "success")
        self.assertEqual(ok["probe"]["source_run"], "fleet-x-lead-010101")
        for name, kw in {
            "t-fail": dict(state="failed"),
            "t-blocked": dict(state="incomplete"),
            "t-onefail": dict(passes=(True, False)),
            "t-nonepass": dict(passes=()),
            "t-null": dict(passes=(True, None)),
        }.items():
            with self.assertRaises(E.NotSuccess, msg=name):
                E.record_success(self.task(name, **kw), root=self.root, now=NOW)
        self.assertEqual(len(E.list_probes(self.root, kind="success")), 1)
        again = E.record_success(self.task("t-ok"), root=self.root, now=NOW + 1)
        self.assertFalse(again["created"])

    def test_success_text_capped_and_sanitized(self):
        T = self.task("t-big", summary=f"done {FAKE_TOKEN} " + "yy " * 7000)
        p = E.record_success(T, root=self.root, now=NOW)["probe"]
        self.assertLessEqual(len(p["situation"]), 1500)
        self.assertNotIn(FAKE_TOKEN, p["situation"])

    def test_cli_record_and_show(self):
        T = self.task("t-cli")
        cli = os.path.join(BIN, "fleet-skill-eval")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run(
            [
                sys.executable,
                cli,
                "--root",
                self.root,
                "--now",
                "2023-11-14T22:13:20Z",
                "record-success",
                "--task-dir",
                T,
                "--area",
                "review gate",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = json.loads(r.stdout)["probe"]["id"]
        r = subprocess.run(
            [sys.executable, cli, "--root", self.root, "show", pid],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(json.loads(r.stdout)["id"], pid)
        bad = self.task("t-bad", state="failed")
        r = subprocess.run(
            [
                sys.executable,
                cli,
                "--root",
                self.root,
                "record-success",
                "--task-dir",
                bad,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("error", json.loads(r.stdout))


class TestSelect(Base):
    def success(self, name, area, age_days, scope="generic"):
        T = self.task(name, summary=f"summary {name}", run=f"fleet-{name}-lead-010101")
        return E.record_success(
            T, scope=scope, area=area, root=self.root, now=NOW - age_days * DAY
        )["probe"]

    def test_select_failure_plus_at_most_two_recent_related_successes(self):
        f = self.mkfail()["probe"]
        s1 = self.success("s1", "review gate", 1)
        s2 = self.success("s2", "review gate merge", 3)
        self.success("s3", "review gate", 5)  # third related: dropped (max 2)
        self.success("old", "review gate", 30)  # outside the 7-day window
        self.success("other", "database migration", 0)  # unrelated area
        r = E.select("generic", "review gate", NOW, root=self.root)
        self.assertEqual(r["failure"]["id"], f["id"])
        ids = [p["id"] for p in r["successes"]]
        self.assertEqual(
            ids, [s1["id"], s2["id"]]
        )  # same overlap: newest first, third related dropped
        for p in r["successes"]:
            self.assertEqual(p["kind"], "success")
            self.assertGreaterEqual(E._epoch(p["created_at"]), NOW - 7 * DAY)
        self.assertEqual(
            r, E.select("generic", "review gate", NOW, root=self.root)
        )  # deterministic

    def test_select_prefers_overlap_then_newest(self):
        self.mkfail()
        a = self.success("a", "review", 0)
        b = self.success("b", "review gate", 4)
        r = E.select("generic", "review gate", NOW, root=self.root)
        self.assertEqual([p["id"] for p in r["successes"]], [b["id"], a["id"]])

    def test_select_uses_correction_and_failure_id(self):
        orig = self.mkfail()["probe"]
        fixed = self.mkfail(
            expected="wait for the reviewer verdict", corrected_from=orig["id"]
        )["probe"]
        self.assertEqual(
            E.select("generic", "review gate", NOW, root=self.root)["failure"]["id"],
            fixed["id"],
        )
        self.assertEqual(
            E.select("generic", "x", NOW, root=self.root, failure_id=orig["id"])[
                "failure"
            ]["id"],
            orig["id"],
        )
        self.assertIsNone(
            E.select("generic", "unrelated topic", NOW, root=self.root)["failure"]
        )

    def test_project_scope_sees_own_and_generic_successes(self):
        mine = self.success("m", "review gate", 1, scope="project:acme")
        gen = self.success("g", "review gate", 2)
        self.success("o", "review gate", 0, scope="project:other")
        r = E.select("project:acme", "review gate", NOW, root=self.root)
        self.assertEqual({p["id"] for p in r["successes"]}, {mine["id"], gen["id"]})


class TestNewEvidence(Base):
    def test_rules(self):
        cited = self.mkfail(run="run-a")["probe"]
        c = [{"id": cited["id"], "hash": cited["hash"], "source_run": "run-a"}]
        batch = ["run-b", "run-a"]
        other = self.mkfail(situation="a different failure", run="run-b")["probe"]
        self.assertEqual(E.is_new_evidence(other, c, batch), "new")
        self.assertFalse(
            E.is_new_evidence(other, c, ["run-z"])
        )  # run not in the current batch
        self.assertFalse(E.is_new_evidence(cited, c, batch))  # hash already cited
        same_run = self.mkfail(situation="another angle, same run", run="run-a")[
            "probe"
        ]
        self.assertFalse(
            E.is_new_evidence(same_run, c, batch)
        )  # same source_run, not a correction
        same_hash_other_run = dict(cited, source_run="run-b")
        self.assertFalse(
            E.is_new_evidence(same_hash_other_run, c, batch)
        )  # hash not new
        fixed = self.mkfail(
            expected="wait for the verdict", corrected_from=cited["id"], run="run-a"
        )["probe"]
        self.assertEqual(E.is_new_evidence(fixed, c, batch), "probe_corrected")
        self.assertFalse(
            E.is_new_evidence(fixed, c, ["run-b"])
        )  # corrected but run not in batch
        self.assertEqual(E.is_new_evidence(other, [], batch), "new")


class TestHardening(Base):
    def test_select_failure_id_validated(self):
        f = self.mkfail(scope="project:acme")["probe"]
        s = E.record_success(self.task("t-ok"), root=self.root, now=NOW)["probe"]
        with self.assertRaises(E.ProbeRefused):  # success probe as failure
            E.select("generic", "x", NOW, root=self.root, failure_id=s["id"])
        with self.assertRaises(E.ProbeRefused):  # other scope
            E.select("project:other", "x", NOW, root=self.root, failure_id=f["id"])
        with self.assertRaises(E.ProbeRefused):
            E.select("generic", "x", NOW, root=self.root, failure_id=f["id"])
        got = E.select("project:acme", "x", NOW, root=self.root, failure_id=f["id"])
        self.assertEqual(got["failure"]["id"], f["id"])

    def test_sanitize_small_caps(self):
        for cap in (0, 1, 5, 13, 14, 15, 30):
            self.assertLessEqual(len(E.sanitize("word " * 50, cap)), cap, cap)

    def test_symlink_and_fifo_refused(self):
        T = self.task("t-sym")
        real = os.path.join(self.tmp, "elsewhere.md")
        write(real, "outside")
        os.unlink(os.path.join(T, "progress.md"))
        os.symlink(real, os.path.join(T, "progress.md"))
        with self.assertRaises(E.NotSuccess):
            E.success_from_task_dir(T)
        T = self.task("t-fifo")
        os.unlink(os.path.join(T, "criteria.json"))
        os.mkfifo(os.path.join(T, "criteria.json"))
        with self.assertRaises(E.NotSuccess):
            E.success_from_task_dir(T)
        T = self.task("t-linkstatus")
        st = os.path.join(T, "runs", "fleet-x-lead-010101.status.json")
        os.rename(st, real)
        os.symlink(real, st)
        with self.assertRaises(E.NotSuccess):
            E.success_from_task_dir(T)

    def test_oversized_files_bounded(self):
        T = self.task("t-big2")
        write(os.path.join(T, "progress.md"), "ok line\n" * 2_000_000)
        m = E.success_from_task_dir(T)
        self.assertLessEqual(len(m["situation"]), 2000)
        write(os.path.join(T, "criteria.json"), " " * (E.JSON_CAP + 10))
        with self.assertRaises(E.NotSuccess):
            E.success_from_task_dir(T)

    def test_secret_straddling_cut_does_not_leak(self):
        T = self.task("t-straddle", summary="")
        cap = E.CAPS["progress"]
        pre = "x " * ((cap // 3) - 8)
        write(
            os.path.join(T, "progress.md"), pre + FAKE_TOKEN + " tail\n" + "y " * 3000
        )
        m = E.success_from_task_dir(T)
        self.assertNotIn("test" * 3, m["situation"])
        self.assertNotIn("sk-", m["situation"])

    def test_run_name_validated(self):
        T = self.task("t-run")
        for bad in ("../x", "a/b", "..", "", "a b"):
            with self.assertRaises(E.ProbeError, msg=bad):
                E.success_from_task_dir(T, bad)
        self.assertEqual(
            E.success_from_task_dir(T, "fleet-x-lead-010101")["source_run"],
            "fleet-x-lead-010101",
        )

    def test_lock_does_not_follow_symlink(self):
        d = os.path.join(self.root, "skill", "evals")
        os.makedirs(d)
        victim = os.path.join(self.tmp, "victim")
        write(victim, "keep")
        os.symlink(victim, os.path.join(d, ".lock"))
        with self.assertRaises(OSError):
            self.mkfail()
        self.assertEqual(open(victim).read(), "keep")

    def test_more_secret_shapes_refused(self):
        aws_secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
        akia = "AKIA" + "IOSFODNN7EXAMPLE"
        jwt = ".".join(
            ["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJzdWIiOiJ0ZXN0In0", "c2lnbmF0dXJl"]
        )
        for secret in (aws_secret, akia, jwt):
            with mock.patch.object(E, "redact", lambda t: t):
                with self.assertRaises(E.ProbeRefused, msg=secret):
                    E.sanitize(f"saw {secret} here", 1500)
            self.assertNotIn(
                secret, E.redact(f"saw {secret} here") if secret != aws_secret else ""
            )
        E.sanitize(
            "see /home/user/projects/fleet/skill/evals/some/long/path/name here", 1500
        )

    def test_ansi_split_token_is_redacted_before_cut(self):
        split = "sk-test\x1b[0mtest" + "test" * 6
        T = self.task("ansi", summary="x " * 400 + split + " tail")
        got = E.success_from_task_dir(T)["situation"]
        self.assertNotIn("sk-test", got)
        self.assertNotIn("testtest", got)
        with mock.patch.object(E, "redact", lambda t: t):
            with self.assertRaises(E.ProbeRefused):
                E.sanitize("key sk-test\x1b[1mtest" + "test" * 6 + " end", 1500)
        self.assertEqual(E.normalize("a\x1b[31mb\x00c"), "abc")

    def test_git_sha_not_refused_but_other_long_runs_are(self):
        sha = "a1b2c3d4e5" * 4
        self.assertIn(sha, E.sanitize(f"commit {sha} merged", 1500))
        sha256 = "0123456789abcdef" * 4
        self.assertIn(sha256, E.sanitize(f"digest {sha256}", 1500))
        for bad in ("a1b2c3d4e5" * 4 + "f", "A1b2c3d4e5" * 4, "z" * 44):
            with self.assertRaises(E.ProbeRefused, msg=bad):
                E.sanitize(f"x {bad} y", 1500)

    def test_runs_symlink_refused(self):
        T = self.task("lnk")
        real = os.path.join(self.tmp, "lnk", "realruns")
        os.rename(os.path.join(T, "runs"), real)
        os.symlink(real, os.path.join(T, "runs"))
        with self.assertRaises(E.ProbeRefused):
            E.success_from_task_dir(T)

    def test_probe_file_symlink_and_oversize_skipped(self):
        a = self.mkfail()["probe"]
        d = E.evals_dir(self.root)
        path = os.path.join(d, a["id"] + ".json")
        with open(path) as f:
            body = f.read()
        os.remove(path)
        other = os.path.join(self.tmp, "elsewhere.json")
        write(other, body)
        os.symlink(other, path)
        with self.assertRaises(E.ProbeError):
            E.load_probe(a["id"], self.root)
        self.assertEqual(E.list_probes(self.root), [])
        os.remove(path)
        write(path, body + " " * (E.JSON_CAP + 1))
        with self.assertRaises(E.ProbeError):
            E.load_probe(a["id"], self.root)


if __name__ == "__main__":
    unittest.main()
