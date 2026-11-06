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


if __name__ == "__main__":
    unittest.main()
