---
name: coniso
description: Hierarchical agent team for programming work, plus the HTML prompt roadmap it builds from. Call a role and it brings its team — engineer (one task), lead (one ticket in its own worktree), manager (a phase), review lead (adversarial review), director (a session-long sprint off the roadmap). Use when the user names a Coniso role or says "coniso", "run a sprint", "director, continue"; and for the roadmap — creating one, "what's next", ticket ids like P2-04, phase gates, pasted "Copy status for Claude" text, or revising tickets after a decision.
---

# Coniso

The main session is always the top of the hierarchy: the Director for a full sprint, and a lighter Director for smaller calls. It talks to the user, owns `.coniso/`, merges, and syncs the roadmap. It never does line work, never opens diffs, and reads only 12-line returns and script output.

## Roadmap (no Coniso setup needed)
For anything about the roadmap itself (create, status sync, what's next, expand a phase, revise tickets), read `.coniso/ref/roadmap.md`, or `${CLAUDE_PLUGIN_ROOT}/skills/coniso/references/roadmap.md` before init, and follow it. Roadmap work never needs init, preflight or a sprint. Building tickets does: that's the calls below.

## Roles

| Role | Agent | Model · effort | Directs | Called alone brings |
|---|---|---|---|---|
| Director | main session | the session's model | managers, the review lead, or leads and engineers directly | the whole team — a session-long sprint |
| Manager | `coniso:manager` | opus · high | teams and solo engineers in one track | a track — one phase or workstream |
| Review Lead | `coniso:review-lead` | opus · medium | reviewers | the review team — verdicts on built tickets, or a sweep over merged work |
| Lead | `coniso:lead` | sonnet · high (opus on opus tickets) | 1–4 engineers | one ticket in its own worktree — a team for L or xhigh tickets, otherwise a solo engineer |
| Engineer | `coniso:engineer` | sonnet · medium (haiku mechanical, opus hard) | — | itself |

Support roles, spawned by leaders:
- **Auditor** (`coniso:auditor`) audits the judgment calls in every leader's delegation plan, and every review ruling and hand-off claim. It runs one tier cheaper than its leader: opus leader → sonnet auditor, sonnet leader → haiku auditor. The mechanical plan rules (D3 ownership, D5 assignment files, D6 slots) are checked by `CONISO validate-plan` instead, before the auditor sees a plan.
- **Reviewer** (`coniso:reviewer`) is the adversarial reviewer.
- **Scout** (`coniso:scout`) does read-only recon and writes maps.

Builders never judge their own work. Finished tickets go to reviewers, which the Review Lead dispatches, or the dispatching leader when no Review Lead serves.

## Setup (once per session, every call)

1. If `.coniso/config.json` is missing, or `.coniso/VERSION` differs from 1.0.1, initialise from the repo root:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coniso.py" init --roadmap <docs/roadmap/X.html> --test "<test cmd>" [--build "<cmd>"] [--setup "<cmd per new worktree>"] [--link node_modules] [--copy .env.local] [--stack <pack>|none]`
   - Use `python` or `py -3` if `python3` is missing.
   - **Permissions are opt-in.** Init never edits Claude settings. When it (or preflight) prints `PERMISSIONS NOT CONFIGURED`, ask the user once, in one message: list exactly what it would add to `.claude/settings.local.json` (local, git-excluded): the allow rules, and any env fix it names. On yes run `CONISO permissions --write`; on no run `CONISO permissions --decline` and carry on (agents may hit prompts). Never write or edit the settings file yourself.
   - **Unattended sprints:** only when the user asks for them, ask separately before running `CONISO permissions --accept-edits` (sets acceptEdits as the project's default mode). `CONISO permissions --remove` undoes everything Coniso added.
   - **Stacks.** `--stack <name>` installs a stack pack from the plugin's `stacks/` folder: rules added to the brief, extra specialties in `.coniso/specialties/`, an optional check command run after build and test, an optional review lens and proving pass. Coniso 1.0 ships no packs; the mechanism is there for your own (see README → Stack packs).
   - Init finds the roadmap itself when the repo has exactly one. Find the test command in the repo (CLAUDE.md, package.json, *.csproj). Ask the user only if it isn't there.
   - **No roadmap yet?** Fine for one-off lead or engineer calls (they become adhoc tickets). For a sprint, offer to create one first (`roadmap.md` → Creating a roadmap).
   - No git repo? Offer `git init` plus a first commit (worktrees need it), or init with `--no-worktrees` for non-code work.
2. From then on use the command init prints. It is also in the brief header, and is called `CONISO` below. Init copies the script and references into `.coniso/` so agents never read outside the project.
3. Run `CONISO preflight`.
   - **Dirty checkout:** ask the user to commit or stash. Never stash their work yourself.
   - **RELAY MODE reported:** spawning works as described under Spawn mechanics.
4. Brief: `.coniso/BRIEF.md` needs the objective, pinned versions, commands and codebase map filled in. Fill it from repo files. For a large unfamiliar codebase, send 1–3 scouts (haiku) to write `.coniso/maps/*.md` and point the brief at them. Keep the core brief under 150 lines.

## Calls

- **Director** ("run a sprint", "director, continue"): read `.coniso/ref/director.md` and run the sprint.
- **Manager** ("manager on P3"):
  1. Run `CONISO schedule --phase P3` (or `--tickets …`).
  2. Write the manager's assignment (`.coniso/ref/formats.md`). Note that no Review Lead serves, so the manager dispatches its own reviewers.
  3. Spawn one `coniso:manager`.
  4. Run the merge loop from director.md §6–8 on its returns, and continue it wave by wave with SendMessage.
- **Review Lead** ("review lead, review P2-01..P2-05", "review sweep"):
  - For built tickets: list them from `CONISO status` and spawn `coniso:review-lead` with an assignment.
  - For merged work: give it a git range in the assignment. Its findings come back as proposed tickets. Add them per `roadmap.md` (Operations → Findings): one ticket per finding, test-first prompt.
- **Lead** ("lead on P2-04", or a described feature):
  1. Create the ticket: `CONISO schedule --tickets P2-04`, or `CONISO adhoc <ID> --title "…" --size M --model sonnet --accept "…" --prompt-file <file>`.
  2. Run `CONISO dispatch <id> [--specialty <name>]` (chained to step 1 with `&&`) and spawn the `SPAWN` line it prints.
     - **team** (non-haiku tickets of size L, or with effort xhigh): a `coniso:lead` whose assignment says no Review Lead serves, so it dispatches its own reviewer.
     - **solo** (other S and M tickets, and every haiku ticket): a `coniso:engineer` in a fresh worktree. It records `built` itself. Then review it per director.md §5b: `CONISO review <id> --tokens <n>`, spawn the reviewer line, and act on the `RULED` / `NEEDS RULING` line its return carries.
  3. On a pass, run `CONISO land <id>`: it merges per director.md §6 and runs `roadmap-sync`.
- **Engineer** ("engineer, rename X"):
  - For a quick task that doesn't deserve a ticket: write `.coniso/assignments/<id>.md` with WORKDIR set to the main checkout and spawn `coniso:engineer`. Check its result yourself. No worktree, no merge.
  - Any roadmap ticket goes through the Lead call instead, so it gets a worktree and a reviewer. S and M tickets automatically become one solo engineer plus one reviewer.

## Spawn mechanics (all roles)

- Use `subagent_type: coniso:<role>` and pass `model` from the assignment.
- The prompt is one line with the absolute path: `Assignment: <repo root>/.coniso/assignments/<unit>.md`. Paste nothing else; everything lives in files. Assignments use absolute paths throughout.
- Put independent spawns in one message so they run concurrently.
- A session runs at most 20 subagents at once. The schedule's slot budget (build plus 4 review slots) keeps you under it.
- **Relay mode.** A leader without the Agent tool (nesting limit) returns `STATUS: relay` with a spawn file `.coniso/assignments/<unit>.spawn.json`.
  1. Read only that file and spawn exactly what it lists.
  2. When they finish, resume the leader: `SendMessage` to its agentId with "Relay done: <unit ids>. Reports in .coniso/reports/".
  3. Do not read the subordinates' reports yourself.

## Context discipline

- **Files carry the detail.** Brief, assignments, ticket packets, reports, diffs and test logs live in `.coniso/`. Agents pass paths, never contents.
- **Every agent returns at most 12 lines** (format in formats.md). Leaders open a report file only when FLAGS asks for a decision.
- **The script does the bookkeeping.** Scheduling, state, worktrees, assignment files, diffs, tests, merges, plan validation, the mechanical verdict rules and roadmap edits are deterministic commands (`dispatch`, `review`, `verdict` and `land` bundle the common sequences), so no agent reasons through them. Chain consecutive bookkeeping commands with `&&` in one Bash call.
- **Minimal reading.** Agents read the brief once, their own assignment, and only their own specialty file. Continued agents get "Brief updated: <line>", not a re-read.
- **Cheapest model that does the job well.** The playbook is in `.coniso/ref/playbook.md`. When cost has to drop, cut breadth before cutting review.

## Standing rules

- The user's explicit instructions override the playbook.
- Never push.
- Never set a roadmap ticket to done. Sync sets `review`; the user confirms.
- Merge only passed tickets. Use `--force` only on the user's say-so.
- Coniso is the only orchestration authority for code work. Format skills (docx, xlsx, pdf, pptx) run inside the worker that produces that file.
- **Every delegation plan with more than one unit passes `CONISO validate-plan` before the auditor sees it or anything is spawned.** A single-unit dispatch (one manager, lead, engineer or reviewer) has nothing for D3 or D6 to check, so it skips the table; its assignment file still carries every formats.md field.
- **Lessons.** When the user critiques an agent's work, append a one-line imperative lesson to `docs/agent-feedback/<specialty>.md` and commit it with the related fix. Engineers read that file before working.
- **Specialties** live in `.coniso/ref/specialties/`: builder, artisan, batch, tester, ui, net, level, audio, docs, synth. A stack pack or the team can add more in `.coniso/specialties/`, which take precedence.
  - **Forge** a new one when three or more packets share a need. Copy `_template.md` to `.coniso/specialties/<name>.md`; engineers check there first.
  - **Promote** a proven one by adding it to the plugin's references in a new plugin version.
