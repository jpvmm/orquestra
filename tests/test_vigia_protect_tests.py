#!/usr/bin/env python3
"""--protect-tests must ignore test-named paths outside the run cwd/worktree.
Regression: a /tmp scratch matching TEST_RE used to kill the run as loop.
Run: python3 tests/test_vigia_protect_tests.py"""
import atexit, os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
_BASE = tempfile.mkdtemp(prefix="vigia-test-")   # snapshots go to $VIGIA_BASE/protected: keep them out of ~/vigia
atexit.register(shutil.rmtree, _BASE, True)
os.environ["VIGIA_BASE"] = _BASE
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bin"))
import vigia_lib as V


class IsTestRoots(unittest.TestCase):
    def test_without_roots_keeps_old_behavior(self):
        self.assertTrue(V.is_test("/tmp/test_scratch.py"))
        self.assertTrue(V.is_test("tests/test_a.py"))

    def test_abs_outside_cwd_not_protected(self):
        root = "/home/user/proj/wt-x"
        self.assertFalse(V.is_test("/tmp/test_scratch.py", roots=[root]))
        self.assertFalse(V.is_test("/var/tmp/tests/foo.py", roots=[root]))

    def test_abs_inside_cwd_still_protected(self):
        root = "/home/user/proj/wt-x"
        self.assertTrue(V.is_test(root + "/tests/test_a.py", roots=[root]))
        self.assertTrue(V.is_test(root + "/conftest.py", roots=[root]))

    def test_filesystem_root(self):
        self.assertTrue(V.is_test("/tmp/test_x.py", roots=["/"]))

    def test_relative_counts_as_inside(self):
        self.assertTrue(V.is_test("tests/test_a.py", roots=["/any/cwd"]))
        self.assertTrue(V.is_test("test_a.py", roots=["/any/cwd"]))

    def test_shell_scratch_tmp_ignored(self):
        root = "/wt"
        self.assertEqual(V.shell_test_writes("rm -f /tmp/test_foo.py", roots=[root]), [])
        self.assertEqual(
            V.shell_test_writes("rm -f /wt/tests/test_foo.py", roots=[root]),
            ["/wt/tests/test_foo.py"],
        )


