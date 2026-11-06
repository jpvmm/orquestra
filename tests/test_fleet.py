#!/usr/bin/env python3
"""Fleet tests without any LLM: fleet-wt (DAG/waves, commit-free snapshots, junk excluded, integration and conflict) and
fleet-run --dry-run (fleet.json not written; agents.json with each role's model; webhook defaults; publishing
commands disallowed). Run: python3 tests/test_fleet.py   (temporary repos and VIGIA_BASE only; nothing is launched)"""
import json, os, subprocess, tempfile, unittest, warnings
warnings.simplefilter("ignore", ResourceWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
FLEET = os.path.join(ROOT, "fleet")
BIN = os.path.join(FLEET, "bin")
SCRATCH = tempfile.mkdtemp(prefix="fleet-test-base-")   # VIGIA_BASE for every subprocess: never the real one
WT = os.path.join(BIN, "fleet-wt")
RUN = os.path.join(BIN, "fleet-run")


def clean_env(**extra):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    env.pop("VIGIA_WEBHOOK_ENV", None); env.pop("FLEET_WEBHOOK_ENV", None); env.pop("VIGIA_BIN", None)
    env["VIGIA_BASE"] = SCRATCH
    env.update(extra)
    return env


def sh(cmd, cwd=None, check=True, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env or clean_env())
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} exited {r.returncode}: {r.stdout}{r.stderr}")
    return r


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-test-")
        self.repo = os.path.join(self.tmp, "repo"); self.T = os.path.join(self.tmp, "T")
        os.makedirs(self.repo)
        sh(["git", "init", "-q"], self.repo)
        write(os.path.join(self.repo, "a.py"), "x = 1\n")
        sh(["git", "add", "."], self.repo); sh(["git", "commit", "-qm", "base"], self.repo)

    def tearDown(self):
        sh(["git", "worktree", "prune"], self.repo, check=False)
        subprocess.run(["rm", "-rf", self.tmp])

    def tasks(self, ts, mx=None):
        write(os.path.join(self.T, "tasks.json"), json.dumps({"tasks": ts}))
        if mx: write(os.path.join(self.T, "fleet.json"), json.dumps({"max_parallel": mx}))

    def wt(self, *a, check=True):
        return sh([WT, "--task-dir", self.T, *a], check=check)


class Waves(Base):
    def test_independent_together_same_file_apart(self):
        self.tasks([{"id": "1", "files": ["a.py"]}, {"id": "2", "files": ["b.py"]},
                    {"id": "3", "files": ["a.py"]}, {"id": "4", "files": ["c.py"], "depends_on": ["2"]}])
        o = json.loads(self.wt("waves").stdout)
        self.assertTrue(o["ok"]); self.assertEqual(o["waves"], [["1", "2"], ["3", "4"]])

    def test_max_parallel(self):
        self.tasks([{"id": str(i), "files": [f"f{i}.py"]} for i in range(6)], mx=4)
        o = json.loads(self.wt("waves").stdout)
        self.assertEqual([len(x) for x in o["waves"]], [4, 2])

    def test_cycle_and_missing_dep(self):
        self.tasks([{"id": "1", "files": ["a"], "depends_on": ["2"]}, {"id": "2", "files": ["b"], "depends_on": ["1"]}])
        r = self.wt("waves", check=False); self.assertEqual(r.returncode, 1); self.assertIn("cycle", r.stdout)
        self.tasks([{"id": "1", "files": ["a"], "depends_on": ["9"]}])
        r = self.wt("waves", check=False); self.assertEqual(r.returncode, 1); self.assertIn("does not exist", r.stdout)


