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

VERSION = "1.3.0-dev.4"
STATES = ["queued", "building", "built", "reviewing", "passed", "failed",
          "conflict", "merged", "blocked", "dropped"]
ACTIVE = {"building", "built", "reviewing", "passed", "failed", "conflict"}
TERMINAL = {"merged", "blocked", "dropped"}
SIZE_HOURS = {"XS": 0.5, "S": 1.5, "M": 3.5, "L": 7.5}
PATCH_MAX_LINES, PATCH_MAX_FILES = 80, 4     # an XS ticket whose diff exceeds either gets the normal review
RM_RE = re.compile(r'(<script type="application/json" id="roadmap-data">)(.*?)(</script>)', re.S)
SCRIPT = Path(__file__).resolve()


# ---------------------------------------------------------------- utilities

def die(msg, code=1):
    print(f"CONISO ERROR: {msg}")
    sys.exit(code)


def kill_tree(proc):
    """Stop a process and every process it started, then the process itself.

    `Popen.kill()` stops one process. With `shell=True` that one process is the shell, and what the shell started
    (a check run and its compilers) keeps running with nobody reading its result: on Windows it also keeps the output
    pipe open, so the caller hangs until it ends. Here the whole tree goes: `taskkill /T /F` on Windows, the process
    group elsewhere (`run_limited` starts the command in a group of its own)."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def run_limited(cmd, cwd, shell, timeout, env):
    """Run a command with a time limit. On the limit, stop its whole process tree and return exit code 124 with the
    output it had written so far, followed by a TIMEOUT line.

    Output goes to temporary files, not pipes: a process that outlives the kill cannot keep this call waiting, and
    the output written before the limit is kept for the log."""
    def text_of(fh):
        fh.seek(0)
        return fh.read().decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        extra = {} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, shell=shell,
                                stdout=out, stderr=err, env=env, **extra)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            note_ = f"TIMEOUT after {timeout}s: the process tree was stopped"
            so, se = text_of(out), text_of(err)
            return subprocess.CompletedProcess(cmd, 124, so, se + ("" if not se or se.endswith("\n") else "\n") + note_)
        except BaseException:
            kill_tree(proc)      # Ctrl-C or a kill of this script must not leave the tree running either
            raise
        return subprocess.CompletedProcess(cmd, code, text_of(out), text_of(err))


def run(cmd, cwd=None, shell=False, timeout=None, env=None):
    try:
        if timeout is not None:
            return run_limited(cmd, cwd, shell, timeout, env)
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, shell=shell,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
    except FileNotFoundError as e:
        die(f"command not found: {e}")


def git(args, cwd, check=True, env=None):
    r = run(["git"] + args, cwd=cwd, env=env)
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

    def __init__(self, path=None):
        self.path = path or (L / ".lock")

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


def check_side_effects():
    """(path, untracked) for every file the checks changed in the main checkout after a squash: tracked files that
    now differ from the index, and new files .gitignore does not cover. Merges start from a clean checkout, so
    anything here was written by a check (a formatter run with --write, a build that regenerates a tracked file,
    a test that writes into the repo, a cache folder). Only the tracked ones change what would be committed."""
    out = []
    for ln in git(["status", "--porcelain", "--untracked-files=all"], ROOT, check=False).stdout.splitlines():
        if len(ln) < 4:
            continue
        path = ln[3:].strip().strip('"')
        path = path.split(" -> ")[-1]
        if ln.startswith("??"):
            out.append((path, True))
        elif ln[1] != " ":
            out.append((path, False))
    return out


def undo_side_effects(files):
    """Delete the untracked files a check created; `reset --hard` restores the tracked ones."""
    for path, untracked in files:
        if untracked:
            try:
                (ROOT / path).unlink()
            except OSError:
                pass


SIDE_EFFECTS = "CHECKS MODIFIED FILES"


def side_effect_text(files):
    names = ", ".join(x for x, _ in files[:5]) + (f" +{len(files) - 5} more" if len(files) > 5 else "")
    return (f"{names}. A check rewrote tracked files, so what passed is not what would be committed. Make format and "
            "lint check-only (`prettier --check`, not `--write`), commit generated files in the ticket, or fix a test "
            "that writes into the repo. Then land again.")


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
        if t.get("size") not in ("XS", "S", "M", "L"):
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
    if size == "XS" and eff != "xhigh":
        # A patch: one engineer making the smallest change that meets the acceptance items, then a light reviewer
        # whose budget is sized from the diff. `review` promotes it to the normal review when the diff grows past the limit.
        return {"kind": "solo", "model": model, "engineers": 1, "patch": True}
    if model == "haiku":
        return {"kind": "solo", "model": "haiku", "engineers": 1}
    if size in ("S", "M") and eff != "xhigh":
        # One capable engineer holds an S or M ticket in a single context; splitting it adds seams to stitch.
        return {"kind": "solo", "model": model, "engineers": 1}
    return team_formation(size, model)


def team_formation(size, model):
    n = {"XS": 1, "S": 1, "M": 2, "L": 3}.get(size, 2) + (1 if size == "L" and model == "opus" else 0)
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
    elif t.get("size") == "XS":
        plan = {"kind": "patch", "reviewers": [{"model": "opus" if t.get("model") == "opus" else "sonnet",
                                             "lens": "spec+regression", "agent": "reviewer-lite"}]}
    elif (t.get("model") or "sonnet") == "haiku":
        plan = {"kind": "batch", "reviewers": [{"model": "sonnet", "lens": "spec", "agent": "reviewer-lite"}]}
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
        form = f"{'patch' if f.get('patch') else 'solo'}({f['model']})"
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
    patch = [tid for tid, t in c["tickets"].items() if t["review_plan"]["kind"] == "patch" and t["state"] not in TERMINAL]
    print(f"Review: panel {','.join(panels) or '-'} · batch {','.join(batch) or '-'} · patch {','.join(patch) or '-'} · others single reviewer")
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
    # A pack may also offer defaults for the other checks and for process watching. Yours always win: a default is
    # used only where the config has nothing, and `init --test ''` (an empty value) still clears one.
    for key in ("format_cmd", "lint_cmd", "typecheck_cmd", "test_cmd", "watch_cmd_pattern"):
        if pack.get(key) and key not in conf:
            conf[key] = fill(pack[key])


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
    replaced = []
    old_version = (L / "VERSION").read_text(encoding="utf-8").strip() if (L / "VERSION").exists() else None
    if plugin:
        src = plugin / "skills" / "coniso" / "references"
        if (L / "ref").exists():
            replaced.append(f"ref/ ({sum(1 for x in (L / 'ref').rglob('*') if x.is_file())} reference pages)")
            shutil.rmtree(L / "ref")
        shutil.copytree(src, L / "ref")
        old = L / "coniso.py"
        if SCRIPT != old.resolve():
            if old.exists() and old.read_bytes() != SCRIPT.read_bytes():
                bak = L / "backup" / f"coniso-{old_version or 'unknown'}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.py"
                bak.parent.mkdir(exist_ok=True)
                shutil.copy2(old, bak)
                replaced.append(f"coniso.py {old_version or '?'} -> {VERSION} (the replaced copy is kept at {p(bak)})")
            shutil.copy2(SCRIPT, L / "coniso.py")
    local = L / "local"
    if not local.exists():
        local.mkdir()
        (local / "README.md").write_text(LOCAL_README, encoding="utf-8")
    elif not any((L / "ref").iterdir()):
        print("WARN: run init from the plugin copy once so references get installed into .coniso/ref/")
    if is_git:
        r = git(["rev-parse", "--git-path", "info/exclude"], ROOT)
        ex = (ROOT / r.stdout.strip()).resolve()
        ex.parent.mkdir(parents=True, exist_ok=True)
        text = ex.read_text(encoding="utf-8") if ex.exists() else ""
        if "/.coniso/" not in text.split():
            ex.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "/.coniso/\n", encoding="utf-8")
        # Claude Code stores "don't ask again" approvals there, command strings included, so keep it out of commits
        # whether or not the user lets Coniso write to it.
        exclude_path("/.claude/settings.local.json")
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
    for key in ("test", "build", "setup", "lint", "typecheck", "format"):
        val = getattr(a, key)
        if val is not None:
            conf[f"{key}_cmd"] = val
    for key in ("check_slots", "test_timeout", "merge_timeout"):
        val = getattr(a, key, None)
        if val is not None:
            if val:
                conf[key] = val
            else:
                conf.pop(key, None)
    for key, attr in (("check_process_pattern", "check_pattern"), ("decisions_dir", "decisions_dir"),
                      ("lessons_dir", "lessons_dir")):
        val = getattr(a, attr, None)
        if val is not None:
            if val:
                conf[key] = val
            else:
                conf.pop(key, None)
    if getattr(a, "house_rules", None) is not None:
        conf["house_rules"] = [x for x in a.house_rules.split(",") if x]
    if getattr(a, "land_hold", None) is not None:
        conf["land_hold"] = a.land_hold
    for kv in getattr(a, "env", None) or []:
        k, _, v = kv.partition("=")
        if k:
            env = conf.setdefault("env", {})
            if v:
                env[k] = v
            else:
                env.pop(k, None)
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
    print(f"CONISO {VERSION} ready in {p(L)}" + (f" (was {old_version})" if old_version and old_version != VERSION else ""))
    if replaced:
        print("Replaced: " + "; ".join(replaced))
    c = load_sprint(required=False)
    kept = ([f"sprint {c['name']} ({len(c['tickets'])} tickets, "
             f"{len([1 for t in c['tickets'].values() if t['state'] not in TERMINAL])} open)"] if c else [])
    kept += [d + "/" for d in ("tickets", "assignments", "reports", "maps", "specialties", "archive")
             if (L / d).is_dir() and any((L / d).iterdir())]
    kept += [x for x in ("BRIEF.md", "RETRO.md", "ledger.jsonl", "flaky.jsonl") if (L / x).exists()]
    yours = sorted(x.name for x in L.iterdir() if x.name not in CONISO_OWNED)
    if kept or yours:
        print("Kept: " + ", ".join(kept + ([f"{len(yours)} file(s) of yours ({', '.join(yours[:6])}"
                                            f"{' …' if len(yours) > 6 else ''})"] if yours else [])))
    if yours:
        print("  .coniso/local/ is the folder for your own scripts and notes: init and close never touch it.")
    print(f"Run as: {conf['cmd']}")
    print(f"Mode: {'worktree per ticket' if worktrees else 'in-place (no worktrees)'} · slots {conf['slots']} · roadmap {conf.get('roadmap') or 'none'}")
    print(f"build: {conf.get('build_cmd') or 'unset'} · test: {conf.get('test_cmd') or 'unset'} · setup: {conf.get('setup_cmd') or 'unset'}")
    if stack:
        print(f"Stack: {stack} · checks after build/test: {conf.get('stack_check_cmd') or 'none'}")
    print("Brief: .coniso/BRIEF.md (fill the TBD sections before the first schedule)")
    print_permissions(read_json(L / "config.json", conf) or conf)


# Everything else in .coniso/ is the project's: init and close leave it where it is.
CONISO_OWNED = {"coniso.py", "VERSION", "config.json", "BRIEF.md", "sprint.json", "ledger.jsonl", "RETRO.md",
                "flaky.jsonl", "landing.json", ".lock", "tickets", "assignments", "reports", "maps", "specialties",
                "ref", "wt", "archive", "stack", "slots", "backup", "local", "cache", "__pycache__"}
LOCAL_README = """# .coniso/local

Your project's own scripts and notes for Coniso: helper tools, standing notes for agents, anything Coniso should
never overwrite. `init` (also when it upgrades Coniso) and `close` never write, move or delete anything in here.

