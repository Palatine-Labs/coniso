"""Tests for the Rust stack pack (stacks/rust) and the script changes it needs: pack defaults for checks, `sweep`.

Run from the repo root:  python -m unittest discover -s tests -v
The cargo tests are skipped where cargo is not installed.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_coniso import PLUGIN, PY, SCRIPT, Repo, ticket

KIT = PLUGIN / "stacks" / "rust" / "rustkit.py"
spec = importlib.util.spec_from_file_location("rustkit_mod", KIT)
RK = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RK)
CARGO = shutil.which("cargo") is not None


class Hardware(unittest.TestCase):
    def test_a_modest_share_of_the_machine(self):
        self.assertEqual(RK.plan_hardware(4), {"usable": 3, "slots": 1, "jobs": 3})
        self.assertEqual(RK.plan_hardware(8), {"usable": 6, "slots": 1, "jobs": 6})
        self.assertEqual(RK.plan_hardware(16), {"usable": 12, "slots": 3, "jobs": 4})
        self.assertEqual(RK.plan_hardware(64), RK.plan_hardware(16))          # never more than max_cores
        for cores in (1, 2, 6, 12, 24, 96):
            hw = RK.plan_hardware(cores)
            self.assertLessEqual(hw["slots"] * hw["jobs"], max(2, hw["usable"]), cores)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rustkit-test-"))

    def tearDown(self):
        def writable(func, path, *_):
            try:
                os.chmod(path, 0o700)
                func(path)
            except OSError:
                pass
        shutil.rmtree(self.root, onerror=writable)

    def write(self, rel, text):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return f

    def git(self, *args):
        r = subprocess.run(["git"] + list(args), cwd=str(self.root), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def repo(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "core.autocrlf", "false")

    def kit(self, *args, code=0, env=None):
        r = subprocess.run([PY, str(KIT)] + list(args), cwd=str(self.root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, **(env or {})))
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r.stdout + r.stderr


class Gate(Tmp):
    def scan(self, text, rel="src/lib.rs"):
        self.write(rel, text)
        return RK.scan(self.root, {rel: list(range(1, len(text.splitlines()) + 1))})

    def test_debug_and_unfinished_macros_block(self):
        blocked, _ = self.scan("pub fn f() -> u8 {\n    dbg!(1);\n    todo!()\n}\n")
        self.assertEqual([x.split(" ")[1] for x in blocked], ["dbg:", "todo:"])

    def test_unsafe_needs_a_safety_comment(self):
        blocked, _ = self.scan("pub fn f(p: *const u8) -> u8 {\n    unsafe { *p }\n}\n")
        self.assertIn("unsafe:", blocked[0])
        blocked, _ = self.scan("pub fn f(p: *const u8) -> u8 {\n    // SAFETY: the caller passes a live pointer\n    unsafe { *p }\n}\n")
        self.assertEqual(blocked, [])

    def test_unwrap_is_a_hint_in_library_code_and_nothing_in_tests(self):
        text = ("pub fn f(s: &str) -> u8 {\n    s.parse().unwrap()\n}\n"
                "#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n        let x: u8 = \"1\".parse().unwrap();\n        todo!()\n    }\n}\n")
        blocked, hints = self.scan(text)
        self.assertEqual(blocked, [])                       # todo! inside the test module does not block
        self.assertEqual(len(hints), 1)
        self.assertIn("src/lib.rs:2 unwrap", hints[0])

    def test_strings_comments_and_the_allow_marker_are_not_findings(self):
        blocked, hints = self.scan('pub fn f() -> &\'static str {\n    // todo!() later\n    "dbg!(x) .unwrap()"\n}\n'
                                   "pub fn g() { dbg!(2); } // rustkit:allow\n")
        self.assertEqual((blocked, hints), ([], []))

    def test_prints_are_fine_in_a_binary_and_sleeps_are_hinted_in_tests(self):
        _, hints = self.scan('fn main() {\n    println!("hi");\n}\n', rel="src/main.rs")
        self.assertEqual(hints, [])
        _, hints = self.scan("#[test]\nfn t() {\n    std::thread::sleep(d);\n    assert!(true);\n}\n", rel="tests/api.rs")
        self.assertIn("sleep", hints[0])

    def test_the_command_reads_the_diff_and_fails_on_a_block(self):
        self.repo()
        self.write("src/lib.rs", "pub fn f() -> u8 {\n    1\n}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.write("src/lib.rs", "pub fn f() -> u8 {\n    1\n}\npub fn g(s: &str) -> u8 {\n    s.parse().unwrap()\n}\n")
        out = self.kit("gate", "--no-fmt")
        self.assertIn("RUST HINTS 1", out)
        self.assertIn("src/lib.rs:5 unwrap", out)
        self.assertIn("RUST GATE: PASS · 1 hint(s)", out)
        self.write("src/new.rs", "pub fn h() {\n    dbg!(3);\n}\n")              # an untracked file counts too
        out = self.kit("gate", "--no-fmt", code=1)
        self.assertIn("RUST GATE: BLOCKED 1", out)
        self.assertIn("src/new.rs:2 dbg", out)


TESTS_RS = '''
pub fn add(a: i32, b: i32) -> i32 { a + b }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds_small() {
        let got = add(1, 2);
        assert_eq!(got, 3, "one plus two");
    }

    #[test]
    fn adds_small_again() {
        let got = add(1, 2);
        assert_eq!(got, 3, "one plus two");   // the same test under another name
    }

    #[test]
    fn adds_big() {
        let got = add(100, 200);
        assert_eq!(got, 300, "bigger numbers");
    }

    #[test]
    fn adds_negative() {
        let got = add(0, 7);
        assert_eq!(got, 7, "zero on the left");
    }

    #[test]
    fn runs() {
        let s = "a brace } in a string and a char '{' must not end the body";
        let _ = add(s.len() as i32, 1);
    }

    #[test]
    #[ignore]
    fn later() {
        std::thread::sleep(std::time::Duration::from_millis(5));
        assert!(add(1, 1) == 2);
    }

    #[tokio::test]
    #[ignore = "needs the staging database"]
    async fn with_a_reason() {
        assert_eq!(add(2, 2), 4);
    }
}
'''


class TestAudit(Tmp):
    def test_finds_duplicates_tables_empty_ignored_and_sleeping_tests(self):
        self.write("src/lib.rs", TESTS_RS)
        self.write("target/debug/build/x.rs", "#[test]\nfn generated() {}\n")          # build output is skipped
        res = RK.audit(self.root)
        self.assertEqual(res["tests"], 7)
        self.assertEqual(len(res["duplicate"]), 1)
        self.assertIn("adds_small", res["duplicate"][0])
        self.assertIn("adds_small_again", res["duplicate"][0])
        self.assertEqual(len(res["table"]), 1)
        self.assertIn("4 tests differ only in their literals", res["table"][0])
        self.assertEqual([x.split()[-1] for x in res["empty"]], ["runs"])
        self.assertEqual([x.split()[-1] for x in res["ignored"]], ["later"])            # one with a reason is fine
        self.assertEqual([x.split()[-1] for x in res["sleep"]], ["later"])

    def test_a_test_that_asserts_through_a_helper_is_not_flagged(self):
        self.write("tests/api.rs", "fn golden(n: u8) {\n    compare(n);\n}\n\nfn compare(n: u8) {\n    assert_eq!(n, 1);\n}\n\n"
                   "fn setup() -> u8 {\n    1\n}\n\n#[test]\nfn through_two_helpers() {\n    golden(1);\n}\n\n"
                   "#[test]\nfn only_sets_up() {\n    let _ = setup();\n}\n")
        self.write("target/debug/big.rs", "#[test]\nfn generated() {}\n")
        res = RK.audit(self.root)
        self.assertEqual(res["tests"], 2)
        self.assertEqual([x.split()[-1] for x in res["empty"]], ["only_sets_up"])

    def test_the_slowest_tests_come_from_a_nextest_log(self):
        self.write("src/lib.rs", TESTS_RS)
        log = self.write("test.log", "        PASS [   0.004s] demo tests::adds_small\n"
                                     "        PASS [   3.000s] demo tests::crawls\n"
                                     "        SLOW [   1.000s] demo tests::walks\n")
        res = RK.audit(self.root, log)
        self.assertEqual(len(res["slow"]), 2)
        self.assertIn("3.00s (75% of test time) demo tests::crawls", res["slow"][0])

    def test_the_command_writes_a_report_and_never_edits(self):
        f = self.write("src/lib.rs", TESTS_RS)
        out = self.kit("test-audit", "--out", str(self.root / "audit.md"))
        self.assertIn("RUST TEST AUDIT 7 test(s) · duplicate 1 · table 1 · asserts nothing 1 · ignored 1 · sleeps 1", out)
        self.assertIn("## DUPLICATE (1)", (self.root / "audit.md").read_text(encoding="utf-8"))
        self.assertEqual(f.read_text(encoding="utf-8"), TESTS_RS)


def meta(root, deps):
    """Fake `cargo metadata`: {crate: [crates it depends on]} laid out under root/crates/<name>."""
    return {"workspace_root": str(root),
            "packages": [{"name": n, "manifest_path": str(root / "crates" / n / "Cargo.toml"),
                          "dependencies": [{"name": d, "path": str(root / "crates" / d)} for d in ds] + [{"name": "serde"}],
                          "targets": []} for n, ds in deps.items()]}


class Scope(Tmp):
    def test_changed_crates_and_the_crates_that_use_them(self):
        m = meta(self.root, {"core": [], "api": ["core"], "cli": ["api"], "tools": []})
        self.assertEqual(RK.affected(m, ["crates/tools/src/lib.rs"])[0], ["tools"])
        self.assertEqual(RK.affected(m, ["crates/api/src/lib.rs", "README.md"])[0], ["api", "cli"])
        self.assertEqual(RK.affected(m, ["crates/cli/tests/data/case.json"])[0], ["cli"])
        self.assertEqual(RK.affected(m, ["docs/guide.md", "README.md"])[0], [])

    def test_anything_that_can_change_every_crate_runs_the_workspace(self):
        m = meta(self.root, {"core": [], "api": ["core"], "tools": []})
        for f in ("Cargo.lock", "Cargo.toml", ".cargo/config.toml", "rust-toolchain.toml", "build/gen.rs"):
            self.assertIsNone(RK.affected(m, [f])[0], f)
        names, why = RK.affected(m, ["crates/core/src/lib.rs", "crates/tools/src/lib.rs"])
        self.assertIsNone(names)
        self.assertIn("every crate", why)


@unittest.skipUnless(CARGO, "cargo is not installed")
class CargoWorkspace(Tmp):
    def setUp(self):
        super().setUp()
        self.repo()
        self.write("Cargo.toml", '[workspace]\nresolver = "2"\nmembers = ["crates/core", "crates/api", "crates/tools"]\n')
        for name, dep in (("core", ""), ("api", 'core = { path = "../core" }\n'), ("tools", "")):
            self.write(f"crates/{name}/Cargo.toml",
                       f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n{dep}')
            self.write(f"crates/{name}/src/lib.rs",
                       "pub fn one() -> u8 {\n    1\n}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn is_one() {\n"
                       "        assert_eq!(super::one(), 1);\n    }\n}\n")
        self.write(".gitignore", "target\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def test_a_change_in_one_crate_tests_that_crate_only(self):
        self.write("crates/tools/src/lib.rs", "pub fn one() -> u8 {\n    1\n}\n\npub fn two() -> u8 {\n    2\n}\n\n"
                   "#[cfg(test)]\nmod tests {\n    #[test]\n    fn is_two() {\n        assert_eq!(super::two(), 2);\n    }\n}\n")
        out = self.kit("test", "--scope", "affected", env={"RUSTKIT_BASE": "HEAD"})
        self.assertIn("RUST TEST scope affected: tools (3 crate(s) in the workspace", out)
        self.assertNotIn("is_one", out.replace("tools::tests", ""))
        out = self.kit("test", "--scope", "affected", "--no-nextest", env={"RUSTKIT_BASE": "HEAD"})
        self.assertIn("runner cargo test", out)
        self.assertIn("test tests::is_two ... ok", out)

    def test_a_change_under_a_dependency_tests_its_users_and_no_change_runs_nothing(self):
        out = self.kit("test", "--scope", "affected", env={"RUSTKIT_BASE": "HEAD"})
        self.assertIn("RUST TEST scope none", out)
        self.write("crates/core/src/lib.rs", "pub fn one() -> u8 {\n    1\n}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n"
                   "    fn is_one() {\n        assert_eq!(super::one(), 2, \"broken on purpose\");\n    }\n}\n")
        out = self.kit("test", "--scope", "affected", "--no-nextest", env={"RUSTKIT_BASE": "HEAD"}, code=101)
        self.assertIn("RUST TEST scope affected: api, core", out)

    def test_arch_init_records_the_code_as_it_is_and_then_holds_the_line(self):
        self.write("crates/core/src/ffi.rs", "pub fn peek(p: *const u8) -> u8 {\n    // SAFETY: test pointer\n    unsafe { *p }\n}\n")
        self.write("crates/tools/src/big.rs", "// filler\n" * 30)
        out = self.kit("arch", "--init", "--max-file-lines", "20")
        self.assertIn("3 crate(s) in 2 layer(s) · unsafe allowed in 1 · 1 file(s) already over 20 lines", out)
        arch = json.loads((self.root / "rust-arch.json").read_text(encoding="utf-8"))
        self.assertEqual(arch["layers"], [["core", "tools"], ["api"]])
        self.assertEqual(arch["unsafe_crates"], ["core"])
        self.assertEqual(arch["oversize"], {"crates/tools/src/big.rs": 30})
        self.assertIn("RUST ARCH: PASS · 3 crate(s) in 2 layer(s)", self.kit("arch"))
        # a lower crate reaching up, unsafe in a crate not allowed it, a big file growing, a new big file, a new crate
        self.write("crates/core/Cargo.toml", '[package]\nname = "core"\nversion = "0.1.0"\nedition = "2021"\n\n'
                   '[dependencies]\ntools = { path = "../tools" }\n')
        arch["layers"] = [["core"], ["tools"], ["api"]]
        (self.root / "rust-arch.json").write_text(json.dumps(arch), encoding="utf-8")
        self.write("crates/api/src/raw.rs", "pub fn f(p: *const u8) -> u8 {\n    // SAFETY: test\n    unsafe { *p }\n}\n")
        self.write("crates/tools/src/big.rs", "// filler\n" * 31)
        self.write("crates/tools/src/huge.rs", "// filler\n" * 25)
        out = self.kit("arch", code=1)
        self.assertIn("RUST ARCH: BLOCKED 4", out)
        self.assertIn("core (layer 0) depends on tools (layer 1)", out)
        self.assertIn("crates/api/src/raw.rs: `unsafe` outside the crates allowed to hold it (core)", out)
        self.assertIn("crates/tools/src/big.rs: grew from 30 to 31 lines", out)
        self.assertIn("crates/tools/src/huge.rs: 25 lines, over the 20-line limit", out)
        self.assertIn("RUST GATE: BLOCKED", self.kit("gate", "--no-fmt", code=1))        # the gate carries it

    def test_hot_crates_get_allocation_hints_on_added_lines(self):
        self.kit("arch", "--init")
        arch = json.loads((self.root / "rust-arch.json").read_text(encoding="utf-8"))
        arch["hot_crates"] = ["core"]
        (self.root / "rust-arch.json").write_text(json.dumps(arch), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "arch")
        for name in ("core", "tools"):
            self.write(f"crates/{name}/src/frame.rs", "pub fn names(xs: &[u8]) -> Vec<String> {\n"
                       "    xs.iter().map(|x| x.to_string()).collect()\n}\n")
        out = self.kit("gate", "--no-fmt")
        self.assertIn("crates/core/src/frame.rs:2 hot:", out)
        self.assertNotIn("crates/tools/src/frame.rs:2 hot:", out)

    def test_a_changed_public_item_in_an_interface_crate_is_hinted(self):
        self.kit("arch", "--init")
        arch = json.loads((self.root / "rust-arch.json").read_text(encoding="utf-8"))
        arch["interface_crates"] = ["core"]
        (self.root / "rust-arch.json").write_text(json.dumps(arch), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "arch")
        for name in ("core", "tools"):
            f = self.root / "crates" / name / "src" / "lib.rs"
            f.write_text(f.read_text(encoding="utf-8").replace("pub fn one() -> u8 {", "pub fn one() -> u16 {"),
                         encoding="utf-8")
        out = self.kit("gate", "--no-fmt")
        self.assertIn("api core: public items +1 -1 in an interface crate: removed or changed items", out)
        self.assertNotIn("api tools", out)

    def test_outside_a_ticket_the_whole_workspace_runs_and_the_gate_checks_format(self):
        out = self.kit("test", "--no-nextest")
        self.assertIn("RUST TEST scope full", out)
        self.assertIn("RUST GATE: PASS", self.kit("gate"))
        self.write("crates/api/src/lib.rs", "pub fn one()->u8{1}\n")
        self.assertIn("RUST GATE: FORMAT", self.kit("gate", code=1))


class BenchAndTwice(Tmp):
    LOG1 = ("test step_small ... bench:       1,200 ns/iter (+/- 30)\n"
            "physics/step_large      time:   [2.9000 ms 3.0000 ms 3.1000 ms]\n"
            "a_long_benchmark_name_on_its_own_line\n                        time:   [40.0 us 50.0 us 60.0 us]\n")

    def test_both_harnesses_are_read(self):
        self.assertEqual(RK.parse_bench(self.LOG1), {"step_small": 1200.0, "physics/step_large": 3e6,
                                                     "a_long_benchmark_name_on_its_own_line": 5e4})

    def test_a_baseline_is_saved_then_a_slower_run_fails(self):
        base = str(self.root / "bench.json")
        log = self.write("b1.log", self.LOG1)
        self.assertIn("baseline of 3 benchmark(s) saved", self.kit("bench", "--from-log", str(log), "--baseline", base))
        self.assertIn("0 slower", self.kit("bench", "--from-log", str(log), "--baseline", base))
        log2 = self.write("b2.log", self.LOG1.replace("1,200", "1,290").replace("3.0000 ms", "3.9000 ms"))
        out = self.kit("bench", "--from-log", str(log2), "--baseline", base, code=1)
        self.assertIn("1 slower than the baseline by over 10%", out)                # +7.5% is inside the tolerance
        self.assertIn("SLOWER physics/step_large: 3.00 ms -> 3.90 ms (+30%)", out)
        self.assertIn("0 slower", self.kit("bench", "--from-log", str(log2), "--baseline", base, "--tolerance", "40"))
        self.kit("bench", "--from-log", str(log2), "--baseline", base, "--save")
        self.assertIn("0 slower", self.kit("bench", "--from-log", str(log2), "--baseline", base))

    def test_twice_passes_a_repeatable_command_and_fails_one_that_is_not(self):
        self.assertIn("RUST TWICE: SAME · 2 line(s)", self.kit("twice", "--", f'"{PY}" -c "print(1);print(2)"'))
        out = self.kit("twice", "--", f'"{PY}" -c "import os;print(7);print(os.getpid())"', code=1)
        self.assertIn("RUST TWICE: DIFFERENT", out)
        self.assertIn("line 2:", out)
        self.assertIn("SAME", self.kit("twice", "--ignore", "^[0-9]{2,}$", "--",
                                       f'"{PY}" -c "import os;print(7);print(os.getpid())"'))
        self.kit("twice", "--", f'"{PY}" -c "raise SystemExit(3)"', code=3)


@unittest.skipUnless(CARGO, "cargo is not installed")
class Ownership(Repo):
    """Tickets name the crates they may change (a CRATES line in the roadmap prompt, so in the packet)."""
    schedule = False
    tickets = (ticket("P1-01"), ticket("P1-02"), ticket("P1-03"))

    def setUp(self):
        super().setUp()
        (self.root / "Cargo.toml").write_text('[workspace]\nresolver = "2"\nmembers = ["crates/core", "crates/tools"]\n',
                                              encoding="utf-8")
        for name in ("core", "tools"):
            d = self.root / "crates" / name / "src"
            d.mkdir(parents=True)
            (d.parent / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
                                                 encoding="utf-8")
            (d / "lib.rs").write_text("pub fn one() -> u8 {\n    1\n}\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("target\nCargo.lock\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "workspace")
        self.sh(PY, str(SCRIPT), "init", "--stack", "rust", "--lint", "", "--format-check", "")
        self.c("schedule", "--phase", "P1")
        for tid, line in (("P1-01", "CRATES: core"), ("P1-02", "- **CRATES:** `core`, `tools`")):
            f = self.root / ".coniso" / "tickets" / f"{tid}.md"
            f.write_text(f.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")

    def kit(self, cwd, *args, code=0):
        r = subprocess.run([PY, str(self.root / ".coniso" / "stack" / "rustkit.py")] + list(args), cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def test_a_ticket_that_leaves_its_crates_is_blocked(self):
        self.c("dispatch", "P1-01")
        wt = self.wt("P1-01")
        (wt / "crates" / "core" / "src" / "lib.rs").write_text(
            "pub fn one() -> u8 {\n    1\n}\n\npub fn two() -> u8 {\n    2\n}\n", encoding="utf-8")
        (wt / "README.md").write_text("demo, edited\n", encoding="utf-8")            # files outside every crate are free
        self.assertIn("RUST GATE: PASS", self.kit(wt, "gate", "--no-fmt"))
        (wt / "crates" / "tools" / "src" / "extra.rs").write_text("pub fn three() -> u8 {\n    3\n}\n", encoding="utf-8")
        out = self.kit(wt, "gate", "--no-fmt", code=1)
        self.assertIn("ownership tools: ticket P1-01 changed 1 file(s) here", out)
        self.assertIn("CRATES: core", out)

    def test_a_ticket_with_no_crates_line_is_not_limited(self):
        self.c("dispatch", "P1-03")
        wt = self.wt("P1-03")
        (wt / "crates" / "tools" / "src" / "extra.rs").write_text("pub fn three() -> u8 {\n    3\n}\n", encoding="utf-8")
        self.assertIn("RUST GATE: PASS", self.kit(wt, "gate", "--no-fmt"))

    def test_wave_names_the_crate_two_open_tickets_share(self):
        out = self.kit(self.root, "wave", code=1)
        self.assertIn("3 ticket(s) · 2 crate(s) named · 1 shared · no CRATES line: P1-03", out)
        self.assertIn("core: P1-01, P1-02", out)
        f = self.root / ".coniso" / "tickets" / "P1-02.md"
        f.write_text(f.read_text(encoding="utf-8").replace("`core`, ", ""), encoding="utf-8")
        self.assertIn("no crate shared", self.kit(self.root, "wave"))


class Install(Repo):
    schedule = False

    def test_init_stack_rust_installs_the_pack_and_keeps_your_commands(self):
        L = self.root / ".coniso"
        out = self.sh(PY, str(SCRIPT), "init", "--stack", "rust")
        self.assertIn("Stack: rust", out)
        conf = json.loads((L / "config.json").read_text(encoding="utf-8"))
        self.assertIn("print(1)", conf["test_cmd"])                     # the test command given at init stays
        self.assertIn("rustkit.py gate", conf["format_cmd"])
        self.assertIn("cargo clippy", conf["lint_cmd"])
        self.assertIn("target", conf["watch_cmd_pattern"])
        for rel in ("stack/rustkit.py", "stack/proving.md", "stack/research.md", "specialties/rust.md",
                    "specialties/rust-test.md"):
            self.assertTrue((L / rel).exists(), rel)
        brief = (L / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("## Rust rules (stack pack: rust)", brief)
        self.assertIn("Review lens `rust`", brief)
        # without a test command of your own, the pack's is used; an empty one you set is respected
        conf.pop("test_cmd")
        (L / "config.json").write_text(json.dumps(conf), encoding="utf-8")
        self.sh(PY, str(SCRIPT), "init")
        self.assertIn("rustkit.py test", json.loads((L / "config.json").read_text(encoding="utf-8"))["test_cmd"])
        self.sh(PY, str(SCRIPT), "init", "--lint", "")
        self.sh(PY, str(SCRIPT), "init")
        self.assertEqual(json.loads((L / "config.json").read_text(encoding="utf-8")).get("lint_cmd"), "")

    def test_the_reviewer_carries_the_rust_lens(self):
        self.sh(PY, str(SCRIPT), "init", "--stack", "rust", "--lint", "", "--format-check", "")
        self.c("schedule", "--phase", "P1")
        self.build("P1-01", fname="greet.txt", body="hello\n")
        self.c("review", "P1-01")
        text = (self.root / ".coniso" / "assignments" / "P1-01.R1.md").read_text(encoding="utf-8")
        self.assertIn("correctness+spec+rust", text)

    def test_doctor_prints_a_plan_and_sweep_reports(self):
        self.sh(PY, str(SCRIPT), "init", "--stack", "rust")
        r = subprocess.run([PY, str(self.root / ".coniso" / "stack" / "rustkit.py"), "doctor"], cwd=str(self.root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RUST DOCTOR", r.stdout)
        self.assertIn("check run(s) at once", r.stdout)
        self.assertIn("doctor --apply", r.stdout)
        self.assertIn("SWEEP", self.c("sweep"))


class NoPrivateReferences(unittest.TestCase):
    """The repository is public: nothing in it may name a private project, a person's machine or an address."""

    def test_tracked_files_hold_no_private_names_paths_or_addresses(self):
        import re
        r = subprocess.run(["git", "-C", str(PLUGIN), "ls-files"], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("not a git checkout")
        bad = re.compile(r"(?i)[a-z]:[\\/]+users[\\/]|/home/[a-z]\w+/|/users/[a-z]\w+/|project[ -](sol|luna)\b"
                         r"|[\w.+-]+@(gmail|outlook|hotmail|yahoo|proton|icloud)\.\w+")
        found = []
        for rel in r.stdout.splitlines():
            f = PLUGIN / rel
            if f.suffix.lower() in (".png", ".jpg", ".ico", ".gif") or f.resolve() == Path(__file__).resolve():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if bad.search(line):
                    found.append(f"{rel}:{i}: {line.strip()[:80]}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
