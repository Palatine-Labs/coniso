#!/usr/bin/env python3
"""Coniso control script.

Deterministic sprint state, scheduling, worktrees, diffs, merges and roadmap
sync, so no agent spends tokens on bookkeeping or graph math.

Run once from the plugin (`init`), then always from <repo>/.coniso/coniso.py.
Standard library only. Works on Windows, macOS and Linux.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1.0.1"
STATES = ["queued", "building", "built", "reviewing", "passed", "failed",
          "conflict", "merged", "blocked", "dropped"]
ACTIVE = {"building", "built", "reviewing", "passed", "failed", "conflict"}
TERMINAL = {"merged", "blocked", "dropped"}
SIZE_HOURS = {"S": 1.5, "M": 3.5, "L": 7.5}
RM_RE = re.compile(r'(<script type="application/json" id="roadmap-data">)(.*?)(</script>)', re.S)
SCRIPT = Path(__file__).resolve()


# ---------------------------------------------------------------- utilities

def die(msg, code=1):
    print(f"CONISO ERROR: {msg}")
    sys.exit(code)


def run(cmd, cwd=None, shell=False, timeout=None):
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, shell=shell,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        die(f"command not found: {e}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"TIMEOUT after {timeout}s")


def git(args, cwd, check=True):
    r = run(["git"] + args, cwd=cwd)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def now_z():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def p(path):
    """Printable absolute path with forward slashes (works in Git Bash and Python)."""
    return Path(path).resolve().as_posix()


def safe(tid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", tid)


def find_root():
    if SCRIPT.parent.name == ".coniso":
        return SCRIPT.parent.parent
    r = run(["git", "rev-parse", "--git-common-dir"], cwd=Path.cwd())
    if r.returncode == 0:
        common = (Path.cwd() / r.stdout.strip()).resolve()
        if common.name == ".git":
            return common.parent
    r = run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else Path.cwd().resolve()


ROOT = find_root()
L = ROOT / ".coniso"


class Lock:
    """Cross-platform lock file. Held only around state and git-ref changes, never during tests."""

    def __init__(self):
        self.path = L / ".lock"

    def __enter__(self):
        deadline = time.time() + 90
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 300:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    die("state lock busy for 90s; another Coniso command may be stuck (delete .coniso/.lock if not)")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def cfg():
    c = read_json(L / "config.json")
    if c is None:
        die("Coniso is not initialised here. Run the plugin's coniso.py init first.")
    return c


def load_sprint(required=True):
    c = read_json(L / "sprint.json")
    if c is None and required:
        die("no active sprint. Run `schedule` (roadmap) or `adhoc` first.")
    return c


def save_sprint(c):
    write_json(L / "sprint.json", c)


def ticket(c, tid):
    t = c["tickets"].get(tid)
    if not t:
        die(f"ticket {tid} is not in sprint {c['name']}")
    return t


def note(t, state, text="", tokens=None, role=None):
    h = {"ts": now_z(), "state": state, "note": text}
    if tokens:
        h["tokens"] = int(tokens)
    if role:
        h["role"] = role
    t.setdefault("history", []).append(h)


def main_dirty(ignore=()):
    r = git(["status", "--porcelain", "--untracked-files=all"], ROOT)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    keep = []
    for ln in lines:
        path = ln[3:].strip().strip('"')
        if any(path == i or path.startswith(i.rstrip("/") + "/") for i in ignore):
            continue
        keep.append(ln)
    return keep


def current_branch():
    r = git(["rev-parse", "--abbrev-ref", "HEAD"], ROOT, check=False)
    return r.stdout.strip() if r.returncode == 0 else "?"


def git_identity_args(cwd):
    r = git(["config", "user.email"], cwd, check=False)
    return [] if r.stdout.strip() else ["-c", "user.name=Coniso", "-c", "user.email=coniso@localhost"]


# ---------------------------------------------------------------- roadmap

def load_roadmap(path):
    if not path.exists():
        die(f"roadmap not found: {path}")
    html = path.read_text(encoding="utf-8")
    m = RM_RE.search(html)
    if not m:
        die(f"no roadmap-data JSON block in {path.name}")
    return html, m, json.loads(m.group(2))


def write_roadmap(path, html, m, data):
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    path.write_text(html[:m.start(2)] + "\n" + payload + "\n" + html[m.end(2):], encoding="utf-8")


def validate_roadmap(data):
    errs = []
    tickets = data.get("tickets", [])
    ids = [t.get("id") for t in tickets]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errs.append(f"duplicate ticket ids: {sorted(dup)}")
    phases = {ph.get("id") for ph in data.get("phases", [])}
    models = {m.get("key") for m in data.get("models", [])}
    efforts = {e.get("key") for e in data.get("efforts", [])}
    for t in tickets:
        tid = t.get("id")
        for d in t.get("deps", []):
            if d not in ids:
                errs.append(f"{tid}: dep {d} does not exist")
        if t.get("phase") not in phases:
            errs.append(f"{tid}: phase {t.get('phase')} does not exist")
        if len(t.get("checks", [])) != len(t.get("accept", [])):
            errs.append(f"{tid}: checks/accept length mismatch")
        if t.get("status") not in ("todo", "progress", "review", "done", "blocked"):
            errs.append(f"{tid}: bad status {t.get('status')}")
        if t.get("size") not in ("S", "M", "L"):
            errs.append(f"{tid}: bad size {t.get('size')}")
        if models and t.get("model") not in models:
            errs.append(f"{tid}: model {t.get('model')} not in models[]")
        if efforts:
            eff = t.get("effort")
            if t.get("model") == "haiku":
                if eff is not None:
                    errs.append(f"{tid}: haiku tickets need effort null")
            elif eff not in efforts:
                errs.append(f"{tid}: effort {eff} not in efforts[]")
        if not isinstance(t.get("review"), bool):
            errs.append(f"{tid}: review must be boolean")
        if not (t.get("modelWhy") or "").strip():
            errs.append(f"{tid}: empty modelWhy")
    return errs


def is_gate(t):
    return bool(re.search(r"\bgate\b", t.get("title", ""), re.I))


# ---------------------------------------------------------------- planning

def formation(t):
    model = t.get("model") or "sonnet"
    size = t.get("size") or "M"
    eff = t.get("effort")
    if model == "haiku":
        return {"kind": "solo", "model": "haiku", "engineers": 1}
    if size in ("S", "M") and eff != "xhigh":
        # One capable engineer holds an S or M ticket in a single context; splitting it adds seams to stitch.
        return {"kind": "solo", "model": model, "engineers": 1}
    return team_formation(size, model)


def team_formation(size, model):
    n = {"S": 1, "M": 2, "L": 3}.get(size, 2) + (1 if size == "L" and model == "opus" else 0)
    return {"kind": "team", "lead": model, "engineers": n, "engineer_model": "sonnet"}


def cost(f):
    return 1 if f["kind"] == "solo" else 2 + f["engineers"]  # lead + engineers + transient auditor


def stack_lens():
    """Extra review lens contributed by the repo's stack pack (stack.json `review_lens`), or None."""
    conf = read_json(L / "config.json", {}) or {}
    if not conf.get("stack"):
        return None
    return (read_json(L / "stack" / "stack.json", {}) or {}).get("review_lens")


def review_plan(t):
    if t.get("review") or t.get("effort") == "xhigh":
        plan = {"kind": "panel", "reviewers": [{"model": "opus", "lens": "correctness"},
                                             {"model": "sonnet", "lens": "spec+regression"}]}
    elif (t.get("model") or "sonnet") == "haiku":
        plan = {"kind": "batch", "reviewers": [{"model": "sonnet", "lens": "spec"}]}
    elif t.get("model") == "opus":
        plan = {"kind": "single", "reviewers": [{"model": "opus", "lens": "correctness+spec"}]}
    else:
        plan = {"kind": "single", "reviewers": [{"model": "sonnet", "lens": "correctness+spec"}]}
    extra = stack_lens()
    if extra:  # the cheapest reviewer carries the stack lens; no extra reviewer is spent on it
        plan["reviewers"][-1]["lens"] += "+" + extra
    return plan


def critical_paths(tickets):
    """Longest remaining chain (hours) from each ticket through its in-sprint dependents."""
    dependents = {tid: [] for tid in tickets}
    for tid, t in tickets.items():
        for d in t["deps"]:
            if d in dependents:
                dependents[d].append(tid)
    memo = {}

    def cp(tid, seen=()):
        if tid in memo:
            return memo[tid]
        if tid in seen:
            die(f"dependency cycle through {tid}")
        own = SIZE_HOURS.get(tickets[tid].get("size"), 3.5)
        memo[tid] = own + max([cp(x, seen + (tid,)) for x in dependents[tid]] or [0])
        return memo[tid]

    return {tid: cp(tid) for tid in tickets}


def schedule(c, to_place, fixed, start_wave, budget):
    """Greedy list scheduling: dependency order, critical path first, capacity per wave."""
    tickets = c["tickets"]
    cps = critical_paths(tickets)
    placed = dict(fixed)
    load = {}
    for tid, w in fixed.items():
        if tickets[tid]["state"] in ACTIVE:
            load[w] = load.get(w, 0) + cost(tickets[tid]["formation"])
    remaining = set(to_place)
    order = []
    while remaining:
        ready = [tid for tid in remaining
                 if all(d in placed or d not in tickets for d in tickets[tid]["deps"])]
        if not ready:
            die(f"cannot order tickets (cycle?): {sorted(remaining)}")
        ready.sort(key=lambda x: (-cps[x], -SIZE_HOURS.get(tickets[x].get("size"), 3.5), x))
        tid = ready[0]
        t = tickets[tid]
        need = cost(t["formation"])
        if need > budget:
            if t["formation"]["kind"] == "team":
                t["formation"]["engineers"] = max(1, budget - 2)
                note(t, t["state"], f"engineers capped to fit slot budget {budget}")
            need = min(cost(t["formation"]), budget)
        w = max([start_wave] + [placed[d] + 1 for d in t["deps"] if d in placed])
        while load.get(w, 0) + need > budget:
            w += 1
        placed[tid] = w
        load[w] = load.get(w, 0) + need
        t["wave"] = w
        remaining.discard(tid)
        order.append(tid)
    waves = {}
    for tid, w in placed.items():
        waves.setdefault(w, []).append(tid)
    c["waves"] = [sorted(waves[w], key=lambda x: (-cps.get(x, 0), x)) for w in sorted(waves)]
    c["wave_numbers"] = sorted(waves)
    return order


def tracks_for(c):
    by_phase = {}
    for tid, t in c["tickets"].items():
        if t["state"] in ("dropped",):
            continue
        by_phase.setdefault(t.get("phase") or "adhoc", []).append(tid)
    phases = sorted(by_phase, key=lambda ph: c.get("phase_order", {}).get(ph, 999))
    groups = [[ph] for ph in phases]
    while len(groups) > 3:  # merge the two smallest adjacent groups
        sizes = [sum(len(by_phase[ph]) for ph in g) for g in groups]
        i = min(range(len(groups) - 1), key=lambda k: sizes[k] + sizes[k + 1])
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
    return [{"id": f"T{i + 1}", "phases": g, "tickets": sorted(sum((by_phase[ph] for ph in g), []))}
            for i, g in enumerate(groups)]


def recommend_formation(c):
    n = len([t for t in c["tickets"].values() if t["state"] not in TERMINAL])
    phases = {t.get("phase") for t in c["tickets"].values()}
    if n > 10 or (n > 6 and len(phases) > 1):
        return "managed"
    return "direct"


def write_ticket_packet(tid, t, rt):
    lines = [f"# TICKET {tid} — {t['title']}",
             f"Phase: {t.get('phase') or '-'} · Size: {t.get('size')} · Roadmap model: {t.get('model')} / effort {t.get('effort')} · review: {t.get('review')}",
             f"Deps: {', '.join(t['deps'] + t.get('ext_deps', [])) or 'none'}",
             ""]
    if rt.get("why"):
        lines += ["## Why", rt["why"], ""]
    if rt.get("what"):
        lines += ["## What"] + [f"- {x}" for x in rt["what"]] + [""]
    if rt.get("accept"):
        lines += ["## Done when (acceptance — each needs evidence)"] + [f"{i + 1}. {x}" for i, x in enumerate(rt["accept"])] + [""]
    if rt.get("prompt"):
        lines += ["## Instructions from the roadmap prompt", rt["prompt"], ""]
    if rt.get("modelWhy"):
        lines += ["## Why this model", rt["modelWhy"], ""]
    if (rt.get("notes") or "").strip():
        lines += ["## The user's notes on this ticket", rt["notes"].strip(), ""]
    (L / "tickets" / f"{safe(tid)}.md").write_text("\n".join(lines), encoding="utf-8")


def new_sprint(name, roadmap_rel, revision):
    return {"name": name, "created": now_z(), "roadmap": roadmap_rel, "roadmap_revision": revision,
            "branch": current_branch() if (ROOT / ".git").exists() else None,
            "base_sha": git(["rev-parse", "HEAD"], ROOT, check=False).stdout.strip() or None,
            "tickets": {}, "waves": [], "excluded": [], "phase_order": {}}


def sprint_ticket(rt, internal_deps, ext_deps, adhoc=False):
    t = {"title": rt.get("title", ""), "phase": rt.get("phase"), "size": rt.get("size") or "M",
         "model": rt.get("model") or "sonnet", "effort": rt.get("effort"), "review": bool(rt.get("review")),
         "deps": internal_deps, "ext_deps": ext_deps, "adhoc": adhoc, "gate": is_gate(rt),
         "state": "queued", "attempts": 0, "wave": None, "wt": None, "branch": None, "base": None,
         "merged_sha": None, "verdict": None, "history": [], "accept_n": len(rt.get("accept") or [])}
    t["formation"] = formation(t)
    t["review_plan"] = review_plan(t)
    return t


# ---------------------------------------------------------------- printing

def describe(t):
    f = t["formation"]
    if f["kind"] == "solo":
        form = f"solo({f['model']})"
    else:
        form = f"team({f['lead']}, {f['engineers']} eng)"
    return form


