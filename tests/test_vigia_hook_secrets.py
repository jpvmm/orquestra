#!/usr/bin/env python3
"""Hooks deny secret files (always, any tool) and redact secrets before anything is truncated or written.
Run: python3 tests/test_vigia_hook_secrets.py"""
import shutil, json, os, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import vigia_comms as C
import vigia_lib as V


def run_hook(name, ev, **envx):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIGIA_")}
    return subprocess.run([os.path.join(ROOT, "bin", name)], input=json.dumps(ev), capture_output=True, text=True,
                          env=dict(env, **envx))


class SecretPolicy(unittest.TestCase):
    def test_secret_paths_and_commands(self):
        for txt in (".env", "app/.env", "cat .env.local", "cp webhook.env /tmp/x", "cat .e?v", "head .env*",
                    "less webhook*"):
            self.assertTrue(V.touches_secret(txt), txt)
        for txt in (".env.example", "cp .env.sample .env.template", "environment.py", "src/envelope.md"):
            self.assertFalse(V.touches_secret(txt), txt)

    def test_denied_without_protect_tests_for_any_tool(self):
        pre = "PreToolUse"
        self.assertEqual(V.HookPolicy.deny_reason(pre, "Read", {"file_path": "/r/.env"}, env={}), V.HookPolicy.SECRET)
        self.assertEqual(V.HookPolicy.deny_reason(pre, "Grep", {"path": "/r/.env.prod"}, env={}), V.HookPolicy.SECRET)
        self.assertEqual(V.HookPolicy.deny_reason(pre, "Bash", {"command": "cat .env"}, env={}), V.HookPolicy.SECRET)
        self.assertIsNone(V.HookPolicy.deny_reason(pre, "Read", {"file_path": "/r/.env.example"}, env={}))
        self.assertIsNone(V.HookPolicy.deny_reason("PostToolUse", "Read", {"file_path": "/r/.env"}, env={}))


class Redaction(unittest.TestCase):
    def test_quoted_values_masked_whole(self):
        self.assertEqual(C.redact('TOKEN="alpha beta gamma"'), 'TOKEN="***"')
        self.assertEqual(C.redact("password: 'my long pass'"), "password: '***'")
        self.assertEqual(C.redact(json.dumps({"stdout": 'TOKEN="alpha beta gamma"'})), '{"stdout": "TOKEN=\\"***\\""}')
        self.assertEqual(C.redact('TOKEN="abc'), 'TOKEN="***')
        self.assertEqual(C.redact("auth: none"), "auth: none")

    def test_claude_hook_redacts_before_truncating_and_serializing(self):
        with tempfile.TemporaryDirectory() as td:
            hb = os.path.join(td, "hb.jsonl")
            r = run_hook("vigia-claude-hook", {"hook_event_name": "PreToolUse", "tool_name": "Read",
                                               "tool_input": {"file_path": td + "/.env"}}, VIGIA_HEARTBEAT=hb)
            self.assertIn('"deny"', r.stdout)
            self.assertEqual(json.loads(open(hb).readline())["denied"], "secret")
            run_hook("vigia-claude-hook", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {
                "command": "x" * 480 + " OPENAI_API_KEY=sk-abcdefghijklmnop1234567890"}}, VIGIA_HEARTBEAT=hb)
            run_hook("vigia-claude-hook", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                           "tool_input": {"command": "env"},
                                           "tool_response": {"stdout": 'TOKEN="alpha beta gamma"', "stderr": ""}},
                     VIGIA_HEARTBEAT=hb)
            run_hook("vigia-claude-hook", {"hook_event_name": "Stop",
                                           "last_assistant_message": "y" * 290 + " API_KEY=abcdef123456"},
                     VIGIA_HEARTBEAT=hb)
            txt = open(hb).read()
            for leak in ("sk-abcdefghij", "gamma", "abcdef1"):
                self.assertNotIn(leak, txt)

    def test_codex_hook_denies_secret_and_redacts_output(self):
        with tempfile.TemporaryDirectory() as td:
            hb = os.path.join(td, "hb.jsonl")
            r = run_hook("vigia-codex-hook", {"hook_event_name": "PreToolUse", "tool_name": "shell",
                                              "tool_input": {"command": "cat webhook.env"}}, VIGIA_HEARTBEAT=hb, VIGIA_CWD=td)
            self.assertIn('"deny"', r.stdout)
            run_hook("vigia-codex-hook", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                          "tool_input": {"command": "env"},
                                          "tool_response": {"output": 'DB_PASSWORD="two words"'}},
                     VIGIA_HEARTBEAT=hb, VIGIA_CWD=td)
            lines = [json.loads(x) for x in open(hb)]
            self.assertEqual(lines[0]["denied"], "secret")
            self.assertNotIn("two words", open(hb).read())


