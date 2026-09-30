"""Her updates and the family's checks when a stored row can't be decoded: that
assignment's record is set apart, whole, every page still answers and says so in the
reader's voice, the plan still counts the work, and nothing is written over it."""

import logging
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from html import unescape

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.assignment_status import statuses_for
from blossom.plans import DailyPlan
from blossom.routes.runs import PlanGraphs, plan_graphs
from blossom.routes.student import ASSIGNMENTS_CHANGED, NOT_SAVED, NOT_UNDONE
from tests.support import (
    DETAILS,
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    MISSING_EMAIL,
    NOT_UTF8,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    READING_LOG_ID,
    REPORT,
    SAME_ORIGIN,
    THEIRS,
    UNDO,
    Answer,
    Scripted,
    a_discrepancy,
    a_row,
    accepting,
    action_of,
    as_stored,
    browser,
    card_for,
    family_page,
    fixture_settings,
    fixture_week_plan,
    form_fields,
    hidden,
    human_text,
    lands_on,
    main_of,
    mark,
    page_of,
    report,
    row_for,
    save,
    scripted_graphs,
    signed_in,
    signed_in_household,
    spoil,
    store_of,
    waiting_note,
    week_card,
    whole_form,
    words,
)

YOURS = (
    "Your updates on this assignment can't be read right now. "
    "No update can be saved until that record can be read."
)
HERS_ON_HER_PAGES = (
    "Her updates on this assignment can't be read right now. "
    "No update can be saved until that record can be read."
)
CHECK_ON_HER_PAGES = "A parent's check on this assignment can't be read right now."
HERS_ON_THE_FAMILY_PAGE = (
    "Her updates on this assignment can't be read right now, so they aren't shown."
)
CHECK_ON_THE_FAMILY_PAGE = (
    "The family check on this assignment can't be read right now. "
    "No check can be saved until that record can be read."
)
YOURS_AS_A_CANDIDATE = "Your updates on this assignment can't be read right now."
HERS_AS_A_CANDIDATE = "Her updates on this assignment can't be read right now."
UPDATES_NOTICE = f"Her updates can't be read right now for: {ESSAY_TITLE} (World History)."
CHECKS_NOTICE = f"Family checks can't be read right now for: {ESSAY_TITLE} (World History)."
UNREADABLE_GROUP = "Records that can't be read"

HER_NOTE = "Handed in Tuesday."
THEIR_NOTE = "Teacher has it on paper."
WORDS = "ZEBRA " * 100
"""600 characters, past the limit, with a word to look for in a log."""

UPDATE_DAMAGES = {
    "a note past the limit": ("student_reports", "note", WORDS),
    "a status that is neither": ("student_reports", "status", "finished"),
    "an operation that is neither": ("student_reports", "operation", "erase"),
    "a day that is no day": ("student_reports", "reported_on", "not-a-day"),
    "a moment that is no moment": ("student_reports", "reported_at", "not-a-moment"),
    "text that is not UTF-8": ("student_reports", "note", NOT_UTF8),
}
CHECK_DAMAGES = {
    "a note past the limit": ("family_checks", "note", WORDS),
    "a blank basis": ("family_checks", "basis", "   "),
    "an operation that is neither": ("family_checks", "operation", "erased"),
    "a day that is no day": ("family_checks", "checked_on", "not-a-day"),
    "text that is not UTF-8": ("family_checks", "note", NOT_UTF8),
}
DAMAGES = {
    **{f"update, {name}": spoiled for name, spoiled in UPDATE_DAMAGES.items()},
    **{f"check, {name}": spoiled for name, spoiled in CHECK_DAMAGES.items()},
}


def looked_for(value: str) -> str:
    """The words of a damage a log must never hold."""
    return "ZEBRA" if value in (WORDS, NOT_UTF8) else value


