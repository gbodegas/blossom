"""Which homework a school row is about, asked where the record cannot tell.

A row with the class and title of homework made from her note asks whether it is
the same homework or different homework with the same title, until a parent has
said. Two or more assignments under one name ask which one, and are never
combined or chosen by the nearest date. An undated report asks for each report
where more than one assignment could be meant. The answers are kept, a retry
changes nothing, and an answer made against facts that changed since writes
nothing. A parent's typed note never replaces the note she gave her homework.
"""

import html
import pathlib
import re
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.intake import ChangedSinceShown, Kept, keep, read_text
from blossom.reconciliation import SourceChannel
from blossom.settings import Settings
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    SAME_ORIGIN,
    fixture_settings,
    homework_from_a_note,
    store_of,
)

HER_NOTE = "Get it signed at dinner."
GUIDE_CARD = (
    "Homework for Wren\n- 09/02/2026 - Wednesday\n"
    "Health - Assigned: Course Guide Due: (Due:09/09/2026)\n"
    "Return the course guide signed by a parent after reading it.\n"
)
GUIDE_MISSING = "Assignments:\n09/14 Health - A: Homework: Course Guide Due Grade: Missing\n"
GUIDE_MISSING_LATER = "Assignments:\n09/21 Health - A: Homework: Course Guide Due Grade: Missing\n"
GUIDE_DUE_CARD = "Homework for Wren\n- 09/09/2026 - Wednesday\nHealth - Due: Course Guide Due:\n"
GUIDE_DUE_LATER = "Homework for Wren\n- 09/16/2026 - Wednesday\nHealth - Due: Course Guide Due:\n"
GUIDE_NEXT_ROUND = (
    "Homework for Wren\n- 09/21/2026 - Monday\n"
    "Health - Assigned: Course Guide Due: (Due:09/30/2026)\n"
)
LAB_LOG = "Lab Log"
DIFFERENT = "different"
"""The answer that the row is different homework with the same title, as the page sends it."""


def settings_in(tmp_path: pathlib.Path, today: str = "2026-09-03") -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY=today,
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


def client_in(tmp_path: pathlib.Path, today: str = "2026-09-03") -> TestClient:
    return TestClient(
        create_app(settings_in(tmp_path, today)), follow_redirects=False, headers=SAME_ORIGIN
    )


