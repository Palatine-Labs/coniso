#!/usr/bin/env python3
"""rustkit: the Rust stack pack's helper. Standard library only; it opens no network connection itself (cargo and
the cargo tools it calls do their own).

  doctor [--apply] [--max-cores N]   what is installed, and concurrency and cache settings sized to this machine
  gate [--no-fmt]                    block debug macros and unexplained `unsafe` in the diff, print hints, check format
  test [--scope auto|full|affected]  run the tests of the crates a ticket changed; the whole workspace at a merge
  test-audit [--log FILE]            redundant, empty, ignored and slow tests (reports; never edits)
  arch [--init]                      crate layers, file size limits and where `unsafe` may live (rust-arch.json)
  wave [--wave N]                    open tickets of one wave that name the same crate (packet line `CRATES: a, b`)
  bench [-p CRATE] [--save]          benchmarks against a saved baseline; fails when one is slower than the tolerance
  twice [--file F] -- CMD            run a command twice and compare its output: a determinism check
  deps                               duplicate versions, advisories and unused dependencies, in a few lines
  trim [--max-gb N]                  delete the shared build cache when it is over its size limit

Installed at .coniso/stack/rustkit.py by `init --stack rust`. Run it from a ticket's worktree or the main checkout.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONISO = HERE.parent if HERE.name == "stack" and HERE.parent.name == ".coniso" else None
CACHE = "cache/rust-target"


def sh(args, cwd=None, env=None):
    try:
        return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", f"not found: {args[0]}")


def have(*args):
    return sh(list(args)).returncode == 0


def top(cwd=None):
    r = sh(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else Path(cwd or ".").resolve()


def coniso_dir(here):
    """.coniso of the main checkout: beside this script when installed, else found from the checkout."""
    if CONISO:
        return CONISO
    r = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], here)
    if r.returncode == 0 and r.stdout.strip():
        d = Path(r.stdout.strip()).parent / ".coniso"
        if d.is_dir():
            return d
    return None


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def context(cwd=None):
    """Where this run is: (checkout top, is it a ticket worktree, the ref its changes are measured from)."""
    here = top(cwd)
    base = os.environ.get("RUSTKIT_BASE")
    L = coniso_dir(here)
    if not L:
        return here, False, base or "HEAD"
    sprint = read_json(L / "sprint.json")
    for t in (sprint.get("tickets") or {}).values():
        if t.get("wt") and Path(t["wt"]).resolve() == here:
            return here, True, base or t.get("base") or "HEAD"
    return here, False, base or sprint.get("branch") or "HEAD"


def changed_files(here, base):
    out = set()
    for args in (["git", "diff", "--name-only", base], ["git", "ls-files", "-o", "--exclude-standard"]):
        r = sh(args, here)
        if r.returncode == 0:
            out.update(x.strip() for x in r.stdout.splitlines() if x.strip())
    return sorted(out)


# ---------------------------------------------------------------- doctor

def plan_hardware(cores, max_cores=16):
    """Slots and jobs for a modest share of the machine: three quarters of the cores, never more than max_cores."""
    usable = max(2, int(min(cores, max_cores) * 0.75))
    slots = max(1, min(3, usable // 4))
    return {"usable": usable, "slots": slots, "jobs": max(2, usable // slots)}


def cmd_doctor(a):
    here = top()
    L = coniso_dir(here)
    cores = os.cpu_count() or 4
    hw = plan_hardware(cores, a.max_cores)
    tools = {"cargo": have("cargo", "--version"), "clippy": have("cargo", "clippy", "--version"),
             "rustfmt": have("cargo", "fmt", "--version"), "nextest": have("cargo", "nextest", "--version"),
             "sccache": have("sccache", "--version"), "cargo-audit": have("cargo", "audit", "--version"),
             "cargo-machete": have("cargo", "machete", "--version")}
    print("RUST DOCTOR")
    print("  tools: " + " · ".join(f"{k} {'yes' if v else 'NO'}" for k, v in tools.items()))
    env = {"CARGO_BUILD_JOBS": str(hw["jobs"]), "NEXTEST_TEST_THREADS": str(hw["jobs"]), "CARGO_INCREMENTAL": "0",
           "CARGO_TERM_COLOR": "never"}
    if tools["sccache"]:
        env["RUSTC_WRAPPER"] = "sccache"
        cache = "sccache (each worktree keeps its own target folder; compiled crates are shared)"
    elif L:
        env["CARGO_TARGET_DIR"] = (L / CACHE).as_posix()
        cache = (f"one target folder for every worktree ({(L / CACHE).as_posix()}): dependencies compile once; "
                 "cargo makes builds take turns on it")
    else:
        cache = "none (no .coniso here)"
    print(f"  machine: {cores} cores · using {hw['usable']} · {hw['slots']} check run(s) at once x {hw['jobs']} jobs each")
    print(f"  cache: {cache}")
    for name, why in (("nextest", "faster test runs and per-test times (cargo install cargo-nextest --locked)"),
                      ("sccache", "a compile cache that parallel worktrees can share (cargo install sccache --locked)"),
                      ("cargo-audit", "`deps` reports advisories (cargo install cargo-audit --locked)")):
        if not tools[name]:
            print(f"  missing {name}: {why}")
    flags = ["init", "--check-slots", str(hw["slots"])] + [x for k, v in env.items() for x in ("--env", f"{k}={v}")]
    if not L or not (L / "coniso.py").exists():
        print("  to apply: coniso " + " ".join(flags))
        return
    if a.apply:
        r = sh([sys.executable, str(L / "coniso.py")] + flags, L.parent)
        print("  applied" if r.returncode == 0 else f"  NOT applied: {(r.stdout + r.stderr).strip()[-300:]}")
        sys.exit(r.returncode)
    print("  to apply: run `doctor --apply` (it runs init with --check-slots and --env for the lines above)")


# ---------------------------------------------------------------- gate

BLOCK = [("dbg", re.compile(r"\bdbg!\s*\("), "debug macro left in", True),
         ("todo", re.compile(r"\b(todo|unimplemented)!\s*\("), "unfinished code", False)]
HINTS = [("unwrap", re.compile(r"\.(unwrap|expect)\s*\("), "can panic: return the error, or say why it cannot fail", False),
         ("panic", re.compile(r"\bpanic!\s*\("), "panics in library code", False),
         ("allow", re.compile(r"#!?\[allow\("), "a lint switched off: say why beside it", True),
         ("ignore", re.compile(r"#\[ignore"), "a test switched off", True),
         ("print", re.compile(r"\be?println!\s*\("), "prints from library code: use the project's logging", False),
         ("sleep", re.compile(r"\b(thread::sleep|time::sleep|sleep)\s*\("), "a sleep in a test: slow, and flaky under load", None)]
UNSAFE = re.compile(r"\bunsafe\s*(\{|fn\b|impl\b|trait\b|extern\b)")
SAFETY = re.compile(r"(?i)(//|///|//!|\*)\s*#?\s*safety\b")
TEST_PATH = re.compile(r"(^|/)(tests|benches|examples)/|(^|/)(tests?|.*_tests?)\.rs$")
BIN_PATH = re.compile(r"(^|/)(main|build)\.rs$|(^|/)bin/")


def strip_code(line):
    """The line without string contents and without a trailing // comment (good enough for pattern checks)."""
    line = re.sub(r'"(\\.|[^"\\])*"', '""', line)
    return line.split("//")[0]


