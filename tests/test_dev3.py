"""Tests for 1.3.0-dev.3: the faults Project Sol's Director hit on 1.2.0 and dev.2, check slots and quiet runs,
upgrades over an open sprint, the tool contract, and configurable memory sources.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_coniso import MOD, PLUGIN, PY, SCRIPT, Repo, seed, ticket

WINDOWS = os.name == "nt"


def alive(pid):
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pid):
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def orphan(code):
    """Start `python -c code` from a parent that exits at once, so the process has no living parent; its pid."""
    r = subprocess.run([PY, "-c", "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', sys.argv[1]], "
                                  "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)",
                        code], capture_output=True, text=True, timeout=30)
    return int(r.stdout.strip())


# ---------------------------------------------------------------- Part 1, fault 1: the time limit
class TimeLimit(unittest.TestCase):
    """Project Sol's test_coniso_timeout.py, as unit tests. dev.2 failed 3 of these 8 checks."""

    GRAND = 25     # seconds the grandchild sleeps: long enough to see whether it outlives the limit

    def test_the_whole_tree_stops_at_the_limit_and_output_is_kept(self):
        py = sys.executable.replace("\\", "/")
        inner = (f"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep({self.GRAND})']); "
                 f"print('grandchild',p.pid,flush=True); print('to stderr',file=sys.stderr,flush=True); time.sleep({self.GRAND})")
        t0 = time.time()
        r = MOD.run(f'"{py}" -c "{inner}"', shell=True, timeout=4)
        took = time.time() - t0
        m = re.search(r"grandchild (\d+)", r.stdout or "")
        self.assertEqual(r.returncode, 124)                                   # 1. exit code 124 at the limit
        self.assertLess(took, 15, "returns when the grandchild ends, not at the limit")      # 2.
        self.assertIsNotNone(m, f"stdout before the limit was lost: {r.stdout!r}")           # 3.
        self.assertIn("to stderr", r.stderr or "")                            # 4. stderr kept, then the TIMEOUT line
        self.assertIn("TIMEOUT after 4s", r.stderr or "")
        time.sleep(1)
        self.assertFalse(alive(int(m.group(1))), "the grandchild outlived the limit")       # 5.

    def test_a_command_inside_the_limit_keeps_its_code_and_streams(self):
        py = sys.executable.replace("\\", "/")
        r = MOD.run(f'"{py}" -c "import sys; print(\'a\'); print(\'b\', file=sys.stderr); sys.exit(3)"', shell=True, timeout=30)
        self.assertEqual(r.returncode, 3)                                     # 6.
        self.assertEqual((r.stdout, r.stderr), ("a\n", "b\n"))                # 7.

    def test_no_limit_and_bytes_that_are_not_utf8(self):
        r = MOD.run([sys.executable, "-c", "print('x')"])
        self.assertEqual((r.returncode, r.stdout), (0, "x\n"))
        py = sys.executable.replace("\\", "/")
        r = MOD.run(f'"{py}" -c "import sys; sys.stdout.buffer.write(bytes([255,254,10]))"', shell=True, timeout=30)
        self.assertEqual(r.returncode, 0)                                     # 8.
        self.assertTrue(r.stdout.endswith("\n"))


