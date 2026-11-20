# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""How a screenshot review's work grows with its cards, on the page with every question and box.

A group whose date, kind or instructions questions are checked under each answer they offer is
worked out over its own pieces and those its checks against other cards read, and the saved
assignments are read once for each working out of the review a page makes, so the page's work grows
with its cards, not with their square. The tests count, so they depend on nothing but the code and
no clock: the first, the workings out of the review and the record's reads of every assignment over
the whole of the worst page, built and pressed; the second, the pieces each answer preview works
out. A save's own working out, inside its transaction, reads as it reads
(``tests/test_screenshot_save.py`` counts its statements).
"""

import inspect
import pathlib
from collections.abc import Callable, Sequence
from typing import Any

import pytest

import tests.import_review_cases as cases
from blossom import screenshot_save
from blossom.routes import imports
from blossom.screenshot_review import Piece, Validation, validate
from blossom.screenshot_save import ReviewState
from blossom.stores.project_state import Assignment, ProjectStateStore


def reads_while_worked_out(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, cards: int
) -> tuple[int, int]:
    """While the worst page of one screenshot with this many cards is built and pressed: how many
    times a page works out the review (``review_state`` as the routes call it), and how many times
    the record reads every assignment during those workings out."""
    worked = [0]
    reads = [0]
    inside = [False]
    read: Callable[[ProjectStateStore], list[Assignment]] = ProjectStateStore.all_assignments
    work_out = screenshot_save.review_state

    def counted(store: ProjectStateStore) -> list[Assignment]:
        if inside[0]:
            reads[0] += 1
        return read(store)

    def working_out(*args: Any, **kwargs: Any) -> ReviewState:  # noqa: ANN401
        worked[0] += 1
        inside[0] = True
        try:
            return work_out(*args, **kwargs)
        finally:
            inside[0] = False

    folder = tmp_path / str(cards)
    folder.mkdir()
    with cases.here(cases.open_settings(folder)) as browser, monkeypatch.context() as patched:
        patched.setattr(ProjectStateStore, "all_assignments", counted)
        patched.setattr(imports, "review_state", working_out)
        cases.the_worst_page(browser, monkeypatch, 1, cards)
    return worked[0], reads[0]


def test_each_working_out_of_the_review_a_page_makes_reads_every_assignment_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counts the workings out of the review and the record's reads of every assignment over the
    whole of the worst page, built and pressed (``the_worst_page``), and asserts one read for
    each working out, the same counts at 10 cards and at 20."""
    ten = reads_while_worked_out(tmp_path, monkeypatch, 10)
    twenty = reads_while_worked_out(tmp_path, monkeypatch, 20)
    said = f"(workings out, reads): 10 cards {ten}; 20 cards {twenty}"
    assert ten[0] > 0, said
    assert ten[1] == ten[0], said
    assert twenty == ten, said


def pieces_each_answer_preview_works_out(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, cards: int
) -> set[int]:
    """While the worst page of one screenshot with this many cards is built and pressed: each
    number of pieces a preview of a group's answer combination works out, as ``ways`` passes them
    to ``work`` and ``work`` checks them (``validate``)."""
    counts: set[int] = set()
    check = validate

    def counted(pieces: Sequence[Piece], *args: Any, **kwargs: Any) -> Validation:  # noqa: ANN401
        frame = inspect.currentframe()
        work = None if frame is None else frame.f_back
        caller = None if work is None else work.f_back
        if caller is not None and caller.f_code.co_name == "ways":
            counts.add(len(pieces))
        return check(pieces, *args, **kwargs)

    folder = tmp_path / str(cards)
    folder.mkdir()
    with cases.here(cases.open_settings(folder)) as browser, monkeypatch.context() as patched:
        patched.setattr(screenshot_save, "validate", counted)
        cases.the_worst_page(browser, monkeypatch, 1, cards)
    return counts


def test_each_answer_preview_works_out_the_same_pieces_at_10_20_and_60_cards(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counts the pieces each answer preview works out: the same numbers of pieces at 10, 20 and
    60 cards."""
    found = {
        cards: pieces_each_answer_preview_works_out(tmp_path, monkeypatch, cards)
        for cards in (10, 20, 60)
    }
    assert found[10], found
    assert found[10] == found[20] == found[60], found