def added_lines(here, base):
    """{file: [new line numbers]} for .rs files changed since base, working tree included."""
    r = sh(["git", "diff", "-U0", base, "--", "*.rs"], here)
    out, cur, n = {}, None, 0
    for line in (r.stdout or "").splitlines():
        if line.startswith("+++ "):
            cur = None if line.endswith("/dev/null") else line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and cur:
            out.setdefault(cur, []).append(n)
            n += 1
    for f in sh(["git", "ls-files", "-o", "--exclude-standard", "--", "*.rs"], here).stdout.splitlines():
        try:
            out[f.strip()] = list(range(1, len((here / f.strip()).read_text(encoding="utf-8", errors="replace").splitlines()) + 1))
        except OSError:
            pass
    return out


def scan(here, files):
    """(blocking findings, hints) for the added lines. Code after a file's first #[cfg(test)] counts as test code."""
    blocked, hints = [], []
    for rel, nums in sorted(files.items()):
        try:
            lines = (here / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        test_from = next((i + 1 for i, x in enumerate(lines) if "#[cfg(test)]" in x), None)
        path_is_test, is_bin = bool(TEST_PATH.search(rel)), bool(BIN_PATH.search(rel))
        for n in nums:
            if not 1 <= n <= len(lines) or "rustkit:allow" in lines[n - 1]:
                continue
            code = strip_code(lines[n - 1])
            in_test = path_is_test or (test_from is not None and n >= test_from)
            where = f"{rel}:{n}"
            for name, rx, why, in_tests_too in BLOCK:
                if rx.search(code) and (in_tests_too or not in_test):
                    blocked.append(f"{where} {name}: {why}")
            if UNSAFE.search(code) and not any(SAFETY.search(x) for x in lines[max(0, n - 7):n]):
                blocked.append(f"{where} unsafe: no `// SAFETY:` comment (or `# Safety` doc section) in the 6 lines above")
            for name, rx, why, where_ok in HINTS:
                ok = in_test if where_ok is None else (where_ok or not in_test)
                if name == "print" and is_bin:
                    ok = False
                if ok and rx.search(code):
                    hints.append(f"{where} {name}: {why}")
    return blocked, hints


def cmd_gate(a):
    here, _, base = context()
    files = added_lines(here, base)
    blocked, hints = scan(here, files)
    arch = load_arch(here)
    meta = metadata(here) if (here / "Cargo.toml").exists() else None
    if meta:
        blocked += ownership(here, meta, changed_files(here, base))
    if arch is not None and meta:
        more, notes = arch_check(here, meta, arch)
        blocked += ["arch " + x for x in more]
        hints += hot_hints(here, meta, arch, files) + api_hints(here, meta, arch, base) + ["arch " + x for x in notes]
    if hints:
        print(f"RUST HINTS {len(hints)} (never block; the reviewer rules on each)")
        for x in hints[:20]:
            print("  " + x)
        if len(hints) > 20:
            print(f"  ... {len(hints) - 20} more")
    if blocked:
        print(f"RUST GATE: BLOCKED {len(blocked)} (a deliberate one gets `rustkit:allow` on its line)")
        for x in blocked[:30]:
            print("  " + x)
        sys.exit(1)
    if not a.no_fmt and (here / "Cargo.toml").exists():
        r = sh(["cargo", "fmt", "--all", "--check"], here)
        if r.returncode != 0:
            names = sorted(set(re.findall(r"^Diff in (.+?)(?::\d+| at line \d+):?", r.stdout, re.M)))
            print(f"RUST GATE: FORMAT {len(names) or '?'} file(s) need `cargo fmt --all`: {', '.join(names[:8])}"
                  if names else "RUST GATE: FORMAT `cargo fmt --all --check` failed:\n" + (r.stdout + r.stderr)[-600:])
            sys.exit(1)
    print(f"RUST GATE: PASS · {len(hints)} hint(s)")


# ---------------------------------------------------------------- test scope

GLOBAL = re.compile(r"(^|/)(Cargo\.toml|Cargo\.lock|rust-toolchain(\.toml)?|rustfmt\.toml|clippy\.toml)$|^\.cargo/")
IGNORED = re.compile(r"\.(md|txt|png|jpg|svg|html)$|^(docs|\.github|\.coniso)/|(^|/)(LICENSE|NOTICE|\.gitignore)$")


def metadata(here):
    r = sh(["cargo", "metadata", "--no-deps", "--format-version", "1"], here)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip().startswith("{") else None


def affected(meta, files):
    """(crate names to test, why) or (None, why) when the whole workspace has to run."""
    root = Path(meta["workspace_root"]).resolve()
    pkgs = {pk["name"]: Path(pk["manifest_path"]).resolve().parent for pk in meta["packages"]}
    hit = set()
    for f in files:
        if IGNORED.search(f):
            continue
        path = (root / f).resolve()
        owners = [n for n, d in pkgs.items() if d == path.parent or d in path.parents]
        owner = max(owners, key=lambda n: len(pkgs[n].parts)) if owners else None
        if owner is None or (pkgs[owner] == root and len(pkgs) > 1 and GLOBAL.search(f)):
            if owner is None and not (GLOBAL.search(f) or f.endswith(".rs")):
                continue        # a file no crate owns and that cannot change a build
            return None, f"{f} can change every crate"
        hit.add(owner)
    by_dir = {d: n for n, d in pkgs.items()}
    users = {}
    for pk in meta["packages"]:
        for dep in pk.get("dependencies", []):
            d = by_dir.get(Path(dep["path"]).resolve()) if dep.get("path") else None
            if d:
                users.setdefault(d, set()).add(pk["name"])
    todo = list(hit)
    while todo:
        for u in users.get(todo.pop(), ()):
            if u not in hit:
                hit.add(u)
                todo.append(u)
    if len(hit) == len(pkgs):
        return None, "every crate is affected"
    return sorted(hit), "changed crates and the crates that depend on them"


def has_doctests(meta, names):
    return any(t.get("doctest") and set(t.get("kind", [])) & {"lib", "rlib", "proc-macro", "dylib", "cdylib"}
               for pk in meta["packages"] if names is None or pk["name"] in names for t in pk.get("targets", []))


def cmd_test(a):
    here, is_ticket, base = context()
    meta = metadata(here)
    if not meta:
        print("RUST TEST: `cargo metadata` failed here; is this a cargo project?")
        sys.exit(2)
    names, why = None, "merge gate or no ticket: the whole workspace"
    if a.scope == "affected" or (a.scope == "auto" and is_ticket):
        names, why = affected(meta, changed_files(here, base))
    if names is not None and not names:
        print("RUST TEST scope none: no crate changed since " + base[:12])
        return
    nextest = not a.no_nextest and have("cargo", "nextest", "--version")
    sel = ["--workspace"] if names is None else [x for n in names for x in ("-p", n)]
    runs = [["cargo", "nextest", "run"] + sel + a.rest] if nextest else [["cargo", "test"] + sel + a.rest]
    if nextest and has_doctests(meta, names):
        runs.append(["cargo", "test", "--doc"] + sel)      # nextest does not run doctests
    print(f"RUST TEST scope {'full' if names is None else 'affected: ' + ', '.join(names)} "
          f"({len(meta['packages'])} crate(s) in the workspace; {why}) · runner {'nextest' if nextest else 'cargo test'}",
          flush=True)
    for cmd in runs:
        code = subprocess.run(cmd, cwd=str(here)).returncode
        if code != 0:
            sys.exit(code)


# ---------------------------------------------------------------- test audit

TEST_ATTR = re.compile(r"#\[\s*(?:[\w:]+::)?(?:test|rstest|test_case|bench)\b[^\]]*\]")
FN = re.compile(r"\bfn\s+(\w+)\s*[<(]")
SKIP_DIRS = {"target", ".git", ".coniso", "node_modules", "vendor"}


def rs_files(root):
    """Every .rs file under root, without walking into build output or other skipped folders."""
    for dp, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            if n.endswith(".rs"):
                yield Path(dp) / n


def body_at(text, i):
    """The {...} block starting at or after i: (body, end index). Skips strings, char literals and comments."""
    n, depth, start = len(text), 0, None
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if text.startswith("/*", i):
            i = text.find("*/", i)
            i = n if i < 0 else i + 2
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "'" and i + 2 < n and (text[i + 2] == "'" or (text[i + 1] == "\\" and text[i + 3:i + 4] == "'")):
            i += 3 if text[i + 2] == "'" else 4
            continue
        elif c == "{":
            depth += 1
            start = i if start is None else start
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start + 1:i], i + 1
        elif c == ";" and start is None:
            return None, i + 1
        i += 1
    return None, n


