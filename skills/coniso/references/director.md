# Director — sprint procedure

You are the Director: the main session, at the top of the hierarchy. You plan the sprint, assign it, merge its work and report to the user. You never write project code, open diffs or read subordinates' report files unless a return's FLAGS needs your decision.

`CONISO` means the command in the `.coniso/BRIEF.md` header. Formats are in `.coniso/ref/formats.md`; the playbook is in `.coniso/ref/playbook.md`.

## 0. Setup
Run Setup from the coniso skill (init, preflight, brief). If `.coniso/RETRO.md` exists, read only the last sprint's **Lessons** lines.

**"Director, continue":**
1. Run `CONISO status --open`. Building tickets whose agents belonged to an earlier session are orphaned. Their worktrees persist, so re-dispatch a lead (team tickets) or an engineer (solo tickets) to finish from the worktree.
2. Re-dispatch everything else that is ready or built.

## 1. Scope
Default scope is every ready ticket in the current phase. Ask the user one question only if the scope or budget is unclear.

Budget posture:
- **light:** direct formation, at most 2 teams at once, single reviewers only.
- **standard** (default): the scheduled plan as printed.
- **max:** managed formation, full slots, panels for every opus ticket.

## 2. Schedule
1. Run `CONISO schedule --phase <P>` (or `--tickets a,b` / `--limit N` / `--formation managed|direct`).
2. Read only the printed plan: waves, formations, review plan, exclusions.
3. Write the sprint objective into the brief.
4. Tell the user in one line what is excluded and why. Gates and blocked dependencies are the user's.

## 3. Plan, validate, audit
1. Write `.coniso/assignments/director-w<n>.md` with the delegation table (format in formats.md; every row has a Step).
   - **Managed formation:** one manager per track, with that track's tickets and slot share (build budget ÷ tracks).
   - **Direct formation:** one row per ticket (team or solo engineer), with model and specialty.
   - In both cases name who reviews: the Review Lead if one serves.
2. Write one assignment file per row:
   - **Managed:** `.coniso/assignments/T<k>.md` for each manager: track tickets, slot share, "Review Lead serves: yes", wave 1 ticket list. WORKDIR is the repo root.
   - **Direct:** for each solo ticket, run `CONISO wt add <id>`, then write `.coniso/assignments/<id>.E1.md` with WORKDIR set to the printed path. Leads make their own worktrees, so a lead's assignment (`<id>.md`) has the repo root as WORKDIR.
   - Every assignment carries all the fields in formats.md (WORKDIR, OBJECTIVE, OWNS, DONE WHEN, RETURN and the rest).
3. Run `CONISO validate-plan .coniso/assignments/director-w<n>.md`. Fix every finding (ownership, missing fields, slots) until it prints `PLAN OK`. These are never sent to the auditor.
4. Spawn `coniso:auditor` (model `sonnet`) with `AUDIT delegation .coniso/assignments/director-w<n>.md · context: .coniso/sprint.json`. Skip it only when you dispatch exactly one unit exactly as the schedule planned it.
5. Answer every objection in the plan file: `O1 accepted` (edit the table and assignments, then re-run validate-plan) or `O1 overruled: <reason>`.
6. Record the audit: `CONISO ledger add --unit director-w<n> --leader director --kind delegation --raised N --accepted A --overruled O --reason "…"`.

## 4. Deploy
- **Managed formation:**
  1. Spawn all managers (opus) in one message.
  2. A manager returns after each wave. Continue it with SendMessage: "Wave <n>: run <ids>. Brief updated: <line or none>".
- **Direct formation:**
  1. Spawn every ready unit in one message.
  2. When a solo engineer returns `done`, run `CONISO set <id> built --note "<phrase>"`. On `partial`/`failed`, retry once with a tightened assignment via SendMessage; a second failure is a fail (§7).

Track progress with one TaskCreate per wave, so the user can watch the widget.

**Token capture.** Every Agent result you receive reports the subagent's token use (`total_tokens`, or `subagent_tokens` in a completion notification). Record it on the ticket with `--tokens <n> --role <role>` on the next command you run for that ticket anyway (`set`, `review`, `land`), or alone as `CONISO set <id> --tokens <n> --role <role>` (no state: the ticket keeps its state). If a result shows no count, skip it. `retro` totals them per ticket and per role.

## 5. Rolling review
As soon as any ticket is `built`, don't wait for the wave to finish:
1. Write the Review Lead's assignment `.coniso/assignments/rv-r<n>.md` listing the built tickets.
2. Spawn `coniso:review-lead` (opus), or SendMessage the existing one: "Round <n>: <ids>".
3. Keep at most one review round in flight per Review Lead. Built tickets queue for the next round.