ESCAPED = 'TOKEN="alpha\\" beta gamma"'
MULTI = 'DB_PASSWORD="first line\nsecond gamma"'


class QuotedEdgeCases(unittest.TestCase):
    def test_escaped_quote_and_multiline(self):
        self.assertEqual(C.redact(ESCAPED), 'TOKEN="***"')
        self.assertEqual(C.redact(MULTI + " tail"), 'DB_PASSWORD="***" tail')
        for raw in (ESCAPED + " tail", MULTI):
            out = C.redact(json.dumps({"stdout": raw}))
            self.assertNotIn("gamma", out)
            self.assertNotIn("beta", out)
        self.assertEqual(C.redact('pass="ab" and token="cd"'), 'pass="ab" and token="cd"')
        self.assertEqual(C.redact('TOKEN="abcdef1" next="x"'), 'TOKEN="***" next="x"')

    def check(self, name, tool):
        with tempfile.TemporaryDirectory() as td:
            hb = os.path.join(td, "hb.jsonl")
            env = dict(VIGIA_HEARTBEAT=hb, VIGIA_CWD=td)
            for raw in (ESCAPED, MULTI):
                run_hook(name, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": "x " + raw}}, **env)
                run_hook(name, {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": "env"},
                                "tool_response": {"stdout": raw, "stderr": ""}}, **env)
                run_hook(name, {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": "env"},
                                "tool_response": raw}, **env)
                run_hook(name, {"hook_event_name": "Stop", "last_assistant_message": "done " + raw}, **env)
            txt = open(hb).read()
            self.assertGreaterEqual(len(txt.splitlines()), 8)
            for leak in ("beta", "gamma", "second"):
                self.assertNotIn(leak, txt)

    def test_claude_hook(self):
        self.check("vigia-claude-hook", "Bash")

    def test_codex_hook(self):
        self.check("vigia-codex-hook", "Bash")


class GrepSecrets(unittest.TestCase):
    def setUp(self):
        self.V = V
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        os.makedirs(os.path.join(self.repo, "src"))
        open(os.path.join(self.repo, "src", "a.py"), "w").write("x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def deny(self, **inp):
        return self.V.HookPolicy.deny_reason("PreToolUse", "Grep", {"pattern": ".", **inp}, env={"VIGIA_CWD": self.repo})

    def test_glob_naming_a_secret_is_denied(self):
        self.assertTrue(self.deny(path=self.repo, glob=".env", output_mode="content"))
        self.assertTrue(self.deny(path=self.repo, glob="webhook*"))

    def test_unscoped_search_over_a_secret_is_denied_scoped_one_allowed(self):
        self.assertIsNone(self.deny(path=self.repo))                       # no secret file here
        open(os.path.join(self.repo, ".env"), "w").write("TOKEN=abc123456\n")
        self.assertTrue(self.deny(path=self.repo))
        self.assertTrue(self.deny())                                        # path defaults to the run cwd
        self.assertTrue(self.deny(path=self.repo, glob="*"))
        self.assertTrue(self.deny(path=self.repo, glob="!*.md"))
        self.assertIsNone(self.deny(path=self.repo, glob="*.py"))
        self.assertIsNone(self.deny(path=self.repo, glob="**/*.{py,md}"))
        self.assertTrue(self.deny(path=self.repo, type="py"))              # type is refused: use a glob
        self.assertTrue(self.deny(path=self.repo, type="sh"))
        self.assertTrue(self.deny(path=self.repo, type="config"))
        self.assertIsNone(self.deny(path=os.path.join(self.repo, "src")))

    def test_filters_are_checked_against_real_secret_files(self):
        """A glob/type that a real secret file matches must not pass (not just a few example names)."""
        open(os.path.join(self.repo, ".env.staging"), "w").write("TOKEN=abc123456\n")
        self.assertTrue(self.deny(path=self.repo, glob="*.staging"))
        self.assertTrue(self.deny(path=self.repo, glob="*staging*"))
        self.assertIsNone(self.deny(path=self.repo, glob="*.py"))
        self.assertIsNone(self.deny(path=self.repo, glob="!*.staging"))
        self.assertTrue(self.deny(path=self.repo, type="json"))
        open(os.path.join(self.repo, "src", ".env.json"), "w").write('{"TOKEN": "abc123456"}\n')
        self.assertTrue(self.deny(path=self.repo, type="json"))
        self.assertTrue(self.deny(path=self.repo, glob="src/*.json"))
        self.assertTrue(self.deny(path=self.repo, glob="**/*.json"))
        self.assertTrue(self.deny(path=os.path.join(self.repo, "src"), glob="*.{json,py}"))
        self.assertIsNone(self.deny(path=self.repo, glob="*.md"))

    def test_ripgrep_semantics_and_no_skipped_dirs(self):
        """negated globs do not cross `/`, a positive glob overrides type, build/ etc. are searched too."""
        os.makedirs(os.path.join(self.repo, "src", "sub"))
        open(os.path.join(self.repo, "src", "sub", ".env.json"), "w").write("{}\n")
        self.assertTrue(self.deny(path=self.repo, glob="!src/*.json"))           # src/sub/.env.json is still searched
        self.assertIsNone(self.deny(path=self.repo, glob="!**/*.json"))
        os.remove(os.path.join(self.repo, "src", "sub", ".env.json"))
        for d in ("build", "dist", "node_modules", ".git"):
            os.makedirs(os.path.join(self.repo, d), exist_ok=True)
            f = os.path.join(self.repo, d, "webhook.env")
            open(f, "w").write("T=1\n")
            self.assertTrue(self.deny(path=self.repo, glob="*"), d)
            os.remove(f)
        self.assertIsNone(self.deny(path=self.repo, glob="*"))
        self.assertTrue(self.deny(path=self.repo, glob="*.{a,{b,c}}"))           # unexpanded nesting fails closed

    def test_type_is_refused(self):
        """ripgrep's type globs drift across versions (*.rake, *.svelte.ts): any type fails closed."""
        self.assertIsNone(self.deny(path=self.repo))                        # nothing secret here, no filter: allowed
        for t in ("json", "ruby", "py", "svelte", "nosuchtype"):
            self.assertTrue(self.deny(path=self.repo, type=t), t)
            self.assertTrue(self.deny(path=self.repo, glob="*.py", type=t), t)

    def test_anchored_exclusion_stays_at_the_root(self):
        """`!/name` excludes only the root file; a nested secret is still searched."""
        os.makedirs(os.path.join(self.repo, "sub"))
        open(os.path.join(self.repo, "sub", "webhook.env"), "w").write("T=1\n")
        self.assertTrue(self.deny(path=self.repo, glob="!/webhoo[k].env"))
        self.assertIsNone(self.deny(path=self.repo, glob="!webhoo[k].env"))
        self.assertTrue(self.deny(path=self.repo, glob="!/sub"))

    def test_backslash_in_glob_fails_closed(self):
        """ripgrep treats `\\` as an escape (`webhoo\\k.env` matches webhook.env); the hook's regex did not."""
        os.makedirs(os.path.join(self.repo, "sub"))
        open(os.path.join(self.repo, "webhook.env"), "w").write("T=1\n")
        open(os.path.join(self.repo, "sub", "webhook.env"), "w").write("T=1\n")
        for g in ("webhoo\\k.env", "!webhoo\\k.env", "!/webhoo\\k.env"):
            self.assertTrue(self.deny(path=self.repo, glob=g), g)
        os.remove(os.path.join(self.repo, "webhook.env"))
        self.assertTrue(self.deny(path=self.repo, glob="!webhoo\\k.env"))       # nested secret still present

    @unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
    @unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
    def test_allowed_searches_never_list_a_secret_in_ripgrep(self):
        """Differential: whenever the hook allows a Grep, `rg --files --hidden --no-ignore` with the same filters lists
        no secret file."""
        import subprocess
        for d in ("sub/deep", "build", "src/x"):
            os.makedirs(os.path.join(self.repo, d), exist_ok=True)
        for f in (".env", "sub/.env.json", "sub/deep/webhook.env", "build/.env.staging", "src/x/.env.local", "src/x/b.py",
                  "sub/c.md", "sub/deep/d.json"):
            open(os.path.join(self.repo, f), "w").write("x\n")
        globs = ["*.py", "*.json", "!*.json", "!/.env", "!.env*", "!**/.env*", "!sub/*", "!sub/**", "**/*.py", "src/**",
                 "*.{py,md}", "!*.{json,env}", "!{.env*,webhook.env}", "*.md", "sub/*.md", "!/sub", "!build", "*", "!webhook.env", "sub/*.json", "!src/x/*",
                 "webhoo\\k.env"]
        allowed = 0
        for g in globs + [""]:
            inp = {"glob": g} if g else {}
            if self.deny(path=self.repo, **inp) is not None:
                continue
            allowed += 1
            cmd = ["rg", "--files", "--hidden", "--no-ignore"] + (["-g", g] if g else [])
            out = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True).stdout.split()
            leaked = [x for x in out if self.V.touches_secret(x)]
            self.assertEqual(leaked, [], g)
        self.assertGreater(allowed, 3)

    def test_hook_blocks_and_logs_it_as_secret(self):
        open(os.path.join(self.repo, ".env"), "w").write("TOKEN=abc123456\n")
        hb = os.path.join(self.repo, "hb.jsonl")
        r = run_hook("vigia-claude-hook", {"hook_event_name": "PreToolUse", "tool_name": "Grep",
                                       "tool_input": {"pattern": ".", "path": self.repo, "output_mode": "content"}},
                 VIGIA_HEARTBEAT=hb, VIGIA_CWD=self.repo)
        self.assertIn('"deny"', r.stdout)
        self.assertEqual(json.loads(open(hb).readline())["denied"], "secret")


class AnalyzerAlerts(unittest.TestCase):
    def test_denied_secret_is_an_alert_not_a_test_edit(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("vigia_run_sec", os.path.join(ROOT, "bin", "vigia-run"))
        vr = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(vr)

        class A:
            protect_tests = True; max_files = 2; cwd = "/w"; task_dir = "/w/T"; loop_repeat = 3; max_turns_hint = None
        an = vr.Analyzer(A())
        an.hook({"hook_event_name": "PreToolUse", "tool_name": "Read", "denied": "secret", "path": "/w/.env"})
        an.hook({"hook_event_name": "PreToolUse", "tool_name": "shell", "origin": "codex", "denied": "secret",
                 "path": "cat .env"})
        self.assertTrue(any("secret file" in x for x in an.alerts))
        self.assertFalse(an.loop)


if __name__ == "__main__":
    unittest.main()