def print_plan(c, budget, conf):
    print(f"SPRINT {c['name']} · branch {c.get('branch')} @ {(c.get('base_sha') or '')[:7]} · roadmap rev {c.get('roadmap_revision')}")
    rec = c.get("formation") or recommend_formation(c)
    print(f"Formation: {rec} · slots {conf['slots']} (build {budget}, review {conf['review_slots']})")
    for w, ids in zip(c["wave_numbers"], c["waves"]):
        live = [i for i in ids if c["tickets"][i]["state"] not in TERMINAL]
        if not live:
            continue
        cells = []
        for tid in live:
            t = c["tickets"][tid]
            deps = f" <- {','.join(t['deps'])}" if t["deps"] else ""
            gate = " [GATE]" if t.get("gate") else ""
            cells.append(f"{tid} {describe(t)}{deps}{gate}")
        used = sum(cost(c["tickets"][i]["formation"]) for i in live)
        print(f"  W{w} ({used}/{budget}): " + " · ".join(cells))
    panels = [tid for tid, t in c["tickets"].items() if t["review_plan"]["kind"] == "panel" and t["state"] not in TERMINAL]
    batch = [tid for tid, t in c["tickets"].items() if t["review_plan"]["kind"] == "batch" and t["state"] not in TERMINAL]
    print(f"Review: panel {','.join(panels) or '-'} · batch {','.join(batch) or '-'} · others single reviewer")
    if c.get("formation") == "managed" or rec == "managed":
        for s in c.get("tracks", tracks_for(c)):
            print(f"  Track {s['id']} ({'+'.join(map(str, s['phases']))}): {', '.join(s['tickets'])}")
    for ex in c.get("excluded", [])[-12:]:
        print(f"  excluded {ex['id']}: {ex['reason']}")
    print("Files: .coniso/sprint.json · .coniso/tickets/<id>.md")


# ---------------------------------------------------------------- stacks

STACK_MARK = "coniso-stack:"

def replace_block(text, name, block):
    begin, end = f"<!-- {STACK_MARK}{name}:begin -->", f"<!-- {STACK_MARK}{name}:end -->"
    wrapped = f"{begin}\n{block.strip()}\n{end}"
    if begin in text and end in text:
        return text[:text.index(begin)] + wrapped + text[text.index(end) + len(end):]
    anchor = "## Stack & pinned versions"
    if anchor in text:
        i = text.index(anchor)
        return text[:i] + wrapped + "\n\n" + text[i:]
    return text.rstrip() + "\n\n" + wrapped + "\n"


def install_stack(name, conf, a, plugin):
    """Copy a stack pack into .coniso/stack, its specialties into .coniso/specialties, and wire config."""
    src = plugin / "stacks" / name if plugin else None
    dst = L / "stack"
    if src is None or not src.is_dir():
        if dst.is_dir() and conf.get("stack") == name:
            print(f"stack {name}: plugin copy not found, keeping the installed .coniso/stack")
            return
        die(f"unknown stack '{name}' (looked in {p(plugin / 'stacks') if plugin else 'the plugin'})")
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        parts = f.relative_to(src).parts
        if f.is_file() and "specialties" not in parts and "__pycache__" not in parts:
            t = dst / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)          # lint-baseline.json is not in the pack, so it survives refreshes
    for f in sorted((src / "specialties").glob("*.md")):
        t = L / "specialties" / f.name
        if t.exists() and STACK_MARK not in t.read_text(encoding="utf-8").splitlines()[0]:
            print(f"stack {name}: keeping your customised .coniso/specialties/{f.name}")
            continue
        shutil.copy2(f, t)
    conf["stack"] = name
    pack = read_json(dst / "stack.json", {}) or {}
    q = (lambda x: f'"{x}"' if " " in x else x)
    fill = (lambda cmd: cmd.replace("{python}", q(Path(conf["python"]).as_posix())).replace("{stack}", q(p(dst))))
    if pack.get("check_cmd"):
        conf["stack_check_cmd"] = fill(pack["check_cmd"])
    if pack.get("setup_cmd") and not conf.get("setup_cmd"):
        conf["setup_cmd"] = fill(pack["setup_cmd"])
    if pack.get("build_cmd") and not conf.get("build_cmd"):
        conf["build_cmd"] = fill(pack["build_cmd"])


def write_stack_brief(name, conf, brief):
    src = L / "stack"
    rules = (src / "rules.md").read_text(encoding="utf-8")
    text = brief.read_text(encoding="utf-8")
    text = replace_block(text, name, rules)
    settings = src / "settings.md"
    if settings.exists():
        head = settings.read_text(encoding="utf-8").splitlines()[0]
        if head not in text:
            perf = (read_json(src / "stack.json", {}) or {}).get("perf_defaults", {})
            body = settings.read_text(encoding="utf-8")
            for k, v in perf.items():
                body = body.replace("{" + k + "}", f"{v:,}")
            end = f"<!-- {STACK_MARK}{name}:end -->"
            i = text.index(end) + len(end)
            text = text[:i] + "\n\n" + body.strip() + "\n" + text[i:]
    brief.write_text(text, encoding="utf-8")


def exclude_path(entry):
    if run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT).returncode != 0:
        return
    ex = (ROOT / git(["rev-parse", "--git-path", "info/exclude"], ROOT).stdout.strip()).resolve()
    ex.parent.mkdir(parents=True, exist_ok=True)
    text = ex.read_text(encoding="utf-8") if ex.exists() else ""
    if entry not in text.split():
        ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + entry + "\n", encoding="utf-8")


# ---------------------------------------------------------------- commands

def cmd_init(a):
    is_git = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT).returncode == 0
    conf = read_json(L / "config.json", {}) or {}
    worktrees = conf.get("worktrees", True) if a.no_worktrees is None else not a.no_worktrees
    if worktrees:
        if not is_git:
            die("not a git repository. Run `git init` and make a first commit, or init with --no-worktrees.")
        if run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT).returncode != 0:
            die("the repository has no commits yet; make an initial commit first.")
    for d in ("tickets", "assignments", "reports", "maps", "specialties", "ref", "wt", "archive"):
        (L / d).mkdir(parents=True, exist_ok=True)
    plugin = SCRIPT.parent.parent if (SCRIPT.parent.parent / "skills" / "coniso").is_dir() else None
    if plugin:
        src = plugin / "skills" / "coniso" / "references"
        if (L / "ref").exists():
            shutil.rmtree(L / "ref")
        shutil.copytree(src, L / "ref")
        if SCRIPT != (L / "coniso.py").resolve():
            shutil.copy2(SCRIPT, L / "coniso.py")
    elif not any((L / "ref").iterdir()):
        print("WARN: run init from the plugin copy once so references get installed into .coniso/ref/")
    if is_git:
        r = git(["rev-parse", "--git-path", "info/exclude"], ROOT)
        ex = (ROOT / r.stdout.strip()).resolve()
        ex.parent.mkdir(parents=True, exist_ok=True)
        text = ex.read_text(encoding="utf-8") if ex.exists() else ""
        if "/.coniso/" not in text.split():
            ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "/.coniso/\n", encoding="utf-8")
    if not a.roadmap and not conf.get("roadmap"):
        found = resolve_roadmap(must=False)
        if found:
            a.roadmap = found.relative_to(ROOT).as_posix()
            print(f"Roadmap found: {a.roadmap}")
    if a.roadmap:
        rp = (ROOT / a.roadmap).resolve()
        if not rp.exists():
            die(f"roadmap not found: {a.roadmap}")
        conf["roadmap"] = rp.relative_to(ROOT).as_posix()
    for key in ("test", "build", "setup"):
        val = getattr(a, key)
        if val is not None:
            conf[f"{key}_cmd"] = val
    if a.link is not None:
        conf["link_dirs"] = [x for x in a.link.split(",") if x]
    if a.copy is not None:
        conf["copy_files"] = [x for x in a.copy.split(",") if x]
    env_cap = os.environ.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")
    default_slots = max(6, int(env_cap) - 2) if env_cap and env_cap.isdigit() else 18
    if a.slots:
        conf["slots"] = a.slots
    conf.setdefault("slots", default_slots)
    conf.setdefault("review_slots", 4)
    conf.setdefault("test_timeout", 1200)
    conf.setdefault("link_dirs", [])
    conf.setdefault("copy_files", [])
    conf["worktrees"] = worktrees
    py = sys.executable or "python3"
    conf["python"] = py
    def q(x):
        return f'"{x}"' if " " in x else x
    conf["cmd"] = f"{q(Path(py).as_posix())} {q(p(L / 'coniso.py'))}"
    conf["version"] = VERSION
    stack = a.stack or conf.get("stack")
    if stack and stack != "none" and not a.stack and not plugin and not (L / "stack").is_dir():
        print(f"WARN: stack {stack} configured but this script has no stack packs; run init from the plugin copy to install it")
        stack = None
    if stack == "none":
        for k in ("stack", "stack_check_cmd"):
            conf.pop(k, None)
        stack = None
    if stack:
        install_stack(stack, conf, a, plugin)
    write_json(L / "config.json", conf)
    (L / "VERSION").write_text(VERSION, encoding="utf-8")
    brief = L / "BRIEF.md"
    if not brief.exists():
        title, preamble = ROOT.name, "<none — add the project's hard rules here>"
        if conf.get("roadmap"):
            _, _, data = load_roadmap(ROOT / conf["roadmap"])
            title = data.get("meta", {}).get("title") or title
            preamble = (data.get("meta", {}).get("preamble") or preamble).strip()
        tmpl = (L / "ref" / "brief-template.md")
        body = tmpl.read_text(encoding="utf-8") if tmpl.exists() else "# CONISO BRIEF — {title}\n\n{preamble}\n"
        body = (body.replace("{title}", title).replace("{date}", now_z()[:10])
                .replace("{cmd}", conf["cmd"]).replace("{root}", p(ROOT)).replace("{preamble}", preamble)
                .replace("{build}", conf.get("build_cmd") or "TBD").replace("{test}", conf.get("test_cmd") or "TBD")
                .replace("{setup}", conf.get("setup_cmd") or "none"))
        brief.write_text(body, encoding="utf-8")
    if stack:
        write_stack_brief(stack, conf, brief)
    print(f"CONISO {VERSION} ready in {p(L)}")
    print(f"Run as: {conf['cmd']}")
    print(f"Mode: {'worktree per ticket' if worktrees else 'in-place (no worktrees)'} · slots {conf['slots']} · roadmap {conf.get('roadmap') or 'none'}")
    print(f"build: {conf.get('build_cmd') or 'unset'} · test: {conf.get('test_cmd') or 'unset'} · setup: {conf.get('setup_cmd') or 'unset'}")
    if stack:
        print(f"Stack: {stack} · checks after build/test: {conf.get('stack_check_cmd') or 'none'}")
    print("Brief: .coniso/BRIEF.md (fill the TBD sections before the first schedule)")
    print_permissions(read_json(L / "config.json", conf) or conf)


SPAWN_DEPTH = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"
AGENT_TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"


def local_settings():
    try:
        return read_json(ROOT / ".claude" / "settings.local.json", {}) or {}
    except ValueError:
        return {}


def env_problems():
    """Environment values that would degrade Coniso, as {var: fixed value}. Unset means Claude Code defaults, which are fine."""
    fixes = {}
    depth = os.environ.get(SPAWN_DEPTH)
    if depth and depth.isdigit() and int(depth) < 3:
        fixes[SPAWN_DEPTH] = "3"
    if os.environ.get(AGENT_TEAMS) == "1":
        fixes[AGENT_TEAMS] = "0"
    return fixes


SETTINGS_FILE = ".claude/settings.local.json"


def wanted_rules(conf):
    return [f"Bash({conf['cmd']}:*)"] + [f"Bash({conf[k]}:*)" for k in ("build_cmd", "test_cmd") if conf.get(k)]


def permission_state(conf):
    """(missing allow rules, pending env fixes, whether Coniso set acceptEdits) against the local settings file."""
    data = local_settings()
    allow = (data.get("permissions") or {}).get("allow") or []
    env = data.get("env") or {}
    missing = [r for r in wanted_rules(conf) if r not in allow]
    pending = {k: v for k, v in env_problems().items() if env.get(k) != v}
    return missing, pending, (data.get("permissions") or {}).get("defaultMode") == "acceptEdits"


def print_permissions(conf):
    """Init and `permissions` without flags: report only. Coniso never edits Claude settings without an explicit opt-in."""
    missing, pending, accept = permission_state(conf)
    granted = (conf.get("permissions") or {})
    if not missing and not pending:
        print(f"Permissions: Coniso commands allowed in {SETTINGS_FILE}" + (" · default mode acceptEdits" if accept else ""))
        return
    if granted.get("declined"):
        print("Permissions: not configured (user declined); agents may stop on permission prompts.")
        return
    print(f"PERMISSIONS NOT CONFIGURED. Coniso changes no Claude settings unless the user opts in. Ask the user; on yes run "
          f"`permissions --write`, on no run `permissions --decline`. It would add to {SETTINGS_FILE} (local, git-excluded):")
    for r in missing:
        print(f"  allow {r}")
    for k, v in pending.items():
        print(f"  env {k}={v}  (current value breaks nested subagents)")


