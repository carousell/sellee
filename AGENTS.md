# Agent instructions — selly-agent

Conventions for anyone (human or agent) writing code in this repo. This
codebase must stand on its own; the design plans live in a separate projects
repo and are not present here.

## Runtime is the stdlib plus a small, reviewed dependency set

The runtime is not the user's `python3`. `uv` provisions a standalone CPython at
the version `.python-version` pins, and installs dependencies from `uv.lock`
into a venv that belongs to the install. Nothing about the machine's own Python
matters — that is the point, and it is what removed the largest class of install
failure.

Code under `src/` may import the stdlib and the packages in
`ALLOWED_RUNTIME_DEPS` (`tests/guard/test_stdlib_only.py`), and nothing else.
Adding a runtime dependency means all three of:

1. an entry in `[project].dependencies` in `pyproject.toml`,
2. a relocked, committed `uv.lock`,
3. an entry in `ALLOWED_RUNTIME_DEPS`.

Do all three in one commit and say in the message why the dependency is worth
it. This is a supply-chain grant on a machine holding marketplace credentials
and a logged-in browser, so it is a review decision, not a convenience. The
guard fails any import that skipped the path, and also fails an allowlisted name
that is not actually installed.

Dev tooling (pytest, ruff, pyright, the MCP SDK) lives in the `dev`
**dependency group** and never appears under `src/`. `uv sync` includes it;
`uv sync --no-dev` is what a user's install runs.

Our own package is deliberately *not* installed into the venv
(`tool.uv.package = false`): the launcher and pytest put `src/` on the path
themselves. An editable install would build this package on a user's machine
during setup and leave a path pinned into a version directory that `update`
prunes.

`make bootstrap` sets all of this up locally; it calls the same `./setup` path a
user does.

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

## The interpreter is pinned; the syntax floor deliberately lags it

The runtime is the CPython version in `.python-version` (3.14), provisioned by
uv. There is no older interpreter to stay compatible with.

The **syntax** floor has not moved yet, though: `ruff` is still pinned to
`target-version = "py39"`, and the tree is still written that way —
`from __future__ import annotations` everywhere, no `match`, no runtime `X | Y`
unions, no `tomllib`. That is on purpose. Modernizing is a mechanical sweep worth
doing as one reviewable diff rather than drifting in a file at a time, so until
that sweep happens, keep writing to the existing style and let ruff hold the
line.

Two things follow from the pin that you *can* rely on: the interpreter is always
a final release (setup asks it directly, so a pre-release can never be what got
provisioned), and dev tooling no longer needs version markers — the MCP
conformance suite runs everywhere rather than skipping under an old floor.

## Imports are absolute

Intra-package imports use the absolute `from selly_agent.x import y` style (not
relative `from .x import y`, and not bare `import selly_agent.x.y`), so moving a
module never forces rewriting its own imports. Relative imports fail `make lint`
(ruff TID252, `ban-relative-imports = "all"`).

## Types are checked, not just written

