"""Fuzz-style property for the parser that reads untrusted subprocess output.

`claude -p --output-format stream-json` is another process's stdout: the schema can change
under us and a crashed child can emit a partial line. parse_stream_line's docstring promises
"Never raises", and a reader loop depends on it, so the promise is worth generating against.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from sellee.pass_stream import parse_stream_line

# Arbitrary bytes-as-text, plus well-formed JSON, so the generator spends some of its budget
# past the json.loads guard rather than only on garbage that fails to parse.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=8,
)
lines = st.one_of(
    st.text(),
    json_values.map(json.dumps),
    st.fixed_dictionaries(
        {"type": st.sampled_from(["system", "assistant", "user", "result"])}
    ).flatmap(lambda d: st.just(json.dumps(d))),
)


@given(lines)
def test_it_never_raises_and_always_returns_pairs(line: str) -> None:
    """Totality, as the docstring promises. Anything unrecognised must degrade to an event,
    never to an exception that kills the reader."""
    events = parse_stream_line(line)
    assert isinstance(events, list)
    for event in events:
        kind, payload = event
        assert isinstance(kind, str)
        assert isinstance(payload, dict)
