"""Tests for the terminal renderer's pure logic.

Rendering itself is not asserted character by character -- that would pin
cosmetics. What is tested is the logic that is easy to get wrong and that
silently degrades the output: citation parsing, colour gating, and the
sparkline's handling of degenerate series.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from show import (  # noqa: E402
    Palette,
    cited_refs,
    highlight_refs,
    should_colorize,
    sparkline,
    truncate,
)


# ------------------------------------------------------------ citation parsing


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("driven by earnings [M3]", {"M3"}),
        ("two sources [A1][A2]", {"A1", "A2"}),
        ("combined [A18, A19]", {"A18", "A19"}),
        # The model mixes a ref with prose inside one bracket.
        ("[M12, +7.80% on 2026-01-09] was the biggest", {"M12"}),
        ("[see A4 and M2 below]", {"A4", "M2"}),
        ("mixed [M1] and [A7, A8]", {"M1", "A7", "A8"}),
        ("no citations here", set()),
        ("a [markdown](https://example.com) link", set()),
    ],
)
def test_citation_forms_are_all_recognised(answer, expected):
    assert cited_refs(answer) == expected


def test_refs_are_not_matched_outside_brackets():
    """Bare 'A1' in prose is not a citation."""
    assert cited_refs("the A1 highway and M2 motorway") == set()


def test_highlighting_leaves_non_citation_brackets_alone():
    colour = Palette(enabled=True)
    plain = Palette(enabled=False)

    assert highlight_refs("see [A1]", plain) == "see [A1]"
    assert "\033[" in highlight_refs("see [A1]", colour)
    # An ordinary markdown link must not be painted.
    assert highlight_refs("a [link](http://x)", colour) == "a [link](http://x)"


# --------------------------------------------------------------------- colour


def test_palette_disabled_emits_no_escape_codes():
    plain = Palette(enabled=False)
    assert plain("down", "-8.00%") == "-8.00%"
    assert plain.tier("hard", "hard") == "hard"


def test_palette_enabled_wraps_and_resets():
    colour = Palette(enabled=True)
    painted = colour("down", "-8.00%")
    assert painted.startswith("\033[")
    assert painted.endswith("\033[0m")
    assert "-8.00%" in painted


def test_unknown_role_is_passed_through_unstyled():
    assert Palette(enabled=True)("no_such_role", "text") == "text"


@pytest.mark.parametrize(
    ("choice", "isatty", "env", "expected"),
    [
        ("always", False, {}, True),
        ("never", True, {}, False),
        ("auto", True, {}, True),
        ("auto", False, {}, False),  # piped to a file
        ("auto", True, {"NO_COLOR": "1"}, False),
        ("auto", True, {"TERM": "dumb"}, False),
    ],
)
def test_colour_gating(choice, isatty, env, expected, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)

    assert should_colorize(choice) is expected


# ------------------------------------------------------------------ sparkline


def test_sparkline_maps_a_rising_series_upward():
    line = sparkline([1, 2, 3, 4, 5, 6, 7, 8], width=8)
    assert line[0] == "▁"
    assert line[-1] == "█"


def test_sparkline_handles_a_flat_series_without_dividing_by_zero():
    line = sparkline([100.0] * 10, width=10)
    assert len(line) == 10
    assert len(set(line)) == 1


def test_sparkline_of_an_empty_series_is_empty():
    assert sparkline([]) == ""


def test_sparkline_downsamples_to_the_requested_width():
    assert len(sparkline(list(range(500)), width=40)) == 40


def test_sparkline_shorter_than_width_is_not_padded():
    assert len(sparkline([1, 2, 3], width=40)) == 3


# ------------------------------------------------------------------ truncate


def test_truncate_collapses_whitespace_and_adds_an_ellipsis():
    assert truncate("a   b\n c", 80) == "a b c"
    assert truncate("x" * 50, 10) == "x" * 9 + "…"


def test_truncate_handles_none():
    assert truncate(None, 10) == ""
