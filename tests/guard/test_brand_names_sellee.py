"""Copy names the marketplace Sellee, and a carousell.ai host appears only where it is still
accepted.

The marketplace was called carousell.ai and is called Sellee now. Two rules follow:

- **No copy says "carousell.ai".** Everything the seller, the buyer or the model reads says Sellee:
  string literals in the package, the skills, the buyer simulator's visible text, and each
  marketplace's display name. `carousell-ai` (the market id), relay addresses like
  `seller@inbox.carousell.ai`, and lookalike hosts like `carousell.ai.scam.site` are not the word.
- **Pages are on www.sellee.ai.** Links are built from the configured web base, so no string
  literal names a carousell.ai page host, except the entry in `config.PRODUCTION_WEB_BASES` that
  keeps old links accepted. In the marketplace registry, carousell.ai appears only in the rail's
  `also_hosts`, which the scam allowlist reads.

Literals only, never docstrings or comments: comments describe code, and tests that check old
links are still accepted have to name the old host.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from sellee import marketplaces

SRC = Path(__file__).resolve().parents[2] / "src" / "sellee"
SKILLS = SRC / "skills"
BUYER_PAGE = SRC / "data" / "buyer.html"
REGISTRY = SRC / "data" / "marketplaces.json"

# "carousell.ai" as a word, in any case, a sentence's closing period included: not part of a host,
# an address or an id.
BRAND_WORD = re.compile(r"(?i)(?<![\w.@/-])carousell\.ai(?![\w@/-]|\.\w)")
# A carousell.ai page or API host.
PAGE_HOST = re.compile(
    r"(?i)(www\.|api\.)?carousell\.ai/|(www|api)\.carousell\.ai|://carousell\.ai"
)

# HTML comments, JS block comments, and JS line comments (a `//` that starts a line or follows
# whitespace, so a URL's `https://` is left alone).
_PAGE_COMMENTS = re.compile(r"<!--.*?-->|/\*.*?\*/|(?:^|(?<=\s))//.*?$", re.DOTALL | re.MULTILINE)


def _literals(path: Path):
    """Every string literal in a module that is not a docstring, as (node, value)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docstrings:
                yield node, node.value


def _production_web_bases_literals() -> set:
    """Where the literals inside config.PRODUCTION_WEB_BASES sit, as (path, line, value): the one
    place an old host is kept."""
    path = SRC / "config.py"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PRODUCTION_WEB_BASES" for t in node.targets
        ):
            return {
                (path, n.lineno, n.value)
                for n in ast.walk(node.value)
                if isinstance(n, ast.Constant)
            }
    raise AssertionError("config.PRODUCTION_WEB_BASES not found")


def _modules():
    return sorted(SRC.rglob("*.py"))


def test_no_string_literal_names_carousell_ai() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{node.lineno} — {value!r}"
        for path in _modules()
        for node, value in _literals(path)
        if BRAND_WORD.search(value)
    ]
    assert offenders == [], "copy naming carousell.ai:\n" + "\n".join(offenders)


def test_no_skill_names_carousell_ai() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in sorted(SKILLS.rglob("*.md"))
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if BRAND_WORD.search(line)
    ]
    assert offenders == [], "skills naming carousell.ai:\n" + "\n".join(offenders)


def test_the_buyer_page_text_never_names_carousell_ai() -> None:
    text = _PAGE_COMMENTS.sub("", BUYER_PAGE.read_text())
    assert not BRAND_WORD.search(text)


def test_no_skill_or_buyer_page_links_a_carousell_ai_page() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in [*sorted(SKILLS.rglob("*.md")), BUYER_PAGE]
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"(?i)carousell\.ai/", line)
    ]
    assert offenders == [], "carousell.ai page links:\n" + "\n".join(offenders)


def test_no_display_name_says_carousell_ai() -> None:
    names = [entry.get("display_name", "") for entry in marketplaces.all_marketplaces()]
    assert [name for name in names if BRAND_WORD.search(name)] == []


def test_only_production_web_bases_names_a_carousell_ai_host() -> None:
    allowed = _production_web_bases_literals()
    offenders = [
        f"{path.relative_to(SRC)}:{node.lineno} — {value!r}"
        for path in _modules()
        for node, value in _literals(path)
        if PAGE_HOST.search(value) and (path, node.lineno, value) not in allowed
    ]
    assert offenders == [], "carousell.ai hosts outside PRODUCTION_WEB_BASES:\n" + "\n".join(
        offenders
    )


def test_the_registry_names_carousell_ai_only_as_the_rails_extra_host() -> None:
    offenders = []
    for entry in json.loads(REGISTRY.read_text())["marketplaces"]:
        rest = dict(entry)
        if entry["id"] == marketplaces.RAIL:
            rest.pop("also_hosts", None)
        if re.search(r"(?i)carousell\.ai", json.dumps(rest)):
            offenders.append(entry["id"])
    assert offenders == []
