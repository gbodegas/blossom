# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The CI step that holds a run's results to its collected cases.

``.github/scripts/reported_cases.py`` is not part of the package, so it is loaded from its
file. Each case writes a small collected listing and a small results file, as pytest would,
and checks the script's exit status and the line it explains itself with.
"""

import importlib.util
import pathlib
from types import ModuleType

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "reported_cases.py"
COUNT = "{} tests collected in 0.01s"
LISTED = ["tests/test_a.py::test_one", "tests/test_a.py::TestB::test_two[a b::c]"]
REPORTED = [
    ("tests.test_a", "test_one", "passed"),
    ("tests.test_a.TestB", "test_two[a b::c]", "skipped"),
]


def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reported_cases", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def listing(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def results(cases: list[tuple[str, str, str]]) -> str:
    made = []
    for classname, name, outcome in cases:
        inner = "" if outcome == "passed" else f'<{outcome} message="made up"/>'
        made.append(
            f'<testcase classname="{classname}" name="{name}" time="0.01">{inner}</testcase>'
        )
    suite = f'<testsuite name="pytest">{"".join(made)}</testsuite>'
    return f'<?xml version="1.0"?><testsuites>{suite}</testsuites>'


WHOLE = listing([*LISTED, "", COUNT.format(2)])

CASES = {
    "every case once, one of them skipped": (WHOLE, results(REPORTED), 0, "exactly once"),
    "a failed case is still a reported one": (
        WHOLE,
        results([REPORTED[0], (*REPORTED[1][:2], "failure")]),
        0,
        "exactly once",
    ),
    "a collected case the results leave out": (
        WHOLE,
        results(REPORTED[:1]),
        1,
        "not in the results",
    ),
    "one case twice and another absent": (
        WHOLE,
        results([REPORTED[0], REPORTED[0]]),
        1,
        "more often than collected",
    ),
    "a case in the results that was not collected": (
        WHOLE,
        results([*REPORTED, ("tests.test_a", "test_three", "passed")]),
        1,
        "more often than collected",
    ),
    "results that name no case": (WHOLE, results([]), 1, "not in the results"),
    "results cut short": (WHOLE, results(REPORTED)[:80], 1, "cannot read"),
    "an empty results file": (WHOLE, "", 1, "cannot read"),
    "no results file": (WHOLE, None, 1, "cannot read"),
    "no listing": (None, results(REPORTED), 1, "cannot read"),
    "an empty listing": ("", results(REPORTED), 1, "exactly one must close the listing"),
    "a listing with a count and no case": (
        listing(["", COUNT.format(2)]),
        results(REPORTED),
        1,
        "no collected case",
    ),
    "a listing without its count": (
        listing(LISTED),
        results(REPORTED),
        1,
        "exactly one must close the listing",
    ),
    "a listing with two counts": (
        listing([*LISTED, COUNT.format(2), COUNT.format(2)]),
        results(REPORTED),
        1,
        "exactly one must close the listing",
    ),
    "a case after the count": (
        listing([LISTED[0], COUNT.format(2), LISTED[1]]),
        results(REPORTED),
        1,
        "not the last line with text",
    ),
    "a count above the cases listed": (
        listing([*LISTED, COUNT.format(3)]),
        results(REPORTED),
        1,
        "the listing states 3",
    ),
    "a line that only looks like a case": (
        listing([*LISTED, "tests/test_b.py::", COUNT.format(2)]),
        results(REPORTED),
        1,
        "line 3 is not a case",
    ),
    "a summary of warnings in the listing": (
        listing(
            [*LISTED, "", "===== warnings summary =====", "tests/test_a.py:3", COUNT.format(2)]
        ),
        results(REPORTED),
        1,
        "line 4 is not a case",
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_the_results_are_held_to_the_collected_cases(
    name: str, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    collected, reported, status, said = CASES[name]
    if collected is not None:
        (tmp_path / "collected.txt").write_text(collected, encoding="utf-8")
    if reported is not None:
        (tmp_path / "junit.xml").write_text(reported, encoding="utf-8")

    answer = script().main([str(tmp_path / "collected.txt"), str(tmp_path / "junit.xml")])

    assert answer == status
    assert said in capsys.readouterr().out


def test_a_wrong_command_line_is_its_own_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert script().main(["only-one"]) == 2
    assert "usage" in capsys.readouterr().out
