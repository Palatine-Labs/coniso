---
name: director
description: Bring the whole team for a session-long sprint off the roadmap.
disable-model-invocation: true
argument-hint: "[phase | ticket ids | continue] [light|max]"
---

Call: **Director** — target: $ARGUMENTS

1. Load the `coniso:coniso` skill with the Skill tool, then run its Setup.
2. Read `.coniso/ref/director.md` and run the sprint. If the target is `continue`, resume per that file's §0. With no target, take every ready ticket in the current phase. `light` or `max` sets the budget posture.
