# Roadmap — the HTML prompt roadmap

Each project is tracked in one self-contained HTML file: phases, small tickets, a copy-ready prompt per ticket, and a recommended model and effort per ticket. The page renders everything from a JSON block inside the file. It is the record that carries work from session to session, and Coniso builds from it.

`CONISO` below means `python3 <plugin or .coniso>/coniso.py`. The `roadmap` commands work in any folder, with or without `.coniso/`. Use `python` or `py -3` if `python3` is missing. Never hand-edit the JSON and never rebuild a roadmap from memory: read it with the script, write it with the script.

## Use when
- The user mentions the roadmap, a ticket id (P0-03, E-07…), a phase gate, or asks "what's next".
- The user pastes the page's "Copy status for Claude" text.
- A decision changes scope and tickets need revising.
- A phase marked as an outline is about to start and needs real tickets.
- A new project needs a roadmap.

## Commands
| Command | Does |
|---|---|
| `CONISO roadmap new --title "…" [--product …] [--summary …] [--from seed.json] [--out docs/roadmap/X-Roadmap.html]` | Creates the file from the bundled page. Coniso adopts it if `.coniso/` has no roadmap yet. |
| `CONISO roadmap show` / `next` | Status counts, phases, ready tickets, review queue, open gates, and the Coniso command to run next. |
| `CONISO roadmap validate` | Checks the schema rules below. |
| `CONISO roadmap extract [--out f.json]` | Writes the JSON to edit. |
| `CONISO roadmap write --from f.json [--log "…"] [--commit]` | Validates and writes. It handles `updated` stamps (see File anatomy), bumps the revision, appends the log entry, and refuses to delete `done` tickets. |
| `CONISO roadmap set <id> --status <s> [--note "Claude: …"] [--check 1,3] [--log …]` | Quick single-ticket change. |
| `CONISO roadmap log --log "…"` | Appends a log entry. |
| `CONISO roadmap upgrade` | Moves an older file onto the current page. The data is unchanged and a `.bak` backup is kept. |

Pass `--file <path>` when the repo has several roadmaps and `.coniso/` isn't configured.

## File anatomy
- All data lives in `<script type="application/json" id="roadmap-data">…</script>`. The rest of the file is the page; don't change it unless the user asks for a page feature (then edit the plugin's template and `upgrade`).
- The page keeps the user's clicks (status, checkboxes, notes, log) in browser storage until they press **Save updated file**. Per ticket, the browser copy wins only when it's newer than the ticket's `updated` in the file.
  - When a ticket's status, notes or checks change, `updated` must be fresh, or a browser copy may override the edit. `roadmap write` and `roadmap set` do this automatically.
  - When only other fields change (model, effort, review, prompt, why, what, accept wording), `updated` must stay as it was, so the user's browser progress keeps merging over the file. `roadmap write` keeps it.

## Schema
```
meta: { id, title, product, owner, licensee, filename, revision, created, updated, summary, howto[], preamble,
        modelNote, effortNote, reviewPrompt }   // reviewPrompt uses {id} and {title}
models[]: { key: opus|sonnet|haiku, name, short, when }
efforts[]: { key: low|medium|high|xhigh, when }
phases[]: { id, name, short, weeks, why, gate, parallel?, outline?[] }   // outline = not yet expanded
tickets[]: { id, phase, title, size: S|M|L, deps[], why, what[], accept[], prompt,
             model, effort (null for haiku), review: bool, modelWhy,
             status: todo|progress|review|done|blocked, notes, checks[bool per accept], updated }
decisions[], open[], later[]      // strings, or {date, text, why}
log[]: { id, date: YYYY-MM-DD, text }
```
- Timestamps: ISO 8601 UTC with `Z`, e.g. `2026-10-04T18:20:00Z`.
- Sizes are the user's time including review and testing: S 1–2 h, M 2–5 h, L 5–10 h. Nothing larger; split it.
- Inline formatting in text fields: `code` and **bold** only.
- `models[]` lists the models the user can run. Ask once when creating a roadmap if unsure, and update `models[]` when their plan or the lineup changes; tickets reference the key. Coniso's tiers map onto opus, sonnet and haiku.
- Effort is Claude Code's `/effort`. Haiku has no effort setting (`effort: null`). Don't assign `max`: it's session-only, costs the most, and can overthink.