class ProtectViaAnalyzer(unittest.TestCase):
    """Minimal integration: Analyzer._edit with --protect-tests."""

    def setUp(self):
        import importlib.machinery, importlib.util
        path = os.path.join(ROOT, "bin", "vigia-run")
        loader = importlib.machinery.SourceFileLoader("vigia_run_mod", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.vr = importlib.util.module_from_spec(spec)
        loader.exec_module(self.vr)

    def test_edit_tmp_does_not_mark_loop(self):
        class A:
            protect_tests = True
            max_files = 0
            cwd = "/home/user/proj/wt"
            task_dir = "/home/user/proj"
            loop_repeat = 3
            max_turns_hint = None
        an = self.vr.Analyzer(A())
        an._edit("/tmp/test_scratch.py", "x")
        self.assertIsNone(an.loop)

    def test_edit_test_in_cwd_marks_loop(self):
        class A:
            protect_tests = True
            max_files = 0
            cwd = "/home/user/proj/wt"
            task_dir = "/home/user/proj"
            loop_repeat = 3
            max_turns_hint = None
        an = self.vr.Analyzer(A())
        an._edit("/home/user/proj/wt/tests/test_a.py", "x")
        self.assertIsNotNone(an.loop)
        self.assertIn("protected test", an.loop)


class BaseSnapshot(unittest.TestCase):
    """Only tests that exist on the base are protected; new test files are allowed (TDD)."""

    def setUp(self):
        import subprocess, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo"); self.T = os.path.join(self.tmp.name, "T")
        os.makedirs(os.path.join(self.repo, "tests"))
        open(os.path.join(self.repo, "tests", "test_old.py"), "w").write("x = 1\n")
        open(os.path.join(self.repo, "app.py"), "w").write("x = 1\n")
        e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for c in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "b"]):
            subprocess.run(c, cwd=self.repo, check=True, env=e)
        self.path, self.n = V.ProtectedTests.snapshot(self.repo, "HEAD", self.T)
        self.wt = os.path.join(self.T, "wt", "task-1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_lists_only_base_tests(self):
        self.assertEqual(self.n, 1)
        self.assertEqual(V.ProtectedTests.load(self.T), frozenset({"tests/test_old.py"}))

    def test_existing_test_protected_in_any_task_worktree(self):
        integ = os.path.join(self.T, "wt", "integration")
        for p in (self.wt + "/tests/test_old.py", os.path.join(self.T, "wt", "task-7", "tests", "test_old.py")):
            self.assertTrue(V.is_test(p, roots=[integ, self.T], task_dir=self.T), p)
        self.assertTrue(V.is_test("tests/test_old.py", roots=[self.wt], task_dir=self.T))

    def test_snapshot_lives_outside_task_dir(self):
        """Agents can write T, so the list must not be there."""
        self.assertFalse(os.path.abspath(self.path).startswith(os.path.abspath(self.T) + os.sep))
        self.assertTrue(self.path.startswith(os.path.join(_BASE, "protected")))

    def test_relative_dotdot_into_other_worktree_is_protected(self):
        integ = os.path.join(self.T, "wt", "integration")
        self.assertTrue(V.is_test("../task-1/tests/test_old.py", roots=[integ, self.T], task_dir=self.T))

    def test_new_test_file_allowed(self):
        self.assertFalse(V.is_test(self.wt + "/tests/test_new.py", roots=[self.wt, self.T], task_dir=self.T))
        self.assertFalse(V.is_test("tests/test_new.py", roots=[self.wt], task_dir=self.T))

    def test_without_snapshot_every_test_is_protected(self):
        os.remove(self.path)
        self.assertTrue(V.is_test(self.wt + "/tests/test_new.py", roots=[self.wt], task_dir=self.T))


class TrustedBase(unittest.TestCase):
    """The trusted base (vigia-run --protect-base → env, or the base fleet-run recorded) beats the list file; an empty
    list is not trusted; the end-of-run gate compares file contents, whatever command changed them."""

    def setUp(self):
        import subprocess, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("VIGIA_PROTECT_BASE", "VIGIA_CWD")}
        self.repo = os.path.join(self.tmp.name, "repo"); self.T = os.path.join(self.tmp.name, "T")
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "tests", "test_old.py"), "w") as f:
            f.write("x = 1\n")
        self.e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                      GIT_COMMITTER_EMAIL="t@t")
        for c in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "b"]):
            subprocess.run(c, cwd=self.repo, check=True, env=self.e)
        self.base = V.rev_sha(self.repo, "HEAD")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_tampered_or_empty_list(self):
        V.ProtectedTests.snapshot(self.repo, "HEAD", self.T)
        os.remove(V.ProtectedTests.base_path(self.T))
        open(V.ProtectedTests.path(self.T), "w").close()            # truncated by an agent
        self.assertIsNone(V.ProtectedTests.load(self.T))           # → every test-named path protected
        os.environ["VIGIA_PROTECT_BASE"], os.environ["VIGIA_CWD"] = self.base, self.repo
        self.assertEqual(V.ProtectedTests.load(self.T), frozenset({"tests/test_old.py"}))   # git, not the file

    def test_modified_by_content(self):
        import subprocess
        g = lambda *c: subprocess.run(["git", *c], cwd=self.repo, check=True, env=self.e, capture_output=True)
        self.assertEqual(V.ProtectedTests.modified(self.base, [self.repo]), [])
        with open(os.path.join(self.repo, "tests", "test_new.py"), "w") as f:
            f.write("y = 1\n")                                                       # new test: fine
        self.assertEqual(V.ProtectedTests.modified(self.base, [self.repo]), [])
        g("mv", "tests/test_old.py", "tests/test_moved.py")                         # staged rename
        self.assertIn(f"{self.repo}: tests/test_old.py (deleted)", V.ProtectedTests.modified(self.base, [self.repo]))
        g("mv", "tests/test_moved.py", "tests/test_old.py")
        g("update-index", "--assume-unchanged", "tests/test_old.py")                 # hidden from git diff
        with open(os.path.join(self.repo, "tests", "test_old.py"), "a") as f:
            f.write("# x\n")
        self.assertIn(f"{self.repo}: tests/test_old.py", V.ProtectedTests.modified(self.base, [self.repo]))
        bad = V.ProtectedTests.modified("0" * 40, [self.repo, os.path.join(self.tmp.name, "nogit")])
        self.assertEqual(len(bad), 2)
        self.assertTrue(all("cannot verify" in b for b in bad))                      # fails closed

    def test_repo_without_tests_can_add_its_first(self):
        import subprocess
        empty = os.path.join(self.tmp.name, "empty"); os.makedirs(empty)
        for c in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "b"]):
            subprocess.run(c, cwd=empty, check=True, env=self.e)
        T2 = os.path.join(self.tmp.name, "T2")
        V.ProtectedTests.snapshot(empty, "HEAD", T2)
        os.environ.pop("VIGIA_PROTECT_BASE", None); os.environ["VIGIA_CWD"] = empty
        self.assertEqual(V.ProtectedTests.load(T2), frozenset())
        self.assertFalse(V.is_test("tests/test_first.py", roots=[empty], task_dir=T2))

    def test_vigia_run_end_gate_turns_done_into_loop(self):
        import json, subprocess
        env = dict(self.e, VIGIA_BASE=_BASE, VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3")
        cmd = [os.path.join(ROOT, "bin", "vigia-run"), "--tool", "other", "--task-dir", self.T, "--name", "gate",
               "--cwd", self.repo, "--protect-tests", "--protect-base", self.base, "--foreground", "--",
               "python3", "-c", "open('tests/test_old.py','a').write('#x')"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.T, "runs", "gate.status.json")) as f:
            st = json.load(f)
        self.assertEqual(st["state"], "loop")
        self.assertIn("tests/test_old.py", st["loop_reason"])


