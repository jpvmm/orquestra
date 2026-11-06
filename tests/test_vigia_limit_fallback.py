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

    def test_unknown_role_pauses(self):
        self.assertEqual(V.limit_fallback_plan("")["action"], "pause")
        self.assertEqual(V.limit_fallback_plan("something")["action"], "pause")


class RealFollowUp(unittest.TestCase):
    """The plan is acted on: a review is relaunched on Codex (verified), a lead pause writes a resume file."""

    def test_role_of_prefers_flag_then_fleet_json_then_name(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(V.LimitFallback.role_of("reviewer", td, "x"), "review")
            with open(os.path.join(td, "fleet.json"), "w") as f:
                json.dump({"runs": [{"name": "r1", "role": "team-lead"}, {"name": "r2", "role": "dev"}]}, f)
            self.assertEqual(V.LimitFallback.role_of(None, td, "r1"), "lead")   # this run, not the last one
            self.assertEqual(V.LimitFallback.role_of(None, td, "fleet-x-rev-1"), "review")
            self.assertIsNone(V.LimitFallback.role_of(None, td, "other"))

    def test_resume_file_keeps_exact_argv(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            argv = ["/x/fleet-run", "--task-dir", td, "--stage", "review", "--budget-usd", "3"]
            p = V.LimitFallback.resume_file(td, name="lead-1", role="lead", resets="11pm", relaunch_argv=argv)
            self.assertEqual(json.load(open(p))["resume_command"], argv)

    def fake_fleet_bin(self, td, stdout, rc=0):
        import stat
        b = os.path.join(td, "fbin"); os.makedirs(b)
        p = os.path.join(b, "fleet-codex")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" > {td}/args\necho '{stdout}'\nexit {rc}\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return b

    def test_codex_review_launch_verified(self):
        with tempfile.TemporaryDirectory() as td:
            st = {"task_dir": td, "name": "fleet-x-rev-1", "cwd": td, "cmd": ["claude", "-p", "review it", "--model", "opus"],
                  "output": os.path.join(td, "review", "r.md")}
            ok = V.LimitFallback.launch_codex_review(
                st, fleet_bin=self.fake_fleet_bin(td, '{"state": "running", "status_path": "/s"}'))
            self.assertTrue(ok["launched"], ok)
            args = open(os.path.join(td, "args")).read()
            self.assertIn("reviewer", args); self.assertIn("r.md", args)
            self.assertEqual(open(os.path.join(td, ".fleet", "fleet-x-rev-1.limit-fallback.prompt.md")).read(), "review it")

    def test_codex_review_launch_failure_reported(self):
        with tempfile.TemporaryDirectory() as td:
            st = {"task_dir": td, "name": "r", "cmd": ["claude", "-p", "p"]}
            bad = V.LimitFallback.launch_codex_review(st, fleet_bin=self.fake_fleet_bin(td, '{"error": "cap"}', rc=3))
            self.assertFalse(bad["launched"])
            self.assertIn("cap", bad["error"])


class VigiaRunEndToEnd(unittest.TestCase):
    def run_v(self, td, tool, role):
        import json, subprocess
        env = dict(os.environ, VIGIA_BASE=os.path.join(td, "v"), VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3",
                   ORQ_RELAUNCH_ARGV=json.dumps(["/x/fleet-run", "--complexity", "M"]))
        cmd = [os.path.join(ROOT, "bin", "vigia-run"), "--tool", tool, "--task-dir", td, "--name", f"{tool}-{role}",
               "--role", role, "--foreground", "--", "sh", "-c", f"echo \"{SAMPLE}\" >&2; exit 1"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.load(open(os.path.join(td, "runs", f"{tool}-{role}.status.json")))

    def test_claude_lead_pauses_with_resume_file(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            st = self.run_v(td, "claude", "lead")
            self.assertEqual(st["state"], "limit_paused")
            res = json.load(open(os.path.join(td, ".fleet", "limit-resume.json")))
            self.assertEqual(res["resume_command"], ["/x/fleet-run", "--complexity", "M"])

    def test_lead_exit_0_with_limit_stays_paused_not_incomplete(self):
        import json, subprocess
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, VIGIA_BASE=os.path.join(td, "v"), VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3")
            cmd = [os.path.join(ROOT, "bin", "vigia-run"), "--tool", "claude", "--task-dir", td,
                   "--name", "fleet-x-lead-1", "--role", "lead", "--foreground", "--",
                   "sh", "-c", f"echo \"{SAMPLE}\" >&2; exit 0"]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            st = json.load(open(os.path.join(td, "runs", "fleet-x-lead-1.status.json")))
            self.assertEqual(st["state"], "limit_paused")
            self.assertNotIn("incomplete_reason", st)

    def review_with_stdout_limit(self, td, codex_script):
        """A Claude review whose stream ends with the limit as its final result (stdout, exit 0)."""
        import json, stat, subprocess
        fb = os.path.join(td, "fbin"); os.makedirs(fb)
        with open(os.path.join(fb, "fleet-codex"), "w") as f:
            f.write(codex_script)
        os.chmod(os.path.join(fb, "fleet-codex"), 0o755)
        fake = os.path.join(td, "claude-fake")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n" + json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                                           "result": SAMPLE}) + "\nEOF\nexit 0\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ, VIGIA_BASE=os.path.join(td, "v"), VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3", FLEET_BIN=fb)
        cmd = [os.path.join(ROOT, "bin", "vigia-run"), "--tool", "claude", "--task-dir", td, "--name", "fleet-x-rev-1",
               "--role", "review", "--output", os.path.join(td, "review", "r.md"), "--foreground", "--", fake, "-p", "review it"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.load(open(os.path.join(td, "runs", "fleet-x-rev-1.status.json")))

    def test_limit_in_final_result_falls_back_and_waiters_see_a_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            sp, seen = os.path.join(td, "runs", "fleet-x-rev-1.status.json"), os.path.join(td, "seen")
            st = self.review_with_stdout_limit(td, (
                f"#!/bin/sh\npython3 -c \"import json;print(json.load(open('{sp}'))['state'])\" > {seen}\n"
                "echo '{\"state\": \"running\", \"status_path\": \"/s/new.json\"}'\n"))
            self.assertEqual(open(seen).read().strip(), "limit_handoff")   # not terminal while the relaunch is decided
            self.assertNotIn("limit_handoff", V.FINAL_STATES)
            self.assertEqual(st["state"], "limit_fallback")
            self.assertEqual(st["fallback_status_path"], "/s/new.json")

    def test_failed_relaunch_restores_the_prior_state(self):
        with tempfile.TemporaryDirectory() as td:
            st = self.review_with_stdout_limit(td, "#!/bin/sh\necho '{\"error\": \"cap\"}'\nexit 3\n")
            self.assertEqual(st["state"], "failed")      # exit 0 but no review produced: not done
            self.assertIn("FAILED", st["summary"])

    def test_fallback_exception_still_ends_in_a_final_state(self):
        with tempfile.TemporaryDirectory() as td:
            open(os.path.join(td, ".fleet"), "w").close()   # the fallback prompt file cannot be written
            st = self.review_with_stdout_limit(td, "#!/bin/sh\necho '{\"state\": \"running\"}'\n")
            self.assertEqual(st["state"], "failed")
            self.assertIn("FAILED", st["summary"])

    def run_review(self, td, name, script):
        import json, subprocess
        env = dict(os.environ, VIGIA_BASE=os.path.join(td, "v"), VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3")
        out = os.path.join(td, "review", "r.md")
        cmd = [os.path.join(ROOT, "bin", "vigia-run"), "--tool", "codex", "--task-dir", td, "--name", name,
               "--role", "review", "--output", out, "--foreground", "--", "sh", "-c", script.replace("OUT", out)]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out, json.load(open(os.path.join(td, "runs", f"{name}.status.json")))

    def test_review_output_is_this_runs_or_the_run_fails(self):
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "review"))
            open(os.path.join(td, "review", "r.md"), "w").write("OLD ROUND\n")
            out, st = self.run_review(td, "rev-a", "exit 0")          # wrote nothing: the old file must not count
            self.assertEqual(st["state"], "failed")
            self.assertIn("--output", st["summary"])
            self.assertFalse(os.path.exists(out))
            self.assertEqual(open(out + ".prev").read(), "OLD ROUND\n")
            out, st = self.run_review(td, "rev-b", "printf 'NEW\\n' > OUT")
            self.assertEqual(st["state"], "done")
            self.assertEqual(st["output_sha256"], hashlib.sha256(b"NEW\n").hexdigest())

    def test_codex_run_is_not_a_claude_limit(self):
        with tempfile.TemporaryDirectory() as td:
            st = self.run_v(td, "codex", "lead")
            self.assertNotIn("limit", st)
            self.assertNotEqual(st["state"], "limit_paused")


if __name__ == "__main__":
    unittest.main()
