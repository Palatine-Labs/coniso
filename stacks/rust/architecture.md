# Architecture for agent-built Rust that keeps growing
These conditions exist because of how agents work, not for taste. An agent sees one packet and a few files; many agents build at once; every check run costs minutes. A codebase shaped for that stays fast at 500,000 lines. `RK` is `<python> .coniso/stack/rustkit.py`.

## The conditions
1. **One workspace, many small crates, in layers.** A crate uses only crates in lower layers. `rust-arch.json` lists the layers and `RK arch` (part of the gate) blocks an upward edge and a crate nobody placed. Why: crates compile in parallel, a ticket's tests run only its crate and its users, and two agents in two crates cannot collide.
2. **A crate is the unit of a ticket.** A ticket names the crates it may change (`CRATES: a, b` in its prompt); the gate blocks a change outside them and `RK wave` shows two tickets of a wave that share one. A ticket that needs three crates is three tickets, or a lead with one engineer per crate. A change to the public API of a crate in `interface_crates` gets an `api` hint and is its own ticket, merged before the tickets that use it.
3. **Interfaces sit below their implementations.** Traits and plain data types live in a small low crate; backends (a renderer, a physics binding, a platform layer) implement them higher up and are chosen at the top. Agents building against the interface never wait on, or read, the backend.
4. **Files stay small enough to read whole.** `max_file_lines` (1,000 by default). A file already over the limit may shrink but not grow: new code goes in a new module. Why: an agent that reads half a file writes code that fights the other half.
5. **`unsafe` and FFI live in named crates.** `unsafe_crates` lists them; anywhere else the gate blocks it. Each block carries `// SAFETY:`. Reviewers at opus for those crates only.
6. **Hot crates say so.** `hot_crates` lists the crates on the per-frame or per-request path. Added lines there that allocate or copy get a `hot:` hint for the reviewer. A hot crate keeps a benchmark; `RK bench -p <crate> --save` records a baseline and `RK bench -p <crate>` fails a run over 10% slower. Timings on a busy machine mislead, so confirm a failure with `CONISO quiet --runs 5 -- cargo bench -p <crate>`.
7. **Determinism is tested, not hoped for.** Where output must repeat (a simulation step, a build, a serialised format): no wall clock, no unseeded random, no iteration over a `HashMap` in that path, and one check that runs it twice and compares (`RK twice [--file <output>] -- <command>`).
8. **Public surface is deliberate.** `pub(crate)` by default; no glob re-exports; a crate's `lib.rs` is its table of contents. A crate map under `.coniso/maps/` (one line per crate: owns, uses, must not use) is what scouts and leads read instead of the code.
9. **Dependencies are a decision.** Third-party crates are declared once in `[workspace.dependencies]`; adding one needs a research map or a Decisions row. `RK deps` watches duplicates and advisories.

## Starting on an existing codebase
`RK arch --init` writes `rust-arch.json` from the code as it is: layers from the dependency graph, the crates that already hold `unsafe`, and every file already over the limit with its size. Nothing fails on day one; from then on the shape can only get better. Review the file, name the hot crates, commit it.

## When to split a crate
`RK arch` notes a crate over `max_crate_lines` (15,000). Split when two tickets in a wave keep landing in the same crate, or when its tests take over a minute. Split along a trait, put the trait in the lower half, and do it as one ticket with no behaviour change.