def cmd_permissions(a):
    """Show, grant (--write [--accept-edits]), decline or remove Coniso's entries in the local Claude settings."""
    conf = cfg()
    if a.decline:
        conf["permissions"] = {"declined": True}
        write_json(L / "config.json", conf)
        print("Permissions: declined; Coniso won't ask again. Grant later with `permissions --write`.")
        return
    if not (a.write or a.accept_edits or a.remove):
        print_permissions(conf)
        return
    path = ROOT / ".claude" / "settings.local.json"
    try:
        data = read_json(path, {}) or {}
    except ValueError:
        die(f"{p(path)} is not valid JSON; fix or remove it first")
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    env = data.get("env") or {}
    record = conf.get("permissions") or {}
    record.pop("declined", None)
    if a.remove:
        rules = record.get("rules", [])
        perms["allow"] = [r for r in allow if r not in rules]
        for k, v in (record.get("env") or {}).items():
            if env.get(k) == v:
                env.pop(k)
        if record.get("accept_edits") and perms.get("defaultMode") == "acceptEdits":
            perms.pop("defaultMode")
        data["env"] = env
        if not env:
            data.pop("env")
        if not perms["allow"]:
            perms.pop("allow")
        if not perms:
            data.pop("permissions")
        write_json(path, data)
        conf["permissions"] = {}
        write_json(L / "config.json", conf)
        print(f"Permissions: removed Coniso's entries from {SETTINGS_FILE} ({len(rules)} rule(s)"
              + (", env overrides" if record.get("env") else "") + (", acceptEdits" if record.get("accept_edits") else "") + ")")
        return
    added = [r for r in wanted_rules(conf) if r not in allow]
    allow.extend(added)
    fixed = {k: v for k, v in env_problems().items() if env.get(k) != v}
    env.update(fixed)
    if env:
        data["env"] = env
    if a.accept_edits and perms.get("defaultMode") != "acceptEdits":
        perms["defaultMode"] = "acceptEdits"
        record["accept_edits"] = True
    record["rules"] = sorted(set(record.get("rules", [])) | set(added))
    record["env"] = {**(record.get("env") or {}), **fixed}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    exclude_path("/.claude/settings.local.json")
    conf["permissions"] = record
    write_json(L / "config.json", conf)
    parts = [f"{len(added)} allow rule(s)"] + (["env " + ", ".join(f"{k}={v}" for k, v in fixed.items())] if fixed else []) \
        + (["default mode acceptEdits"] if a.accept_edits else [])
    print(f"Permissions: wrote {SETTINGS_FILE} ({'; '.join(parts)}). Local, never committed. Undo with `permissions --remove`.")
    if fixed:
        print("  Env overrides apply to sessions started after this one; this session runs in relay mode if nesting is off.")


def cmd_preflight(a):
    conf = cfg()
    out = []
    ok = True

    def line(level, msg):
        nonlocal ok
        if level == "FAIL":
            ok = False
        out.append(f"{level:4} {msg}")

    if not (L / "VERSION").exists():
        line("WARN", "no VERSION file; re-run init from the plugin")
    elif (L / "VERSION").read_text().strip() != VERSION:
        line("WARN", f".coniso is v{(L / 'VERSION').read_text().strip()}, script v{VERSION}; re-run init from the plugin")
    if conf.get("worktrees", True):
        if run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT).returncode != 0:
            line("FAIL", "no git commits")
        else:
            dirty = main_dirty()
            if dirty:
                line("FAIL", f"main checkout has {len(dirty)} uncommitted change(s), e.g. {dirty[0][3:]}; commit or stash before scheduling")
            else:
                line("OK", f"main checkout clean on {current_branch()}")
            if current_branch() == "HEAD":
                line("WARN", "detached HEAD; check out a branch so merges land somewhere named")
    if conf.get("roadmap"):
        try:
            _, _, data = load_roadmap(ROOT / conf["roadmap"])
            errs = validate_roadmap(data)
            line("OK" if not errs else "WARN", f"roadmap {conf['roadmap']} rev {data.get('meta', {}).get('revision')}"
                 + (f" — {len(errs)} validation issue(s): {errs[0]}" if errs else ""))
        except SystemExit:
            line("FAIL", f"roadmap unreadable: {conf['roadmap']}")
    else:
        line("INFO", "no roadmap configured (adhoc tickets only)")
    if not conf.get("test_cmd"):
        if conf.get("stack_check_cmd"):
            line("INFO", f"no unit-test command; {conf.get('stack')} stack checks still run in every test and merge")
        else:
            line("WARN", "no test command; reviewers and merges cannot run tests (init --test \"...\")")
    depth = os.environ.get(SPAWN_DEPTH)
    d = int(depth) if depth and depth.isdigit() else None
    if d is None:
        line("OK", "spawn depth: Claude Code default; leaders spawn directly while they have the Agent tool, and fall back to relay mode if not")
    elif d >= 3:
        line("OK", f"spawn depth {d}: full hierarchy (Manager > Lead > Engineer)")
    elif d == 2:
        line("WARN", "spawn depth 2: teams spawn directly; a Manager's leads relay their engineers through the main session")
    else:
        line("WARN", f"spawn depth {d}: nesting off — RELAY MODE: leaders write spawn requests, the main session spawns every agent")
    if os.environ.get(AGENT_TEAMS) == "1":
        line("WARN", "agent teams enabled: named subagents may launch as teammates, which cannot spawn")
    fixes = env_problems()
    if fixes:
        local = local_settings()
        pending = {k: v for k, v in fixes.items() if (local.get("env") or {}).get(k) != v}
        line("INFO", f"the env fix is in {SETTINGS_FILE}; it applies from the next session"
             if not pending else "env fix needed for nested subagents: see `permissions` (opt-in)")
    missing, _, _ = permission_state(conf)
    if not missing:
        line("OK", f"Claude settings: Coniso commands allowed ({SETTINGS_FILE})")
    elif (conf.get("permissions") or {}).get("declined"):
        line("INFO", "Coniso commands not allowed (user declined); agents may stop on permission prompts")
    else:
        line("WARN", "PERMISSIONS NOT CONFIGURED: agents may stop on prompts. Ask the user, then `permissions --write` or `permissions --decline`")
    c = load_sprint(required=False)
    if c:
        live = [tid for tid, t in c["tickets"].items() if t["state"] in ACTIVE]
        line("INFO", f"active sprint {c['name']}: {len(live)} ticket(s) in flight; use status / continue")
    stale = [d.name for d in (L / "wt").iterdir() if d.is_dir()] if (L / "wt").exists() else []
    if stale and not c:
        line("WARN", f"leftover worktrees with no sprint: {', '.join(stale)} (wt remove <id> --force)")
    print("\n".join(out))
    sys.exit(0 if ok else 1)


def select_from_roadmap(a, conf, c):
    if not conf.get("roadmap"):
        die("no roadmap configured; use `init --roadmap <file>` or `adhoc`")
    rp = ROOT / conf["roadmap"]
    _, _, data = load_roadmap(rp)
    rts = data.get("tickets", [])
    status = {t["id"]: t.get("status") for t in rts}
    by_id = {t["id"]: t for t in rts}
    phase_order = {ph["id"]: i for i, ph in enumerate(data.get("phases", []))}
    allowed_status = {"todo", "progress"} if a.include_progress else {"todo"}
    wanted_ids = set(x.strip() for x in a.tickets.split(",")) if a.tickets else None
    wanted_phases = set(x.strip() for x in a.phase.split(",")) if a.phase else None
    if wanted_ids:
        missing = wanted_ids - set(by_id)
        if missing:
            die(f"not in roadmap: {', '.join(sorted(missing))}")
    cands = []
    excluded = []
    for t in sorted(rts, key=lambda t: (phase_order.get(t.get("phase"), 999), t["id"])):
        if c and t["id"] in c["tickets"]:
            continue
        if wanted_ids is not None and t["id"] not in wanted_ids:
            continue
        if wanted_phases is not None and t.get("phase") not in wanted_phases:
            continue
        if t.get("status") not in allowed_status:
            if wanted_ids is not None:
                excluded.append({"id": t["id"], "reason": f"status {t.get('status')}"})
            continue
        cands.append(t["id"])
    in_sprint = set(c["tickets"]) if c else set()
    ok_ext = {"done"} if a.strict else {"done", "review"}
    chosen = []
    changed = True
    pool = list(cands)
    while changed:
        changed = False
        chosen = []
        for tid in pool:
            t = by_id[tid]
            reason = None
            for d in t.get("deps", []):
                if d in pool or d in in_sprint:
                    if d in by_id and is_gate(by_id[d]):
                        reason = f"waits on gate {d} (your go/no-go)"
                    continue
                ds = status.get(d)
                if ds is None:
                    reason = f"dep {d} missing from roadmap"
                elif ds not in ok_ext or (ds != "done" and is_gate(by_id[d])):
                    reason = f"waits on {d} ({ds})"
                if reason:
                    break
            if reason:
                excluded.append({"id": tid, "reason": reason})
                changed = True
            else:
                chosen.append(tid)
        pool = chosen
    if a.limit and len(chosen) > a.limit:
        keep = []
        for tid in chosen:  # chosen is in phase/id order; keep deps closed
            if len(keep) >= a.limit:
                excluded.append({"id": tid, "reason": f"over --limit {a.limit}"})
                continue
            deps_in = [d for d in by_id[tid].get("deps", []) if d in chosen]
            if all(d in keep for d in deps_in):
                keep.append(tid)
            else:
                excluded.append({"id": tid, "reason": "dep cut by --limit"})
        chosen = keep
    return data, by_id, chosen, excluded, phase_order


def cmd_schedule(a):
    conf = cfg()
    c = load_sprint(required=False)
    if c and not a.add:
        live = [tid for tid, t in c["tickets"].items() if t["state"] not in TERMINAL]
        if live and not a.replace:
            die(f"sprint {c['name']} still has {len(live)} open ticket(s). Use --add to extend it, "
                f"--replace to discard it, or `close` it first.")
        if a.replace:
            cmd_close(argparse.Namespace(force=True, quiet=True))
            c = None
        elif not live:
            cmd_close(argparse.Namespace(force=False, quiet=True))
            c = None
    if conf.get("worktrees", True) and main_dirty():
        die("main checkout has uncommitted changes; commit or stash them first (Coniso branches from HEAD).")
    data, by_id, chosen, excluded, phase_order = select_from_roadmap(a, conf, c)
    if not chosen:
        for ex in excluded[:15]:
            print(f"  excluded {ex['id']}: {ex['reason']}")
        die("no ready tickets match", code=2)
    revision = data.get("meta", {}).get("revision")
    if c is None:
        name = a.name or "s" + dt.datetime.now().strftime("%Y%m%d-%H%M")
        c = new_sprint(name, conf["roadmap"], revision)
    c["phase_order"].update(phase_order)
    pool = set(chosen) | set(c["tickets"])
    for tid in chosen:
        rt = by_id[tid]
        internal = [d for d in rt.get("deps", []) if d in pool]
        ext = [d for d in rt.get("deps", []) if d not in pool]
        c["tickets"][tid] = sprint_ticket(rt, internal, ext)
        write_ticket_packet(tid, c["tickets"][tid], rt)
    c["excluded"] = [e for e in c.get("excluded", []) if e["id"] not in c["tickets"]] + excluded
    finish_planning(a, c, conf)


def finish_planning(a, c, conf):
    fixed = {tid: t["wave"] for tid, t in c["tickets"].items() if t["state"] != "queued" and t["wave"] is not None}
    to_place = [tid for tid, t in c["tickets"].items() if t["state"] == "queued"]
    start = max([w for tid, w in fixed.items() if c["tickets"][tid]["state"] in ACTIVE] or [0]) + 1
    c["formation"] = getattr(a, "formation", None) or c.get("formation_override") or recommend_formation(c)
    if getattr(a, "formation", None):
        c["formation_override"] = a.formation
    managers = len(tracks_for(c)) if c["formation"] == "managed" else 0
    budget = max(3, conf["slots"] - conf["review_slots"] - managers)
    if getattr(a, "slots", None):
        budget = max(3, a.slots - conf["review_slots"] - managers)
    with Lock():
        schedule(c, to_place, fixed, start, budget)
        c["tracks"] = tracks_for(c) if c["formation"] == "managed" else []
        c["budget"] = budget
        save_sprint(c)
    print_plan(c, budget, conf)


def cmd_adhoc(a):
    conf = cfg()
    c = load_sprint(required=False)
    if c is None:
        c = new_sprint(a.name or "s" + dt.datetime.now().strftime("%Y%m%d-%H%M"), conf.get("roadmap"), None)
    tid = a.id
    if tid in c["tickets"]:
        die(f"{tid} already in sprint")
    prompt = a.prompt or (Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else "")
    rt = {"id": tid, "title": a.title, "phase": a.phase or "adhoc", "size": a.size, "model": a.model,
          "effort": a.effort if a.model != "haiku" else None, "review": a.review, "deps": [],
          "why": a.why or "", "what": [], "accept": a.accept or [], "prompt": prompt, "modelWhy": "ad-hoc ticket"}
    deps = [d.strip() for d in (a.deps or "").split(",") if d.strip()]
    internal = [d for d in deps if d in c["tickets"]]
    c["tickets"][tid] = sprint_ticket(rt, internal, [d for d in deps if d not in internal], adhoc=True)
    write_ticket_packet(tid, c["tickets"][tid], rt)
    finish_planning(a, c, conf)