def review_form(page: str) -> dict[str, str]:
    """The review form as a browser sends it back untouched: hidden fields, each select's
    chosen option, each ticked box, and the text."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)
    }
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]+)" selected>', body)
        assert chosen is not None, name
        fields[name] = chosen.group(1)
    for name in re.findall(r'<input type="checkbox" name="([^"]+)" value="1" checked>', page):
        fields[name] = "1"
    text = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', page, re.S)
    if text is not None:
        fields["text"] = html.unescape(text.group(1))
    return fields


def question(page: str, key: int) -> str:
    """The identity question on one card, or empty when the card asks none."""
    found = re.search(
        rf'<fieldset class="choice identity" id="identity-question-{key}"[^>]*>(.*?)</fieldset>',
        page,
        re.S,
    )
    return "" if found is None else found.group(1)


def choices(page: str, key: int) -> list[str]:
    """The values the identity question on one card offers, in order."""
    return re.findall(rf'name="identity-{key}" value="([^"]+)"', question(page, key))


def kept_tables(client: TestClient) -> list[str]:
    """The tables a save writes, of those the file has."""
    have = {
        name
        for (name,) in store_of(client)._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    wanted = (
        "assignments",
        "date_claims",
        "status_reports",
        "school_instructions",
        "intake_decisions",
    )
    return [name for name in wanted if name in have]


def tables(client: TestClient) -> dict[str, list[tuple[object, ...]]]:
    connection = store_of(client)._connection
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in kept_tables(client)
    }


def decisions(client: TestClient) -> list[tuple[object, ...]]:
    if "intake_decisions" not in kept_tables(client):
        return []
    return (
        store_of(client)
        ._connection.execute(
            "SELECT kind, course, title, due_date, lands_on, creation FROM intake_decisions "
            "ORDER BY sequence"
        )
        .fetchall()
    )


def read(client: TestClient, text: str) -> str:
    page = client.post("/parent/inbox/read", data={"text": text})
    assert page.status_code == 200, page.text
    return page.text


def hers(client: TestClient, due: date | None = date(2026, 9, 9)) -> str:
    return homework_from_a_note(
        store_of(client), course="Health", title="Course Guide Due", due_date=due, note=HER_NOTE
    )


def school_rows(client: TestClient, *dues: date | None) -> list[str]:
    """Lab Log assignments on record from the school, one for each due date."""
    names = [f"assignment-lab-log-{place}" for place in range(len(dues))]
    store_of(client).put_on_record(
        [
            Assignment(
                assignment_id=name,
                course="Science",
                title=LAB_LOG,
                due_date=due,
                dependencies=[],
                reported_submission_status="unknown",
                kind=AssignmentKind.HOMEWORK,
                origins={"record": SourceChannel.LMS},
            )
            for name, due in zip(names, dues, strict=True)
        ],
        {},
    )
    return names


GUIDE_COPY = "assignment-health-guide-copy"


def copy_of_the_guide(due: date) -> Assignment:
    """Another assignment of the guide's class and title, from the school."""
    return Assignment(
        assignment_id=GUIDE_COPY,
        course="Health",
        title="Course Guide Due",
        due_date=due,
        dependencies=[],
        reported_submission_status="unknown",
        kind=AssignmentKind.HOMEWORK,
        origins={"record": SourceChannel.LMS},
    )


def lab_card(due: str) -> str:
    return f"Homework for Wren\n- 09/28/2026 - Monday\nScience - Assigned: {LAB_LOG}: (Due:{due})\n"


LAB_MISSING = f"Assignments:\n09/30 Science - A: Homework: {LAB_LOG} Grade: Missing\n"


# ------------------------------------------------------------------ her homework asks first


def test_a_row_about_her_homework_asks_same_or_different_and_writes_nothing_unanswered(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_CARD)
        before = tables(client)
        unanswered = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    asked = question(page, 0)
    assert choices(page, 0) == [mine, DIFFERENT]
    assert "Is this the same homework?" in asked
    assert "This has the class and title of homework from her note." in html.unescape(page)
    assert "The school's dates, reports, and instructions go to her assignment." in html.unescape(
        asked
    )
    assert "The school's is saved as its own assignment." in html.unescape(asked)
    assert HER_NOTE in html.unescape(asked)
    assert "About homework she added from a note" not in page
    assert unanswered.status_code == 200
    assert choices(unanswered.text, 0) == [mine, DIFFERENT]
    assert after == before


