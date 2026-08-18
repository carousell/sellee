"""The prompt layer: skill files as package data, composed into a pass's system prompt.

A *skill* is the standing rulebook a pass works under — voice, the listing flow, the house
conventions. It is stable across passes, so it rides in the system prompt (which a harness can
cache) while the dynamic task — the rows to handle, the item to publish, the conversation so far —
stays in the user prompt.

Which skills a pass type gets is declared with the pass type itself, not here: adding a pass type
stays a single registry entry rather than an edit in two places.

Skill files ship inside the package (the marketplaces.json convention) and are read
`__file__`-relative, so a versioned install serves them from its own tree and a checkout serves
them from the checkout. Frontmatter is stripped when a file is inlined into a prompt — it is
metadata for a human reader, not instructions for the model.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent
COMMANDS_DIR = SKILLS_DIR / "commands"

# A ceiling on the composed system prompt, in characters. Every skill added to a pass type is paid
# on every pass of that type, forever — this fails a test rather than quietly inflating the bill.
MAX_SYSTEM_PROMPT_CHARS = 40_000

_FRONTMATTER_FENCE = "---"


class SkillNotFound(Exception):
    """A skill file named by a pass type does not exist — a packaging error, caught at load."""


def skill_path(name: str) -> Path:
    return SKILLS_DIR / f"{name}.md"


def command_path(name: str) -> Path:
    return COMMANDS_DIR / f"{name}.md"


def available() -> list:
    """Every installed skill name, sorted — the inventory a test can walk."""
    return sorted(p.stem for p in SKILLS_DIR.glob("*.md"))


def available_commands() -> list:
    return sorted(p.stem for p in COMMANDS_DIR.glob("*.md"))


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block. Anything else — including a `---` rule further down
    the file — is content and survives untouched."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text  # an unterminated fence is not frontmatter


@cache
def load(name: str) -> str:
    """One skill's body, frontmatter stripped. Cached: the files are immutable in an install."""
    path = skill_path(name)
    try:
        raw = path.read_text()
    except OSError as exc:
        raise SkillNotFound(f"no skill file for {name!r} at {path}") from exc
    return strip_frontmatter(raw).strip()


def compose_system_prompt(names) -> str:
    """The skills, in the order the pass type declares them, as one system prompt."""
    names = tuple(names)
    if not names:
        return ""
    composed = "\n\n".join(load(name) for name in names)
    if len(composed) > MAX_SYSTEM_PROMPT_CHARS:
        raise ValueError(
            f"composed system prompt is {len(composed)} chars, over the "
            f"{MAX_SYSTEM_PROMPT_CHARS} cap: {', '.join(names)}"
        )
    return composed