def find_tests(root):
    """Every test function under root: {file, line, name, attrs, body}."""
    out, helpers = [], {}
    for path in rs_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "#[" in text and "test" in text:
            for f in FN.finditer(text):               # every function of a file with tests: a test may assert through one
                body, _ = body_at(text, f.end())
                if body:
                    helpers[f.group(1)] = helpers.get(f.group(1), "") + body
        pos = 0
        for m in TEST_ATTR.finditer(text):
            if m.start() < pos:
                continue                      # a second attribute of a test already taken
            f = FN.search(text, m.end())
            if not f or text.count("\n\n", m.end(), f.start()) > 0 and "#[" not in text[m.end():f.start()]:
                continue
            body, end = body_at(text, f.end())
            if body is None:
                continue
            attrs = text[m.start():f.start()]
            out.append({"file": path.relative_to(root).as_posix(), "line": text.count("\n", 0, f.start()) + 1,
                        "name": f.group(1), "attrs": attrs, "body": body})
            pos = end
    asserting = {n for n, b in helpers.items() if ASSERTS.search(b)}
    for _ in range(3):                                # helpers that assert through another helper
        asserting |= {n for n, b in helpers.items() if n not in asserting and CALL.findall(b) and asserting & set(CALL.findall(b))}
    for t in out:
        t["asserts"] = bool(ASSERTS.search(t["attrs"] + t["body"]) or asserting & set(CALL.findall(t["body"])))
    return out


