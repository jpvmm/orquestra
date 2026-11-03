#!/usr/bin/env python3
"""Tests for the per-run webhook in vigia-notify (VIGIA_WEBHOOK_ENV / "webhook_env" in status.json).
Everything runs with VIGIA_BASE in a temp folder and a local HTTP receiver: never reads or uses a real webhook.env.
Run: python3 tests/test_vigia_notify.py"""
import http.server, json, os, subprocess, sys, tempfile, threading, unittest
sys.dont_write_bytecode = True
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
NOTIFY = os.path.join(BIN, "vigia-notify")


class Receiver:
    """Local HTTP server that keeps the POSTs it receives."""
    def __init__(self):
        received = self.received = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                received.append({"auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(n))})
                self.send_response(200); self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


class PerRunWebhook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "vigia")
        os.makedirs(self.base)
        self.default, self.other = Receiver(), Receiver()
        self._env(os.path.join(self.base, "webhook.env"), self.default.url, "default-key")
        self.alt = os.path.join(self.tmp.name, "bot2", "webhook.env")
        os.makedirs(os.path.dirname(self.alt))
        self._env(self.alt, self.other.url, "bot2-key")

    def tearDown(self):
        self.default.close(); self.other.close(); self.tmp.cleanup()

    def _env(self, path, url, key):
        with open(path, "w") as f:
            f.write(f"VIGIA_WEBHOOK_URL={url}\nVIGIA_WEBHOOK_KEY={key}\n")

    def _status(self, name, **extra):
        runs = os.path.join(self.tmp.name, "task", "runs")
        os.makedirs(runs, exist_ok=True)
        sp = os.path.join(runs, name + ".status.json")
        st = {"name": name, "tool": "claude", "state": "done", "task": "test", "task_dir": os.path.dirname(runs),
              "exit_code": 0, "summary": "ok", **extra}
        with open(sp, "w") as f:
            json.dump(st, f)
        return sp

    def _notify(self, sp, env_extra=None, event="finished", state="done", prev="running"):
        env = dict(os.environ, VIGIA_BASE=self.base)
        env.pop("VIGIA_WEBHOOK_ENV", None)
        env.pop("VIGIA_NOTIFY_STARTED", None)
        env.update(env_extra or {})
        return subprocess.run([NOTIFY, sp, "--event", event, "--state", state, "--prev", prev],
                              env=env, capture_output=True, text=True, timeout=60)

    def _logged(self, name):
        with open(os.path.join(self.base, "events.jsonl")) as f:
            return [json.loads(ln) for ln in f if json.loads(ln).get("run") == name]

    def test_default_unchanged(self):
        r = self._notify(self._status("r1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.default.received), 1)
        self.assertEqual(self.default.received[0]["auth"], "Bearer default-key")
        self.assertEqual(self.other.received, [])

    def test_environment_variable(self):
        r = self._notify(self._status("r2"), {"VIGIA_WEBHOOK_ENV": self.alt})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received, [])
        self.assertEqual(len(self.other.received), 1)
        self.assertEqual(self.other.received[0]["auth"], "Bearer bot2-key")
        self.assertEqual(self.other.received[0]["body"]["run"], "r2")

    def test_stored_in_status_beats_environment(self):
        # e.g. one bot's vigia-status cleaning up another bot's run: the notification must go to the run's own bot
        r = self._notify(self._status("r3", webhook_env=self.alt))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received, [])
        self.assertEqual(len(self.other.received), 1)

    def test_missing_file_sends_nothing(self):
        r = self._notify(self._status("r4"), {"VIGIA_WEBHOOK_ENV": os.path.join(self.tmp.name, "missing.env")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received + self.other.received, [])
        self.assertEqual(len(self._logged("r4")), 1)  # but it is recorded

    def test_empty_in_status_mutes(self):
        r = self._notify(self._status("r5", webhook_env=""))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received + self.other.received, [])
        self.assertEqual(len(self._logged("r5")), 1)

    def test_empty_environment_mutes(self):
        r = self._notify(self._status("r5b"), {"VIGIA_WEBHOOK_ENV": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received + self.other.received, [])

    def test_old_status_without_field_uses_default(self):
        r = self._notify(self._status("r6"))  # status without "webhook_env" (runs from before this change)
        self.assertEqual(len(self.default.received), 1, r.stderr)

    def test_started_is_logged_not_posted(self):
        sp = self._status("r7", state="running")
        r = self._notify(sp, event="started", state="running", prev="starting")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.default.received, [])
        self.assertEqual([e["event"] for e in self._logged("r7")], ["started"])

    def test_started_posted_when_enabled(self):
        sp = self._status("r8", state="running")
        r = self._notify(sp, {"VIGIA_NOTIFY_STARTED": "1"}, event="started", state="running", prev="starting")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.default.received), 1)
        self.assertEqual(self.default.received[0]["body"]["event"], "started")

    def test_finished_sent_once(self):
        sp = self._status("r9")
        self._notify(sp); self._notify(sp)
        self.assertEqual(len(self.default.received), 1)


class VigiaRunStoresIt(unittest.TestCase):
    def test_webhook_env_from_environ(self):
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("vigia_run_mod", os.path.join(BIN, "vigia-run"))
        spec = importlib.util.spec_from_loader("vigia_run_mod", ld)
        m = importlib.util.module_from_spec(spec); ld.exec_module(m)
        old = os.environ.pop("VIGIA_WEBHOOK_ENV", None)
        try:
            self.assertIsNone(m.webhook_env_from_environ())
            os.environ["VIGIA_WEBHOOK_ENV"] = ""
            self.assertEqual(m.webhook_env_from_environ(), "")
            os.environ["VIGIA_WEBHOOK_ENV"] = "/x/../y/webhook.env"
            self.assertEqual(m.webhook_env_from_environ(), "/y/webhook.env")
        finally:
            os.environ.pop("VIGIA_WEBHOOK_ENV", None)
            if old is not None:
                os.environ["VIGIA_WEBHOOK_ENV"] = old


if __name__ == "__main__":
    unittest.main()
