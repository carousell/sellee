"""The repo is Apache-2.0 licensed, and every artifact that says so must stay intact.

The repo is public: without a license file, "public" means all rights reserved and nobody
may actually use, modify, or redistribute the code. Apache-2.0's one hard requirement on
us is that the license text ships with the work, verbatim. The rest of what this guards is
the machinery that keeps that promise true everywhere the code travels: NOTICE carries the
attribution redistributors must preserve (§4(d)), pyproject's license-files puts both files
into any wheel or sdist, and the README tells a human what they may do. An edit that drops
or mangles any of these fails here, in the suite, rather than in a downstream fork.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Phrases that appear in the canonical text from https://www.apache.org/licenses/LICENSE-2.0.txt
# and not in a summary or a stub. Checking phrases rather than a byte hash tolerates
# whitespace normalization while still rejecting a paraphrase.
CANONICAL_LICENSE_MARKERS = (
    "Apache License",
    "Version 2.0, January 2004",
    "4. Redistribution.",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "APPENDIX: How to apply the Apache License to your work.",
)


def test_license_is_the_canonical_apache_2_text() -> None:
    path = ROOT / "LICENSE"
    assert path.is_file(), (
        "LICENSE is missing — the repo is public, so absent a license nothing may be reused"
    )
    text = path.read_text()
    missing = [marker for marker in CANONICAL_LICENSE_MARKERS if marker not in text]
    assert not missing, f"LICENSE is not the canonical Apache-2.0 text; missing: {missing}"


def test_notice_carries_the_attribution() -> None:
    path = ROOT / "NOTICE"
    assert path.is_file(), (
        "NOTICE is missing — it is the only line redistributors must carry forward"
    )
    text = path.read_text()
    assert "Sellee" in text and "Copyright" in text, (
        "NOTICE must name the project and hold a Copyright line; anything else here is noise"
    )


def test_pyproject_declares_the_license_and_ships_both_files() -> None:
    # Exact-line markers rather than a TOML parse: the tree holds the py39 line (no tomllib,
    # see AGENTS.md), and pyproject is our own file, so its formatting is ours to pin.
    text = (ROOT / "pyproject.toml").read_text()
    assert 'license = "Apache-2.0"' in text, (
        "pyproject [project].license must be the SPDX expression 'Apache-2.0'"
    )
    assert 'license-files = ["LICENSE", "NOTICE"]' in text, (
        "pyproject [project].license-files must list LICENSE and NOTICE so wheel builds ship them"
    )


def test_readme_tells_the_reader_the_terms() -> None:
    text = (ROOT / "README.md").read_text()
    assert "## License" in text and "Apache License 2.0" in text and "(LICENSE)" in text, (
        "README must keep a License section naming Apache License 2.0 and linking the LICENSE file"
    )