Tools that call Coniso should rely only on what `.coniso/ref/contract.md` lists: commands, printed markers, exit
codes and file locations that do not change within a major version.
"""

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
    core, core_bytes = brief_core_lines(), brief_core_bytes()
    if core > BRIEF_CORE_MAX or core_bytes > BRIEF_CORE_MAX_BYTES:
        line("WARN", f"brief core is {core} lines / {core_bytes // 1024} KB (limit {BRIEF_CORE_MAX} lines or "
                     f"{BRIEF_CORE_MAX_BYTES // 1024} KB; every agent reads it on every spawn): trim it, move detail to "
                     ".coniso/maps/*.md and point at it; `brief-trim` moves older Decisions log rows to .coniso/maps/decisions.md")
    stale_maps = stale_map_report()
    if stale_maps:
        line("INFO", f"maps written {MAP_STALE_COMMITS}+ commits ago (re-scout or delete before trusting them): "
                     + ", ".join(stale_maps))
    _, mem_issues = memory_report()
    mem_issues = [x for x in mem_issues if "commits old" not in x]     # stale maps are listed above
    if mem_issues:
        line("INFO", f"memory: {len(mem_issues)} issue(s): " + "; ".join(mem_issues) + " (`memory` for the full report)")
    if int(conf.get("check_slots") or 0) > 0:
        with Lock(slots_dir() / ".lock"):
            st = slot_state(conf)
        line("INFO", f"check slots: {st['in_use']} of {conf['check_slots']} in use"
             + (f" · counting `{conf['check_process_pattern']}` processes" if conf.get("check_process_pattern") else "")
             + (" · land_hold on" if conf.get("land_hold") else "") + (" · QUIET held" if st["quiet"] else ""))
    m = land_marker()
    if m and not pid_alive(m.get("pid")):
        line("WARN", f"INTERRUPTED LAND of {', '.join(m.get('ids', []))} (pid {m.get('pid')} is gone): its branch was "
                     "not moved. The next land recovers it when the checkout is clean; `land --recover` does it now")
    strays = stray_processes(conf)
    if strays:
        line("INFO", f"{len(strays)} process(es) left running with no parent (an earlier session's agents?). "
                     "Stop the ones nobody here started:")
        for x in strays[:8]:
            out.append("        " + x)
    c = load_sprint(required=False)
    if c:
        live = [tid for tid, t in c["tickets"].items() if t["state"] in ACTIVE]
        line("INFO", f"active sprint {c['name']}: {len(live)} ticket(s) in flight; use status / continue")
    stale = [d.name for d in (L / "wt").iterdir() if d.is_dir()] if (L / "wt").exists() else []
    if stale and not c:
        line("WARN", f"leftover worktrees with no sprint: {', '.join(stale)} (wt remove <id> --force)")
    print("\n".join(out))
    sys.exit(0 if ok else 1)


MAP_STALE_COMMITS = 30


def stale_map_report():
    """Scout maps (not the decisions archive) with MAP_STALE_COMMITS or more commits on HEAD since the map was written."""
    d = L / "maps"
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        if f.name == Path(DECISIONS_ARCHIVE).name:
            continue
        since = dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
        r = git(["rev-list", "--count", f"--since={since}", "HEAD"], ROOT, check=False)
        n = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
        if n >= MAP_STALE_COMMITS:
            out.append(f"{f.name} ({n})")
    return out


BRIEF_CORE_MAX = 150
BRIEF_CORE_MAX_BYTES = 12 * 1024    # about 3k tokens; a line count alone is met by ever-longer lines
DECISIONS_ARCHIVE = "maps/decisions.md"


def brief_core_lines():
    """Lines of BRIEF.md above "## Track notes": the part every agent reads."""
    f = L / "BRIEF.md"
    if not f.exists():
        return 0
    n = 0
    for ln in f.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## Track notes"):
            break
        n += 1
    return n


def brief_core_bytes():
    """UTF-8 size of BRIEF.md above "## Track notes"."""
    f = L / "BRIEF.md"
    if not f.exists():
        return 0
    return len(f.read_text(encoding="utf-8").split("\n## Track notes")[0].encode("utf-8"))


def cmd_brief_trim(a):
    """Move Decisions log rows older than the current sprint (or --before) to .coniso/maps/decisions.md."""
    brief = L / "BRIEF.md"
    if not brief.exists():
        die("no .coniso/BRIEF.md")
    cutoff = a.before
    if not cutoff:
        c = load_sprint(required=False)
        if not c:
            die("no active sprint: pass --before YYYY-MM-DD")
        cutoff = c["created"][:10]
    lines = brief.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## Decisions log")), None)
    if start is None:
        die("the brief has no '## Decisions log' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    keep, moved, seen_header = [], [], False
    for ln in lines[start + 1:end]:
        s = ln.strip()
        if s.startswith("|") and not seen_header:
            keep.append(ln)
            if re.fullmatch(r"\|[\s:|-]+\|", s):
                seen_header = True
            continue
        if s.startswith("|"):
            m = re.search(r"\d{4}-\d{2}-\d{2}", s.strip("|").split("|")[0])
            if (m and m.group(0) < cutoff) or is_superseded(ln):
                moved.append(ln)
                continue
        keep.append(ln)
    if not moved:
        print(f"brief-trim: no Decisions log rows dated before {cutoff} · core {brief_core_lines()} lines")
        return
    pointer = (f"Older decisions (still binding): .coniso/{DECISIONS_ARCHIVE}. "
               f"Run `{cfg().get('cmd', 'CONISO')} decisions --grep <term>` before changing an area they cover.")
    if not any(pointer.split(":")[0] in ln for ln in keep):
        while keep and not keep[-1].strip():
            keep.pop()
        keep += [pointer, ""]
    archive = L / DECISIONS_ARCHIVE
    archive.parent.mkdir(parents=True, exist_ok=True)
    head = "" if archive.exists() else ("# Decisions log — archived from BRIEF.md (still binding)\n\n"
                                         "| Stamp | Decision | Binds |\n|-------|----------|-------|\n")
    with archive.open("a", encoding="utf-8") as fh:
        fh.write(head + "\n".join(moved) + "\n")
    brief.write_text("\n".join(lines[:start + 1] + keep + lines[end:]) + "\n", encoding="utf-8")
    print(f"brief-trim: moved {len(moved)} row(s) dated before {cutoff} or superseded to .coniso/{DECISIONS_ARCHIVE} "
          f"· core now {brief_core_lines()} lines")


def is_superseded(row):
    """A Decisions log row retired by prefixing its Decision cell with SUPERSEDED (rows are never deleted)."""
    cells = row.strip().strip("|").split("|")
    return len(cells) > 1 and cells[1].strip().upper().startswith("SUPERSEDED")


def project_conf():
    return read_json(L / "config.json", {}) or {}


def feedback_dir():
    """Where per-specialty lessons live: config lessons_dir, default docs/agent-feedback."""
    return ROOT / (project_conf().get("lessons_dir") or "docs/agent-feedback")


def house_files():
    """Standing rules every engineer and reviewer reads (config house_rules: globs from the repo root)."""
    out = []
    for pat in project_conf().get("house_rules") or []:
        out += [x for x in sorted(ROOT.glob(pat)) if x.is_file()]
    return [p(x) for x in dict.fromkeys(out)]


RECORD_FIELD = r"^\s*[-*]?\s*\**{}\**\s*:?\**\s*:?\s*(.+)$"


def decision_records():
    """One record per file in config decisions_dir, for projects that keep a decision-record folder (one decision per
    file, `# 0098 Title`, a `Status:` line) instead of the brief's Decisions log. A Status that says superseded
    retires the record; an optional `Binds:` line scopes it like the log's Binds cell."""
    d = project_conf().get("decisions_dir")
    folder = ROOT / d if d else None
    if not folder or not folder.is_dir():
        return []
    out = []
    for fp in sorted(folder.glob("*.md")):
        if fp.name.lower() in ("readme.md", "index.md", "template.md"):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        head = text.splitlines()[:40]

        def field(name):
            rx = re.compile(RECORD_FIELD.format(name), re.I)
            return next((m.group(1).strip() for m in map(rx.match, head) if m), "")

        num = re.match(r"(\d+)", fp.name)
        title = next((ln.lstrip("#").strip() for ln in head if ln.startswith("#")), fp.stem)
        title = re.sub(r"^\d+\s*[-:.]?\s*", "", title)
        status = field("status")
        out.append({"num": num.group(1) if num else fp.stem, "title": title, "status": status, "binds": field("binds"),
                    "path": fp.relative_to(ROOT).as_posix(), "text": text,
                    "superseded": bool(re.search(r"supersed", status, re.I))})
    return out


def cited_records(text):
    """Decision numbers a text cites: "decision 0098", "decisions 0065 and 0067", or a link to 0098-*.md."""
    nums = set()
    for m in re.finditer(r"(?i)\bdecisions?\s+((?:\d{3,5}\b(?:\s*(?:,|and|&|or)\s*)?)+)", text):
        nums.update(int(x) for x in re.findall(r"\d{3,5}", m.group(1)))
    nums.update(int(x) for x in re.findall(r"(?<![\w-])(\d{3,5})-[\w.-]+\.md", text))
    return nums


def decision_rows():
    """(source, row) for every data row of the brief's Decisions log and of the archive, in file order."""
    out = []
    brief = L / "BRIEF.md"
    if brief.exists():
        lines = brief.read_text(encoding="utf-8").splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.startswith("## Decisions log")), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            out += [("brief", ln) for ln in lines[start + 1:end]]
    arch = L / DECISIONS_ARCHIVE
    if arch.exists():
        out = [("archive", ln) for ln in arch.read_text(encoding="utf-8").splitlines()] + out
    rows = []
    for src, ln in out:
        s = ln.strip()
        if not s.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|", s) or re.match(r"\|\s*Stamp\s*\|", s):
            continue
        rows.append((src, ln))
    return rows


def cmd_decisions(a):
    """Print binding decisions from the brief and the archive, optionally filtered; superseded rows only with --all."""
    rows = [(s, r) for s, r in decision_rows() if a.all or not is_superseded(r)]
    if a.grep:
        rows = [(s, r) for s, r in rows if a.grep.lower() in r.lower()]
    recs = [x for x in decision_records() if a.all or not x["superseded"]]
    if a.grep:
        recs = [x for x in recs if a.grep.lower() in x["text"].lower()]
    if not rows and not recs:
        print("no matching decisions" if a.grep else "no decisions recorded")
        return
    if rows:
        print("| Stamp | Decision | Binds |  (source)")
        for s, r in rows:
            print(f"{r.rstrip()}  ({s})")
    for x in recs:
        hit = next((ln.strip() for ln in x["text"].splitlines()[1:] if a.grep and a.grep.lower() in ln.lower()), "")
        print(f"[{x['num']}] {x['title']}" + (f" · {x['status'][:80]}" if x["status"] else "") + f"  ({x['path']})"
              + (f"\n    {hit[:160]}" if hit else ""))


def memory_report():
    """(summary lines, issue lines) for the long-lived guidance: lessons, decisions, retro lessons and maps."""
    lines, issues = [], []
    fb = feedback_dir()
    files = sorted(f for f in fb.glob("*.md") if not f.name.endswith(RETIRED_SUFFIX)) if fb.is_dir() else []
    if files:
        parts = []
        for f in files:
            act = [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip().startswith(("-", "*"))]
            ret = f.with_name(f.stem + RETIRED_SUFFIX)
            n_ret = len([x for x in ret.read_text(encoding="utf-8").splitlines() if x.startswith("-")]) if ret.exists() else 0
            unstamped = [x for x in act if not re.match(r"^\s*[-*]\s*\[\d{4}-\d{2}-\d{2}", x)]
            parts.append(f"{f.stem} {len(act)}" + (f" (+{n_ret} retired)" if n_ret else ""))
            if len(act) > FEEDBACK_MAX:
                issues.append(f"{f.name} has {len(act)} lessons (cap {FEEDBACK_MAX}): condense with `lesson {f.stem} --retire`")
            if unstamped:
                issues.append(f"{f.name}: {len(unstamped)} lesson(s) with no date or source; re-add them with `lesson` "
                              "when next touched")
        lines.append("Lessons: " + ", ".join(parts))
    else:
        lines.append("Lessons: none yet (docs/agent-feedback/)")
    recs = decision_records()
    if recs:
        lines.append(f"Decision records: {len(recs)} in {project_conf().get('decisions_dir')} "
                     f"({len([x for x in recs if x['superseded']])} superseded)")
    if project_conf().get("house_rules"):
        hf = house_files()
        lines.append(f"House rules: {len(hf)} file(s) every engineer and reviewer reads")
        if not hf:
            issues.append("config house_rules matches no file")
    rows = decision_rows()
    sup = [r for _, r in rows if is_superseded(r)]
    sup_brief = [r for s, r in rows if s == "brief" and is_superseded(r)]
    undated = [r for _, r in rows if not re.search(r"\d{4}-\d{2}-\d{2}", r.strip().strip("|").split("|")[0])]
    lines.append(f"Decisions: {len([1 for s, r in rows if s == 'brief' and not is_superseded(r)])} in the brief, "
                 f"{len([1 for s, r in rows if s == 'archive' and not is_superseded(r)])} archived, {len(sup)} superseded")
    if sup_brief:
        issues.append(f"{len(sup_brief)} superseded decision(s) still in the brief: run `brief-trim`")
    if undated:
        issues.append(f"{len(undated)} decision row(s) with no YYYY-MM-DD stamp: brief-trim never archives them")
    rf = L / "RETRO.md"
    secs = retro_sections(rf.read_text(encoding="utf-8")) if rf.exists() else []
    with_l = [s for s in secs if section_lessons(s)]
    lines.append(f"Retro: {len(secs)} sprint(s), {len(with_l)} with lessons")
    if len(secs) >= 2 and not section_lessons(secs[-1]) and not section_lessons(secs[-2]):
        issues.append("the last two retros recorded no lessons")
    maps = [f for f in (L / "maps").glob("*.md") if f.name != Path(DECISIONS_ARCHIVE).name] if (L / "maps").is_dir() else []
    stale = stale_map_report()
    lines.append(f"Maps: {len(maps)}" + (f", {len(stale)} stale: {', '.join(stale)}" if stale else ""))
    if stale:
        issues.append(f"{len(stale)} map(s) {MAP_STALE_COMMITS}+ commits old: re-scout or delete")
    return lines, issues


def cmd_memory(a):
    """Health of the long-lived guidance, for setup and phase gates."""
    lines, issues = memory_report()
    print("MEMORY\n" + "\n".join("  " + x for x in lines))
    print("Issues:\n" + "\n".join("  - " + x for x in issues) if issues else "Issues: none")


DECISIONS_INLINE_MAX = 8
# A Binds cell that is exactly one of these binds every ticket. "all P2 tickets" or "project settings UI" does not:
# those are matched by the ticket ids and phases they name.
BINDS_EVERYONE = ("all", "every", "everything", "everyone", "*", "project", "whole project", "all tickets",
                  "every ticket", "all roles", "every role", "all agents", "every agent")


