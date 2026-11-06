#!/usr/bin/env python3
"""fleet-run criteria gate (no LLM): every criterion is one short executable command that cannot mask a failure;
a UI criterion checks its artifact. Run: python3 tests/test_fleet_criteria.py"""
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ld = importlib.machinery.SourceFileLoader("fleet_run", os.path.join(ROOT, "fleet", "bin", "fleet-run"))
R = importlib.util.module_from_spec(importlib.util.spec_from_loader(_ld.name, _ld))
_ld.exec_module(R)
PATH = os.environ.get("PATH", "")


class CriteriaGate(unittest.TestCase):
    def bad(self, **c):
        return R.Criteria.problems([{"id": "C1", **c}], PATH)

    def test_executable_commands_pass(self):
        for cmd in ("python3 -m pytest -q", "test -s proofs/x.png", "[ -f out.txt ]", "FOO=1 python3 -c 'print(1); x'",
                    "cd sub && python3 -m pytest -q 2>&1", "bash -c 'python3 -m pytest -q'", "timeout 60 python3 -m pytest"):
            self.assertEqual(self.bad(command=cmd), [], cmd)

    def test_manual_vacuous_and_masking_rejected(self):
        for cmd in ("", "manual check of the page", "Verify visually that it works", "N/A", "...", "true", "echo ok",
                    "see README", "nosuchtool-xyz --run", "/bin/true", "env true", "timeout 5 true",
                    "python3 -m pytest || true", "python3 -m pytest; exit 0", "test 1 # proofs/ui.png", "test 1",
                    "[ 1 ]", "bash -c true", "sh -c 'true && :'", "python3 -m pytest; printf ok",
                    "python3 -m pytest | tee log", "python3 -m pytest &", "(python3 -m pytest)", "echo $(python3 x)",
                    "python3 -m pytest && true", "cd sub"):
            self.assertTrue(self.bad(command=cmd), cmd)

    def test_scripts_must_exist_and_be_executable(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "checks", "C1.sh")
            os.makedirs(os.path.dirname(script))
            with open(script, "w") as f:
                f.write("exit 1\n")
            ok = R.Criteria.problems([{"id": "C1", "command": "bash checks/C1.sh"},
                                      {"id": "C2", "command": "bash " + script}], PATH, repo=d)
            self.assertEqual(ok, [])
            for cmd in ("./missing-check", "bash checks/missing.sh", "./checks", "./checks/C1.sh", "bash -x"):
                self.assertTrue(R.Criteria.problems([{"id": "C1", "command": cmd}], PATH, repo=d), cmd)
            os.chmod(script, 0o755)
            self.assertEqual(R.Criteria.problems([{"id": "C1", "command": "./checks/C1.sh"}], PATH, repo=d), [])

    def test_long_or_multiline_goes_to_checks(self):
        self.assertIn("T/checks/C1.sh", self.bad(command="python3 -c '" + "x" * 300 + "'")[0])
        self.assertTrue(self.bad(command="python3 -m pytest\npython3 -m pytest"))

    def test_ui_criterion_must_check_its_artifact(self):
        art = "proofs/ui.png"
        self.assertTrue(self.bad(command="python3 -m pytest -q", when="ui"))
        self.assertTrue(self.bad(command="python3 -m pytest -q", when="ui", artifact=art))
        self.assertTrue(self.bad(command="test -n proofs/ui.png", when="ui", artifact=art))
        self.assertTrue(self.bad(command="test -s proofs/ui.png || /bin/true", when="ui", artifact=art))
        self.assertEqual(self.bad(command="[ -s proofs/ui.png ]", when="ui", artifact=art), [])
        self.assertEqual(self.bad(command="python3 -m pytest -q && test -s proofs/ui.png", when="ui", artifact=art), [])

    def test_template_criteria_shape(self):
        with open(os.path.join(ROOT, "fleet", "skill", "fleet-sdd", "templates", "criteria.json")) as f:
            crit = json.load(f)["criteria"]
        self.assertEqual(R.Criteria.problems([c for c in crit if c["id"] == "C-UI"], PATH), [])


if __name__ == "__main__":
    unittest.main()
