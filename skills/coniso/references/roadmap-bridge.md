# Roadmap bridge — how sprints read and write the HTML prompt roadmap

The roadmap stays the record that carries work from session to session; Coniso is how a long session gets through a block of it. Coniso never changes the renderer, and it never edits the JSON by hand. `CONISO schedule` reads it and `CONISO roadmap-sync` writes it, following `roadmap.md`. Everything else about the roadmap (creating it, status sync, expanding phases) is in `roadmap.md`.

## Reading (schedule)
| Roadmap field | Coniso use |
|---|---|
| `status` | Only `todo` is scheduled (`--include-progress` adds `progress`). `review` and `done` satisfy dependencies; `--strict` accepts only `done`. |
| `deps` | Dependencies inside the sprint become wave order. Outside dependencies must be satisfied, or the ticket is excluded with the reason. |
| gate tickets (title contains "Gate") | Can run, but nothing that depends on one starts until the user marks it `done`. |
| `size` S/M/L | Formation (solo engineer for S and M; a team of a lead and 3–4 engineers for L) and slot cost. |
| `model` | Lead model (or solo engineer model). Haiku tickets are batched for review. |
| `effort` xhigh / `review: true` | Reviewer panel (xhigh also makes S and M tickets a team). |
| `why` / `what` / `accept` / `prompt` / `modelWhy` / `notes` | Copied into `.coniso/tickets/<id>.md`, the only ticket text agents read. `accept` items become numbered acceptance criteria (A1, A2…). |
| `meta.preamble` | Seeds "Project rules" in the brief at init. |

## Writing (roadmap-sync)
| Sprint state | Roadmap status | Note appended |
|---|---|---|
| merged | `review` (never `done` — the user confirms by clicking through) | `Claude: Coniso sprint <name> merged <sha> on <branch> — reviewer verdict …, attempts n.` |
| blocked | `blocked` | `Claude: Coniso sprint <name> blocked after n attempt(s): <reason>` |
| building / built / reviewing / passed / failed / conflict | `progress` | `Claude: Coniso sprint <name> in progress.` |

Every sync also does the following:
- It keeps the user's existing notes and adds Coniso's as separate `Claude:` lines.
- It sets a fresh `updated` on each changed ticket, so the file beats stale browser storage for those tickets. Tickets Coniso didn't touch keep their timestamps, so the user's browser clicks still merge.
- It appends one `log` entry, bumps `meta.revision` and sets `meta.updated`.
- It validates against the schema in `roadmap.md` before writing. A pre-existing invalid file blocks the write until it is fixed.
- It never touches a ticket the user already marked `done`.
- It commits only the roadmap, as `coniso: roadmap sync (<sprint>, rev n)`, and only when nothing else is uncommitted.

## Choosing model and effort
When Coniso proposes new tickets (Review Lead sweeps, findings), it follows `roadmap.md`'s rules for model and effort: only keys in the roadmap's `models[]`, no `max` effort, and xhigh kept to about one ticket in seven. Add them per `roadmap.md`, one ticket per finding, with a test-first prompt.

## Ad-hoc tickets
`CONISO adhoc <id> …` creates tickets that exist only in the sprint. They merge as `<title> (<id>)` and roadmap-sync skips them. If one turns out to matter, add it to the roadmap afterwards.
