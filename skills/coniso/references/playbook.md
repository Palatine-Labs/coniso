# Playbook — routing, effort, escalation, budget

## Model tier per packet
| Packet | Model |
|---|---|
| Mechanical and fully specified: renames, format migrations, boilerplate from a spec, an extra export over existing data, config tweaks | haiku |
| Well-specified work following an existing pattern or a written spec: panels, forms, CRUD, follow-on calculators, tests for stated criteria | sonnet |
| Load-bearing work: data model, engine core, the first of a kind that later work copies, security, hard stateful logic, real debugging, anything escalated | opus |

**Tiebreaker:** if a mistake would be caught by tests or by the user clicking through, go cheaper. If it would pass silently into numbers, data or user-facing output, go up a tier.

When unsure between two tiers, take the cheaper one. The reviewer pass is cheaper than over-provisioning.

## Effort
Effort is set per role in agent frontmatter. The Agent tool cannot override it per call.

| Role | Effort |
|---|---|
| engineer | medium |
| lead | high |
| manager | high |
| review lead | medium |
| reviewer | high |
| auditor, scout | none (haiku has no effort setting) |

A roadmap ticket's effort shows up two ways instead:
- `xhigh` puts the ticket under a reviewer panel and makes it a team even when its size is S or M. `review: true` also gets a panel.
- The ticket's model sets the lead's model.

## Formation (schedule decides; leaders may refine)
| Ticket | Formation |
|---|---|
| size XS (not xhigh) | patch: one engineer at the ticket's model, told to make the smallest change that meets the acceptance items; one light reviewer |
| haiku model | solo engineer at haiku; reviewed in a batch with other haiku tickets |
| size S or M (not xhigh) | solo engineer at the ticket's model; single reviewer |
| size L | team: lead at the ticket's model + 3 engineers (4 for opus) |
| xhigh at size S or M | team: lead at the ticket's model + 1 engineer (S) or 2 (M); reviewer panel |

XS is for a fix that stands alone: a bug found after a merge, a one-file change the user asks for. The engineer's assignment says so, and `CONISO review` measures the diff: over 80 lines or 4 files, the ticket gets its normal review (`PATCH UPGRADED`). The limit is a rule in the script, so the upgrade is never a judgment call to audit.

Why S and M go solo: one capable engineer holds a whole S or M ticket in a single context. Splitting it across parallel engineers creates file seams that a lead then has to stitch. Teams are kept for L tickets, where the work really divides, and for xhigh tickets, where the extra structure is the point.

Slot cost: 1 for a solo engineer. A team costs 2 + its engineers (lead + engineers + a transient auditor).

Sprints with more than 10 tickets, or more than 6 across several phases, get the managed formation: one manager per track (at most 3). Otherwise the formation is direct.

## Review allotment
| Ticket | Review |
|---|---|
| `review: true` or effort xhigh | panel: reviewer opus (lens correctness) + reviewer sonnet (lens spec+regression) |
| opus | single opus reviewer |
| other sonnet | single sonnet reviewer |
| size XS (patch) | one light reviewer (`coniso:reviewer-lite`, sonnet; opus on an opus ticket), lens spec+regression |
| haiku | batch light reviewer (sonnet) |
| re-review after a fix round | one light reviewer, `SCOPE: delta` (fix list + files the fix touched); sonnet, unless the ticket is opus and the failing major was a design or correctness one |
| minor fixes before merge | tests, docs or comments only: none (the merge test is the evidence); production code: one sonnet delta check |

The Review Lead may upgrade a review (big diff, load-bearing files) but never downgrade one.

**Reviewer budget.** `CONISO review` measures each diff and writes a BUDGET line into the reviewer's assignment: 12 tool calls and 1 new probe under 60 changed lines, 25 and 2 under 300, otherwise 40 (60 at opus) and 2; delta and patch reviews get 12 or 18. Big diffs keep the full ceiling; small ones stop paying for it. The light reviewer (`coniso:reviewer-lite`, sonnet at medium effort) takes the small diffs: patch tickets, delta re-reviews and haiku batches. It holds the same evidence standard, and when a diff is too big for its budget it returns `FLAGS: full review recommended` instead of stretching; the leader then re-dispatches a full reviewer.

