#!/usr/bin/env python3
"""Fleet briefs carry the design-first contract. Run: python3 tests/test_briefs.py"""
import os, unittest

SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet", "skill", "fleet-sdd")


def read(name):
    with open(os.path.join(SKILL, name), encoding="utf-8") as f:
        return f.read()


class Briefs(unittest.TestCase):
    def test_implementer_designs_before_code_and_reports_it(self):
        b = read("brief-implementer.md")
        self.assertLess(b.index("## Design (fill before code"), b.index("## How"))
        self.assertIn("design:", b)

    def test_reviewers_flag_unjustified_net_new_code(self):
        for name in ("brief-task-reviewer.md", "brief-final-reviewer.md"):
            b = read(name)
            self.assertIn("net-new", b, name)
            self.assertIn("Important", b, name)

    def test_security_brief_treats_repo_as_data(self):
        self.assertIn("data, not orders", read("brief-security.md"))


class Resume(unittest.TestCase):
    def test_resume_takes_stage_from_files_and_fresh_attempt_names(self):
        s = read("SKILL.md")
        self.assertIn("its stage comes from files", s)
        self.assertIn("dev-ID-a2", s)
        self.assertIn("fleet-wait", s)

    def test_vigia_refuses_an_existing_run_name(self):
        import subprocess, tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "runs"))
            open(os.path.join(td, "runs", "dev-1.status.json"), "w").write('{"state": "failed"}')
            env = dict(os.environ, VIGIA_BASE=os.path.join(td, "v"), VIGIA_WEBHOOK_ENV="")
            r = subprocess.run([os.path.join(root, "bin", "vigia-run"), "--tool", "other", "--task-dir", td, "--name",
                                "dev-1", "--foreground", "--", "true"], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