def ticket_decisions(pairs):
    """Binding decision rows whose Binds cell names one of these tickets, its phase, or everyone."""
    keys = set()
    for tid, t in pairs:
        keys.add(tid)
        if t.get("phase"):
            keys.add(t["phase"])
    out = []
    for _, row in decision_rows():
        if is_superseded(row):
            continue
        cells = [x.strip() for x in row.strip().strip("|").split("|")]
        binds = cells[2] if len(cells) > 2 else ""
        whole = " ".join(re.findall(r"[\w*]+", binds.lower()))
        if whole in BINDS_EVERYONE or any(
                re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", binds, re.I) for k in keys):
            out.append(cells)
    recs = [x for x in decision_records() if not x["superseded"]]
    if recs:
        cited = set()
        for tid, _ in pairs:
            pk = L / "tickets" / f"{safe(tid)}.md"
            if pk.exists():
                cited |= cited_records(pk.read_text(encoding="utf-8"))
        for x in recs:
            binds = x["binds"]
            whole = " ".join(re.findall(r"[\w*]+", binds.lower()))
            scoped = binds and (whole in BINDS_EVERYONE or any(
                re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", binds, re.I) for k in keys))
            if scoped or (x["num"].isdigit() and int(x["num"]) in cited):
                out.append([x["num"], x["title"], binds])
    return out


def with_decisions(fields, pairs, cmd):
    """Insert a DECISIONS field after INPUTS when decisions bind these tickets: the few rows an agent must honour,
    inline, so it need not search the whole log."""
    rows = ticket_decisions(pairs)
    if not rows:
        return fields
    shown = "; ".join(f"[{r[0]}] {r[1] if len(r) > 1 else ''}" for r in rows[:DECISIONS_INLINE_MAX])
    more = len(rows) - DECISIONS_INLINE_MAX
    if more > 0:
        shown += f"; +{more} more: `{cmd} decisions --grep {pairs[0][0]}`"
    i = next((k for k, (name, _) in enumerate(fields) if name == "INPUTS"), len(fields) - 1)
    return fields[:i + 1] + [("DECISIONS", shown + " (binding)")] + fields[i + 1:]


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
    if a.field:
        if not a.id:
            die("status --field needs a ticket id")
        val = ticket(c, a.id)
        for part in a.field.split("."):
            if not isinstance(val, dict) or part not in val:
                die(f"{a.id} has no field {a.field}")
            val = val[part]
        print(val if isinstance(val, str) else json.dumps(val, separators=(",", ":")))
        return
    if a.json:
        print(json.dumps(c if not a.id else ticket(c, a.id), separators=(",", ":"), ensure_ascii=False))
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
            if t["review_plan"]["kind"] == "patch":     # an escalated patch is no longer small work
                t["review_plan"] = review_plan(dict(t, size="S"))
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


CHECK_KEYS = ("format_cmd", "lint_cmd", "typecheck_cmd", "build_cmd", "test_cmd", "stack_check_cmd")   # cheap first


FLAKY_AT_MERGE = "FLAKY AT MERGE"


def flaky_records():
    f = L / "flaky.jsonl"
    if not f.exists():
        return []
    out = []
    for x in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(x))
        except ValueError:
            continue
    return out


def check_timeout(conf, gate=False):
    """Seconds one check command may run: merge_timeout at the merge gate when set, else test_timeout."""
    return int((gate and conf.get("merge_timeout")) or conf.get("test_timeout", 1200))


def check_env(conf):
    """The environment for check runs: this process's, plus config `env` (e.g. an SDK path a build needs)."""
    extra = conf.get("env") or {}
    return dict(os.environ, **{k: str(v) for k, v in extra.items()}) if extra else None


def run_checks(target, conf, log, label=None, gate=False):
    """Run the configured checks behind a check slot (see acquire_slot). A land holds its slot for its whole length,
    so checks run inside a land take none. Raises SlotBusy when no slot came free in time."""
    held = None if _LANDING["owner"] else acquire_slot(conf, "test", label or p(target))
    try:
        return run_checks_now(target, conf, log, label, gate)
    finally:
        release_slot(held)


def run_checks_now(target, conf, log, label=None, gate=False):
    """Run the configured checks in order, stopping at the first failure. A failing test command is rerun once
    (unless retry_flaky is off, or the failing run took longer than flaky_retry_max_seconds), and the event is
    recorded in .coniso/flaky.jsonl.
    - At a ticket's own run, a pass on the rerun counts as a pass; the reviewer sees FLAKY and rules on it.
    - At the merge gate (gate=True), it counts only if this test command has flaked before. A race the ticket just
      introduced looks exactly like a flaky test, so the first flake ever seen at a merge fails it (FLAKY AT MERGE).
      It is on record afterwards, so a merge retried after a look passes. Config merge_flaky "pass" turns this off."""
    cmds = [(k, conf[k]) for k in CHECK_KEYS if conf.get(k)]
    if not any(k in ("build_cmd", "test_cmd", "stack_check_cmd") for k, _ in cmds):
        return None, "no build/test command configured"
    text = []
    code = 0
    for key, cmd in cmds:
        t0 = time.monotonic()
        r = run(cmd, cwd=target, shell=True, timeout=check_timeout(conf, gate), env=check_env(conf))
        took = time.monotonic() - t0
        text.append(f"$ {cmd}\n{r.stdout or ''}{r.stderr or ''}\n[exit {r.returncode}]\n")
        code = r.returncode
        cap = conf.get("flaky_retry_max_seconds", 900)
        if code == 124 and "TIMEOUT after" in (r.stderr or ""):
            text.append(f"NO RERUN: the run hit its time limit ({check_timeout(conf, gate)}s: "
                        f"{'merge_timeout' if gate and conf.get('merge_timeout') else 'test_timeout'} in config)\n")
        elif code != 0 and key == "test_cmd" and conf.get("retry_flaky", True) and cap and took > cap:
            text.append(f"NO RERUN: the failing run took {int(took)}s, over flaky_retry_max_seconds ({cap}); "
                        "a real failure is not run twice\n")
        elif code != 0 and key == "test_cmd" and conf.get("retry_flaky", True):
            known = any(x.get("cmd") == cmd for x in flaky_records())
            r2 = run(cmd, cwd=target, shell=True, timeout=check_timeout(conf, gate), env=check_env(conf))
            text.append(f"$ {cmd}   (rerun: flaky check)\n{r2.stdout or ''}{r2.stderr or ''}\n[exit {r2.returncode}]\n")
            if r2.returncode == 0:
                rec = {"ts": now_z(), "where": label or p(target), "cmd": cmd, "first_exit": code, "log": p(log)}
                if gate and not known and conf.get("merge_flaky", "known") != "pass":
                    rec["blocked"] = True
                    text.append(f"{FLAKY_AT_MERGE}: `{cmd}` failed (exit {code}), then passed on rerun. This test command "
                                "has never flaked before, so the merge fails: a race the ticket just introduced looks "
                                "exactly like a flaky test. If the failing test above is unrelated to the ticket, land it "
                                "again (the flake is now on record and passes); if it covers the ticket's code, send it "
                                "to the builder.\n")
                else:
                    text.append(f"FLAKY: `{cmd}` failed (exit {code}) then passed on rerun; recorded in .coniso/flaky.jsonl\n")
                    code = 0
                with (L / "flaky.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            else:
                code = r2.returncode
        if code != 0:
            text.append(f"CHECK FAILED: {key[:-4]}\n")
            break
    log.write_text("\n".join(text), encoding="utf-8")
    return code, "\n".join(text)


# Blocking rules: formats that are a credential by their shape alone, so a hit is almost never a false alarm.
SECRET_PATTERNS = [
    ("private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    ("AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("Stripe live key", r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    ("Supabase service key", r"(?i)service_role[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._-]{30,}"),
]
# Hints: shapes that are often a credential and often not (sample JWTs, local dev URLs, token-ish names). They never
# block; `test` prints them and the reviewer rules on each one.
SECRET_HINTS = [
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("assigned secret", r"(?i)\b[\w-]*(?:api[_-]?key|secret|passw(?:or)?d|token)[\w-]*[\"']?\s*[:=]\s*"
                        r"[\"'](?P<v>[^\"'\s$<{]{16,})[\"']"),
    ("connection string with password", r"\b[a-z][a-z0-9+]*://[^/\s:@]+:[^@\s/]{6,}@[\w.-]+"),
]
# Files that hold credentials by convention. Key stores are binary, so they are caught from the diff's file header.
SECRET_FILES = (r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)|[\w.-]*\.(?:p12|pfx|jks|keystore)"
                r"|credentials\.json|service-account[\w.-]*\.json)$")
SECRET_FILES_OK = r"(?:^|/)\.env\.(?:example|sample|template|dist)$"
SECRET_FILE_HINTS = r"(?:^|/)[\w.-]*\.(?:pem|key|crt)$"    # usually a public certificate; a private key inside still blocks
ALLOW_MARK = "coniso:allow-secret"


def looks_random(value):
    """A token-like value: letters and digits mixed, not a path, URL or plain identifier."""
    return bool(re.search(r"[A-Za-z]", value) and re.search(r"\d", value) and "/" not in value and " " not in value)


def scan_secrets(diff_text, conf):
    """(blocking, hints) in a unified diff: each a list of (file, line no, kind); line 0 means the whole file.
    Added lines are checked against SECRET_PATTERNS (blocking) and SECRET_HINTS; added files by name, binary ones
    included. A line containing `coniso:allow-secret`, or a path matching config secret_allow (regexes), is skipped."""
    allow = [re.compile(x) for x in conf.get("secret_allow", [])]
    block, hints, f, n = [], [], None, 0

    def check_file(path):
        if not path or path == "/dev/null" or any(a.search(path) for a in allow):
            return
        if re.search(SECRET_FILES, path) and not re.search(SECRET_FILES_OK, path):
            block.append((path, 0, "secret-looking file"))
        elif re.search(SECRET_FILE_HINTS, path):
            hints.append((path, 0, "certificate or key file"))

    for ln in diff_text.splitlines():
        if ln.startswith("+++ "):
            f = ln[4:].strip().strip('"')
            f = f[2:] if f.startswith("b/") else (None if f == "/dev/null" else f)
            check_file(f)
            continue
        m = re.match(r"^Binary files .* and (.+) differ$", ln)
        if m:
            path = m.group(1).strip().strip('"')
            check_file(path[2:] if path.startswith("b/") else path)
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", ln)
        if m:
            n = int(m.group(1)) - 1
            continue
        if ln.startswith("+") and f:
            n += 1
            if ALLOW_MARK in ln or any(a.search(f) for a in allow):
                continue
            hit = next((kind for kind, rx in SECRET_PATTERNS if re.search(rx, ln[1:])), None)
            if hit:
                block.append((f, n, hit))
                continue
            for kind, rx in SECRET_HINTS:
                mm = re.search(rx, ln[1:])
                if mm and (kind != "assigned secret" or looks_random(mm.group("v"))):
                    hints.append((f, n, kind))
                    break
        elif not ln.startswith("-"):
            n += 1
    return block, hints


def findings_text(found):
    return "; ".join(f"{f}:{n} {k}" if n else f"{f} ({k})" for f, n, k in found[:10]) \
        + (f"; +{len(found) - 10} more" if len(found) > 10 else "")


def secret_report(found):
    return ("SECRETS: " + findings_text(found)
            + f". Move them to untracked config or env vars. A deliberate fake (a test fixture) gets `{ALLOW_MARK}` "
              "on its line, or its path in config secret_allow.")


def hint_report(hints):
    return ("SECRET HINTS (not blocking): " + findings_text(hints)
            + ". Each looks like it could be a credential. The reviewer rules on each one; a real credential is a blocker.")


TEST_PATH = r"(?:^|/)(?:tests?|__tests__|specs?|testing)/|(?:^|/)test_[^/]*$|[._-](?:test|spec|tests)\.[\w]+$|_test\.\w+$|Tests?\.\w+$"
CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".gd",
            ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".dart", ".lua", ".scala", ".vue", ".svelte", ".sql")


def ticket_diff(t, conf, names=False):
    """A ticket worktree's diff against its base, new files included (text, or changed paths with names=True)."""
    wt = Path(t["wt"])
    git(["add", "-N", "--"] + pathspec_excludes(conf)[2:] + ["."], wt, check=False)
    args = ["diff", "--name-only"] if names else ["diff", "-U0"]
    out = git(args + [t["base"]] + pathspec_excludes(conf), wt, check=False).stdout
    return out.split("\n") if names else out


def test_change_warning(paths):
    """A warning when source files changed and no test file did, else None."""
    paths = [x for x in paths if x]
    tests = [x for x in paths if re.search(TEST_PATH, x)]
    code = [x for x in paths if x.endswith(CODE_EXT) and x not in tests]
    if code and not tests:
        return (f"TESTS UNCHANGED: {len(code)} source file(s) changed and no test file did "
                f"({', '.join(code[:3])}{' …' if len(code) > 3 else ''})")
    return None


def tree_id(path, conf):
    """Git tree id of a checkout's current content: tracked and untracked files that .gitignore allows, minus linked
    dirs and copied files, computed in a scratch index so the real one is untouched. Two checkouts with the same tree
    id hold byte-identical files, so a passing test run on one is evidence for the other."""
    scratch = L / "reports" / f".tree-index-{os.getpid()}"
    env = dict(os.environ, GIT_INDEX_FILE=str(scratch))
    try:
        if git(["read-tree", "HEAD"], path, check=False, env=env).returncode != 0:
            return None
        git(["add", "-A", "--", "."] + pathspec_excludes(conf)[2:], path, check=False, env=env)
        return git(["write-tree"], path, check=False, env=env).stdout.strip() or None
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass


def cached_pass(t, tree):
    """The ticket's recorded passing `test` run for exactly this tree, or None."""
    tc = (t or {}).get("test_cache") or {}
    if tree and tc.get("tree") == tree and tc.get("code") == 0 and Path(tc.get("log") or "").exists():
        return tc
    return None


def cmd_test(a):
    conf = cfg()
    t = None
    if a.id == "main":
        target = ROOT
    else:
        c = load_sprint()
        t = ticket(c, a.id)
        target = Path(t["wt"]) if t.get("wt") else ROOT
    log = L / "reports" / f"{safe(a.id)}.test.log"
    if t is not None and t.get("wt") and t.get("base"):
        found, hints = scan_secrets(ticket_diff(t, conf), conf)
        if found:
            print(f"TEST {a.id}: FAIL (secret scan) · nothing else ran")
            print(secret_report(found))
            sys.exit(1)
        if len(hints) != (t.get("secret_hints") or 0):
            with Lock():
                c = load_sprint()
                ticket(c, a.id)["secret_hints"] = len(hints)
                save_sprint(c)
        if hints:
            print(hint_report(hints))
        warn = test_change_warning(ticket_diff(t, conf, names=True))
        if bool(warn) != bool(t.get("tests_unchanged")):
            with Lock():
                c = load_sprint()
                ticket(c, a.id)["tests_unchanged"] = bool(warn)
                save_sprint(c)
        if warn:
            print(warn + "; a reviewer will ask why unless the acceptance items need no test")
    tree = tree_id(target, conf) if (t is not None and t.get("wt") and conf.get("test_cache", True) and not a.fresh) else None
    hit = cached_pass(t, tree)
    if hit:
        print(f"TEST {a.id}: PASS (cached: tree {tree[:7]} unchanged since its last run at {hit['ts']}; "
              f"`test {a.id} --fresh` reruns) · log {hit['log']}")
        return
    try:
        code, text = run_checks(target, conf, log, label=a.id)
    except SlotBusy as e:
        print(f"TEST {a.id}: BUSY — {e}; run it again")
        sys.exit(9)
    if code is None:
        print(f"TEST {a.id}: SKIPPED — {text}")
        sys.exit(2)
    if tree:
        after = tree_id(target, conf)
        if after and after != tree:
            changed = git(["diff", "--name-only", "--diff-filter=MD", tree, after], target, check=False).stdout.split()
            if changed:
                print(f"WARN: checks changed tracked files while they ran: {', '.join(changed[:5])}"
                      f"{' …' if len(changed) > 5 else ''}. Review the change; at merge it fails the land. Make format "
                      "and lint check-only, or fix the test that writes files.")
            tree = None     # the pass does not cover the tree as it is now: do not reuse it
    if tree and code == 0:
        with Lock():
            c = load_sprint()
            ticket(c, a.id)["test_cache"] = {"tree": tree, "code": 0, "ts": now_z(), "log": p(log)}
            save_sprint(c)
    tail = text.strip().splitlines()[-(5 if code == 0 else 25):]
    flaky = " · FLAKY (failed, then passed on rerun)" if code == 0 and "FLAKY:" in text else ""
    print(f"TEST {a.id}: {'PASS' if code == 0 else 'FAIL'} (exit {code}){flaky} · log {p(log)}")
    print("\n".join(tail))
    sys.exit(0 if code == 0 else 1)


def cmd_sync(a):
    """Bring the main branch into a ticket's worktree (conflict resolution or pre-review refresh). It merges the
    branch, which a land moves only after its checks pass, so it never brings in untested commits."""
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    if not t.get("wt"):
        die(f"{a.id} has no worktree")
    if t["state"] == "reviewing" and not a.cont and not getattr(a, "force", False):
        die(f"{a.id} is in review: a sync now changes the files under its reviewer (one left conflict markers that "
            f"way). Wait for the verdict, or pass --force if no reviewer is running.", code=8)
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


# ---------------------------------------------------------------- check slots, quiet runs, leftover processes
# A check run can need most of a machine (one field project: gigabytes of memory and every core for tens of minutes). These keep
# the runs within a cap, let a land go first, hold the machine still for timing runs, and show what an earlier
# session left running. All of it is off until config turns it on (check_slots, check_process_pattern, land_hold).

SLOTS = "slots"
QUIET_FLAG = "QUIET"
DEFAULT_WATCH = (r"(?i)^(python[\d.]*w?|py|node|cargo|rustc|cl|link|lld-link|clang|msbuild|dotnet|go|java|gradle|"
                 r"npm|npx|pnpm|yarn|pytest|ninja|cmake|make|gcc|g\+\+)(\.exe)?$")
DEFAULT_QUIET_IGNORE = (r"(?i)^(idle|system|system idle process|registry|memory compression|secure system|dwm|csrss|"
                        r"svchost|msmpeng|searchindexer|searchhost|wmiprvse|audiodg|taskmgr)(\.exe)?$")


class SlotBusy(Exception):
    pass


def slots_dir():
    d = L / SLOTS
    d.mkdir(parents=True, exist_ok=True)
    return d


def etime_seconds(text):
    """ps etime/time: [[dd-]hh:]mm:ss[.cc] -> seconds."""
    days, _, rest = text.strip().rpartition("-")
    parts = [float(x) for x in rest.split(":")]
    secs = 0.0
    for x in parts:
        secs = secs * 60 + x
    return secs + (int(days) * 86400 if days.isdigit() else 0)


PS_PROCS = ("Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}|{2}|{3}|{4}' -f $_.ProcessId, $_.ParentProcessId, "
            "$(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }), $_.Name, "
            "(($_.CommandLine -replace '\\s+',' ')) }")


def proc_list():
    """Every process as {pid, ppid, start (epoch s), name, cmd}; [] when the list cannot be read."""
    out = []
    if os.name == "nt":
        r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_PROCS], timeout=120)
        for line in (r.stdout or "").splitlines():
            parts = line.split("|", 4)
            if len(parts) == 5 and parts[0].strip().isdigit():
                try:
                    ft = int(parts[2])
                    start = (ft - 116444736000000000) / 1e7 if ft else 0.0
                except ValueError:
                    start = 0.0
                out.append({"pid": int(parts[0]), "ppid": int(parts[1]) if parts[1].strip().isdigit() else -1,
                            "start": start, "name": parts[3].strip(), "cmd": parts[4].strip()})
        return out
    r = run(["ps", "-A", "-o", "pid=,ppid=,etime=,args="], timeout=60)
    now = time.time()
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit():
            cmd = parts[3] if len(parts) > 3 else ""
            try:
                start = now - etime_seconds(parts[2])
            except ValueError:
                start = 0.0
            out.append({"pid": int(parts[0]), "ppid": int(parts[1]) if parts[1].isdigit() else -1, "start": start,
                        "name": os.path.basename(cmd.split()[0]) if cmd.split() else "", "cmd": cmd})
    return out


def family(procs, pid):
    """This process's ancestors and descendants (and itself), by pid."""
    parent = {pr["pid"]: pr["ppid"] for pr in procs}
    mine, x = set(), pid
    while x not in mine:
        mine.add(x)
        if x not in parent:
            break
        x = parent[x]
    children = {}
    for pr in procs:
        children.setdefault(pr["ppid"], []).append(pr["pid"])
    stack = [pid]
    while stack:
        for ch in children.get(stack.pop(), []):
            if ch not in mine:
                mine.add(ch)
                stack.append(ch)
    return mine


def check_runs(conf, procs):
    """Processes whose command line matches config check_process_pattern: check runs started by hand or by another
    tool, which no Coniso claim covers. One per run: a match started by another match belongs to it."""
    pat = conf.get("check_process_pattern")
    if not pat:
        return []
    rx = re.compile(pat, re.I)
    found = [pr for pr in procs if pr["pid"] != os.getpid() and rx.search(pr["cmd"] or "")]
    ids = {pr["pid"] for pr in found}
    return [pr for pr in found if pr["ppid"] not in ids]


def quiet_owner():
    """The quiet run holding the machine ({pid, ts, cmd}), or None. A flag whose process died is removed."""
    f = L / SLOTS / QUIET_FLAG
    if not f.exists():
        return None
    try:
        q = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        q = {}
    if pid_alive(q.get("pid")):
        return q
    try:
        f.unlink()
    except OSError:
        pass
    return None


def slot_state(conf, me=None):
    """What holds the check slots now. Claims of processes that died are removed. A Coniso run whose check process
    shows up in check_process_pattern is counted once, as that process."""
    procs = proc_list() if conf.get("check_process_pattern") else []
    runs = check_runs(conf, procs)
    ttl = float(conf.get("check_claim_seconds", 150))
    now = time.time()
    starts = sorted(r["start"] for r in runs)
    claims = []
    for fp in sorted(slots_dir().glob("claim-*.json")):
        try:
            c = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        c["file"] = fp
        if c.get("kind") == "hand":       # `slot`: lives until its check run shows up, or for check_claim_seconds
            hit = next((x for x in starts if x >= c.get("ts", 0) - 2.0), None)
            dead = now - c.get("ts", 0) > ttl or hit is not None
            if hit is not None:
                starts.remove(hit)
        else:
            dead = not pid_alive(c.get("pid"))
        if dead:
            try:
                fp.unlink()
            except OSError:
                pass
        elif fp != me:
            claims.append(c)
    parent = {pr["pid"]: pr["ppid"] for pr in procs}

    def under(pid, root):
        seen = set()
        while pid not in seen:
            if pid == root:
                return True
            seen.add(pid)
            if pid not in parent:
                return False
            pid = parent[pid]
        return False

    running = [c for c in claims if c.get("state") == "running"]
    covered = {c["pid"] for c in running if any(under(r["pid"], c["pid"]) for r in runs)}
    hand = [c for c in claims if c.get("kind") == "hand"]
    in_use = len(runs) + len([c for c in running if c["pid"] not in covered]) + len(hand)
    return {"in_use": in_use, "runs": runs, "claims": claims, "quiet": quiet_owner(),
            "land_waiting": any(c.get("kind") == "land" and c.get("state") == "waiting" for c in claims),
            "land_running": any(c.get("kind") == "land" and c.get("state") == "running" for c in claims)}


def slot_refusal(conf, kind, st):
    """Why a check run of this kind may not start now, or None."""
    limit = int(conf.get("check_slots") or 0)
    if st["quiet"]:
        return f"a quiet run holds the machine (pid {st['quiet'].get('pid')}: {str(st['quiet'].get('cmd'))[:60]})"
    if limit <= 0:
        return None
    if kind == "land":
        most = max(0, min(int(conf.get("land_max_others", 1)), limit - 1))
        if st["in_use"] > most:
            return f"{st['in_use']} check run(s) active; a land starts when {most} or fewer are"
        return None
    if st["land_waiting"]:
        return "a land is waiting for a slot and goes first"
    if st["land_running"] and conf.get("land_hold"):
        return "a land is running and holds new check runs (land_hold)"
    if st["in_use"] >= limit:
        return f"{st['in_use']} of {limit} check slot(s) in use"
    return None


def acquire_slot(conf, kind, label):
    """Wait for a check slot and claim it; returns the claim file (release it with release_slot), or None when slots
    are off. kind "land" goes ahead of waiting tests and starts once at most land_max_others runs are active; with
    land_hold, no new test starts while a land runs. Claims are written under a lock, so waiting callers cannot all
    see a free slot at once and start together. Raises SlotBusy after check_slot_wait_seconds (a land:
    land_slot_wait_seconds)."""
    if int(conf.get("check_slots") or 0) <= 0 and not (L / SLOTS / QUIET_FLAG).exists():
        return None
    d = slots_dir()
    me = d / f"claim-{kind}-{os.getpid()}-{int(time.time() * 1000)}.json"
    write_json(me, {"pid": os.getpid(), "kind": kind, "state": "waiting", "ts": time.time(), "label": label})
    wait = float(conf.get("land_slot_wait_seconds", 7200) if kind == "land" else conf.get("check_slot_wait_seconds", 540))
    poll = float(conf.get("check_slot_poll_seconds", 20))
    deadline, said, why = time.time() + wait, 0.0, None
    try:
        while True:
            with Lock(d / ".lock"):
                why = slot_refusal(conf, kind, slot_state(conf, me))
                if why is None:
                    data = json.loads(me.read_text(encoding="utf-8"))
                    data.update(state="running", ts_run=time.time())
                    write_json(me, data)
                    return me
            if time.time() >= deadline:
                raise SlotBusy(f"{why}; waited {int(wait)}s")
            if time.time() - said > 300:
                print(f"waiting for a check slot: {why}", flush=True)
                said = time.time()
            time.sleep(poll)
    except BaseException:
        try:
            me.unlink()
        except OSError:
            pass
        raise


def release_slot(claim):
    if claim:
        try:
            claim.unlink()
        except OSError:
            pass


def cmd_slot(a):
    """For a check run started by hand: wait for a slot and hold it for check_claim_seconds, until the run shows up."""
    conf = cfg()
    if a.now:
        with Lock(slots_dir() / ".lock"):
            st = slot_state(conf)
        why = slot_refusal(conf, "test", st)
        if why:
            print(f"{'QUIET' if st['quiet'] else 'BUSY'} {why} (report only: no slot taken)")
            sys.exit(9)
        print(f"GO {st['in_use']} check run(s) active (report only: no slot taken)")
        return
    if int(conf.get("check_slots") or 0) <= 0 and not quiet_owner():
        print("GO check slots are off (check_slots 0 in config)")
        return
    try:
        claim = acquire_slot(conf, "test", "hand")
    except SlotBusy as e:
        print(f"BUSY {e}; run it again")
        sys.exit(9)
    data = json.loads(claim.read_text(encoding="utf-8"))
    data.update(kind="hand", ts=time.time())
    write_json(claim, data)
    print(f"GO the slot is yours for {int(conf.get('check_claim_seconds', 150))}s: start your check run now")


def cpu_sample(seconds):
    """{pid: (name, CPU seconds used during the sample)} for every process that used any."""
    if os.name == "nt":
        script = ("$a=@{}; Get-Process | ForEach-Object { $a[$_.Id]=$_.CPU }; Start-Sleep -Milliseconds %d; "
                  "Get-Process | ForEach-Object { if ($_.CPU -ne $null -and $a[$_.Id] -ne $null) "
                  "{ '{0}|{1}|{2}' -f $_.Id, $_.Name, ($_.CPU - $a[$_.Id]) } }") % int(seconds * 1000)
        r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=120)
        out = {}
        for line in (r.stdout or "").splitlines():
            parts = line.split("|")
            if len(parts) == 3 and parts[0].strip().isdigit():
                try:
                    used = float(parts[2].replace(",", "."))
                except ValueError:
                    continue
                if used > 0:
                    out[int(parts[0])] = (parts[1].strip(), used)
        return out

    def snap():
        res, proc = {}, Path("/proc")
        if proc.is_dir():
            tick = os.sysconf("SC_CLK_TCK")
            for d in proc.iterdir():
                if d.name.isdigit():
                    try:
                        st = (d / "stat").read_text()
                        fields = st[st.rindex(")") + 2:].split()
                        res[int(d.name)] = (st[st.index("(") + 1:st.rindex(")")], (int(fields[11]) + int(fields[12])) / tick)
                    except (OSError, ValueError, IndexError):
                        pass
            return res
        r = run(["ps", "-A", "-o", "pid=,time=,comm="], timeout=60)
        for line in (r.stdout or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                try:
                    res[int(parts[0])] = (os.path.basename(parts[2]), etime_seconds(parts[1]))
                except ValueError:
                    pass
        return res

    a1 = snap()
    time.sleep(seconds)
    a2 = snap()
    return {pid: (n, t - a1[pid][1]) for pid, (n, t) in a2.items() if pid in a1 and t - a1[pid][1] > 0}


def busy_processes(conf):
    """Processes outside this one's family that used more than quiet_busy_cpu CPU seconds in one sample of
    quiet_sample_seconds: any program, a stray `python -` or the user's own apps included, not only builds."""
    procs = proc_list()
    mine = family(procs, os.getpid())
    secs = float(conf.get("quiet_sample_seconds", 3))
    sample = cpu_sample(secs)
    ign = re.compile(conf.get("quiet_ignore") or DEFAULT_QUIET_IGNORE)
    thr = float(conf.get("quiet_busy_cpu", 0.3))
    cmd = {pr["pid"]: pr["cmd"] for pr in procs}
    busy = []
    for pid, (name, used) in sorted(sample.items(), key=lambda x: -x[1][1]):
        if pid in mine or pid == 0 or used < thr or ign.search(name or ""):
            continue
        busy.append(f"pid {pid} {name}: {used:.2f}s CPU in {secs:g}s · {(cmd.get(pid) or '')[-100:]}")
    return busy


def cmd_quiet(a):
    """A timing run on a quiet machine: hold new check runs, wait for running ones to end and for the machine to go
    quiet, then run the command N times, keeping the busy-process list before and after each run. A run disturbed by
    another busy process is not counted."""
    conf = cfg()
    command = [x for x in (a.command or []) if x != "--"]
    if not command:
        die("quiet needs a command after --, e.g. quiet --runs 5 -- python bench.py")
    owner = quiet_owner()
    if owner:
        die(f"a quiet run is already going (pid {owner.get('pid')})", code=8)
    out = Path(a.out) if a.out else L / "reports" / f"quiet-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    cwd = Path(a.cwd) if a.cwd else ROOT
    poll = float(conf.get("check_slot_poll_seconds", 20))
    log = (out / "summary.txt").open("a", encoding="utf-8")

    def say(text):
        line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {text}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    flag = slots_dir() / QUIET_FLAG
    write_json(flag, {"pid": os.getpid(), "ts": now_z(), "cmd": " ".join(command)})
    code = 9
    try:
        say(f"QUIET held: no new check run starts. Command: {' '.join(command)} in {p(cwd)}")
        deadline, said = time.time() + float(conf.get("quiet_wait_seconds", 4500)), 0.0
        while True:
            with Lock(slots_dir() / ".lock"):
                st = slot_state(conf)
            if st["in_use"] == 0:
                break
            if time.time() > deadline:
                say(f"GAVE UP waiting for {st['in_use']} check run(s) to end")
                return sys.exit(9)
            if time.time() - said > 300:
                say(f"waiting for {st['in_use']} check run(s) to end")
                said = time.time()
            time.sleep(poll)
        deadline = time.time() + float(conf.get("quiet_settle_seconds", 2400))
        while True:
            busy = busy_processes(conf)
            if not busy:
                break
            if time.time() > deadline:
                say(f"GAVE UP waiting for a quiet machine; still busy: " + "; ".join(busy[:5]))
                return sys.exit(9)
            say(f"waiting, {len(busy)} busy process(es): {busy[0]}")
            time.sleep(max(poll, 1.0))
        say("machine quiet: no busy process outside this run")
        counted = passed = attempt = 0
        while counted < a.runs and attempt < a.runs + 3:
            attempt += 1
            before = busy_processes(conf)
            t0 = time.monotonic()
            r = run(command, cwd=cwd, timeout=int(conf.get("quiet_run_timeout", 1800)))
            took = time.monotonic() - t0
            after = busy_processes(conf)
            (out / f"run-{attempt}.txt").write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""), encoding="utf-8")
            (out / f"run-{attempt}.processes.txt").write_text(
                "busy before:\n" + ("\n".join(before) or "(none)") + "\n\nbusy after:\n" + ("\n".join(after) or "(none)") + "\n",
                encoding="utf-8")
            disturbed = before + after
            say(f"run {attempt}: exit {r.returncode} in {took:.1f}s"
                + (f" · DISTURBED by {len(disturbed)} busy process(es), not counted: {disturbed[0][:90]}" if disturbed
                   else " · quiet before and after"))
            if not disturbed:
                counted += 1
                passed += 1 if r.returncode == 0 else 0
        say(f"QUIET RUNS {passed} of {counted} counted run(s) exited 0 · {attempt - counted} disturbed · results {p(out)}")
        code = 0 if counted == a.runs and passed == counted else 1
    finally:
        try:
            flag.unlink()
        except OSError:
            pass
        log.close()
    sys.exit(code)


