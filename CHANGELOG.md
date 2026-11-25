# Changelog

All notable changes to Coniso are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.0-dev.3] - 2026-11-25

Dev pre-release. Fixes the faults Project Sol's Director hit on 1.2.0 that dev.2 still had, and takes in what that project had to build for itself (a cap on check runs, a land's priority, quiet timing runs), so upgrading stops costing a project its own tools.

### Fixed
- **A command that passes its time limit is stopped with everything it started.** dev.2 killed one process: with a shell, that was the shell, and the check run under it kept going and held the output pipe open on Windows, so the call returned only when the grandchild ended and lost the output written before the limit. `run` now stops the whole tree (`taskkill /T` on Windows, the process group elsewhere) and keeps the output. Ported from Project Sol. A run that hits its limit is not rerun as flaky.
- **`review <id> --model opus` writes an opus reviewer** on a sonnet ticket, in the assignment's first line and the spawn line. The flag reached only delta reviews.
- **`verdict` rules a single reviewer's round** when the report says `R1` instead of `P2-03.R1`. dev.2 recorded the verdict under the short name and printed "waiting on" the real unit forever.
- **Findings under a `## FINDINGS` heading are counted.** dev.2 read only a `FINDINGS:` line, so a pass-with-fixes report with three numbered minors ruled `RULED pass … m0`. A report with no findings section the parser knows has its tags counted wherever they are. A pass that carries findings now says `· WITH FIXES: <n> minor finding(s) to read before landing: <report>` in its `RULED` line, and `land` prints `WITH FIXES <id>` for it.
- **A land never moves the branch before its checks pass.** A batch land used to commit its squashes on the branch and test them there, so a review opened during the land synced untested commits into its ticket. It now commits on a detached HEAD in the main checkout (build caches stay warm) and moves the branch only on a pass. One land at a time; a land whose process died is recovered by the next land or `land --recover`.
- **`sync` refuses a ticket in review** (exit 8; `--force` when no reviewer is running). One left conflict markers under a running reviewer. `review` already had `--no-sync`.
- **`quickfix` takes a `built` ticket.** Its first review then covers the fix; a ticket in review is still refused, with a message that says to wait for the verdict.

### Added
- **Check slots** (`init --check-slots N`): at most N check runs at once across every agent, claimed under a lock so waiting agents cannot all start together. `test` waits up to `check_slot_wait_seconds` (540) and then prints `TEST <id>: BUSY` (exit 9). `--check-pattern <regex>` also counts runs started by hand; `slot` is the wait for an agent that starts one. A land goes ahead of waiting tests and starts when `land_max_others` (1) runs are left; `--land-hold` keeps new runs out for its length. Off unless configured.
- **`quiet --runs N -- <cmd>`**: timing runs on a quiet machine. It holds new check runs, waits for running ones, then for no busy process outside its own family (any program, not only builds: a stray `python -` counts), and keeps the busy list before and after each run; a disturbed run is not counted.
- **`preflight` lists leftover processes**: build and tool processes whose parent is gone, usually left by an earlier session's agents. It never stops them.
- **`--merge-timeout`** for a land's checks, **`--test-timeout`**, and **`--env KEY=VALUE`** for the environment checks need.
- **A contract for local tools** (`.coniso/ref/contract.md`): the commands, printed markers, exit codes and file locations that stay the same within major version 1, checked by a test.
- **`.coniso/local/`**, the project's own folder: `init` and `close` never touch it.
- **Memory from the project's own records.** `--decisions-dir` reads a decision-record folder (one decision per file; `Status: Superseded` retires one; a ticket that cites `decision 0098` gets it on its DECISIONS line). `--house-rules` adds standing notes to every engineer's and reviewer's READ FIRST. `--lessons-dir` moves the per-specialty lessons.

### Changed
- **`init` over an open sprint says what it did**: what it replaced, what it kept (the sprint, tickets, assignments, reports and every file it did not install), and it keeps a script copy that differs from the new one in `.coniso/backup/`. A test upgrades a real 1.2.0 folder with a ticket in review and finishes the sprint on the new script.
- New exit codes: 8 (something else holds it: a land, a quiet run, a ticket in review) and 9 (busy: run again).

## [1.3.0-dev.2] - 2026-11-24