def cmd_status(a):
    c = load_sprint()
    if a.json:
        print(json.dumps(c if not a.id else c["tickets"].get(a.id), indent=1))
        return
    counts = {}
    for t in c["tickets"].values():
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    print(f"SPRINT {c['name']} · {c.get('formation')} · " + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    for w, ids in zip(c.get("wave_numbers", []), c.get("waves", [])):
        for tid in ids:
            t = c["tickets"][tid]
            if a.open and t["state"] in TERMINAL:
                continue
            last = (t["history"][-1]["note"] if t["history"] else "")[:70]
            extra = f" sha {t['merged_sha'][:7]}" if t.get("merged_sha") else ""
            att = f" try{t['attempts'] + 1}" if t["attempts"] else ""
            print(f"  W{w} {tid:10} {t['state']:9} {describe(t)}{att}{extra}" + (f" — {last}" if last else ""))
    ready = next_ready(c)
    if ready:
        print(f"Ready to start now: {', '.join(ready)}")


def next_ready(c):
    out = []
    for tid, t in c["tickets"].items():
        if t["state"] != "queued":
            continue
        if all(c["tickets"].get(d, {}).get("state") == "merged" for d in t["deps"]):
            if not any(c["tickets"].get(d, {}).get("gate") for d in t["deps"]):
                out.append(tid)
    return sorted(out, key=lambda x: (c["tickets"][x].get("wave") or 0, x))


def cmd_set(a):
    if a.state is None:             # token/note record only: the ticket keeps its state
        if not (a.tokens or a.note):
            die("set <id> needs a state, or --tokens/--note to record without changing state")
        with Lock():
            c = load_sprint()
            t = ticket(c, a.id)
            note(t, t["state"], a.note or "", a.tokens, a.role)
            save_sprint(c)
        print(f"{a.id} {t['state']}" + (f" · {a.tokens} tokens ({a.role or '?'})" if a.tokens else ""))
        return
    if a.state not in STATES or a.state == "merged":
        die(f"state must be one of {', '.join(s for s in STATES if s != 'merged')} (merge sets merged)")
    with Lock():
        c = load_sprint()
        t = ticket(c, a.id)
        if t["state"] in ("failed", "conflict") and a.state == "building":
            t["attempts"] += 1
        if a.verdict:
            t["verdict"] = a.verdict
        if a.model:
            prev = t["formation"].get("lead") or t["formation"].get("model")
            if t["formation"]["kind"] == "team":
                t["formation"]["lead"] = a.model
                note(t, t["state"], f"escalated {prev} -> {a.model}")
            else:                       # playbook: on escalation a solo ticket becomes a team
                t["formation"] = team_formation(t.get("size") or "M", a.model)
                note(t, t["state"], f"escalated {prev} -> {a.model}; solo -> team({t['formation']['engineers']} eng)")
        t["state"] = a.state
        note(t, a.state, a.note or "", a.tokens, a.role)
        save_sprint(c)
    print(f"{a.id} -> {a.state}" + (f" (attempt {t['attempts'] + 1})" if t["attempts"] else ""))


def link_dir(src, dst):
    if dst.exists() or not src.exists():
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
        return "symlink"
    except (OSError, NotImplementedError):
        if os.name == "nt" and src.is_dir():
            try:
                import _winapi
                _winapi.CreateJunction(str(src), str(dst))
                return "junction"
            except Exception:
                pass
        return "failed"


def cmd_wt(a):
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    if not conf.get("worktrees", True):
        print(p(ROOT))
        return
    path = L / "wt" / safe(a.id)
    branch = f"coniso/{safe(a.id)}"
    if a.action == "path":
        print(p(t["wt"]) if t.get("wt") else "none")
        return
    if a.action == "remove":
        with Lock():
            if path.exists():
                git(["worktree", "remove", "--force", str(path)], ROOT, check=not a.force)
                if path.exists() and a.force:
                    shutil.rmtree(path, ignore_errors=True)
                git(["worktree", "prune"], ROOT, check=False)
            git(["branch", "-D", branch], ROOT, check=False)
            c = load_sprint()
            t = ticket(c, a.id)
            t["wt"] = None
            t["branch"] = None
            save_sprint(c)
        print(f"removed worktree and branch for {a.id}")
        return
    # add
    if path.exists() and t.get("wt"):
        print(p(path))
        return
    if t["state"] in TERMINAL:
        die(f"{a.id} is {t['state']}")
    with Lock():
        exists = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], ROOT, check=False).returncode == 0
        if exists:
            die(f"branch {branch} already exists (stale?). Run `wt remove {a.id} --force` first.")
        base = git(["rev-parse", "HEAD"], ROOT).stdout.strip()
        git(["worktree", "add", "-b", branch, str(path), base], ROOT)
        c = load_sprint()
        t = ticket(c, a.id)
        t.update({"wt": p(path), "branch": branch, "base": base})
        if t["state"] == "queued":
            t["state"] = "building"
            note(t, "building", "worktree created")
        save_sprint(c)
    msgs = []
    for d in conf.get("link_dirs", []):
        msgs.append(f"{d}:{link_dir(ROOT / d, path / d)}")
    for f in conf.get("copy_files", []):
        if (ROOT / f).exists() and not (path / f).exists():
            (path / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, path / f)
            msgs.append(f"{f}:copied")
    if conf.get("setup_cmd"):
        r = run(conf["setup_cmd"], cwd=path, shell=True, timeout=conf.get("test_timeout", 1200))
        log = L / "reports" / f"{safe(a.id)}.setup.log"
        log.write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        msgs.append(f"setup exit {r.returncode} (log {p(log)})")
    print(p(path))
    if msgs:
        print("  " + " · ".join(msgs))


def snapshot(t, conf):
    wt = Path(t["wt"])
    git(["add", "-A"], wt)
    for d in conf.get("link_dirs", []) + conf.get("copy_files", []):
        git(["reset", "-q", "--", d], wt, check=False)
    staged = git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0
    if staged:
        git(git_identity_args(wt) + ["commit", "-q", "--no-verify", "-m", f"{t.get('title', '')[:60]} (coniso snapshot)"], wt)
    return staged


def pathspec_excludes(conf):
    return ["--", "."] + [f":(exclude){d}" for d in conf.get("link_dirs", []) + conf.get("copy_files", [])]


def cmd_diff(a):
    conf = cfg()
    out = L / "reports"
    if a.range:
        r = git(["diff", a.range], ROOT)
        stat = git(["diff", "--stat", a.range], ROOT).stdout
        f = out / f"range-{safe(a.range)}.diff"
    else:
        c = load_sprint()
        t = ticket(c, a.id)
        if not conf.get("worktrees", True):
            die("in-place mode: review the files listed in the unit's report instead of a diff")
        if not t.get("wt"):
            die(f"{a.id} has no worktree")
        wt = Path(t["wt"])
        git(["add", "-N", "--"] + pathspec_excludes(conf)[2:] + ["."], wt, check=False)
        r = git(["diff", t["base"]] + pathspec_excludes(conf), wt)
        stat = git(["diff", "--stat", t["base"]] + pathspec_excludes(conf), wt).stdout
        f = out / f"{safe(a.id)}.diff"
    f.write_text(r.stdout, encoding="utf-8")
    lines = r.stdout.count("\n")
    s = stat.strip().splitlines()
    print(f"DIFF {a.id or a.range}: {lines} lines -> {p(f)}")
    print("\n".join(s[-25:]) if s else "(no changes)")


def run_checks(target, conf, log):
    cmds = [x for x in (conf.get("build_cmd"), conf.get("test_cmd"), conf.get("stack_check_cmd")) if x]
    if not cmds:
        return None, "no build/test command configured"
    text = []
    code = 0
    for cmd in cmds:
        r = run(cmd, cwd=target, shell=True, timeout=conf.get("test_timeout", 1200))
        text.append(f"$ {cmd}\n{r.stdout or ''}{r.stderr or ''}\n[exit {r.returncode}]\n")
        code = r.returncode
        if code != 0:
            break
    log.write_text("\n".join(text), encoding="utf-8")
    return code, "\n".join(text)


def cmd_test(a):
    conf = cfg()
    if a.id == "main":
        target = ROOT
    else:
        c = load_sprint()
        t = ticket(c, a.id)
        target = Path(t["wt"]) if t.get("wt") else ROOT
    log = L / "reports" / f"{safe(a.id)}.test.log"
    code, text = run_checks(target, conf, log)
    if code is None:
        print(f"TEST {a.id}: SKIPPED — {text}")
        sys.exit(2)
    tail = text.strip().splitlines()[-(5 if code == 0 else 25):]
    print(f"TEST {a.id}: {'PASS' if code == 0 else 'FAIL'} (exit {code}) · log {p(log)}")
    print("\n".join(tail))
    sys.exit(0 if code == 0 else 1)


def cmd_sync(a):
    """Bring the main branch into a ticket's worktree (conflict resolution or pre-review refresh)."""
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    if not t.get("wt"):
        die(f"{a.id} has no worktree")
    wt = Path(t["wt"])
    main_ref = c.get("branch") or "HEAD"
    if a.cont:
        r = git(["diff", "--name-only", "--diff-filter=U"], wt, check=False)
        files = [x for x in r.stdout.splitlines() if x]
        marked = []
        for f in set(files) | set(git(["diff", "--name-only"], wt, check=False).stdout.split()):
            fp = wt / f
            if fp.is_file():
                try:
                    if re.search(r"^(<{7}|>{7}) ", fp.read_text(encoding="utf-8", errors="ignore"), re.M):
                        marked.append(f)
                except OSError:
                    pass
        if marked:
            die(f"conflict markers remain in: {', '.join(marked)}")
        git(["add", "-A"], wt)
        for d in conf.get("link_dirs", []) + conf.get("copy_files", []):
            git(["reset", "-q", "--", d], wt, check=False)
        git(git_identity_args(wt) + ["commit", "-q", "--no-verify", "--no-edit"], wt, check=False)
    else:
        snapshot(t, conf)
        head = git(["rev-parse", main_ref], ROOT).stdout.strip()
        r = git(git_identity_args(wt) + ["merge", "--no-edit", "--no-verify", head], wt, check=False)
        if r.returncode != 0:
            files = git(["diff", "--name-only", "--diff-filter=U"], wt, check=False).stdout.split()
            print(f"SYNC {a.id}: CONFLICTS in {', '.join(files) or '(see git output)'}")
            print(f"Resolve them inside {p(wt)}, remove all markers, then run: sync {a.id} --continue")
            sys.exit(3)
    head = git(["rev-parse", main_ref], ROOT).stdout.strip()
    with Lock():
        c = load_sprint()
        t = ticket(c, a.id)
        t["base"] = head
        if t["state"] == "conflict":
            t["state"] = "built"
        note(t, t["state"], f"synced with {main_ref} @ {head[:7]}")
        save_sprint(c)
    print(f"SYNC {a.id}: up to date with {main_ref} @ {head[:7]}; diff base moved there")


def cmd_merge(a):
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    if t["state"] != "passed" and not a.force:
        die(f"{a.id} is {t['state']}; only passed tickets merge (use --force to override, and say why in the report)")

    def mark(state, text, sha=None):
        with Lock():
            c2 = load_sprint()
            t2 = ticket(c2, a.id)
            t2["state"] = state
            if sha:
                t2["merged_sha"] = sha
            note(t2, state, text)
            save_sprint(c2)

    if not conf.get("worktrees", True):
        mark("merged", "in-place mode: accepted")
        print(f"MERGED {a.id} (in-place mode, nothing to merge)")
        return
    if not t.get("wt"):
        die(f"{a.id} has no worktree")
    dirty = main_dirty()
    if dirty:
        die(f"main checkout has uncommitted changes ({dirty[0][3:]} …); commit or stash them, Coniso will not touch them")
    if c.get("branch") and current_branch() != c["branch"]:
        die(f"main checkout is on {current_branch()} but the sprint merges into {c['branch']}")
    snapshot(t, conf)
    ahead = git(["rev-list", "--count", f"{t['base']}..{t['branch']}"], ROOT, check=False).stdout.strip()
    if ahead in ("", "0"):
        mark("failed", "no changes produced")
        die(f"{a.id} produced no changes", code=4)
    with Lock():
        r = git(["merge", "--squash", t["branch"]], ROOT, check=False)
        conflicts = git(["diff", "--name-only", "--diff-filter=U"], ROOT, check=False).stdout.split()
        if r.returncode != 0 or conflicts:
            git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
    if r.returncode != 0 or conflicts:
        mark("conflict", f"squash conflicts: {', '.join(conflicts)}")
        print(f"CONFLICT {a.id}: {', '.join(conflicts) or (r.stderr or r.stdout).strip()[:200]}")
        print(f"Send its lead or engineer to run `sync {a.id}`, resolve inside the worktree, then `sync {a.id} --continue`; re-review the resolution.")
        sys.exit(3)
    if git(["diff", "--cached", "--quiet"], ROOT, check=False).returncode == 0:
        git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
        mark("failed", "squash produced no net changes")
        die(f"{a.id}: no net changes after squash", code=4)
    if not a.no_test:
        log = L / "reports" / f"{safe(a.id)}.merge-test.log"
        code, text = run_checks(ROOT, conf, log)
        if code not in (None, 0):
            git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
            mark("failed", f"integration checks failed on {c.get('branch')} after squash; log {p(log)}")
            print(f"INTEGRATION FAIL {a.id} (exit {code}) · log {p(log)} · main reset, nothing committed")
            print("\n".join(text.strip().splitlines()[-20:]))
            sys.exit(4)
    msg = f"{a.id}: {t['title']}" if not t.get("adhoc") else f"{t['title']} ({a.id})"
    r = git(["commit", "-q", "-m", msg, "-m", f"Coniso sprint {c['name']}"], ROOT, check=False)
    if r.returncode != 0:
        git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
        mark("failed", f"commit refused: {(r.stderr or r.stdout).strip()[:200]}")
        die(f"commit refused (hook?): {(r.stderr or r.stdout).strip()[:300]}", code=5)
    sha = git(["rev-parse", "HEAD"], ROOT).stdout.strip()
    stat = git(["show", "--stat", "--format=", "HEAD"], ROOT).stdout.strip().splitlines()
    mark("merged", f"merged as {sha[:7]}", sha)
    if not a.keep:
        cmd_wt(argparse.Namespace(action="remove", id=a.id, force=True))
    print(f"MERGED {a.id} as {sha[:7]} on {c.get('branch')} · {stat[-1].strip() if stat else ''}")
    ready = next_ready(load_sprint())
    if ready:
        print(f"Now ready: {', '.join(ready)}")


def cmd_ledger(a):
    path = L / "ledger.jsonl"
    if a.action == "stats":
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
        print(ledger_stats(rows) or "ledger empty")
        return
    if not a.unit or not a.kind:
        die("ledger add needs --unit and --kind")
    row = {"ts": now_z(), "unit": a.unit, "leader": a.leader, "kind": a.kind, "raised": a.raised,
           "accepted": a.accepted, "overruled": a.overruled, "reasons": a.reason or []}
    if a.accepted + a.overruled > a.raised:
        die("accepted + overruled cannot exceed raised")
    with Lock():
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"ledger: {a.leader or '?'} {a.unit} {a.kind} raised {a.raised} accepted {a.accepted} overruled {a.overruled}")


# ---------------------------------------------------------------- plan validation (rules D3, D5, D6)

PLAN_FIELDS = ("WORKDIR", "OBJECTIVE", "OWNS", "DONE WHEN", "RETURN")
GLOB_CHARS = "*?["


def parse_plan(text):
    """Rows of the first markdown table whose header starts with 'Unit', as dicts keyed by short column names."""
    header, rows = None, []
    for ln in text.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            if header and rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            if cells and cells[0].lower() == "unit":
                header = []
                for c in cells:
                    low = c.lower()
                    key = next((k for k in ("unit", "role", "model", "step", "owns", "needs", "done") if low.startswith(k)), low)
                    header.append(key)
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells)))
    return header, rows


def owned_patterns(cell):
    out = []
    for part in re.split(r"[,;·]|\s+and\s+", cell.replace("`", "")):
        x = part.strip().replace("\\", "/")
        if not x or x.lower() in ("none", "-", "n/a", "nothing"):
            continue
        x = re.sub(r"^\./", "", x)
        if x.endswith("/"):
            x += "**"
        out.append(x)
    return out


