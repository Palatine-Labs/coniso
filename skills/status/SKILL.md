---
name: status
description: Show the current Coniso sprint board.
disable-model-invocation: true
---

From the repo root, run `python3 .coniso/coniso.py status` (or `python` / `py -3` if `python3` is missing). Show the user the output as-is, then add at most two lines: what is ready to start, and anything waiting on the user (conflicts, blocked tickets, gates). If there is no `.coniso/` folder, say Coniso hasn't been set up in this repo yet.
