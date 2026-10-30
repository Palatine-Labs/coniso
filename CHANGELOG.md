# Changelog

All notable changes to Coniso are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `PRIVACY.md` privacy policy, linked from `plugin.json` (`privacyPolicyUrl`) and the README.

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

[Unreleased]: https://github.com/Palatine-Labs/coniso/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/Palatine-Labs/coniso/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/Palatine-Labs/coniso/releases/tag/v1.0.0