Dev pre-release. Fixes to 1.3.0-dev.1's pipeline checks and memory, found by probing them with ordinary code.

### Changed
- **The secret scan blocks only on shapes that are a credential by themselves**: private keys, AWS, GitHub, Slack, Google, Anthropic and OpenAI-style keys, live Stripe keys, Supabase service keys, and `.env`, SSH key, key-store and credentials files. In dev.1 it also blocked on `TOKEN_TYPE = "authorization_code_grant"`, `password_reset_template = "emails/password_reset.html"`, local `postgres://user:pass@db` URLs, test fixtures and public `.pem` certificates. Those shapes (JWTs, token-ish assignments, credentialed URLs, `.pem`/`.key`/`.crt` files) are now `SECRET HINTS`: `test` prints them, they never block, and the reviewer rules on each. An assignment is a hint only when its value mixes letters and digits and is not a path.
- **A flaky pass no longer gets through the merge gate unseen.** At `merge` and `land`, a pass on the rerun counts only if this test command has flaked before. The first flake ever seen at a merge fails it (`FLAKY AT MERGE`, exit 4), main is reset, and the ticket keeps its state; once on record, a retried land passes. A batch land stops instead of falling back to one-at-a-time, which would pass on the now-recorded flake. A ticket's own `test` keeps the old behaviour. `merge_flaky: "pass"` in config turns the gate check off.
- **A failing run is not repeated when it took over `flaky_retry_max_seconds`** (default 900), so a genuine failure in a long suite costs one run, not two.
- **Decision scoping.** A Decisions row binds every ticket only when its Binds cell is exactly a word like `all`, `project` or `everyone`. In dev.1, `all P2 tickets` and `project settings UI` bound every ticket in every phase.
- **`revert` updates the roadmap**: the ticket goes back to `todo` with a `Claude:` note and the roadmap is committed. A ticket you marked `done` keeps its status and gets the note.

### Fixed
- **A check that writes files no longer stalls the sprint.** A formatter run with `--write`, a build that regenerates a tracked file, or a test that writes into the repo used to leave main dirty after a passing merge, and every later merge then refused to run. `merge` and `land` now fail with `CHECKS MODIFIED FILES` (exit 7) when a check rewrote a tracked file: they name the files and reset main, and the ticket keeps its state. New untracked files a check leaves behind (a cache folder, build output) cannot change what is committed, so they are removed with a note and the merge goes on. A ticket's own `test` warns about rewritten files and does not cache that pass.
- Binary key stores (`.p12`, `.pfx`, `.jks`, `.keystore`) were never caught: git prints no `+++` line for a binary file. The scan now reads the binary-file header too.

## [1.3.0-dev.1] - 2026-11-24

Dev pre-release on the `dev` branch: install with `/plugin marketplace add Palatine-Labs/coniso#dev`. `main` stays on 1.2.0 until this is promoted.

### Added — pipeline checks
- **Lint, type check and format check.** `init --lint`, `--typecheck` and `--format-check` add commands that run before build and test, cheapest first, in `test`, `merge` and `land`. The first failure stops the run and is named (`CHECK FAILED: lint`). Pass `''` to clear one.
- **Secret scan.** `test` scans a ticket's diff, and `merge` scans the squash, for private keys, cloud and API tokens, live payment keys, JWTs, credentialed connection strings, hard-coded secret assignments, and files such as `.env` or `*.pem`. `test` fails; `merge` resets main, marks the ticket failed and exits 6 (`SECRETS BLOCKED`). A deliberate fake gets `coniso:allow-secret` on its line or its path in config `secret_allow`. `merge --allow-secrets` is for the user's say-so only.
- **Tests-unchanged warning.** `test` prints `TESTS UNCHANGED` when source files changed and no test file did, and records it on the ticket. Reviewers treat it as a finding unless the acceptance items need no test.
- **Flaky detection.** A failing test command is rerun once. A pass on the rerun counts as a pass, is marked `FLAKY` and recorded in `.coniso/flaky.jsonl`, and `retro` lists it. Set config `retry_flaky: false` to turn it off.
- **`revert <id> --reason …`** undoes a merged ticket with a revert commit on the sprint branch and sends it back to queued (refusing if a merged ticket depends on it). `retro` lists reverts with their reasons.

