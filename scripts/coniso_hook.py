#!/usr/bin/env python3
"""Coniso SessionStart hook (matcher `compact`).

After Claude Code compacts the main session, print the open sprint board and the current wave so the
Director picks up from files instead of from memory. Prints nothing and exits 0 when the project has no
active sprint (`.coniso/sprint.json`), so it costs nothing outside Coniso sprints.
Standard library only; never fails the session.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

TERMINAL = {"merged", "blocked", "dropped"}


def find_root(start):
    for d in [start] + list(start.parents):
        if (d / ".coniso" / "sprint.json").is_file():
            return d
    return None


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        data = {}
    root = find_root(Path(data.get("cwd") or os.getcwd()).resolve())
    if root is None:
        return
    sprint = json.loads((root / ".coniso" / "sprint.json").read_text(encoding="utf-8"))
    live = [t.get("wave") for t in sprint.get("tickets", {}).values()
            if t.get("state") not in TERMINAL and t.get("wave") is not None]
    script = root / ".coniso" / "coniso.py"
    board = ""
    if script.is_file():
        r = subprocess.run([sys.executable, str(script), "status", "--open"], cwd=str(root), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        board = (r.stdout or "").strip()
    print(f"Coniso sprint {sprint.get('name')} is active · current wave W{min(live) if live else '-'} "
          f"· state is on disk; the Director procedure is .coniso/ref/director.md")
    if board:
        print(board)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        main()
    except Exception:       # a hook must never break the session
        pass
    sys.exit(0)
