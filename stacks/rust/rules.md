## Rust rules (stack pack: rust)
Checks, cheapest first: `rustkit gate` (diff rules and format), clippy with warnings as errors, then `rustkit test`. `RK` below is `<python> .coniso/stack/rustkit.py`.

**Building**
- Errors are values. Library code returns `Result`; `unwrap`, `expect` and `panic!` need a reason on the same line or a test around them. No `todo!`, `unimplemented!` or `dbg!` in a hand-off: the gate blocks them.
- Every `unsafe` has a `// SAFETY:` comment directly above saying why it is sound. No new `unsafe` without the ticket asking for it.
- Never silence a lint to pass: fix the code. A needed `#[allow(...)]` goes on the smallest item, with the reason beside it.
- Public items get a doc comment, with one example when the use is not obvious. Match the crate's existing error type, module layout and naming before adding your own.
- Async: nothing blocking inside a task (file, lock, sleep, heavy compute): use the runtime's version or `spawn_blocking`. Never hold a std lock across an `.await`.
- New dependency: only when the ticket names it or a map under `.coniso/maps/` recommends it. Add it with the fewest features that work.

**Architecture** (when `rust-arch.json` exists; the why is in `.coniso/stack/architecture.md`)
- A crate uses only crates in lower layers. Need something from above: move the shared part down or take it as a trait. Never edit `rust-arch.json` to pass; a change to it is a Decisions row.
- Stay inside the crates your packet names on its `CRATES:` line; the gate blocks a change outside them. A file over the line limit does not grow: add a module.
- A changed or removed public item in an interface crate is its own ticket, merged before its users.
- `unsafe` only in the crates listed for it. In a hot crate, do not allocate or copy on the per-frame path.

**Testing**
- A ticket's `test` runs the crates it changed and the crates that depend on them; the merge runs the whole workspace. Do not widen a ticket's run by hand.
- One behaviour, one test. Before adding a test, search for one that already covers it and extend that. Cases that differ only in their values go in one table-driven test.
- No sleeps and no real clock, network or unseeded random in a test. Unit tests sit beside the code; `tests/` is for the public API.
- Never weaken, `#[ignore]` or delete a test to pass. Removing a redundant test is its own XS ticket, named in the audit report.

**Review lens `rust`** (the reviewer carrying it checks, in this order)
1. Every `RUST HINTS` line in the test log: accept with a reason, or raise a finding.
2. Error paths: each new `Result` has a test for a failure, and errors keep their cause.
3. Ownership and cost: a clone, allocation or lock inside a hot loop; a `Vec` where a slice would do; a public API that takes ownership it does not need.
4. Concurrency: shared state, lock order, and whether each new `.await` inside a `select!` or timeout is safe to cancel.
5. Tests: a new test that repeats an existing one is a minor finding; a test that asserts nothing is a major one.

**Housekeeping** (Director)
- Once after `init --stack rust`: `RK doctor`, then `RK doctor --apply` if its plan suits the machine.
- Roadmap tickets carry `CRATES: a, b` in their prompt. After `schedule`, run `RK wave`: where two tickets of a wave share a crate, make one depend on the other or give the crate to a lead who splits it by file.
- A ticket in a hot crate reports `RK bench -p <crate>` (after `CONISO quiet` when the machine is busy). Where output must repeat, its acceptance includes `RK twice -- <command>`.
- After each wave: the proving pass in `.coniso/stack/proving.md`.
- Before choosing a crate, an API or a pattern: `.coniso/stack/research.md`. Facts from tools first, one cheap scout second.
