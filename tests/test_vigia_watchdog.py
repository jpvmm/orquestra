#!/usr/bin/env python3
"""Tests for the vigia-run caps and the vigia-status watchdog (wall-clock cap across suspension, stall with CPU,
fleet lead without FINAL-SUMMARY.md, supervisor that stopped watching, PID reuse).
Everything runs with VIGIA_BASE in a temp folder and VIGIA_WEBHOOK_ENV="" (no webhook): never touches real state.
Run: python3 tests/test_vigia_watchdog.py"""
import json, os, signal, subprocess, sys, tempfile, time, unittest
sys.dont_write_bytecode = True
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
RUN, STATUS = os.path.join(BIN, "vigia-run"), os.path.join(BIN, "vigia-status")


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "vigia")
        self.td = os.path.join(self.tmp.name, "task")
        os.makedirs(self.base); os.makedirs(self.td)
        self.env = dict(os.environ, VIGIA_BASE=self.base, VIGIA_WEBHOOK_ENV="", VIGIA_POLL_SEC="0.3",
                        VIGIA_KILL_GRACE="1", VIGIA_WD_RECHECK_SEC="1", VIGIA_HB_STALE_SEC="2", VIGIA_WD_GRACE_SEC="0")

    def tearDown(self):
        self.tmp.cleanup()

    def run_fg(self, name, cmd, *opts, timeout=60):
        r = subprocess.run([RUN, "--tool", "other", "--task-dir", self.td, "--name", name, "--foreground", *opts, "--", *cmd],
                           env=self.env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.status(name)

    def status(self, name):
        with open(os.path.join(self.td, "runs", name + ".status.json")) as f:
            return json.load(f)

    def events(self, name):
        p = os.path.join(self.base, "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [e for e in map(json.loads, f) if e.get("run") == name]

    def kinds(self, name):
        return [(e["event"], e["state"]) for e in self.events(name)]


class Caps(Base):
    def test_timeout_kills(self):
        t = time.time()
        st = self.run_fg("t-timeout", ["sleep", "60"], "--max-min", "0.05", "--stall-min", "5")
        self.assertEqual(st["state"], "timeout")
        self.assertLess(time.time() - t, 20)
        self.assertTrue(st["kill"]["ok"])
        self.assertIn(("detected", "running"), self.kinds("t-timeout"))
        self.assertIn(("finished", "timeout"), self.kinds("t-timeout"))

    def test_stall_kills(self):
        st = self.run_fg("t-stall", ["sleep", "60"], "--max-min", "5", "--stall-min", "0.05")
        self.assertEqual(st["state"], "stalled")
        self.assertIn(("finished", "stalled"), self.kinds("t-stall"))

    def test_stall_even_with_cpu(self):
        # a live process spinning CPU without producing a byte: used to never stall (CPU counted as life)
        self.env["VIGIA_STALL_CPU_MULT"] = "3"
        st = self.run_fg("t-cpu", [sys.executable, "-c", "while True: pass"], "--max-min", "2", "--stall-min", "0.03")
        self.assertEqual(st["state"], "stalled")
        self.assertIn("even with CPU", st["summary"])

    def test_suspended_box_counts_toward_cap(self):
        """A paused VM stops the monotonic clock. Simulate the wall clock jumping 1 h after 2 s."""
        boot = (
            "import importlib.machinery, importlib.util, sys, time\n"
            "sys.dont_write_bytecode = True\n"
            "real, m0 = time.time, time.monotonic()\n"
            "time.time = lambda: real() + (3600 if time.monotonic() - m0 > 2 else 0)\n"
            f"ld = importlib.machinery.SourceFileLoader('vigia_run', {RUN!r})\n"
            "mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('vigia_run', ld)); ld.exec_module(mod)\n"
            "sys.argv = ['vigia-run'] + sys.argv[1:]; mod.main()\n")
        r = subprocess.run([sys.executable, "-c", boot, "--tool", "other", "--task-dir", self.td, "--name", "t-susp",
                            "--foreground", "--max-min", "30", "--stall-min", "20", "--", "sleep", "60"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.status("t-susp")
        self.assertEqual(st["state"], "timeout")
        self.assertGreaterEqual(st["suspended_sec"], 3500)
        self.assertTrue(any("box suspended" in a for a in st["alerts"]))
        self.assertIn(("alert", "running"), self.kinds("t-susp"))


class FleetLead(Base):
    def test_lead_without_final_summary_is_incomplete(self):
        cmd = ["sh", "-c", "echo 'Background tasks still running 10m after the last turn (subagent \"dev\"); stopping them.' >&2; exit 0"]
        st = self.run_fg("fleet-x-lead-000001", cmd, "--max-min", "1")
        self.assertEqual(st["state"], "incomplete")
        self.assertIn("FINAL-SUMMARY", st["summary"])
        self.assertTrue(any("Background tasks still running" in a for a in st["alerts"]))
        kinds = self.kinds("fleet-x-lead-000001")
        self.assertIn(("alert", "running"), kinds)
        self.assertIn(("detected", "incomplete"), kinds)
        self.assertEqual(kinds[-1], ("finished", "incomplete"))

    def test_lead_with_final_summary_is_done(self):
        st = self.run_fg("fleet-x-lead-000002", ["sh", "-c", f"echo ok > {self.td}/FINAL-SUMMARY.md"], "--max-min", "1")
        self.assertEqual(st["state"], "done")

    def test_old_final_summary_does_not_count(self):
        fs = os.path.join(self.td, "FINAL-SUMMARY.md")
        with open(fs, "w") as f:
            f.write("old")
        old = time.time() - 3600
        os.utime(fs, (old, old))
        st = self.run_fg("fleet-x-lead-000003", ["true"], "--max-min", "1")
        self.assertEqual(st["state"], "incomplete")

    def test_non_lead_stays_done(self):
        self.assertEqual(self.run_fg("dev-x", ["true"], "--max-min", "1")["state"], "done")


class Watchdog(Base):
    def test_stopped_supervisor_past_cap(self):
        """A supervisor that stopped watching (SIGSTOP): vigia-status kills the group, marks timeout, notifies once."""
        r = subprocess.run([RUN, "--tool", "other", "--task-dir", self.td, "--name", "t-wd", "--max-min", "0.05",
                            "--stall-min", "10", "--", "sleep", "300"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        sup, child = info["supervisor_pid"], info["pid"]
        os.kill(sup, signal.SIGSTOP)
        try:
            time.sleep(5)  # past the cap (3 s) and the heartbeat (2 s) without the supervisor watching
            out = subprocess.run([STATUS, "--short"], env=self.env, capture_output=True, text=True, timeout=60)
            self.assertIn("watchdog: t-wd: timeout", out.stderr)
            st = self.status("t-wd")
            self.assertEqual(st["state"], "timeout")
            self.assertIn("watchdog", st["summary"])
            self.assertTrue(st["kill"]["ok"])
            self.assertFalse(alive(child))
            subprocess.run([STATUS, "--short"], env=self.env, capture_output=True, text=True, timeout=60)  # 2nd time: no repeat
        finally:
            os.kill(sup, signal.SIGCONT)
        for _ in range(100):  # the supervisor wakes up, sees the child dead and does not overwrite the watchdog's state
            if not os.path.exists(f"/proc/{sup}"):
                break
            time.sleep(0.2)
        st = self.status("t-wd")
        self.assertEqual(st["state"], "timeout")
        kinds = self.kinds("t-wd")
        self.assertEqual(kinds.count(("detected", "running")), 1)
        self.assertEqual([k for k in kinds if k[0] == "finished"], [("finished", "timeout")])

    def test_healthy_supervisor_is_left_alone(self):
        """Fresh heartbeat and within the cap: the watchdog does nothing."""
        r = subprocess.run([RUN, "--tool", "other", "--task-dir", self.td, "--name", "t-ok", "--max-min", "1",
                            "--stall-min", "10", "--", "sleep", "300"], env=self.env, capture_output=True, text=True, timeout=30)
        info = json.loads(r.stdout)
        try:
            time.sleep(3)
            out = subprocess.run([STATUS, "--short"], env=self.env, capture_output=True, text=True, timeout=60)
            self.assertNotIn("watchdog", out.stderr)
            self.assertEqual(self.status("t-ok")["state"], "running")
            self.assertEqual([k for k in self.kinds("t-ok") if k[0] != "started"], [])
        finally:
            os.kill(info["supervisor_pid"], signal.SIGTERM)
            for _ in range(100):
                if self.status("t-ok")["state"] != "running":
                    break
                time.sleep(0.2)
        self.assertEqual(self.status("t-ok")["state"], "killed")


class PidReuse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["VIGIA_BASE"] = os.path.join(cls.tmp.name, "vigia")  # before importing vigia_lib
        os.environ["VIGIA_WEBHOOK_ENV"] = ""
        sys.path.insert(0, BIN)
        import vigia_lib
        cls.V = vigia_lib

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_reused_pid_is_not_a_target(self):
        """The recorded pid exists but with another start time (reuse): neither it nor its group is killed."""
        V = self.V
        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            real = V.proc_stat(p.pid)["start"]
            g, sel = V._valid_targets(p.pid, p.pid, real + 1)
            self.assertIsNone(g); self.assertEqual(sel, set())
            r = V._kill_pg(p.pid, p.pid, real + 1)
            self.assertIsNone(p.poll())  # still alive
            self.assertEqual(r["targets"], [])
            g, sel = V._valid_targets(p.pid, p.pid, real)
            self.assertEqual(sel, {p.pid})
        finally:
            p.kill(); p.wait()

    def test_zombie_leader_is_not_reuse(self):
        """The leader dies on SIGTERM and stays a zombie (nobody reaps it); a child ignores TERM: it must get SIGKILL."""
        V = self.V
        p = subprocess.Popen([sys.executable, "-c", "import subprocess, signal; "
                              "subprocess.Popen(['sh', '-c', 'trap \"\" TERM; sleep 30; :']); signal.pause()"],
                             start_new_session=True)
        try:
            start = V.proc_stat(p.pid)["start"]
            end = time.time() + 5
            while time.time() < end and len(V.members(p.pid, [p.pid])[0]) < 3:
                time.sleep(0.1)
            r = V._kill_pg(p.pid, p.pid, start)
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["kill_sent"])
            self.assertEqual(V.members(p.pid, [p.pid])[0], set())
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