def patterns_overlap(a, b):
    import fnmatch
    ta, tb = a.lower().startswith("ticket "), b.lower().startswith("ticket ")
    if ta or tb:                      # a whole-ticket scope only collides with the same ticket
        return ta and tb and a.split()[1:2] == b.split()[1:2]
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True

    def literal(x):
        i = min([x.index(ch) for ch in GLOB_CHARS if ch in x] or [len(x)])
        return x[:i]
    la, lb = literal(a), literal(b)
    wild_a, wild_b = la != a, lb != b
    if wild_a and lb.startswith(la):
        return True
    if wild_b and la.startswith(lb):
        return True
    return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def is_abs_path(v):
    v = v.strip().strip("`")
    return bool(re.match(r"^([A-Za-z]:[\\/]|/|\\\\)", v))


def read_fields(path):
    fields = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z][A-Z ]+):\s*(.*)$", ln)
        if m and m.group(1).strip() in PLAN_FIELDS and m.group(1).strip() not in fields:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def validate_plan(plan_path, slots=None, check_files=True):
    """Deterministic plan checks. Returns (findings, summary). Each finding: (rule, unit, text)."""
    text = plan_path.read_text(encoding="utf-8")
    header, rows = parse_plan(text)
    findings = []
    if not header:
        return [("PLAN", "-", "no delegation table (header must start with | Unit |)")], "0 units"
    for col in ("unit", "model", "step", "owns"):
        if col not in header:
            findings.append(("PLAN", "-", f"table has no '{col}' column"))
    if findings:
        return findings, f"{len(rows)} units"
    steps = {}
    for r in rows:
        unit = r["unit"].strip("` ")
        if not unit:
            findings.append(("PLAN", "?", "row without a unit id"))
            continue
        if not re.fullmatch(r"\d+", r.get("step", "").strip()):
            findings.append(("PLAN", unit, f"step must be a whole number, got '{r.get('step', '')}'"))
            continue
        steps.setdefault(int(r["step"]), []).append((unit, owned_patterns(r.get("owns", ""))))
        if r.get("model", "").strip("` ").lower() not in ("haiku", "sonnet", "opus"):
            findings.append(("PLAN", unit, f"model must be haiku, sonnet or opus, got '{r.get('model', '')}'"))
    # D5: every unit has a complete assignment file (read first: D3 needs each unit's WORKDIR)
    workdirs = {}
    if check_files:
        for step, units in sorted(steps.items()):
            for unit, _ in units:
                f = L / "assignments" / f"{unit}.md"
                if not f.exists():
                    findings.append(("D5", unit, f"missing {p(f)}"))
                    continue
                fields = read_fields(f)
                missing = [k for k in PLAN_FIELDS if not fields.get(k)]
                if missing:
                    findings.append(("D5", unit, f"assignment lacks {', '.join(missing)}"))
                wd = fields.get("WORKDIR", "").strip().strip("`")
                if wd and not is_abs_path(wd):
                    findings.append(("D5", unit, f"WORKDIR is not absolute: {wd[:60]}"))
                elif wd:
                    workdirs[unit] = wd.replace("\\", "/").rstrip("/").lower()
    # D3: one owner per file among units that run at the same time in the same WORKDIR.
    # Units in different worktrees cannot collide; `ticket <id>` scopes only collide with the same ticket.
    for step, units in sorted(steps.items()):
        for i in range(len(units)):
            for j in range(i + 1, len(units)):
                (ua, pa), (ub, pb) = units[i], units[j]
                wa, wb = workdirs.get(ua), workdirs.get(ub)
                if wa and wb and wa != wb:
                    continue
                for x in pa:
                    hit = next((y for y in pb if patterns_overlap(x, y)), None)
                    if hit:
                        findings.append(("D3", f"{ua}/{ub}", f"step {step}: both own '{x}' / '{hit}'"))
                        break
    # D6: concurrency within the slot limit
    peak = max([len(u) for u in steps.values()] or [0])
    m = re.search(r"Slots:\s*(\d+)\s*/\s*(\d+)", text)
    allowed = slots if slots else (int(m.group(2)) if m else None)
    if allowed is None:
        findings.append(("D6", "-", "no 'Slots: <used>/<allowed>' line and no --slots given"))
    elif peak > allowed:
        findings.append(("D6", "-", f"{peak} units run at once in one step; limit is {allowed}"))
    summary = f"{len(rows)} unit(s) · {len(steps)} step(s) · peak {peak}/{allowed if allowed is not None else '?'}"
    return findings, summary


def schedule_match(plan_path):
    """True when every row is a ticket-level unit whose model and formation equal the scheduled plan, with a
    script-generated assignment. Such a plan holds no judgment call for the delegation auditor (D1, D2, D4, D8 were
    settled by the roadmap and the schedule). Packet plans inside a ticket never match: splitting is judgment."""
    c = load_sprint(required=False)
    _, rows = parse_plan(plan_path.read_text(encoding="utf-8"))
    if not c or not rows:
        return False
    tracks = {tr["id"] for tr in c.get("tracks") or []}
    for r in rows:
        unit, model, role = r["unit"].strip("` "), r.get("model", "").strip("` ").lower(), r.get("role", "").lower()
        base, _, sub = unit.partition(".")
        t = c["tickets"].get(base)
        f_ = (t or {}).get("formation") or {}
        if unit in tracks:
            ok = role.startswith("manager") and model == "opus"
        elif t and not sub:
            ok = f_.get("kind") == "team" and role.startswith("lead") and model == f_.get("lead")
        elif t and sub == "E1":
            ok = f_.get("kind") == "solo" and role.startswith("engineer") and model == f_.get("model")
        elif t and re.fullmatch(r"R\d+", sub):
            allowed = {rv["model"] for rv in t["review_plan"]["reviewers"]} | ({"sonnet"} if t.get("attempts") else set())
            ok = role.startswith("reviewer") and model in allowed
        else:
            ok = False
        af = L / "assignments" / f"{unit}.md"
        if not ok or not af.exists() or GEN_MARK not in af.read_text(encoding="utf-8"):
            return False
    return True


def cmd_validate_plan(a):
    path = Path(a.plan)
    if not path.is_absolute():
        path = Path.cwd() / path if (Path.cwd() / path).exists() else ROOT / a.plan
    if not path.exists():
        die(f"plan not found: {a.plan} (relative paths are tried from here and from the repo root)")
    findings, summary = validate_plan(path, a.slots, not a.no_files)
    if not findings:
        print(f"PLAN OK {path.name}: {summary} · D3 D5 D6 pass")
        return
    print(f"PLAN FAIL {path.name}: {summary} · {len(findings)} finding(s)")
    for rule, unit, text in findings[:20]:
        print(f"  {rule} · {unit} — {text}")
    if len(findings) > 20:
        print(f"  … {len(findings) - 20} more")
    print("Fix the plan or assignment files, then run validate-plan again. Do not send these to the auditor.")
    sys.exit(1)


def ledger_stats(rows):
    agg = {}
    for r in rows:
        k = (r.get("leader") or "?", r.get("kind"))
        s = agg.setdefault(k, [0, 0, 0, 0])
        s[0] += 1
        s[1] += r.get("raised", 0)
        s[2] += r.get("accepted", 0)
        s[3] += r.get("overruled", 0)
    out = []
    for (leader, kind), (n, raised, acc, over) in sorted(agg.items()):
        out.append(f"  {leader:13} {kind:10} audits {n:3} objections {raised:3} accepted {acc:3} overruled {over:3}")
    return "\n".join(out)


def cmd_roadmap_sync(a):
    conf = cfg()
    c = load_sprint()
    if not conf.get("roadmap"):
        die("no roadmap configured")
    rp = ROOT / conf["roadmap"]
    html, m, data = load_roadmap(rp)
    idx = {t["id"]: t for t in data.get("tickets", [])}
    stamp = now_z()
    merged, blocked, progress = [], [], []
    changed = []
    for tid, ct in c["tickets"].items():
        if ct.get("adhoc") or tid not in idx:
            continue
        rt = idx[tid]
        st = ct["state"]
        if st == "merged":
            want = "review"
            n = (f"Claude: Coniso sprint {c['name']} merged {ct['merged_sha'][:7]} on {c.get('branch')}"
                 f" — reviewer verdict {ct.get('verdict') or 'pass'}, attempts {ct['attempts'] + 1}. Click-through checks are yours.")
            merged.append(tid)
        elif st == "blocked":
            want = "blocked"
            last = next((h["note"] for h in reversed(ct["history"]) if h["note"]), "see .coniso reports")
            n = f"Claude: Coniso sprint {c['name']} blocked after {ct['attempts'] + 1} attempt(s): {last}"
            blocked.append(tid)
        elif st in ACTIVE:
            want = "progress"
            n = f"Claude: Coniso sprint {c['name']} in progress."
            progress.append(tid)
        else:
            continue
        notes = rt.get("notes") or ""
        if rt.get("status") == want and n in notes:
            continue
        if rt.get("status") == "done":
            continue  # never overwrite the user's confirmation
        rt["status"] = want
        if n not in notes:
            rt["notes"] = (notes.rstrip() + "\n" + n).strip()
        rt["updated"] = stamp
        changed.append(tid)
    merged = [x for x in merged if x in changed]
    blocked = [x for x in blocked if x in changed]
    progress = [x for x in progress if x in changed]
    if not changed:
        print("roadmap already current")
        return
    parts = []
    if merged:
        parts.append(f"merged {', '.join(merged)} → review")
    if blocked:
        parts.append(f"blocked {', '.join(blocked)}")
    if progress:
        parts.append(f"in progress {', '.join(progress)}")
    data.setdefault("log", []).append({"id": f"L{int(time.time() * 1000)}", "date": dt.date.today().isoformat(),
                                       "text": f"Coniso sprint {c['name']}: " + "; ".join(parts) + "."})
    meta = data.setdefault("meta", {})
    meta["revision"] = int(meta.get("revision") or 0) + 1
    meta["updated"] = stamp
    errs = validate_roadmap(data)
    if errs and not a.force:
        print("\n".join(f"  {e}" for e in errs[:15]))
        die("roadmap fails validation; nothing written. Run `roadmap validate`, fix it per .coniso/ref/roadmap.md, or pass --force.")
    if a.dry_run:
        print("DRY RUN — would write rev", meta["revision"], ":", "; ".join(parts))
        return
    write_roadmap(rp, html, m, data)
    with Lock():
        c2 = load_sprint()
        for tid in merged + blocked + progress:
            c2["tickets"][tid]["synced"] = stamp
        save_sprint(c2)
    committed = ""
    if conf.get("worktrees", True) and not a.no_commit:
        dirty = main_dirty(ignore=[conf["roadmap"]])
        if not dirty:
            git(["add", "--", conf["roadmap"]], ROOT)
            r = git(["commit", "-q", "-m", f"coniso: roadmap sync ({c['name']}, rev {meta['revision']})"], ROOT, check=False)
            committed = " · committed" if r.returncode == 0 else f" · commit failed: {(r.stderr or r.stdout).strip()[:120]}"
        else:
            committed = " · not committed (other changes present)"
    print(f"ROADMAP rev {meta['revision']}: " + "; ".join(parts) + committed)


def token_totals(c):
    """Retro lines: reported subagent tokens per ticket and per role (from `set --tokens`)."""
    per_ticket, per_role = {}, {}
    for tid, t in c["tickets"].items():
        for h in t.get("history", []):
            n = h.get("tokens")
            if n:
                per_ticket[tid] = per_ticket.get(tid, 0) + n
                r = h.get("role") or "?"
                per_role[r] = per_role.get(r, 0) + n
    if not per_ticket:
        return []
    total = sum(per_ticket.values())
    return [f"Tokens (reported): {total:,} total · per ticket: "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(per_ticket.items(), key=lambda x: -x[1])),
            "Tokens per role: " + ", ".join(f"{k} {v:,}" for k, v in sorted(per_role.items(), key=lambda x: -x[1]))]


def cmd_retro(a):
    c = load_sprint()
    rows = []
    lp = L / "ledger.jsonl"
    if lp.exists():
        rows = [json.loads(x) for x in lp.read_text(encoding="utf-8").splitlines() if x.strip()]
    by = {}
    for tid, t in c["tickets"].items():
        by.setdefault(t["state"], []).append(tid)
    lines = [f"## {c['name']} — {dt.date.today().isoformat()}",
             f"Formation {c.get('formation')} · {len(c['tickets'])} ticket(s) · waves {len(c.get('waves', []))} · branch {c.get('branch')}",
             "Outcome: " + "; ".join(f"{k} {', '.join(sorted(v))}" for k, v in sorted(by.items()))]
    retries = [f"{tid} ({t['attempts'] + 1} tries)" for tid, t in c["tickets"].items() if t["attempts"]]
    if retries:
        lines.append("Retries: " + ", ".join(retries))
    esc = [f"{tid}: {h['note']}" for tid, t in c["tickets"].items() for h in t["history"] if h["note"].startswith("escalated")]
    if esc:
        lines.append("Escalations: " + "; ".join(esc))
    conf_ = [tid for tid, t in c["tickets"].items() if any("conflict" in h["note"] for h in t["history"])]
    if conf_:
        lines.append("Merge conflicts: " + ", ".join(conf_))
    tk = token_totals(c)
    if tk:
        lines += tk
    st = ledger_stats(rows)
    if st:
        lines += ["Auditor ledger (overrules worth a look):", st]
    lines += ["Lessons:", "- (Director: brief gaps, forged specialties worth keeping, routing that the auditor caught)", ""]
    text = "\n".join(lines)
    with (L / "RETRO.md").open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)


def cmd_close(a):
    c = load_sprint(required=False)
    if not c:
        if not getattr(a, "quiet", False):
            print("no sprint to close")
        return
    open_wt = [tid for tid, t in c["tickets"].items() if t.get("wt")]
    if open_wt and not a.force:
        die(f"worktrees still open for {', '.join(open_wt)}; merge, `wt remove`, or close --force")
    for tid in open_wt:
        cmd_wt(argparse.Namespace(action="remove", id=tid, force=True))
    dest = L / "archive" / c["name"]
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("sprint.json", "ledger.jsonl"):
        if (L / name).exists():
            shutil.move(str(L / name), str(dest / name))
    for d in ("tickets", "assignments", "reports"):
        if (L / d).exists() and any((L / d).iterdir()):
            shutil.move(str(L / d), str(dest / d))
            (L / d).mkdir()
    if not getattr(a, "quiet", False):
        left = [f"{tid}({t['state']})" for tid, t in c["tickets"].items() if t["state"] not in ("merged", "dropped")]
        print(f"closed {c['name']} -> {p(dest)}")
        if left:
            print(f"Not merged (back in the roadmap pool): {', '.join(left)}")



