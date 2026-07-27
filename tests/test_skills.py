"""The prompt layer: the loader, the per-pass-type skill sets, and what ends up in a pass's
system prompt versus its task prompt.

These are behavioral, not editorial — nothing here pins prose. Renaming a heading must stay free;
losing a skill file, dropping the marker, or quietly doubling every pass's prompt must not.
"""

from __future__ import annotations

import pytest

from selly_agent import passes, skills
from selly_agent.harness import claude
from selly_agent.proc_tree import PASS_PROMPT_MARKER

# The set a pass type is allowed to draw from, and the composition per type. A change here should
# be a deliberate diff, because every skill is paid on every pass of that type.
EXPECTED_SKILL_SETS = {
    "publish": ("selly-conventions", "listing-flow"),
    "channel": ("selly-conventions", "voice-and-style", "seller-comms", "listing-flow"),
}


def _spec_for(pass_type_name: str, prompt: str = "do the thing"):
    return passes.build_spec(
        prompt, "http://127.0.0.1:1/mcp", "TOK", "sonnet", passes.PASS_TYPES[pass_type_name]
    )


# --- the loader ---------------------------------------------------------------------------------


def test_every_installed_skill_loads() -> None:
    names = skills.available()
    assert names  # a packaging failure would silently make this empty
    for name in names:
        assert skills.load(name).strip()


def test_frontmatter_is_stripped_but_content_rules_survive() -> None:
    assert skills.strip_frontmatter("---\ndescription: x\n---\n\n# Title\nbody") == "# Title\nbody"
    # a horizontal rule inside the body is content, not a fence
    assert skills.strip_frontmatter("# Title\n\n---\n\nmore") == "# Title\n\n---\n\nmore"
    # an unterminated fence is not frontmatter either — better to keep text than eat the file
    assert skills.strip_frontmatter("---\ndescription: x\n") == "---\ndescription: x\n"


def test_no_loaded_skill_still_carries_frontmatter() -> None:
    for name in skills.available():
        assert not skills.load(name).startswith("---")


def test_a_missing_skill_fails_loudly() -> None:
    with pytest.raises(skills.SkillNotFound):
        skills.load("no-such-skill")


def test_the_drafts_ship_without_their_authoring_notes() -> None:
    """The skill files are prompt content for a model that has no access to the planning repo, so
    a dangling reference to a plan number is worse than no note at all."""
    for name in skills.available():
        body = skills.load(name)
        assert "plan-0" not in body
        assert "TODO(" not in body


# --- composition per pass type ------------------------------------------------------------------


def test_every_pass_type_declares_resolvable_skills() -> None:
    for name, pass_type in passes.PASS_TYPES.items():
        assert pass_type.skills, f"{name} declares no skills"
        for skill in pass_type.skills:
            assert skills.skill_path(skill).exists(), f"{name} names a missing skill {skill!r}"


def test_skill_sets_are_exactly_as_declared() -> None:
    assert {n: t.skills for n, t in passes.PASS_TYPES.items()} == EXPECTED_SKILL_SETS


def test_composition_concatenates_in_declared_order() -> None:
    composed = skills.compose_system_prompt(("selly-conventions", "listing-flow"))
    assert composed.index(skills.load("selly-conventions")) < composed.index(
        skills.load("listing-flow")
    )


def test_composition_of_nothing_is_empty() -> None:
    assert skills.compose_system_prompt(()) == ""


def test_the_size_cap_holds_for_every_pass_type() -> None:
    for name, pass_type in passes.PASS_TYPES.items():
        composed = skills.compose_system_prompt(pass_type.skills)
        assert len(composed) <= skills.MAX_SYSTEM_PROMPT_CHARS, name


def test_the_size_cap_is_enforced_not_decorative(monkeypatch) -> None:
    monkeypatch.setattr(skills, "MAX_SYSTEM_PROMPT_CHARS", 10)
    with pytest.raises(ValueError, match="over the"):
        skills.compose_system_prompt(("selly-conventions",))


# --- what lands where in the spec ---------------------------------------------------------------


def test_the_rulebook_rides_the_system_prompt_and_the_task_rides_the_user_prompt() -> None:
    spec = _spec_for("publish", passes.publish_prompt("item_1"))
    assert spec.append_system_prompt == skills.compose_system_prompt(
        passes.PASS_TYPES["publish"].skills
    )
    # the task prompt carries the marker the reaper greps for, and the rulebook does not duplicate
    # into it — the whole point of the split is that the stable half can be cached
    assert spec.prompt.startswith(PASS_PROMPT_MARKER)
    assert "item_1" in spec.prompt
    assert skills.load("listing-flow") not in spec.prompt