def stray_processes(conf, raw=False):
    """Build, test and tool processes whose parent is gone: usually left by an agent of an earlier session (a check run
    whose agent stopped, a `python -` waiting on input). Names come from config watch_processes ("" turns it off);
    anything matching check_process_pattern counts too. Lists only; never stops anything."""
    pat = conf.get("watch_processes", DEFAULT_WATCH)
    if not pat:
        return []
    procs = proc_list()
    pids = {pr["pid"] for pr in procs}
    mine = family(procs, os.getpid())
    names = re.compile(pat)
    min_age = float(conf.get("stray_min_age_seconds", 60))
    runs = re.compile(conf["check_process_pattern"], re.I) if conf.get("check_process_pattern") else None
    paths = re.compile(conf["watch_cmd_pattern"], re.I) if conf.get("watch_cmd_pattern") else None
    now, out = time.time(), []
    for pr in procs:
        if pr["pid"] in mine or pr["ppid"] in pids or (pr["start"] and now - pr["start"] < min_age):
            continue
        if (pr["name"] or "").lower() == "msbuild.exe" and "nodereuse:true" in (pr["cmd"] or "").lower():
            continue      # an idle MSBuild helper node: left running by design
        if (names.search(pr["name"] or "") or (runs and runs.search(pr["cmd"] or ""))
                or (paths and paths.search(pr["cmd"] or ""))):
            age = f"{int((now - pr['start']) // 60)} min" if pr["start"] else "age unknown"
            text = f"pid {pr['pid']} {pr['name']} ({age}, parent gone): {(pr['cmd'] or '')[-90:]}"
            out.append((pr["pid"], text) if raw else text)
    return out