A memory pass. Lessons, decisions and retro notes now build up across sprints instead of being lost to trimming, empty retros or unrecorded pruning.

### Added
- **`lesson <spec> "<text>" [--source ID]`** appends a stamped lesson (`- [YYYY-MM-DD · source] …`) to `docs/agent-feedback/<spec>.md`. It refuses duplicates, and refuses a lesson that was retired unless `--force` is passed.
- **`lesson <spec> --retire MATCH --by "<reason>"`** moves one lesson to `<spec>.retired.md` with the date and reason, so condensing a file leaves a record and a dropped lesson isn't silently re-added. `retro` doesn't count `*.retired.md` toward the 30-line cap.
- **`decisions [--grep TERM] [--all]`** searches the brief's Decisions log and `.coniso/maps/decisions.md` together. Rows whose Decision starts with `SUPERSEDED` are hidden unless `--all` is passed.
- **`retro --lessons --sprints N`** prints the Lessons of the last N sprints that recorded any, each under its heading. The Director reads three at setup.
- `preflight` lists scout maps written 30 or more commits ago.
- **Decisions scoped to each ticket.** `dispatch` and `review` add a `DECISIONS` line to an assignment when a Decisions log row's Binds names the ticket, its phase or `all`. Agents get the few rows that bind them inline; superseded rows are left out. Leads copy it to their engineers.
- **Reviewers read the engineer's lessons.** `dispatch` records a ticket's specialty, and `review` puts that specialty's lessons file in the reviewer's READ FIRST. Breaking a lesson is a spec finding.
- **`memory`** reports lessons per specialty (and how many are retired), decisions in the brief, archived and superseded, retro sprints with lessons, and stale maps, with a list of issues. `preflight` prints the issues in one line.
- **Phase-gate memory review** (director.md §9): before go/no-go the Director promotes recurring retro lessons, retires what the phase made false, fixes `memory` issues, and adds a Memory block to the gate report for the user to approve.
- **Research ahead** (director.md §9): at a gate, the Director dispatches up to 3 docs scouts for libraries, APIs and versions the next phase needs that no map covers. Scout maps start with a `Researched:` date and versions line.
- Review failures feed lessons: at retro the Director turns a retried ticket's fix list into a `lesson` when a one-line rule would have prevented it. Fix lists name the regression test that locks each behaviour bug.

### Changed
- `retro --lessons` skips sections with no lessons recorded, so one empty retro no longer hides earlier lessons.
- Re-running `retro` in the same sprint rewrites that sprint's section and keeps its Lessons, instead of appending a second placeholder section.
- `brief-trim` also archives `SUPERSEDED` rows, and its pointer names the `decisions` command. Reviewers (spec lens) run `decisions --grep` for each area a diff changes, so archived decisions are still checked.
- Dispatch finds a lessons file kept under a specialty's legacy name (e.g. `audio-designer.md`) when the current name is missing.
- The engineer prompt reads the lessons file named in READ FIRST (main checkout) instead of the worktree copy, which can be older than the newest lesson.
- The brief template's Decisions log is no longer labelled "append-only". It gives the stamp format and the supersede rule.

## [1.2.0] - 2026-11-10

A usage pass on small work and on test runs. Review stays on every ticket that merges; what shrinks is the ceiling a reviewer is given for a small diff, the number of times a suite runs per ticket, and the context an auditor reads.

### Added
- **Patch tickets.** `adhoc --size XS` (and `XS` in a roadmap) makes a patch: one engineer told to make the smallest change that meets the acceptance items, then one light reviewer. `CONISO review` measures the diff and promotes an XS ticket over 80 changed lines or 4 files to its normal review (`PATCH UPGRADED`), by rule, so there is nothing to audit. An escalated patch loses the light review.
- **`coniso:reviewer-lite`**, a sonnet reviewer at medium effort for small diffs: patch tickets, delta re-reviews after a fix round, and haiku batches. Same evidence standard and verdict format; it returns `FLAGS: full review recommended` instead of stretching past its budget.
- **Reviewer budgets sized from the diff.** Every reviewer assignment carries a `BUDGET` line: 12 tool calls under 60 changed lines, 25 under 300, otherwise the old 40 (60 at opus). Reviewer prompts point at that line instead of a fixed number.
- **One test run per tree.** `test` records the worktree's tree id with a passing run and reuses it while the tree is unchanged (`PASS (cached …)`); `merge`/`land` reuse it when the squash is byte-identical to that tree (`checks PASS (cached …)`). `--fresh` forces a rerun on `test`, `merge` and `land`.
- **Batch land.** `land` with several tickets squashes and commits each, then runs the integration checks once on the combined result. On any failure it resets the branch and merges them one at a time, each tested, so the culprit still surfaces as `INTEGRATION FAIL`. `--each` forces one-at-a-time.
- **Auditor context slices.** Every validated table comes with `<table>.ctx.md`, one line per ticket it covers. `dispatch`, `review` and `validate-plan` write it and print a `CONTEXT` pointer when a plan deviates. Agents are told to hand auditors this file, never `sprint.json`.

