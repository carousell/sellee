# Agent instructions — selly-agent

Conventions for anyone (human or agent) writing code in this repo. This
codebase must stand on its own; the design plans live in a separate projects
repo and are not present here.

## Runtime is stdlib-only

Nothing under `src/` may import a non-stdlib package — the user's own `python3`
is the only runtime dependency, and there is no pip install step on a user
machine. This is enforced mechanically: `tests/guard/test_stdlib_only.py`
walks every import under `src/` and fails on any third-party module (and on
network imports outside an explicit allowlist). Dev/test dependencies (pytest,
ruff) live in the `[dev]` extra and never appear under `src/`.

## Network access is allowlisted

Most of `src/` does no network I/O. A module that imports a network stdlib
package (`socket`, `ssl`, `urllib`, `http`, `asyncio`, …) must be added by its
src-relative path to `NETWORK_ALLOWLIST` in
`tests/guard/test_stdlib_only.py`, or the guard fails. Adding an entry is a
deliberate act: it grants that module the capability to open sockets, and a
reviewer should treat it as such. Everything else stays network-free.

## Python 3.9 floor

The runtime floor is Python 3.9 (macOS Command Line Tools). The suite must pass
on it — run `make test-3.9`. Syntax discipline:

- `from __future__ import annotations` at the top of every module.
- No `match` statements.
- No runtime `X | Y` unions. In annotations they are fine (postponed
  annotations make them strings); ruff is pinned to `py39` and will flag
  runtime uses.
- No `tomllib` (3.11+).

`ruff` with `target-version = "py39"` is a second mechanical enforcer of the
floor — it flags 3.10+ syntax at lint time.

## Before finishing up

Run both, green:

```sh
make lint     # ruff check + ruff format --check
make test     # and make test-3.9 if a 3.9 interpreter is available
```

Do **not** add GitHub Actions / CI workflows — CI is owner-managed to org
conventions. The Makefile targets are the seam CI will call.

## Tests are plain pytest

No homegrown test framework. Fixtures point `$XDG_*_HOME` at a tmpdir so tests
never touch a real install. Ported legacy tests are converted to plain pytest.

## Path authority

`paths.py` is the only module allowed to resolve the home directory or read an
`XDG_*` variable. Everything else routes through it. A guard test
(`tests/guard/test_path_authority.py`) enforces this — it is the structural
defense against writing to a location the running daemon never reads.

## State is two SQLite DBs

`data/selly.db` is business data (migrated, snapshotted before migrations).
`state/events.db` is the event/transcript store (prunable, deletable without
data loss, never backed up). Never open a cross-DB transaction — events are
observability, not ledger. All writes go through the single write connection
per DB; the LLM never writes state directly.

## Engines stay pure

Modules under `src/selly_agent/engines/` are pure decision layers. They must not
import tool or server modules, must not touch the store or the network, and must
not read the clock except through a `now` parameter. A tool composes an engine
with the store (one transaction per decision); the engine only decides. This is
what keeps the money/safety logic unit-testable in isolation and the network-free
guarantee structural (the stdlib-only guard would flag a stray `urllib`).

## Secrets never cross the tool-read boundary

The floor and the buyer max budget are confidential. They live in their own
tables, are loaded only inside the engines, and are never returned by any read an
LLM-facing tool can call — engine outputs are secret-free by construction (the
never-below-floor / never-above-budget asserts are the backstops), and error
text carries no value. A tool parameter that carries a secret (floor, budget,
origin address) is listed in its `ToolSpec.secret_params` so dispatch masks it
before any event sink. When you add a tool that takes such a value, mark it.

## Comments

The codebase must read on its own for someone who does not have the plans.

- **Never reference plan files or decision IDs in comments** (no "per A5", "see
  plan 08", "INV-27 requires…"). Those belong in commit messages. State the
  rule inline instead: not `# exit 0 (INV-27)` but
  `# a clean exit so the supervisor's keep-alive won't respawn a duplicate`.
- **Keep comments sparse and proportionate.** Most code needs none. Reserve
  real comment blocks for genuinely complex code or behavior whose *why*
  depends on context the code can't convey; then state that reasoning inline,
  self-contained.

## Version control

Commit in logical units, each building and passing tests on
its own; order commits so a reviewer never sees code that calls something
introduced later. Isolate generated/mechanical churn from hand-written diffs.