def norm(body, literals=False):
    body = re.sub(r"//[^\n]*", "", body)
    if literals:
        body = re.sub(r'"(\\.|[^"\\])*"', "S", body)
        body = re.sub(r"\b\d[\d_]*(\.\d+)?(_?[iuf]\d+)?\b", "N", body)
    return re.sub(r"\s+", "", body)


CALL = re.compile(r"\b([a-z_]\w*)\s*(?:::<[^>]*>)?\s*\(")
ASSERTS = re.compile(r"assert|expect\s*\(|unwrap\s*\(|panic!|\?\s*;|should_panic|prop_|insta::|check|verify|matches!")
TIMING = re.compile(r"^\s*(PASS|FAIL|SLOW|LEAK|FLAKY)\s+\[\s*([\d.]+)s\]\s+(\S+)\s+(\S+)", re.M)


def audit(root, log=None):
    tests = find_tests(root)
    loc = lambda t: f"{t['file']}:{t['line']} {t['name']}"
    exact, shape = {}, {}
    for t in tests:
        if len(norm(t["body"])) >= 40:
            exact.setdefault(hashlib.sha1(norm(t["body"]).encode()).hexdigest(), []).append(t)
            shape.setdefault(hashlib.sha1(norm(t["body"], True).encode()).hexdigest(), []).append(t)
    dup = [g for g in exact.values() if len(g) > 1]
    in_dup = {id(t) for g in dup for t in g}
    table = [g for g in shape.values() if len(g) >= 3 and not all(id(t) in in_dup for t in g)]
    res = {"tests": len(tests),
           "duplicate": [" = ".join(loc(t) for t in g) for g in dup],
           "table": [f"{len(g)} tests differ only in their literals: " + ", ".join(loc(t) for t in g[:4])
                     + (" ..." if len(g) > 4 else "") for g in table],
           "empty": [loc(t) for t in tests if not t["asserts"]],
           "ignored": [loc(t) for t in tests if re.search(r"#\[ignore\s*\]", t["attrs"])],
           "sleep": [loc(t) for t in tests if re.search(r"\bsleep\s*\(", t["body"])],
           "slow": [], "total": 0.0}
    if log:
        seen = [(float(s), f"{b} {n}") for _, s, b, n in TIMING.findall(Path(log).read_text(encoding="utf-8", errors="replace"))]
        res["total"] = sum(s for s, _ in seen)
        res["slow"] = [f"{s:.2f}s ({s / res['total'] * 100:.0f}% of test time) {n}"
                       for s, n in sorted(seen, reverse=True)[:10] if res["total"] and s >= 0.5]
    return res


AUDIT_ROWS = (("duplicate", "DUPLICATE", "same body as another test: keep one"),
              ("table", "TABLE", "one table-driven test would cover these"),
              ("empty", "ASSERTS NOTHING", "no assertion in sight (a helper may assert): check, then fix or delete"),
              ("ignored", "IGNORED", "`#[ignore]` with no reason: give one, fix it or delete it"),
              ("sleep", "SLEEPS", "waits on a clock: use a channel, a barrier or paused time"),
              ("slow", "SLOW", "the slowest tests in the log: narrow them or move them behind a feature"))


def cmd_test_audit(a):
    here = top()
    res = audit(here, a.log)
    L = coniso_dir(here)
    out = Path(a.out) if a.out else (L / "maps" / "rust-test-audit.md" if L else None)
    lines = [f"# Rust test audit · {res['tests']} test(s)" + (f" · {res['total']:.1f}s in the log" if a.log else ""), ""]
    for key, head, what in AUDIT_ROWS:
        if res[key]:
            lines += [f"## {head} ({len(res[key])}): {what}"] + [f"- {x}" for x in res[key]] + [""]
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
    counts = " · ".join(f"{head.lower()} {len(res[key])}" for key, head, _ in AUDIT_ROWS if res[key])
    print(f"RUST TEST AUDIT {res['tests']} test(s) · {counts or 'nothing to clean up'}" + (f" · report {out.as_posix()}" if out else ""))
    for key, head, _ in AUDIT_ROWS:
        for x in res[key][:3]:
            print(f"  {head}: {x[:150]}")


# ---------------------------------------------------------------- architecture

