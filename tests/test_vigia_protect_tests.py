#!/usr/bin/env python3
"""--protect-tests must ignore test-named paths outside the run cwd/worktree.
Regression: a /tmp scratch matching TEST_RE used to kill the run as loop.
Run: python3 tests/test_vigia_protect_tests.py"""
import os, sys, unittest
sys.dont_write_bytecode = True
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


if __name__ == "__main__":
    unittest.main()