def cmd_sweep(a):
    """List the processes preflight calls leftovers (a watched build, test or tool process whose parent is gone and
    that is older than stray_min_age_seconds). With --stop, stop each one and everything it started. A process with a
    living parent is never touched, so a running agent's check run is safe; one started by hand in a terminal that
    was closed is not, which is why stopping is a flag and not the default."""
    conf = cfg()
    strays = stray_processes(conf, raw=True)
    if not strays:
        print("SWEEP nothing left running")
        return
    stopped = 0
    for pid, text in strays:
        if a.stop:
            if os.name == "nt":
                r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                ok = r.returncode == 0
            else:
                try:
                    os.kill(pid, 9)
                    ok = True
                except OSError:
                    ok = False
            stopped += ok
            print(f"  {'stopped' if ok else 'could not stop'} {text}")
        else:
            print(f"  {text}")
    print(f"SWEEP {stopped} of {len(strays)} leftover process(es) stopped" if a.stop else
          f"SWEEP {len(strays)} leftover process(es) · `sweep --stop` stops them and what they started")


LANDING = "landing.json"
_LANDING = {"owner": False}


def pid_alive(pid):
    """Whether a process with this id is running. Never signals it (os.kill(pid, 0) would kill it on Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k.GetLastError() == 5               # access denied: it exists
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def land_marker():
    return read_json(L / LANDING, None)


def recover_land(m, force=False):
    """Put the main checkout back after a land whose process died: on the branch, which a land moves only after its
    checks pass, so the branch itself needs nothing. Tickets the dead land recorded as merged whose commit never
    reached the branch go back to passed."""
    branch = m.get("branch") or "HEAD"
    dirty = main_dirty(ignore=[LANDING])
    if dirty and not force:
        die(f"an interrupted land (pid {m.get('pid')}, {', '.join(m.get('ids', []))}) left the main checkout with "
            f"changes ({dirty[0][3:]} …). If none of them are yours, run `land --recover`: it discards them and puts "
            f"the checkout back on {branch}.", code=8)
    if dirty or current_branch() != branch:
        git(["checkout", "-q", "-f", branch], ROOT, check=False)
        git(["reset", "--hard", "-q", branch], ROOT, check=False)
    back = []
    with Lock():
        c = load_sprint(required=False)
        if c:
            for tid in m.get("ids", []):
                t = c["tickets"].get(tid)
                if t and t["state"] == "merged" and t.get("merged_sha") and git(
                        ["merge-base", "--is-ancestor", t["merged_sha"], branch], ROOT, check=False).returncode != 0:
                    t["state"], t["merged_sha"] = "passed", None
                    note(t, "passed", "interrupted land recovered: its commit never reached the branch")
                    back.append(tid)
            save_sprint(c)
    try:
        (L / LANDING).unlink()
    except FileNotFoundError:
        pass
    print(f"RECOVERED an interrupted land (pid {m.get('pid')}): {branch} was not moved by it"
          + (f"; back to passed: {', '.join(back)}" if back else ""))


class landing:
    """Held for the length of a land or a merge: one at a time, and a land whose process died is recovered first.
    Nested use (land calling merge) is a no-op."""

    def __init__(self, ids, conf):
        self.ids, self.conf, self.nested, self.slot = ids, conf, False, None

    def __enter__(self):
        if _LANDING["owner"]:
            self.nested = True
            return self
        m = land_marker()
        if m:
            if pid_alive(m.get("pid")) and m.get("pid") != os.getpid():
                die(f"a land is running (pid {m['pid']}: {', '.join(m.get('ids', []))}, since {m.get('ts')}); "
                    "land after it ends", code=8)
            recover_land(m)
        c = load_sprint(required=False) or {}
        write_json(L / LANDING, {"pid": os.getpid(), "ids": self.ids, "branch": c.get("branch") or current_branch(),
                                 "start": git(["rev-parse", "HEAD"], ROOT, check=False).stdout.strip(), "ts": now_z()})
        _LANDING["owner"] = True
        try:
            self.slot = acquire_slot(self.conf, "land", " ".join(self.ids)) if self.conf.get("worktrees", True) else None
        except SlotBusy as e:
            self.__exit__(None, None, None)
            print(f"BUSY land {' '.join(self.ids)}: {e}; nothing was touched, land again")
            sys.exit(9)
        return self

    def __exit__(self, *exc):
        if self.nested:
            return False
        _LANDING["owner"] = False
        try:
            (L / LANDING).unlink()
        except FileNotFoundError:
            pass
        release_slot(self.slot)
        return False


def cmd_merge(a):
    with landing([a.id], cfg()):
        merge_one(a)


def merge_one(a):
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
    if c.get("branch") and current_branch() != c["branch"] and not getattr(a, "detached_ok", False):
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
    found, hints = scan_secrets(git(["diff", "--cached", "-U0"], ROOT, check=False).stdout, conf)
    if hints:
        with Lock():
            c2 = load_sprint()
            note(ticket(c2, a.id), c2["tickets"][a.id]["state"], "secret hints at merge (not blocking): " + findings_text(hints))
            save_sprint(c2)
    if found and not getattr(a, "allow_secrets", False):
        git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
        mark("failed", f"secret scan: {len(found)} finding(s)")
        print(f"SECRETS BLOCKED {a.id}: main reset, nothing committed")
        print(secret_report(found))
        print(f"Send the builder the list. `merge {a.id} --allow-secrets` only on the user's say-so.")
        sys.exit(6)
    checks = "skipped (--no-test)"
    if not a.no_test:
        log = L / "reports" / f"{safe(a.id)}.merge-test.log"
        tree = git(["write-tree"], ROOT, check=False).stdout.strip() if conf.get("test_cache", True) and not getattr(a, "fresh", False) else ""
        hit = cached_pass(t, tree)
        if hit:     # the squash produced exactly the tree the ticket's last passing `test` ran on
            checks = f"PASS (cached: tree {tree[:7]} already tested at {hit['ts']}, log {hit['log']})"
        else:
            code, text = run_checks(ROOT, conf, log, label=f"merge {a.id}", gate=True)
            side = check_side_effects()
            created = [x for x in side if x[1]]
            side = [x for x in side if not x[1]]
            undo_side_effects(created)     # new untracked files cannot change what is committed, only dirty main
            if created:
                print(f"Removed {len(created)} untracked file(s) the checks created "
                      f"({', '.join(x for x, _ in created[:3])}{' …' if len(created) > 3 else ''}); gitignore them")
            if side:
                git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
                with Lock():
                    c2 = load_sprint()
                    note(ticket(c2, a.id), c2["tickets"][a.id]["state"],
                         f"{SIDE_EFFECTS.lower()}: {', '.join(x for x, _ in side[:5])}")
                    save_sprint(c2)
                print(f"{SIDE_EFFECTS} {a.id}: main reset, nothing committed; the ticket keeps its state · log {p(log)}")
                print(side_effect_text(side))
                if code not in (None, 0):
                    print("The checks also failed:\n" + "\n".join(text.strip().splitlines()[-12:]))
                sys.exit(7)
            if code not in (None, 0) and FLAKY_AT_MERGE in text:
                git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
                with Lock():
                    c2 = load_sprint()
                    note(ticket(c2, a.id), c2["tickets"][a.id]["state"], f"{FLAKY_AT_MERGE.lower()}; log {p(log)}")
                    save_sprint(c2)
                print(f"{FLAKY_AT_MERGE} {a.id}: main reset, nothing committed; the ticket keeps its state · log {p(log)}")
                print("\n".join(text.strip().splitlines()[-20:]))
                sys.exit(4)
            if code not in (None, 0):
                git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
                mark("failed", f"integration checks failed on {c.get('branch')} after squash; log {p(log)}")
                print(f"INTEGRATION FAIL {a.id} (exit {code}) · log {p(log)} · main reset, nothing committed")
                print("\n".join(text.strip().splitlines()[-20:]))
                sys.exit(4)
            checks = "PASS" if code == 0 else "none configured"
    msg = f"{a.id}: {t['title']}" if not t.get("adhoc") else f"{t['title']} ({a.id})"
    r = git(["commit", "-q", "-m", msg, "-m", f"Coniso sprint {c['name']}"], ROOT, check=False)
    if r.returncode != 0:
        git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
        mark("failed", f"commit refused: {(r.stderr or r.stdout).strip()[:200]}")
        die(f"commit refused (hook?): {(r.stderr or r.stdout).strip()[:300]}", code=5)
    sha = git(["rev-parse", "HEAD"], ROOT).stdout.strip()
    stat = git(["show", "--stat", "--format=", "HEAD"], ROOT).stdout.strip().splitlines()
    mark("merged", f"merged as {sha[:7]}; checks {checks}", sha)
    if not a.keep:
        cmd_wt(argparse.Namespace(action="remove", id=a.id, force=True))
    print(f"MERGED {a.id} as {sha[:7]} on {c.get('branch')} · {stat[-1].strip() if stat else ''} · checks {checks}")
    ready = next_ready(load_sprint())
    if ready:
        print(f"Now ready: {', '.join(ready)}")


def cmd_revert(a):
    """Undo a merged ticket: a revert commit of its squash on the sprint branch, and the ticket back to queued."""
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    sha = t.get("merged_sha")
    if t["state"] != "merged" or not sha:
        die(f"{a.id} is {t['state']}; only a merged ticket can be reverted")
    if not a.reason or not a.reason.strip():
        die("say why: --reason \"<what broke>\"")
    after = [x for x, tt in c["tickets"].items() if a.id in tt.get("deps", []) and tt["state"] == "merged"]
    if after and not a.force:
        die(f"merged tickets depend on {a.id}: {', '.join(after)}. Revert them first, or pass --force on the user's say-so")
    if conf.get("worktrees", True):
        dirty = main_dirty()
        if dirty:
            die(f"main checkout has uncommitted changes ({dirty[0][3:]} …); commit or stash them first")
        if c.get("branch") and current_branch() != c["branch"]:
            die(f"main checkout is on {current_branch()} but the sprint merges into {c['branch']}")
        r = git(["revert", "--no-commit", sha], ROOT, check=False)
        if r.returncode != 0:
            git(["revert", "--abort"], ROOT, check=False)
            git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
            die(f"revert of {sha[:7]} conflicts with later work: {(r.stderr or r.stdout).strip()[:200]}", code=3)
        road_msg, road_rel = revert_roadmap(conf, c, a.id, sha, a.reason.strip())
        if road_rel:
            git(["add", "--", road_rel], ROOT, check=False)     # one commit: the code revert and the roadmap note
        r = git(git_identity_args(ROOT) + ["commit", "-q", "-m", f"Revert {a.id}: {t['title']}",
                                           "-m", f"Reason: {a.reason.strip()}", "-m", f"Reverts {sha}. Coniso sprint {c['name']}"],
                ROOT, check=False)
        if r.returncode != 0:
            git(["reset", "--hard", "-q", "HEAD"], ROOT, check=False)
            die(f"commit refused (hook?): {(r.stderr or r.stdout).strip()[:300]}", code=5)
        rsha = git(["rev-parse", "HEAD"], ROOT).stdout.strip()
        if git(["rev-parse", "--verify", "--quiet", f"refs/heads/coniso/{safe(a.id)}"], ROOT, check=False).returncode == 0:
            with redirect_out():
                cmd_wt(argparse.Namespace(action="remove", id=a.id, force=True))
    else:
        rsha = None
        road_msg, _ = revert_roadmap(conf, c, a.id, sha, a.reason.strip())
    with Lock():
        c = load_sprint()
        t = ticket(c, a.id)
        t.setdefault("reverts", []).append({"ts": now_z(), "merged_sha": sha, "revert_sha": rsha, "reason": a.reason.strip()})
        for k in ("merged_sha", "verdict", "wt", "branch", "base"):
            t[k] = None
        for k in ("test_cache", "review_round", "tests_unchanged"):
            t.pop(k, None)
        t["state"] = "queued"
        t["attempts"] += 1
        note(t, "queued", f"reverted {sha[:7]}" + (f" as {rsha[:7]}" if rsha else "") + f": {a.reason.strip()}")
        save_sprint(c)
    print(f"REVERTED {a.id}: {sha[:7]} undone" + (f" by {rsha[:7]}" if rsha else " (in-place mode: undo the files by hand)")
          + f" · ticket back to queued (attempt {t['attempts'] + 1})")
    if road_msg:
        print(road_msg)
    print(f"Run `test main`. Before re-dispatching, put the reason in the packet or a fix list, and consider "
          f"`lesson <specialty> \"<rule>\" --source {a.id}` so it does not happen again.")


def revert_roadmap(conf, c, tid, sha, reason):
    """Put a reverted roadmap ticket back to todo with a Claude: note; the caller commits it with the revert.
    A ticket the user marked done keeps its status: it gets the note, and the user is told.
    Returns (message or None, the roadmap's repo-relative path when written, else None)."""
    t = c["tickets"].get(tid) or {}
    rp = ROOT / conf["roadmap"] if conf.get("roadmap") else None
    if t.get("adhoc") or not rp or not rp.exists():
        return None, None
    html, m, data = load_roadmap(rp)
    rt = next((x for x in data.get("tickets", []) if x.get("id") == tid), None)
    if rt is None:
        return None, None
    n = (f"Claude: Coniso sprint {c['name']} reverted {sha[:7]} ({dt.date.today().isoformat()}): {reason}. "
         "Back in the sprint queue.")
    done = rt.get("status") == "done"
    if not done:
        rt["status"] = "todo"
    rt["notes"] = ((rt.get("notes") or "").rstrip() + "\n" + n).strip()
    rt["updated"] = now_z()
    if validate_roadmap(data):
        return f"WARN: roadmap fails validation; {tid} not updated there (run `roadmap validate`)", None
    save_roadmap_data(rp, html, m, data, log_text=f"Coniso: reverted {tid}: {reason}"[:200], quiet=True)
    return (f"roadmap: {tid} " + ("is marked done by you, so it stays done, with a revert note: set it back if the revert stands"
                                  if done else "back to todo, with a revert note")), rp.relative_to(ROOT).as_posix()


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
        match = not a.no_files and schedule_match(path)
        print(f"PLAN OK {path.name}: {summary} · D3 D5 D6 pass" + (" · MATCHES SCHEDULE (skip the delegation audit)" if match else ""))
        ctx = write_plan_ctx(path)
        if ctx and not match:
            print(f"CONTEXT {p(ctx)} — the auditor's context (one line per ticket), never sprint.json")
        return
    print(f"PLAN FAIL {path.name}: {summary} · {len(findings)} finding(s)")
    for rule, unit, text in findings[:20]:
        print(f"  {rule} · {unit} — {text}")
    if len(findings) > 20:
        print(f"  … {len(findings) - 20} more")
    print("Fix the plan or assignment files, then run validate-plan again. Do not send these to the auditor.")
    sys.exit(1)


MARKER_RE = re.compile(r"\[VERIFY\]|\bTODO\b")


def cmd_validate_handoff(a):
    """Mechanical hand-off checks (H1-H3): evidence per acceptance item, a passing test log, no hidden markers."""
    conf = cfg()
    c = load_sprint()
    t = ticket(c, a.id)
    rep = L / "reports" / f"{safe(a.id)}.md"
    if not rep.exists():
        die(f"no hand-off report {p(rep)}")
    text = rep.read_text(encoding="utf-8")
    findings = []
    met, no_ev = set(), set()
    for ln in text.splitlines():
        m = ACCEPT_RE.match(ln)
        if m and m.group(2).lower() == "met":
            items = expand_items(m.group(1))
            met |= items
            ev = m.group(3).strip().lstrip("—–-:· ").strip()
            if not ev or ev.lower() in ("none", "n/a", "-", "tbd"):
                no_ev |= items
    n = accept_count(a.id, t)
    missing = sorted(set(range(1, n + 1)) - met)
    if missing:
        findings.append(("H1", "no 'A<n> met — <evidence>' line for A" + ",A".join(map(str, missing))))
    if no_ev:
        findings.append(("H1", "no evidence for A" + ",A".join(map(str, sorted(no_ev)))))
    logs = []
    for m in re.finditer(r"[\w.:/\\~-]+\.log\b", text):
        cand = Path(m.group(0))
        cand = cand if cand.is_absolute() else ROOT / cand
        if cand.exists():
            logs.append(cand)
    default = L / "reports" / f"{safe(a.id)}.test.log"
    if not logs and default.exists():
        logs.append(default)
    if not logs:
        findings.append(("H2", "no test log: the report names none that exists, and .coniso/reports/" + default.name + " is missing"))
    else:
        exits = re.findall(r"\[exit (-?\d+)\]", logs[0].read_text(encoding="utf-8", errors="replace"))
        if exits and exits[-1] != "0" and not re.search(r"\bfail", text, re.I):
            findings.append(("H2", f"{logs[0].name} ends with exit {exits[-1]} and the report does not explain the failure"))
    if t.get("wt") and conf.get("worktrees", True):
        wt = Path(t["wt"])
        git(["add", "-N", "--"] + pathspec_excludes(conf)[2:] + ["."], wt, check=False)
        diff = git(["diff", "-U0", t["base"]] + pathspec_excludes(conf), wt, check=False).stdout
        hidden, cur = {}, None
        for ln in diff.splitlines():
            if ln.startswith("+++ "):
                cur = ln[6:] if ln.startswith("+++ b/") else None
            elif ln.startswith("+") and cur and MARKER_RE.search(ln):
                hidden[cur] = hidden.get(cur, 0) + 1
        listed = [ln for ln in text.splitlines() if re.search(r"VERIFY|TODO", ln)]
        for f, k in sorted(hidden.items()):
            if not any(f in ln for ln in listed):    # H3: a marker listed with its file is fine; a hidden one is not
                findings.append(("H3", f"{k} [VERIFY]/TODO marker(s) added in {f}, not listed in the report"))
    if findings:
        print(f"HANDOFF FAIL {a.id}: {len(findings)} finding(s)")
        for rule, msg in findings:
            print(f"  {rule} — {msg}")
        sys.exit(1)
    print(f"HANDOFF OK {a.id}: {n} acceptance item(s) evidenced · log {logs[0].name} · no unlisted markers")


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


FEEDBACK_MAX = 30
RETIRED_SUFFIX = ".retired.md"


def lesson_key(line):
    """Comparable text of a lesson line: no bullet, no [stamp] prefix, no retirement suffix, lowercase, words only."""
    s = re.sub(r"^\s*[-*]\s*", "", line)
    s = re.sub(r"^\[[^\]]*\]\s*", "", s)
    s = s.split(" — retired ")[0]
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def cmd_lesson(a):
    """Add a stamped lesson, or retire one into <spec>.retired.md with a dated reason. Never commits:
    the main session commits the lessons file with the related fix."""
    active = feedback_file(a.spec)
    retired = active.with_name(active.stem + RETIRED_SUFFIX)
    lines = active.read_text(encoding="utf-8").splitlines() if active.exists() else []
    today = dt.date.today().isoformat()
    if a.retire:
        if a.text:
            die("pass either a lesson to add or --retire MATCH, not both")
        hits = [i for i, ln in enumerate(lines) if ln.strip() and a.retire.lower() in ln.lower()]
        if len(hits) != 1:
            die(f"--retire must match exactly one line in {active.relative_to(ROOT).as_posix()}; it matched {len(hits)}")
        if not a.by:
            die("--retire needs --by \"<why, or the lesson that replaces it>\"")
        line_ = lines.pop(hits[0])
        active.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        new = not retired.exists()
        with retired.open("a", encoding="utf-8") as fh:
            if new:
                fh.write(f"# Retired lessons — {a.spec}\nNo longer binding. `lesson` refuses to re-add one of these without --force.\n\n")
            fh.write(f"{line_.rstrip()} — retired {today}: {a.by}\n")
        print(f"lesson retired from {active.name} to {retired.name} · {len([x for x in lines if x.strip()])} active line(s)")
        return
    if not a.text or not a.text.strip():
        die("give the lesson text, e.g. lesson ui \"Check contrast in dark mode before handing off.\"")
    text = " ".join(a.text.split())
    key = lesson_key(text)
    if any(lesson_key(ln) == key for ln in lines if ln.strip()):
        die(f"already in {active.name}: nothing added")
    if retired.exists() and not a.force:
        old = [ln for ln in retired.read_text(encoding="utf-8").splitlines() if ln.startswith("-") and lesson_key(ln) == key]
        if old:
            die(f"this lesson was retired: {old[0].strip()}\nRe-add only if the reason no longer holds: pass --force")
    stamp = today + (f" · {a.source}" if a.source else "")
    active.parent.mkdir(parents=True, exist_ok=True)
    lines.append(f"- [{stamp}] {text}")
    active.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n = len([x for x in lines if x.strip()])
    warn = f" · over {FEEDBACK_MAX}: condense at retro" if n > FEEDBACK_MAX else ""
    print(f"lesson added to {active.relative_to(ROOT).as_posix()} ({n} line(s){warn}); commit it with the related fix")


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


LESSON_PLACEHOLDER = "- (Director: brief gaps, forged specialties worth keeping, routing that the auditor caught)"


def retro_sections(text):
    """RETRO.md split into sections, each starting at a "## " heading (text before the first heading is dropped)."""
    return [s for s in re.split(r"(?m)^(?=## )", text) if s.startswith("## ")]


def section_lessons(section):
    """The Director's lines under "Lessons:" in one retro section, without the placeholder."""
    out, on = [], False
    for ln in section.splitlines():
        if ln.startswith("Lessons:"):
            on = True
            continue
        if on and ln.strip() and "(Director:" not in ln:
            out.append(ln)
    return out


def last_lessons(sprints=1):
    """Lessons from the most recent `sprints` retro sections that recorded any (what the Director reads at setup).
    Sections whose Lessons are empty or only the placeholder are skipped, so one empty retro hides nothing."""
    f = L / "RETRO.md"
    if not f.exists():
        return []
    picked = []
    for sec in reversed(retro_sections(f.read_text(encoding="utf-8"))):
        ls = section_lessons(sec)
        if ls:
            picked.append((sec.splitlines()[0], ls))
            if len(picked) >= sprints:
                break
    if sprints <= 1:
        return picked[0][1] if picked else []
    out = []
    for head, ls in reversed(picked):     # oldest first, like the file
        out += [head] + ls
    return out


def cmd_retro(a):
    if a.lessons:
        ls = last_lessons(max(1, a.sprints))
        print("\n".join(ls) if ls else "no lessons recorded")
        return
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
    if c.get("quickfixes"):
        lines.append(f"Quick fixes: {c['quickfixes']} — " + ", ".join(
            f"{tid}×{t['quickfixes']}" if t["quickfixes"] > 1 else tid for tid, t in c["tickets"].items() if t.get("quickfixes")))
    rev = [f"{tid} ({r['reason']})" for tid, t in c["tickets"].items() for r in t.get("reverts", [])]
    if rev:
        lines.append("Reverted after merge (a lesson each?): " + "; ".join(rev))
    fl = L / "flaky.jsonl"
    if fl.exists():
        since = c.get("created", "")
        fr = [json.loads(x) for x in fl.read_text(encoding="utf-8").splitlines() if x.strip()]
        fr = [r for r in fr if r.get("ts", "") >= since]
        if fr:
            lines.append(f"Flaky test runs (failed, then passed on rerun): {len(fr)} · "
                         + ", ".join(sorted({r['where'] for r in fr})) + " · fix or quarantine the flaky tests")
    tk = token_totals(c)
    if tk:
        lines += tk
    st = ledger_stats(rows)
    if st:
        lines += ["Auditor ledger (overrules worth a look):", st]
    fb = feedback_dir()
    long_fb = []
    if fb.is_dir():
        for f in sorted(fb.glob("*.md")):
            if f.name.endswith(RETIRED_SUFFIX):
                continue
            n = len([x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()])
            if n > FEEDBACK_MAX:
                long_fb.append(f"{f.name} ({n})")
    if long_fb:
        lines.append(f"Agent feedback over {FEEDBACK_MAX} lines (condense with `lesson <spec> --retire`, director.md §10): "
                     + ", ".join(long_fb))
    rf = L / "RETRO.md"
    prior = rf.read_text(encoding="utf-8") if rf.exists() else ""
    secs = retro_sections(prior)
    rerun = bool(secs) and secs[-1].startswith(f"## {c['name']} — ")
    kept = section_lessons(secs[-1]) if rerun else []     # a re-run replaces its own section and keeps its Lessons
    lines += ["Lessons:"] + (kept or [LESSON_PLACEHOLDER]) + [""]
    text = "\n".join(lines)
    if rerun:
        head = prior[:len(prior) - len(secs[-1])]
        rf.write_text(head + text + "\n", encoding="utf-8")
    else:
        with rf.open("a", encoding="utf-8") as fh:
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


def specialty_file(spec):
    own = L / "specialties" / f"{spec}.md"
    return own if own.exists() else L / "ref" / "specialties" / f"{spec}.md"


def feedback_file(spec):
    """docs/agent-feedback/<spec>.md, or the legacy name the specialty header gives ("older projects: x.md")
    when only that one exists. Always a path in the main checkout, never a worktree copy."""
    fb_dir = feedback_dir()
    canon = fb_dir / f"{spec}.md"
    if canon.exists():
        return canon
    sf = specialty_file(spec)
    if sf.exists():
        m = re.search(r"older projects:\s*([\w.-]+\.md)", "\n".join(sf.read_text(encoding="utf-8").splitlines()[:3]))
        if m and (fb_dir / m.group(1)).exists():
            return fb_dir / m.group(1)
    return canon


def specialty_files(spec):
    out = [p(specialty_file(spec))]
    fb = feedback_file(spec)
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


def write_ctx(name, ids, c):
    """`<plan>.ctx.md`: one line per ticket a plan covers. It is the auditor's context in place of sprint.json,
    which grows with every note and token record of the sprint."""
    lines = [f"CONTEXT {name} · sprint {c['name']} · {len(ids)} ticket(s), one line each · {GEN_MARK}"]
    for tid in ids:
        t = c["tickets"].get(tid)
        if not t:
            continue
        f_ = t["formation"]
        form = (("patch " if f_.get("patch") else "solo ") + f_["model"]) if f_["kind"] == "solo" \
            else f"team: lead {f_['lead']} + {f_['engineers']} eng"
        rv = " + ".join(f"{r['model']} {r['lens']}" for r in t["review_plan"]["reviewers"])
        lines.append(f"{tid} · {t['title']} · size {t.get('size')} · model {t.get('model')} · effort {t.get('effort') or '-'}"
                     f" · review {t['review_plan']['kind']} ({rv}) · state {t['state']} · attempts {t['attempts']}"
                     f" · deps {','.join(t['deps']) or '-'} · formation {form}")
    f = L / "assignments" / f"{name}.ctx.md"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def plan_tickets(rows, c):
    """Ticket ids a delegation table refers to: unit prefixes, `ticket X` scopes, and the tickets of named tracks."""
    out = []
    tracks = {tr["id"]: tr for tr in c.get("tracks") or []}
    for r in rows:
        unit = r["unit"].strip("` ")
        cand = [unit.partition(".")[0]] + re.findall(r"ticket\s+(\S+)", r.get("owns", "") + " " + r.get("needs", ""))
        if unit in tracks:
            cand += tracks[unit].get("tickets", [])
        for x in cand:
            if x in c["tickets"] and x not in out:
                out.append(x)
    return out


def write_plan_ctx(plan_path):
    """Write the plan's context slice from the sprint; every open ticket when the rows name none."""
    c = load_sprint(required=False)
    if not c:
        return None
    _, rows = parse_plan(plan_path.read_text(encoding="utf-8"))
    ids = plan_tickets(rows or [], c) or [tid for tid, t in c["tickets"].items() if t["state"] not in TERMINAL]
    return write_ctx(plan_path.stem, ids, c)


def check_table(f):
    findings, summary = validate_plan(f)
    if findings:
        print(f"PLAN FAIL {f.name}: {summary}")
        for rule, unit, text in findings[:10]:
            print(f"  {rule} · {unit} — {text}")
        sys.exit(1)
    match = schedule_match(f)
    print(f"PLAN OK {f.name}: {summary}" + (" · MATCHES SCHEDULE" if match else " · deviates from the schedule: audit it"))
    ctx = write_plan_ctx(f)
    if ctx and not match:
        print(f"CONTEXT {p(ctx)} — the auditor's context (one line per ticket), never sprint.json")


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
        if f_.get("patch"):
            bounds += (f" Patch ticket: the smallest change that meets the acceptance items, in the files it needs; no refactors "
                       f"or drive-by fixes. A diff over {PATCH_MAX_LINES} lines or {PATCH_MAX_FILES} files loses the light review.")
        if f_["kind"] == "solo":
            if conf.get("worktrees", True):
                with redirect_out():
                    cmd_wt(argparse.Namespace(action="add", id=tid, force=False))
                workdir = load_sprint()["tickets"][tid]["wt"]
            else:
                workdir = p(ROOT)
            unit, spec = f"{tid}.E1", specs.get(tid, "builder")
            read = " · ".join([p(L / "BRIEF.md"), packet] + house_files() + specialty_files(spec))
            path, how = write_assignment(unit, f"ASSIGNMENT {unit} · engineer/{spec} · model {f_['model']} · from {a.from_}", with_decisions([
                ("READ FIRST", read), ("WORKDIR", p(workdir)),
                ("OBJECTIVE", f"Build ticket {tid} ({t['title']}) so every acceptance item is met, with evidence."),
                ("OWNS", "** (solo: the whole ticket inside WORKDIR)"), ("INPUTS", inputs), ("BOUNDARIES", bounds),
                ("DONE WHEN", f"{accept_range(n)} met, each with evidence (test name or file:line); `{cmd} test {tid}` passes."),
                ("ON DONE", f"after the test passes run `{cmd} set {tid} built --note \"<one phrase>\"` (solo ticket: you record built)."),
                ("RETURN", f"standard return + detail in {p(L / 'reports' / (unit + '.md'))}")], [(tid, t)], cmd), a.rewrite)
            rows.append([unit, f"engineer/{spec}", f_["model"], "1", "**", f"ticket {tid}", accept_range(n)])
            spawns.append(spawn_line("coniso:engineer", f_["model"], path))
        else:
            unit = tid
            review = "review lead serves" if a.review == "lead" else "dispatch your own reviewer"
            path, how = write_assignment(unit, f"ASSIGNMENT {unit} · lead · model {f_['lead']} · from {a.from_}", with_decisions([
                ("READ FIRST", " · ".join([p(L / "BRIEF.md"), packet] + house_files())), ("WORKDIR", p(ROOT)),
                ("OBJECTIVE", f"Deliver ticket {tid} ({t['title']}) built, tested and handed off with evidence for every acceptance item."),
                ("OWNS", f"ticket {tid}"), ("INPUTS", inputs), ("BOUNDARIES", bounds),
                ("DONE WHEN", f"{accept_range(n)} evidenced; `{cmd} validate-handoff {tid}` prints HANDOFF OK; state built."),
                ("REVIEW", review), ("SLOTS", str(f_["engineers"])),
                ("ENGINEERS", f"up to {f_['engineers']} (default engineer model {f_.get('engineer_model', 'sonnet')})"),
                ("RETURN", f"standard return + detail in {p(L / 'reports' / (tid + '.md'))}")], [(tid, t)], cmd), a.rewrite)
            rows.append([unit, "lead", f_["lead"], "1", f"ticket {tid}", f"ticket {tid}", accept_range(n)])
            spawns.append(spawn_line("coniso:lead", f_["lead"], path))
        used += cost(f_)
        spec_ = specs.get(tid) if (f_["kind"] == "solo" or a.specialty) else None
        if spec_ and t.get("spec") != spec_:     # reviewers read this specialty's lessons
            with Lock():
                c2 = load_sprint()
                c2["tickets"][tid]["spec"] = spec_
                save_sprint(c2)
        if how == "kept":
            print(f"  kept existing {path.name} (--rewrite regenerates it)")
    if len(ids) > 1 or a.plan:
        wave = min((c["tickets"][i].get("wave") or 0) for i in ids)
        name = a.plan or f"{a.from_}-w{wave}"
        review = "Review Lead" if a.review == "lead" else "each leader dispatches its own reviewer"
        check_table(write_table(name, rows, review, used, c.get("budget") or conf["slots"]))
    print("\n".join(spawns))


def diff_stats(t, conf, base=None):
    """(changed lines, changed files) of a ticket's worktree against its base (or the given commit), new files
    included; (None, None) when there is no worktree to measure."""
    base = base or t.get("base")
    if not (conf.get("worktrees", True) and t.get("wt") and base):
        return None, None
    wt = Path(t["wt"])
    git(["add", "-N", "--"] + pathspec_excludes(conf)[2:] + ["."], wt, check=False)
    r = git(["diff", "--numstat", base] + pathspec_excludes(conf), wt, check=False)
    lines = files = 0
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            files += 1
            lines += sum(int(x) for x in parts[:2] if x.isdigit())
    return lines, files


def review_budget(lines, model, kind, cmd):
    """The reviewer's BUDGET line, sized from the diff it has to read. Big diffs keep the full ceiling; small ones
    stop paying for it."""
    if lines is None:
        calls = 60 if model == "opus" else 40
    elif kind in ("delta", "patch"):
        calls = 12 if lines <= 60 else 18
    elif lines <= 60:
        calls = 12
    elif lines <= 300:
        calls = 25
    else:
        calls = 60 if model == "opus" else 40
    probes = 1 if calls <= 18 else 2
    size = "a diff of unknown size" if lines is None else f"this {lines}-line diff"
    return (f"at most {calls} tool calls for {size}; rerun the builder's probes first, add at most {probes} new probe(s) "
            f"and 1 mutation per acceptance-critical path, and run `{cmd} test` once, after your last restore")


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
                    cmd_sync(argparse.Namespace(id=tid, cont=False, force=True))
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
        lines_, files_, measured = 0, 0, False
        for x in group:
            ln_, fl_ = diff_stats(c["tickets"][x], conf)
            if ln_ is not None:
                measured, lines_, files_ = True, lines_ + ln_, files_ + fl_
        kind = t["review_plan"]["kind"]
        if kind == "patch" and not delta and measured and (lines_ > PATCH_MAX_LINES or files_ > PATCH_MAX_FILES):
            with Lock():    # the script decided this by rule, so the upgraded plan is the scheduled plan: nothing to audit
                c = load_sprint()
                t = c["tickets"][first]
                t["review_plan"] = review_plan(dict(t, size="S"))
                note(t, t["state"], f"patch upgraded: {lines_} lines / {files_} files over {PATCH_MAX_LINES}/{PATCH_MAX_FILES}")
                save_sprint(c)
            kind = t["review_plan"]["kind"]
            print(f"PATCH UPGRADED {first}: {lines_} lines across {files_} files exceeds the patch limit "
                  f"({PATCH_MAX_LINES} lines / {PATCH_MAX_FILES} files); it gets the normal {kind} review")
        if delta:   # playbook: re-reviews get one light reviewer on the fix list and the files it touched
            reviewers = [{"model": a.model or "sonnet", "lens": "spec+regression", "agent": "reviewer-lite"}]
        elif a.panel and (a.panel == "*" or first in a.panel.split(",")) and t["review_plan"]["kind"] != "panel":
            reviewers = review_plan(dict(t, review=True))["reviewers"]
        else:
            reviewers = t["review_plan"]["reviewers"]
        if a.model and not delta:     # an explicit --model sets every reviewer of this review, not only delta ones
            reviewers = [dict(rv, model=a.model) for rv in reviewers]
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
            agent = rv.get("agent", "reviewer")
            lessons = sorted({f for x in group if c["tickets"][x].get("spec")
                              for f in specialty_files(c["tickets"][x]["spec"])[1:]})
            path, _ = write_assignment(unit, f"ASSIGNMENT {unit} · {agent} · model {rv['model']} · from {a.from_}", with_decisions([
                ("READ FIRST", " · ".join([p(L / "BRIEF.md"), packets] + house_files() + lessons)), ("WORKDIR", p(t.get("wt") or ROOT)),
                ("OBJECTIVE", f"Verify {', '.join(group)} against the acceptance items and the brief through lens {rv['lens']}, and return an evidence-backed verdict."),
                ("OWNS", f"{p(rep)}, {p(L / 'reports' / unit)}/**, {p(fix)}"), ("LENS", rv["lens"]),
                ("SCOPE", "delta" if delta else "full"),
                ("BUDGET", review_budget(lines_ if measured else None, rv["model"], "delta" if delta else kind, cmd)),
                ("INPUTS", inputs),
                ("BOUNDARIES", "Never edit WORKDIR files or run git; probes only under your reports folder."),
                ("DONE WHEN", f"a verdict per formats.md in your report, with {items} each marked met or unmet with evidence. "
                              f"Then run `{cmd} verdict {','.join(group)} --report {p(rep)}` and put the line it prints in your return."),
                ("RETURN", f"verdict return (formats.md) + detail in {p(rep)}")], [(x, c["tickets"][x]) for x in group], cmd), True)
            rows.append([unit, f"{agent}/{rv['lens']}", rv["model"], "1",
                         f".coniso/reports/{unit}.md, .coniso/reports/{unit}/**, .coniso/reports/{fix.name}",
                         ", ".join(f"ticket {x}" for x in group), "verdict"])
            spawns.append(spawn_line(f"coniso:{agent}", rv["model"], path))
        for x in group:
            plans[x] = units
    with Lock():
        c = load_sprint()
        for x, units in plans.items():
            t = c["tickets"][x]
            t["review_round"] = {"units": units, "verdicts": {}}
            t.pop("with_fixes", None)
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
        hm = re.match(r"^#{1,6}\s+(.+?)\s*#*$", s)
        if hm:     # a markdown heading: "## FINDINGS" opens the findings, any other heading closes them
            in_findings = hm.group(1).strip("*: ").upper().startswith("FINDINGS")
            cur["saw_findings"] = cur.get("saw_findings") or in_findings
            continue
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
            cur["saw_findings"] = True
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
        elif not in_findings and "suspicion" not in s.lower():
            cur.setdefault("loose", []).append(s)
    for b in blocks.values():
        # No findings section the parser knows: count the tags wherever they are, rather than report none.
        if not b.get("saw_findings"):
            for s in b.get("loose", []):
                for sev in SEV_RE.findall(s):
                    b[sev.lower()] += 1
        b.pop("loose", None)
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


def round_unit(unit, tid, rp, rnd):
    """The review round's unit a VERDICT line means. Reviewers often write `R1` for `P2-03.R1`; a verdict recorded
    under a name the round does not know would leave the round "waiting on" the real unit forever."""
    units = rnd.get("units") or []
    if unit in units:
        return unit
    for cand in (f"{tid}.{unit}", rp.stem):
        if cand in units:
            return cand
    pending = [u for u in units if u not in (rnd.get("verdicts") or {})]
    if len(pending) == 1 and re.fullmatch(r"(?:[\w-]+\.)?R\d+", unit or ""):
        return pending[0]
    return unit


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
                unit = round_unit(b["unit"], tid, rp, rnd)
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
            said_fixes = any(v["stated"] == "pass-with-fixes" for v in vs.values())
            reports = sorted({v["report"] for v in vs.values()})
            fixes = ""
            if ruling == "pass" and (minors or said_fixes):
                fixes = (f" · WITH FIXES: {minors} minor finding(s) to read before landing: {', '.join(reports)}" if minors
                         else f" · WITH FIXES: the reviewer said pass-with-fixes but no [minor] tag was counted; "
                              f"read {', '.join(reports)} before landing")
                t["with_fixes"] = {"minors": minors, "reports": reports}
            else:
                t.pop("with_fixes", None)
            t["state"], t["verdict"] = ("passed" if ruling == "pass" else "failed"), ruling
            text = f"ruled {ruling}: {summary}" + (f"; {minors} minor(s) carried as notes" if ruling == "pass" and minors else "") \
                + (f"; fix list .coniso/reports/{main_fix.name}" if ruling == "fail" else "")
            note(t, t["state"], text)
            (L / "reports" / f"{safe(tid)}.ruling.md").write_text(f"{tid} {text}\n", encoding="utf-8")
            audit = ruling == "fail" or t.get("model") == "opus" or t.get("review")
            out.append(f"RULED {ruling} {tid} — {summary}" + fixes + (" · verdict audit required (playbook)" if audit else ""))
        save_sprint(c)
    print("\n".join(out))


QUICKFIX_STATES = ("failed", "passed", "built")     # built: before its first review, which then covers the fix


def cmd_quickfix(a):
    """A bounded fix made by the leader who holds the cross-ticket picture, inside the ticket's worktree.
    begin: sync with main, snapshot the builder's work, open the fix.
    done:  measure the change against the limit, run the checks once, set built for a delta review.
    abort: drop the edits and leave the worktree as the builder left it.
    The script enforces the size limit and the single attempt, so a quick fix cannot quietly become a fix round,
    and it never sets passed: a reviewer still judges the change."""
    conf = cfg()
    if not conf.get("worktrees", True):
        die("quick fixes need worktrees")
    max_lines, max_files = conf.get("quickfix_max_lines", 20), conf.get("quickfix_max_files", 2)
    c = load_sprint()
    t = ticket(c, a.id)
    if not t.get("wt") or not Path(t["wt"]).exists():
        die(f"{a.id} has no worktree; a fix after the merge is an XS ticket (adhoc --size XS)")
    wt = Path(t["wt"])
    q = t.get("quickfix")
    if a.action == "begin":
        if q:
            die(f"{a.id} already has an open quick fix: finish it with `quickfix {a.id} done --note …` or `quickfix {a.id} abort`")
        if t["state"] not in QUICKFIX_STATES:
            die(f"{a.id} is {t['state']}; a quick fix takes a built ticket (before review), a failed one (review or "
                f"integration) or a passed one with minors to clear. A ticket in review: wait for its verdict.")
        r = redirect_out()
        try:
            with r:
                cmd_sync(argparse.Namespace(id=a.id, cont=False))
        except SystemExit as e:
            print(r.text.strip())
            print(f"Not a quick fix: {a.id} conflicts with main. Send its builder the conflict.")
            sys.exit(e.code or 3)
        snapshot(ticket(load_sprint(), a.id), conf)
        base = git(["rev-parse", "HEAD"], wt).stdout.strip()
        with Lock():
            c = load_sprint()
            t = ticket(c, a.id)
            t["quickfix"] = {"base": base, "from": t["state"], "ts": now_z()}
            note(t, t["state"], "quick fix opened")
            save_sprint(c)
        hints = [f"{label} {p(x)}" for label, x in (("fix list", L / "reports" / f"{safe(a.id)}.fix.md"),
                                                    ("integration log", L / "reports" / f"{safe(a.id)}.merge-test.log"))
                 if x.exists()]
        print(f"QUICKFIX {a.id} open · WORKDIR {p(wt)} · limit {max_lines} changed lines in {max_files} file(s) · one attempt")
        if hints:
            print("Read only: " + " · ".join(hints) + " · and the lines they name")
        print(f"Then: quickfix {a.id} done --note \"<what you changed and why>\"   (too big or a failing test: quickfix {a.id} abort)")
        return
    if not q:
        die(f"{a.id} has no open quick fix; start with `quickfix {a.id} begin`")
    if a.action == "abort":
        git(["reset", "--hard", "-q", q["base"]], wt, check=False)
        keep = [x.rstrip("/") for x in conf.get("link_dirs", []) + conf.get("copy_files", [])]
        for ln in git(["status", "--porcelain", "--untracked-files=all"], wt, check=False).stdout.splitlines():
            rel = ln[3:].strip().strip('"')
            if ln.startswith("?? ") and not any(rel == k or rel.startswith(k + "/") for k in keep):
                try:
                    (wt / rel).unlink()     # a file the quick fix created; the snapshot left none untracked
                except OSError:
                    pass
        with Lock():
            c = load_sprint()
            t = ticket(c, a.id)
            t.pop("quickfix", None)
            note(t, t["state"], "quick fix aborted")
            save_sprint(c)
        print(f"QUICKFIX {a.id} aborted · the worktree is back at the builder's work · send the builder the fix list")
        return
    # done
    if not (a.note or "").strip():
        die("quickfix done needs --note \"<what you changed and why>\": the delta reviewer reads it")
    lines, files = diff_stats(t, conf, base=q["base"])
    if not lines and not files:
        die(f"{a.id}: nothing changed since the quick fix opened (`quickfix {a.id} abort` closes it)")
    if lines > max_lines or files > max_files:
        print(f"QUICKFIX TOO BIG {a.id}: {lines} changed line(s) in {files} file(s); the limit is {max_lines} lines in {max_files} file(s).")
        print(f"That is a fix round, not a quick fix: `quickfix {a.id} abort`, then send the builder the fix list.")
        sys.exit(1)
    names = git(["diff", "--name-only", q["base"]] + pathspec_excludes(conf), wt, check=False).stdout.split()
    log = L / "reports" / f"{safe(a.id)}.test.log"
    tree = tree_id(wt, conf) if conf.get("test_cache", True) else None
    try:
        code, text = run_checks(wt, conf, log, label=f"quickfix {a.id}")
    except SlotBusy as e:
        print(f"QUICKFIX BUSY {a.id}: {e}; the fix is still open, run `quickfix {a.id} done` again")
        sys.exit(9)
    if code not in (None, 0):
        print(f"QUICKFIX TEST FAIL {a.id} (exit {code}) · log {p(log)}")
        print("\n".join(text.strip().splitlines()[-15:]))
        print(f"One attempt only: `quickfix {a.id} abort`, then send the builder the fix list and this log.")
        sys.exit(1)
    fixf = L / "reports" / f"{safe(a.id)}.fix.md"
    with fixf.open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Quick fix by {a.from_} — {now_z()}\n{a.note.strip()}\n"
                 f"Changed {lines} line(s) in: {', '.join(names) or '?'} (since {q['base'][:7]}). "
                 f"Review exactly this change and its regression risk.\n")
    with Lock():
        c = load_sprint()
        t = ticket(c, a.id)
        t.pop("quickfix", None)
        t["quickfixes"] = t.get("quickfixes", 0) + 1
        c["quickfixes"] = n = c.get("quickfixes", 0) + 1
        if tree and code == 0:
            t["test_cache"] = {"tree": tree, "code": 0, "ts": now_z(), "log": p(log)}
        t["state"] = "built"
        note(t, "built", f"quick fix by {a.from_}: {lines} line(s) in {files} file(s) — {a.note.strip()[:160]}")
        save_sprint(c)
    print(f"QUICKFIX OK {a.id}: {lines} line(s) in {files} file(s) · checks {'PASS' if code == 0 else 'none configured'} "
          f"· state built · quick fix {n} this sprint")
    if q.get("from") == "built":
        print(f"Next: review {a.id}   (its first review covers the quick fix too)")
    else:
        print(f"Next: review {a.id} --delta   (a quick fix never merges unreviewed)")


