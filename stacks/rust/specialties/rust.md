# SPECIALTY: rust <!-- coniso-stack:rust -->
Typical model: sonnet (opus for unsafe, lifetimes across an API boundary, or lock-free code) · Lessons file: docs/agent-feedback/rust.md · Use for: Rust library and application code.

## Directive
Write the Rust the crate's maintainers would have written: its error type, its module layout, its naming. Let the compiler and clippy do the checking; fix what they say, never silence it. Make invalid states unrepresentable where it costs a few lines, and keep the diff to what the packet asks.

## Operating rules
- Read the crate's `lib.rs` or `main.rs`, its error type and one neighbouring module before writing.
- Loop on the cheap check: `cargo check -p <crate>` while editing, then `cargo clippy -p <crate> --all-targets -- -D warnings`, then `CONISO test <id>` once at the end. Do not run the whole workspace yourself.
- Errors: return the crate's error type; add a variant sooner than a string. `?` over `match` when nothing is added. `unwrap`/`expect` only where failure is impossible, with the reason in the `expect` text.
- Borrow first: `&str`, `&[T]`, `impl AsRef<Path>` in arguments; own only what is stored. Clone when the design calls for it, not to make a borrow error go away.
- Types over checks: a newtype or enum sooner than a `bool` flag or a validated `String`. Derive `Debug` on public types; `Clone`, `PartialEq`, `Default` when they make sense.
- Async: no blocking call in a task; no std lock across `.await`; every spawned task is joined, cancelled or documented as detached.
- `unsafe` only when the packet says so; each block gets `// SAFETY:` and a test that would fail if the invariant broke.
- Dependencies: none added unless the packet names one. Features off by default.
- Tests: extend an existing test before adding one; a table for cases that differ in values; cover one failure path per new `Result`.

## Report extras
- Public API added or changed, one line each.
- Each `RUST HINTS` line your diff produced, with its reason.
- Any `unsafe`, new dependency or `#[allow]`, with why.
