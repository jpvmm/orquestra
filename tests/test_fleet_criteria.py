#!/usr/bin/env python3
"""fleet-run criteria gate (no LLM): every criterion is a whitelisted command (test runner, T/checks script, or a UI
criterion's `test -s ARTIFACT`) joined only by &&. Run: python3 tests/test_fleet_criteria.py"""
import importlib.machinery, importlib.util, json, os, sys, tempfile, unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ld = importlib.machinery.SourceFileLoader("fleet_run", os.path.join(ROOT, "fleet", "bin", "fleet-run"))
R = importlib.util.module_from_spec(importlib.util.spec_from_loader(_ld.name, _ld))
_ld.exec_module(R)
PATH = os.environ.get("PATH", "")


def write(p, txt):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(txt)


class CriteriaGate(unittest.TestCase):
    """Whitelist: test runners, T/checks scripts, and `test -s ARTIFACT` for UI criteria. Nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")          # empty repo: relative paths resolve here
        self.T = os.path.join(self.tmp.name, "T")
        os.makedirs(os.path.join(self.repo, "sub")); os.makedirs(os.path.join(self.T, "checks"))
        self.c1 = os.path.join(self.T, "checks", "C1.sh")
        write(self.c1, "#!/bin/sh\ntest -s out\n"); os.chmod(self.c1, 0o755)
        self.noexec = os.path.join(self.T, "checks", "C2.sh")
        write(self.noexec, "exit 1\n")
        self.other = os.path.join(self.repo, "check.sh")         # executable script outside T/checks
        write(self.other, "#!/bin/sh\nexit 0\n"); os.chmod(self.other, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def bad(self, **c):
        return R.Criteria.problems([{"id": "C1", **c}], PATH, repo=self.repo, task_dir=self.T)

    def test_whitelisted_commands_pass(self):
        rel_from_sub = os.path.relpath(self.c1, os.path.join(self.repo, "sub"))
        for cmd in ("python3 -m pytest -q", "pytest -q tests/test_a.py", "python3 -m unittest discover -s tests",
                    "cd sub && python3 -m pytest -q 2>&1", "PYTHONPATH=src python3 -m pytest -q",
                    f"{sys.executable} -m pytest -q", "python3 -B -m pytest", self.c1, f"bash {self.c1}",
                    f"sh {self.noexec}", f"cd sub && {rel_from_sub}", f"cd {self.T} && checks/C1.sh",
                    f"python3 -m pytest -q && {self.c1}", "cd ~ && python3 -m pytest -q",
                    "python3 -m pytest -q -x -k 'add and not slow' --tb=short tests/test_a.py",
                    "python3 -m unittest -v tests.test_a", "python3 -m unittest discover -s tests -p 'test_*.py'",
                    "npm test", "go test -count=1 -race ./...", "cargo test --workspace -q"):
            self.assertEqual(self.bad(command=cmd), [], cmd)

    def test_everything_else_is_refused(self):
        for cmd in ("manual check of the page", "Verify visually that it works", "N/A", "...", "true", "echo ok",
                    "see README", "nosuchtool-xyz --run", "/bin/true", "env true", "timeout 5 python3 -m pytest",
                    "python3 -m pytest || true", "python3 -m pytest; exit 0", "test 1", "[ 1 ]", "bash -c true",
                    "bash -c 'python3 -m pytest -q'", "sh -c 'true && :'", "python3 -m pytest | tee log",
                    "python3 -m pytest &", "(python3 -m pytest)", "echo $(python3 x)", "python3 -m pytest && true",
                    "cd sub", "python3", "python3 -c pass", "python3 -c 'x=1'", "python3 -c 'assert 1 == 1'",
                    "python3 --version", "node -e 'process.exit(0)'", "node --test", "python3 test_x.py",
                    "python3 -m http.server", "python3 -O -m pytest", "PYTHONOPTIMIZE=1 python3 -m pytest",
                    "PYTEST_ADDOPTS=--collect-only python3 -m pytest", "BASH_ENV=x bash " + self.c1,
                    "set -n && python3 -m pytest", "exec python3 -m pytest", "env -S 'python3 -m pytest'",
                    "xargs -r test -s proofs/ui.png", "find . -name x -exec true ;", "parallel true",
                    "python3 -m pytest --help", "python3 -m pytest --collect-only", "pytest --co", "cargo test --no-run",
                    "npm run build", "npm", "go vet ./...", "go test -list .", "pwd", "ls", "cat README.md",
                    "grep -q x README.md", "diff a b", "curl -f http://x", "make test", "/bin/ls",
                    "/does/not/exist/test -s proof.png", "/does/not/exist/pytest", "/does/not/exist/python3 -m pytest",
                    self.other, f"bash {self.other}", f"cd {self.repo} && ./check.sh", self.noexec,
                    f"bash -x {self.c1}", "bash", "./checks/C1.sh", "test -s proofs/ui.png", "cd $HOME && pytest",
                    "cd - && pytest", f"cd sub && {self.c1}x",
                    # runner arguments are whitelisted too
                    "python3 -m unittest -vh", "python3 -m unittest -h", "go test -exec /bin/true ./...",
                    "go test -run '^$' ./...", "go test -list .", "pytest -p no:python", "pytest -qx --co",
                    "pytest @args.txt", "pytest -c /dev/null", "pytest --rootdir=/", "pytest -k --collect-only",
                    "npm test -- --passWithNoTests", "npm test --if-present", "yarn test --help",
                    "cargo test nonexistent_name", "cargo test -- --list", "cargo test --no-run",
                    "python3 -m unittest discover -h", "python3 -m unittest --locals x"):
            self.assertTrue(self.bad(command=cmd), cmd)

    def test_literal_args_values_and_runner_identity(self):
        for cmd in ("python3 -m unittest ${UNSET:---help}", "python3 -m unittest $X", "pytest \\--co", "pytest *",
                    "pytest {--co,}", "pytest ~/x", "pytest 'a'$'--co'", "go test -count=0 ./...",
                    "go test -count 0 ./...", "go test -p=0 ./...", "go test -timeout=x ./...",
                    "pytest --maxfail=0", "pytest --tb=--co", "cargo test -j 0", "PATH=/tmp/fake pytest",
                    "PYTEST_ADDOPTS=--co pytest", "GOFLAGS=-run=^$ go test ./...", "CARGO_TARGET_DIR=x cargo test",
                    "HOME=/tmp cd ~ && pytest", "LD_PRELOAD=x pytest", "PYTHONHOME=/x python3 -m pytest"):
            self.assertTrue(self.bad(command=cmd), cmd)
        fake = os.path.join(self.tmp.name, "fake")
        os.makedirs(fake)
        for n in ("pytest", "python3", "go"):
            os.symlink("/bin/true", os.path.join(fake, n))
            self.assertTrue(self.bad(command=os.path.join(fake, n) + (" -m pytest" if n == "python3" else " test")), n)
        write(os.path.join(fake, "python3.12"), "#!/bin/sh\nexit 0\n"); os.chmod(os.path.join(fake, "python3.12"), 0o755)
        self.assertTrue(self.bad(command=os.path.join(fake, "python3.12") + " -m pytest"))
        self.assertEqual(self.bad(command=f"{os.path.realpath(sys.executable)} -m pytest -q"), [])
        write(os.path.join(self.repo, "pytest.py"), "")                     # would shadow the real pytest
        self.assertTrue(self.bad(command="python3 -m pytest -q"))
        os.remove(os.path.join(self.repo, "pytest.py"))
        os.makedirs(os.path.join(self.repo, "src", "unittest"))
        self.assertTrue(self.bad(command="PYTHONPATH=src python3 -m unittest"))
        self.assertEqual(self.bad(command="go test -count=1 -timeout 5m ./..."), [])
        self.assertEqual(self.bad(command="python3 -m unittest discover -s tests -p 'test_*.py'"), [])

    def test_redirections_refused(self):
        """`> T/checks/C1.sh` empties the script before bash runs it: success without the proof."""
        for cmd in (f"bash {self.c1} > {self.c1}", f"{self.c1} >{self.c1}", f"bash {self.c1} >> log",
                    f"bash {self.c1} < /dev/null", "python3 -m pytest -q > /dev/null", "python3 -m pytest 1>&2",
                    "python3 -m pytest >&2", "python3 -m pytest 2>/dev/null", "python3 -m pytest &>log"):
            self.assertTrue(self.bad(command=cmd), cmd)
        self.assertEqual(self.bad(command="python3 -m pytest -q 2>&1"), [])
        self.assertEqual(self.bad(command="python3 -m pytest -q -k 'a or b'"), [])

    def test_fake_test_path_and_inline_code(self):
        """Round-10 bypasses: a fake `test` path and assignment-only inline code."""
        for cmd in ("/does/not/exist/test -s proof.png", "python3 -c 'x=1'"):
            self.assertTrue(self.bad(command=cmd), cmd)
            self.assertTrue(self.bad(command=cmd, when="ui", artifact="proof.png"), cmd)

    def test_binary_in_t_checks_is_refused(self):
        b = os.path.join(self.T, "checks", "bin")
        with open(b, "wb") as f:
            f.write(b"\x7fELF\0\0")
        os.chmod(b, 0o755)
        self.assertTrue(self.bad(command=b))
        os.symlink("/bin/true", os.path.join(self.T, "checks", "link"))
        self.assertTrue(self.bad(command=os.path.join(self.T, "checks", "link")))

    def test_scripts_need_a_task_dir(self):
        self.assertTrue(R.Criteria.problems([{"id": "C1", "command": self.c1}], PATH, repo=self.repo))

    def test_long_or_multiline_goes_to_checks(self):
        self.assertIn("T/checks/C1.sh", self.bad(command="python3 -m pytest " + "x" * 300)[0])
        self.assertTrue(self.bad(command="python3 -m pytest\npython3 -m pytest"))

    def test_ui_criterion_must_check_its_artifact(self):
        ui = dict(when="ui", artifact="proofs/ui.png")
        self.assertTrue(self.bad(command="python3 -m pytest -q", when="ui"))
        self.assertTrue(self.bad(command="python3 -m pytest -q", **ui))
        self.assertTrue(self.bad(command="test -n proofs/ui.png", **ui))
        self.assertTrue(self.bad(command="test -f proofs/ui.png", **ui))
        self.assertTrue(self.bad(command="test -s proofs/other.png", **ui))
        self.assertTrue(self.bad(command="test -s proofs/ui.png || true", **ui))
        self.assertTrue(self.bad(command="cd sub && test -s proofs/ui.png", **ui))     # resolves to another file
        self.assertEqual(self.bad(command="python3 -m pytest -q && test -s proofs/ui.png", **ui), [])
        self.assertEqual(self.bad(command="[ -s proofs/ui.png ]", **ui), [])
        self.assertEqual(self.bad(command="test -s proofs/ui.png", **ui), [])
        write(self.c1, "#!/bin/sh\n# proofs/ui.png\nexit 0\n")                     # naming it is not checking it
        self.assertTrue(self.bad(command=self.c1, **ui))
        write(self.c1, "#!/bin/sh\ntest -s proofs/ui.png\n")
        self.assertTrue(self.bad(command=self.c1, **ui))                                  # explicit check required
        self.assertEqual(self.bad(command=f"{self.c1} && test -s proofs/ui.png", **ui), [])

    def test_template_criteria_are_valid(self):
        crit = json.load(open(os.path.join(ROOT, "fleet", "skill", "fleet-sdd", "templates", "criteria.json")))["criteria"]
        c2 = os.path.join(self.T, "checks", "C2.sh")
        for c in crit:
            c["command"] = c["command"].replace("/ABSOLUTE/PATH/TO/T/checks/C2.sh", c2)
        self.assertEqual(R.Criteria.problems(crit, PATH, repo=self.repo, task_dir=self.T), [])


if __name__ == "__main__":
    unittest.main()