def test_an_undated_report_about_her_homework_alone_asks_first(tmp_path: pathlib.Path) -> None:
    """A Missing email names her homework and nothing else of that name. One candidate does
    not settle it: the card asks whether it is the same homework, and nothing is written
    until a parent says."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_MISSING)
        before = tables(client)
        unanswered = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    assert choices(page, 0) == [mine, DIFFERENT]
    assert "Is this the same homework?" in question(page, 0)
    assert unanswered.status_code == 200
    assert after == before


def test_same_homework_puts_the_school_facts_on_hers_and_a_later_report_needs_no_question(
    tmp_path: pathlib.Path,
) -> None:
    """Same settles identity only: her due date and note stay, the school's date is a claim
    beside hers, its instruction is kept as the school's, and after a restart a Missing
    email lands on hers without a question, dated by the day it was pasted."""
    with client_in(tmp_path) as client:
        mine = hers(client, due=date(2026, 9, 10))
        page = read(client, GUIDE_CARD)
        saved = client.post("/parent/inbox/keep", data={**review_form(page), "identity-0": mine})
        store = store_of(client)
        row = store.one_assignment(mine)
        kept = store.school_instruction_readings([mine]).readable[mine]
        claims = [claim.asserted_value for claim in store.deadline_records(mine)]
        again = client.post("/parent/inbox/keep", data={**review_form(page), "identity-0": mine})
        first = decisions(client)
        rows = [item.assignment_id for item in store.all_assignments()]
    with client_in(tmp_path, today="2026-09-15") as client:
        later = read(client, GUIDE_MISSING)
        landed = client.post("/parent/inbox/keep", data=review_form(later))
        store = store_of(client)
        reports = store.status_reports(mine)
        after = store.one_assignment(mine)

    assert saved.status_code == 303
    assert again.status_code == 303
    assert rows == [mine]
    assert row is not None
    assert row.due_date == date(2026, 9, 10)
    assert row.note == HER_NOTE
    assert row.note_by == "student"
    assert kept.texts == ("Return the course guide signed by a parent after reading it.",)
    assert "2026-09-09" in claims
    assert first == [("same", "Health", "Course Guide Due", "2026-09-09", mine, None)]
    assert question(later, 0) == ""
    assert landed.status_code == 303
    assert [(item.status, item.source_date_text) for item in reports] == [("missing", "09/14")]
    assert reports[0].reported_on == date(2026, 9, 15)
    assert after is not None
    assert after.due_date == date(2026, 9, 10)


def test_different_homework_is_its_own_and_each_later_report_asks_which(
    tmp_path: pathlib.Path,
) -> None:
    """Different makes the school's own assignment, named by the token its review page made.
    A later Missing email then could mean either, so it asks which, showing the email's date
    as unexplained, and the answer places that report only. The same email pasted again
    lands where it was placed; the next day's report asks again, and its answer is kept as
    an answer of its own."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_CARD)
        form = review_form(page)
        token = form["creation-0"]
        saved = client.post("/parent/inbox/keep", data={**form, "identity-0": DIFFERENT})
        theirs = f"assignment-{token}"
        rows = sorted(item.assignment_id for item in store_of(client).all_assignments())
        report_page = read(client, GUIDE_MISSING)
        placed = client.post(
            "/parent/inbox/keep", data={**review_form(report_page), "identity-0": theirs}
        )
        replayed = client.post(
            "/parent/inbox/keep", data={**review_form(report_page), "identity-0": theirs}
        )
        repeated = read(client, GUIDE_MISSING)
        recorded = decisions(client)
        their_reports = store_of(client).status_reports(theirs)
        her_reports = store_of(client).status_reports(mine)
    with client_in(tmp_path, today="2026-09-22") as client:
        next_page = read(client, GUIDE_MISSING_LATER)
        next_placed = client.post(
            "/parent/inbox/keep", data={**review_form(next_page), "identity-0": theirs}
        )
        recorded_later = decisions(client)

    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert saved.status_code == 303
    assert rows == sorted([mine, theirs])
    assert sorted(choices(report_page, 0)) == sorted([mine, theirs, DIFFERENT])
    assert "Which homework is this report about?" in question(report_page, 0)
    assert "The email writes 09/14 beside it, which it does not explain." in html.unescape(
        report_page
    )
    assert placed.status_code == 303
    assert replayed.status_code == 303
    assert replayed.headers["location"].startswith("/parent?added=0&updated=0&unchanged=1")
    assert [report.source_date_text for report in their_reports] == ["09/14"]
    assert her_reports == []
    assert question(repeated, 0) == ""
    assert sorted(choices(next_page, 0)) == sorted([mine, theirs, DIFFERENT])
    assert next_placed.status_code == 303
    assert [kind for kind, *_ in recorded_later] == ["different", "report_placed", "report_placed"]
    assert [(kind, lands_on) for kind, _, _, _, lands_on, _ in recorded] == [
        ("different", theirs),
        ("report_placed", theirs),
    ]
    assert recorded[0][5] == token