# ---------------------------------------------------------------- compound commands (dispatch, review, verdict, land)
# Each one replaces several Director tool calls with one, so the main session's context grows slower.

GEN_MARK = "GENERATED: coniso"


class redirect_out:
    """Capture a sub-command's stdout, so compound commands print only their own summary."""

    def __enter__(self):
        import io
        self.old, sys.stdout = sys.stdout, io.StringIO()
        self.text = ""
        return self

    def __exit__(self, *exc):
        self.text, sys.stdout = sys.stdout.getvalue(), self.old


def accept_count(tid, t):
    if t.get("accept_n") is not None:
        return t["accept_n"]
    f = L / "tickets" / f"{safe(tid)}.md"
    n, sec = 0, False
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.startswith("## "):
                sec = ln.startswith("## Done when")
            elif sec and re.match(r"\d+\.\s", ln):
                n += 1
    return n


def accept_range(n):
    return f"acceptance items 1-{n}" if n > 1 else ("acceptance item 1" if n == 1 else "the ticket's objective")


def specialty_files(spec):
    own = L / "specialties" / f"{spec}.md"
    out = [p(own if own.exists() else L / "ref" / "specialties" / f"{spec}.md")]
    fb = ROOT / "docs" / "agent-feedback" / f"{spec}.md"
    if fb.exists():
        out.append(p(fb))
    return out


def write_assignment(unit, head, fields, rewrite):
    """Write .coniso/assignments/<unit>.md with the formats.md fields. Keeps an existing file unless rewrite."""
    f = L / "assignments" / f"{unit}.md"
    if f.exists() and not rewrite:
        return f, "kept"
    body = [head] + [f"{k}: {v}" for k, v in fields if v] + [GEN_MARK]
    f.write_text("\n".join(body) + "\n", encoding="utf-8")
    return f, "written"


def spawn_line(agent, model, path):
    return f"SPAWN {agent} · model {model} · prompt: Assignment: {p(path)}"