class ClaudeHook(unittest.TestCase):
    def hook(self, ev, **envx):
        import json, subprocess
        return subprocess.run([os.path.join(ROOT, "bin", "vigia-claude-hook")], input=json.dumps(ev),
                              capture_output=True, text=True, env=dict(os.environ, **envx))

    def test_deny_existing_allow_new_tag_subagent(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as td:
            T = os.path.join(td, "T"); wt = os.path.join(T, "wt", "task-1")
            dest = V.ProtectedTests.path(T); os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "w").write("tests/test_old.py\n")
            hb = os.path.join(td, "hooks.jsonl")
            env = dict(VIGIA_HEARTBEAT=hb, VIGIA_PROTECT_TESTS="1", VIGIA_CWD=wt, VIGIA_TASK_DIR=T)
            pre = {"hook_event_name": "PreToolUse", "tool_name": "Edit"}
            r = self.hook({**pre, "tool_input": {"file_path": wt + "/tests/test_old.py"}, "agent_id": "a1"}, **env)
            self.assertIn('"deny"', r.stdout)
            r = self.hook({**pre, "tool_input": {"file_path": wt + "/tests/test_new.py"}}, **env)
            self.assertEqual(r.stdout.strip(), "")
            first = json.loads(open(hb).readline())
            self.assertEqual(first.get("agent_id"), "a1")
            self.assertTrue(first.get("denied"))
            bash = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
            r = self.hook({**bash, "tool_input": {"command": "sed -i s/1/2/ tests/test_old.py"}}, **env)
            self.assertIn('"deny"', r.stdout)
            r = self.hook({**bash, "tool_input": {"command": "echo x > tests/test_new.py"}}, **env)
            self.assertEqual(r.stdout.strip(), "")


class CodexHook(unittest.TestCase):
    def test_shell_write_to_protected_test_denied(self):
        import json, subprocess, tempfile
        with tempfile.TemporaryDirectory() as td:
            wt = os.path.join(td, "T", "wt", "task-1"); os.makedirs(wt)
            env = dict(os.environ, VIGIA_PROTECT_TESTS="1", VIGIA_CWD=wt, VIGIA_TASK_DIR="")
            ev = {"hook_event_name": "PreToolUse", "tool_name": "shell", "tool_input": {"command": "rm tests/test_a.py"}}
            r = subprocess.run([os.path.join(ROOT, "bin", "vigia-codex-hook")], input=json.dumps(ev),
                               capture_output=True, text=True, env=env)
            self.assertIn('"deny"', r.stdout)


class OwnCallsOnly(unittest.TestCase):
    """The lead's loop/scope signals count only its own calls, not its subagents'."""

    def setUp(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("vigia_run_own", os.path.join(ROOT, "bin", "vigia-run"))
        self.vr = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.vr)

    def an(self):
        class A:
            protect_tests = True; max_files = 2; cwd = "/w/T/wt/integration"; task_dir = "/w/T"
            loop_repeat = 3; max_turns_hint = None
        return self.vr.Analyzer(A())

    def ev(self, path, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent,
                "message": {"content": [{"type": "tool_use", "id": path, "name": "Edit", "input": {"file_path": path}}]}}

    def test_subagent_edits_do_not_grow_scope(self):
        an = self.an()
        for i in range(5):
            an.claude(self.ev(f"/w/T/wt/task-{i}/f{i}.py", parent="toolu_x"))
        self.assertIsNone(an.loop)

    def test_subagent_test_edit_is_alert_not_kill(self):
        an = self.an()
        an.claude(self.ev("/w/T/wt/task-1/tests/test_a.py", parent="toolu_x"))
        self.assertIsNone(an.loop)
        self.assertTrue(any("protected test" in a for a in an.alerts))

    def test_own_edits_still_count(self):
        an = self.an()
        for i in range(3):
            an.claude(self.ev(f"/w/T/wt/integration/f{i}.py"))
        self.assertIn("growing scope", an.loop or "")


if __name__ == "__main__":
    unittest.main()