def different_from_hers(client: TestClient, due: date) -> tuple[str, str]:
    """Her homework, due ``due``, and the school's own, made by answering Different."""
    mine = hers(client, due=due)
    page = read(client, GUIDE_CARD)
    form = review_form(page)
    saved = client.post("/parent/inbox/keep", data={**form, "identity-0": DIFFERENT})
    assert saved.status_code == 303
    return mine, f"assignment-{form['creation-0']}"


def claims_on(client: TestClient, *names: str) -> list[int]:
    return [len(store_of(client).deadline_records(name)) for name in names]


def test_after_different_a_card_of_that_date_lands_on_the_schools_without_a_question(
    tmp_path: pathlib.Path,
) -> None:
    """Both are due that day, and the answer that the school's row was different homework
    showed hers: a later card of that date lands on the school's own."""
    with client_in(tmp_path) as client:
        mine, theirs = different_from_hers(client, date(2026, 9, 9))
        before = claims_on(client, mine, theirs)
        due_card = read(client, GUIDE_DUE_CARD)
        saved = client.post("/parent/inbox/keep", data=review_form(due_card))
        after = claims_on(client, mine, theirs)

    assert question(due_card, 0) == ""
    assert saved.status_code == 303
    assert after == [before[0], before[1] + 1]


def test_after_different_a_card_of_her_date_lands_on_hers_without_a_question(
    tmp_path: pathlib.Path,
) -> None:
    """Different settles her homework as well: a later card due on her date, which only hers
    has, lands there by that date, as it would for any two under one name."""
    with client_in(tmp_path) as client:
        mine, theirs = different_from_hers(client, date(2026, 9, 16))
        before = claims_on(client, mine, theirs)
        due_card = read(client, GUIDE_DUE_LATER)
        saved = client.post("/parent/inbox/keep", data=review_form(due_card))
        after = claims_on(client, mine, theirs)

    assert question(due_card, 0) == ""
    assert saved.status_code == 303
    assert after == [before[0] + 1, before[1]]


def test_different_on_a_later_round_makes_another_assignment_of_its_own(
    tmp_path: pathlib.Path,
) -> None:
    """A card due on a date neither has asks which; Different there makes one more
    assignment, named by that card's own token, never the first answer's."""
    with client_in(tmp_path) as client:
        mine, theirs = different_from_hers(client, date(2026, 9, 9))
        page = read(client, GUIDE_NEXT_ROUND)
        form = review_form(page)
        saved = client.post("/parent/inbox/keep", data={**form, "identity-0": DIFFERENT})
        rows = sorted(item.assignment_id for item in store_of(client).all_assignments())

    assert sorted(choices(page, 0)) == sorted([mine, theirs, DIFFERENT])
    assert saved.status_code == 303
    assert form["creation-0"] != theirs.removeprefix("assignment-")
    assert rows == sorted([mine, theirs, f"assignment-{form['creation-0']}"])