ARCH_FILE = "rust-arch.json"
HOT = re.compile(r"\b(Vec::new|Vec::with_capacity|vec!|Box::new|String::new|String::from|HashMap::new|BTreeMap::new|"
                 r"format!)\b|\.(to_string|to_owned|to_vec|clone|collect)\s*(::<[^>]*>)?\s*\(")
UNSAFE_ANY = re.compile(r"\bunsafe\b")


def crate_files(pkg_dir):
    return rs_files(pkg_dir)


def uses_unsafe(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "unsafe" in text and any(UNSAFE_ANY.search(strip_code(x)) for x in text.splitlines()
                                    if "unsafe" in x and "unsafe_code" not in x)


def internal_deps(meta):
    """{crate: set of workspace crates it builds against} (dev-dependencies left out: tests may reach anywhere)."""
    by_dir = {Path(pk["manifest_path"]).resolve().parent: pk["name"] for pk in meta["packages"]}
    out = {}
    for pk in meta["packages"]:
        out[pk["name"]] = {by_dir[Path(d["path"]).resolve()] for d in pk.get("dependencies", [])
                           if d.get("path") and d.get("kind") != "dev" and Path(d["path"]).resolve() in by_dir}
        out[pk["name"]].discard(pk["name"])
    return out


def derive_layers(deps):
    """Layers from the dependency graph: a crate sits one layer above the highest crate it depends on."""
    depth = {}

    def d(n, seen=()):
        if n not in depth:
            depth[n] = 0 if not deps[n] else 1 + max(d(x, seen + (n,)) for x in deps[n] if x not in seen)
        return depth[n]
    for n in deps:
        d(n)
    return [sorted(n for n in deps if depth[n] == i) for i in range(max(depth.values(), default=-1) + 1)]


def arch_check(here, meta, arch):
    """(blocking findings, notes) of the workspace against rust-arch.json."""
    blocked, notes = [], []
    root = Path(meta["workspace_root"]).resolve()
    dirs = {pk["name"]: Path(pk["manifest_path"]).resolve().parent for pk in meta["packages"]}
    layer = {n: i for i, names in enumerate(arch.get("layers") or []) for n in names}
    deps = internal_deps(meta)
    if layer:
        for n in sorted(deps):
            if n not in layer:
                blocked.append(f"{n}: not in any layer of {ARCH_FILE}; place it before it gains users")
                continue
            for d in sorted(deps[n]):
                if d in layer and layer[d] >= layer[n]:
                    blocked.append(f"{n} (layer {layer[n]}) depends on {d} (layer {layer[d]}): a crate may only use "
                                   "lower layers. Move the shared part down, or pass it in through a trait")
    allowed = arch.get("unsafe_crates")
    max_file, max_crate = arch.get("max_file_lines"), arch.get("max_crate_lines")
    known = arch.get("oversize") or {}
    for n, d in sorted(dirs.items()):
        total = 0
        for f in crate_files(d):
            rel = f.relative_to(root).as_posix()
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").count("\n")
            except OSError:
                continue
            total += lines
            if max_file and lines > max_file:
                if rel not in known:
                    blocked.append(f"{rel}: {lines} lines, over the {max_file}-line limit: split it by responsibility")
                elif lines > known[rel]:
                    blocked.append(f"{rel}: grew from {known[rel]} to {lines} lines; it is already over the "
                                   f"{max_file}-line limit, so new code goes in a new module")
            if allowed is not None and n not in allowed and uses_unsafe(f):
                blocked.append(f"{rel}: `unsafe` outside the crates allowed to hold it ({', '.join(allowed) or 'none'})")
        if max_crate and total > max_crate:
            notes.append(f"{n}: {total} lines, over {max_crate}: a candidate to split into two crates")
    return blocked, notes


def load_arch(here):
    f = here / ARCH_FILE
    return read_json(f) if f.exists() else None


def cmd_arch(a):
    here = top()
    meta = metadata(here)
    if not meta:
        print("RUST ARCH: `cargo metadata` failed here; is this a cargo project?")
        sys.exit(2)
    f = Path(meta["workspace_root"]) / ARCH_FILE
    if a.init:
        if f.exists() and not a.force:
            print(f"RUST ARCH: {ARCH_FILE} exists; --init --force rewrites it from the code as it is now")
            sys.exit(2)
        root = Path(meta["workspace_root"]).resolve()
        dirs = {pk["name"]: Path(pk["manifest_path"]).resolve().parent for pk in meta["packages"]}
        over = {}
        for d in dirs.values():
            for x in crate_files(d):
                n = x.read_text(encoding="utf-8", errors="replace").count("\n")
                if n > a.max_file_lines:
                    over[x.relative_to(root).as_posix()] = n
        arch = {"layers": derive_layers(internal_deps(meta)),
                "unsafe_crates": sorted(n for n, d in dirs.items() if any(uses_unsafe(x) for x in crate_files(d))),
                "hot_crates": [], "max_file_lines": a.max_file_lines, "max_crate_lines": a.max_crate_lines,
                "oversize": dict(sorted(over.items()))}
        f.write_text(json.dumps(arch, indent=1) + "\n", encoding="utf-8")
        print(f"RUST ARCH: wrote {ARCH_FILE} from the code as it is: {len(dirs)} crate(s) in {len(arch['layers'])} "
              f"layer(s) · unsafe allowed in {len(arch['unsafe_crates'])} · {len(over)} file(s) already over "
              f"{a.max_file_lines} lines (they may shrink, not grow). Review it, list hot_crates, and commit it.")
        return
    arch = read_json(f) if f.exists() else None
    if arch is None:
        print(f"RUST ARCH: no {ARCH_FILE}; `arch --init` writes one from the code as it is")
        return
    blocked, notes = arch_check(here, meta, arch)
    for x in notes[:10]:
        print("  note " + x)
    if blocked:
        print(f"RUST ARCH: BLOCKED {len(blocked)}")
        for x in blocked[:30]:
            print("  " + x)
        sys.exit(1)
    print(f"RUST ARCH: PASS · {len(meta['packages'])} crate(s) in {len(arch.get('layers') or [])} layer(s)")


def hot_hints(here, meta, arch, files):
    """Hints for allocations and copies on added lines of the crates rust-arch.json calls hot."""
    root = Path(meta["workspace_root"]).resolve()
    hot = [Path(pk["manifest_path"]).resolve().parent for pk in meta["packages"] if pk["name"] in (arch.get("hot_crates") or [])]
    out = []
    for rel, nums in sorted(files.items()):
        path = (here / rel).resolve()
        if TEST_PATH.search(rel) or "/benches/" in rel or not any(d in path.parents for d in hot):
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        test_from = next((i + 1 for i, x in enumerate(lines) if "#[cfg(test)]" in x), len(lines) + 1)
        for n in nums:
            if 1 <= n < test_from and n <= len(lines) and "rustkit:allow" not in lines[n - 1] and HOT.search(strip_code(lines[n - 1])):
                out.append(f"{rel}:{n} hot: allocates or copies in a hot crate: reuse a buffer, borrow, or say why it is off the per-frame path")
    return out


# ---------------------------------------------------------------- crate ownership, public API

CRATES_LINE = re.compile(r"(?im)^[\s>*\-]*\**CRATES\**\s*:\s*\**\s*(.+)$")
PUB = re.compile(r"^\s*pub\s+(?:async\s+|const\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
                 r"(fn|struct|enum|trait|type|const|static|mod|use|union)\b")


def ticket_here(here):
    """(ticket id, .coniso) when this checkout is a ticket's worktree, else (None, .coniso or None)."""
    L = coniso_dir(here)
    if L:
        for tid, t in (read_json(L / "sprint.json").get("tickets") or {}).items():
            if t.get("wt") and Path(t["wt"]).resolve() == here:
                return tid, L
    return None, L


def declared_crates(L, tid):
    """The crates a ticket's packet says it may change (a `CRATES: a, b` line), or None when it has no such line."""
    f = L / "tickets" / (re.sub(r"[^\w.-]", "_", tid) + ".md")
    try:
        m = CRATES_LINE.search(f.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return sorted(set(re.findall(r"[\w-]+", m.group(1).replace("`", " ")))) if m else None


def owning_crates(meta, files):
    """{crate: [files]} for the files that sit inside a workspace crate."""
    root = Path(meta["workspace_root"]).resolve()
    pkgs = {pk["name"]: Path(pk["manifest_path"]).resolve().parent for pk in meta["packages"]}
    out = {}
    for f in files:
        path = (root / f).resolve()
        owners = [n for n, d in pkgs.items() if d in path.parents and d != root]
        if owners and not IGNORED.search(f):
            out.setdefault(max(owners, key=lambda n: len(pkgs[n].parts)), []).append(f)
    return out


def ownership(here, meta, files):
    """Blocking findings when a ticket changed a crate its packet does not name."""
    tid, L = ticket_here(here)
    names = declared_crates(L, tid) if tid else None
    if names is None:
        return []
    return [f"ownership {crate}: ticket {tid} changed {len(fs)} file(s) here (first: {fs[0]}) but its packet says "
            f"CRATES: {', '.join(names)}. Put the change in its own ticket, or have the Director widen the line"
            for crate, fs in sorted(owning_crates(meta, files).items()) if crate not in names]


def api_hints(here, meta, arch, base):
    """Hints when the diff changes public items of a crate rust-arch.json lists under interface_crates."""
    watched = set(arch.get("interface_crates") or [])
    if not watched:
        return []
    r = sh(["git", "diff", "-U0", base, "--", "*.rs"], here)
    per, cur = {}, None
    for line in (r.stdout or "").splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur and line[:1] in "+-" and not line.startswith(("+++", "---")) and PUB.search(line[1:]):
            per.setdefault(cur, [0, 0])[line[0] == "-"] += 1
    out = []
    for crate, fs in sorted(owning_crates(meta, [f for f in per if not TEST_PATH.search(f)]).items()):
        if crate in watched:
            add, rem = sum(per[f][0] for f in fs), sum(per[f][1] for f in fs)
            out.append(f"api {crate}: public items +{add} -{rem} in an interface crate"
                       + (": removed or changed items can break every user; this belongs in its own ticket, merged first"
                          if rem else ": additions only"))
    return out


def cmd_wave(a):
    """Two open tickets of one wave that name the same crate would build in the same files at the same time."""
    here = top()
    L = coniso_dir(here)
    sprint = read_json(L / "sprint.json") if L else {}
    waves = {}
    for tid, t in (sprint.get("tickets") or {}).items():
        if t.get("state") not in ("merged", "dropped") and t.get("wave") is not None and (a.wave is None or t["wave"] == a.wave):
            waves.setdefault(t["wave"], []).append(tid)
    if not waves:
        print("RUST WAVE: no open tickets" + (f" in wave {a.wave}" if a.wave is not None else ""))
        return
    clashes = 0
    for w in sorted(waves):
        by_crate, unscoped = {}, []
        for tid in sorted(waves[w]):
            names = declared_crates(L, tid)
            if names is None:
                unscoped.append(tid)
            for n in names or []:
                by_crate.setdefault(n, []).append(tid)
        shared = {n: ids for n, ids in by_crate.items() if len(ids) > 1}
        clashes += len(shared)
        print(f"RUST WAVE {w}: {len(waves[w])} ticket(s) · {len(by_crate)} crate(s) named · "
              + (f"{len(shared)} shared" if shared else "no crate shared")
              + (f" · no CRATES line: {', '.join(unscoped)}" if unscoped else ""))
        for n, ids in sorted(shared.items()):
            print(f"  {n}: {', '.join(ids)} (make one depend on the other, or split the crate's work by file in a lead's plan)")
    sys.exit(1 if clashes else 0)


# ---------------------------------------------------------------- benchmarks, repeatability

BENCH_LIBTEST = re.compile(r"^test (\S+)\s+\.\.\. bench:\s+([\d,.]+) ns/iter", re.M)
UNIT = {"ns": 1.0, "µs": 1e3, "us": 1e3, "ms": 1e6, "s": 1e9}


def parse_bench(text):
    """{benchmark: nanoseconds} from `cargo bench` output: the built-in harness and criterion's `time: [low mid high]`."""
    out = {n: float(v.replace(",", "")) for n, v in BENCH_LIBTEST.findall(text)}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"time:\s+\[\S+ \S+ ([\d.]+) (ns|µs|us|ms|s) ", line)
        if m:
            name = line.split("time:")[0].strip() or next((x.strip() for x in reversed(lines[:i]) if x.strip()), "")
            if name:
                out[name] = float(m.group(1)) * UNIT[m.group(2)]
    return out


def fmt_ns(ns):
    for unit, k in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if ns >= k:
            return f"{ns / k:.2f} {unit}"
    return f"{ns:.0f} ns"


def cmd_bench(a):
    here = top()
    L = coniso_dir(here)
    store = Path(a.baseline) if a.baseline else ((L / "cache" if L else here) / "rust-bench.json")
    key = a.package or "workspace"
    if a.from_log:
        text = Path(a.from_log).read_text(encoding="utf-8", errors="replace")
    else:
        cmd = ["cargo", "bench"] + (["-p", a.package] if a.package else ["--workspace"]) + a.rest
        r = sh(cmd, here)
        text = r.stdout + r.stderr
        if r.returncode != 0:
            print(f"RUST BENCH: `{' '.join(cmd)}` failed:\n{text[-600:]}")
            sys.exit(r.returncode)
    now = parse_bench(text)
    if not now:
        print("RUST BENCH: no benchmark results found in the output")
        sys.exit(2)
    data = read_json(store) if store.exists() else {}
    old = data.get(key) or {}
    if a.save or not old:
        data[key] = now
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"RUST BENCH {key}: baseline of {len(now)} benchmark(s) saved to {store.as_posix()}"
              + ("" if a.save else " (there was none to compare with)"))
        return
    slower = []
    for name, ns in sorted(now.items()):
        if name in old and old[name] > 0:
            change = (ns - old[name]) / old[name] * 100
            if change > a.tolerance:
                slower.append(f"{name}: {fmt_ns(old[name])} -> {fmt_ns(ns)} (+{change:.0f}%)")
    new = sorted(set(now) - set(old))
    print(f"RUST BENCH {key}: {len(now)} benchmark(s) · {len(slower)} slower than the baseline by over {a.tolerance:g}%"
          + (f" · {len(new)} new" if new else ""))
    for x in slower[:15]:
        print("  SLOWER " + x)
    if slower:
        print("  Timings on a busy machine mislead: confirm with `CONISO quiet --runs 5 -- cargo bench ...` before ruling. "
              "`bench --save` accepts the new numbers.")
        sys.exit(1)