def seeded(client: TestClient) -> None:
    """The school says the essay is missing, she reports it done with a note, and a parent
    marks it checked with a note."""
    a_discrepancy(client)
    assert mark(client, ESSAY_ID, THEIR_NOTE).status_code == 303


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record as a handler would write it, the traceback included."""
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


# ------------------------------------------------------------- the store's readings


@pytest.mark.parametrize("spoiled", UPDATE_DAMAGES.values(), ids=UPDATE_DAMAGES.keys())
def test_an_update_row_that_cannot_be_decoded_sets_its_assignment_apart_and_reads_the_rest(
    spoiled: tuple[str, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with browser() as client:
        seeded(client)
        report(client, QUIZ_ID, "not_yet", "Two pages left.")
        store = store_of(client)
        before = spoil(store, *spoiled)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            read = store.update_readings([ESSAY_ID, QUIZ_ID, READING_LOG_ID])
        after = as_stored(store, "student_reports")

    assert read.unreadable == {ESSAY_ID}
    assert ESSAY_ID not in read.chains
    assert [event.note for event in read.chains[QUIZ_ID]] == ["Two pages left."]
    assert READING_LOG_ID not in read.chains
    assert after == before
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert ESSAY_ID in record.getMessage()
    assert looked_for(spoiled[2]) not in logged(caplog)
    assert HER_NOTE not in logged(caplog)


@pytest.mark.parametrize("spoiled", CHECK_DAMAGES.values(), ids=CHECK_DAMAGES.keys())
def test_a_check_row_that_cannot_be_decoded_sets_its_assignment_apart_and_reads_the_rest(
    spoiled: tuple[str, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with browser() as client:
        seeded(client)
        store = store_of(client)
        before = spoil(store, *spoiled)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            read = store.check_readings([ESSAY_ID, QUIZ_ID])
        after = as_stored(store, "family_checks")

    assert read.unreadable == {ESSAY_ID}
    assert read.chains == {}
    assert after == before
    [record] = caplog.records
    assert record.exc_info is None
    assert ESSAY_ID in record.getMessage()
    assert looked_for(spoiled[2]) not in logged(caplog)
    assert THEIR_NOTE not in logged(caplog)


def test_a_hand_in_row_whose_text_is_not_utf8_sets_only_its_assignment_apart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with browser() as client:
        store = store_of(client)
        for name in (ESSAY_ID, QUIZ_ID):
            store.record_hand_in(
                name,
                "needs_hand_in",
                None,
                "In my folder.",
                expected_head=None,
                now=store._clock.now(),
                today=store._clock.today(),
            )
        before = spoil(store, "hand_in_events", "note", NOT_UTF8)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            read = store.hand_in_readings([ESSAY_ID, QUIZ_ID])
        after = as_stored(store, "hand_in_events")

    assert read.unreadable == {ESSAY_ID}
    assert read.readable[QUIZ_ID].state == "needs_hand_in"
    assert after == before
    assert "ZEBRA" not in logged(caplog)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("table", ["student_reports", "family_checks"])
def test_a_note_of_exactly_500_characters_reads_and_one_more_is_set_apart(table: str) -> None:
    with browser() as client:
        seeded(client)
        store = store_of(client)
        readings = store.update_readings if table == "student_reports" else store.check_readings
        spoil(store, table, "note", "x" * 500)
        whole = readings([ESSAY_ID])
        spoil(store, table, "note", "x" * 501)
        past = readings([ESSAY_ID])

    assert whole.unreadable == frozenset()
    assert [event.note for event in whole.chains[ESSAY_ID]] == ["x" * 500]
    assert past.unreadable == {ESSAY_ID}


@pytest.mark.parametrize(
    ("table", "reader"),
    [
        ("student_reports", "update_readings"),
        ("family_checks", "check_readings"),
        ("hand_in_events", "hand_in_readings"),
    ],
)
def test_a_read_the_file_refuses_is_raised_and_the_connection_reads_text_as_before(
    table: str, reader: str
) -> None:
    with browser() as client:
        store = store_of(client)
        read = getattr(store, reader)
        kept = store._connection.text_factory
        read([ESSAY_ID])
        after_a_read = store._connection.text_factory
        store._connection.execute(f"ALTER TABLE {table} RENAME TO {table}_gone")
        with pytest.raises(sqlite3.OperationalError):
            read([ESSAY_ID])
        after_a_refusal = store._connection.text_factory
        nothing = store._connection.execute("SELECT 'text'").fetchone()[0]

    assert after_a_read is kept
    assert after_a_refusal is kept
    assert nothing == "text"


def test_naming_no_assignment_reads_nothing() -> None:
    with browser() as client:
        store = store_of(client)
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        updates, checks = store.update_readings([]), store.check_readings([])
        store._connection.set_trace_callback(None)

    assert (updates.chains, updates.unreadable) == ({}, frozenset())
    assert (checks.chains, checks.unreadable) == ({}, frozenset())
    assert seen == []


# ------------------------------------------------------------- what stands


@pytest.mark.parametrize("spoiled", UPDATE_DAMAGES.values(), ids=UPDATE_DAMAGES.keys())
def test_updates_that_cannot_be_read_stand_as_nothing_known_and_stay_work_to_plan(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        report(client, QUIZ_ID, "done")
        store = store_of(client)
        spoil(store, *spoiled)
        statuses = statuses_for(store, [ESSAY_ID, QUIZ_ID])

    essay, quiz = statuses[ESSAY_ID], statuses[QUIZ_ID]
    assert essay.updates_unavailable
    assert not essay.checks_unavailable
    assert (essay.head, essay.asserted, essay.history) == (None, None, ())
    assert essay.work_state == "unreported"
    assert essay.needs_homework
    assert essay.check_basis is None
    assert not essay.needs_a_check
    assert essay.school_says_missing
    assert not quiz.updates_unavailable
    assert quiz.status == "done"


@pytest.mark.parametrize("spoiled", CHECK_DAMAGES.values(), ids=CHECK_DAMAGES.keys())
def test_checks_that_cannot_be_read_leave_her_done_and_its_planning_as_they_were(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        store = store_of(client)
        spoil(store, *spoiled)
        essay = statuses_for(store, [ESSAY_ID])[ESSAY_ID]

    assert essay.checks_unavailable
    assert not essay.updates_unavailable
    assert essay.status == "done"
    assert essay.note == HER_NOTE
    assert not essay.needs_homework
    assert essay.checks == ()
    assert essay.check is None
    assert essay.check_the_school_record
    assert essay.needs_a_check


def test_both_records_of_one_assignment_can_be_unreadable_and_another_reads_as_usual() -> None:
    with browser() as client:
        seeded(client)
        report(client, QUIZ_ID, "not_yet", "Two pages left.")
        store = store_of(client)
        spoil(store, "student_reports", "note", WORDS)
        spoil(store, "family_checks", "note", NOT_UTF8)
        statuses = statuses_for(store, [ESSAY_ID, QUIZ_ID])
        week = page_of(client, week=FIXTURE_WEEK)
        family = family_page(client)

    assert statuses[ESSAY_ID].updates_unavailable
    assert statuses[ESSAY_ID].checks_unavailable
    assert not statuses[QUIZ_ID].updates_unavailable
    assert statuses[QUIZ_ID].note == "Two pages left."
    essay_card, quiz_card = words(card_for(week, ESSAY_ID)), words(card_for(week, QUIZ_ID))
    assert YOURS in essay_card
    assert CHECK_ON_HER_PAGES in essay_card
    assert "Two pages left." in quiz_card
    assert "can't be read" not in quiz_card
    essay_row = words(row_for(family, ESSAY_ID))
    assert HERS_ON_THE_FAMILY_PAGE in essay_row
    assert CHECK_ON_THE_FAMILY_PAGE in essay_row
    assert UPDATES_NOTICE in words(family)
    assert CHECKS_NOTICE in words(family)
    assert "Two pages left." in words(row_for(family, QUIZ_ID))


# ------------------------------------------------------------- every page still answers

PAGES = {
    HER_PAGE: 200,
    f"{HER_PAGE}?week={FIXTURE_WEEK}": 200,
    f"{HER_PAGE}?week=2026-08-24": 200,
    f"{HER_PAGE}?week=not-a-day": 422,
    f"{HER_PAGE}?show_plan=1": 200,
    f"{HER_PAGE}?week={FIXTURE_WEEK}&change={ESSAY_ID}": 200,
    DETAILS: 200,
    f"{DETAILS}?hand_in=change": 200,
    "/student/to-turn-in": 200,
    "/student/plans/today": 200,
}
FAMILY_PAGES = {"/parent": 200, "/parent/approvals": 200}
NOTE_PAGES = ("/student/homework-notes/{note}/search?q=canal", "/student/homework-notes/{note}/add")
FAMILY_NOTE_PAGES = (
    "/parent/homework-notes/{note}/search?q=canal",
    "/parent/homework-notes/{note}/add",
)


def graphs(planners: list[Scripted[DailyPlan]] | None = None) -> Callable[..., PlanGraphs]:
    return scripted_graphs(
        lambda: [fixture_week_plan()] * 3, lambda: [accepting()] * 3, planners=planners
    )


def with_a_plan(client: TestClient) -> str:
    """Today's plan, saved before anything is said; the address of its family review."""
    client.app.dependency_overrides[plan_graphs] = graphs()  # type: ignore[attr-defined]
    assert client.post("/student/actions/plan", headers=PAGE_HEADERS).status_code == 303
    draft = re.search(r'/parent/actions/decide/([^"]+)"', family_page(client))
    assert draft is not None
    return f"/parent/approvals/{unescape(draft.group(1))}"