def test_the_same_text_pasted_again_after_more_homework_arrived_lands_where_it_was_saved(
    tmp_path: pathlib.Path,
) -> None:
    """Same was answered, then another assignment of that name and date arrived. The same
    text pasted again holds nothing new: it lands on hers, where it was saved, and asks
    nothing."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data={**review_form(first), "identity-0": mine})
        store_of(client).put_on_record([copy_of_the_guide(date(2026, 9, 9))], {})
        again = read(client, GUIDE_CARD)
        before = tables(client)
        saved = client.post("/parent/inbox/keep", data=review_form(again))
        after = tables(client)

    assert question(again, 0) == ""
    assert saved.status_code == 303
    assert saved.headers["location"].startswith("/parent?added=0&updated=0&unchanged=1")
    assert after == before


def test_text_saved_before_lands_where_it_was_saved_without_a_question(
    tmp_path: pathlib.Path,
) -> None:
    """The school's row was saved before her note became homework under the same name. The
    same text pasted again holds nothing new, so it lands where it was saved: no question is
    reopened by a repeat."""
    with client_in(tmp_path) as client:
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data=review_form(first))
        homework_from_a_note(
            store_of(client), course="Health", title="Course Guide Due", choice="separate"
        )
        again = read(client, GUIDE_CARD)
        before = tables(client)
        saved = client.post("/parent/inbox/keep", data=review_form(again))
        after = tables(client)

    assert question(again, 0) == ""
    assert saved.status_code == 303
    assert saved.headers["location"].startswith("/parent?added=0&updated=0&unchanged=1")
    assert after == before


def test_her_homework_kept_apart_when_her_note_was_added_is_not_asked_about_again(
    tmp_path: pathlib.Path,
) -> None:
    """Her note was kept apart from the school's assignment when it was added to homework.
    A later card about the school's assignment lands there without asking whether hers is
    the same."""
    with client_in(tmp_path) as client:
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data=review_form(first))
        (theirs,) = [item.assignment_id for item in store_of(client).all_assignments()]
        homework_from_a_note(
            store_of(client), course="Health", title="Course Guide Due", choice="separate"
        )
        due_card = read(client, GUIDE_DUE_CARD)
        saved = client.post("/parent/inbox/keep", data=review_form(due_card))
        claims = store_of(client).deadline_records(theirs)

    assert question(due_card, 0) == ""
    assert saved.status_code == 303
    assert len(claims) == 2


def test_an_answer_that_never_showed_a_homework_now_due_that_day_asks_again(
    tmp_path: pathlib.Path,
) -> None:
    """Same was answered while hers was the only homework due that day. Another assignment
    of that name and date has arrived since, which the answer never showed, so a new card
    for that date asks which. That answer is the latest and showed both, so an entry for
    that date lands where it said."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data={**review_form(first), "identity-0": mine})
        store_of(client).put_on_record([copy_of_the_guide(date(2026, 9, 9))], {})
        due_card = read(client, GUIDE_DUE_CARD)
        answered = client.post(
            "/parent/inbox/keep", data={**review_form(due_card), "identity-0": GUIDE_COPY}
        )
        typed = client.post("/parent/inbox/enter", data=entry())
        saved = client.post("/parent/inbox/keep", data=review_form(typed.text))
        claims = [claim.channel for claim in store_of(client).deadline_records(GUIDE_COPY)]

    assert sorted(choices(due_card, 0)) == sorted([mine, GUIDE_COPY, DIFFERENT])
    assert answered.status_code == 303
    assert question(typed.text, 0) == ""
    assert saved.status_code == 303
    assert SourceChannel.PARENT_ENTRY in claims


# ------------------------------------------------------------------ several under one name


@pytest.mark.parametrize(
    ("dues", "text"),
    [
        ((date(2026, 10, 1), date(2026, 10, 8)), LAB_MISSING),
        ((date(2026, 10, 1), date(2026, 10, 8)), lab_card("10/15/2026")),
        ((date(2026, 10, 1), date(2026, 10, 1)), lab_card("10/01/2026")),
    ],
    ids=["undated-report", "a-date-matching-neither", "two-share-the-date"],
)
def test_two_known_homework_under_one_name_ask_which_and_are_never_combined(
    tmp_path: pathlib.Path, dues: tuple[date, date], text: str
) -> None:
    with client_in(tmp_path) as client:
        names = school_rows(client, *dues)
        page = read(client, text)
        before = tables(client)
        unanswered = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    assert sorted(choices(page, 0)) == sorted([*names, DIFFERENT])
    assert unanswered.status_code == 200
    assert after == before