def cmd_twice(a):
    """Run a command twice and compare what it printed (and any files named): the cheapest determinism test."""
    here = top()
    if not a.rest:
        print("RUST TWICE: give a command after --")
        sys.exit(2)
    drop = re.compile(a.ignore) if a.ignore else None
    runs = []
    for _ in range(2):
        r = subprocess.run(" ".join(a.rest) if len(a.rest) > 1 else a.rest[0], cwd=str(here), shell=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"RUST TWICE: the command failed (exit {r.returncode}):\n{(r.stdout + r.stderr)[-400:]}")
            sys.exit(r.returncode)
        lines = [x for x in r.stdout.splitlines() if not (drop and drop.search(x))]
        files = {}
        for f in a.file or []:
            try:
                files[f] = hashlib.sha256((here / f).read_bytes()).hexdigest()
            except OSError:
                files[f] = "missing"
        runs.append((lines, files))
    (l1, f1), (l2, f2) = runs
    diff_files = sorted(f for f in f1 if f1[f] != f2[f])
    if l1 == l2 and not diff_files:
        print(f"RUST TWICE: SAME · {len(l1)} line(s)" + (f" · {len(f1)} file(s)" if f1 else "")
              + " · " + hashlib.sha256("\n".join(l1).encode()).hexdigest()[:12])
        return
    print("RUST TWICE: DIFFERENT")
    for f in diff_files:
        print(f"  file {f} differs between the two runs")
    for i, (x, y) in enumerate(zip(l1, l2), 1):
        if x != y:
            print(f"  line {i}:\n    1: {x[:160]}\n    2: {y[:160]}")
            break
    else:
        if len(l1) != len(l2):
            print(f"  run 1 printed {len(l1)} line(s), run 2 printed {len(l2)}")
    sys.exit(1)


