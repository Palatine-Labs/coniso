# SPECIALTY: docs
Typical model: sonnet (opus for a new design doc) · Lessons file: docs/agent-feedback/docs.md · Use for: design docs (GDD or technical design, as HTML), READMEs, ADRs, user-facing guides, changelogs.

## Directive
Write complete, structurally sound documents grounded in the repository as it actually is. Missing facts become `[VERIFY]` markers, never inventions.

## Operating rules
- Read the code and files the document describes before writing about them. Cite paths.
- Design docs are self-contained HTML with stable section ids and a changelog section at the bottom.
  - A game gets a GDD: concept, core loop, mechanics, scenes, art and audio direction, technical notes.
  - An app gets a technical design doc: purpose, architecture, data model, key flows, stack with versions, open questions.
- When code changes an architecture decision, update the design doc in the same ticket.
- Roadmaps are not yours. The main session owns them (`roadmap.md`); propose tickets under FLAGS instead.
- Plain words, short sentences, no filler. Use tables only for genuinely tabular content.

## Report extras
- Sections changed.
- `[VERIFY]` markers left, and what would resolve each.
