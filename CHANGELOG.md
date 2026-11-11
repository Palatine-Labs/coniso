# Changelog

All notable changes to Coniso are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Palatine-Labs/coniso/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/Palatine-Labs/coniso/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Palatine-Labs/coniso/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/Palatine-Labs/coniso/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Palatine-Labs/coniso/releases/tag/v1.0.0