def write_table(name, rows, review, used, allowed):
    lines = ["| Unit | Role/specialty | Model | Step | Owns | Needs (inputs/order) | Done when |",
             "|------|----------------|-------|------|------|----------------------|-----------|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    lines += [f"Review: {review}   Slots: {used}/{allowed}", "Auditor: "]
    f = L / "assignments" / f"{name}.md"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def check_table(f):
    findings, summary = validate_plan(f)
    if findings:
        print(f"PLAN FAIL {f.name}: {summary}")
        for rule, unit, text in findings[:10]:
            print(f"  {rule} · {unit} — {text}")
        sys.exit(1)
    match = schedule_match(f)
    print(f"PLAN OK {f.name}: {summary}" + (" · MATCHES SCHEDULE" if match else " · deviates from the schedule: audit it"))


def parse_specialties(arg, ids):
    out = {tid: "builder" for tid in ids}
    for part in (arg or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        elif part:
            out = {tid: part for tid in ids}
    return out


def cmd_dispatch(a):
    """Worktree, assignment file and exact spawn line per ticket; a validated wave table when there are several."""
    conf = cfg()
    c = load_sprint()
    ids = [x for x in ",".join(a.ids).split(",") if x]
    specs = parse_specialties(a.specialty, ids)
    rows, spawns, used = [], [], 0
    cmd = conf["cmd"]
    for tid in ids:
        t = ticket(c, tid)
        if t["state"] in TERMINAL:
            die(f"{tid} is {t['state']}")
        waiting = [d for d in t["deps"] if c["tickets"].get(d, {}).get("state") != "merged"]
        if waiting and not a.force:
            die(f"{tid} waits on {', '.join(waiting)} (not merged); dispatch it after they land")
        f_ = t["formation"]
        n = accept_count(tid, t)
        packet = p(L / "tickets" / f"{safe(tid)}.md")
        fix = L / "reports" / f"{safe(tid)}.fix.md"
        inputs = packet + (f" · fix list {p(fix)}" if fix.exists() else "")
        bounds = "This ticket only: nothing from other tickets or later work. Never run git or edit the roadmap or the brief."
        if f_["kind"] == "solo":
            if conf.get("worktrees", True):
                with redirect_out():
                    cmd_wt(argparse.Namespace(action="add", id=tid, force=False))
                workdir = load_sprint()["tickets"][tid]["wt"]
            else:
                workdir = p(ROOT)
            unit, spec = f"{tid}.E1", specs.get(tid, "builder")
            read = " · ".join([p(L / "BRIEF.md"), packet] + specialty_files(spec))
            path, how = write_assignment(unit, f"ASSIGNMENT {unit} · engineer/{spec} · model {f_['model']} · from {a.from_}", [
                ("READ FIRST", read), ("WORKDIR", p(workdir)),
                ("OBJECTIVE", f"Build ticket {tid} ({t['title']}) so every acceptance item is met, with evidence."),
                ("OWNS", "** (solo: the whole ticket inside WORKDIR)"), ("INPUTS", inputs), ("BOUNDARIES", bounds),
                ("DONE WHEN", f"{accept_range(n)} met, each with evidence (test name or file:line); `{cmd} test {tid}` passes."),
                ("ON DONE", f"after the test passes run `{cmd} set {tid} built --note \"<one phrase>\"` (solo ticket: you record built)."),
                ("RETURN", f"standard return + detail in {p(L / 'reports' / (unit + '.md'))}")], a.rewrite)
            rows.append([unit, f"engineer/{spec}", f_["model"], "1", "**", f"ticket {tid}", accept_range(n)])
            spawns.append(spawn_line("coniso:engineer", f_["model"], path))
        else:
            unit = tid
            review = "review lead serves" if a.review == "lead" else "dispatch your own reviewer"
            path, how = write_assignment(unit, f"ASSIGNMENT {unit} · lead · model {f_['lead']} · from {a.from_}", [
                ("READ FIRST", f"{p(L / 'BRIEF.md')} · {packet}"), ("WORKDIR", p(ROOT)),
                ("OBJECTIVE", f"Deliver ticket {tid} ({t['title']}) built, tested and handed off with evidence for every acceptance item."),
                ("OWNS", f"ticket {tid}"), ("INPUTS", inputs), ("BOUNDARIES", bounds),
                ("DONE WHEN", f"{accept_range(n)} evidenced; `{cmd} validate-handoff {tid}` prints HANDOFF OK; state built."),
                ("REVIEW", review), ("SLOTS", str(f_["engineers"])),
                ("ENGINEERS", f"up to {f_['engineers']} (default engineer model {f_.get('engineer_model', 'sonnet')})"),
                ("RETURN", f"standard return + detail in {p(L / 'reports' / (tid + '.md'))}")], a.rewrite)
            rows.append([unit, "lead", f_["lead"], "1", f"ticket {tid}", f"ticket {tid}", accept_range(n)])
            spawns.append(spawn_line("coniso:lead", f_["lead"], path))
        used += cost(f_)
        if how == "kept":
            print(f"  kept existing {path.name} (--rewrite regenerates it)")
    if len(ids) > 1 or a.plan:
        wave = min((c["tickets"][i].get("wave") or 0) for i in ids)
        name = a.plan or f"{a.from_}-w{wave}"
        review = "Review Lead" if a.review == "lead" else "each leader dispatches its own reviewer"
        check_table(write_table(name, rows, review, used, c.get("budget") or conf["slots"]))
    print("\n".join(spawns))


def next_reviewer_n(tid):
    n = 1
    while (L / "assignments" / f"{tid}.R{n}.md").exists():
        n += 1
    return n


def cmd_review(a):
    """Sync each ticket with main, write reviewer assignments per review_plan, set reviewing, print spawn lines."""
    conf = cfg()
    ids = [x for x in ",".join(a.ids).split(",") if x]
    cmd = conf["cmd"]
    c = load_sprint()
    for tid in ids:
        t = ticket(c, tid)
        if t["state"] not in ("built", "reviewing") and not a.force:
            die(f"{tid} is {t['state']}; review takes built tickets")
        if a.tokens:
            with Lock():
                c2 = load_sprint()
                note(ticket(c2, tid), t["state"], "builder tokens", a.tokens, a.role or "engineer")
                save_sprint(c2)
        if conf.get("worktrees", True) and t.get("wt") and not a.no_sync:
            r = redirect_out()
            try:
                with r:
                    cmd_sync(argparse.Namespace(id=tid, cont=False))
            except SystemExit as e:
                print(r.text.strip())
                print(f"Review not dispatched: send {tid}'s builder the conflict (resolve, sync {tid} --continue, test, set built), then review again.")
                sys.exit(e.code or 3)
    c = load_sprint()
    batch = [tid for tid in ids if c["tickets"][tid]["review_plan"]["kind"] == "batch"
             and not (c["tickets"][tid]["attempts"] or a.delta)]
    groups = ([batch] if len(batch) > 1 else []) + [[tid] for tid in ids if not (len(batch) > 1 and tid in batch)]
    rows, spawns, plans = [], [], {}
    for group in groups:
        first = group[0]
        t = c["tickets"][first]
        delta = a.delta or t["attempts"] > 0
        if delta:   # playbook: re-reviews get one reviewer on the fix list and the files it touched
            reviewers = [{"model": a.model or "sonnet", "lens": "spec+regression"}]
        elif a.panel and (a.panel == "*" or first in a.panel.split(",")) and t["review_plan"]["kind"] != "panel":
            reviewers = review_plan(dict(t, review=True))["reviewers"]
        else:
            reviewers = t["review_plan"]["reviewers"]
        n0 = next_reviewer_n(first)
        panel = len(reviewers) > 1
        units = []
        packets = " · ".join(p(L / "tickets" / f"{safe(x)}.md") for x in group)
        items = ", ".join(f"{x} {accept_range(accept_count(x, c['tickets'][x]))}" for x in group)
        for k, rv in enumerate(reviewers):
            unit = f"{first}.R{n0 + k}"
            units.append(unit)
            fix = L / "reports" / (f"{safe(first)}.{unit}.fix.md" if panel else f"{safe(first)}.fix.md")
            rep = L / "reports" / f"{unit}.md"
            inputs = packets
            if delta:
                prev = [x for x in sorted((L / "reports").iterdir())
                        if re.fullmatch(re.escape(safe(first)) + r"\.R\d+\.md", x.name) and x.name != rep.name]
                inputs += f" · fix list {p(L / 'reports' / (safe(first) + '.fix.md'))}" + "".join(f" · earlier review {p(x)}" for x in prev)
            path, _ = write_assignment(unit, f"ASSIGNMENT {unit} · reviewer · model {rv['model']} · from {a.from_}", [
                ("READ FIRST", f"{p(L / 'BRIEF.md')} · {packets}"), ("WORKDIR", p(t.get("wt") or ROOT)),
                ("OBJECTIVE", f"Verify {', '.join(group)} against the acceptance items and the brief through lens {rv['lens']}, and return an evidence-backed verdict."),
                ("OWNS", f"{p(rep)}, {p(L / 'reports' / unit)}/**, {p(fix)}"), ("LENS", rv["lens"]),
                ("SCOPE", "delta" if delta else "full"), ("INPUTS", inputs),
                ("BOUNDARIES", "Never edit WORKDIR files or run git; probes only under your reports folder."),
                ("DONE WHEN", f"a verdict per formats.md in your report, with {items} each marked met or unmet with evidence. "
                              f"Then run `{cmd} verdict {','.join(group)} --report {p(rep)}` and put the line it prints in your return."),
                ("RETURN", f"verdict return (formats.md) + detail in {p(rep)}")], True)
            rows.append([unit, f"reviewer/{rv['lens']}", rv["model"], "1",
                         f".coniso/reports/{unit}.md, .coniso/reports/{unit}/**, .coniso/reports/{fix.name}",
                         ", ".join(f"ticket {x}" for x in group), "verdict"])
            spawns.append(spawn_line("coniso:reviewer", rv["model"], path))
        for x in group:
            plans[x] = units
    with Lock():
        c = load_sprint()
        for x, units in plans.items():
            t = c["tickets"][x]
            t["review_round"] = {"units": units, "verdicts": {}}
            t["state"] = "reviewing"
            note(t, "reviewing", f"reviewers {', '.join(units)}")
        save_sprint(c)
    if len(rows) > 1 or a.plan:
        check_table(write_table(a.plan or f"{ids[0]}-review", rows, "this table's reviewers", len(rows), conf.get("review_slots", 4)))
    print(f"REVIEWING {', '.join(ids)}")
    print("\n".join(spawns))


VERDICT_RE = re.compile(r"^\s*VERDICT\s+(\S+)\s*[·|]\s*(\S+)\s*[·|]\s*lens\s+(.+?):\s*(pass-with-fixes|pass|fail)\b", re.I)
ACCEPT_RE = re.compile(r"^\s*[-*]?\s*(A\d+(?:\s*(?:-|–|,|\.\.|and)\s*A?\d+)*)\s+(met|unmet)\b(.*)$", re.I)
SEV_RE = re.compile(r"\[(blocker|major|minor)\]", re.I)


def expand_items(spec):
    out = set()
    for part in re.split(r"\s*(?:,|and)\s*", re.sub(r"[Aa]", "", spec)):
        m = re.fullmatch(r"(\d+)\s*(?:-|–|\.\.)\s*(\d+)", part.strip())
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.strip().isdigit():
            out.add(int(part.strip()))
    return out


def parse_verdicts(text):
    """VERDICT blocks of a reviewer report, keyed by ticket: stated verdict, items met/unmet, evidence gaps, severities."""
    blocks, cur, in_findings = {}, None, False
    for ln in text.splitlines():
        m = VERDICT_RE.match(ln)
        if m:
            cur = {"unit": m.group(2), "stated": m.group(4).lower(), "met": set(), "unmet": set(),
                   "no_evidence": set(), "blocker": 0, "major": 0, "minor": 0, "fix": None}
            blocks[m.group(1)] = cur
            in_findings = False
            continue
        if cur is None:
            continue
        s = ln.strip()
        head, _, rest = s.partition(":")
        head = head.strip("-* ").upper()
        if head in ("TESTS", "FIX LIST", "REPORT"):
            in_findings = False
            if head == "FIX LIST":
                v = rest.strip().split()[0] if rest.strip() else ""
                cur["fix"] = None if v.lower() in ("", "none", "-", "n/a") else v
            continue
        if head == "FINDINGS":
            in_findings, s = True, rest
        am = None if in_findings else ACCEPT_RE.match(ln)
        if am:
            items = expand_items(am.group(1))
            ev = am.group(3).strip().lstrip("—–-:· ").strip()
            (cur["met"] if am.group(2).lower() == "met" else cur["unmet"]).update(items)
            if not ev or ev.lower() in ("none", "n/a", "-", "tbd"):
                cur["no_evidence"].update(items)
        elif in_findings and "suspicion" not in s.lower():
            for sev in SEV_RE.findall(s):
                cur[sev.lower()] += 1
    return blocks


def rule_round(vs):
    """Mechanical ruling over every verdict of a round: ('pass'|'fail', None), or (None, why a leader must rule)."""
    void = [u for u, v in vs.items() if v["void"]]
    if void:
        return None, "void verdict " + "; ".join(f"{u}: {vs[u]['void']}" for u in void) + " (re-run that reviewer once)"
    stated = {("fail" if v["stated"] == "fail" else "pass") for v in vs.values()}
    if len(stated) > 1:
        return None, "panel split: " + ", ".join(f"{u} {v['stated']}" for u, v in vs.items())
    if any(v["blocker"] or v["unmet"] for v in vs.values()):
        mech = "fail"
    elif any(v["major"] for v in vs.values()):
        return None, "major finding(s) from " + ", ".join(u for u, v in vs.items() if v["major"]) + ": fail unless unsafe-to-merge is ruled out"
    else:
        mech = "pass"
    said = stated.pop()
    if mech != said:
        return None, f"the rules give {mech} but the reviewer said {said}"
    return mech, None


def cmd_verdict(a):
    """Record a reviewer's verdict; when the round is complete, apply the mechanical review rules."""
    cfg()
    ids = [x for x in a.ids.split(",") if x]
    rp = Path(a.report)
    rp = rp if rp.is_absolute() else ROOT / rp
    if not rp.exists():
        die(f"report not found: {a.report}")
    blocks = parse_verdicts(rp.read_text(encoding="utf-8"))
    out = []
    with Lock():
        c = load_sprint()
        for tid in ids:
            t = ticket(c, tid)
            rnd = t.setdefault("review_round", {"units": [], "verdicts": {}})
            b = blocks.get(tid)
            if b is None:
                unit = a.unit or rp.stem
                rec = {"stated": "?", "void": "no VERDICT line for this ticket", "blocker": 0, "major": 0, "minor": 0, "unmet": 0}
            else:
                unit = b["unit"]
                missing = sorted(set(range(1, accept_count(tid, t) + 1)) - b["met"] - b["unmet"])
                void = (["no verdict for A" + ",A".join(map(str, missing))] if missing else []) + \
                       (["no evidence for A" + ",A".join(map(str, sorted(b["no_evidence"])))] if b["no_evidence"] else [])
                rec = {"stated": b["stated"], "void": "; ".join(void), "blocker": b["blocker"], "major": b["major"],
                       "minor": b["minor"], "unmet": len(b["unmet"]), "fix": b["fix"]}
            rec["report"] = p(rp)
            rnd["verdicts"][unit] = rec
            if unit not in rnd["units"]:
                rnd["units"].append(unit)
            pending = [u for u in rnd["units"] if u not in rnd["verdicts"]]
            if pending:
                out.append(f"RECORDED {tid} {unit} {rec['stated']}; waiting on {', '.join(pending)}")
                continue
            vs = rnd["verdicts"]
            ruling, why = rule_round(vs)
            main_fix = L / "reports" / f"{safe(tid)}.fix.md"
            if ruling == "fail":
                parts = sorted(x for x in (L / "reports").iterdir()
                               if re.fullmatch(re.escape(safe(tid)) + r"\.[^.]+\.R\d+\.fix\.md", x.name))
                if parts:   # a panel's fix lists become one
                    main_fix.write_text("\n\n".join(f"<!-- {x.name} -->\n" + x.read_text(encoding="utf-8") for x in parts), encoding="utf-8")
                if not main_fix.exists():
                    ruling, why = None, f"fail without a fix list: the reviewer must write .coniso/reports/{main_fix.name}"
            summary = "; ".join(f"{u} {v['stated']} b{v['blocker']} M{v['major']} m{v['minor']}" for u, v in vs.items())
            if ruling is None:
                note(t, t["state"], f"needs ruling: {why}")
                out.append(f"NEEDS RULING {tid}: {why} · reports: " + ", ".join(sorted({v['report'] for v in vs.values()})))
                continue
            minors = sum(v["minor"] for v in vs.values())
            t["state"], t["verdict"] = ("passed" if ruling == "pass" else "failed"), ruling
            text = f"ruled {ruling}: {summary}" + (f"; {minors} minor(s) carried as notes" if ruling == "pass" and minors else "") \
                + (f"; fix list .coniso/reports/{main_fix.name}" if ruling == "fail" else "")
            note(t, t["state"], text)
            (L / "reports" / f"{safe(tid)}.ruling.md").write_text(f"{tid} {text}\n", encoding="utf-8")
            audit = ruling == "fail" or t.get("model") == "opus" or t.get("review")
            out.append(f"RULED {ruling} {tid} — {summary}" + (" · verdict audit required (playbook)" if audit else ""))
        save_sprint(c)
    print("\n".join(out))


def cmd_land(a):
    """Merge passed tickets in dependency order; run roadmap-sync once their wave has nothing in flight."""
    conf = cfg()
    c = load_sprint()
    ids = [x for x in ",".join(a.ids).split(",") if x]
    order = sorted(ids, key=lambda x: (ticket(c, x).get("wave") or 0, len(c["tickets"][x]["deps"]), x))
    if a.tokens:
        with Lock():
            c2 = load_sprint()
            note(ticket(c2, order[0]), c2["tickets"][order[0]]["state"], "reviewer tokens", a.tokens, a.role or "reviewer")
            save_sprint(c2)
    for tid in order:
        r = redirect_out()
        try:
            with r:
                cmd_merge(argparse.Namespace(id=tid, force=False, no_test=False, keep=False))
        except SystemExit as e:
            print(r.text.strip())
            sys.exit(e.code or 1)
        print("\n".join(x for x in r.text.splitlines() if x.startswith(("MERGED", "Now ready"))))
    c = load_sprint()
    waves = sorted({c["tickets"][x].get("wave") or 0 for x in order})
    busy = [tid for tid, t in c["tickets"].items() if (t.get("wave") or 0) in waves and t["state"] in ACTIVE]
    if busy:
        print(f"Wave still in flight: {', '.join(busy)}; roadmap-sync waits for it")
        return
    with Lock():
        c = load_sprint()
        landed = c.setdefault("waves_landed", [])
        landed += [w for w in waves if w not in landed]
        save_sprint(c)
    if conf.get("roadmap"):
        cmd_roadmap_sync(argparse.Namespace(dry_run=False, no_commit=False, force=False))
    k = len(landed)
    print(f"WAVE {','.join(map(str, waves))} landed · {k} wave(s) merged this sprint"
          + (" · RESTART POINT (see director.md, Long sessions)" if k % 3 == 0 else ""))


# ---------------------------------------------------------------- roadmap (works with or without .coniso)

TICKET_KEYS = ("id", "phase", "title", "size", "deps", "why", "what", "accept", "prompt", "model", "effort",
               "review", "modelWhy", "status", "notes", "checks", "updated")


def roadmap_template():
    for cand in (SCRIPT.parent.parent / "skills" / "coniso" / "references" / "roadmap-template.html",
                 L / "ref" / "roadmap-template.html"):
        if cand.exists():
            return cand
    die("roadmap template not found; re-run init from the plugin")


def find_roadmaps():
    hits = []
    for pat in ("docs/roadmap/*.html", "docs/*.html", "*.html", "*/*.html", "*/*/*.html"):
        for f in sorted(ROOT.glob(pat)):
            rel = f.relative_to(ROOT).as_posix()
            if rel.startswith((".coniso/", ".git/")) or "/node_modules/" in rel or f in hits:
                continue
            try:
                if 'id="roadmap-data"' in f.read_text(encoding="utf-8", errors="replace")[:400000]:
                    hits.append(f)
            except OSError:
                continue
    return hits


def resolve_roadmap(explicit=None, must=True):
    if explicit:
        f = Path(explicit)
        f = f if f.is_absolute() else (Path.cwd() / f)
        if not f.exists():
            die(f"roadmap not found: {explicit}")
        return f.resolve()
    conf = read_json(L / "config.json", {}) or {}
    if conf.get("roadmap") and (ROOT / conf["roadmap"]).exists():
        return ROOT / conf["roadmap"]
    hits = find_roadmaps()
    if len(hits) == 1:
        return hits[0]
    if not must:
        return None
    if not hits:
        die("no roadmap found in this repo; create one with `roadmap new --title \"…\"` or pass --file")
    die("several roadmaps found; pass --file: " + ", ".join(h.relative_to(ROOT).as_posix() for h in hits))


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "Project"


def ready_tickets(data, strict=False):
    ok = ("done",) if strict else ("done", "review")
    idx = {t["id"]: t for t in data.get("tickets", [])}
    return [t for t in data.get("tickets", []) if t.get("status") == "todo"
            and all(idx.get(d, {}).get("status") in ok for d in t.get("deps", []))]


def fit_checks(t):
    n = len(t.get("accept") or [])
    c = [bool(x) for x in (t.get("checks") or [])][:n]
    t["checks"] = c + [False] * (n - len(c))


def save_roadmap_data(path, html, m, data, log_text=None, commit=False, quiet=False):
    errs = validate_roadmap(data)
    if errs:
        die("roadmap invalid, nothing written:\n  " + "\n  ".join(errs[:20]))
    meta = data.setdefault("meta", {})
    stamp = now_z()
    if log_text:
        data.setdefault("log", []).append({"id": f"L{int(time.time() * 1000)}", "date": dt.date.today().isoformat(), "text": log_text})
    meta["revision"] = int(meta.get("revision") or 0) + 1
    meta["updated"] = stamp
    write_roadmap(path, html, m, data)
    if not quiet:
        print(f"ROADMAP {p(path)} rev {meta['revision']} written")
    if commit and run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT).returncode == 0:
        rel = path.resolve().relative_to(ROOT.resolve()).as_posix()
        git(["add", "--", rel], ROOT)
        r = git(git_identity_args(ROOT) + ["commit", "-q", "-m", f"roadmap: rev {meta['revision']}" + (f" — {log_text[:60]}" if log_text else ""), "--", rel], ROOT, check=False)
        print("committed" if r.returncode == 0 else f"WARN: commit refused: {(r.stderr or r.stdout).strip()[:200]}")


def cmd_roadmap(a):
    act = a.action
    if act == "new":
        if not a.title:
            die("roadmap new needs --title")
        tmpl = roadmap_template()
        html = tmpl.read_text(encoding="utf-8")
        m = RM_RE.search(html)
        data = json.loads(m.group(2))
        seed = read_json(Path(a.seed), None) if a.seed else None
        if a.seed and seed is None:
            die(f"cannot read seed JSON {a.seed}")
        stamp = now_z()
        name = slug(a.title)
        out = Path(a.out) if a.out else ROOT / "docs" / "roadmap" / f"{name}-Roadmap.html"
        out = out if out.is_absolute() else Path.cwd() / out
        if out.exists() and not a.force:
            die(f"{p(out)} exists (use --force to overwrite)")
        meta = data["meta"]
        meta.update({"id": f"{name.lower()}-{int(time.time())}", "title": a.title, "product": a.product or a.title,
                     "owner": a.owner or "", "filename": out.name, "revision": 1, "created": stamp, "updated": stamp,
                     "summary": a.summary or ""})
        if seed:
            for k, v in seed.items():
                if k == "meta":
                    meta.update({mk: mv for mk, mv in v.items() if mk not in ("id", "revision", "created", "updated", "filename")})
                else:
                    data[k] = v
        for t in data.get("tickets", []):
            t.setdefault("status", "todo"); t.setdefault("notes", ""); t.setdefault("deps", [])
            t.setdefault("review", False); fit_checks(t); t.setdefault("updated", stamp)
            if t.get("model") == "haiku":
                t["effort"] = None
        errs = validate_roadmap(data)
        if errs:
            die("seed invalid, nothing written:\n  " + "\n  ".join(errs[:20]))
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
        out.write_text(html[:m.start(2)] + "\n" + payload + "\n" + html[m.end(2):], encoding="utf-8")
        print(f"ROADMAP created: {p(out)} ({len(data.get('phases', []))} phase(s), {len(data.get('tickets', []))} ticket(s))")
        if (L / "config.json").exists():
            conf = cfg()
            if not conf.get("roadmap"):
                conf["roadmap"] = out.resolve().relative_to(ROOT.resolve()).as_posix()
                write_json(L / "config.json", conf)
                print(f"Coniso now uses it: roadmap = {conf['roadmap']}")
        return
    path = resolve_roadmap(a.file)
    html, m, data = load_roadmap(path)
    tickets = data.get("tickets", [])
    idx = {t["id"]: t for t in tickets}
    if act == "validate":
        errs = validate_roadmap(data)
        print(f"VALID {path.name} rev {data.get('meta', {}).get('revision')}" if not errs else "INVALID:\n  " + "\n  ".join(errs))
        sys.exit(0 if not errs else 1)
    if act in ("show", "next"):
        meta = data.get("meta", {})
        counts = {s: 0 for s in ("todo", "progress", "review", "done", "blocked")}
        for t in tickets:
            counts[t.get("status")] = counts.get(t.get("status"), 0) + 1
        print(f"ROADMAP {meta.get('title')} · rev {meta.get('revision')} · {p(path)}")
        print("  " + " · ".join(f"{k} {v}" for k, v in counts.items()))
        if act == "show":
            for ph in data.get("phases", []):
                pt = [t for t in tickets if t.get("phase") == ph.get("id")]
                done = sum(t.get("status") == "done" for t in pt)
                extra = " · outline only" if ph.get("outline") and not pt else ""
                print(f"  {ph.get('id')}: {ph.get('name')} — {done}/{len(pt)} done{extra}")
        rd = ready_tickets(data)
        print("Ready: " + (", ".join(f"{t['id']} ({t.get('size')}, {t.get('model')}{'/' + t['effort'] if t.get('effort') else ''})" for t in rd) or "none"))
        for label, st in (("In progress", "progress"), ("Awaiting the user's check", "review"), ("Blocked", "blocked")):
            ids = [t["id"] for t in tickets if t.get("status") == st]
            if ids:
                print(f"{label}: {', '.join(ids)}")
        gates = [t["id"] for t in tickets if is_gate(t) and t.get("status") != "done"]
        if gates:
            print(f"Open gates: {', '.join(gates)}")
        if rd:
            phase = rd[0].get("phase")
            print(f"Run with Coniso: /coniso:director {phase}   or one ticket: /coniso:lead {rd[0]['id']}")
        return
    if act == "extract":
        out = Path(a.out) if a.out else ((L / "roadmap-edit.json") if L.exists() else Path(tempfile.gettempdir()) / f"roadmap-{slug(path.stem)}.json")
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"EXTRACTED {len(tickets)} ticket(s) → {p(out)}  (edit, then: roadmap write --from {p(out)})")
        return
    if act == "write":
        if not a.seed:
            die("roadmap write needs --from <json>")
        new = read_json(Path(a.seed), None)
        if new is None:
            die(f"cannot read {a.seed}")
        stamp = now_z()
        new_ids = {t.get("id") for t in new.get("tickets", [])}
        gone = [t["id"] for t in tickets if t["id"] not in new_ids]
        gone_done = [i for i in gone if idx[i].get("status") == "done"]
        if gone_done and not a.allow_remove:
            die(f"refusing to delete done ticket(s) {', '.join(gone_done)}; add a note and a follow-up instead (or --allow-remove)")
        changes = []
        for t in new.get("tickets", []):
            fit_checks(t)
            t.setdefault("notes", ""); t.setdefault("deps", []); t.setdefault("status", "todo")
            if t.get("model") == "haiku":
                t["effort"] = None
            old = idx.get(t.get("id"))
            if old is None:
                t["updated"] = stamp
                changes.append(f"+{t['id']}")
            elif any(old.get(k) != t.get(k) for k in ("status", "notes", "checks")):
                t["updated"] = stamp     # progress fields changed: the file must beat older browser copies
                if old.get("status") != t.get("status"):
                    changes.append(f"{t['id']} {old.get('status')}→{t.get('status')}")
                else:
                    changes.append(f"{t['id']} notes/checks")
            else:
                t["updated"] = old.get("updated", t.get("updated"))   # keep: browser progress keeps merging over it
                if any(old.get(k) != t.get(k) for k in TICKET_KEYS):
                    changes.append(f"{t['id']} edited")
        changes += [f"-{i}" for i in gone]
        new.setdefault("meta", {})["revision"] = data.get("meta", {}).get("revision", 0)
        save_roadmap_data(path, html, m, new, log_text=a.log, commit=a.commit)
        print("Changes: " + (", ".join(changes) if changes else "meta/phases only"))
        return
    if act == "set":
        if not a.id or not a.status:
            die("roadmap set <id> --status <todo|progress|review|done|blocked> [--note …] [--check 1,3]")
        t = idx.get(a.id) or die(f"no ticket {a.id}")
        if a.status:
            t["status"] = a.status
        if a.note:
            t["notes"] = ((t.get("notes") or "").rstrip() + "\n" + a.note).strip()
        if a.check:
            fit_checks(t)
            for n in a.check.split(","):
                i = int(n) - 1
                if 0 <= i < len(t["checks"]):
                    t["checks"][i] = True
        t["updated"] = now_z()
        save_roadmap_data(path, html, m, data, log_text=a.log, commit=a.commit)
        return
    if act == "log":
        if not a.log:
            die("roadmap log --log \"text\"")
        save_roadmap_data(path, html, m, data, log_text=a.log, commit=a.commit)
        return
    if act == "upgrade":
        tmpl = roadmap_template().read_text(encoding="utf-8")
        tm = RM_RE.search(tmpl)
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
        path.write_text(tmpl[:tm.start(2)] + "\n" + payload + "\n" + tmpl[tm.end(2):], encoding="utf-8")
        print(f"UPGRADED {path.name} to the current page (data unchanged) · backup {backup.name}")
        return
    die(f"unknown roadmap action {act}")

