# Proving pass: rust (after each wave; about four commands, no agents)
`RK` is `<python> .coniso/stack/rustkit.py`; `CONISO` is the usual command. Run from the main checkout.

1. `RK test-audit --log <the land's test log>`: redundant, empty, ignored, sleeping and slow tests.
   - Nothing listed: done.
   - Anything listed: one XS ticket for the next wave (`CONISO adhoc --size XS`, specialty `rust-test`), titled "Test cleanup: <n> items", with `.coniso/maps/rust-test-audit.md` as its packet. Its acceptance: the named tests are merged or removed, and no behaviour loses its only test. One cleanup ticket per wave at most; carry the rest over.
2. `CONISO sweep`: processes left running with no parent. If it lists any that no running agent owns, `CONISO sweep --stop`.
3. `RK trim`: clears the shared build cache when it is over 20 GB.
4. Every third wave, or when `Cargo.lock` changed: `RK deps`. A new advisory is an adhoc ticket; duplicate versions and unused dependencies go in the retro.

Report one line: `Proving: tests <n> (<k> to clean) · swept <n> · cache <x> GB · deps <ok | n advisories>`.