# ---------------------------------------------------------------- deps, trim

def cmd_deps(a):
    here = top()
    r = sh(["cargo", "tree", "-d", "--depth", "0", "--prefix", "none", "-e", "normal"], here)
    if r.returncode != 0:
        print("RUST DEPS: `cargo tree` failed: " + (r.stderr or r.stdout).strip()[-200:])
        sys.exit(2)
    vers = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^([\w-]+) v(\S+)", line.strip())
        if m:
            vers.setdefault(m.group(1), set()).add(m.group(2))
    dups = {k: sorted(v) for k, v in vers.items() if len(v) > 1}
    print(f"RUST DEPS duplicate versions: {len(dups)}" + ("" if dups else " (none)"))
    for k, v in sorted(dups.items())[:8]:
        print(f"  {k}: {', '.join(v)}")
    if have("cargo", "audit", "--version"):
        j = sh(["cargo", "audit", "--json"] + (["--no-fetch"] if a.offline else []), here)
        try:
            found = json.loads(j.stdout)["vulnerabilities"]["list"]
            print(f"  advisories: {len(found)}")
            for v in found[:6]:
                print(f"  {v['advisory']['id']} {v['package']['name']} {v['package']['version']}: {v['advisory']['title'][:80]}")
        except (ValueError, KeyError, TypeError):
            print("  advisories: cargo audit gave no readable result")
    else:
        print("  advisories: not checked (cargo-audit is not installed)")
    if have("cargo", "machete", "--version"):
        m = sh(["cargo", "machete"], here)
        unused = [x.strip() for x in m.stdout.splitlines() if x.startswith(("\t", "  "))]
        print(f"  unused dependencies: {len(unused)}" + (": " + ", ".join(unused[:8]) if unused else ""))
    else:
        print("  unused dependencies: not checked (cargo-machete is not installed)")


