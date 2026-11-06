#!/usr/bin/env python3
"""fleet-run sizing gate and justified security/judgment (no LLM). Run: python3 tests/test_fleet_sizing.py"""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "fleet", "bin", "fleet-run")
_ld = importlib.machinery.SourceFileLoader("fleet_run", RUN)
R = importlib.util.module_from_spec(importlib.util.spec_from_loader(_ld.name, _ld))
_ld.exec_module(R)


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


class SizingGate(unittest.TestCase):
    def ts(self, n, **extra):
        return {"tasks": [{"id": str(i), "files": ["a.py"], **extra} for i in range(n)]}

    def test_sizes(self):
        self.assertEqual(R.Sizing.problems("P", self.ts(1), None), [])
        self.assertTrue(R.Sizing.problems("P", self.ts(2), None))
        self.assertTrue(R.Sizing.problems("P", {"tasks": [{"id": "1", "files": list("abcdef")}]}, None))
        self.assertTrue(R.Sizing.problems("P", self.ts(1), 60))
        self.assertEqual(R.Sizing.problems("M", self.ts(6), 120), [])
        self.assertTrue(R.Sizing.problems("M", self.ts(7), None))
        self.assertIn("split", R.Sizing.problems("G", self.ts(9), None)[0])
        self.assertEqual(R.Sizing.problems("G", self.ts(8), 240), [])
        self.assertTrue(R.Sizing.problems("G", self.ts(8), 300))

    def test_judgment_and_security_need_reasons(self):
        self.assertTrue(R.Sizing.problems("M", self.ts(2, judgment=True), None))
        self.assertTrue(R.Sizing.problems("M", self.ts(2, security=True), None))
        self.assertEqual(R.Sizing.problems("M", self.ts(2, judgment=True, judgment_reason="x",
                                                       security=True, security_reason="y"), None), [])

    def test_judgment_share_warns(self):
        ts = self.ts(6)
        for t in ts["tasks"][:3]:
            t.update(judgment=True, judgment_reason="r")
        self.assertTrue(R.Sizing.warnings(ts))
        ts["tasks"][2]["judgment"] = False
        self.assertEqual(R.Sizing.warnings(ts), [])


class SecurityRoles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.T = os.path.join(self.tmp.name, "T"); self.repo = os.path.join(self.tmp.name, "r")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        write(os.path.join(self.T, "plan.md"), "Plan with auth token, http input and network words.")
        write(os.path.join(self.T, "criteria.json"), json.dumps({"criteria": [{"id": "C1", "command": "python3 -m pytest"}]}))
        self.env = dict(os.environ, VIGIA_BASE=os.path.join(self.tmp.name, "v"), VIGIA_WEBHOOK_ENV="")

    def tearDown(self):
        self.tmp.cleanup()

    def run_fr(self, tasks, *extra):
        write(os.path.join(self.T, "tasks.json"), json.dumps({"tasks": tasks}))
        return subprocess.run([RUN, "--task-dir", self.T, "--task", "t", "--repo", self.repo, "--complexity", "M",
                               "--lead", "claude", "--dry-run", *extra], capture_output=True, text=True, env=self.env)

    def test_keywords_no_longer_enable_security_but_final_pass_runs(self):
        r = self.run_fr([{"id": "1", "files": ["a"], "security": True, "security_reason": "parses input"},
                         {"id": "2", "files": ["b"]}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sec = json.loads(r.stdout)["security"]
        self.assertEqual(sec["tasks"], ["1"])
        self.assertTrue(sec["final"])

    def test_security_yes_needs_reasons(self):
        r = self.run_fr([{"id": "1", "files": ["a"]}, {"id": "2", "files": ["b"]}], "--security", "yes")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("security_reason", r.stdout)


if __name__ == "__main__":
    unittest.main()