A static type checker runs via `make typecheck` (pyright, dev-only — it never
ships). It is scoped to the annotated store surface;
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
make bootstrap  # once, or after a dependency change: uv, the interpreter, the deps
make lint       # ruff check + ruff format --check
make typecheck  # pyright over the annotated store surface
make test
```

Every target runs through `uv run`, so what you test on is what a user's install
runs on.

Do **not** add GitHub Actions / CI workflows — CI is owner-managed to org
conventions. The Makefile targets are the seam CI will call.

## Tests are plain pytest

No homegrown test framework. Fixtures point `$XDG_*_HOME` at a tmpdir so tests
never touch a real install. Ported legacy tests are converted to plain pytest.

## Path authority

`paths.py` is the only module allowed to resolve the home directory or read an
`XDG_*` variable — or their Windows spellings, `USERPROFILE`, `LOCALAPPDATA` and
`APPDATA`. Everything else routes through it. A guard test
(`tests/guard/test_path_authority.py`) enforces this — it is the structural
defense against writing to a location the running daemon never reads.

## Platform differences live in one of two places, never scattered

`platform/` is the seam for *host integration*: where a job definition lives, how
it is registered, what it is called. Nothing else may name launchd or Task
Scheduler.

Everything else per-OS goes in the portable module that owns the concern, with the
branch inside it — `paths.py` (roots, venv layout), `lock.py`, `pointer.py`
(`current` and the shim), `spawn.py` (resolution, creation flags, exec),
`proc_tree.py`. (`images.py` earned its way out of the list: Pillow made it
branch-free.) The reason for the split is ordering: `get_platform()` refuses an
unsupported host, and these have to answer before anything has decided whether the
host is supported — during a bootstrap, or from a CLI holding no platform.

Three guards keep this honest: every module must import on any OS (no exemptions —
even `platform/windows.py` keeps its module-level imports portable); per-OS stdlib
imports (`fcntl`, `msvcrt`, `winreg`, `_winapi`) are recognised as stdlib whichever
OS runs the suite, so a verdict never depends on the machine; and platform
conditionals (`os.name`, `sys.platform`, the per-OS stdlib modules) may appear only
in an allowlisted set of owner modules
(`tests/guard/test_portability.py`) — growing that list is a deliberate,
visible act.

Windows-specific rules worth stating once: file modes are not applied there
rather than emulated (privacy rests on the user profile, as it does for Claude
Code's own credentials); there is no exec that replaces a process, so `spawn.become`
runs the child and adopts its status; a bare program name does not resolve, so
every spawn goes through `spawn.resolve`; and the scheduled task's definition
cannot carry environment variables, so the supervisor writes them to a companion
`.env.json` beside the task XML and the launcher applies it (`--env-file`).

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
  attended-direct via `update_seller_config`, or by the installer through
  `/control/seller-basics`. Both share one validator (`tools.seller.validate_basics`),
  which upper-cases region and currency because every registry lookup is an exact
  match, and checks the timezone against the zone database.
- **Seller settings** — the `settings` table plus the `settings.py` code
  registry. Runtime behavior knobs the seller changes through a door. Defaults
  live in the registry, not as rows: an unset key reads as its default. Adding a
  setting is one `SettingSpec` (type/parse/render/default/description/approval
  policy) — the registry is the validation source, the card's discoverability
  source, and the LLM's vocabulary at once. `SettingSpec.parse` is pure: a value
  that is only valid against seller state (which marketplaces exist for their
  region) is checked in `settings.check_for_seller`, called once from the propose
  tool.

**Settings change only through a door — the LLM proposes, it never applies.**
`propose_setting_change` is the *only* settings-mutation tool on any tier; there
is deliberately no apply/approve/undo/cancel tool. The daemon decides by policy
(a registry `requires_approval` flag): high-stakes changes are held for a human
signal (an Approve/Cancel button, an exact `approve <id>` text token, or the
attended `selly-agent settings approve <id>` CLI over `/control/settings-decide`);
low-stakes ones apply immediately in deterministic store code. `selly-agent
settings set <key> <value>` (over `/control/settings-set`) skips the approval
round-trip and only that: the gate exists to stop the *model* changing things
unasked, and someone typing at their own terminal has already given the signal it
waits for — so the same parser and the same `check_for_seller` run, and the prior
value is recorded so Undo still works. The installer writes the marketplaces the
seller opted into through that door rather than touching the database itself. Every apply is one
`selly.db` transaction (setting upsert + ledger row + echo notice), and every
consumer reads its setting at its own decision point (read-at-use — no caching, no
reload). The no-apply-tool rule is enforced by a guard test, not just convention.

## Engines stay pure

Modules under `src/selly_agent/engines/` are pure decision layers. They must not
import tool or server modules, must not touch the store or the network, and must
not read the clock except through a `now` parameter. A tool composes an engine
with the store (one transaction per decision); the engine only decides. This is
what keeps the money/safety logic unit-testable in isolation and the network-free
guarantee structural (the import guard would flag a stray `urllib`).

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

## Skill content

The markdown under `src/selly_agent/skills/` is prompt content, and the same
comment rule applies with more force: it is read by a model that has none of our
context, so a reference it cannot resolve is worse than nothing. Two more:

- **Router to anchor: each rule lives in exactly one file**, and the others point
  at it rather than restating it. A rule stated twice drifts, and the model gets
  to pick which version it follows.
- **No choreography in prose.** If a sequence must happen in an order, that order
  belongs inside a tool, not in instructions the model may skip, reorder, or be
  interrupted halfway through. What stays in prose is judgment: what to say, what
  to refuse, when to ask. If you find yourself writing "first call X, then Y, then
  record Z", the tool surface is missing a tool.

## Version control

Commit in logical units, each building and passing tests on
its own; order commits so a reviewer never sees code that calls something
introduced later. Isolate generated/mechanical churn from hand-written diffs.
