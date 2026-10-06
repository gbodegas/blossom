# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A copy of the family's own state, served for a test pass: every page says "Test copy".

The label stands where "Sample week" stands, in the same markup, from the sign-in page on,
and the two flags are refused together, since one marks synthetic data, the other the
family's own, and a page can't say both. A pinned clock adds the day it is set to, right
after the label or on its own; the real clock adds nothing.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    SAMPLE_VARIABLE,
    TRACE_PATH_VARIABLE,
    Settings,
)
from blossom.stores.paths import TEST_COPY_MARKER, CopyMarkError, refuse_mismarked_state
from tests.support import HERS, PAGE_HEADERS, SAME_ORIGIN, THEIRS, fixture_settings, sign_in_as

TEST_COPY = "BLOSSOM_TEST_COPY"
"""The variable a test-copy launch sets, by the name the household's instructions give."""
LABELS = {TEST_COPY: "Test copy", SAMPLE_VARIABLE: "Sample week"}
PINNED = "2026-09-07"
PINNED_LINE = '<p class="note">Today is set to Monday, September 7, 2026.</p>'


def every_kind_of_page(settings: Settings) -> list[str]:
    """Her week, the family review, and the page for a form that could not be read."""
    with TestClient(create_app(settings), headers=SAME_ORIGIN) as client:
        pages = [client.get(path).text for path in ("/student/due-this-week", "/parent")]
        unreadable = client.post(
            "/student/actions/homework-notes",
            content=b"note=Seven blue kites",
            headers={"Content-Type": "multipart/form-data"},
        )

    assert unreadable.status_code == 400
    assert "That form could not be read" in unreadable.text
    return [*pages, unreadable.text]