class TimeLimitInACheckRun(Repo):
    tickets = (ticket("P1-01"),)

    def grand_cmd(self):
        script = self.root / "slow_check.py"
        script.write_text("import subprocess, sys, time\n"
                          "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)'])\n"
                          "print('grandchild', p.pid, flush=True)\n"
                          "time.sleep(25)\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "slow check")
        return f'"{PY}" slow_check.py'

    def test_a_real_check_run_hitting_the_limit_stops_and_keeps_its_output(self):
        self.c("init", "--test", self.grand_cmd(), "--test-timeout", "3")
        self.build("P1-01")
        t0 = time.time()
        out = self.c("test", "P1-01", code=1)
        self.assertLess(time.time() - t0, 20)
        self.assertIn("FAIL (exit 124)", out)
        log = (self.root / ".coniso" / "reports" / "P1-01.test.log").read_text(encoding="utf-8")
        self.assertIn("TIMEOUT after 3s", log)
        self.assertIn("NO RERUN: the run hit its time limit", log)          # a timed-out run is not repeated
        pid = int(re.search(r"grandchild (\d+)", log).group(1))
        time.sleep(1)
        self.assertFalse(alive(pid))

    def test_merge_timeout_is_its_own_limit(self):
        self.c("init", "--test", self.grand_cmd(), "--test-timeout", "600", "--merge-timeout", "3")
        self.assertEqual(json.loads((self.root / ".coniso" / "config.json").read_text(encoding="utf-8"))["merge_timeout"], 3)
        self.build("P1-01", fname="a.txt", body="a\n")
        t0 = time.time()
        out = self.c("merge", "P1-01", "--force", code=4)
        self.assertLess(time.time() - t0, 30)
        self.assertIn("INTEGRATION FAIL P1-01", out)
        self.assertIn("TIMEOUT after 3s", (self.root / ".coniso" / "reports" / "P1-01.merge-test.log").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- Part 1, faults 2 to 6
class ReviewAndVerdictFaults(Repo):
    tickets = (ticket("P1-01", accept=("a", "b")), ticket("P1-02"))

    def first_line(self, unit):
        return (self.root / ".coniso" / "assignments" / f"{unit}.md").read_text(encoding="utf-8").splitlines()[0]

    def test_review_model_opus_on_a_sonnet_ticket_writes_an_opus_reviewer(self):           # fault 2
        self.build("P1-01")
        out = self.c("review", "P1-01", "--model", "opus")
        self.assertIn("SPAWN coniso:reviewer · model opus", out)
        self.assertEqual(self.first_line("P1-01.R1"), "ASSIGNMENT P1-01.R1 · reviewer · model opus · from director")

    def test_a_verdict_with_a_short_unit_name_is_ruled(self):                              # fault 3
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.report("P1-01.R1.md", "VERDICT P1-01 · R1 · lens correctness+spec: pass\n"
                                         "A1 met — out.txt:1\nA2 met — log\nTESTS: pass · log\nFIX LIST: none\n")
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("RULED pass P1-01", out)
        self.assertNotIn("waiting on", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "passed")

    P155_SHAPE = ("VERDICT P1-01 · P1-01.R1 · lens spec+regression: pass-with-fixes\n"
                  "A1 met — R1's blocker is closed: probe exits 1 on all seven cases.\n"
                  "A2 met — unchanged from R1.\n\n"
                  "## Commands run (work folder)\ncheck 0 · test 0\n\n"
                  "## FINDINGS\n"
                  "1. [minor] Holes not in the cannot-catch list. Fixed when: each is caught or listed.\n"
                  "2. [minor] False alarms on honest text. Fixed when: stated as known false alarms.\n"
                  "3. [minor] physics.md:178 wording points the wrong way. Fixed when it reads ...\n\n"
                  "Gaming check: no test deleted, skipped or loosened.\n\n"
                  "TESTS: pass · log\nFIX LIST: none (minors only)\nFLAGS: none\n")

    def test_findings_under_a_heading_are_counted_and_the_pass_says_with_fixes(self):      # fault 4
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.report("P1-01.R1.md", self.P155_SHAPE)
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("RULED pass P1-01", out)
        self.assertIn("m3", out)
        self.assertIn("WITH FIXES: 3 minor finding(s) to read before landing", out)
        out = self.c("land", "P1-01")
        self.assertIn("WITH FIXES P1-01: passed with 3 minor finding(s) carried", out)
        self.assertIn("MERGED P1-01", out)

    def test_pass_with_fixes_and_no_tag_still_says_so(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.report("P1-01.R1.md", "VERDICT P1-01 · P1-01.R1 · lens spec: pass-with-fixes\nA1 met — x\nA2 met — y\n"
                                         "Two small things to tidy, see below.\nTESTS: pass · log\nFIX LIST: none\n")
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("WITH FIXES: the reviewer said pass-with-fixes but no [minor] tag was counted", out)

    def test_a_clean_pass_has_no_with_fixes(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.verdict_report("P1-01", "P1-01.R1", "pass", ["A1 met — x", "A2 met — y"])
        self.assertNotIn("WITH FIXES", self.c("verdict", "P1-01", "--report", str(rep)))

    def test_sync_refuses_a_ticket_in_review(self):                                       # fault 5, second half
        self.build("P1-01")
        self.c("review", "P1-01")
        self.assertIn("P1-01 is in review", self.c("sync", "P1-01", code=8))
        self.assertIn("SYNC P1-01", self.c("sync", "P1-01", "--force"))

    def test_quickfix_takes_a_built_ticket_and_its_first_review_covers_it(self):          # fault 6
        self.build("P1-01")
        self.assertIn("QUICKFIX P1-01 open", self.c("quickfix", "P1-01", "begin"))
        (self.wt("P1-01") / "out.txt").write_text("hi\n", encoding="utf-8")
        out = self.c("quickfix", "P1-01", "done", "--note", "seam fixed")
        self.assertIn("QUICKFIX OK P1-01", out)
        self.assertIn("Next: review P1-01   (its first review covers the quick fix too)", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "built")


class LandDuringReview(Repo):
    """Fault 5: a land used to put its squash commits on the branch before testing them, so a review opened during
    the land synced untested commits into the ticket."""
    tickets = (ticket("P1-01"), ticket("P1-02"), ticket("P1-03"))

    def slow_checks(self, seconds):
        started = (self.tmp / "checks-started").as_posix()
        (self.root / "slow.py").write_text(f"import time\nopen({started!r}, 'w').close()\ntime.sleep({seconds})\n",
                                           encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "slow check")
        self.c("init", "--test", f'"{PY}" slow.py')

    def pass_ticket(self, tid, fname):
        self.build(tid, fname)
        self.c("review", tid)
        rep = self.verdict_report(tid, f"{tid}.R1", "pass", ["A1 met — ok"])
        self.c("verdict", tid, "--report", str(rep))

    def start_land(self, *ids):
        proc = subprocess.Popen([PY, str(self.root / ".coniso" / "coniso.py"), "land", *ids], cwd=str(self.root),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        marker, started = self.root / ".coniso" / "landing.json", self.tmp / "checks-started"
        if started.exists():
            started.unlink()
        deadline = time.time() + 30
        while time.time() < deadline:                 # wait until the batch is committed and its checks are running
            if marker.exists() and started.exists():
                return proc
            time.sleep(0.2)
        proc.kill()
        self.fail("the land never reached its checks")

    def test_review_during_a_land_sees_only_tested_commits(self):
        self.slow_checks(0)
        self.pass_ticket("P1-01", "a.txt")
        self.pass_ticket("P1-02", "b.txt")
        self.build("P1-03", "c.txt")
        start = self.git("rev-parse", "main").strip()
        self.slow_checks(6)
        start = self.git("rev-parse", "main").strip()
        proc = self.start_land("P1-01", "P1-02")
        try:
            self.assertEqual(self.git("rev-parse", "main").strip(), start)      # the branch has not moved
            self.c("review", "P1-03")                                            # syncs P1-03 with main mid-land
            self.assertEqual(self.sprint()["tickets"]["P1-03"]["base"], start)
            self.assertFalse((self.wt("P1-03") / "a.txt").exists())
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                stop(proc.pid)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("BATCH 2 tickets", out)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertNotEqual(self.git("rev-parse", "main").strip(), start)
        self.assertFalse((self.root / ".coniso" / "landing.json").exists())

    def test_a_second_land_waits_for_the_first(self):
        self.slow_checks(0)
        self.pass_ticket("P1-01", "a.txt")
        self.pass_ticket("P1-02", "b.txt")
        self.pass_ticket("P1-03", "c.txt")
        self.slow_checks(6)
        proc = self.start_land("P1-01", "P1-02")
        try:
            self.assertIn("a land is running", self.c("land", "P1-03", code=8))
            proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                stop(proc.pid)
        self.assertIn("MERGED P1-03", self.c("land", "P1-03"))

    def test_a_land_whose_process_died_is_recovered(self):
        self.slow_checks(0)
        self.pass_ticket("P1-01", "a.txt")
        self.pass_ticket("P1-02", "b.txt")
        self.slow_checks(60)
        start = self.git("rev-parse", "main").strip()
        proc = self.start_land("P1-01", "P1-02")
        MOD.kill_tree(proc)                                                     # the land dies mid-checks
        proc.communicate(timeout=30)
        self.assertEqual(self.git("rev-parse", "main").strip(), start)
        self.assertIn("INTERRUPTED LAND of P1-01, P1-02", self.c("preflight", code=None))
        self.c("init", "--test", f'"{PY}" -c "print(1)"')
        out = self.c("land", "P1-01", "P1-02")
        self.assertIn("RECOVERED an interrupted land", out)
        self.assertIn("back to passed: P1-01, P1-02", out)
        self.assertIn("BATCH 2 tickets", out)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")


# ---------------------------------------------------------------- Part 2: check slots, quiet runs, leftovers
class CheckSlots(Repo):
    tickets = (ticket("P1-01"), ticket("P1-02"), ticket("P1-03"))

    def setUp(self):
        super().setUp()
        self.stamps = self.tmp / "stamps.txt"
        (self.root / "stamp.py").write_text(
            "import os, sys, time\n"
            "f = os.environ['STAMP']\n"
            "name = os.path.basename(os.getcwd())\n"
            "open(f, 'a').write(f'start {name} {time.time()}\\n')\n"
            "time.sleep(float(os.environ.get('HOLD', '1.5')))\n"
            "open(f, 'a').write(f'end {name} {time.time()}\\n')\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "stamp")
        self.c("init", "--test", f'"{PY}" stamp.py', "--env", f"STAMP={self.stamps}")

    def config(self, **kv):
        cf = self.root / ".coniso" / "config.json"
        conf = json.loads(cf.read_text(encoding="utf-8"))
        conf.update(kv, check_slot_poll_seconds=0.2)
        cf.write_text(json.dumps(conf), encoding="utf-8")

    def spawn(self, *args, hold=1.5):
        env = dict(os.environ, HOLD=str(hold))
        return subprocess.Popen([PY, str(self.root / ".coniso" / "coniso.py"), *args], cwd=str(self.root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def intervals(self):
        out = {}
        for ln in self.stamps.read_text(encoding="utf-8").splitlines():
            kind, name, t = ln.split()
            out.setdefault(name, {})[kind] = float(t)
        return out

    def wait_running(self, n=1):
        d = self.root / ".coniso" / "slots"
        deadline = time.time() + 30
        while time.time() < deadline:
            states = [json.loads(f.read_text(encoding="utf-8")).get("state") for f in d.glob("claim-*.json")] if d.exists() else []
            if states.count("running") >= n and self.stamps.exists():
                return
            time.sleep(0.1)
        self.fail("no check run started")

    def passed(self, tid, fname):
        self.build(tid, fname)
        self.c("review", tid)
        rep = self.verdict_report(tid, f"{tid}.R1", "pass", ["A1 met — ok"])
        self.c("verdict", tid, "--report", str(rep))

    def test_one_slot_runs_checks_one_at_a_time(self):
        self.config(check_slots=1)
        self.build("P1-01", "a.txt")
        self.build("P1-02", "b.txt")
        a, b = self.spawn("test", "P1-01"), self.spawn("test", "P1-02")
        for pr in (a, b):
            out, _ = pr.communicate(timeout=120)
            self.assertEqual(pr.returncode, 0, out)
        iv = self.intervals()
        first, second = sorted(iv.values(), key=lambda x: x["start"])
        self.assertLessEqual(first["end"], second["start"], iv)

    def test_two_slots_let_two_run_together(self):
        self.config(check_slots=2)
        self.build("P1-01", "a.txt")
        self.build("P1-02", "b.txt")
        a, b = self.spawn("test", "P1-01", hold=3), self.spawn("test", "P1-02", hold=3)
        for pr in (a, b):
            pr.communicate(timeout=120)
        first, second = sorted(self.intervals().values(), key=lambda x: x["start"])
        self.assertLess(second["start"], first["end"])

    def test_busy_after_the_wait(self):
        self.config(check_slots=1, check_slot_wait_seconds=1)
        self.build("P1-01", "a.txt")
        self.build("P1-02", "b.txt")
        a = self.spawn("test", "P1-01", hold=5)
        self.wait_running()
        out = self.c("test", "P1-02", code=9)
        self.assertIn("TEST P1-02: BUSY", out)
        self.assertIn("1 of 1 check slot(s) in use", out)
        a.communicate(timeout=120)

    def test_a_land_goes_before_tests_already_waiting(self):
        self.config(check_slots=1)
        self.passed("P1-03", "c.txt")
        self.build("P1-01", "a.txt")
        self.build("P1-02", "b.txt")
        a = self.spawn("test", "P1-01", hold=3)
        self.wait_running()
        b = self.spawn("test", "P1-02", hold=1)
        time.sleep(0.8)
        land = self.spawn("land", "P1-03", hold=1)
        for pr in (a, b, land):
            out, _ = pr.communicate(timeout=180)
            self.assertEqual(pr.returncode, 0, out)
        order = [name for name, _ in sorted(((n, iv["start"]) for n, iv in self.intervals().items()), key=lambda x: x[1])]
        self.assertEqual(order, ["P1-01", "repo", "P1-02"])        # the land's checks run in the repo root

    def test_land_hold_keeps_new_runs_out_while_a_land_runs(self):
        self.config(check_slots=2, land_hold=True)
        self.passed("P1-03", "c.txt")
        self.build("P1-01", "a.txt")
        land = self.spawn("land", "P1-03", hold=3)
        self.wait_running()
        t = self.spawn("test", "P1-01", hold=0.5)
        for pr in (land, t):
            out, _ = pr.communicate(timeout=180)
            self.assertEqual(pr.returncode, 0, out)
        iv = self.intervals()
        self.assertGreaterEqual(iv["P1-01"]["start"], iv["repo"]["end"])

    def test_hand_runs_count_through_the_process_pattern_and_slot_claims(self):
        self.config(check_slots=1, check_process_pattern=r"stamp\.py")
        hand = subprocess.Popen([PY, "stamp.py"], cwd=str(self.root), env=dict(os.environ, STAMP=str(self.stamps), HOLD="6"))
        try:
            deadline = time.time() + 10
            while not self.stamps.exists() and time.time() < deadline:
                time.sleep(0.1)
            out = self.c("slot", "--now", code=9)
            self.assertIn("BUSY 1 of 1 check slot(s) in use", out)
        finally:
            hand.wait(timeout=30)
        self.assertIn("GO 0 check run(s) active", self.c("slot", "--now"))
        self.assertIn("GO the slot is yours", self.c("slot"))
        self.assertIn("BUSY", self.c("slot", "--now", code=9))          # the hand claim holds the slot

    def test_a_coniso_run_and_its_check_process_count_once(self):
        self.config(check_slots=2, check_process_pattern=r"stamp\.py")
        self.build("P1-01", "a.txt")
        a = self.spawn("test", "P1-01", hold=5)
        self.wait_running()
        time.sleep(1.0)
        self.assertIn("GO 1 check run(s) active", self.c("slot", "--now"))
        a.communicate(timeout=120)


class QuietRuns(Repo):
    tickets = (ticket("P1-01"),)

    def test_quiet_runs_the_command_n_times_and_holds_check_runs(self):
        cf = self.root / ".coniso" / "config.json"
        conf = json.loads(cf.read_text(encoding="utf-8"))
        conf.update(quiet_busy_cpu=100000, quiet_sample_seconds=0.2, check_slot_poll_seconds=0.2)
        cf.write_text(json.dumps(conf), encoding="utf-8")
        q = subprocess.Popen([PY, str(self.root / ".coniso" / "coniso.py"), "quiet", "--runs", "2", "--out", "q1", "--",
                              PY, "-c", "import time; print('timed'); time.sleep(1.5)"],
                             cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        deadline = time.time() + 30
        while not (self.root / ".coniso" / "slots" / "QUIET").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertIn("QUIET a quiet run holds the machine", self.c("slot", "--now", code=9))
        out, _ = q.communicate(timeout=120)
        self.assertEqual(q.returncode, 0, out)
        self.assertIn("QUIET RUNS 2 of 2 counted run(s) exited 0", out)
        res = self.root / "q1"
        self.assertIn("timed", (res / "run-1.txt").read_text(encoding="utf-8"))
        self.assertTrue((res / "run-2.processes.txt").exists())
        self.assertFalse((self.root / ".coniso" / "slots" / "QUIET").exists())
        self.assertIn("GO", self.c("slot", "--now"))

    def test_busy_detection_sees_any_busy_program(self):
        pid = orphan("import time\nt = time.time()\nwhile time.time() - t < 15: pass")   # not in this test's family
        try:
            time.sleep(0.5)
            busy = MOD.busy_processes({"quiet_busy_cpu": 0.5, "quiet_sample_seconds": 2})
            self.assertTrue(any(b.startswith(f"pid {pid} ") for b in busy), busy)
        finally:
            stop(pid)


class LeftoverProcesses(Repo):
    tickets = (ticket("P1-01"),)

    def test_preflight_lists_a_process_whose_parent_is_gone(self):
        cf = self.root / ".coniso" / "config.json"
        conf = json.loads(cf.read_text(encoding="utf-8"))
        conf["stray_min_age_seconds"] = 0
        cf.write_text(json.dumps(conf), encoding="utf-8")
        pid = orphan("import time; time.sleep(120)")
        try:
            time.sleep(1)
            out = self.c("preflight", code=None)
            self.assertIn("left running with no parent", out)
            self.assertIn(f"pid {pid} python", out)
        finally:
            stop(pid)


# ---------------------------------------------------------------- Part 3: upgrades, the contract, memory sources
class UpgradeFrom120(Repo):
    """init over a .coniso folder made by 1.2.0, with a sprint open and a ticket in review."""
    schedule = False

    def setUp(self):
        r = subprocess.run(["git", "-C", str(PLUGIN), "archive", "--format=tar", "v1.2.0", "scripts", "skills"],
                           capture_output=True)
        if r.returncode != 0:
            self.skipTest("the v1.2.0 tag is not available here")
        super().setUp()
        import io
        import shutil
        import tarfile
        old_plugin = self.tmp / "old"
        tarfile.open(fileobj=io.BytesIO(r.stdout)).extractall(old_plugin)
        shutil.rmtree(self.root / ".coniso")
        self.sh(PY, str(old_plugin / "scripts" / "coniso.py"), "init", "--test", f'"{PY}" -c "print(1)"')
        self.assertEqual((self.root / ".coniso" / "VERSION").read_text(encoding="utf-8").strip(), "1.2.0")
        self.c("permissions", "--decline")
        self.c("schedule", "--phase", "P1")

    def test_init_keeps_the_sprint_and_your_files_and_says_what_it_replaced(self):
        L = self.root / ".coniso"
        self.build("P1-01", fname="greet.txt", body="hello\n")
        self.c("review", "P1-01")
        self.report("P1-01.R1.md", "VERDICT P1-01 · P1-01.R1 · lens correctness+spec: pass\n"
                                   "A1 met — greet.txt:1\nA2 met — log\nTESTS: pass · log\nFIX LIST: none\n")
        (L / "checkslot.py").write_text("# the project's own tool\n", encoding="utf-8")
        (L / "notes-house-1.md").write_text("- a house rule\n", encoding="utf-8")
        with (L / "coniso.py").open("a", encoding="utf-8") as fh:
            fh.write("\n# a local patch\n")
        before = json.loads((L / "sprint.json").read_text(encoding="utf-8"))
        files_before = {x.relative_to(L).as_posix(): x.read_bytes() for d in ("tickets", "assignments", "reports")
                        for x in (L / d).rglob("*") if x.is_file()}
        out = self.sh(PY, str(SCRIPT), "init")
        self.assertIn("CONISO " + MOD.VERSION + " ready", out)
        self.assertIn("(was 1.2.0)", out)
        self.assertIn("Replaced: ", out)
        self.assertIn("coniso.py 1.2.0 -> " + MOD.VERSION + " (the replaced copy is kept at", out)
        self.assertIn("sprint " + before["name"], out)
        self.assertIn("checkslot.py", out)
        self.assertIn(".coniso/local/ is the folder for your own scripts", out)
        backups = list((L / "backup").glob("coniso-1.2.0-*.py"))
        self.assertEqual(len(backups), 1)
        self.assertIn("# a local patch", backups[0].read_text(encoding="utf-8"))
        self.assertEqual(json.loads((L / "sprint.json").read_text(encoding="utf-8")), before)
        for rel, data in files_before.items():
            self.assertEqual((L / rel).read_bytes(), data, rel)
        self.assertEqual((L / "checkslot.py").read_text(encoding="utf-8"), "# the project's own tool\n")
        self.assertTrue((L / "local" / "README.md").exists())
        self.assertTrue((L / "ref" / "contract.md").exists())
        # the open sprint goes on under the new script
        out = self.c("verdict", "P1-01", "--report", str(L / "reports" / "P1-01.R1.md"))
        self.assertIn("RULED pass P1-01", out)
        self.assertIn("MERGED P1-01", self.c("land", "P1-01"))
        self.assertIn("P1-02", self.c("status", "--open"))


class LocalFolder(Repo):
    def test_init_and_close_never_touch_local(self):
        tool = self.root / ".coniso" / "local" / "tool.py"
        tool.write_text("print('mine')\n", encoding="utf-8")
        self.sh(PY, str(SCRIPT), "init")
        self.c("close", "--force")
        self.assertEqual(tool.read_text(encoding="utf-8"), "print('mine')\n")
        self.assertTrue((self.root / ".coniso" / "local" / "README.md").exists())


def contract_rows(section):
    text = (PLUGIN / "skills" / "coniso" / "references" / "contract.md").read_text(encoding="utf-8")
    body = text.split(f"## {section}\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for ln in body.splitlines():
        if ln.startswith("| ") and not ln.startswith("|---") and not ln.startswith("| Marker") \
                and not ln.startswith("| Code") and not ln.startswith("| Path") and not ln.startswith("| Command"):
            rows.append([c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", ln)[1:-1]])
    return rows


class Contract(Repo):
    tickets = (ticket("P1-01", accept=("a", "b")), ticket("P1-02", deps=["P1-01"]))

    def test_every_marker_exit_code_and_file_in_the_contract_is_in_the_script(self):
        src = SCRIPT.read_text(encoding="utf-8")
        markers = contract_rows("Markers")
        self.assertGreater(len(markers), 25)
        for marker, rx, *_ in markers:
            re.compile(rx.strip("`"))
            word = marker.strip("`").split(" <")[0].split(" /")[0].split(" ")[0]
            self.assertIn(word, src, marker)
        for code, *_ in contract_rows("Exit codes"):
            n = int(code)
            if n >= 3:
                self.assertRegex(src, rf"sys\.exit\({n}\)|code={n}\b", f"exit code {n}")
        for path, *_ in contract_rows("Files"):
            name = path.strip("`").split(",")[0].replace(".coniso/", "").split("/")[0].split("<")[0]
            if name:
                self.assertIn(name, src, path)

    def test_markers_printed_during_a_sprint_match_the_contract(self):
        regexes = {m.strip("`"): re.compile(rx.strip("`")) for m, rx, *_ in contract_rows("Markers")}
        seen = []
        seen += self.c("dispatch", "P1-01").splitlines()
        (self.wt("P1-01") / "greet.txt").write_text("hello\n", encoding="utf-8")
        seen += self.c("test", "P1-01").splitlines()
        self.c("set", "P1-01", "built")
        seen += self.c("review", "P1-01").splitlines()
        rep = self.verdict_report("P1-01", "P1-01.R1", "pass", ["A1 met — greet.txt:1", "A2 met — log"])
        seen += self.c("verdict", "P1-01", "--report", str(rep)).splitlines()
        seen += self.c("land", "P1-01").splitlines()
        seen += self.c("verdict", "P1-02", "--report", "no-such-report.md", code=1).splitlines()   # an error line
        matched = {name for name, rx in regexes.items() for ln in seen if rx.search(ln)}
        for need in ("SPAWN", "TEST <id>: PASS", "REVIEWING", "RULED", "MERGED", "WAVE", "Now ready", "CONISO ERROR"):
            self.assertIn(need, matched, f"{need} not printed as the contract says:\n" + "\n".join(seen))


class MemorySources(Repo):
    tickets = (dict(ticket("P1-01"), prompt="Follow decision 0001 for every timestamp."), ticket("P1-02"))

    def setUp(self):
        super().setUp()
        d = self.root / "docs" / "decisions"
        d.mkdir(parents=True)
        (d / "README.md").write_text("# Decisions\n", encoding="utf-8")
        (d / "0001-use-utc.md").write_text("# 0001 Use UTC\n\n- **Status:** Settled on 1 Nov.\n\nAll stored times are UTC.\n", encoding="utf-8")
        (d / "0002-local-time.md").write_text("# 0002 Local time\n\n- **Status:** Superseded by decision 0001.\n\nTimes are local.\n", encoding="utf-8")
        (d / "0003-p1-01-rule.md").write_text("# 0003 Names are ASCII\n\n- **Status:** Settled.\n- **Binds:** P1-01\n", encoding="utf-8")
        (d / "0004-other.md").write_text("# 0004 Unrelated\n\n- **Status:** Settled.\n", encoding="utf-8")
        (self.root / ".coniso" / "local" / "notes-house-1.md").write_text("- Never run git.\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "decisions")
        self.c("init", "--decisions-dir", "docs/decisions", "--house-rules", ".coniso/local/notes-house-*.md",
               "--lessons-dir", "docs/rules")

    def test_decision_records_are_searched_and_superseded_ones_hidden(self):
        out = self.c("decisions", "--grep", "utc")
        self.assertIn("[0001] Use UTC · Settled on 1 Nov.  (docs/decisions/0001-use-utc.md)", out)
        self.assertIn("All stored times are UTC.", out)
        self.assertNotIn("0002", self.c("decisions"))
        self.assertIn("[0002] Local time", self.c("decisions", "--all"))

    def test_assignments_get_the_decisions_a_ticket_cites_or_that_bind_it(self):
        self.c("dispatch", "P1-01")
        a = (self.root / ".coniso" / "assignments" / "P1-01.E1.md").read_text(encoding="utf-8")
        line = next(x for x in a.splitlines() if x.startswith("DECISIONS:"))
        self.assertIn("[0001] Use UTC", line)
        self.assertIn("[0003] Names are ASCII", line)
        self.assertNotIn("0002", line)
        self.assertNotIn("0004", line)

    def test_house_rules_reach_engineers_and_reviewers(self):
        rules = (self.root / ".coniso" / "local" / "notes-house-1.md").resolve().as_posix()
        self.c("dispatch", "P1-01")
        a = (self.root / ".coniso" / "assignments" / "P1-01.E1.md").read_text(encoding="utf-8")
        self.assertIn(rules, next(x for x in a.splitlines() if x.startswith("READ FIRST:")))
        (self.wt("P1-01") / "x.txt").write_text("x\n", encoding="utf-8")
        self.c("set", "P1-01", "built")
        self.c("review", "P1-01")
        r = (self.root / ".coniso" / "assignments" / "P1-01.R1.md").read_text(encoding="utf-8")
        self.assertIn(rules, next(x for x in r.splitlines() if x.startswith("READ FIRST:")))
        out = self.c("memory")
        self.assertIn("House rules: 1 file(s)", out)
        self.assertIn("Decision records: 4 in docs/decisions (1 superseded)", out)

    def test_lessons_go_to_the_configured_folder(self):
        self.c("lesson", "ui", "Check contrast in dark mode.", "--source", "P1-01")
        self.assertIn("Check contrast in dark mode.", (self.root / "docs" / "rules" / "ui.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
