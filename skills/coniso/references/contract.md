# Coniso's contract with local tools

A project's own scripts (a launcher, a dashboard, a hook) may call `.coniso/coniso.py` and read its output and files. Everything on this page holds for every release of major version 1: nothing listed here is removed, renamed or changes meaning. New commands, flags, markers, exit codes and config keys may be added, so a tool should ignore lines and keys it does not know.

Anything not on this page (other output lines, the wording after a marker, the layout of `sprint.json` beyond the fields below, files under `.coniso/ref/`) can change in any release.

## Where your own things go

`.coniso/local/` is yours. `init` (also when it upgrades Coniso) and `close` never write, move or delete anything in it. Put helper scripts and standing notes for agents there. Other files you add at the top of `.coniso/` are also left alone, but `init` lists them as yours each time.

## Commands

The commands below keep their names, positional arguments and the flags shown. Run them from the repo root as the `cmd` value in `.coniso/config.json` (the same command the brief's header gives).

| Command | Stable form |
|---|---|
| status | `status [id] [--json] [--open] [--field name]` |
| set | `set <id> [state] [--note text] [--verdict v] [--model m] [--tokens n --role r]` |
| test | `test <id\|main> [--fresh]` |
| diff | `diff <id>` · `diff --range A..B` |
| dispatch | `dispatch <ids> [--specialty s] [--review lead\|self] [--from unit] [--plan name] [--rewrite]` |
| review | `review <ids> [--delta] [--panel [ids]] [--model sonnet\|opus] [--no-sync] [--from unit] [--plan name]` |
| verdict | `verdict <id[,id]> --report <file>` |
| sync | `sync <id> [--continue] [--force]` |
| land | `land <ids> [--fresh] [--each] [--tokens n]` · `land --recover` |
| merge | `merge <id> [--fresh]` |
| quickfix | `quickfix <id> begin\|done --note text\|abort` |
| revert | `revert <id> --reason text` |
| slot | `slot [--now]` |
| quiet | `quiet [--runs n] [--out dir] [--cwd dir] -- <command>` |
| decisions | `decisions [--grep term] [--all]` |
| lesson | `lesson <spec> "<text>" [--source s]` · `lesson <spec> --retire match --by reason` |
| memory | `memory` |
| preflight | `preflight` |

## Markers

A marker is the start of a printed line. The regex is what a tool may match; the text after it is for people and may change.

| Marker | Regex | Printed by | Means |
|---|---|---|---|
| `TEST <id>: PASS` | `^TEST (\S+): PASS\b` | test | the checks passed; `(cached: …)` follows when an identical tree already passed |
| `TEST <id>: FAIL` | `^TEST (\S+): FAIL\b` | test | a check failed, or the secret scan found a credential |
| `TEST <id>: SKIPPED` | `^TEST (\S+): SKIPPED\b` | test | no build or test command is configured |
| `TEST <id>: BUSY` | `^TEST (\S+): BUSY\b` | test | no check slot came free in time: run it again |
| `RULED` | `^RULED (pass\|fail) (\S+)` | verdict | the review rules settled the round and set the state; ` · WITH FIXES:` follows on a pass that carries findings |
| `RECORDED` | `^RECORDED (\S+) (\S+)` | verdict | one verdict of a panel recorded; the others are still running |
| `NEEDS RULING` | `^NEEDS RULING (\S+):` | verdict | the rules do not settle it: a leader rules |
| `REVIEWING` | `^REVIEWING (.+)$` | review | reviewer assignments written; `SPAWN` lines follow |
| `SPAWN` | `^SPAWN coniso:(\S+) · model (\S+) · prompt: Assignment: (.+)$` | dispatch, review | spawn exactly this agent, at this model, with this prompt |
| `PLAN OK` / `PLAN FAIL` | `^PLAN (OK\|FAIL) (\S+)` | dispatch, review, validate-plan | the delegation table passed or failed the script's checks |
| `MERGED` | `^MERGED (\S+)` | merge, land | the ticket is committed on the sprint branch |
| `WITH FIXES` | `^WITH FIXES (\S+):` | land | a ticket being landed passed with findings carried |
| `BATCH` | `^BATCH (\d+) tickets` | land | several tickets landed with one integration run |
| `BATCH RESET` | `^BATCH RESET:` | land | a batch did not land; the branch was not moved |
| `CONFLICT` | `^CONFLICT (\S+):` | merge, land | the squash conflicts with the branch |
| `INTEGRATION FAIL` | `^INTEGRATION FAIL (\S+)` | merge, land | the checks failed on the integrated result; nothing committed |
| `FLAKY AT MERGE` | `^FLAKY AT MERGE (\S+)` | merge, land | first flake ever seen at a merge; land again once looked at |
| `CHECKS MODIFIED FILES` | `^CHECKS MODIFIED FILES (\S+)` | merge, land | a check rewrote tracked files; nothing committed |
| `SECRETS BLOCKED` | `^SECRETS BLOCKED (\S+)` | merge, land | a credential in the squash; nothing committed |
| `BUSY land` | `^BUSY land ` | land, merge | no check slot came free for the land; nothing was touched |
| `RECOVERED` | `^RECOVERED an interrupted land` | land | a land whose process died was cleaned up |
| `WAVE` | `^WAVE (\S+) landed` | land | the wave is merged and the roadmap synced |
| `Now ready` | `^Now ready: (.+)$` | merge, land | tickets whose dependencies are now all merged |
| `QUICKFIX` | `^QUICKFIX (OK\|TOO BIG\|TEST FAIL\|BUSY\|\S+ open\|\S+ aborted)` | quickfix | the state of a Director's quick fix |
| `REVERTED` | `^REVERTED (\S+):` | revert | a merged ticket was reverted and queued again |
| `HANDOFF OK` / `HANDOFF FAIL` | `^HANDOFF (OK\|FAIL) (\S+)` | validate-handoff | a lead's hand-off claim passed or failed |
| `GO` | `^GO ` | slot | a check run may start now |
| `BUSY` | `^BUSY ` | slot | no check slot came free in time |
| `QUIET` | `^QUIET ` | slot --now | a quiet timing run holds the machine |
| `QUIET RUNS` | `^QUIET RUNS (\d+) of (\d+)` | quiet | how many counted runs exited 0 |
| `CONISO ERROR` | `^CONISO ERROR: ` | any | the command refused or failed; the text says why |

## Exit codes

| Code | Means |
|---|---|
| 0 | done |
| 1 | failed or refused: a check failed, a precondition did not hold (the line after `CONISO ERROR:` says which) |
| 2 | `test` found no check to run (`SKIPPED`), or the command line was wrong |
| 3 | a git conflict: `merge`, `land`, `sync` or `revert` |
| 4 | the integrated result failed: `INTEGRATION FAIL`, `FLAKY AT MERGE`, or a ticket with no changes |
| 5 | a git hook refused the commit |
| 6 | `SECRETS BLOCKED` |
| 7 | `CHECKS MODIFIED FILES` |
| 8 | something else holds it: a land is running, a quiet run is going, the ticket is in review, or an interrupted land needs `land --recover` |
| 9 | busy: no check slot came free in time, or the machine never went quiet. Run it again later |

## Files

| Path under the repo root | Who writes it | A tool may |
|---|---|---|
| `.coniso/config.json` | `init` | read it; change keys with `init` flags |
| `.coniso/sprint.json` | the script | read `name`, `branch` and per ticket `state`, `wave`, `wt`, `branch`, `base`, `merged_sha`, `history`; never write it: use `set` |
| `.coniso/BRIEF.md` | the Director | read it |
| `.coniso/tickets/<id>.md` | `schedule`, `adhoc` | read it |
| `.coniso/assignments/<unit>.md` | `dispatch`, `review`, leaders | read it |
| `.coniso/reports/<unit>.md`, `<id>.fix.md` | agents | read them |
| `.coniso/reports/<id>.test.log`, `<id>.merge-test.log` | `test`, `merge`, `land` | read them |
| `.coniso/flaky.jsonl` | `test`, `merge`, `land` | read it: one JSON object per flaky run |
| `.coniso/landing.json` | `land`, `merge` | read it: present while a land runs (`pid`, `ids`, `branch`, `start`) |
| `.coniso/slots/` | the script | nothing: use `slot` and `quiet`, never write claims by hand |
| `.coniso/wt/<id>/` | `wt`, `dispatch` | read it; write only as the ticket's builder |
| `.coniso/local/` | you | anything |
| `.coniso/backup/` | `init` | read it: the script copies `init` replaced |