class Integration(Base):
    def test_parallel_integrates_without_commit_and_without_pyc(self):
        self.wt("init", "--repo", self.repo)
        integ = os.path.join(self.T, "wt", "integration")
        write(os.path.join(integ, "__pycache__", "a.cpython-313.pyc"), "junk")   # baseline suite produced pyc
        w1 = self.wt("new", "1").stdout.strip(); w2 = self.wt("new", "2").stdout.strip()
        write(os.path.join(w1, "calc.py"), "def add(a, b):\n    return a + b\n")
        write(os.path.join(w1, "__pycache__", "calc.cpython-313.pyc"), "junk")
        write(os.path.join(w2, "sub.py"), "def subtract(a, b):\n    return a - b\n")
        for i in ("1", "2"):
            pk = json.loads(self.wt("package", i).stdout)
            self.assertEqual(pk["files"], ["calc.py" if i == "1" else "sub.py"])
            self.assertNotIn("pyc", open(pk["patch"]).read())
            self.assertFalse(json.loads(self.wt("integrate", i).stdout)["conflict"])
        self.assertTrue(os.path.exists(os.path.join(integ, "calc.py")) and os.path.exists(os.path.join(integ, "sub.py")))
        head = sh(["git", "rev-parse", "HEAD"], integ).stdout.strip()
        self.assertEqual(head, sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip())   # no commit
        final = json.loads(self.wt("package", "final").stdout)
        self.assertEqual(sorted(final["files"]), ["calc.py", "sub.py"])
        # a next-wave task starts from the integrated state
        w3 = self.wt("new", "3").stdout.strip()
        self.assertTrue(os.path.exists(os.path.join(w3, "calc.py")))

    def test_conflict_detected(self):
        self.wt("init", "--repo", self.repo)
        w1 = self.wt("new", "1").stdout.strip(); w2 = self.wt("new", "2").stdout.strip()
        write(os.path.join(w1, "a.py"), "x = 2\n"); write(os.path.join(w2, "a.py"), "x = 3\n")
        self.wt("package", "1"); self.wt("package", "2")
        self.wt("integrate", "1")
        r = self.wt("integrate", "2", check=False)
        self.assertEqual(r.returncode, 1); out = json.loads(r.stdout)
        self.assertTrue(out["conflict"]); self.assertEqual(out["conflicted_files"], ["a.py"])

    def test_outside_declared(self):
        self.tasks([{"id": "1", "files": ["calc.py"]}])
        self.wt("init", "--repo", self.repo)
        w1 = self.wt("new", "1").stdout.strip()
        write(os.path.join(w1, "calc.py"), "y = 1\n"); write(os.path.join(w1, "extra.py"), "z = 1\n")
        self.assertEqual(json.loads(self.wt("package", "1").stdout)["outside_declared"], ["extra.py"])


