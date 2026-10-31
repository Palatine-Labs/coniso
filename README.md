# Coniso

**Coniso: context-isolated agent teams for Claude Code**

by **Palatine Labs** · pronounced *CON-ih-so* (short for **con**text **iso**lation)

> Call a role and it brings its team. Every agent works in a clean, small context, and a deterministic script handles the bookkeeping so no model has to.

**Status: v1.1.** The core is stable and in daily use. Expect sharp edges on uncommon setups. Please report them as [issues](https://github.com/Palatine-Labs/coniso/issues).

**Claude Code only.** Coniso runs a local Python script, git worktrees and nested subagents, so it needs Claude Code: the terminal, an IDE extension, or the desktop app's Code tab. In claude.ai chat it installs but has nothing to run on. Cowork is untested.

---

## The problem

Long Claude Code sessions on large codebases degrade in two predictable ways:

- **Context bleed.** One session accumulates file dumps, diffs, test logs and dead ends from every task it has touched. Later work is reasoned against that noise, instructions from early in the session get diluted, and quality drifts.
- **Token waste.** The same session re-reads all of that history on every turn. Ad hoc subagents help, but they usually hand raw output back up the chain, so the main context fills up anyway, just more slowly.

Coniso fixes this structurally rather than with prompting:

- **Every agent gets one packet in a fresh context** and returns at most **12 lines**. Detail (briefs, assignments, diffs, test logs, reports) lives in files under `.coniso/`, and agents pass *paths*, never contents. Raw output never climbs the hierarchy.
- **A plain Python script, not an LLM, owns the bookkeeping:** scheduling, dependency waves, sprint state, git worktrees, diffs, merges, plan validation and roadmap edits. No tokens are spent on graph math or rule checks a script can do.
- **The main session stays small** for hours, and a sprint survives a restart ("director, continue").

## Roles

The main session is always the top of the hierarchy. You call a role, and it brings the team it needs:

| You call | Agent · default model | It brings | Use it for |
|---|---|---|---|
| **Engineer** (`/coniso:engineer`) | `coniso:engineer` · sonnet (haiku for mechanical, opus for hard) | itself: one worker, no worktree | a quick, well-defined task |
| **Lead** (`/coniso:lead`) | `coniso:lead` · sonnet (opus on opus tickets) | one ticket in its own git worktree: a solo engineer plus one reviewer for S/M tickets, a lead with 3–4 engineers for L tickets | one roadmap ticket or one described feature |
| **Manager** (`/coniso:manager`) | `coniso:manager` · opus | a track of teams and solo engineers, one dependency wave at a time | a roadmap phase or workstream |
| **Review Lead** (`/coniso:review`) | `coniso:review-lead` · opus | adversarial reviewers | verdicts on built tickets, or a sweep for defects over merged work |
| **Director** (`/coniso:director`) | the main session | everything above | a long session working through a block of your roadmap |

Support roles, spawned by leaders, never by you:

- **Reviewer** (`coniso:reviewer`): adversarial review against the acceptance criteria. Every finding needs evidence. It also hunts for gamed tests: deleted or loosened tests, hard-coded expected values, TODOs claimed as done. **Builders never judge their own work.**
- **Auditor** (`coniso:auditor`): a model one tier cheaper than the leader it audits. It challenges judgment calls (model tier, packet size, ordering, scope) and review rulings. Every overrule is logged, so the retro shows whose routing is going wrong.
- **Scout** (`coniso:scout`): read-only recon of a codebase area or docs question, written to a short map file.

**Specialties** (builder, tester, ui, net, level, audio, docs, batch, artisan, synth) are scoped guidance injected into a generalist engineer. They aren't personas with lanes to get stuck in.

## Install

Inside Claude Code:

```
/plugin marketplace add Palatine-Labs/coniso
/plugin install coniso@coniso
```

Or from your shell:

```bash
claude plugin marketplace add Palatine-Labs/coniso
```

```bash
claude plugin install coniso@coniso
```

Install it from one source only. A copy added from Anthropic's directory on claude.ai appears in Claude Code as `coniso@synced`; if you also install `coniso@coniso`, disable one of them.

### Requirements

- **Claude Code** with plugin support and nested subagents. Developed and tested on **v2.1.292**. Older 2.1.x releases may work but aren't tested.
- **Python 3.8+** on `PATH` as `python3`, `python` or `py -3`. Standard library only.
- **git**: each ticket builds in its own worktree. For non-code work, init with `--no-worktrees`.
- Windows, macOS and Linux.

### Experimental flags

**None required.** Coniso runs on Claude Code's defaults. Two existing values would break nesting, and Coniso offers to fix them (see [Permissions](#permissions)):

- `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` below 3: fix sets it to 3.
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`: fix sets it to 0. Coniso uses its own hierarchy, not experimental agent teams.

If nested spawning isn't available at all, leaders switch to **relay mode** on their own: they write spawn requests and the main session spawns for them. Sprints keep working; only parallelism drops. `preflight` (run on every call) reports which mode you're in.

## Quickstart

1. Open Claude Code in a git repo with at least one commit.
2. Make a roadmap (optional for one-off calls, needed for sprints):
   ```
   /coniso:roadmap new My Project
   ```
   Claude settles the goal, stack and phases with you, then writes `docs/roadmap/<Project>-Roadmap.html`.
3. Call a role:
   ```
   /coniso:lead P1-01
   ```
   or describe a feature: `/coniso:lead add CSV export to the reports page`.
4. For a long session:
   ```
   /coniso:director P1
   ```
   The Director schedules ready tickets into waves, builds each in its own worktree, reviews them, and merges the ones that pass. It stops at phase gates for your go/no-go.
5. Check progress any time with `/coniso:status`. To resume in a fresh session, say "director, continue".

The first call runs `init`, which:
- installs `.coniso/` and git-excludes it;
- finds your roadmap and your build and test commands;
- reports which permission rules would stop agents stalling on prompts. Claude then asks whether to add them (below).

## Permissions

**Coniso never changes your Claude Code settings without your yes.** On the first call, Claude shows you exactly what it would add to `.claude/settings.local.json` and asks:

- **allow rules** for the Coniso script and your build and test commands, so agents don't stop on every prompt;
- **an env fix**, only if one of your current values breaks nested subagents (see above).

Say yes and it runs `permissions --write`. Say no and it records that and stops asking; Coniso still works, but agents may pause for approval. That file is local to you and git-excluded.

You can change your mind at any time, by asking Claude or running the script yourself:

| Command | What it does |
|---|---|
| `python .coniso/coniso.py permissions` | Show what's allowed and what's missing |
| `python .coniso/coniso.py permissions --write` | Add the allow rules (and any needed env fix) |
| `python .coniso/coniso.py permissions --accept-edits` | Also make accept-edits the project's default mode, for unattended sprints. Always a separate opt-in |
| `python .coniso/coniso.py permissions --decline` | Stop asking |
| `python .coniso/coniso.py permissions --remove` | Remove everything Coniso added |

Natural language works too ("call a lead on P2-04", "run a sprint on phase 2").

## The HTML prompt roadmap

The roadmap is a single self-contained HTML file per project. It needs no server and no internet, and you open it in any browser. It holds phases, small tickets, a copy-ready prompt per ticket, and a recommended model and effort per ticket.

- **On the page:** set statuses, tick done-when items and write notes. These stay in your browser.
  - **Copy prompt** gives a fresh-session prompt with the project preamble included.
  - **Copy Coniso command** gives `/coniso:lead <id>`, or `/coniso:director <phase>` from a phase header.
  - **Save updated file** downloads the roadmap with your changes.
  - **Copy status for Claude** gives a summary to paste into a session.
- **Keep it current:** `/coniso:roadmap next` shows what's next. Pasting status, or asking to "expand phase 2" or "revise tickets after we switched to Postgres", updates the file through the script. The script validates every write, merges browser and Claude edits without clobbering, and refuses to delete done tickets.
- **How sprints use it:** the Director builds from it. After each wave, `roadmap-sync` moves merged tickets to `review` with `Claude:` notes and bumps the revision. **Tickets are never marked `done` for you.** Clicking through and marking done stays yours.

Roadmap work needs no Coniso setup and works in non-git folders too.

## Token usage and cost

Being straight about this:

- **Coniso does not use fewer total tokens than doing the same work yourself in one session.** A ticket costs one builder plus at least one reviewer, and leaders add auditors. Each agent reads the brief and its own assignment. That overhead is the price of independent review and isolated contexts.
- **What it does is keep every context small and bounded.** The main session doesn't grow with the work, so it doesn't pay to re-read hours of history on every turn, and quality doesn't decay late in a long session. For long sessions on large codebases this is usually a net saving. For a ten-minute fix it isn't. Just ask Claude directly.
- **It spends where it matters:** haiku for mechanical packets, sonnet as the workhorse, opus only for load-bearing work and leadership. A packet that fails climbs one tier at a time. S and M tickets run as one engineer plus one reviewer, with no team overhead.
- **Parallelism multiplies spend rate.** A Director sprint can run up to ~20 subagents at once. That finishes sooner but burns your plan's usage or API budget faster.
- **Levers:**
  - `/coniso:director … light` halves concurrent builds and uses single reviewers.
  - Prefer `/coniso:lead` for single tickets over a full sprint.
  - When cost has to drop, Coniso cuts breadth (fewer concurrent units) before it ever cuts review.
- **The main session stays lean by construction (v1.1):**
  - **Compound commands.** `dispatch`, `review`, `verdict` and `land` turn the Director's per-ticket bookkeeping (worktree, assignment file, plan validation, review setup, verdict rules, merge, roadmap sync) into a handful of script calls, chained with `&&`. A solo ticket now costs the Director about 5 tool calls instead of about 13.
  - **Mechanical rules go to the script, not to auditors.** Reviewers record their own verdicts with `verdict`, which applies the ruling rules and asks a leader only when judgment is needed. Plans that are exactly the scheduled plan (`MATCHES SCHEDULE`) and lead hand-offs (`validate-handoff`) are checked without an auditor. Auditors still review every judgment call.
  - **Quiet run.** The Director writes nothing between tool calls. An optional output style makes that session-wide (below).
  - **Measured, not guessed.** Each agent's reported token use is recorded per ticket (`--tokens`), and `retro` totals it per ticket and per role.
  - **Coordinators rotate.** A fresh Review Lead every 3 review rounds, a fresh manager every 3 waves, and a restart point for the Director every 3 merged waves keep long-lived contexts short. All state is on disk.
- **Idle cost is small:** about 600 tokens per session while installed, whether or not you use it. The compaction hook prints nothing outside an active sprint.
- **Rule of thumb:** use Coniso on a usage-based plan with headroom or an API budget you've set limits on. Watch your first sprint's usage before running unattended (`permissions --accept-edits`).

## Hooks and output style

- **Compaction hook.** Coniso ships one plugin hook (`hooks/hooks.json`): on `SessionStart` with matcher `compact`, it prints the open sprint board and the current wave, so a Director whose context was just compacted picks up from files instead of memory. In a project without an active sprint (`.coniso/sprint.json`) it prints nothing and exits 0. It runs `python3`, `python` or `py -3`, whichever works, through Claude Code's hook shell (Git Bash on Windows).
- **Quiet output style (optional).** `Coniso Quiet` (`output-styles/quiet.md`) tells Claude to write nothing between tool calls and speak only for status lines, decisions you own, errors and final reports. It keeps Claude Code's coding instructions and is never forced on. Pick it in `/config` → Output style for long sprints.

## Commands

| Command | What it does |
|---|---|
| `/coniso:director [phase\|ids\|continue] [light\|max]` | Full sprint |
| `/coniso:manager <phase\|ids>` | One track |
| `/coniso:lead <ticket id \| feature description>` | One ticket |
| `/coniso:review <ids \| A..B range>` | Review round or sweep |
| `/coniso:engineer <task>` | One worker, no worktree |
| `/coniso:status` | Sprint board |
| `/coniso:roadmap [new <project> \| next \| change]` | Create or update the roadmap, or show what's next |

**Always-on footprint: about 600 tokens per session** (measured with `claude plugin details coniso`): one short skill description, one-line agent descriptions, and role commands that Claude can't auto-invoke. Each role's full procedure loads only when you call it.

## Script reference (`.coniso/coniso.py`)

| Command | Purpose |
|---|---|
| `init [--stack <pack>\|none]`, `preflight` | Install or refresh; check git state, roadmap validity, nesting mode and permissions |
| `permissions [--write [--accept-edits] \| --decline \| --remove]` | Show, grant, decline or undo Coniso's entries in `.claude/settings.local.json` |
| `schedule [--phase P] [--tickets ids] [--limit N] [--add] [--formation managed\|direct]` | Plan a sprint |
| `validate-plan <table> [--slots N] [--no-files]` | Check a delegation plan: one owner per file per step, complete assignment files, slot limits; `MATCHES SCHEDULE` when it is exactly the scheduled plan (no audit needed) |
| `validate-handoff <id>` | Check a lead's hand-off: evidence per acceptance item, a passing test log, no unlisted `[VERIFY]`/TODO |
| `adhoc <id> --title … [--size] [--model] [--accept …] [--prompt-file]` | Add a non-roadmap ticket |
| `status [id] [--json] [--open] [--field name]`, `set <id> [<state>] [--note] [--verdict] [--model] [--tokens N --role R]` | Sprint state; `--tokens` records an agent's reported token use for `retro` |
| `wt add\|path\|remove <id>` | Worktrees on branch `coniso/<id>`, based on your current HEAD |
| `diff <id>` / `diff --range A..B`, `test <id\|main>` | Diff and test output go to files; the console gets a summary |
| `sync <id> [--continue]` | Bring main into a ticket's worktree to resolve conflicts |
| `merge <id>` | Squash, integration test, commit; resets cleanly on conflict or failure |
| `dispatch <ids> [--specialty …] [--review lead\|self] [--from U] [--plan name] [--rewrite]` | Worktree, complete assignment file and spawn line per ticket; a validated wave table when several |
| `review <ids> [--panel [ids]] [--delta] [--model opus] [--plan name] [--tokens N]` | Sync with main, reviewer assignments per review plan (batches and delta re-reviews included), set reviewing, spawn lines |
| `verdict <id[,id]> --report <file>` | Record a reviewer verdict; prints `RULED pass\|fail` when the rules settle it, `NEEDS RULING: <reason>` when a leader must |
| `land <ids> [--tokens N]` | Merge passed tickets in dependency order; roadmap-sync once their wave is clear; flags a restart point every 3 waves |
| `ledger add\|stats` | Auditor objection log |
| `brief-trim [--before YYYY-MM-DD]` | Move Decisions log rows older than the current sprint out of the brief, into `.coniso/maps/decisions.md` |
| `roadmap-sync`, `retro [--lessons]`, `close` | Write back, report (tokens per ticket and role, oversized feedback files; `--lessons` prints only the last Lessons), archive |
| `roadmap new\|show\|next\|validate\|extract\|write\|set\|log\|upgrade` | Roadmap file tools (work without `.coniso/`) |

## Stack packs

`init --stack <name>` installs a pack from the plugin's `stacks/<name>/` folder. A pack can provide:
- `rules.md`, added to the brief;
- `specialties/*.md`, extra engineer specialties;
- `stack.json`, with optional `check_cmd`, `setup_cmd`, `build_cmd` and `review_lens`;
- `proving.md`, a pass the Director runs after each wave.

`{python}` and `{stack}` in commands expand to the interpreter and the installed pack folder. **Coniso ships no packs yet.** The mechanism is there for your own. Contributions are welcome.

## Guarantees and notes

- **Nothing is ever pushed.** Coniso never marks roadmap tickets done and never stashes or touches your uncommitted work.
- **What it runs and touches:** your build and test commands, git (worktrees, branches, squash merges on your current branch), and its own files under `.coniso/` and `.git/info/exclude`. With your yes, it edits `.claude/settings.local.json`. Agents' own web lookups (scouts) go through Claude Code's tools. Coniso sends nothing anywhere itself.
- **The merge gate can't be talked past.** Each merge squashes the ticket, runs build and tests on the integrated result, and resets main untouched if anything fails or conflicts.
- Worktrees live in `.coniso/wt/`. If a tool in your repo scans dot-folders (some eslint or jest setups), add `.coniso` to its ignore list.
- Leave the main checkout alone during a sprint: merges need it clean.
- **Lessons loop:** when you critique an agent's work, a one-line lesson goes to `docs/agent-feedback/<specialty>.md`, and engineers read it before working.

## Contributing and security

- Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).
- Vulnerabilities: report privately per [SECURITY.md](SECURITY.md). Please don't use public issues.
- Privacy: Coniso collects and sends nothing. See [PRIVACY.md](PRIVACY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Coniso is an independent project by Palatine Labs and is not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, PBC.