@pytest.mark.parametrize("spoiled", DAMAGES.values(), ids=DAMAGES.keys())
def test_every_page_that_read_the_record_still_answers_with_the_sign_in_off(
    spoiled: tuple[str, str, str],
) -> None:
    with browser(key=True) as client:
        approval = with_a_plan(client)
        seeded(client)
        note = waiting_note(
            store_of(client), course="World History", title=ESSAY_TITLE, text="canal essay?"
        )
        spoil(store_of(client), *spoiled)
        pages = {
            **PAGES,
            **FAMILY_PAGES,
            approval: 200,
            **{page.format(note=note): 200 for page in (*NOTE_PAGES, *FAMILY_NOTE_PAGES)},
        }
        answers = {page: client.get(page, headers=PAGE_HEADERS).status_code for page in pages}
        signed = client.get("/sign-in", headers=PAGE_HEADERS)
        landed = client.get(signed.headers["location"], headers=PAGE_HEADERS)

    assert answers == pages
    assert (signed.status_code, landed.status_code) == (303, 200)


@contextmanager
def damaged_household(tmp_path: pathlib.Path, spoiled: tuple[str, str, str]) -> Iterator[str]:
    """The household, seeded with the sign-in off and damaged, on files a signed-in start
    opens next; the id of her waiting note."""
    files = {
        "BLOSSOM_DATABASE_PATH": str(tmp_path / "blossom.sqlite3"),
        "BLOSSOM_CHECKPOINT_PATH": str(tmp_path / "checkpoints.sqlite3"),
        "BLOSSOM_TRACE_PATH": str(tmp_path / "traces.sqlite3"),
    }
    settings = fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **files)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        seeded(client)
        store = store_of(client)
        note = waiting_note(store, course="World History", title=ESSAY_TITLE, text="canal?")
        spoil(store, *spoiled)
    yield note


