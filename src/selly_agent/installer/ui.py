"""Setup's terminal: the one place that prints for the installer, and the one place that prompts.

An install is a long scroll, so the output is shaped to be skimmable: each phase opens with a
heading — a blank line above it, colour on the text — and everything that phase reports follows
plainly beneath, unindented. The blank line is the only separator, and colour is reserved for
headings and questions, which are the only lines a reader navigates by or has to answer.

Everything decorative is conditional: colour only when stdout is a TTY with NO_COLOR unset, the
banner only when the terminal is wide enough for it. A run whose stdin is not a person never
blocks on a prompt; each one answers with its default and says that it did, so a scripted install
reads as a transcript rather than a hang.
"""

from __future__ import annotations

import os
import shutil
import sys

# The banner is skipped below this width rather than wrapped: a wrapped banner is unreadable
# noise, and the one-line fallback carries the same information.
BANNER_MIN_COLUMNS = 64

BANNER = (
    "███████╗███████╗██╗     ██╗     ██╗   ██╗    ▟██▙     ▟██▙",
    "██╔════╝██╔════╝██║     ██║     ╚██╗ ██╔╝   ▟█████████████▙",
    "███████╗█████╗  ██║     ██║      ╚████╔╝   ▐████  ███  ████▌",
    "╚════██║██╔══╝  ██║     ██║       ╚██╔╝    ▐███████▄███████▌",
    "███████║███████╗███████╗███████╗   ██║      ▜█████████████▛",
    "╚══════╝╚══════╝╚══════╝╚══════╝   ╚═╝        ▀▀▀▀▀▀▀▀▀▀▀",
)

_TEAL = "\033[36m"
_BOLD_TEAL = "\033[1;36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


class Abort(Exception):
    """Setup cannot continue. Carries the reason and, where there is one, the fix.

    Raised rather than printed at the failure site so there is exactly one place that renders a
    fatal error, and so every gate is a pure `raise` a test can assert on.
    """

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.message = message
        self.fix = fix