def land_batch(order, conf, branch):
    """Squash every passed ticket onto the branch, then run the integration checks once on the combined result
    instead of once per ticket. On any problem (a conflict, no changes, failing checks) the branch goes back to
    where it started and the tickets back to passed, so the caller merges and tests them one at a time and finds
    the culprit.

    The squash commits are made on a detached HEAD in the main checkout, so the build caches stay warm, and the
    branch is moved to them only after the combined checks pass. Until then the branch (what `review` and `sync`
    merge into a ticket) still points at tested code. Returns "landed", "fallback", "stop" or "stop7"."""
    start = git(["rev-parse", "HEAD"], ROOT).stdout.strip()
    branch = branch or current_branch()
    git(["checkout", "-q", "--detach"], ROOT)
    touched, problem, side = [], None, []
    for tid in order:
        touched.append(tid)
        r = redirect_out()
        try:
            with r:
                cmd_merge(argparse.Namespace(id=tid, force=False, no_test=True, keep=True, fresh=True, detached_ok=True))
        except SystemExit:
            first = next((x for x in r.text.strip().splitlines() if x.strip()), f"{tid} did not merge")
            problem = first.strip()
            break
    if problem is None:
        log = L / "reports" / f"land-{safe(order[0])}+{len(order) - 1}.merge-test.log"
        code, text = run_checks(ROOT, conf, log, label=f"land {'+'.join(order)}", gate=True)
        side = check_side_effects()
        created = [x for x in side if x[1]]
        undo_side_effects(created)
        if created:
            print(f"Removed {len(created)} untracked file(s) the checks created; gitignore them")
        side = [x for x in side if not x[1]]
        moved = False
        if code in (None, 0) and not side:
            r = git(["update-ref", f"refs/heads/{branch}", "HEAD", start], ROOT, check=False)
            moved = r.returncode == 0
            if moved:
                git(["checkout", "-q", branch], ROOT)
            else:
                problem = f"{branch} moved during the land ({(r.stderr or r.stdout).strip()[:120]}); nothing landed"
        if moved:
            for tid in order:
                with redirect_out():
                    cmd_wt(argparse.Namespace(action="remove", id=tid, force=True))
            c = load_sprint()
            for tid in order:
                print(f"MERGED {tid} as {(c['tickets'][tid].get('merged_sha') or '')[:7]} on {branch} · checks: batch")
            print(f"BATCH {len(order)} tickets · integration checks {'PASS' if code == 0 else 'none configured'} "
                  f"once on the combined result · log {p(log)}")
            ready = next_ready(c)
            if ready:
                print(f"Now ready: {', '.join(ready)}")
            return "landed"
        if problem:
            pass
        elif side:
            problem = f"{SIDE_EFFECTS} on the combined result: {side_effect_text(side)} · log {p(log)}"
        elif FLAKY_AT_MERGE in text:
            problem = f"{FLAKY_AT_MERGE} on the combined result · log {p(log)}"
        else:
            problem = f"integration checks failed on the combined result (exit {code}) · log {p(log)}"
    git(["reset", "--hard", "-q", start], ROOT, check=False)
    git(["checkout", "-q", branch], ROOT, check=False)
    with Lock():
        c = load_sprint()
        for tid in touched:
            t = c["tickets"][tid]
            t["state"], t["merged_sha"] = "passed", None
            note(t, "passed", "batch land reset" + ("" if problem.startswith((FLAKY_AT_MERGE, SIDE_EFFECTS)) else "; merging one by one"))
        save_sprint(c)
    print(f"BATCH RESET: {problem} · {branch} back at {start[:7]}")
    if problem.startswith(SIDE_EFFECTS):       # every ticket would hit it again one at a time: stop for the fix
        return "stop7"
    if problem.startswith(FLAKY_AT_MERGE):     # one at a time would pass on the now-recorded flake: stop for a look
        print("\n".join(text.strip().splitlines()[-20:]))
        return "stop"
    return "fallback"