def test_the_publish_prompt_names_its_item_and_the_reaper_marker() -> None:
    prompt = passes.publish_prompt("item_abc")
    assert PASS_PROMPT_MARKER in prompt
    assert "item_abc" in prompt


def test_web_research_is_on_for_the_conversation_and_off_for_publishing() -> None:
    """Only the flow whose skills tell it to price against comps gets the web; a pass that talks to
    no one has no reason to fetch untrusted pages."""
    assert _spec_for("channel").web_tools is True
    assert _spec_for("publish").web_tools is False
    assert set(claude.WEB_TOOLS) <= set(claude.allowed_tools(_spec_for("channel")))
    assert set(claude.WEB_TOOLS) <= set(claude.denied_tools(_spec_for("publish")))


def test_a_real_spec_renders_to_a_valid_workspace_and_argv() -> None:
    """The round-trip validators run inside these calls, so this is the pin that a composed
    system prompt and the web posture survive emission."""
    for name in passes.PASS_TYPES:
        spec = _spec_for(name)
        claude.render_workspace(spec)
        argv = claude.pass_argv(spec, claude_bin="claude")
        assert argv[argv.index("--append-system-prompt") + 1] == spec.append_system_prompt


# --- media grants per pass type -------------------------------------------------------------------


class _InboxStore:
    """Just enough store for the media-path builders: claimed rows with media."""

    def __init__(self, rows):
        self._rows = rows

    def inbox_for_pass(self, pass_id):
        return self._rows


def test_the_channel_pass_grants_exactly_its_claimed_media() -> None:
    rows = [
        {"kind": "photo", "media_paths": ["/m/a.jpg", "/m/b.jpg"]},
        {"kind": "text", "media_paths": []},
        {"kind": "photo", "media_paths": None},  # a failed download stores no paths
    ]
    build = passes.PASS_TYPES["channel"].build_media_paths
    assert build({}, _InboxStore(rows), "pass_1") == ("/m/a.jpg", "/m/b.jpg")


def test_the_publish_pass_grants_no_media() -> None:
    """Publishing never needs eyes on a photo — the upload is daemon-side — so nothing widens
    its file access."""
    build = passes.PASS_TYPES["publish"].build_media_paths
    assert build({"item_id": "item_1"}, object(), "pass_1") == ()


def test_granted_media_reaches_the_spec_resolved(xdg_tmp) -> None:
    from selly_agent import paths

    photo = paths.media_dir() / "1" / "photo.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"\xff\xd8\xff")
    spec = passes.build_spec(
        "do the thing",
        "http://127.0.0.1:1/mcp",
        "TOK",
        "sonnet",
        passes.PASS_TYPES["channel"],
        media_paths=(str(photo),),
    )
    assert spec.readable_paths == (str(photo.resolve()),)


def test_a_media_grant_outside_the_media_store_is_refused(xdg_tmp, tmp_path) -> None:
    """The store's containment gate should make this unreachable, so reaching it is a bug — the
    spec build fails loudly rather than quietly granting a read outside the media store."""
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(ValueError, match="escapes the media store"):
        passes.build_spec(
            "do the thing",
            "http://127.0.0.1:1/mcp",
            "TOK",
            "sonnet",
            passes.PASS_TYPES["channel"],
            media_paths=(str(outside),),
        )


# --- tier membership ----------------------------------------------------------------------------


def test_the_live_pass_tiers_are_pinned() -> None:
    """A pass's tool surface is what it can do at all, so widening one should read as a decision in
    a diff rather than fall out of a tool registration."""
    import json
    from pathlib import Path

    from selly_agent.tools import tools_for_tier

    golden = json.loads((Path(__file__).parent / "golden" / "pass_tiers.json").read_text())
    for tier, expected in golden.items():
        assert sorted(s.name for s in tools_for_tier(tier)) == expected, tier


def test_every_tool_a_pass_type_can_call_is_reachable_by_name() -> None:
    """The allow-list the harness receives is derived from the tier, so a tier holding a tool the
    registry lost would silently narrow a pass rather than fail."""
    for name, pass_type in passes.PASS_TYPES.items():
        allowed = passes.allowed_tools_for(pass_type.tier)
        assert allowed, f"{name} has an empty tool surface"
        assert all(t.startswith("mcp__selly__") for t in allowed)


# --- commands -----------------------------------------------------------------------------------


def test_the_five_attended_commands_ship() -> None:
    assert set(skills.available_commands()) == {"sell", "selly", "catchup", "pause", "resume"}


def test_command_bodies_keep_their_frontmatter() -> None:
    """Commands are files a harness reads, not text we inline — their frontmatter is load-bearing
    where a skill's is not."""
    for name in skills.available_commands():
        assert skills.command_path(name).read_text().startswith("---\n")
