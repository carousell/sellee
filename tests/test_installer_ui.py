"""Setup's output: the structure a reader navigates by, the decoration gates, and prompts that
never hang."""

from __future__ import annotations

import io

import pytest

from sellee.installer.ui import Abort, Ui


def make_ui(**kwargs) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    defaults = {"stream": out, "err": err, "interactive": False, "color": False, "width": 100}
    defaults.update(kwargs)
    return Ui(**defaults), out, err


def test_a_step_opens_with_a_blank_line_and_what_follows_is_flat() -> None:
    # The blank line above a heading is the whole navigation scheme: it is what separates one
    # phase's report from the next in a scroll that runs for pages. Nothing else is indented.
    ui, out, _ = make_ui()
    ui.step("Installing Sellee")
    ui.say("/opt/versions/1.0.0")
    assert out.getvalue().splitlines() == ["", "Installing Sellee", "/opt/versions/1.0.0"]


def test_colour_marks_headings_and_questions_but_not_what_they_report() -> None:
    # Colour is the reader's index. Spending it on body text as well would leave nothing to
    # distinguish the lines they navigate by or have to answer.
    ui, out, _ = make_ui(color=True)
    ui.step("Checking this machine")
    ui.say("node: v22")
    ui.confirm("Proceed?", default=True)

    heading, body, question = [line for line in out.getvalue().splitlines() if line.strip()]
    assert "\033" in heading
    assert "\033" in question
    assert "\033" not in body


def test_no_color_strips_escapes(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    out = io.StringIO()
    out.isatty = lambda: True  # a TTY that has nonetheless asked for no colour
    ui = Ui(stream=out, interactive=False, width=100)
    ui.say("hello")
    assert "\033" not in out.getvalue()


def test_color_is_off_when_the_stream_is_not_a_tty(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    ui = Ui(stream=io.StringIO(), interactive=False, width=100)
    assert ui.color is False


def test_narrow_terminal_falls_back_to_a_one_line_banner() -> None:
    ui, out, _ = make_ui(width=40)
    ui.banner("1.2.3")
    assert out.getvalue() == "Sellee v1.2.3\n"


def test_wide_terminal_draws_the_banner() -> None:
    ui, out, _ = make_ui(width=100)
    ui.banner("1.2.3")
    assert len(out.getvalue().splitlines()) > 1


def test_banner_art_fits_its_own_width_gate() -> None:
    from sellee.installer import ui as ui_module

    assert max(len(line) for line in ui_module.BANNER) <= ui_module.BANNER_MIN_COLUMNS


# --- prompts ---------------------------------------------------------------------------------


def test_confirm_without_a_terminal_takes_the_default_out_loud() -> None:
    ui, out, _ = make_ui(interactive=False)
    assert ui.confirm("Start at login?", default=True) is True
    assert "Start at login? [Y/n] y" in out.getvalue()


def test_confirm_with_assume_yes_never_reads_stdin() -> None:
    def explode():
        raise AssertionError("stdin must not be read under --yes")

    ui, _, _ = make_ui(interactive=True, assume_yes=True, input_fn=explode)
    assert ui.confirm("Proceed?", default=True) is True


def test_confirm_reads_yes_and_no() -> None:
    answers = iter(["", "maybe", "n"])
    ui, _, _ = make_ui(interactive=True, input_fn=lambda: next(answers))
    assert ui.confirm("A?", default=True) is True  # empty takes the default
    assert ui.confirm("B?", default=True) is False  # "maybe" re-prompts, then "n"


def test_ask_falls_back_to_the_default_on_an_empty_answer() -> None:
    ui, _, _ = make_ui(interactive=True, input_fn=lambda: "")
    assert ui.ask("Region?", default="SG") == "SG"


def test_choose_reprompts_until_the_answer_is_in_range() -> None:
    answers = iter(["9", "x", "2"])
    ui, _, _ = make_ui(interactive=True, input_fn=lambda: next(answers))
    assert ui.choose("Pick", ["a", "b", "c"]) == 1


def test_choose_without_a_terminal_takes_the_default_index() -> None:
    ui, _, _ = make_ui(interactive=False)
    assert ui.choose("Pick", ["a", "b"], default_index=1) == 1


def test_multiselect_accepts_numbers_all_and_skip() -> None:
    ui, _, _ = make_ui(interactive=True, input_fn=lambda: "3,1")
    assert ui.multiselect("Which?", ["a", "b", "c"]) == [0, 2]

    ui, _, _ = make_ui(interactive=True, input_fn=lambda: "a")
    assert ui.multiselect("Which?", ["a", "b"]) == [0, 1]

    ui, _, _ = make_ui(interactive=True, input_fn=lambda: "")
    assert ui.multiselect("Which?", ["a", "b"]) == []


def test_multiselect_reprompts_on_an_out_of_range_number() -> None:
    answers = iter(["4", "2"])
    ui, _, _ = make_ui(interactive=True, input_fn=lambda: next(answers))
    assert ui.multiselect("Which?", ["a", "b", "c"]) == [1]


def test_multiselect_without_a_terminal_selects_nothing() -> None:
    ui, _, _ = make_ui(interactive=False)
    assert ui.multiselect("Which?", ["a", "b"]) == []


def test_multiselect_under_assume_yes_selects_nothing_rather_than_everything() -> None:
    # "Take the default" for a list of things to opt into means take none of them.
    def explode():
        raise AssertionError("must not read stdin")

    ui, _, _ = make_ui(interactive=True, assume_yes=True, input_fn=explode)
    assert ui.multiselect("Which?", ["a", "b"]) == []


def test_an_interrupted_prompt_aborts_rather_than_returning_a_default() -> None:
    def interrupted():
        raise KeyboardInterrupt

    ui, _, _ = make_ui(interactive=True, input_fn=interrupted)
    with pytest.raises(Abort):
        ui.confirm("Proceed?")


# --- fatal errors ----------------------------------------------------------------------------


def test_die_raises_and_fatal_renders_the_fix_to_stderr() -> None:
    ui, out, err = make_ui()
    with pytest.raises(Abort) as caught:
        ui.die("no Chrome here", fix="brew install --cask google-chrome")
    ui.fatal(caught.value)
    assert out.getvalue() == ""
    assert "error: no Chrome here" in err.getvalue()
    assert "brew install --cask google-chrome" in err.getvalue()


def test_multiselect_says_something_when_it_cannot_read_the_answer() -> None:
    answers = iter(["nonsense", "1"])
    ui, out, _ = make_ui(interactive=True, input_fn=lambda: next(answers))
    assert ui.multiselect("Which?", ["a", "b"]) == [0]
    assert "Not understood" in out.getvalue()
