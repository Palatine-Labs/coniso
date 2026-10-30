# CONISO BRIEF — {title}
Updated: {date} · Owner: Director · Every agent reads this. Keep everything above "Track notes" under ~150 lines.

## Coniso script
Repo root: `{root}` — every `.coniso/…` path below is under it.
Run as: `{cmd} <command>` (absolute path; works from any worktree). Never call git yourself.

## Objective
<What exists when this sprint succeeds — set by the Director at schedule. One short paragraph.>

## Project rules (from the roadmap preamble — binding)
{preamble}

## Stack & pinned versions
- <engine / framework / language with exact versions, e.g. "Godot 4.4 .NET, C# 12", "Next.js 15 App Router, TypeScript 5.6">

## Commands
- build: `{build}` · test: `{test}` · new-worktree setup: `{setup}`

## Codebase map
<Entry points, key folders, the 2–3 architectural facts an agent needs to not be dangerous. Pointers to .coniso/maps/*.md, not pasted contents.>

## Glossary
| Term | Means exactly |
|------|---------------|

## Decisions log (append-only, stamped)
| Stamp | Decision | Binds |
|-------|----------|-------|

## Standing rules — every role
- Work only inside your WORKDIR. Use absolute paths; start every Bash call with `cd "<WORKDIR>" && `.
- Never run git. Coniso's script handles branches, diffs, syncs and merges.
- Never edit the roadmap file or this brief; report gaps under FLAGS.
- Write only the files your assignment says you own. Need another file? Flag it up.
- Mark any API or claim you could not verify with `[VERIFY]` in a comment. Never guess silently.
- If `docs/agent-feedback/<specialty>.md` exists in the repo, read it first; its lessons bind you.
- Return the standard 12-line report; put detail in your report file.

## Track notes
<Optional: context only one track needs, appended by managers. Keeps the core brief lean.>
