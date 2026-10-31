"""Tests for scripts/coniso.py and scripts/coniso_hook.py. Standard library only.

Run from the repo root:  python -m unittest discover -s tests -v
Every test builds a throwaway git repo in a temp folder and drives the script as agents would.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN / "scripts" / "coniso.py"
HOOK = PLUGIN / "scripts" / "coniso_hook.py"
PY = sys.executable


def load_module():
    spec = importlib.util.spec_from_file_location("coniso_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = load_module()


def ticket(tid, deps=(), size="S", model="sonnet", accept=("it works",), review=False, effort="medium"):
    return {"id": tid, "phase": "P1", "title": f"Ticket {tid}", "size": size, "deps": list(deps), "why": "w",
            "what": ["x"], "accept": list(accept), "prompt": "do it", "model": model,
            "effort": None if model == "haiku" else effort, "review": review, "modelWhy": "because"}


def seed(*tickets):
    return {"meta": {"preamble": "Keep it simple."},
            "phases": [{"id": "P1", "name": "Core", "short": "core", "weeks": "1", "why": "w", "gate": "none"}],
            "tickets": list(tickets)}


class Repo(unittest.TestCase):
    """A temp git repo with a roadmap, Coniso initialised from the plugin copy."""

    tickets = (ticket("P1-01", accept=("greet.txt says hello", "test passes")), ticket("P1-02", deps=["P1-01"]))
    schedule = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="coniso-test-"))
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "core.autocrlf", "false")
        (self.root / "README.md").write_text("demo\n", encoding="utf-8")
        seed_file = self.tmp / "seed.json"
        seed_file.write_text(json.dumps(seed(*self.tickets)), encoding="utf-8")
        self.sh(PY, str(SCRIPT), "roadmap", "new", "--title", "Demo", "--from", str(seed_file))
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.sh(PY, str(SCRIPT), "init", "--test", f'"{PY}" -c "print(1)"')
        self.c("permissions", "--decline")
        if self.schedule:
            self.c("schedule", "--phase", "P1")

    def tearDown(self):
        def writable(func, path, *_):     # git marks object files read-only, which Windows refuses to delete
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        shutil.rmtree(self.tmp, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))

    # helpers
    def sh(self, *args, code=0, stdin=None, cwd=None):
        r = subprocess.run(list(args), cwd=str(cwd or self.root), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", input=stdin)
        if code is not None:
            self.assertEqual(r.returncode, code, f"{args}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
        return r.stdout

    def git(self, *args):
        return self.sh("git", *args)

    def c(self, *args, code=0):
        return self.sh(PY, str(self.root / ".coniso" / "coniso.py"), *args, code=code)

    def sprint(self):
        return json.loads((self.root / ".coniso" / "sprint.json").read_text(encoding="utf-8"))

    def wt(self, tid):
        return Path(self.c("wt", "path", tid).strip())

    def report(self, name, text):
        f = self.root / ".coniso" / "reports" / name
        f.write_text(text, encoding="utf-8")
        return f

    def build(self, tid, fname="out.txt", body="hello\n"):
        self.c("dispatch", tid)
        (self.wt(tid) / fname).write_text(body, encoding="utf-8")
        self.c("set", tid, "built", "--note", "ok")

    def verdict_report(self, tid, unit, stated, lines, fix="none"):
        return self.report(f"{unit}.md", f"VERDICT {tid} · {unit} · lens correctness+spec: {stated}\n" + "\n".join(lines)
                           + f"\nTESTS: pass · log\nFIX LIST: {fix}\n")


class TokensAndStatus(Repo):
    def test_set_tokens_without_state_and_retro_totals(self):
        self.c("set", "P1-01", "building", "--tokens", "1200", "--role", "engineer")
        out = self.c("set", "P1-01", "--tokens", "300", "--role", "reviewer")
        self.assertIn("300 tokens", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "building")
        self.c("set", "P1-01", code=1)               # neither a state nor something to record
        out = self.c("retro")
        self.assertIn("Tokens (reported): 1,500 total", out)
        self.assertIn("engineer 1,200", out)
        self.assertIn("reviewer 300", out)

    def test_status_field_and_compact_json(self):
        self.assertEqual(self.c("status", "P1-01", "--field", "review_plan.kind").strip(), "single")
        self.assertEqual(self.c("status", "P1-01", "--field", "state").strip(), "queued")
        self.c("status", "P1-01", "--field", "nope", code=1)
        out = self.c("status", "P1-01", "--json")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out)["title"], "Ticket P1-01")

    def test_retro_lessons_prints_only_last_lessons(self):
        (self.root / ".coniso" / "RETRO.md").write_text(
            "## s1 — 2026-01-01\nOutcome: x\nLessons:\n- old lesson\n\n## s2 — 2026-02-01\nOutcome: y\nLessons:\n"
            "- (Director: brief gaps)\n- new lesson A\n- new lesson B\n", encoding="utf-8")
        out = self.c("retro", "--lessons")
        self.assertEqual(out.strip().splitlines(), ["- new lesson A", "- new lesson B"])

    def test_retro_flags_long_feedback_files(self):
        fb = self.root / "docs" / "agent-feedback"
        fb.mkdir(parents=True)
        (fb / "ui.md").write_text("\n".join(f"- lesson {i}" for i in range(31)), encoding="utf-8")
        (fb / "net.md").write_text("- short\n", encoding="utf-8")
        out = self.c("retro")
        self.assertIn("ui.md (31)", out)
        self.assertNotIn("net.md", out)


class Dispatch(Repo):
    tickets = (ticket("P1-01", accept=("a", "b", "c")), ticket("P1-02", deps=["P1-01"]), ticket("P1-03"),
               ticket("P1-04", size="L", model="opus", effort="high"))

    def test_solo_dispatch_writes_worktree_assignment_and_spawn_line(self):
        out = self.c("dispatch", "P1-01", "--specialty", "ui")
        self.assertIn("SPAWN coniso:engineer · model sonnet · prompt: Assignment: ", out)
        a = (self.root / ".coniso" / "assignments" / "P1-01.E1.md").read_text(encoding="utf-8")
        for field in ("READ FIRST:", "WORKDIR:", "OBJECTIVE:", "OWNS:", "INPUTS:", "BOUNDARIES:", "DONE WHEN:", "ON DONE:", "RETURN:"):
            self.assertIn(field, a)
        self.assertIn("acceptance items 1-3", a)
        self.assertIn("specialties/ui.md", a)
        self.assertIn("GENERATED: coniso", a)
        wd = MOD.read_fields(self.root / ".coniso" / "assignments" / "P1-01.E1.md")["WORKDIR"]
        self.assertTrue(MOD.is_abs_path(wd) and Path(wd).is_dir())
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "building")

    def test_existing_assignment_is_kept_unless_rewrite(self):
        self.c("dispatch", "P1-01")
        f = self.root / ".coniso" / "assignments" / "P1-01.E1.md"
        f.write_text(f.read_text(encoding="utf-8") + "TIGHTENED\n", encoding="utf-8")
        self.assertIn("kept existing", self.c("dispatch", "P1-01"))
        self.assertIn("TIGHTENED", f.read_text(encoding="utf-8"))
        self.c("dispatch", "P1-01", "--rewrite")
        self.assertNotIn("TIGHTENED", f.read_text(encoding="utf-8"))

    def test_refuses_unmerged_dependencies(self):
        out = self.c("dispatch", "P1-02", code=1)
        self.assertIn("waits on P1-01", out)

    def test_team_dispatch_writes_lead_assignment(self):
        out = self.c("dispatch", "P1-04", "--review", "lead")
        self.assertIn("SPAWN coniso:lead · model opus", out)
        f = MOD.read_fields(self.root / ".coniso" / "assignments" / "P1-04.md")
        self.assertTrue(MOD.is_abs_path(f["WORKDIR"]))
        a = (self.root / ".coniso" / "assignments" / "P1-04.md").read_text(encoding="utf-8")
        self.assertIn("REVIEW: review lead serves", a)
        self.assertIn("validate-handoff P1-04", a)
        self.assertEqual(self.sprint()["tickets"]["P1-04"]["wt"], None)   # the lead makes its own worktree

    def test_wave_table_validates_and_matches_schedule(self):
        out = self.c("dispatch", "P1-01", "P1-03", "P1-04", "--review", "lead")
        self.assertIn("PLAN OK director-w1.md", out)
        self.assertIn("MATCHES SCHEDULE", out)
        self.assertEqual(out.count("SPAWN "), 3)
        out = self.c("validate-plan", ".coniso/assignments/director-w1.md")
        self.assertIn("MATCHES SCHEDULE", out)

    def test_changed_model_deviates_from_schedule(self):
        self.c("dispatch", "P1-01", "P1-03")
        plan = self.root / ".coniso" / "assignments" / "director-w1.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("| engineer/builder | sonnet |", "| engineer/builder | opus |", 1),
                        encoding="utf-8")
        out = self.c("validate-plan", str(plan))
        self.assertIn("PLAN OK", out)
        self.assertNotIn("MATCHES SCHEDULE", out)

    def test_hand_written_assignment_never_matches(self):
        self.c("dispatch", "P1-01", "P1-03")
        f = self.root / ".coniso" / "assignments" / "P1-03.E1.md"
        f.write_text(f.read_text(encoding="utf-8").replace("GENERATED: coniso", ""), encoding="utf-8")
        self.assertNotIn("MATCHES SCHEDULE", self.c("validate-plan", ".coniso/assignments/director-w1.md"))

    def test_lead_packet_plan_never_matches(self):
        a = self.root / ".coniso" / "assignments"
        wd = self.root.as_posix()
        for u in ("P1-04.E1", "P1-04.E2"):
            (a / f"{u}.md").write_text(f"WORKDIR: {wd}\nOBJECTIVE: x\nOWNS: src/{u}/**\nDONE WHEN: y\nRETURN: z\nGENERATED: coniso\n",
                                      encoding="utf-8")
        (a / "P1-04-plan.md").write_text(
            "| Unit | Role | Model | Step | Owns | Needs | Done |\n|---|---|---|---|---|---|---|\n"
            "| P1-04.E1 | engineer/builder | sonnet | 1 | src/P1-04.E1/** | - | A1 |\n"
            "| P1-04.E2 | engineer/builder | sonnet | 1 | src/P1-04.E2/** | - | A2 |\nSlots: 2/4\n", encoding="utf-8")
        out = self.c("validate-plan", ".coniso/assignments/P1-04-plan.md")
        self.assertIn("PLAN OK", out)
        self.assertNotIn("MATCHES SCHEDULE", out)


class VerdictParsing(unittest.TestCase):
    def vs(self, *reports):
        out = {}
        for i, (stated, body) in enumerate(reports, 1):
            b = MOD.parse_verdicts(f"VERDICT X · X.R{i} · lens c: {stated}\n{body}")["X"]
            out[f"X.R{i}"] = {"stated": b["stated"], "void": "", "blocker": b["blocker"], "major": b["major"],
                             "minor": b["minor"], "unmet": len(b["unmet"]), "met": b["met"], "no_evidence": b["no_evidence"]}
        return out

    def test_parses_ranges_lists_and_severities(self):
        b = MOD.parse_verdicts("noise\nVERDICT P2-01 · P2-01.R1 · lens spec: fail\nA1-A3 met — t.py:4\nA4, A5 unmet — log 12\n"
                               "FINDINGS: [blocker] crashes — t.py:9\n- [major] slow — bench\n- [minor] naming — x.py:1\n"
                               "- [blocker] suspicion: maybe racy\nTESTS: pass\nFIX LIST: .coniso/reports/P2-01.fix.md\n")["P2-01"]
        self.assertEqual(b["met"], {1, 2, 3})
        self.assertEqual(b["unmet"], {4, 5})
        self.assertEqual((b["blocker"], b["major"], b["minor"]), (1, 1, 1))   # the suspicion does not count
        self.assertEqual(b["fix"], ".coniso/reports/P2-01.fix.md")
        self.assertEqual(b["unit"], "P2-01.R1")

    def test_missing_evidence_is_recorded(self):
        b = MOD.parse_verdicts("VERDICT X · X.R1 · lens c: pass\nA1 met —\nA2 met — none\nA3 met — f.py:1\n")["X"]
        self.assertEqual(b["no_evidence"], {1, 2})

    def test_rules(self):
        rule = MOD.rule_round
        self.assertEqual(rule(self.vs(("pass", "A1 met — t"))), ("pass", None))
        self.assertEqual(rule(self.vs(("pass-with-fixes", "A1 met — t\nFINDINGS: [minor] n — x")))[0], "pass")
        self.assertEqual(rule(self.vs(("fail", "A1 met — t\nFINDINGS: [blocker] b — x")))[0], "fail")
        self.assertEqual(rule(self.vs(("fail", "A1 unmet — t")))[0], "fail")
        r, why = rule(self.vs(("fail", "A1 met — t\nFINDINGS: [major] m — x")))
        self.assertIsNone(r)
        self.assertIn("major", why)
        r, why = rule(self.vs(("fail", "A1 met — t\nFINDINGS: [minor] m — x")))   # reviewer departs from the rules
        self.assertIsNone(r)
        self.assertIn("reviewer said fail", why)
        r, why = rule(self.vs(("pass", "A1 met — t\nFINDINGS: [blocker] b — x")))
        self.assertIsNone(r)
        r, why = rule(self.vs(("pass", "A1 met — t"), ("fail", "A1 met — t\nFINDINGS: [blocker] b — x")))
        self.assertIsNone(r)
        self.assertIn("panel split", why)
        vs = self.vs(("pass", "A1 met — t"))
        vs["X.R1"]["void"] = "no evidence for A1"
        r, why = rule(vs)
        self.assertIsNone(r)
        self.assertIn("void", why)


class ReviewAndVerdict(Repo):
    tickets = (ticket("P1-01", accept=("a", "b")), ticket("P1-05", review=True),
               ticket("P1-06", model="haiku"), ticket("P1-07", model="haiku"))

    def test_review_writes_reviewer_assignment_and_sets_reviewing(self):
        self.build("P1-01")
        out = self.c("review", "P1-01", "--tokens", "4000")
        self.assertIn("REVIEWING P1-01", out)
        self.assertIn("SPAWN coniso:reviewer · model sonnet", out)
        a = (self.root / ".coniso" / "assignments" / "P1-01.R1.md").read_text(encoding="utf-8")
        self.assertIn("SCOPE: full", a)
        self.assertIn("verdict P1-01 --report", a)
        t = self.sprint()["tickets"]["P1-01"]
        self.assertEqual(t["state"], "reviewing")
        self.assertEqual(t["review_round"]["units"], ["P1-01.R1"])
        self.assertEqual(sum(h.get("tokens", 0) for h in t["history"]), 4000)

    def test_review_refuses_unbuilt(self):
        self.c("dispatch", "P1-01")
        self.c("review", "P1-01", code=1)

    def test_verdict_pass_sets_passed(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.verdict_report("P1-01", "P1-01.R1", "pass-with-fixes", ["A1-A2 met — out.txt:1", "FINDINGS: [minor] nit — out.txt:1"])
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("RULED pass P1-01", out)
        self.assertNotIn("audit required", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "passed")

    def test_void_verdict_needs_ruling_and_keeps_state(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.verdict_report("P1-01", "P1-01.R1", "pass", ["A1 met — out.txt:1"])   # A2 missing
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("NEEDS RULING P1-01: void verdict", out)
        self.assertIn("A2", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "reviewing")

    def test_fail_needs_fix_list_then_rules_fail_with_audit(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        rep = self.verdict_report("P1-01", "P1-01.R1", "fail", ["A1 met — out.txt:1", "A2 unmet — out.txt:1 empty"],
                                  fix=".coniso/reports/P1-01.fix.md")
        self.assertIn("without a fix list", self.c("verdict", "P1-01", "--report", str(rep)))
        self.report("P1-01.fix.md", "1. out.txt:1 — wrong — check: says bye\n")
        out = self.c("verdict", "P1-01", "--report", str(rep))
        self.assertIn("RULED fail P1-01", out)
        self.assertIn("verdict audit required", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "failed")

    def test_re_review_after_fix_round_is_delta(self):
        self.build("P1-01")
        self.c("review", "P1-01")
        self.report("P1-01.fix.md", "1. fix it\n")
        rep = self.verdict_report("P1-01", "P1-01.R1", "fail", ["A1-A2 unmet — out.txt:1"], fix=".coniso/reports/P1-01.fix.md")
        self.c("verdict", "P1-01", "--report", str(rep))
        self.c("set", "P1-01", "building", "--note", "fix round 1")
        self.c("set", "P1-01", "built")
        out = self.c("review", "P1-01")
        self.assertIn("P1-01.R2.md", out)
        a = (self.root / ".coniso" / "assignments" / "P1-01.R2.md").read_text(encoding="utf-8")
        self.assertIn("SCOPE: delta", a)
        self.assertIn("P1-01.fix.md", a)
        self.assertIn("earlier review", a)
        self.assertNotIn("ruling.md", a)

    def test_panel_waits_for_both_reviewers(self):
        self.build("P1-05")
        out = self.c("review", "P1-05")
        self.assertIn("PLAN OK P1-05-review.md", out)
        self.assertIn("MATCHES SCHEDULE", out)
        self.assertIn("model opus", out)
        r1 = self.verdict_report("P1-05", "P1-05.R1", "pass", ["A1 met — out.txt:1"])
        self.assertIn("RECORDED P1-05 P1-05.R1 pass; waiting on P1-05.R2", self.c("verdict", "P1-05", "--report", str(r1)))
        r2 = self.verdict_report("P1-05", "P1-05.R2", "pass", ["A1 met — out.txt:1"])
        out = self.c("verdict", "P1-05", "--report", str(r2))
        self.assertIn("RULED pass P1-05", out)
        self.assertIn("verdict audit required", out)        # review: true tickets are always audited

    def test_panel_upgrade_deviates_from_schedule(self):
        self.build("P1-01")
        out = self.c("review", "P1-01", "--panel", "P1-01")
        self.assertIn("deviates from the schedule", out)
        self.assertEqual(out.count("SPAWN "), 2)

    def test_haiku_batch_shares_one_reviewer(self):
        self.build("P1-06", "a.txt")
        self.build("P1-07", "b.txt")
        out = self.c("review", "P1-06", "P1-07")
        self.assertEqual(out.count("SPAWN "), 1)
        a = (self.root / ".coniso" / "assignments" / "P1-06.R1.md").read_text(encoding="utf-8")
        self.assertIn("verdict P1-06,P1-07 --report", a)
        rep = self.report("P1-06.R1.md", "VERDICT P1-06 · P1-06.R1 · lens spec: pass\nA1 met — a.txt:1\nFIX LIST: none\n"
                                         "VERDICT P1-07 · P1-06.R1 · lens spec: pass\nA1 met — b.txt:1\nFIX LIST: none\n")
        out = self.c("verdict", "P1-06,P1-07", "--report", str(rep))
        self.assertIn("RULED pass P1-06", out)
        self.assertIn("RULED pass P1-07", out)

    def test_review_stops_on_sync_conflict(self):
        self.build("P1-01", "README.md", "ticket version\n")
        (self.root / "README.md").write_text("main version\n", encoding="utf-8")
        self.git("commit", "-qam", "main moves on")
        out = self.c("review", "P1-01", code=3)
        self.assertIn("CONFLICTS", out)
        self.assertIn("Review not dispatched", out)
        self.assertEqual(self.sprint()["tickets"]["P1-01"]["state"], "built")


class Handoff(Repo):
    tickets = (ticket("P1-04", size="L", accept=("a", "b")),)

    def setUp(self):
        super().setUp()
        self.c("wt", "add", "P1-04")
        self.w = self.wt("P1-04")
        (self.w / "feature.py").write_text("x = 1\n", encoding="utf-8")
        self.c("test", "P1-04")

    def claim(self, extra=""):
        self.report("P1-04.md", "Files: feature.py\nA1 met — test_feature.py::test_a\nA2 met — feature.py:1\n"
                                "Test log: .coniso/reports/P1-04.test.log\n" + extra)

    def test_ok(self):
        self.claim()
        self.assertIn("HANDOFF OK P1-04", self.c("validate-handoff", "P1-04"))

    def test_missing_evidence_fails(self):
        self.report("P1-04.md", "A1 met — t\nA2 met —\nlog .coniso/reports/P1-04.test.log\n")
        out = self.c("validate-handoff", "P1-04", code=1)
        self.assertIn("H1", out)
        self.assertIn("no evidence for A2", out)

    def test_missing_item_fails(self):
        self.report("P1-04.md", "A1 met — t\nlog .coniso/reports/P1-04.test.log\n")
        self.assertIn("A2", self.c("validate-handoff", "P1-04", code=1))

    def test_missing_log_fails(self):
        (self.root / ".coniso" / "reports" / "P1-04.test.log").unlink()
        self.report("P1-04.md", "A1-A2 met — t.py:1\n")
        self.assertIn("H2", self.c("validate-handoff", "P1-04", code=1))

    def test_failing_log_needs_an_explanation(self):
        (self.root / ".coniso" / "reports" / "P1-04.test.log").write_text("$ t\nboom\n[exit 1]\n", encoding="utf-8")
        self.claim()
        self.assertIn("H2", self.c("validate-handoff", "P1-04", code=1))

    def test_unlisted_marker_fails_listed_passes(self):
        (self.w / "feature.py").write_text("x = 1  # TODO handle zero\n", encoding="utf-8")
        self.claim()
        out = self.c("validate-handoff", "P1-04", code=1)
        self.assertIn("H3", out)
        self.assertIn("feature.py", out)
        self.claim("Left: TODO in feature.py (zero case, follow-up)\n")
        self.assertIn("HANDOFF OK", self.c("validate-handoff", "P1-04"))


class BriefTrim(Repo):
    def brief(self):
        return self.root / ".coniso" / "BRIEF.md"

    def test_moves_old_rows_and_leaves_pointer(self):
        b = self.brief()
        b.write_text(b.read_text(encoding="utf-8").replace(
            "|-------|----------|-------|\n",
            "|-------|----------|-------|\n| 2020-01-02 | old rule | all |\n| 2999-01-01 | new rule | all |\n| undated | keep | x |\n", 1),
            encoding="utf-8")
        out = self.c("brief-trim")
        self.assertIn("moved 1 row(s)", out)
        text = b.read_text(encoding="utf-8")
        self.assertNotIn("old rule", text)
        self.assertIn("new rule", text)
        self.assertIn("undated", text)
        self.assertIn("Older decisions (still binding): .coniso/maps/decisions.md", text)
        archive = (self.root / ".coniso" / "maps" / "decisions.md").read_text(encoding="utf-8")
        self.assertIn("| 2020-01-02 | old rule | all |", archive)
        self.assertIn("no Decisions log rows", self.c("brief-trim"))          # idempotent
        self.assertEqual(b.read_text(encoding="utf-8").count("Older decisions"), 1)

    def test_before_flag(self):
        b = self.brief()
        b.write_text(b.read_text(encoding="utf-8").replace(
            "|-------|----------|-------|\n", "|-------|----------|-------|\n| 2025-06-01 | mid | all |\n", 1), encoding="utf-8")
        self.assertIn("no Decisions log rows", self.c("brief-trim", "--before", "2025-01-01"))
        self.assertIn("moved 1", self.c("brief-trim", "--before", "2025-07-01"))

    def test_preflight_warns_on_long_core(self):
        self.git("add", "-A")                      # roadmap/init leave nothing dirty in tracked files; keep it clean
        out = self.c("preflight", code=None)
        self.assertNotIn("brief core", out)
        b = self.brief()
        text = b.read_text(encoding="utf-8")
        b.write_text(text.replace("## Track notes", "\n".join(f"- filler {i}" for i in range(150)) + "\n## Track notes"),
                     encoding="utf-8")
        out = self.c("preflight", code=None)
        self.assertIn("brief core is", out)


class Hook(Repo):
    schedule = False

    def hook(self, cwd, stdin):
        return subprocess.run([PY, str(HOOK)], cwd=str(cwd), input=stdin, capture_output=True, text=True, encoding="utf-8")

    def test_noop_without_sprint(self):
        for stdin in ("", "not json", json.dumps({"cwd": str(self.root)})):
            r = self.hook(self.root, stdin)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self.hook(self.tmp, json.dumps({"cwd": str(self.tmp)}))      # not even a repo
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_prints_board_and_wave_with_sprint(self):
        self.c("schedule", "--phase", "P1")
        self.c("dispatch", "P1-01")
        w = self.wt("P1-01")
        r = self.hook(self.tmp, json.dumps({"cwd": str(w)}))            # from inside a worktree
        self.assertEqual(r.returncode, 0)
        self.assertIn("current wave W1", r.stdout)
        self.assertIn("P1-01", r.stdout)
        self.assertIn("building", r.stdout)

    def test_hooks_json_shape(self):
        data = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entry = data["hooks"]["SessionStart"][0]
        self.assertEqual(entry["matcher"], "compact")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/coniso_hook.py", entry["hooks"][0]["command"])


class EndToEnd(Repo):
    """Dry-run sprint: two tickets through dispatch, review, verdict, land and retro, with stubbed agents."""

    def test_two_ticket_sprint(self):
        out = self.c("dispatch", "P1-01")                                 # Director
        self.assertIn("SPAWN coniso:engineer", out)
        (self.wt("P1-01") / "greet.txt").write_text("hello\n", encoding="utf-8")   # stub engineer
        self.c("test", "P1-01")
        self.c("set", "P1-01", "built", "--note", "greet added")         # its ON DONE line
        self.c("review", "P1-01", "--tokens", "41000")                   # Director
        rep = self.verdict_report("P1-01", "P1-01.R1", "pass", ["A1 met — greet.txt:1", "A2 met — P1-01.test.log"])
        self.assertIn("RULED pass", self.c("verdict", "P1-01", "--report", str(rep)))   # stub reviewer
        out = self.c("land", "P1-01", "--tokens", "22000")               # Director
        self.assertIn("MERGED P1-01", out)
        self.assertIn("Now ready: P1-02", out)
        self.assertIn("ROADMAP rev 2", out)
        self.assertIn("WAVE 1 landed", out)

        self.c("dispatch", "P1-02")
        (self.wt("P1-02") / "bye.txt").write_text("bye\n", encoding="utf-8")
        self.c("set", "P1-02", "built")
        self.c("review", "P1-02")
        rep = self.verdict_report("P1-02", "P1-02.R1", "pass", ["A1 met — bye.txt:1"])
        self.c("verdict", "P1-02", "--report", str(rep))
        out = self.c("land", "P1-02")
        self.assertIn("MERGED P1-02", out)
        self.assertIn("2 wave(s) merged", out)

        s = self.sprint()
        self.assertEqual({t["state"] for t in s["tickets"].values()}, {"merged"})
        self.assertEqual(s["waves_landed"], [1, 2])
        for f in ("greet.txt", "bye.txt"):
            self.assertTrue((self.root / f).exists())
        rm = (self.root / "docs" / "roadmap" / "Demo-Roadmap.html").read_text(encoding="utf-8")
        _, _, data = MOD.load_roadmap(self.root / "docs" / "roadmap" / "Demo-Roadmap.html")
        self.assertEqual([t["status"] for t in data["tickets"]], ["review", "review"])
        self.assertIn("Claude: Coniso sprint", rm)
        log = self.git("log", "--format=%s")
        self.assertIn("P1-02: Ticket P1-02", log)
        self.assertIn("coniso: roadmap sync", log)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        out = self.c("retro")
        self.assertIn("Tokens (reported): 63,000 total", out)
        self.assertIn("engineer 41,000", out)
        self.assertIn("reviewer 22,000", out)


class RestartPoint(Repo):
    tickets = (ticket("P1-01"), ticket("P1-02", deps=["P1-01"]), ticket("P1-03", deps=["P1-02"]))

    def test_restart_point_after_three_waves(self):
        for i, tid in enumerate(("P1-01", "P1-02", "P1-03"), 1):
            self.build(tid, f"f{i}.txt")
            self.c("review", tid)
            rep = self.verdict_report(tid, f"{tid}.R1", "pass", ["A1 met — ok"])
            self.c("verdict", tid, "--report", str(rep))
            out = self.c("land", tid)
            self.assertEqual("RESTART POINT" in out, i == 3, out)


if __name__ == "__main__":
    unittest.main()
