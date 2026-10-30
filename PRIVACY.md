# Privacy Policy

_Last updated: 2026-10-30 · applies to Coniso 1.0.1 and later_

Coniso is a Claude Code plugin published by Palatine Labs. This policy describes the data Coniso handles.

## Summary

**Coniso collects nothing and sends nothing.** It has no servers, accounts, telemetry, analytics or tracking. Palatine Labs receives no data from your use of it.

## What Coniso runs

- `scripts/coniso.py`, a bundled Python script that uses the standard library only. It makes no network connections.
- `git`, for worktrees, branches, diffs and squash merges in your repository.
- The build, test and setup commands you configure for your project.

All of these run on your machine, through Claude Code's Bash tool and Claude Code's normal permission prompts.

## What Coniso stores, and where

Everything stays in your project on your machine:

| Location | Contents |
|---|---|
| `.coniso/` | Brief, tickets, assignments, reports, diffs, test logs, sprint state and archives. Git-excluded, so it's never committed. |
| `.git/info/exclude` | Entries that keep `.coniso/` (and `.claude/settings.local.json`, if you opt in) out of commits |
| Git worktrees and `coniso/<id>` branches | Created per ticket and removed after merge |
| Your roadmap HTML file, and `docs/agent-feedback/*.md` | Project files you can see and commit like any other |
| `.claude/settings.local.json` | **Only if you opt in** (`permissions --write`): allow rules for Coniso, your build and test commands, and nesting fixes. Undo with `permissions --remove`. |
| Your browser's local storage | The roadmap page's statuses and notes, on your machine only |

Coniso keeps this data until you delete it. `close` archives a finished sprint under `.coniso/archive/`, and deleting `.coniso/` removes all of it.

## Data processed by Claude

Coniso's agents are Claude Code subagents. Like any Claude Code session, what they read and write (your code, prompts, command output) is processed by Anthropic under your own agreement with Anthropic and [Anthropic's privacy policy](https://www.anthropic.com/legal/privacy). Coniso adds no destination of its own. Scout agents may use Claude Code's built-in web search and fetch tools to read documentation.

## Credentials

Coniso doesn't ask for, read or store credentials, tokens or API keys. It checks two Claude Code configuration values (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) only to tell you whether nested subagents will work.

## Children

Coniso is a developer tool and isn't directed at people under 18.

## Changes and contact

Changes to this policy are recorded in this file's history and noted in [CHANGELOG.md](CHANGELOG.md).

- Questions: open an [issue](https://github.com/Palatine-Labs/coniso/issues).
- Security concerns: report privately per [SECURITY.md](SECURITY.md).
