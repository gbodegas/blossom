# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A copy of the family's own state, served for a test pass: every page says "Test copy".

The label stands where "Sample week" stands, in the same markup, from the sign-in page on,
and the two flags are refused together, since one marks synthetic data, the other the
family's own, and a page can't say both. A pinned clock adds the day it is set to, right
after the label or on its own; the real clock adds nothing.
"""

import re

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.settings import SAMPLE_VARIABLE, Settings
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
    environment: dict[str, str], label: str
) -> None:
    """On the real clock: the label alone, and no day."""
    for page in every_kind_of_page(fixture_settings(**environment)):
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
def test_a_pinned_clock_says_its_day_after_the_label_or_alone(flag: str | None) -> None:
    flags = {flag: "1"} if flag else {}
    marks = (f'<p class="sample-label">{LABELS[flag]}</p>' if flag else "") + PINNED_LINE

    for page in every_kind_of_page(fixture_settings(BLOSSOM_TODAY=PINNED, **flags)):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == 1


@pytest.mark.parametrize(
    ("flag", "pinned"),
    [(TEST_COPY, PINNED), (SAMPLE_VARIABLE, PINNED), (None, PINNED), (TEST_COPY, None)],
)
def test_the_sign_in_page_and_the_page_for_a_parent_carry_the_same_marks(
    flag: str | None, pinned: str | None
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

    app = create_app(fixture_settings(**environment))
    with TestClient(app, headers=SAME_ORIGIN, follow_redirects=False) as client:
        sign_in = client.get("/sign-in")
        sign_in_as(client, "her")
        not_hers = client.get("/parent", headers=PAGE_HEADERS)

    assert sign_in.status_code == 200
    assert not_hers.status_code == 403
    for page in (sign_in.text, not_hers.text):
        assert opens_with(page, marks)
        assert page.count("Today is set to") == (1 if pinned else 0)
