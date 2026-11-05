#!/usr/bin/env python3
"""OSS port tests: portability grep, install.sh fresh/re-install (local edits and reserved files kept).
No model, no network; temporary roots only. Run: python3 tests/test_oss_port.py (or pytest)."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
LOOP_TOOLS = (
    "fleet-retro",
    "fleet-retro-cycle",
    "fleet-skill-apply",
    "fleet-skill-lint",
    "fleet-skill-judge",
    "fleet-skill-eval",
    "fleet_skill_lib.py",
    "fleet_judge_lib.py",
    "fleet_eval_lib.py",
    "fleet_probation.py",
    "fleet_rules_index.py",
)
BAD = re.compile(r"/workspace|/home/|\bJP\b|painel")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def install(home):
    env = dict(os.environ, ORQUESTRA_HOME=home, HOME=home)
    env.pop("VIGIA_BASE", None)
    return subprocess.run(
        ["sh", os.path.join(REPO, "install.sh")],
        env=env,
        capture_output=True,
        text=True,
    )


class Portability(unittest.TestCase):
    def test_no_hardcoded_paths_or_names(self):
        hits = []
        for top in ("fleet", "bin", "prompts", "docs", "install.sh", "skills"):
            root = os.path.join(REPO, top)
            walk = (
                [(os.path.dirname(root), [], [top])]
                if os.path.isfile(root)
                else os.walk(root)
            )
            for d, _, files in walk:
                for fn in files:
                    p = os.path.join(d, fn)
                    try:
                        text = open(p, encoding="utf-8").read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if BAD.search(line):
                            hits.append("%s:%d" % (os.path.relpath(p, REPO), i))
        self.assertEqual(hits, [])

    def test_changelog_has_v020(self):
        with open(os.path.join(REPO, "CHANGELOG.md")) as f:
            self.assertIn("## [0.2.0]", f.read())


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "orq")
        self.fleet = os.path.join(self.home, "fleet")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_install(self):
        r = install(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        for t in LOOP_TOOLS:
            p = os.path.join(self.fleet, "bin", t)
            self.assertTrue(os.path.isfile(p), t)
            if not t.endswith(".py"):
                self.assertTrue(os.access(p, os.X_OK), t)
        self.assertTrue(
            os.path.isfile(os.path.join(self.fleet, "skill", "retro-policy.json"))
        )
        self.assertTrue(os.path.isfile(os.path.join(self.home, "retro-routine.md")))
        with open(os.path.join(self.fleet, "skill", ".install-baseline.json")) as f:
            base = json.load(f)
        self.assertIn("skill/fleet-sdd/SKILL.md", base)
        self.assertIn("skill/retro-policy.json", base)
        shadow = os.path.join(REPO, "fleet", "bin", "fleet-retro-shadow")
        if os.path.isfile(shadow):
            self.assertTrue(
                os.access(
                    os.path.join(self.fleet, "bin", "fleet-retro-shadow"), os.X_OK
                )
            )

    def test_reinstall_keeps_edits_and_reserved(self):
        self.assertEqual(install(self.home).returncode, 0)
        skill = os.path.join(self.fleet, "skill", "fleet-sdd", "SKILL.md")
        pol = os.path.join(self.fleet, "skill", "retro-policy.json")
        other = os.path.join(self.fleet, "skill", "fleet-sdd", "brief-fixer.md")
        with open(skill, "a") as f:
            f.write("\n- learned rule\n")
        with open(pol, "w") as f:
            f.write('{"owner_name": "Ana"}\n')
        # an upstream-changed but locally untouched file is updated
        with open(other, "w") as f:
            f.write("stale\n")
        # ... as if "stale" were the version installed earlier (baseline = its hash)
        bp = os.path.join(self.fleet, "skill", ".install-baseline.json")
        with open(bp) as f:
            base = json.load(f)
        base["skill/fleet-sdd/brief-fixer.md"] = sha(other)
        with open(bp, "w") as f:
            json.dump(base, f)
        reserved = {
            os.path.join(self.fleet, "skill", "judge-log.jsonl"): b'{"a":1}\n',
            os.path.join(self.fleet, "skill", "rules-index.json"): b"{}\n",
            os.path.join(self.fleet, "skill", "retro-state.json"): b'{"x":1}\n',
            os.path.join(self.fleet, "skill", "ledger.jsonl"): b"l\n",
            os.path.join(self.fleet, "skill", "archive", "old.md"): b"old\n",
            os.path.join(self.fleet, "skill", "evals", "e.json"): b"{}\n",
            os.path.join(self.fleet, "skill", "proposals", "p.diff"): b"d\n",
            os.path.join(self.fleet, "skill", "retro-batches", "b.json"): b"{}\n",
            os.path.join(self.home, "proj", "LESSONS.md"): b"# mine\n",
            os.path.join(self.fleet, "skill", "LESSONS.md"): b"# mine2\n",
            os.path.join(self.home, "learnings.md"): b"# learned\n",
        }
        for p, data in reserved.items():
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        edited, edited_pol = sha(skill), sha(pol)
        r = install(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sha(skill), edited)
        self.assertEqual(sha(pol), edited_pol)
        up = skill + ".upstream"
        self.assertTrue(os.path.isfile(up))
        self.assertEqual(
            sha(up), sha(os.path.join(REPO, "fleet", "skill", "fleet-sdd", "SKILL.md"))
        )
        self.assertTrue(os.path.isfile(pol + ".upstream"))
        self.assertIn(".upstream", r.stdout)
        for p, data in reserved.items():
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data, p)
        self.assertEqual(
            sha(other),
            sha(os.path.join(REPO, "fleet", "skill", "fleet-sdd", "brief-fixer.md")),
        )
        self.assertFalse(os.path.exists(other + ".upstream"))
        # idempotent: a third run changes nothing
        r = install(self.home)
        self.assertEqual(sha(skill), edited)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_resolved_edit_clears_upstream(self):
        self.assertEqual(install(self.home).returncode, 0)
        skill = os.path.join(self.fleet, "skill", "fleet-sdd", "SKILL.md")
        with open(skill, "a") as f:
            f.write("x\n")
        install(self.home)
        self.assertTrue(os.path.isfile(skill + ".upstream"))
        with (
            open(
                os.path.join(REPO, "fleet", "skill", "fleet-sdd", "SKILL.md"), "rb"
            ) as s,
            open(skill, "wb") as d,
        ):
            d.write(s.read())
        install(self.home)
        self.assertFalse(os.path.exists(skill + ".upstream"))


class SuiteUnderTmpHome(unittest.TestCase):
    def test_other_suites_with_orquestra_home_tmp(self):
        me = os.path.basename(__file__)
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ORQUESTRA_HOME=os.path.join(tmp, "orq"))
            for fn in sorted(os.listdir(HERE)):
                if fn.startswith("test_") and fn.endswith(".py") and fn != me:
                    r = subprocess.run(
                        [sys.executable, os.path.join(HERE, fn)],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(r.returncode, 0, fn + "\n" + r.stderr[-800:])


class OneResolver(unittest.TestCase):
    def test_same_root_for_all_tools_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            shutil.copytree(
                os.path.join(REPO, "fleet"),
                os.path.join(home, "fleet"),
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            code = (
                "import importlib.machinery as m, importlib.util as u, sys\n"
                "b = sys.argv[1] + '/fleet/bin/'\n"
                "sys.path.insert(0, b)\n"
                "out = []\n"
                "for n in ('fleet-run', 'fleet-retro', 'fleet-retro-cycle'):\n"
                "    l = m.SourceFileLoader('x' + n.replace('-', '_'), b + n)\n"
                "    mod = u.module_from_spec(u.spec_from_loader(l.name, l))\n"
                "    l.exec_module(mod)\n"
                "    out.append(mod.ORQUESTRA_HOME if n == 'fleet-run' else mod.L.orq_root())\n"
                "print('\\n'.join(out))\n"
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("ORQ_ROOT", "ORQUESTRA_HOME", "FLEET_ROOT", "VIGIA_BASE")
            }
            env["HOME"] = os.path.join(home, "otherhome")
            r = subprocess.run(
                [sys.executable, "-c", code, home],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            self.assertEqual(r.stdout.split(), [home] * 3)


class NeverShipReserved(unittest.TestCase):
    def test_source_tree_reserved_names_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            os.makedirs(src)
            for n in ("bin", "fleet", "docs", "prompts"):
                shutil.copytree(
                    os.path.join(REPO, n),
                    os.path.join(src, n),
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            for n in ("install.sh", "webhook.env.example"):
                shutil.copy(os.path.join(REPO, n), os.path.join(src, n))
            sk = os.path.join(src, "fleet", "skill")
            planted = [
                "LESSONS.md",
                "learnings.md",
                os.path.join("fleet-sdd", "LESSONS.md"),
            ]
            for rel in planted:
                with open(os.path.join(sk, rel), "w") as f:
                    f.write("source copy\n")
            home = os.path.join(tmp, "home")
            env = dict(os.environ, ORQUESTRA_HOME=home, HOME=home)
            env.pop("VIGIA_BASE", None)
            for _ in range(2):
                r = subprocess.run(
                    ["sh", os.path.join(src, "install.sh")],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
            for rel in planted:
                p = os.path.join(home, "fleet", "skill", rel)
                self.assertFalse(os.path.exists(p), rel)
                self.assertFalse(os.path.exists(p + ".upstream"), rel)
            with open(
                os.path.join(home, "fleet", "skill", ".install-baseline.json")
            ) as f:
                base = json.load(f)
            for k in base:
                self.assertNotIn(os.path.basename(k), ("LESSONS.md", "learnings.md"))


if __name__ == "__main__":
    unittest.main()