def cmd_trim(a):
    here = top()
    L = coniso_dir(here)
    d = L / CACHE if L else None
    if not d or not d.is_dir():
        print("RUST CACHE none (no shared target folder)")
        return
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 2 ** 30
    if size <= a.max_gb:
        print(f"RUST CACHE {size:.1f} GB of {a.max_gb} GB: kept")
        return
    shutil.rmtree(d, ignore_errors=True)
    left = d.exists() and any(d.iterdir())
    print(f"RUST CACHE {size:.1f} GB was over {a.max_gb} GB: " +
          ("partly removed (a build is using it; run trim again when none is)" if left else "removed; the next build refills it"))


def main():
    ap = argparse.ArgumentParser(prog="rustkit", description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("doctor")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--max-cores", type=int, default=16)
    s.set_defaults(fn=cmd_doctor)
    s = sp.add_parser("gate")
    s.add_argument("--no-fmt", action="store_true")
    s.set_defaults(fn=cmd_gate)
    s = sp.add_parser("test")
    s.add_argument("--scope", choices=["auto", "full", "affected"], default="auto")
    s.add_argument("--no-nextest", action="store_true")
    s.add_argument("rest", nargs=argparse.REMAINDER, help="-- then extra arguments for the test runner")
    s.set_defaults(fn=cmd_test)
    s = sp.add_parser("test-audit")
    s.add_argument("--log", help="a test log with nextest's per-test times, for the slowest tests")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_test_audit)
    s = sp.add_parser("arch")
    s.add_argument("--init", action="store_true", help="write rust-arch.json from the code as it is now")
    s.add_argument("--force", action="store_true")
    s.add_argument("--max-file-lines", type=int, default=1000)
    s.add_argument("--max-crate-lines", type=int, default=15000)
    s.set_defaults(fn=cmd_arch)
    s = sp.add_parser("wave")
    s.add_argument("--wave", type=int)
    s.set_defaults(fn=cmd_wave)
    s = sp.add_parser("bench")
    s.add_argument("-p", "--package")
    s.add_argument("--save", action="store_true", help="make this run the baseline")
    s.add_argument("--tolerance", type=float, default=10, help="percent slower that fails (default 10)")
    s.add_argument("--from-log", help="read results from a saved `cargo bench` output and run nothing")
    s.add_argument("--baseline", help="baseline file (default .coniso/cache/rust-bench.json)")
    s.add_argument("rest", nargs=argparse.REMAINDER, help="-- then extra arguments for cargo bench")
    s.set_defaults(fn=cmd_bench)
    s = sp.add_parser("twice")
    s.add_argument("--file", action="append", help="a file the command writes; compared too (repeatable)")
    s.add_argument("--ignore", help="regex: printed lines that match are left out of the comparison (timings)")
    s.add_argument("rest", nargs=argparse.REMAINDER, help="-- then the command")
    s.set_defaults(fn=cmd_twice)
    s = sp.add_parser("deps")
    s.add_argument("--offline", action="store_true")
    s.set_defaults(fn=cmd_deps)
    s = sp.add_parser("trim")
    s.add_argument("--max-gb", type=float, default=20)
    s.set_defaults(fn=cmd_trim)
    a = ap.parse_args()
    if getattr(a, "rest", None) and a.rest[0] == "--":
        a.rest = a.rest[1:]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    a.fn(a)


if __name__ == "__main__":
    main()
