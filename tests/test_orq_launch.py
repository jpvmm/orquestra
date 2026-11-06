#!/usr/bin/env python3
"""orq-launch builds role flags and refuses duplicate locks (no LLM)."""
import json, os, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH = os.path.join(ROOT, "bin", "orq-launch")


class OrqLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = os.path.join(self.tmp.name, "T")
        self.cwd = os.path.join(self.tmp.name, "wt")
        os.makedirs(self.td)
        os.makedirs(self.cwd)
        self.env = dict(os.environ, VIGIA_BASE=os.path.join(self.tmp.name, "vigia"))
        os.makedirs(os.path.join(self.env["VIGIA_BASE"], "active"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_l(self, *extra, tool="claude", role="implement"):
        cmd = [LAUNCH, "--tool", tool, "--role", role, "--cwd", self.cwd,
               "--task-dir", self.td, "--prompt", "hi", "--dry-run", *extra]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.env)

    def test_prompt_right_after_p(self):
        r = self.run_l("--force-lock")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        cmd = data["cmd"]
        self.assertEqual(cmd[cmd.index("-p") + 1], "hi")
        self.assertIn("--protect-tests", data["vigia_args"])
        self.assertIn("Write", cmd[cmd.index("--allowedTools") + 1])

    def test_dev_alias_and_lead_agent_tools(self):
        r = self.run_l("--force-lock", "--agent", "dev", "--agents", "/tmp/agents.json", role="dev")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["role"], "implement")
        r2 = self.run_l("--force-lock", "--agent", "team-lead", role="team-lead")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        data = json.loads(r2.stdout)
        self.assertEqual(data["role"], "lead")
        self.assertIn("Agent", data["cmd"][data["cmd"].index("--allowedTools") + 1])

    def test_lock_refuses_second(self):
        r1 = self.run_l()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        lock = os.path.join(self.td, ".orq-launch-implement.lock")
        with open(lock, "w") as f:
            json.dump({"pid": os.getpid(), "role": "implement", "name": "x"}, f)
        r2 = self.run_l()
        self.assertEqual(r2.returncode, 3)

    def test_review_codex_readonly(self):
        r = self.run_l("--force-lock", tool="codex", role="reviewer")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("read-only", data["cmd"])
        self.assertNotIn("--protect-tests", data["vigia_args"])

    def test_claude_denies_and_no_mcp(self):
        for role in ("implement", "review", "lead", "plan"):
            cmd = json.loads(self.run_l("--force-lock", role=role).stdout)["cmd"]
            self.assertIn("--strict-mcp-config", cmd)
            deny = cmd[cmd.index("--disallowedTools") + 1:]
            for rule in ("Bash(git push:*)", "Bash(git commit:*)", "Bash(gh:*)", "Bash(curl:*)", "Read(**/.env)"):
                self.assertIn(rule, deny, role)
            self.assertEqual(cmd[cmd.index("-p") + 1], "hi")

    def test_plan_has_no_bash_and_lead_protects_tests(self):
        plan = json.loads(self.run_l("--force-lock", role="plan").stdout)["cmd"]
        self.assertNotIn("Bash", plan[plan.index("--allowedTools") + 1])
        self.assertEqual(plan[plan.index("--tools") + 1], "Read,Grep,Glob")   # availability, not just approval
        for t in ("Bash", "Edit", "Write"):
            self.assertIn(t, plan[plan.index("--disallowedTools") + 1:])
        impl = json.loads(self.run_l("--force-lock", role="implement").stdout)["cmd"]
        self.assertNotIn("--tools", impl)
        lead = json.loads(self.run_l("--force-lock", role="lead").stdout)
        self.assertIn("--protect-tests", lead["vigia_args"])

    def test_codex_write_roles_have_network_off(self):
        for role in ("implement", "lead"):
            cmd = json.loads(self.run_l("--force-lock", tool="codex", role=role).stdout)["cmd"]
            self.assertIn("sandbox_workspace_write.network_access=false", cmd)
            self.assertNotIn("sandbox_workspace_write.network_access=true", cmd)

    def test_output_schema_only_read_only(self):
        rev = json.loads(self.run_l("--force-lock", "--output-schema", "s.json", tool="codex", role="review").stdout)["cmd"]
        self.assertIn("--output-schema", rev)
        dev = json.loads(self.run_l("--force-lock", "--output-schema", "s.json", tool="codex", role="dev").stdout)["cmd"]
        self.assertNotIn("--output-schema", dev)


if __name__ == "__main__":
    unittest.main()