def cmd_land(a):
    """Merge passed tickets in dependency order, as one batch with one integration run when there are several;
    run roadmap-sync once their wave has nothing in flight."""
    conf = cfg()
    if getattr(a, "recover", False):
        m = land_marker()
        if not m:
            print("no interrupted land to recover")
            return
        if pid_alive(m.get("pid")) and m.get("pid") != os.getpid():
            die(f"the land (pid {m['pid']}) is still running", code=8)
        recover_land(m, force=True)
        return
    if not a.ids:
        die("land needs ticket ids (or --recover)")
    ids = [x for x in ",".join(a.ids).split(",") if x]
    with landing(ids, conf):
        land_tickets(a, conf, ids)


def land_tickets(a, conf, ids):
    c = load_sprint()
    order = sorted(ids, key=lambda x: (ticket(c, x).get("wave") or 0, len(c["tickets"][x]["deps"]), x))
    if a.tokens:
        with Lock():
            c2 = load_sprint()
            note(ticket(c2, order[0]), c2["tickets"][order[0]]["state"], "reviewer tokens", a.tokens, a.role or "reviewer")
            save_sprint(c2)
    for tid in order:
        wf = c["tickets"][tid].get("with_fixes")
        if wf and c["tickets"][tid]["state"] == "passed":
            print(f"WITH FIXES {tid}: passed with {wf['minors'] or 'untagged'} minor finding(s) carried, from "
                  f"{', '.join(wf['reports'])}")
    if len(order) > 1 and not a.each and conf.get("worktrees", True):
        res = land_batch(order, conf, c.get("branch"))
        if res == "stop":
            sys.exit(4)
        if res == "stop7":
            sys.exit(7)
        if res == "fallback":
            print("Merging one by one to find the culprit.")
    for tid in order:
        if load_sprint()["tickets"][tid]["state"] == "merged":
            continue
        r = redirect_out()
        try:
            with r:
                cmd_merge(argparse.Namespace(id=tid, force=False, no_test=False, keep=False, fresh=a.fresh))
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
    s.add_argument("--lint", help="lint command run before build and test (e.g. 'ruff check .', 'npx eslint .'); '' clears it")
    s.add_argument("--typecheck", help="type check run before build and test (e.g. 'npx tsc --noEmit', 'mypy src'); '' clears it")
    s.add_argument("--format-check", dest="format", help="formatting check, not a fixer (e.g. 'npx prettier --check .'); '' clears it")
    s.add_argument("--setup", help="command run in each new worktree, e.g. 'npm ci' or 'dotnet restore'")
    s.add_argument("--test-timeout", dest="test_timeout", type=int, help="seconds one check command may run (default 1200)")
    s.add_argument("--merge-timeout", dest="merge_timeout", type=int,
                   help="seconds one check command may run at merge and land (default: --test-timeout); 0 clears it")
    s.add_argument("--check-slots", dest="check_slots", type=int,
                   help="check runs allowed at once on this machine (0: no limit, the default)")
    s.add_argument("--check-pattern", dest="check_pattern",
                   help="regex for check-run command lines, so runs started by hand count too, e.g. 'tools[\\/]check\\.py'")
    g2 = s.add_mutually_exclusive_group()
    g2.add_argument("--land-hold", dest="land_hold", action="store_true", default=None,
                    help="no new check run starts while a land runs")
    g2.add_argument("--no-land-hold", dest="land_hold", action="store_false")
    s.add_argument("--env", action="append", metavar="KEY=VALUE",
                   help="environment for check runs (repeatable); KEY= removes one")
    s.add_argument("--decisions-dir", dest="decisions_dir", help="a decision-record folder, one decision per file")
    s.add_argument("--house-rules", dest="house_rules", help="comma list of globs every engineer and reviewer reads")
    s.add_argument("--lessons-dir", dest="lessons_dir", help="per-specialty lessons folder (default docs/agent-feedback)")
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
    s.add_argument("--size", default="M", choices=["XS", "S", "M", "L"], help="XS: a patch (smallest change, light review sized to the diff)")
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
    s.add_argument("--json", action="store_true", help="compact JSON")
    s.add_argument("--field", help="one field of the ticket, dotted for nested ones, e.g. review_plan.kind")
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
    s.add_argument("--fresh", action="store_true", help="rerun even if this exact tree already passed")
    s.set_defaults(fn=cmd_test)

    s = sp.add_parser("sync")
    s.add_argument("id")
    s.add_argument("--continue", dest="cont", action="store_true")
    s.add_argument("--force", action="store_true", help="sync a ticket in review (only when no reviewer is running)")
    s.set_defaults(fn=cmd_sync)

    s = sp.add_parser("merge")
    s.add_argument("id")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-test", action="store_true")
    s.add_argument("--keep", action="store_true")
    s.add_argument("--fresh", action="store_true", help="run the integration checks even if the squashed tree already passed `test`")
    s.add_argument("--allow-secrets", dest="allow_secrets", action="store_true",
                   help="merge despite secret-scan findings (only on the user's say-so)")
    s.set_defaults(fn=cmd_merge)

    s = sp.add_parser("revert", help="undo a merged ticket with a revert commit and send it back to queued")
    s.add_argument("id")
    s.add_argument("--reason", required=True, help="what broke")
    s.add_argument("--force", action="store_true", help="revert even though merged tickets depend on it")
    s.set_defaults(fn=cmd_revert)

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

    s = sp.add_parser("land", help="merge passed tickets (several: one batch, one integration run); roadmap-sync when their wave has nothing in flight")
    s.add_argument("ids", nargs="*")
    s.add_argument("--recover", action="store_true", help="put the main checkout back after a land whose process died")
    s.add_argument("--each", action="store_true", help="merge and test one ticket at a time instead of one batch run")
    s.add_argument("--fresh", action="store_true", help="never reuse a ticket's passing `test` run for the integration checks")
    s.add_argument("--tokens", type=int, help="the reviewer's reported tokens, recorded on the first ticket")
    s.add_argument("--role", help="role for --tokens (default reviewer)")
    s.set_defaults(fn=cmd_land)

    s = sp.add_parser("quickfix", help="a bounded fix by the Director inside a ticket's worktree: begin, done --note, abort")
    s.add_argument("id")
    s.add_argument("action", choices=["begin", "done", "abort"])
    s.add_argument("--note", help="done: what you changed and why (the delta reviewer reads it)")
    s.add_argument("--from", dest="from_", default="director")
    s.set_defaults(fn=cmd_quickfix)

    s = sp.add_parser("slot", help="wait for a check slot before a check run started by hand (GO / BUSY / QUIET)")
    s.add_argument("--now", action="store_true", help="report once and return; take no slot")
    s.set_defaults(fn=cmd_slot)

    s = sp.add_parser("sweep", help="list processes left running with no parent; --stop stops them")
    s.add_argument("--stop", action="store_true", help="stop each leftover process and everything it started")
    s.set_defaults(fn=cmd_sweep)

    s = sp.add_parser("quiet", help="timing runs on a quiet machine: hold check runs, wait for quiet, run N times")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--out", help="results folder (default .coniso/reports/quiet-<time>)")
    s.add_argument("--cwd", help="folder to run the command in (default the repo root)")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command")
    s.set_defaults(fn=cmd_quiet)

    s = sp.add_parser("validate-handoff", help="mechanical hand-off checks: evidence per acceptance item, test log, no hidden [VERIFY]/TODO")
    s.add_argument("id")
    s.set_defaults(fn=cmd_validate_handoff)

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

    s = sp.add_parser("brief-trim", help="move Decisions log rows older than the current sprint to .coniso/maps/decisions.md")
    s.add_argument("--before", help="cutoff date YYYY-MM-DD (default: the sprint's start date)")
    s.set_defaults(fn=cmd_brief_trim)

    s = sp.add_parser("retro")
    s.add_argument("--lessons", action="store_true",
                   help="print the Lessons lines of the last sprint that recorded any; writes nothing")
    s.add_argument("--sprints", type=int, default=1,
                   help="with --lessons: how many recent sprints with lessons to print, each under its heading (default 1)")
    s.set_defaults(fn=cmd_retro)

    s = sp.add_parser("decisions", help="search the Decisions log in the brief and its archive (superseded rows hidden)")
    s.add_argument("--grep", help="case-insensitive text to match in a row")
    s.add_argument("--all", action="store_true", help="include SUPERSEDED rows")
    s.set_defaults(fn=cmd_decisions)

    s = sp.add_parser("memory", help="health of lessons, decisions, retro lessons and maps (setup and phase gates)")
    s.set_defaults(fn=cmd_memory)

    s = sp.add_parser("lesson", help="add a stamped lesson to docs/agent-feedback/<spec>.md, or retire one with a record")
    s.add_argument("spec", help="specialty, e.g. builder, ui, net")
    s.add_argument("text", nargs="?", help="the lesson: a one-line imperative")
    s.add_argument("--source", help="where it came from: a roadmap ticket id, a sprint, or 'user'")
    s.add_argument("--retire", metavar="MATCH", help="move the one active lesson containing MATCH to <spec>.retired.md")
    s.add_argument("--by", help="with --retire: why, or the lesson that replaces it")
    s.add_argument("--force", action="store_true", help="add even though a retired lesson says the same thing")
    s.set_defaults(fn=cmd_lesson)

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