def state_in(folder: Path, *, marked: bool) -> dict[str, str]:
    """The three state paths in ``folder``, marked as a test copy the way the copy step marks
    it when ``marked``."""
    folder.mkdir(parents=True, exist_ok=True)
    if marked:
        (folder / TEST_COPY_MARKER).write_text("", encoding="utf-8")
    return {
        DATABASE_PATH_VARIABLE: str(folder / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(folder / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(folder / "traces.sqlite3"),
    }


def flagged(environment: dict[str, str], folder: Path) -> dict[str, str]:
    """``environment``, served from a marked copy in ``folder`` when it turns the copy on."""
    if environment.get(TEST_COPY, "").strip().lower() in {"1", "on", "true", "yes"}:
        return {**environment, **state_in(folder, marked=True)}
    return environment


def opens_with(page: str, marks: str) -> bool:
    """Whether ``marks`` come first in the page's main content, before anything of its own."""
    return re.search(r'<main id="main"[^>]*>\s*' + re.escape(marks), page) is not None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("1", True),
        ("true", True),
        ("Yes", True),
        ("on", True),
        ("0", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_the_test_copy_flag_reads_yes_and_no(given: str, expected: bool) -> None:
    assert fixture_settings(BLOSSOM_TEST_COPY=given).test_copy is expected


def test_the_test_copy_flag_refuses_anything_else_by_name() -> None:
    with pytest.raises(ValueError, match=TEST_COPY):
        fixture_settings(BLOSSOM_TEST_COPY="maybe")


@pytest.mark.parametrize("sample", ["1", "yes"])
@pytest.mark.parametrize("test_copy", ["1", "on"])
def test_the_sample_and_the_test_copy_are_refused_together_by_name(
    sample: str, test_copy: str
) -> None:
    with pytest.raises(ValueError, match=SAMPLE_VARIABLE) as refused:
        fixture_settings(BLOSSOM_SAMPLE=sample, BLOSSOM_TEST_COPY=test_copy)

    assert TEST_COPY in str(refused.value)


@pytest.mark.parametrize(
    ("environment", "label"),
    [
        ({"BLOSSOM_TEST_COPY": "1"}, "Test copy"),
        ({"BLOSSOM_TEST_COPY": "on", "BLOSSOM_SAMPLE": "no"}, "Test copy"),
        ({"BLOSSOM_SAMPLE": "1"}, "Sample week"),
        ({"BLOSSOM_SAMPLE": "1", "BLOSSOM_TEST_COPY": "0"}, "Sample week"),
    ],
)
def test_every_page_carries_the_one_label_its_flag_sets(
    environment: dict[str, str], label: str, tmp_path: Path
) -> None:
    """On the real clock: the label alone, and no day."""
    for page in every_kind_of_page(fixture_settings(**flagged(environment, tmp_path))):
        assert opens_with(page, f'<p class="sample-label">{label}</p>\n')
        assert page.count('class="sample-label"') == 1
        assert "Today is set to" not in page


def test_with_neither_flag_and_the_real_clock_no_page_carries_a_mark() -> None:
    for page in every_kind_of_page(fixture_settings()):
        assert 'class="sample-label"' not in page
        assert "Sample week" not in page
        assert "Test copy" not in page
        assert "Today is set to" not in page


@pytest.mark.parametrize("flag", [TEST_COPY, SAMPLE_VARIABLE, None])
def test_a_pinned_clock_says_its_day_after_the_label_or_alone(
    flag: str | None, tmp_path: Path
) -> None:
    flags = flagged({flag: "1"} if flag else {}, tmp_path)
    marks = (f'<p class="sample-label">{LABELS[flag]}</p>' if flag else "") + PINNED_LINE

    for page in every_kind_of_page(fixture_settings(BLOSSOM_TODAY=PINNED, **flags)):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == 1


@pytest.mark.parametrize(
    ("flag", "pinned"),
    [(TEST_COPY, PINNED), (SAMPLE_VARIABLE, PINNED), (None, PINNED), (TEST_COPY, None)],
)
def test_the_sign_in_page_and_the_page_for_a_parent_carry_the_same_marks(
    flag: str | None, pinned: str | None, tmp_path: Path
) -> None:
    """With the sign-in on, the first page anyone sees says what the instance is, and so
    does the page that tells her the family review is a parent's."""
    environment = {"BLOSSOM_STUDENT_PASSPHRASE": HERS, "BLOSSOM_PARENT_PASSPHRASE": THEIRS}
    if flag:
        environment[flag] = "1"
    if pinned:
        environment["BLOSSOM_TODAY"] = pinned
    marks = (f'<p class="sample-label">{LABELS[flag]}</p>' if flag else "") + (
        PINNED_LINE if pinned else ""
    )

    app = create_app(fixture_settings(**flagged(environment, tmp_path)))
    with TestClient(app, headers=SAME_ORIGIN, follow_redirects=False) as client:
        sign_in = client.get("/sign-in")
        sign_in_as(client, "her")
        not_hers = client.get("/parent", headers=PAGE_HEADERS)

    assert sign_in.status_code == 200
    assert not_hers.status_code == 403
    for page in (sign_in.text, not_hers.text):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == (1 if pinned else 0)


def test_a_test_copy_refuses_to_start_on_unmarked_folders_and_touches_nothing(
    tmp_path: Path,
) -> None:
    """A path left to its default or to the household's own setting names an unmarked folder:
    the start is refused before anything beside the state files is claimed or opened."""
    folder = tmp_path / "household"
    settings = fixture_settings(**{TEST_COPY: "1"}, **state_in(folder, marked=False))

    with (
        pytest.raises(CopyMarkError, match="holds no TEST-COPY file"),
        TestClient(create_app(settings), headers=SAME_ORIGIN),
    ):
        pass

    assert list(folder.iterdir()) == []


def test_a_marked_folder_refuses_to_start_without_the_flag(tmp_path: Path) -> None:
    folder = tmp_path / "copy"
    settings = fixture_settings(**state_in(folder, marked=True))

    with (
        pytest.raises(CopyMarkError, match="set BLOSSOM_TEST_COPY=1"),
        TestClient(create_app(settings), headers=SAME_ORIGIN),
    ):
        pass

    assert [path.name for path in folder.iterdir()] == [TEST_COPY_MARKER]


def test_every_state_folder_must_carry_the_marker(tmp_path: Path) -> None:
    """One unmarked folder among the three is enough to refuse a copy."""
    marked, unmarked = tmp_path / "copy", tmp_path / "household"
    state_in(marked, marked=True)
    state_in(unmarked, marked=False)
    paths = (
        marked / "blossom.sqlite3",
        marked / "checkpoints.sqlite3",
        unmarked / "traces.sqlite3",
    )

    with pytest.raises(CopyMarkError, match=re.escape(str(unmarked))):
        refuse_mismarked_state(paths, test_copy=True)
    refuse_mismarked_state(paths[:2], test_copy=True)
    with pytest.raises(CopyMarkError, match=re.escape(str(marked))):
        refuse_mismarked_state(paths, test_copy=False)
    refuse_mismarked_state(paths[2:], test_copy=False)