# ---------------------------------------------------------------- cli

def main():
    for stream in (sys.stdout, sys.stderr):    # Windows consoles default to cp1252, which cannot print · or →
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="coniso", description="Coniso sprint control")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init", help="install/refresh .coniso in this repo")
    s.add_argument("--roadmap")
    s.add_argument("--test")
    s.add_argument("--build")
    s.add_argument("--setup", help="command run in each new worktree, e.g. 'npm ci' or 'dotnet restore'")
    s.add_argument("--link", help="comma list of untracked dirs to link into worktrees, e.g. node_modules")
    s.add_argument("--copy", help="comma list of untracked files to copy into worktrees, e.g. .env.local")
    s.add_argument("--slots", type=int)
    s.add_argument("--stack", help="stack pack from the plugin's stacks/ folder, or 'none'")
    s.add_argument("--no-claude-settings", dest="no_claude_settings", action="store_true",
                   help=argparse.SUPPRESS)  # pre-1.0.1 flag; init never writes Claude settings now
    g = s.add_mutually_exclusive_group()
    g.add_argument("--no-worktrees", dest="no_worktrees", action="store_true", default=None)
    g.add_argument("--worktrees", dest="no_worktrees", action="store_false")
    s.set_defaults(fn=cmd_init)

    s = sp.add_parser("preflight")
    s.set_defaults(fn=cmd_preflight)

    s = sp.add_parser("schedule", help="plan a sprint from roadmap tickets")
    s.add_argument("--phase")
    s.add_argument("--tickets")
    s.add_argument("--limit", type=int)
    s.add_argument("--slots", type=int)
    s.add_argument("--name")
    s.add_argument("--formation", choices=["direct", "managed"])
    s.add_argument("--strict", action="store_true", help="external deps must be done, not just review")
    s.add_argument("--include-progress", action="store_true")
    s.add_argument("--add", action="store_true")
    s.add_argument("--replace", action="store_true")
    s.set_defaults(fn=cmd_schedule)

    s = sp.add_parser("adhoc", help="add a non-roadmap ticket")
    s.add_argument("id")
    s.add_argument("--title", required=True)
    s.add_argument("--size", default="M", choices=["S", "M", "L"])
    s.add_argument("--model", default="sonnet", choices=["haiku", "sonnet", "opus"])
    s.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh"])
    s.add_argument("--review", action="store_true")
    s.add_argument("--deps")
    s.add_argument("--phase")
    s.add_argument("--why")
    s.add_argument("--accept", action="append")
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--name")
    s.add_argument("--formation", choices=["direct", "managed"])
    s.add_argument("--slots", type=int)
    s.set_defaults(fn=cmd_adhoc)

    s = sp.add_parser("status")
    s.add_argument("id", nargs="?")
    s.add_argument("--open", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sp.add_parser("set")
    s.add_argument("id")
    s.add_argument("state", nargs="?", help="new state; omit to record --tokens/--note only")
    s.add_argument("--tokens", type=int, help="total_tokens an Agent result reported for this ticket's unit")
    s.add_argument("--role", help="role that spent --tokens: engineer, lead, reviewer, auditor, manager, review-lead, scout")
    s.add_argument("--note")
    s.add_argument("--verdict")
    s.add_argument("--model", choices=["haiku", "sonnet", "opus"], help="record an escalation")
    s.set_defaults(fn=cmd_set)

    s = sp.add_parser("wt", help="worktree add|path|remove")
    s.add_argument("action", choices=["add", "path", "remove"])
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_wt)

    s = sp.add_parser("diff")
    s.add_argument("id", nargs="?")
    s.add_argument("--range")
    s.set_defaults(fn=cmd_diff)

    s = sp.add_parser("test")
    s.add_argument("id")
    s.set_defaults(fn=cmd_test)

    s = sp.add_parser("sync")
    s.add_argument("id")
    s.add_argument("--continue", dest="cont", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sp.add_parser("merge")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-test", action="store_true")
    s.add_argument("--keep", action="store_true")
    s.set_defaults(fn=cmd_merge)

    s = sp.add_parser("ledger")
    s.add_argument("action", choices=["add", "stats"])
    s.add_argument("--unit")
    s.add_argument("--leader")
    s.add_argument("--kind", choices=["delegation", "verdict", "handoff"])
    s.add_argument("--raised", type=int, default=0)
    s.add_argument("--accepted", type=int, default=0)
    s.add_argument("--overruled", type=int, default=0)
    s.add_argument("--reason", action="append")
    s.set_defaults(fn=cmd_ledger)

    s = sp.add_parser("validate-plan", help="deterministic plan checks: D3 ownership, D5 assignment files, D6 slots")
    s.add_argument("plan", help="delegation table file, e.g. .coniso/assignments/P2-03-plan.md")
    s.add_argument("--slots", type=int, help="slot limit (default: the plan's 'Slots: used/allowed' line)")
    s.add_argument("--no-files", action="store_true", help="skip D5 (assignment files not written yet)")
    s.set_defaults(fn=cmd_validate_plan)

    s = sp.add_parser("dispatch", help="worktree + assignment + spawn line per ready ticket (a validated wave table when several)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--specialty", help="solo engineer specialty: a name, or id=name,id=name")
    s.add_argument("--review", choices=["lead", "self"], default="self", help="lead: a Review Lead serves; self: leads dispatch their own reviewer")
    s.add_argument("--from", dest="from_", default="director", help="leader unit, e.g. director or T1")
    s.add_argument("--plan", help="wave table name (default <from>-w<wave> when several tickets)")
    s.add_argument("--rewrite", action="store_true", help="regenerate assignment files that already exist")
    s.add_argument("--force", action="store_true", help="dispatch although dependencies are not merged")
    s.set_defaults(fn=cmd_dispatch)

    s = sp.add_parser("review", help="sync with main, write reviewer assignments per review_plan, set reviewing, print spawn lines")
    s.add_argument("ids", nargs="+")
    s.add_argument("--delta", action="store_true", help="one reviewer, SCOPE delta (automatic after a fix round)")
    s.add_argument("--panel", nargs="?", const="*", help="upgrade to a panel: all tickets, or the comma-listed ids (never downgrades)")
    s.add_argument("--model", choices=["sonnet", "opus"], help="delta reviewer model (default sonnet)")
    s.add_argument("--plan", help="review table name (default <id>-review when there are several reviewers)")
    s.add_argument("--from", dest="from_", default="director")
    s.add_argument("--no-sync", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--tokens", type=int, help="the builder's reported tokens, recorded on the ticket")
    s.add_argument("--role", help="role for --tokens (default engineer)")
    s.set_defaults(fn=cmd_review)

    s = sp.add_parser("verdict", help="record a reviewer verdict and apply the mechanical ruling rules")
    s.add_argument("ids", help="ticket id, or comma-separated ids for a batch review")
    s.add_argument("--report", required=True)
    s.add_argument("--unit", help="reviewer unit when the report has no VERDICT line")
    s.set_defaults(fn=cmd_verdict)

    s = sp.add_parser("land", help="merge passed tickets; roadmap-sync when their wave has nothing in flight")
    s.add_argument("ids", nargs="+")
    s.add_argument("--tokens", type=int, help="the reviewer's reported tokens, recorded on the first ticket")
    s.add_argument("--role", help="role for --tokens (default reviewer)")
    s.set_defaults(fn=cmd_land)

    s = sp.add_parser("roadmap-sync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-commit", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_roadmap_sync)

    s = sp.add_parser("permissions", help="show, grant, decline or remove Coniso's entries in .claude/settings.local.json")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="add allow rules for Coniso, build and test (and nesting env fixes if needed)")
    g.add_argument("--decline", action="store_true", help="record that the user declined; stop asking")
    g.add_argument("--remove", action="store_true", help="remove everything Coniso added")
    s.add_argument("--accept-edits", dest="accept_edits", action="store_true",
                   help="also set acceptEdits as this project's default permission mode (unattended sprints)")
    s.set_defaults(fn=cmd_permissions)

    s = sp.add_parser("retro")
    s.set_defaults(fn=cmd_retro)

    s = sp.add_parser("close")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_close)

    s = sp.add_parser("roadmap", help="HTML prompt roadmap: new, show, next, validate, extract, write, set, log, upgrade")
    s.add_argument("action", choices=["new", "show", "next", "validate", "extract", "write", "set", "log", "upgrade"])
    s.add_argument("id", nargs="?")
    s.add_argument("--file", help="roadmap HTML (default: Coniso's configured one, or the only one in the repo)")
    s.add_argument("--title"); s.add_argument("--product"); s.add_argument("--owner"); s.add_argument("--summary")
    s.add_argument("--out"); s.add_argument("--force", action="store_true")
    s.add_argument("--from", dest="seed", help="JSON to seed `new` or to apply with `write`")
    s.add_argument("--status", choices=["todo", "progress", "review", "done", "blocked"])
    s.add_argument("--note"); s.add_argument("--check", help="1-based done-when items to tick, e.g. 1,3")
    s.add_argument("--log", help="append a log entry")
    s.add_argument("--allow-remove", dest="allow_remove", action="store_true")
    s.add_argument("--commit", action="store_true", help="commit the roadmap file afterwards")
    s.set_defaults(fn=cmd_roadmap)

    s = sp.add_parser("version")
    s.set_defaults(fn=lambda a: print(VERSION))

    a = ap.parse_args()
    if a.cmd not in ("init", "version", "roadmap") and not L.exists():
        die("no .coniso here; run the plugin's coniso.py init from the repo root")
    a.fn(a)


if __name__ == "__main__":
    main()
