#!/usr/bin/env python3
"""Detect Claude 5-hour usage limit and choose fallback.
Run: python3 tests/test_vigia_limit_fallback.py"""
import os, sys, tempfile, unittest
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bin"))
import vigia_lib as V

# Captured from a live Claude CLI usage-limit response:
SAMPLE = "You've hit your 5-hour limit · resets 11:50pm (America/Sao_Paulo)"
SAMPLE_STDERR = (
    "Error: Claude Code is unable to respond to this request.\n"
    "You've hit your 5-hour limit · resets 11:50pm (America/Sao_Paulo)\n"
)


class ClaudeLimit(unittest.TestCase):
    def test_detects_real_sample(self):
        info = V.detect_claude_limit(SAMPLE)
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "claude_usage_limit")
        self.assertIn("11:50pm", info["resets"])

    def test_detects_in_stderr_block(self):
        info = V.detect_claude_limit(SAMPLE_STDERR)
        self.assertIsNotNone(info)

    def test_no_false_positive(self):
        self.assertIsNone(V.detect_claude_limit("tests passed in 5 hours of work"))
        self.assertIsNone(V.detect_claude_limit(""))

    def test_alert_re_also_matches(self):
        self.assertTrue(V.ALERT_RE.search(SAMPLE))

    def test_review_falls_back_to_codex(self):
        fb = V.limit_fallback_plan("reviewer")
        self.assertEqual(fb["action"], "fallback_tool")
        self.assertEqual(fb["tool"], "codex")

    def test_lead_pauses_without_loop(self):
        fb = V.limit_fallback_plan("team-lead")
        self.assertEqual(fb["action"], "pause")
        self.assertEqual(fb["state"], "limit_paused")

    def test_appends_usage_to_progress(self):
        with tempfile.TemporaryDirectory() as td:
            open(os.path.join(td, "progress.md"), "w", encoding="utf-8").write("# Progress\n")
            ok = V.append_usage_progress(
                td, name="rev-1", tool="claude", model="opus",
                usage={"role": "reviewer", "total_cost_usd": 0.0, "num_turns": 1},
                limit_info=V.detect_claude_limit(SAMPLE),
            )
            self.assertTrue(ok)
            txt = open(os.path.join(td, "progress.md")).read()
            self.assertIn("LIMIT claude_usage_limit", txt)
            self.assertIn("fallback=fallback_tool:codex", txt)
            self.assertIn("model=opus", txt)


if __name__ == "__main__":
    unittest.main()
