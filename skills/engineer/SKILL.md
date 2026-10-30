---
name: engineer
description: Send a single engineer on a quick, well-defined task.
disable-model-invocation: true
argument-hint: "<task> [model haiku|sonnet|opus] [specialty]"
---

Call: **Engineer** — target: $ARGUMENTS

Load the `coniso:coniso` skill with the Skill tool, run its Setup, then follow its **Engineer** call for this target. If the target is a roadmap ticket id, use the Lead call instead, so it gets a worktree and a reviewer.