## Procedure
1. Find the file (`CONISO roadmap show`). If none exists and the user wants one, see "Creating a roadmap".
2. For anything beyond a one-ticket status change, `CONISO roadmap extract`, edit the JSON file with your file tools, then `CONISO roadmap write --from <file> --log "<what changed>"`.
3. For one ticket, use `CONISO roadmap set`.
4. Tell the user in one or two lines what changed (tickets added, statuses set, decisions recorded). If the repo is git-tracked and Coniso isn't running a sprint, pass `--commit`.

## Operations
- **Status sync.** Apply what the user reports or pastes from "Copy status for Claude". Never mark a ticket `done` that the user hasn't confirmed. Keep their notes; add yours as a separate line starting `Claude:`.
- **Log entry.** Gate go/no-go decisions always get one.
- **Expand an outline phase.** When the previous phase's gate has passed, replace the phase's `outline` with tickets. Remove `outline` once every item is covered or explicitly moved to `later`. End each phase with a gate ticket (title contains "Gate").
- **Revise after a decision.** Update `decisions`/`open`, then edit affected tickets. Don't delete done tickets: add a note and a follow-up ticket. Moving scope out means adding it to `later`, never silently dropping it.
- **Findings** (from a validation pass or a Review Lead sweep): one ticket per finding, with a test-first prompt.
- **Escalation.** If a ticket failed twice: haiku → sonnet at high; sonnet → opus at the same effort; opus → one effort level higher (xhigh at most). Update `modelWhy` with the reason and add a log entry.

## Writing tickets
- One ticket = one Claude coding session with a result the user can check by clicking through the app or running a command.
- `why`: one or two plain-language sentences about the payoff to the user or their users, not the implementation.
- `what`: 2–4 short bullets.
- `accept`: 2–4 checkable statements the user can observe ("an empty file shows the warning"), never "code is clean".
- `prompt`: only ticket-specific instructions. The page prepends `meta.preamble` and appends the done-when list automatically, and Coniso's ticket packets carry both, so never repeat either. Name the exact docs to read (CLAUDE.md is implied). State scope boundaries. Put any value the user must supply in a `[PASTE …]` placeholder.
- Aim for 80–250 words per prompt. Longer usually means the ticket should be split.
- Order tickets by dependency and list real `deps`, so "Up next" and Coniso's waves are right.
- Never write a ticket that breaks the project rules in `meta.preamble`.

## Choosing model, effort and review
Every ticket gets `model`, `effort`, `review` and a one-line `modelWhy` naming the deciding factor. Pick by what a wrong answer costs and how much is already decided, not by size.

Model:
- **opus**: real building, like new libraries, performance, interaction feel and data safety. Anything that sets a pattern later tickets copy (the first of a kind), phase gate reviews, and load-bearing tickets.
- **sonnet**: well-specified work that follows an existing pattern or a written spec, like panels, lists, forms, CRUD dialogs, and follow-on features implementing a spec.
- **haiku**: small mechanical changes with nothing to decide, like an extra export over existing data, renames or config tweaks.

Effort:
- **low**: a small change the user checks right away.
- **medium** (the default): clear-scope features the user can verify by clicking through, or where tests already catch mistakes. Don't raise effort by reflex.
- **high**: correctness matters or edge cases are likely, as in math, parsing, scale, data safety, performance-critical paths and gate reviews.
- **xhigh**: Opus only, for load-bearing designs that are expensive to change (data model, engine core, core algorithms), the hardest stateful logic, security-sensitive work, and validation where a quiet error reaches users. Keep it to roughly one ticket in seven.

Review: set `review: true` on xhigh tickets that produce code or a design others build on. The page then offers "Copy review prompt" for a fresh session, and Coniso gives these tickets a reviewer panel.

Tiebreaker: if a mistake would be caught by tests or by the user clicking through, go cheaper. If it would pass silently into numbers, data or user-facing output, go up a level.

## Creating a roadmap
1. Settle direction with the user first: the goal, the stack, the hard rules, and roughly the phases. Ask what's unclear, but don't interrogate.
2. Write a seed JSON. It needs `meta.preamble` and `phases[]`. Add tickets only for the first phase; later phases get `outline[]`.
   - The preamble states the stack, the hard rules and the finish routine: test, update docs, commit `<id>: <title>`, report manual checks.
   - Every ticket needs a model, effort, review flag and `modelWhy`.
   - Leave out `models[]`/`efforts[]` to keep the defaults.
3. `CONISO roadmap new --title "<Project>" --from <seed.json>`. It goes to `docs/roadmap/<Project>-Roadmap.html` unless `--out` says otherwise.
4. Tell the user to open the file in a browser. From there they can copy prompts, or run `/coniso:director P0` for the first phase.
