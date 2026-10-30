---
name: roadmap
description: Create or update the project's HTML prompt roadmap, or show what's next.
disable-model-invocation: true
argument-hint: "[new <project> | next | status text | change to make]"
---

Roadmap request: $ARGUMENTS

Read `${CLAUDE_PLUGIN_ROOT}/skills/coniso/references/roadmap.md` (or `.coniso/ref/roadmap.md`) and follow it. You don't need the rest of Coniso's Setup, and the `roadmap` commands work without `.coniso/`. Run the script from the plugin with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coniso.py" roadmap …`, or from `.coniso/coniso.py` if the repo has one.

- No arguments: run `roadmap next` and show the result in a few lines.
- `new <project>`: follow "Creating a roadmap".
- Pasted "Copy status for Claude" text: apply it per "Status sync".
- Anything else: make the change per "Operations".
