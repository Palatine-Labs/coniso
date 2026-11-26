# Research: rust (cheapest source first)
Research answers one question for the Director or a manager. It never edits the project.

**1. Tools, no agent (seconds, no tokens)**
- What we depend on, and whether it is healthy: `RK deps`.
- A crate's latest version, licence, features and repository: `cargo info <crate>`.
- Who pulls a crate in, and with which features: `cargo tree -i <crate> -e features`.
- What the code does today: one scoped Grep.

**2. One scout (haiku), only if step 1 left the question open**
Dispatch `coniso:scout` with this assignment and nothing else:
```
QUESTION: <one sentence that ends in a decision: "A or B for X?", "is pattern P still the standard for Y?">
PINNED: <rust version, edition, and the crates already chosen, from the brief>
SOURCES, in this order, stop when answered: the crate's docs.rs page and changelog · the Rust API Guidelines ·
  the Rust reference or std docs · the RustSec advisory database · the crate's repository issues
BUDGET: 8 tool calls. No blog posts older than two years. No code samples in the map.
MAP: .coniso/maps/research-<topic>.md, at most 30 lines:
  Researched: <date> · <versions>
  ANSWER: <one line>
  STANDARD PRACTICE: <1-3 lines, each with a URL>
  OPTIONS: <a table: option · fits pinned versions? · maintenance · cost to adopt>
  IMPROVEMENTS WE COULD MAKE: <0-3 lines, each naming the file it would change>
  HAZARDS / UNKNOWNS
```
At most three scouts at once, one question each. Two scouts never share a question.

**3. A sonnet scout** only when a haiku map comes back `partial` on a question that blocks a ticket.

The Director reads the ANSWER line and the HAZARDS, not the whole map. Engineers get the map path in READ FIRST.