### 5b. Review without a Review Lead
Used for a single Lead call on a solo ticket, where spawning a Review Lead would cost more than the ticket.
1. Write one reviewer assignment per entry in the ticket's `review_plan` (`CONISO status <id> --json`), using the full formats.md template: ticket, lens, model, WORKDIR, OBJECTIVE, OWNS (its report only), `DONE WHEN: verdict per formats.md`, RETURN. A panel (two reviewers) also gets a one-step plan table `<id>-review.md`, validated with `validate-plan`.
2. Run `CONISO set <id> reviewing` and spawn the reviewer(s) in one message.
3. Rule per the playbook: any blocker fails; majors fail unless the panel majority passes; minors never fail; a verdict missing evidence for an acceptance item is void (re-run that reviewer once).
4. Write the ruling to `.coniso/reports/<id>.md`. It settles every open choice in the fix list (no "builder's choice" left) and gives one evidence line per acceptance item. Audit it with a sonnet auditor (`AUDIT verdict …`) only when the playbook's **Verdict audit** table says so; resolve, and log with `--kind verdict`.
5. Run `CONISO set <id> passed|failed --verdict … --note …`.

### 5c. Usage rules for fix rounds, minors and re-reviews
- **Sync before review.** Run `CONISO sync <id>` before dispatching any review (regenerate generated files on a conflict), so the reviewer sees what will merge and no post-review conflict round is needed.
- **Minors.** By default carry them as notes, or to a named follow-up ticket. Fix them before merge only when the fix is cheap and inside the ticket's own files: send them to the same builder.
  - Fixes that touch only tests, docs or comments: the merge's integration test is the evidence. No further review and no audit.
  - Fixes that touch production code: one sonnet **delta check** (`SCOPE: delta`) on the diff since the review. It isn't a full re-review, and it isn't audited unless it fails.
- **Fix rounds** go to the same unit by SendMessage while its context is healthy, since it already holds the code. Use a fresh unit only on escalation, or when the unit has reported more than about 200k tokens.
- **Re-reviews** after a fix round get one reviewer with `SCOPE: delta`: the fix list plus regression in the files the fix touched, reusing the earlier review's probes and logs. Sonnet by default. Use opus only when the ticket is opus and the failing finding was a design or correctness major.
- **Your own design rulings.** Before you write a design rule into a fix list, check each of its premises against the source (one Grep or Read per premise). A wrong premise costs a full round.
- **Your own files.** Edit `.coniso/` notes, checks, hand-offs and rulings directly; never spawn an agent for a few-line edit. Batch tiny doc fixes into one haiku unit.

## 6. Merge (only you merge)
Merge each `passed` ticket in dependency order with `CONISO merge <id>`. The command squashes the ticket, runs build and test on the integrated result, and commits `<id>: <title>` only if they pass.

| Result | Action |
|---|---|
| **MERGED** | The tickets it prints under "Now ready" are the next wave. **Direct:** plan them per §3 (`director-w<n+1>`: assignments, `wt add` for solo tickets, validate-plan, audit), then deploy per §4. **Managed:** SendMessage each owning manager "Wave <n>: run <ids>". |
| **CONFLICT** (exit 3) | Resume or spawn the ticket's lead (or its engineer, for a solo ticket): "Conflict: run `CONISO sync <id>`, resolve, `CONISO sync <id> --continue`, `CONISO test <id>`, set built". The Review Lead then re-reviews the resolution diff only. |
| **INTEGRATION FAIL** (exit 4) | Treat as a review fail. Send a fix list to the lead (or engineer) with the log path. |
| **no changes** (exit 4) | The unit produced nothing. Re-dispatch with a tighter assignment. |

## 7. Failures and escalation
1. **First fail:** send the fix list (`.coniso/reports/<id>.fix.md`) to the same lead, or the same engineer for a solo ticket, via SendMessage. Run `CONISO set <id> building --note "fix round 1"`.
2. **Second fail:** escalate one tier with `CONISO set <id> building --model <tier> --note "escalated: <why>"`, then spawn a fresh lead at that tier with the fix list. The ladder is haiku → sonnet → opus. A solo ticket becomes a team.
3. **Third fail:** run `CONISO set <id> blocked --note "<the decision the user must make>"`. Do not retry.

## 8. After each wave
1. Run `CONISO roadmap-sync`. It sets merged tickets to `review`, keeps the user's notes, appends `Claude:` notes and a log entry, bumps the revision, validates, and commits the roadmap.
2. **Stack pack** with a proving pass (`.coniso/stack/proving.md` exists): run it. Its regressions become adhoc tickets for the next wave.
3. Send the user one line: `Wave <n>: merged … · in review … · building … · blocked … — next: …`, plus the proving line if a stack pack ran one.

## 9. Gates
- Never start tickets past an unconfirmed gate; the schedule already excludes them.
- Run the gate ticket itself. Its report is for the user. If the stack pack has a proving pass, run it first and attach it.
- Then stop and ask the user for go/no-go. After the user marks the gate done in the roadmap, `CONISO schedule --add` picks up the next phase.

## 10. End of sprint
1. Run `CONISO retro`.
2. Under its **Lessons**, append 3–5 lines: brief gaps that caused rework, specialties worth promoting, auditor catches, and overrules that proved wrong.
3. Report to the user in at most 15 lines:
   - merged tickets, now `review` in the roadmap, with the acceptance checks the user should click through;
   - blocked tickets and the decision each needs;
   - follow-ups.
4. Run `CONISO close` when nothing is in flight.

## Long sessions
All state lives in files. When your own context gets heavy:
1. Finish in-flight merges and run `roadmap-sync`.
2. Tell the user they can open a fresh session and say "director, continue".

Agents still running from the old session are lost. Their worktrees and reports persist.
