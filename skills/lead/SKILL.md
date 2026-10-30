---
name: lead
description: "Build one ticket or one described feature in its own worktree: a lead and its team for L or xhigh tickets, a solo engineer otherwise, then review."
disable-model-invocation: true
argument-hint: "<ticket id | short feature description>"
---

Call: **Lead** — target: $ARGUMENTS

Load the `coniso:coniso` skill with the Skill tool, run its Setup, then follow its **Lead** call for this target. A description rather than a ticket id becomes an `adhoc` ticket. Pick its id, size and model by the playbook, and write the acceptance criteria from the description.