def test_a_date_that_one_of_two_has_lands_there_without_a_question(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        early, late = school_rows(client, date(2026, 10, 1), date(2026, 10, 8))
        page = read(client, lab_card("10/08/2026") + "Bring the log.\n")
        saved = client.post("/parent/inbox/keep", data=review_form(page))
        kept = store_of(client).school_instruction_readings([early, late]).readable

    assert question(page, 0) == ""
    assert saved.status_code == 303
    assert kept[late].texts == ("Bring the log.",)
    assert early not in kept or kept[early].texts == ()


# ------------------------------------------------------------------ retries and stale forms


@pytest.mark.parametrize("answer", ["same", "different"])
def test_the_same_review_sent_again_after_a_lost_response_writes_nothing_more(
    tmp_path: pathlib.Path, answer: str
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_CARD)
        form = {**review_form(page), "identity-0": mine if answer == "same" else DIFFERENT}
        first = client.post("/parent/inbox/keep", data=form)
        after_first = tables(client)
        second = client.post("/parent/inbox/keep", data=form)
        after_second = tables(client)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"].startswith("/parent?added=0&updated=0&unchanged=1")
    assert after_second == after_first


def test_a_second_tab_answering_otherwise_after_the_first_was_saved_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client)
        one = read(client, GUIDE_CARD)
        two = read(client, GUIDE_CARD)
        saved = client.post("/parent/inbox/keep", data={**review_form(one), "identity-0": mine})
        before = tables(client)
        refused = client.post(
            "/parent/inbox/keep", data={**review_form(two), "identity-0": DIFFERENT}
        )
        after = tables(client)
        recorded = decisions(client)

    assert saved.status_code == 303
    assert refused.status_code == 409
    assert after == before
    assert [kind for kind, *_ in recorded] == ["same"]
    assert f'value="{DIFFERENT}" checked' not in question(refused.text, 0)