- **Director quick fix.** `quickfix <id> begin | done --note … | abort` lets the Director make one bounded fix in a failed (review or integration) or passed ticket's worktree instead of resuming its builder. `begin` syncs with main and snapshots the builder's work; `done` refuses more than 20 changed lines or 2 files, runs the checks once, appends the note to the fix list and sets `built`; the change then goes to a delta review and never merges unreviewed. `abort` restores the builder's work. `retro` counts quick fixes.

### Changed
- `preflight` also warns when the brief's core is over 12 KB: a 150-line limit alone is met by ever-longer lines.
- `init` always adds `.claude/settings.local.json` to `.git/info/exclude`, whether or not you let Coniso write permissions. Claude Code stores "don't ask again" approvals there, command strings included.
- `MERGED` lines say how the integration checks were satisfied (`checks PASS`, `checks PASS (cached …)`, `checks: batch`).
- Haiku batch reviews and delta re-reviews are dispatched to `coniso:reviewer-lite`.

## [1.1.0] - 2026-10-31

A context-and-usage pass: fewer Director tool calls, fewer auditor round-trips that judge nothing, and less text in every long-lived context. Isolation is unchanged: one packet per agent, files carry the detail, 12-line returns.

### Added
- **Compound commands.** `dispatch` (worktree, complete assignment from the formats.md template, validated wave table, spawn lines), `review` (sync with main, reviewer assignments per review plan including panels, haiku batches and delta re-reviews, spawn lines), `verdict` (records a reviewer's verdict and applies the mechanical rules: any blocker or unmet item fails, minors never fail, missing evidence voids; prints `RULED` or `NEEDS RULING`) and `land` (merge in dependency order, then roadmap-sync once the wave is clear).
- **Token capture.** `set <id> --tokens N --role R` (state optional) records reported subagent tokens; `retro` totals them per ticket and per role. `review` and `land` take `--tokens` too.
- `validate-plan` prints `MATCHES SCHEDULE` when every row is the scheduled ticket-level plan with script-written assignments; leaders skip the delegation audit then. Lead packet plans never match.
- `validate-handoff <id>` replaces the lead's hand-off audit: evidence per acceptance item, an existing passing test log, no unlisted `[VERIFY]`/TODO added in the diff.
- `brief-trim` moves Decisions log rows older than the sprint to `.coniso/maps/decisions.md`; `preflight` warns when the brief's core exceeds 150 lines.
- `status <id> --field <name>`; `retro --lessons`; `retro` flags `docs/agent-feedback/*.md` files over 30 lines.
- Plugin hook: `SessionStart` (`compact`) prints the open sprint board and current wave; a no-op without an active sprint.
- Optional `Coniso Quiet` output style (`output-styles/quiet.md`, `keep-coding-instructions: true`, not forced).
- Test suite under `tests/` (standard-library `unittest`), including a two-ticket end-to-end sprint.
- `PRIVACY.md` privacy policy, linked from `plugin.json` (`privacyPolicyUrl`) and the README.

### Changed
- The Director, the Lead call, leads, managers and the Review Lead use the compound commands and chain bookkeeping with `&&`. Solo engineers record `built` themselves; reviewers run `verdict` themselves.
- Quiet-run rule: the Director writes nothing between tool calls except wave status, user decisions, gates, errors and the final report.
- The Director never reads project source; premises are checked with one scoped Grep or a one-line haiku scout. Scouts can answer a single question without a map.
- The Director reads only the last Lessons (`retro --lessons`), restarts after every 3 merged waves, rotates the Review Lead every 3 rounds and managers every 3 waves, and moves fix rounds to a fresh unit above ~100k reported tokens (was ~200k).
- Manager and Review Lead set `omitClaudeMd: true`; manager, Review Lead and lead set `experimental.cacheTtl: 1h`.
- `status --json` is compact.
- README: describe the merge gate's limits, the 12-line return as a rule rather than an enforced limit, the real escalation path, and relay mode in the requirements. Stop presenting unmeasured token savings as fact.

### Fixed
- The script no longer crashes printing `→` on Windows consoles that default to cp1252.

## [1.0.1] - 2026-10-30

### Changed
- **Permissions are now opt-in.** `init` no longer writes `.claude/settings.local.json`. It reports the allow rules (and any nesting env fix) it would add, and Claude asks you before anything is written.
- `init --accept-edits` moved to `permissions --accept-edits`, a separate opt-in. `init --no-claude-settings` is still accepted but no longer needed.

### Added
- `permissions` command: show status, `--write`, `--accept-edits`, `--decline` (stop asking), and `--remove`, which undoes exactly what Coniso added and keeps your own entries.
- README: a "Claude Code only" notice, a Permissions section, a list of everything Coniso runs and touches, and a note about not installing from both the marketplace and Anthropic's directory.

## [1.0.0] - 2026-10-30

First public release.

### Added
- **Role hierarchy.** Call a role and it brings its team:
  - **Engineer**: one worker.
  - **Lead**: one ticket in its own git worktree, solo or as a team.
  - **Manager**: one phase or track, wave by wave.
  - **Review Lead**: adversarial review rounds and sweeps.
  - **Director**: a session-long sprint off the roadmap.
- **Support agents.**
  - **Reviewers**: evidence-backed verdicts with anti-gaming checks.
  - **Auditors**: one tier cheaper, challenging leaders' judgment calls and rulings.
  - **Scouts**: read-only recon maps.
- **Context isolation by design.**
  - A file bus under `.coniso/` for briefs, assignments, ticket packets, reports, diffs and test logs.
  - Agents pass paths, never contents, and return at most 12 lines.
- **Deterministic control script** (`scripts/coniso.py`, standard library only). It handles:
  - scheduling, dependency waves under a slot budget, and sprint state;
  - worktrees, diffs and tests;
  - a squash merge gate with integration tests that resets cleanly on failure;
  - an auditor ledger, retro and close.
- **`validate-plan`**: script-checked delegation plans covering one owner per file per step, complete assignment files and slot limits.
- **Right-sized formations.**
  - S and M tickets: a solo engineer plus one reviewer.
  - L and xhigh tickets: a team.
  - A ticket that fails twice escalates one model tier at a time.
- **Relay mode.** When nested subagents are unavailable, the main session spawns on leaders' behalf, so sprints keep working.
- **HTML prompt roadmap.** A self-contained page with copy-ready prompts and Coniso commands, and script tools (`roadmap new|show|next|validate|extract|write|set|log|upgrade`) that validate every write and never overwrite browser edits.
- **Zero-flag setup.** `init` writes local allow rules to `.claude/settings.local.json`. It overrides environment settings only when they would break nesting.
- **Engineer specialties.** builder, tester, ui, net, level, audio, docs, batch, artisan and synth, plus forging new specialties and a lessons loop (`docs/agent-feedback/`).
- **Stack-pack mechanism** (`init --stack <name>`) for project-specific rules, specialties, checks, review lens and proving pass. No packs ship in 1.0.0.

[Unreleased]: https://github.com/Palatine-Labs/coniso/compare/v1.3.0-dev.3...HEAD
[1.3.0-dev.3]: https://github.com/Palatine-Labs/coniso/compare/v1.3.0-dev.2...v1.3.0-dev.3
[1.3.0-dev.2]: https://github.com/Palatine-Labs/coniso/compare/v1.3.0-dev.1...v1.3.0-dev.2
[1.3.0-dev.1]: https://github.com/Palatine-Labs/coniso/compare/v1.2.0...v1.3.0-dev.1
[1.2.0]: https://github.com/Palatine-Labs/coniso/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Palatine-Labs/coniso/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/Palatine-Labs/coniso/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Palatine-Labs/coniso/releases/tag/v1.0.0
