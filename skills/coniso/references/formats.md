# Assignments, returns and verdicts — formats

`CONISO` means the command in the `.coniso/BRIEF.md` header.

## Unit ids
| Unit | Form |
|---|---|
| ticket / team | `P2-03` |
| engineers | `P2-03.E1`, `P2-03.E2` |
| reviewers | `P2-03.R1` |
| auditor checks | `P2-03.AU1` |
| manager tracks | `T1` |
| Review Lead rounds | `rv-r1` |
| Director waves | `director-w1` |
| quick tasks | `Q-<word>` |

## Assignment file — `.coniso/assignments/<unit>.md` (every spawn)
```
ASSIGNMENT <unit> · <role>[/<specialty>] · model <haiku|sonnet|opus> · from <leader unit>
READ FIRST: .coniso/BRIEF.md · .coniso/tickets/<id>.md · [specialty file] · [docs/agent-feedback/<specialty>.md if it exists]
WORKDIR: <absolute path printed by `CONISO wt add <id>`; the repo root for leads and managers>
OBJECTIVE: <one outcome-shaped sentence>
OWNS: <files/globs this unit may write — nothing else>
INPUTS: <paths, prior reports — pointers only, never pasted content>
BOUNDARIES: <out of scope; decisions that are not yours>
DONE WHEN: <testable criteria — for a ticket, its acceptance items by number>
SCOPE: full | delta   (reviewers only; delta = fix list + files the fix touched; omit for full)
LENS: <lens>                                                 (reviewers only)
ON DONE: <CONISO set <id> built …>                           (solo engineers only: they record built themselves)
REVIEW: <review lead serves | dispatch your own reviewer>   (leaders only)
SLOTS: <max concurrent subordinates>                         (leaders only)
RETURN: standard return + detail in .coniso/reports/<unit>.md
```
- `CONISO dispatch` (engineers and leads) and `CONISO review` (reviewers) write these files with every field filled and a closing `GENERATED: coniso` line. Write one by hand only for managers, a lead's engineers, quick tasks and scouts.
- If a haiku engineer could misread the packet, it is under-specified.
- If it needs more than 5 lines of background, the background belongs in the brief or a track note.

## Spawn prompt
A single line with the absolute path: `Assignment: <repo root>/.coniso/assignments/<unit>.md`. Use `subagent_type: coniso:<role>` and pass `model`. `dispatch` and `review` print it ready to use: `SPAWN coniso:<role> · model <m> · prompt: Assignment: <path>`.
Inside assignment files, write every path as absolute (repo root from the brief header), because agents' file tools need absolute paths.

## Standard return — every agent's final message, at most 12 lines
```
STATUS: done | partial | failed | blocked | relay
UNIT: <id>
RESULT: <one line>
CHANGED: <n files: top 3 paths> | none
CHECKS: <command → pass/fail · log path> | n/a
FLAGS: <decisions needed, brief gaps, [VERIFY] left> | none
REPORT: .coniso/reports/<unit>.md
```
Leaders add one line per ticket: `<id> <state> — <one phrase>`.

## Delegation table — `.coniso/assignments/<leader-unit>-w<n>.md`, `<id>-plan.md`, `rv-r<n>-plan.md` or `<id>-review.md`
```
| Unit | Role/specialty | Model | Step | Owns | Needs (inputs/order) | Done when |
|------|----------------|-------|------|------|----------------------|-----------|
Review: <who reviews>   Slots: <used>/<allowed>
Auditor: <O1 accepted | O1 overruled: reason | NO OBJECTIONS>
```
- **Step** is a whole number. Units with the same step run at the same time; a unit that consumes another's output gets a later step.
- **Owns** lists files or globs separated by commas (`src/ui/**, src/app.ts`). A leader's row may name its ticket instead (`ticket P2-03`). A solo engineer building a whole ticket alone in its worktree may own `**` (nobody else works there).
- Tables with a single unit are optional (see the playbook). Every row has an assignment file `.coniso/assignments/<unit>.md` before validation.
- Run `CONISO validate-plan <table file>` and fix its findings until it prints `PLAN OK`. It enforces D3 (one owner per file within a step), D5 (each assignment file has an absolute WORKDIR, OBJECTIVE, OWNS, DONE WHEN and RETURN) and D6 (units per step within the slot limit). Only then does the auditor see the plan, for the judgment rules.

## Audit request (the auditor's assignment file, or the one-line prompt)
`AUDIT <delegation|verdict|handoff> <file to audit> · context: <one or two paths>`

## Auditor reply
```
NO OBJECTIONS
— or, at most 6 —
O1 [blocker|major|minor] <rule id> · <unit> — <problem> — evidence: "<≤10 words quoted>" — fix: <≤12 words>
```
The leader then:
1. writes `O<n> accepted` or `O<n> overruled: <reason>` into the audited file;
2. runs `CONISO ledger add --unit <u> --leader <role> --kind <kind> --raised N --accepted A --overruled O [--reason "O2: …"]`.

## Verdict — a reviewer's return body, also at the top of its report
```
VERDICT <ticket> · <reviewer unit> · lens <lens>: pass | pass-with-fixes | fail
A1 met — <evidence: test name / file:line / log line>
A2 unmet — <evidence>
FINDINGS: [blocker|major|minor] <claim> — <evidence>   (one per line; "suspicion" marks one without evidence)
TESTS: <CONISO test result · log path>
FIX LIST: .coniso/reports/<ticket>.fix.md   (fail only)
RULING: <line printed by CONISO verdict>
```
`CONISO verdict <id> --report <file>` parses this block. It voids a verdict that skips an acceptance item or gives one no evidence; any blocker or unmet item fails; minors never fail. It prints `RULED pass|fail` when those rules settle the round, and `NEEDS RULING: <reason>` for a panel split, a major, a void, or a reviewer verdict the rules contradict.

## Fix list — `.coniso/reports/<ticket>.fix.md`
Number each problem and give for each:
- where it is (file:line);
- what is wrong;
- what "fixed" looks like as a check.

A builder must be able to execute it without guessing.

## Relay spawn file — `.coniso/assignments/<unit>.spawn.json` (leader lacks the Agent tool)
```json
{"from": "P2-03", "return_to": "the P2-03 lead",
 "spawns": [{"agent": "coniso:engineer", "model": "sonnet", "unit": "P2-03.E1", "assignment": ".coniso/assignments/P2-03.E1.md"}]}
```
Return `STATUS: relay` with the file path. The main session spawns the units and resumes you with "Relay done".

## Status line to the user
`Wave <n>: merged … · in review … · building … · blocked … — next: …`