class FleetRunDry(Base):
    def prepare(self, plan="Add two numbers.", n=2):
        write(os.path.join(self.T, "plan.md"), plan)
        write(os.path.join(self.T, "criteria.json"), json.dumps({"criteria": [{"id": "C1", "command": "true", "passes": False}]}))
        self.tasks([{"id": "1", "files": ["calc.py"]}, {"id": "2", "files": ["sub.py"]}][:n])   # P takes 1 task

    def run_dry(self, *extra, check=True, env=None):
        return sh([RUN, "--task-dir", self.T, "--task", "test", "--repo", self.repo, "--dry-run", *extra], check=check, env=env)

    def test_m_claude(self):
        self.prepare()
        out = json.loads(self.run_dry("--complexity", "M", "--lead", "claude").stdout)
        cmd = out["command"]
        self.assertIn("vigia-run", cmd[0]); self.assertIn("--agent", cmd); self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertFalse(os.path.exists(os.path.join(self.T, "fleet.json")) and "runs" in open(os.path.join(self.T, "fleet.json")).read())
        ag = json.load(open(os.path.join(self.T, ".fleet", "agents.json")))
        self.assertEqual(set(ag), {"team-lead", "dev", "reviewer-claude", "security"})
        self.assertEqual(ag["dev"]["model"], "sonnet")
        self.assertIn(os.path.join(FLEET, "skill", "fleet-sdd", "SKILL.md"), ag["team-lead"]["prompt"])
        self.assertNotIn("@FLEET_HOME@", json.dumps(ag))
        self.assertEqual(cmd[0], os.path.join(ROOT, "bin", "vigia-run"))   # VIGIA_BIN default: bin/ next to fleet/

    def test_claude_runs_cannot_publish(self):
        for extra in (("--complexity", "M", "--lead", "claude"), ("--complexity", "P", "--lead", "claude"),
                      ("--complexity", "P", "--lead", "codex", "--stage", "review")):
            self.prepare(n=1 if "P" in extra else 2)
            cmd = json.loads(self.run_dry(*extra).stdout)["command"]
            i = cmd.index("--disallowedTools")
            for rule in ("Bash(git commit:*)", "Bash(git push:*)", "Bash(git merge:*)", "Bash(git rebase:*)",
                         "Bash(gh:*)", "Bash(rm -rf *)"):
                self.assertIn(rule, cmd[i + 1:], extra)

    def test_codex_lead_adds_vigia_base(self):
        self.prepare()
        cmd = json.loads(self.run_dry("--complexity", "M", "--lead", "codex").stdout)["command"]
        self.assertIn(SCRATCH, cmd)

    def test_criterion_without_command_refused(self):
        self.prepare()
        write(os.path.join(self.T, "criteria.json"), json.dumps({"criteria": [{"id": "C1"}]}))
        r = self.run_dry("--complexity", "M", "--lead", "claude", check=False)
        self.assertEqual(r.returncode, 2); self.assertIn("without a proving command", r.stdout)

    def test_p_codex_dev_has_claude_reviewer(self):
        self.prepare(n=1)
        out = json.loads(self.run_dry("--complexity", "P", "--lead", "codex").stdout)
        self.assertIn("codex-dev", out["command"][0])
        out = json.loads(self.run_dry("--complexity", "P", "--lead", "codex", "--stage", "review").stdout)
        self.assertIn("vigia-run", out["command"][0]); self.assertIn("reviewer-claude", out["command"])
        for f in ("task-1-brief.md", "rev-1-r1.md"):   # dry-run never overwrites the real briefs
            self.assertFalse(os.path.exists(os.path.join(self.T, "briefs", f)))

    def test_p_claude_dev_has_codex_reviewer(self):
        self.prepare(n=1)
        out = json.loads(self.run_dry("--complexity", "P", "--lead", "claude").stdout)
        self.assertIn("vigia-run", out["command"][0]); self.assertIn("dev", out["command"])
        out = json.loads(self.run_dry("--complexity", "P", "--lead", "claude", "--stage", "review").stdout)
        self.assertIn("codex-reviewer", out["command"][0])

    def test_webhook_default_is_orquestra_vigia(self):
        # no VIGIA_WEBHOOK_ENV / FLEET_WEBHOOK_ENV → fleet-run does not set it → Vigia default (Orquestra)
        self.prepare()
        out = json.loads(self.run_dry("--complexity", "M", "--lead", "claude").stdout)
        self.assertIsNone(out["webhook_env"])

    def test_webhook_per_bot_override_and_muted(self):
        self.prepare()
        out = json.loads(self.run_dry("--complexity", "M", "--lead", "claude", env=clean_env(FLEET_WEBHOOK_ENV="/tmp/other-bot/webhook.env")).stdout)
        self.assertEqual(out["webhook_env"], "/tmp/other-bot/webhook.env")
        out = json.loads(self.run_dry("--complexity", "M", "--lead", "claude", env=clean_env(VIGIA_WEBHOOK_ENV="")).stdout)
        self.assertEqual(out["webhook_env"], "")

    def test_no_machine_specific_paths(self):
        for dp, _, fs in os.walk(FLEET):
            if "__pycache__" in dp: continue
            for f in fs:
                txt = open(os.path.join(dp, f), errors="ignore").read()
                for bad in ("/workspace/", "/home/"):
                    self.assertNotIn(bad, txt, os.path.join(dp, f))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1, warnings="ignore")
    finally:
        subprocess.run(["rm", "-rf", SCRATCH])