class Ui:
    """Setup's terminal. Construct once and pass it down; tests build one over a StringIO."""

    def __init__(
        self,
        *,
        stream=None,
        err=None,
        interactive: bool | None = None,
        color: bool | None = None,
        width: int | None = None,
        assume_yes: bool = False,
        input_fn=None,
    ):
        self.stream = stream if stream is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self.interactive = self._detect_interactive() if interactive is None else interactive
        self.color = self._detect_color() if color is None else color
        self._width = width
        self.assume_yes = assume_yes
        self._input = input_fn if input_fn is not None else input

    # --- environment ---------------------------------------------------------------------

    def _detect_interactive(self) -> bool:
        return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(
            getattr(self.stream, "isatty", lambda: False)()
        )

    def _detect_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    @property
    def width(self) -> int:
        if self._width is not None:
            return self._width
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    # --- output --------------------------------------------------------------------------

    def step(self, text: str) -> None:
        """Open a phase: a blank line, then the phase's name in colour.

        Headings are what makes the transcript navigable, so a phase gets exactly one — either
        this, or the question that opens it (the prompts render themselves the same way).
        """
        print("", file=self.stream)
        print(self._paint(text, _BOLD_TEAL), file=self.stream)

    def say(self, text: str = "") -> None:
        """A line of body text: everything a phase reports, and the only unmarked output.

        Flat rather than indented under its heading. The blank line a heading opens with is
        separation enough, and indenting what follows buys nothing back for the width it costs on
        lines that are mostly paths.
        """
        print(text, file=self.stream)

    def note(self, text: str) -> None:
        """A dimmed aside: what a step assumed, what was skipped, where to look."""
        print(self._paint(text, _DIM), file=self.stream)

    def warn(self, text: str) -> None:
        print(self._paint(f"warn: {text}", _YELLOW), file=self.stream)

    def banner(self, version: str) -> None:
        if self.width < BANNER_MIN_COLUMNS or not self._can_encode(BANNER[0]):
            print(self._paint(f"Selly v{version}", _BOLD_TEAL), file=self.stream)
            return
        for line in BANNER:
            print(self._paint(line, _TEAL), file=self.stream)

    def _can_encode(self, text: str) -> bool:
        """Whether this terminal can print `text` at all.

        A legacy Windows console runs a code page that has none of the box-drawing characters, and
        printing them there does not degrade — it raises UnicodeEncodeError partway through the
        first line of the installer. Asked rather than assumed, so a console that *can* show them
        still does.
        """
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        try:
            text.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
        return True

    def fatal(self, exc: Abort) -> None:
        """Render an Abort. The only place a fatal error is printed."""
        print(self._paint(f"error: {exc.message}", _RED), file=self.err)
        if exc.fix:
            for line in exc.fix.split("\n"):
                print(f"  {line}", file=self.err)

    def die(self, message: str, fix: str = "") -> None:
        raise Abort(message, fix)

    # --- prompts -------------------------------------------------------------------------

    def _open_prompt(self, lead: bool) -> None:
        """A question doubles as its phase's heading, so by default it gets the same blank line
        above it that `step` gives. `lead=False` is for a question nested inside an open phase."""
        if lead:
            print("", file=self.stream)

    def _ask_raw(self, prompt: str) -> str:
        print(self._paint(prompt, _BOLD_TEAL), end="", file=self.stream, flush=True)
        try:
            return self._input().strip()
        except (EOFError, KeyboardInterrupt):
            print("", file=self.stream)
            raise Abort("interrupted — nothing further was changed") from None

    def _auto_answer(self, text: str) -> None:
        """Echo a question and the answer taken for it, so a non-interactive run still reads as
        a transcript of decisions rather than as output with unexplained gaps."""
        print(self._paint(text, _BOLD_TEAL), file=self.stream)

    def confirm(self, question: str, *, default: bool = True, lead: bool = True) -> bool:
        """A yes/no question. `--yes` and a non-interactive run both take the default, out loud."""
        suffix = "[Y/n]" if default else "[y/N]"
        self._open_prompt(lead)
        if self.assume_yes or not self.interactive:
            self._auto_answer(f"{question} {suffix} {'y' if default else 'n'}")
            return default
        while True:
            answer = self._ask_raw(f"{question} {suffix} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def ask(self, question: str, *, default: str = "", lead: bool = True) -> str:
        """Free text, with a default shown when there is one."""
        self._open_prompt(lead)
        if not self.interactive:
            self._auto_answer(f"{question} {default}")
            return default
        shown = f" [{default}]" if default else ""
        return self._ask_raw(f"{question}{shown} ") or default

    def choose(self, question: str, options, *, default_index: int = 0, lead: bool = True) -> int:
        """Pick one of a numbered list; returns its index."""
        self._open_prompt(lead)
        if not self.interactive:
            self._auto_answer(f"{question} {options[default_index]}")
            return default_index
        self._auto_answer(question)
        for number, option in enumerate(options, start=1):
            self.say(f"{number}) {option}")
        while True:
            raw = self._ask_raw(f"Enter 1-{len(options)} [default {default_index + 1}]: ")
            if not raw:
                return default_index
            try:
                picked = int(raw)
            except ValueError:
                continue
            if 1 <= picked <= len(options):
                return picked - 1

    def multiselect(self, question: str, options, *, lead: bool = True) -> list:
        """Pick any number of a numbered list; returns their indices, possibly none.

        Skipping is a first-class answer (plain Enter), because every caller of this is an
        optional step — offering a list must never become a step you cannot get past.
        """
        if not options:
            return []
        self._open_prompt(lead)
        if not self.interactive or self.assume_yes:
            # Unlike a yes/no question, this one's default is *none*: every caller of it opts the
            # seller into something public, and choosing that for them because they passed --yes
            # is not a default, it is a decision.
            self._auto_answer(f"{question} (skipped — nothing selected)")
            return []
        self._auto_answer(question)
        for number, option in enumerate(options, start=1):
            self.say(f"{number}) {option}")
        while True:
            raw = self._ask_raw("Choose (comma-separated, 'a' for all, empty to skip): ").lower()
            if not raw:
                return []
            if raw in ("a", "all"):
                return list(range(len(options)))
            picked = []
            for part in raw.replace(" ", ",").split(","):
                if not part:
                    continue
                try:
                    index = int(part) - 1
                except ValueError:
                    picked = []
                    break
                if not 0 <= index < len(options) or index in picked:
                    picked = []
                    break
                picked.append(index)
            if picked:
                return sorted(picked)
            self.say(f"Not understood — numbers from 1 to {len(options)}, or Enter to skip.")
