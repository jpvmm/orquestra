#!/usr/bin/env python3
"""Hooks deny secret files (always, any tool) and redact secrets before anything is truncated or written.
Run: python3 tests/test_vigia_hook_secrets.py"""
import json, os, subprocess, sys, tempfile, unittest
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
