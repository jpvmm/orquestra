#!/usr/bin/env python3
"""criteria.json template must ship a C-UI visual-proof criterion for UI tasks."""
import json, os, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "fleet/skill/fleet-sdd/templates/criteria.json")


class CriteriaUITemplate(unittest.TestCase):
    def test_c_ui_present(self):
        with open(TEMPLATE, encoding="utf-8") as _f:
            data = json.load(_f)
        ids = [c["id"] for c in data["criteria"]]
        self.assertIn("C-UI", ids)
        cui = next(c for c in data["criteria"] if c["id"] == "C-UI")
        self.assertIn("visual proof", cui["description"].lower())
        self.assertTrue(cui["command"])

    def test_c_ui_command_fails_without_proof(self):
        with open(TEMPLATE, encoding="utf-8") as _f:
            data = json.load(_f)
        cui = next(c for c in data["criteria"] if c["id"] == "C-UI")
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(["bash", "-lc", cui["command"]], cwd=td, capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            os.makedirs(os.path.join(td, "proofs"))
            with open(os.path.join(td, "proofs", "ui-proof.png"), "wb") as _f:
                _f.write(b"x")
            r2 = subprocess.run(["bash", "-lc", cui["command"]], cwd=td, capture_output=True)
            self.assertEqual(r2.returncode, 0)

    def test_ui_note_stays_generic(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            raw = f.read().lower()
        # Banlist built at runtime so the source does not embed private host/product names.
        parts = [
            ("fab", "rica"), ("the", "mis"), ("spi", "cybot"),
            ("pai", "nel"), ("delivery-", "control"), ("spa", "rk"),
        ]
        for a, b in parts:
            self.assertNotIn(a + b, raw)


if __name__ == "__main__":
    unittest.main()