**Ruling:**
- Any blocker fails the ticket.
- Majors fail it unless the panel majority passes it.
- Minors never fail a ticket; they become notes.
- A verdict without evidence for every acceptance item is void.

## Verdict audit
| Ruling | Audit |
|---|---|
| fail | yes |
| pass on an opus or `review: true` ticket | yes |
| a ruling that departs from its reviewer (severity changed, finding overruled, a Director design rule added) | yes |
| pass or pass-with-fixes on a sonnet or haiku ticket that follows its reviewer | no |
| delta checks, and minor fixes to tests, docs or comments | no |

## Escalation ladder
1. First fail: fix list to the same unit.
2. Second fail: one tier up (haiku → sonnet → opus); a solo ticket becomes a team.
3. Third fail: blocked, with the decision the user must make.

Every escalation is recorded with `CONISO set --model`.

## Auditor — the adversary of every leader
- **Mechanical rules go to the script, not the auditor.** Every delegation plan first passes `CONISO validate-plan`, which enforces D3 (one owner per file within a step), D5 (complete assignment files with absolute WORKDIRs) and D6 (units per step within the slot limit). It is free, instant and never wrong about these, so the auditor no longer spends tokens on them.
- **The auditor keeps the judgment calls:** model tier (D1), packet size (D2), ordering (D4), reviewer independence (D7) and scope creep (D8) on delegation plans that deviate from the schedule, plus review rulings (V1–V6) per the Verdict audit table.
- **The script takes the rest.** `validate-plan` prints `MATCHES SCHEDULE` when every row is a ticket-level unit (a lead, a solo engineer, a reviewer) at exactly the scheduled model and formation, with a script-written assignment. The roadmap and the schedule already settled D1, D2, D4 and D8 for such a plan, so it skips the delegation audit. A lead's packet plan never matches: splitting a ticket is judgment. `validate-handoff` checks a lead's hand-off claim (H1–H3), and `verdict` applies the mechanical ruling rules.
- It runs one model tier cheaper than the leader (opus → sonnet, sonnet → haiku).
- Auditors raise only objections that would change a verdict, a severity, a fix-list item, or a plan's model, size or order. Wording, citation format and log-path style are not objections. Each audit uses at most 8 tool calls.
- The exemptions: a dispatch of exactly one unit whose model and formation match the scheduled plan, and a plan that validate-plan marks `MATCHES SCHEDULE`. Single-unit dispatches also skip validate-plan (nothing for D3 or D6 to check), but their assignment still carries every formats.md field.
- Each objection must cite a rule from the auditor agent.
- The leader accepts or overrules each objection with a reason and logs it to the ledger.
- The retro report shows overrule counts per leader. Overrules that later failed review mark that leader's routing as the thing to fix.

## Budget
- Cut breadth (fewer concurrent units) before cutting review.
- Never skip the reviewer on a ticket that merges.
- The schedule keeps 4 slots for review out of the 20-agent session cap.
- **Usage habits that keep review intact:**
  - Engineers run narrow checks while iterating and `CONISO test` once, at the end.
  - Reviewers rerun the builder's probes first, add at most 2 new adversarial probes and 1 mutation per critical path, and run `CONISO test` once, after their last restore.
  - A reviewer who finds one instance of a class of defect (a rejected name, a missed edge) sweeps the whole class from the source in the same review, so one fix round covers it.
  - `director continue light` halves concurrent builds and keeps single reviewers for maintenance sessions.
- **One test run per tree.** `CONISO test` records the worktree's tree id with a passing run and reuses it while the tree is unchanged (`PASS (cached …)`), so the reviewer's run after the builder's costs nothing. `land` reuses it again when the squash is byte-identical to that tree, and several passed tickets land as one batch with one integration run (a failure falls back to one ticket at a time). The happy path runs a ticket's suite once instead of three times; `--fresh` forces a rerun anywhere.

## Context discipline
- Paths, not contents.
- Every agent returns at most 12 lines.
- A scout's map is at most 60 lines.
- Long test output lives in log files; `CONISO test` prints only the tail.
- Agents read files in ranges and Grep before Read.
- Agents never echo file contents back to the agent that assigned them.