@pytest.mark.parametrize("who", ["her", "a parent"])
@pytest.mark.parametrize("spoiled", DAMAGES.values(), ids=DAMAGES.keys())
def test_every_page_that_read_the_record_still_answers_whoever_is_signed_in(
    spoiled: tuple[str, str, str], who: str, tmp_path: pathlib.Path
) -> None:
    with damaged_household(tmp_path, spoiled) as note:
        app = create_app(signed_in_household(tmp_path))
        with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
            signed_in(client, HERS if who == "her" else THEIRS)
            pages = {
                **PAGES,
                "/student/plans/today": 404,
                **{page.format(note=note): 200 for page in NOTE_PAGES},
                **(
                    {}
                    if who == "her"
                    else {
                        **FAMILY_PAGES,
                        **{page.format(note=note): 200 for page in FAMILY_NOTE_PAGES},
                    }
                ),
            }
            answers = {page: client.get(page, headers=PAGE_HEADERS).status_code for page in pages}

    assert answers == pages


# ------------------------------------------------------------- the pages say so


@pytest.mark.parametrize("spoiled", UPDATE_DAMAGES.values(), ids=UPDATE_DAMAGES.keys())
def test_her_pages_say_her_updates_cannot_be_read_and_offer_nothing_to_save(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        spoil(store_of(client), *spoiled)
        week = card_for(page_of(client, week=FIXTURE_WEEK), ESSAY_ID)
        asked = card_for(page_of(client, week=FIXTURE_WEEK, change=ESSAY_ID), ESSAY_ID)
        details = main_of(client.get(DETAILS, headers=PAGE_HEADERS).text)

    for shown in (week, asked, details):
        assert YOURS in words(shown)
        assert REPORT not in shown
        assert UNDO not in shown
        assert "Change<" not in shown
        assert "No update from" not in words(shown)
        assert "No student update" not in words(shown)
        assert "update: Not yet" not in words(shown)
        assert HER_NOTE not in shown
        assert "ZEBRA" not in shown


@pytest.mark.parametrize("spoiled", CHECK_DAMAGES.values(), ids=CHECK_DAMAGES.keys())
def test_her_pages_keep_her_done_and_say_the_check_cannot_be_read(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        spoil(store_of(client), *spoiled)
        week = card_for(page_of(client, week=FIXTURE_WEEK), ESSAY_ID)
        details = main_of(client.get(DETAILS, headers=PAGE_HEADERS).text)

    for shown in (week, details):
        assert CHECK_ON_HER_PAGES in words(shown)
        assert "Your update: Done" in words(shown)
        assert HER_NOTE in words(shown)
        assert "A parent marked this checked" not in words(shown)
        assert THEIR_NOTE not in shown
        assert "ZEBRA" not in shown


def test_a_parent_signed_in_reads_about_her_on_her_pages_and_the_candidates(
    tmp_path: pathlib.Path,
) -> None:
    with damaged_household(tmp_path, UPDATE_DAMAGES["a note past the limit"]) as note:
        app = create_app(signed_in_household(tmp_path))
        with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
            signed_in(client, THEIRS)
            week = card_for(page_of(client, week=FIXTURE_WEEK), ESSAY_ID)
            details = main_of(client.get(DETAILS, headers=PAGE_HEADERS).text)
            add = main_of(
                client.get(f"/parent/homework-notes/{note}/add", headers=PAGE_HEADERS).text
            )

    for shown in (week, details):
        assert HERS_ON_HER_PAGES in words(shown)
        assert "Your updates" not in words(shown)
    assert HERS_AS_A_CANDIDATE in words(add)
    assert YOURS_AS_A_CANDIDATE not in words(add)


@pytest.mark.parametrize("spoiled", UPDATE_DAMAGES.values(), ids=UPDATE_DAMAGES.keys())
def test_the_family_page_names_her_unreadable_updates_and_links_to_the_row(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        spoil(store_of(client), *spoiled)
        page = family_page(client)

    row = row_for(page, ESSAY_ID)
    notice = re.search(r"<p class=\"problem\">Her updates can.*?</p>", page, re.S)
    assert notice is not None
    assert words(notice.group(0)) == UPDATES_NOTICE
    link = re.search(r'href="([^"]+)"', notice.group(0))
    assert link is not None
    assert f'id="update-{ESSAY_ID}"' in lands_on(page, f"/parent{unescape(link.group(1))}")
    assert HERS_ON_THE_FAMILY_PAGE in words(row)
    assert "She reported" not in words(row)
    assert "the school reports it missing" in words(row).lower()
    assert "Mark checked" not in row
    assert "ZEBRA" not in page
    assert HER_NOTE not in page


@pytest.mark.parametrize("spoiled", CHECK_DAMAGES.values(), ids=CHECK_DAMAGES.keys())
def test_a_discrepancy_whose_check_cannot_be_read_stays_worth_checking_with_no_check_controls(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        spoil(store_of(client), *spoiled)
        page = family_page(client)

    row = row_for(page, ESSAY_ID)
    assert page.index("Worth checking together") < page.index(f'id="update-{ESSAY_ID}"')
    assert CHECKS_NOTICE in words(page)
    assert CHECK_ON_THE_FAMILY_PAGE in words(row)
    assert "She reported it done" in words(row)
    assert "Check the school record." in words(row)
    assert "/mark" not in row
    assert "/again" not in row
    assert "Mark checked" not in row
    assert "Check again" not in row
    assert THEIR_NOTE not in page
    assert "ZEBRA" not in page


def test_a_row_whose_updates_cannot_be_read_and_fit_no_group_is_listed_on_its_own() -> None:
    with browser() as client:
        report(client, QUIZ_ID, "not_yet", "Two pages left.")
        store = store_of(client)
        spoil(store, "student_reports", "note", WORDS, QUIZ_ID)
        page = family_page(client)
        [quiz] = [item for item in store.all_assignments() if item.assignment_id == QUIZ_ID]

    assert page.index(UNREADABLE_GROUP) < page.index(f'id="update-{QUIZ_ID}"')
    assert HERS_ON_THE_FAMILY_PAGE in words(row_for(page, QUIZ_ID))
    assert f"Her updates can't be read right now for: {quiz.title} ({quiz.course})." in words(page)
    assert "Recent updates" not in page


# ------------------------------------------------------------- nothing is written over it


@pytest.mark.parametrize("spoiled", UPDATE_DAMAGES.values(), ids=UPDATE_DAMAGES.keys())
def test_her_save_and_undo_over_unreadable_updates_write_nothing_and_keep_her_words(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        seeded(client)
        card = week_card(client)
        undo = form_fields(card_for(page_of(client, week=FIXTURE_WEEK), ESSAY_ID), UNDO)
        store = store_of(client)
        before = spoil(store, *spoiled)
        saved = save(client, card, "not_yet", "Still drafting.")
        undone = client.post(UNDO, data=undo, headers=PAGE_HEADERS)
        after = as_stored(store, "student_reports")

    assert (saved.status_code, undone.status_code) == (500, 500)
    assert after == before
    kept = card_for(saved.text, ESSAY_ID)
    assert NOT_SAVED in words(saved.text)
    assert YOURS in words(kept)
    assert "Your unsaved update" in words(kept)
    assert "Not yet" in words(kept)
    assert "Still drafting." in kept
    assert NOT_UNDONE in words(undone.text)
    assert YOURS in words(card_for(undone.text, ESSAY_ID))


def test_a_first_update_that_cannot_be_read_refuses_a_save_on_the_latest() -> None:
    with browser() as client:
        report(client, ESSAY_ID, "not_yet", "Half left.")
        report(client, ESSAY_ID, "done", HER_NOTE)
        store = store_of(client)
        first = store.student_reports(ESSAY_ID)[0].report_id
        store._connection.execute(
            "UPDATE student_reports SET note = ? WHERE report_id = ?", (WORDS, first)
        )
        store._connection.commit()
        before = as_stored(store, "student_reports")
        head = store._head_locked(ESSAY_ID)
        assert head is not None
        refused = client.post(
            REPORT,
            data={
                "status": "not_yet",
                "note": "",
                "expected_report_id": head.report_id,
                "week": FIXTURE_WEEK,
            },
            headers=PAGE_HEADERS,
        )
        after = as_stored(store, "student_reports")

    assert refused.status_code == 500
    assert after == before


@pytest.mark.parametrize("spoiled", CHECK_DAMAGES.values(), ids=CHECK_DAMAGES.keys())
def test_a_check_marked_or_reopened_over_unreadable_checks_writes_nothing(
    spoiled: tuple[str, str, str],
) -> None:
    with browser() as client:
        a_discrepancy(client)
        row = row_for(family_page(client), ESSAY_ID)
        mark_at = action_of(row, "mark")
        marking = {"basis": hidden(row, "basis"), "expected_check_id": "", "note": "Again."}
        assert mark(client, ESSAY_ID, THEIR_NOTE).status_code == 303
        checked = row_for(family_page(client), ESSAY_ID)
        again_at = action_of(checked, "again")
        again = {"check_id": hidden(checked, "check_id"), "basis": hidden(checked, "basis")}
        store = store_of(client)
        before = spoil(store, *spoiled)
        marked = client.post(mark_at, data=marking, headers=PAGE_HEADERS)
        reopened = client.post(again_at, data=again, headers=PAGE_HEADERS)
        after = as_stored(store, "family_checks")

    assert (marked.status_code, reopened.status_code) == (500, 500)
    assert after == before
    for answer in (marked, reopened):
        assert CHECK_ON_THE_FAMILY_PAGE in words(answer.text)


def test_a_refused_save_refused_again_then_repaired_saves_once() -> None:
    with browser() as client:
        report(client, ESSAY_ID, "done", HER_NOTE)
        card = week_card(client)
        store = store_of(client)
        spoil(store, "student_reports", "note", WORDS)
        first = save(client, card, "not_yet", "Still drafting.")
        second = save(client, card, "not_yet", "Still drafting.")
        refused = as_stored(store, "student_reports")
        spoil(store, "student_reports", "note", HER_NOTE)
        third = save(client, card, "not_yet", "Still drafting.")
        landed = client.get(third.headers["location"], headers=PAGE_HEADERS)
        fourth = save(client, card, "not_yet", "Still drafting.")
        chain = store.student_reports(ESSAY_ID)

    assert (first.status_code, second.status_code) == (500, 500)
    assert len(refused) == 1
    assert third.status_code == 303
    assert landed.status_code == 200
    assert fourth.status_code == 303
    assert [(event.status, event.note) for event in chain] == [
        ("done", HER_NOTE),
        ("not_yet", "Still drafting."),
    ]


def test_a_save_beside_unreadable_updates_lands_on_a_page_that_answers_and_never_twice() -> None:
    with browser() as client:
        seeded(client)
        quiz = week_card(client, QUIZ_ID)
        opened = client.get(DETAILS, params={"hand_in": "change"}, headers=PAGE_HEADERS).text
        hand_in_at = f"/student/actions/assignments/{ESSAY_ID}/hand-in"
        handing = {
            **form_fields(opened, hand_in_at),
            "state": "needs_hand_in",
            "next_action": "",
            "note": "",
        }
        help_form = form_fields(page_of(client), "/student/actions/ask-for-help")
        store = store_of(client)
        spoil(store, "student_reports", "note", WORDS)
        presses: list[tuple[str, dict[str, str]]] = [
            (
                f"/student/actions/assignments/{QUIZ_ID}/report",
                {
                    **form_fields(quiz, f"/student/actions/assignments/{QUIZ_ID}/report"),
                    "status": "done",
                    "note": "",
                },
            ),
            (hand_in_at, handing),
            ("/student/actions/ask-for-help", {**help_form, "note": "Stuck on the essay."}),
            ("/parent/inbox/keep", {"text": MISSING_EMAIL}),
        ]
        answers: list[tuple[Answer, Answer, Answer]] = []
        for action, form in presses:
            sent = client.post(action, data=form, headers=PAGE_HEADERS)
            again = client.post(action, data=form, headers=PAGE_HEADERS)
            answers.append((sent, again, client.get(sent.headers["location"])))
        quiz_chain = store.student_reports(QUIZ_ID)
        hand_ins = store.hand_in_chains([ESSAY_ID])[ESSAY_ID]

    for first, repeated, landed in answers:
        assert first.status_code == 303, first.text[:300]
        assert repeated.status_code == 303, repeated.text[:300]
        assert landed.status_code == 200
    assert [event.status for event in quiz_chain] == ["done"]
    assert len(hand_ins) == 1


# ------------------------------------------------------------- homework that is already here


def test_the_essay_stays_a_choice_beside_updates_that_cannot_be_read_and_can_be_joined() -> None:
    with browser() as client:
        seeded(client)
        store = store_of(client)
        note = waiting_note(store, course="World History", title=ESSAY_TITLE, text="canal?")
        spoil(store, "student_reports", "note", WORDS)
        add = client.get(f"/student/homework-notes/{note}/add", headers=PAGE_HEADERS).text
        search = client.get(
            f"/student/homework-notes/{note}/search", params={"q": "canal"}, headers=PAGE_HEADERS
        ).text
        form = whole_form(add, f"/student/actions/homework-notes/{note}/add")
        joined = client.post(
            f"/student/actions/homework-notes/{note}/add",
            data={**form, "candidate": f"same:{ESSAY_ID}"},
            headers=PAGE_HEADERS,
        )
        kept = store.capture(note)

    assert f'value="same:{ESSAY_ID}"' in add
    assert YOURS_AS_A_CANDIDATE in words(add)
    assert "No update from you" not in words(add)
    assert YOURS_AS_A_CANDIDATE in words(search)
    assert joined.status_code == 303, joined.text[:300]
    assert kept is not None
    assert kept.assignment_id == ESSAY_ID


@pytest.mark.parametrize(
    "direction", ["read, then damaged", "damaged, then repaired", "nothing said, then damaged"]
)
def test_a_choice_made_before_updates_became_unreadable_or_readable_is_asked_again(
    direction: str,
) -> None:
    with browser() as client:
        if direction != "nothing said, then damaged":
            seeded(client)
        store = store_of(client)
        note = waiting_note(store, course="World History", title=ESSAY_TITLE, text="canal?")
        if direction == "damaged, then repaired":
            spoil(store, "student_reports", "note", WORDS)
        add = client.get(f"/student/homework-notes/{note}/add", headers=PAGE_HEADERS).text
        form = whole_form(add, f"/student/actions/homework-notes/{note}/add")
        if direction == "nothing said, then damaged":
            report(client, ESSAY_ID, "done", HER_NOTE)
        spoil(
            store,
            "student_reports",
            "note",
            HER_NOTE if direction == "damaged, then repaired" else WORDS,
        )
        asked = client.post(
            f"/student/actions/homework-notes/{note}/add",
            data={**form, "candidate": f"same:{ESSAY_ID}"},
            headers=PAGE_HEADERS,
        )
        kept = store.capture(note)

    assert asked.status_code == 409, asked.text[:300]
    assert kept is not None
    assert kept.assignment_id is None


def test_a_school_report_that_cannot_be_read_is_never_taken_for_no_homework_here() -> None:
    with browser() as client:
        seeded(client)
        store = store_of(client)
        note = waiting_note(store, course="World History", title=ESSAY_TITLE, text="canal?")
        spoil(store, "status_reports", "reported_on", "not-a-day")
        with pytest.raises(ValueError, match="isoformat"):
            client.get(f"/student/homework-notes/{note}/add", headers=PAGE_HEADERS)


# ------------------------------------------------------------- plans


@pytest.mark.parametrize(
    ("press", "made"),
    [
        (("POST", "/student/actions/plan", None), 303),
        (("POST", "/parent/actions/plan", None), 303),
        (("POST", "/student/plans", None), 201),
        (("POST", "/parent/plans", {"plan_date": PLAN_DATE.isoformat()}), 201),
    ],
    ids=["her page", "the family page", "her JSON route", "the family JSON route"],
)
def test_a_plan_counts_work_whose_updates_cannot_be_read_and_is_told_none_of_their_words(
    press: tuple[str, str, dict[str, str] | None], made: int
) -> None:
    planners: list[Scripted[DailyPlan]] = []
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = graphs(planners)  # type: ignore[attr-defined]
        seeded(client)
        spoil(store_of(client), "student_reports", "note", WORDS)
        method, path, body = press
        answer = client.request(method, path, json=body, headers=PAGE_HEADERS)

    assert answer.status_code == made, answer.text[:300]
    brief = human_text(planners[0].briefs[0])
    assert ESSAY_TITLE in brief
    assert "ZEBRA" not in brief
    assert HER_NOTE not in brief


def test_a_plan_made_while_she_reported_done_fits_again_once_her_updates_are_repaired() -> None:
    whole = fixture_week_plan()
    without_the_essay = DailyPlan(
        plan_date=PLAN_DATE, blocks=whole.blocks[1:], deferred=whole.deferred
    )
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [without_the_essay], lambda: [accepting()]
        )
        report(client, ESSAY_ID, "done", HER_NOTE)
        assert client.post("/student/actions/plan", headers=PAGE_HEADERS).status_code == 303
        fits = words(page_of(client))
        spoil(store_of(client), "student_reports", "note", WORDS)
        unreadable = words(page_of(client))
        spoil(store_of(client), "student_reports", "note", HER_NOTE)
        repaired = words(page_of(client))

    assert ASSIGNMENTS_CHANGED not in fits
    assert ASSIGNMENTS_CHANGED in unreadable
    assert YOURS in unreadable
    assert ASSIGNMENTS_CHANGED not in repaired


def test_a_recent_row_whose_checks_cannot_be_read_is_unfolded_for_the_link_to_it() -> None:
    with browser() as client:
        seeded(client)
        report(client, ESSAY_ID, "not_yet", "More to do.")
        spoil(store_of(client), "family_checks", "note", WORDS)
        page = family_page(client)

    row = page.index(f'id="update-{ESSAY_ID}"')
    fold = page.rfind("<details", 0, row)
    assert page.rfind("Recent updates", 0, row) > fold
    assert page[fold : page.index(">", fold) + 1] == '<details class="steps" open>'
    assert CHECKS_NOTICE in words(page)
    assert CHECK_ON_THE_FAMILY_PAGE in words(row_for(page, ESSAY_ID))


def test_the_link_to_a_row_reaches_an_id_that_holds_a_slash_a_space_and_a_hash() -> None:
    name = "unit/3 part?b#c"
    with browser() as client:
        store = store_of(client)
        store.put_on_record([a_row(name, "Slashed set")], {}, {})
        store.report_status(
            name,
            "done",
            None,
            expected_head=None,
            now=store._clock.now(),
            today=store._clock.today(),
        )
        spoil(store, "student_reports", "note", WORDS, name)
        page = family_page(client)

    notice = re.search(r"<p class=\"problem\">Her updates can.*?</p>", page, re.S)
    assert notice is not None
    link = re.search(r'href="([^"]+)"', notice.group(0))
    assert link is not None
    assert link.group(1) == "#update-unit%2F3%20part%3Fb%23c"
    assert f'id="update-{name}"' in lands_on(page, f"/parent{link.group(1)}")
    assert page.index(UNREADABLE_GROUP) < page.index(f'id="update-{name}"')


def test_a_first_check_that_cannot_be_read_refuses_a_mark_after_the_latest() -> None:
    with browser() as client:
        a_discrepancy(client)
        assert mark(client, ESSAY_ID, THEIR_NOTE).status_code == 303
        checked = row_for(family_page(client), ESSAY_ID)
        reopened = client.post(
            action_of(checked, "again"),
            data={"check_id": hidden(checked, "check_id"), "basis": hidden(checked, "basis")},
            headers=PAGE_HEADERS,
        )
        assert reopened.status_code == 303
        row = row_for(family_page(client), ESSAY_ID)
        marking = {
            "basis": hidden(row, "basis"),
            "expected_check_id": hidden(row, "expected_check_id"),
            "note": "Seen again.",
        }
        store = store_of(client)
        first = store.family_checks(ESSAY_ID)[0].check_id
        store._connection.execute(
            "UPDATE family_checks SET note = ? WHERE check_id = ?", (WORDS, first)
        )
        store._connection.commit()
        before = as_stored(store, "family_checks")
        marked = client.post(action_of(row, "mark"), data=marking, headers=PAGE_HEADERS)
        after = as_stored(store, "family_checks")

    assert marked.status_code == 500
    assert after == before
    assert CHECK_ON_THE_FAMILY_PAGE in words(row_for(marked.text, ESSAY_ID))
