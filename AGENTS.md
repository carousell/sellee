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

The channel poller thread is the **one consumer** of all Telegram Bot API
traffic — only `channel/telegram/transport.py` talks to the network, and the
poller is its only caller of `get_updates`. That single-consumer property is
what makes "an unbound channel consumes nothing" a state of one thread rather
than a convention; keep it that way (a guard also pins that the channel *core*
imports no provider).

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

## Imports are absolute

Intra-package imports use the absolute `from selly_agent.x import y` style (not
relative `from .x import y`, and not bare `import selly_agent.x.y`), so moving a
module never forces rewriting its own imports. Relative imports fail `make lint`
(ruff TID252, `ban-relative-imports = "all"`).

## Types are checked, not just written

A static type checker runs via `make typecheck` (pyright, dev-only — it never
ships, the runtime stays stdlib-only). It is scoped to the annotated store surface;
widening it to the rest of the tree is a separate decision. The store's stable
record and ack returns are `TypedDict`s (a dict at runtime, a checked shape under
the checker) — new store returns of that shape should be too. Two conventions the
checker now enforces rather than documents:

- **Secret-free acks are value-free by construction.** `FloorAck`/`BudgetAck` have
  no floor/budget field, so an accidental value key is a `make typecheck` failure,
  not something only a test can catch.
- **The JSON boundary stays `dict`.** Tool `params` are validated by each
  `ToolSpec.input_schema` (the schema is the contract, not a second Python type),
  and discriminated-union returns whose key set depends on the decision
  (`negotiate_*`, the send bracket, the gate checks) stay a bare `dict`.

## Before finishing up

Run these, green:

```sh
make lint       # ruff check + ruff format --check
make typecheck  # pyright over the annotated store surface
make test       # and make test-3.9 if a 3.9 interpreter is available
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

## Three kinds of config, three homes

Keep these distinct — where a value lives is a decision about what it *is*:

- **Operator/install knobs** — `config.py` reading `config.json`. Validated at
  load, restart semantics, written only by the installer/tools, never the daemon.
  (e.g. `http_port`, pacing jitter/cap, `pacing_mode`.)
- **Seller domain records** — the `seller_config` table (basics, shipping zones,
  the origin address). Free-form JSON sections the flows consult; written
  attended-direct via `update_seller_config`.
- **Seller settings** — the `settings` table plus the `settings.py` code
  registry. Runtime behavior knobs the seller changes through a door. Defaults
  live in the registry, not as rows: an unset key reads as its default. Adding a
  setting is one `SettingSpec` (type/parse/render/default/description/approval
  policy) — the registry is the validation source, the card's discoverability
  source, and the LLM's vocabulary at once.

**Settings change only through a door — the LLM proposes, it never applies.**
`propose_setting_change` is the *only* settings-mutation tool on any tier; there
is deliberately no apply/approve/undo/cancel tool. The daemon decides by policy
(a registry `requires_approval` flag): high-stakes changes are held for a human
signal (an Approve/Cancel button, an exact `approve <id>` text token, or the
attended `selly-agent settings approve <id>` CLI over `/control/settings-decide`);
low-stakes ones apply immediately in deterministic store code. Every apply is one
`selly.db` transaction (setting upsert + ledger row + echo notice), and every
consumer reads its setting at its own decision point (read-at-use — no caching, no
reload). The no-apply-tool rule is enforced by a guard test, not just convention.

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