def test_homework_arriving_between_the_review_and_the_save_puts_the_question_again(
    tmp_path: pathlib.Path,
) -> None:
    """The question listed hers alone. Another assignment of that name arrives before the
    save, so the answer was made against facts that changed: nothing is written, and the
    page asks again with both listed."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_CARD)
        store_of(client).put_on_record([copy_of_the_guide(date(2026, 9, 30))], {})
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data={**review_form(page), "identity-0": mine})
        after = tables(client)

    assert choices(page, 0) == [mine, DIFFERENT]
    assert refused.status_code == 409
    assert after == before
    assert sorted(choices(refused.text, 0)) == sorted([mine, GUIDE_COPY, DIFFERENT])


def test_a_second_tab_on_an_entry_that_adds_no_facts_is_refused_after_the_first_answer(
    tmp_path: pathlib.Path,
) -> None:
    """A typed entry with no dates names two assignments of its class and title, so it asks
    which. The answer changes no row, only the answers kept, and a second tab's other
    answer, made before it, is refused."""
    with client_in(tmp_path) as client:
        early, late = school_rows(client, date(2026, 10, 1), date(2026, 10, 8))
        lab_entry = entry(course="Science", title=LAB_LOG, due_date="")
        one = client.post("/parent/inbox/enter", data=lab_entry)
        two = client.post("/parent/inbox/enter", data=lab_entry)
        saved = client.post(
            "/parent/inbox/keep", data={**review_form(one.text), "identity-0": early}
        )
        before = tables(client)
        refused = client.post(
            "/parent/inbox/keep", data={**review_form(two.text), "identity-0": late}
        )
        after = tables(client)

    asked = html.unescape(question(one.text, 0))
    assert "Which homework is this?" in asked
    assert "This entry is saved as its own assignment." in asked
    assert saved.status_code == 303
    assert refused.status_code == 409
    assert after == before


def test_homework_made_from_her_note_after_the_review_is_met_inside_the_save(
    tmp_path: pathlib.Path,
) -> None:
    """The review found no homework of that class and title; her note became homework
    before the save. The save finds it inside its own transaction: nothing is written, and
    the page asks with the facts as they are now."""
    with client_in(tmp_path) as client:
        page = read(client, GUIDE_CARD)
        mine = hers(client)
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    assert question(page, 0) == ""
    assert refused.status_code == 409
    assert after == before
    assert choices(refused.text, 0) == [mine, DIFFERENT]


def test_a_failed_decision_write_keeps_the_paste_and_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client)
        page = read(client, GUIDE_CARD)
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "record_intake_decision", refuse)
        before = tables(client)
        failed = client.post("/parent/inbox/keep", data={**review_form(page), "identity-0": mine})
        after = tables(client)

    assert failed.status_code == 500
    assert after == before
    assert "Course Guide Due" in html.unescape(failed.text)


def test_a_kept_answer_that_cannot_be_read_saves_nothing_and_keeps_the_paste(
    tmp_path: pathlib.Path,
) -> None:
    """A kept answer that is not in the shape the store writes is never guessed around: the
    review and the save each say it cannot be read, write nothing, and keep the text."""
    with client_in(tmp_path) as client:
        mine = hers(client)
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data={**review_form(first), "identity-0": mine})
        page = read(client, GUIDE_MISSING)
        store = store_of(client)
        store._connection.execute("UPDATE intake_decisions SET shown = '{\"a\": 1}'")
        store._connection.commit()
        before = tables(client)
        reviewed = client.post("/parent/inbox/read", data={"text": GUIDE_MISSING})
        saved = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    for answer in (reviewed, saved):
        words = html.unescape(answer.text)
        assert answer.status_code == 500
        assert "A saved answer about which homework a row is cannot be read right now" in words
        assert "Course Guide Due" in words
    assert after == before


# ------------------------------------------------------------------ the creation token


def test_a_creation_token_survives_a_refused_form_and_makes_one_assignment(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        hers(client)
        page = read(client, GUIDE_CARD)
        form = review_form(page)
        token = form["creation-0"]
        broken = client.post(
            "/parent/inbox/keep",
            data={**form, "identity-0": DIFFERENT, "instructions-0": "bad"},
        )
        returned = review_form(broken.text)
        saved = client.post("/parent/inbox/keep", data={**returned, "identity-0": DIFFERENT})
        again = client.post("/parent/inbox/keep", data={**returned, "identity-0": DIFFERENT})
        made = [
            item.assignment_id
            for item in store_of(client).all_assignments()
            if "from-note" not in item.assignment_id
        ]

    assert broken.status_code == 422
    assert returned["creation-0"] == token
    assert saved.status_code == 303
    assert again.status_code == 303
    assert made == [f"assignment-{token}"]


@pytest.mark.parametrize(
    "forged", ["a-token-the-page-never-made", "an-id-never-shown", "a-card-never-asked"]
)
def test_a_token_or_a_choice_the_page_did_not_give_is_refused(
    tmp_path: pathlib.Path, forged: str
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client)
        (other,) = school_rows(client, date(2026, 12, 1))
        page = read(client, GUIDE_CARD)
        form = review_form(page)
        if forged == "a-token-the-page-never-made":
            form = {**form, "creation-0": "0" * 32, "identity-0": DIFFERENT}
        elif forged == "an-id-never-shown":
            form = {**form, "identity-0": other}
        else:
            form = {**form, "identity-0": mine, "identity-7": DIFFERENT}
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=form)
        after = tables(client)

    assert refused.status_code == 422
    assert after == before


# ------------------------------------------------------------------ direct callers


def test_a_direct_caller_meets_the_same_rules(tmp_path: pathlib.Path) -> None:
    """``keep`` asks rather than choosing, applies an answer made against the facts as they
    stand, and refuses one made against facts that changed since, one naming homework the
    question never listed, and a Different without a token the page could have made."""
    from blossom.intake import IdentityAnswer

    with client_in(tmp_path) as client:
        mine = hers(client)
        (other,) = school_rows(client, date(2026, 12, 1))
        store: ProjectStateStore = store_of(client)
        now = store.instruction_moment()
        items = read_text(GUIDE_CARD, now=now[0], today=now[1]).items
        asked = keep(items, store, imported_by="parent", now=now[0], today=now[1])
        assert isinstance(asked, list)
        (card,) = asked
        answer = IdentityAnswer(mine, card.identity_shown, card.identity_basis)
        wrong = (
            IdentityAnswer(mine, card.identity_shown, "0" * 64),
            IdentityAnswer(other, card.identity_shown, card.identity_basis),
            IdentityAnswer(DIFFERENT, card.identity_shown, card.identity_basis, "not-a-token"),
        )
        refused = [
            keep(items, store, identities={0: item}, imported_by="parent", now=now[0], today=now[1])
            for item in wrong
        ]
        before = tables(client)
        rows = sorted(item.assignment_id for item in store.all_assignments())
        saved = keep(
            items, store, identities={0: answer}, imported_by="parent", now=now[0], today=now[1]
        )

    assert card.identity_asked
    assert card.identity_shown == (mine,)
    assert all(isinstance(item, ChangedSinceShown) for item in refused)
    assert before.get("intake_decisions") == []
    assert rows == sorted([mine, other])
    assert isinstance(saved, Kept)


# ------------------------------------------------------------------ a parent's typed note


def entry(**fields: str) -> dict[str, str]:
    return {
        "course": "Health",
        "title": "Course Guide Due",
        "assigned_on": "",
        "due_date": "2026-09-09",
        "kind": "HOMEWORK",
        "note": "",
        **fields,
    }


KEPT_NOTE = (
    "Her note will stay. Your note won't be saved to this assignment. The other changes can "
    "still be saved."
)


def test_a_parents_typed_note_leaves_her_note_and_says_so_before_and_after_saving(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = hers(client, due=None)
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data={**review_form(first), "identity-0": mine})
        shown = client.post(
            "/parent/inbox/enter", data=entry(note="Signed copy is in the blue folder.")
        )
        saved = client.post("/parent/inbox/keep", data=review_form(shown.text))
        family = client.get(saved.headers["location"])
        row = store_of(client).one_assignment(mine)

    words = html.unescape(shown.text)
    assert KEPT_NOTE in words
    assert HER_NOTE in words
    assert "Signed copy is in the blue folder." in words
    assert saved.status_code == 303
    assert "kept_note=1" in saved.headers["location"]
    assert "Her note stayed. Your note wasn't saved to her homework." in html.unescape(family.text)
    assert row is not None
    assert row.note == HER_NOTE
    assert row.note_by == "student"


def test_a_parents_typed_note_still_replaces_a_parents_note(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        first = client.post("/parent/inbox/enter", data=entry(note="Old words."))
        client.post("/parent/inbox/keep", data=review_form(first.text))
        shown = client.post("/parent/inbox/enter", data=entry(note="New words."))
        saved = client.post("/parent/inbox/keep", data=review_form(shown.text))
        (row,) = store_of(client).all_assignments()

    assert "Your note replaces the saved note." in html.unescape(shown.text)
    assert KEPT_NOTE not in html.unescape(shown.text)
    assert saved.status_code == 303
    assert row.note == "New words."
    assert row.note_by == "parent"


def test_her_note_arriving_after_the_review_refuses_the_save(tmp_path: pathlib.Path) -> None:
    """The review said the parent's note would be saved; by the save the assignment carries
    her note. Nothing is written, and the page returned says her note will stay."""
    with client_in(tmp_path) as client:
        mine = hers(client, due=None)
        first = read(client, GUIDE_CARD)
        client.post("/parent/inbox/keep", data={**review_form(first), "identity-0": mine})
        store = store_of(client)
        store._connection.execute(
            "UPDATE assignments SET note = NULL WHERE assignment_id = ?", (mine,)
        )
        store._connection.commit()
        shown = client.post("/parent/inbox/enter", data=entry(note="A parent's words."))
        store._connection.execute(
            "UPDATE assignments SET note = ? WHERE assignment_id = ?", (HER_NOTE, mine)
        )
        store._connection.commit()
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=review_form(shown.text))
        after = tables(client)

    assert "The note is saved." in html.unescape(shown.text)
    assert refused.status_code == 409
    assert after == before
    assert KEPT_NOTE in html.unescape(refused.text)
    assert str(escape("A parent's words.")) in refused.text
