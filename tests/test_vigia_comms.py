#!/usr/bin/env python3
"""Unit tests for vigia_comms (redaction, prompt extraction, transcript). Run: python3 tests/test_vigia_comms.py"""
import os, sys, tempfile, unittest
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))
import vigia_comms as C


class Redact(unittest.TestCase):
    def test_tokens(self):
        cases = {
            "key sk-ant-api03-AbCdEf1234567890xyz end": "sk-***",
            "gh ghp_abcdefghijklmnopqrstuvwxyz0123 x": "ghp_***",
            "gho_abcdefghijklmnopqrstuvwxyz0123": "gho_***",
            "github_pat_11ABCDEFG0123456789_abcdefghijkl": "github_pat_***",
            "slack xoxb-1234-5678-abcdefgh": "xoxb-***",
            "xoxp-1234567890-abc": "xoxp-***",
            "Authorization: Bearer abc123def456ghi789": "Bearer ***",
            "aws AKIAABCDEFGHIJKLMNOP done": "AKIA***",
        }
        for src, mark in cases.items():
            out = C.redact(src)
            self.assertIn(mark, out, src)
            secret = src.split()[1] if src.split()[0] in ("key", "gh", "slack", "aws") else None
            if secret:
                self.assertNotIn(secret[6:], out)

    def test_assignments(self):
        self.assertEqual(C.redact("OPENAI_API_KEY=abc"), "OPENAI_API_KEY=***")
        self.assertEqual(C.redact("export GITHUB_TOKEN='x9y8z7w6'"), "export GITHUB_TOKEN='***'")
        self.assertEqual(C.redact("DB_PASSWORD=hunter2\nFOO=bar"), "DB_PASSWORD=***\nFOO=bar")
        self.assertEqual(C.redact("MY_SECRET=s3cr3tvalue"), "MY_SECRET=***")
        self.assertEqual(C.redact('{"api_key": "Zx81kq9Lm2"}'), '{"api_key": "***"}')
        self.assertIn("password: ***", C.redact("password: Tr0ub4dor&3"))

    def test_no_false_positives(self):
        for s in ("max_tokens=100", "fix the task-list-manager-component", "sort(key=lambda x: x)",
                  "the token: expired", "Bearer tokens are used", "/srv/orquestra/myproject/runs/a.status.json",
                  "ask-the-user-something-1234567", "KEY=*** already", "monkey=banana"):
            self.assertEqual(C.redact(s), s, s)

    def test_misc(self):
        self.assertEqual(C.redact("https://user:p4ss@host/x"), "https://user:***@host/x")
        self.assertNotIn("MIIE", C.redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----"))
        obj = C.redact_obj({"a": ["TOKEN=abc123"], "n": 3, "b": None})
        self.assertEqual(obj, {"a": ["TOKEN=***"], "n": 3, "b": None})
        self.assertIsNone(C.redact(None))


class Extract(unittest.TestCase):
    def test_claude(self):
        cmd = ["claude", "-p", "Fix the bug in foo.py", "--output-format", "stream-json", "--verbose",
               "--model", "opus", "--append-system-prompt", "Be brief", "--settings", "/x/hooks.json"]
        i = C.extract_prompt("claude", cmd)
        self.assertEqual((i["prompt"], i["model"], i["prompt_source"]), ("Fix the bug in foo.py", "opus", "argv"))
        self.assertEqual(i["system"], [["--append-system-prompt", "Be brief"]])

    def test_claude_positional_later(self):
        i = C.extract_prompt("claude", ["claude", "--model=sonnet", "-p", "--output-format", "json", "do it"])
        self.assertEqual((i["prompt"], i["model"]), ("do it", "sonnet"))

    def test_codex(self):
        cmd = ["codex", "-m", "gpt-5", "exec", "--json", "-s", "workspace-write", "-o", "/tmp/last.txt",
               "-c", 'developer_instructions="no tests"', "Implement X"]
        i = C.extract_prompt("codex", cmd)
        self.assertEqual((i["prompt"], i["model"], i["reply_file"]), ("Implement X", "gpt-5", "/tmp/last.txt"))
        self.assertEqual(i["system"], [["-c developer_instructions", "no tests"]])

    def test_stdin_prompt_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("from file")
        i = C.extract_prompt("codex", ["codex", "exec", "--json", "-"], prompt_file=f.name)
        self.assertEqual(i["prompt"], "from file")
        self.assertTrue(i["prompt_source"].startswith("--prompt-file"))
        os.unlink(f.name)

    def test_wrapped(self):
        i = C.extract_prompt("other", ["timeout", "600", "bash", "-lc", "cd /x && claude -p 'hello there' --model haiku"])
        self.assertEqual((i["agent"], i["prompt"], i["model"]), ("claude", "hello there", "haiku"))


class TranscriptT(unittest.TestCase):
    def test_render(self):
        d = tempfile.mkdtemp()
        t = C.Transcript(os.path.join(d, "r.conversation.md"), "r", "task", "claude", "opus", "PROMPT TOKEN=abc123", "/p.md")
        t.add("assistant", "Looking at it")
        t.add("tool", C.tool_line("Bash", {"command": "pytest -q"}))
        t.subagent_call("id1", "Explore", "find files", "search for X")
        t.subagent_call("id1", "Explore", "find files", "search for X")
        t.add("sub_result", "found 3 files")
        t.add("assistant", "Done: all good")
        t.add("final", "Done: all good")
        t.flush()
        with open(t.path) as f:
            md = f.read()
        self.assertIn("TOKEN=***", md)
        self.assertNotIn("abc123", md)
        self.assertEqual(md.count("Explore"), 1)
        self.assertEqual(md.count("Done: all good"), 1)
        self.assertEqual(t.last_reply, "Done: all good")
        self.assertIn("`Bash: pytest -q`", md)
        import json
        with open(os.path.join(d, "r.conversation.json")) as f:
            j = json.load(f)
        self.assertEqual(j["schema_version"], 1)
        self.assertEqual([e["kind"] for e in j["entries"]],
                         ["prompt", "assistant", "tool", "sub_call", "sub_result", "final"])
        self.assertNotIn("abc123", json.dumps(j))
        self.assertTrue(all(e["ts"][-6] in "+-" for e in j["entries"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
