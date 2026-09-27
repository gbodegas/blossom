"""Homework already here under another name.

The school can name homework differently from the name it's saved under here: her
note says "Patterns quiz", the portal says "Q1 Check 3". A card that would save a
new assignment, or that asks which homework it is, offers a search of the homework
already here. Find, Next, Previous, Choose, and Remove only update the review; Save
is the one press that writes. The choice is kept as a ``renamed`` answer for the
school's class and title, so the next paste of that work lands without a question,
under today's date rules. Once an assignment with the school's own name exists, a
card asks which, listing both. The chosen homework keeps its own name, her note,
and its history, and two assignments are never combined.
"""

import html
import json
import pathlib
import re
import sqlite3
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.intake import ChangedSinceShown, Kept, read_text
from blossom.reconciliation import SourceChannel
from blossom.settings import Settings
from blossom.stores.intake_decisions import (
    INSERT_DECISION,
    DecisionToKeep,
    UnreadableDecision,
)
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore
from tests.support import (
    SAME_ORIGIN,
    Answer,
    fixture_clock,
    fixture_settings,
    homework_from_a_note,
    store_of,
)

HER_NOTE = "Patterns, then the proofs page."
BASIS = "a" * 64
DECIDED_AT = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)

# ------------------------------------------------------------------ the answers table


OLD_TABLE = """
CREATE TABLE intake_decisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('same', 'different', 'which', 'report_placed')),
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT,
    lands_on TEXT NOT NULL,
    shown TEXT NOT NULL,
    basis TEXT NOT NULL,
    creation TEXT UNIQUE,
    report TEXT,
    authored_by TEXT NOT NULL,
    channel TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decided_on TEXT NOT NULL
)
"""
"""The answers table as the version before this one made it."""


def answer_row(kind: str, lands_on: str, due: str | None = "2026-10-01") -> tuple[object, ...]:
    return (
        kind,
        "08 Algebra",
        "Patterns quiz",
        due,
        lands_on,
        json.dumps([lands_on]),
        BASIS,
        None,
        None,
        "parent",
        SourceChannel.PARENT_ENTRY.value,
        DECIDED_AT.isoformat(),
        "2026-09-24",
    )


def file_from_before(path: pathlib.Path) -> list[tuple[object, ...]]:
    """A household file whose answers table predates ``renamed``, holding two answers, as
    the version before wrote them; the rows it holds."""
    store = ProjectStateStore.open(path, fixture_clock())
    mine = homework_from_a_note(
        store, course="08 Algebra", title="Patterns quiz", due_date=date(2026, 10, 1)
    )
    store.close()
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("DROP TABLE intake_decisions")
        connection.execute(OLD_TABLE)
        connection.execute(
            "CREATE INDEX intake_decisions_by_name ON intake_decisions (course, title)"
        )
        connection.execute(INSERT_DECISION, answer_row("same", mine))
        connection.execute(INSERT_DECISION, answer_row("which", mine, None))
    rows = connection.execute("SELECT * FROM intake_decisions ORDER BY sequence").fetchall()
    connection.close()
    return rows


def answers_table(path: pathlib.Path) -> tuple[str, list[tuple[object, ...]], list[str]]:
    """The answers table as the file holds it: how it's made, its rows, and its indexes."""
    connection = sqlite3.connect(path)
    try:
        made = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'intake_decisions'"
        ).fetchone()[0]
        rows = connection.execute("SELECT * FROM intake_decisions ORDER BY sequence").fetchall()
        indexes = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'intake_decisions' AND sql IS NOT NULL"
            )
        ]
        return str(made), rows, indexes
    finally:
        connection.close()


def a_renamed_answer(lands_on: str) -> DecisionToKeep:
    return DecisionToKeep(
        kind="renamed",
        course="08 Algebra",
        title="Q1 Check 3",
        due_date=date(2026, 10, 1),
        lands_on=lands_on,
        shown=(lands_on,),
        basis=BASIS,
    )


def keep_answer(store: ProjectStateStore, answer: DecisionToKeep) -> None:
    store.record_intake_decision(
        answer,
        authored_by="parent",
        channel=SourceChannel.PARENT_ENTRY,
        now=DECIDED_AT,
        today=date(2026, 9, 24),
    )


def test_a_file_from_before_keeps_every_answer_and_takes_renamed_ones(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "record.sqlite3"
    before = file_from_before(path)
    mine = str(before[0][5])

    store = ProjectStateStore.open(path, fixture_clock())
    keep_answer(store, a_renamed_answer(mine))
    kept = store.intake_decisions([("08 Algebra", "Q1 Check 3")])[("08 Algebra", "Q1 Check 3")]
    store.close()
    made, rows, indexes = answers_table(path)

    assert "'renamed'" in made
    assert rows[: len(before)] == before
    assert [row[1] for row in rows] == ["same", "which", "renamed"]
    assert int(str(rows[-1][0])) > max(int(str(row[0])) for row in before)
    assert [(item.kind, item.lands_on, item.shown) for item in kept] == [("renamed", mine, (mine,))]
    assert indexes == ["intake_decisions_by_name"]


def test_a_second_start_on_a_file_already_upgraded_changes_nothing(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    file_from_before(path)
    ProjectStateStore.open(path, fixture_clock()).close()
    first = answers_table(path)

    ProjectStateStore.open(path, fixture_clock()).close()

    assert answers_table(path) == first


def test_a_start_refused_midway_leaves_the_answers_as_they_were(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.sqlite3"
    before = file_from_before(path)
    made_before = answers_table(path)
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def no_drop(action: int, first: str | None, *rest: object) -> int:
        refused = action == sqlite3.SQLITE_DROP_TABLE and first == "intake_decisions"
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[call-overload]
        connection.set_authorizer(no_drop)
        opened.append(connection)
        return connection  # type: ignore[no-any-return]

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.DatabaseError):
        ProjectStateStore.open(path, fixture_clock())
    for connection in opened:
        connection.close()
    monkeypatch.undo()
    connection = sqlite3.connect(path)
    tables = {
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()

    assert answers_table(path) == made_before
    assert made_before[1] == before
    assert "intake_decisions_next" not in tables


@pytest.mark.parametrize(
    "bent",
    ["shown-other-homework-too", "a-creation-token", "a-report"],
)
def test_a_renamed_answer_is_kept_and_read_only_in_the_shape_the_store_writes(
    tmp_path: pathlib.Path, bent: str
) -> None:
    """A renamed answer names exactly the homework it lands on, carries no creation token,
    and places no report; any other shape is refused on the way in and on the way out."""
    store = ProjectStateStore.open(tmp_path / "record.sqlite3", fixture_clock())
    mine = homework_from_a_note(store, course="08 Algebra", title="Patterns quiz")
    fields = list(answer_row("renamed", mine))
    fields[1], fields[2] = "08 Algebra", "Q1 Check 3"
    answer = a_renamed_answer(mine)
    if bent == "shown-other-homework-too":
        fields[5] = json.dumps(["assignment-something-else", mine])
        answer = DecisionToKeep(**{**answer.__dict__, "shown": ("assignment-something-else", mine)})
    elif bent == "a-creation-token":
        fields[7] = "b" * 32
        answer = DecisionToKeep(**{**answer.__dict__, "creation": "b" * 32})
    else:
        report = {"channel": "EMAIL", "status": "missing", "day": "2026-09-24"}
        fields[8] = json.dumps({**report, "source_date_text": None})
        answer = DecisionToKeep(
            **{**answer.__dict__, "report": {**report, "source_date_text": None}}
        )

    with pytest.raises(ValueError, match="cannot be kept"):
        keep_answer(store, answer)
    with store._connection:
        store._connection.execute(INSERT_DECISION, tuple(fields))
    with pytest.raises(UnreadableDecision):
        store.intake_decisions([("08 Algebra", "Q1 Check 3")])


# ------------------------------------------------------------------ the review, before Save

TODAY = "2026-09-24"
Q1_ASSIGNED = (
    "Homework for Wren\n- 09/23/2026 - Wednesday\n"
    "08 Algebra - Assigned: Q1 Check 3: (Due:10/01/2026)\n"
    "Patterns, if-then statements, first proofs.\n"
)
Q1_DUE = "Homework for Wren\n- 10/01/2026 - Thursday\n08 Algebra - Due: Q1 Check 3:\n"
Q1_MISSING = "Assignments:\n10/05 08 Algebra - A: Homework: Q1 Check 3 Grade: Missing\n"
GUIDE_CARD = (
    "Homework for Wren\n- 09/02/2026 - Wednesday\n"
    "Health - Assigned: Course Guide Due: (Due:09/09/2026)\n"
)
SAID_OF_Q1 = "The school calls this Q1 Check 3, 08 Algebra, due Thursday, October 1, 2026."
SAVED_AS_QUIZ = "It's saved here as Patterns quiz, 08 Algebra, due Thursday, October 1, 2026."
KEPT_SEPARATE = (
    "These assignments were kept separate earlier. Choosing one here adds this school update "
    "to it. Their existing records stay separate; combining them isn't available here."
)


def q1_due_on(day: str, weekday: str) -> str:
    return f"Homework for Wren\n- {day} - {weekday}\n08 Algebra - Due: Q1 Check 3:\n"


def settings_in(tmp_path: pathlib.Path, today: str = TODAY) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY=today,
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


def client_in(tmp_path: pathlib.Path, today: str = TODAY) -> TestClient:
    return TestClient(
        create_app(settings_in(tmp_path, today)), follow_redirects=False, headers=SAME_ORIGIN
    )


def review_form(page: str) -> dict[str, str]:
    """The review form as a browser sends it back untouched: hidden fields, the words in each
    text field, each select's chosen option, each ticked box, and the text."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)
    }
    for field in re.findall(r'<input type="text"[^>]*>', page):
        named = re.search(r'name="([^"]+)"', field)
        valued = re.search(r'value="([^"]*)"', field)
        if named is not None:
            fields[named.group(1)] = html.unescape(valued.group(1) if valued else "")
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


def read(client: TestClient, text: str) -> str:
    page = client.post("/parent/inbox/read", data={"text": text})
    assert page.status_code == 200, page.text
    return page.text


def press(client: TestClient, page: str, **fields: str) -> Answer:
    """A press of Find, Next, Previous, Choose, or Remove on a review page."""
    return client.post("/parent/inbox/find", data={**review_form(page), **fields})


def save(client: TestClient, page: str, **fields: str) -> Answer:
    return client.post("/parent/inbox/keep", data={**review_form(page), **fields})


def patterns_quiz(client: TestClient, due: date | None = date(2026, 10, 1)) -> str:
    """Her homework, from her note, under the name she gave it."""
    return homework_from_a_note(
        store_of(client), course="08 Algebra", title="Patterns quiz", due_date=due, note=HER_NOTE
    )


def school_row(client: TestClient, name: str, course: str, title: str, due: date | None) -> str:
    store_of(client).put_on_record(
        [
            Assignment(
                assignment_id=name,
                course=course,
                title=title,
                due_date=due,
                dependencies=[],
                reported_submission_status="unknown",
                kind=AssignmentKind.HOMEWORK,
                origins={"record": SourceChannel.LMS},
            )
        ],
        {},
    )
    return name


def found(page: str, key: int) -> list[str]:
    """The homework the page offers to choose for one card, in order."""
    return re.findall(rf'name="choose" value="{key}:([^"]+)"', page)


def names(page: str, key: int) -> str:
    """What the card says of the school's name and the name it's saved under, if anything."""
    said = re.search(rf'<p class="source names" id="names-{key}">(.*?)</p>', page, re.S)
    if said is None:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", said.group(1))).split())


def offered(page: str, key: int) -> bool:
    return f'id="rename-{key}"' in page


def question(page: str, key: int) -> str:
    found_question = re.search(
        rf'<fieldset class="choice identity" id="identity-question-{key}"[^>]*>(.*?)</fieldset>',
        page,
        re.S,
    )
    return "" if found_question is None else found_question.group(1)


def choices(page: str, key: int) -> list[str]:
    values = re.findall(rf'name="identity-{key}" value="([^"]+)"', question(page, key))
    return [value.removeprefix("same:") for value in values]


def tables(client: TestClient) -> dict[str, list[tuple[object, ...]]]:
    connection = store_of(client)._connection
    wanted = (
        "assignments",
        "date_claims",
        "status_reports",
        "school_instructions",
        "intake_decisions",
        "capture_events",
    )
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in wanted
    }


def decisions(client: TestClient) -> list[tuple[object, ...]]:
    return (
        store_of(client)
        ._connection.execute(
            "SELECT kind, course, title, due_date, lands_on, creation FROM intake_decisions "
            "ORDER BY sequence"
        )
        .fetchall()
    )


def chosen(
    client: TestClient, text: str, target: str, query: str = "patterns", key: int = 0
) -> str:
    """The review page for ``text`` with ``target`` found and chosen on card ``key``."""
    page = read(client, text)
    looked = press(client, page, find=str(key), **{f"search-{key}": query})
    assert looked.status_code == 200, looked.text
    assert target in found(looked.text, key)
    picked = press(client, looked.text, choose=f"{key}:{target}")
    assert picked.status_code == 200, picked.text
    return picked.text


def test_a_card_that_would_save_new_homework_offers_a_search_and_looking_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = read(client, Q1_ASSIGNED)
        before = tables(client)
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        after = tables(client)

    shown = html.unescape(looked.text)
    assert offered(page, 0)
    assert "Is this homework already here under another name?" in page
    assert looked.status_code == 200
    assert found(looked.text, 0) == [mine]
    assert "Patterns quiz" in shown
    assert "Thursday, October 1, 2026" in shown
    disclosure = re.search(r'id="rename-0".*?</details>', looked.text, re.S)
    assert disclosure is not None
    assert "checked" not in disclosure.group(0)
    assert re.search(r'id="search-results-0" tabindex="-1" autofocus', looked.text)
    assert re.search(r'<details class="rename" id="rename-0" open>', looked.text)
    assert after == before


@pytest.mark.parametrize(
    ("words", "said"),
    [
        ("", "Type a word or two from the class or the title to search."),
        ("zebra", "Nothing on record matches every word. Try fewer words, or other ones."),
        ("x" * 201, "Search words are one line of up to 200 characters."),
    ],
    ids=["no-words", "nothing-found", "too-many-words"],
)
def test_find_says_what_to_do_when_it_finds_nothing(
    tmp_path: pathlib.Path, words: str, said: str
) -> None:
    with client_in(tmp_path) as client:
        patterns_quiz(client)
        page = read(client, Q1_ASSIGNED)
        looked = press(client, page, find="0", **{"search-0": words})

    assert looked.status_code == 200
    assert said in html.unescape(looked.text)
    assert found(looked.text, 0) == []
    assert 'id="search-hint-0" tabindex="-1" autofocus' in looked.text
    assert '<details class="rename" id="rename-0" open>' in looked.text


def test_results_come_twenty_to_a_page_and_only_pages_the_form_offered(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        for place in range(21):
            school_row(
                client, f"assignment-warmup-{place:02}", "Warmups", f"Warmup {place:02}", None
            )
        page = read(client, Q1_ASSIGNED)
        first = press(client, page, find="0", **{"search-0": "warmup"})
        second = press(client, first.text, results_page="0:2")
        back = press(client, second.text, results_page="0:1")
        beyond = press(client, first.text, results_page="0:3")
        elsewhere = press(client, first.text, results_page="1:1")

    assert len(found(first.text, 0)) == 20
    assert 'name="results_page" value="0:2"' in first.text
    assert found(second.text, 0) == ["assignment-warmup-20"]
    assert 'name="results_page" value="0:1"' in second.text
    assert found(back.text, 0) == found(first.text, 0)
    assert beyond.status_code == 422
    assert elsewhere.status_code == 422


def test_searching_keeps_the_draft_and_every_answer_on_other_cards(tmp_path: pathlib.Path) -> None:
    """Her Course Guide asks Same or Different on the first card; the second card would
    save Q1 Check 3 as new. The search works with the question unanswered, and once it's
    answered, the answer and a type chosen come back with the results."""
    with client_in(tmp_path) as client:
        guide = homework_from_a_note(
            store_of(client), course="Health", title="Course Guide Due", note=HER_NOTE
        )
        mine = patterns_quiz(client)
        page = read(client, GUIDE_CARD + Q1_ASSIGNED.split("\n", 1)[1])
        unanswered = press(client, page, find="1", **{"search-1": "patterns"})
        answered = press(
            client,
            page,
            find="1",
            **{"search-1": "patterns", "identity-0": f"same:{guide}", "kind-1": "TASK"},
        )
        before = tables(client)
        again = press(client, answered.text, results_page="1:1")
        after = tables(client)

    assert choices(page, 0) == [guide, "different"]
    assert found(unanswered.text, 1) == [mine]
    assert found(answered.text, 1) == [mine]
    carried = review_form(answered.text)
    assert carried["identity-0"] == f"same:{guide}"
    assert carried["kind-1"] == "TASK"
    assert review_form(again.text)["identity-0"] == f"same:{guide}"
    assert after == before


def test_choose_shows_both_names_and_dates_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        before = tables(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        after = tables(client)

    assert names(page, 0) == f"{SAID_OF_Q1} {SAVED_AS_QUIZ}"
    assert review_form(page)["picked-0"] == mine
    assert 'name="remove" value="0"' in page
    assert re.search(r'id="picked-0" tabindex="-1" autofocus', page)
    card = re.search(r"<article.*?</article>", page, re.S)
    assert card is not None
    assert "New</span>" not in card.group(0)
    assert after == before


@pytest.mark.parametrize("pressed", ["homework-not-in-the-results", "a-card-never-offered"])
def test_choose_takes_only_a_result_the_page_offered_for_that_card(
    tmp_path: pathlib.Path, pressed: str
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = school_row(client, "assignment-unit-1", "08 Algebra", "Unit 1 review", None)
        page = read(client, Q1_ASSIGNED)
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        before = tables(client)
        refused = press(
            client,
            looked.text,
            choose=f"0:{other}" if pressed == "homework-not-in-the-results" else f"4:{mine}",
        )
        after = tables(client)

    assert found(looked.text, 0) == [mine]
    assert refused.status_code == 422
    assert "picked-0" not in review_form(refused.text)
    assert after == before


def test_a_new_search_keeps_the_choice_named_even_when_it_isnt_among_the_results(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        school_row(client, "assignment-unit-1", "08 Algebra", "Unit 1 review", None)
        page = chosen(client, Q1_ASSIGNED, mine)
        looked = press(client, page, find="0", **{"search-0": "unit"})

    assert found(looked.text, 0) == ["assignment-unit-1"]
    assert review_form(looked.text)["picked-0"] == mine
    assert names(looked.text, 0) == f"{SAID_OF_Q1} {SAVED_AS_QUIZ}"


def test_remove_puts_the_card_back_as_new_homework(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        removed = press(client, page, remove="0")

    assert removed.status_code == 200
    assert "picked-0" not in review_form(removed.text)
    assert names(removed.text, 0) == ""
    assert offered(removed.text, 0)
    assert ">New</span>" in removed.text
    assert found(removed.text, 0) == [mine]


# ------------------------------------------------------------------ Save, and later pastes


def test_saving_the_choice_puts_the_school_facts_on_that_homework_and_keeps_its_name(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        saved = save(client, page)
        store = store_of(client)
        rows = store.all_assignments()
        claims = store.deadline_records(mine)
        kept = store.school_instruction_readings([mine]).readable[mine]
        recorded = decisions(client)

    (row,) = rows
    assert saved.status_code == 303
    assert (row.assignment_id, row.course, row.title) == (mine, "08 Algebra", "Patterns quiz")
    assert (row.note, row.note_by) == (HER_NOTE, "student")
    assert row.due_date == date(2026, 10, 1)
    assert [claim.channel for claim in claims].count(SourceChannel.LMS) == 1
    assert kept.texts == ("Patterns, if-then statements, first proofs.",)
    assert recorded == [("renamed", "08 Algebra", "Q1 Check 3", "2026-10-01", mine, None)]


def test_the_same_review_sent_again_after_the_save_writes_nothing(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        first = save(client, page)
        before = tables(client)
        second = save(client, page)
        after = tables(client)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"].startswith("/parent?added=0&updated=0&unchanged=1")
    assert after == before


def test_a_later_card_under_the_school_name_lands_on_that_homework_without_a_question(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        save(client, chosen(client, Q1_ASSIGNED, mine))
        later = read(client, Q1_DUE)
        saved = save(client, later)
        claims = store_of(client).deadline_records(mine)
        rows = store_of(client).all_assignments()

    assert question(later, 0) == ""
    assert not offered(later, 0)
    assert names(later, 0) == f"{SAID_OF_Q1} {SAVED_AS_QUIZ}"
    assert saved.status_code == 303
    assert len(rows) == 1
    assert len(claims) == 3


def test_a_later_missing_email_lands_there_and_its_date_stays_the_emails(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path, today="2026-10-05") as client:
        mine = patterns_quiz(client)
        save(client, chosen(client, Q1_ASSIGNED, mine))
        later = read(client, Q1_MISSING)
        saved = save(client, later)
        reports = store_of(client).status_reports(mine)
        row = store_of(client).one_assignment(mine)

    assert question(later, 0) == ""
    assert saved.status_code == 303
    assert [(report.status, report.source_date_text) for report in reports] == [
        ("missing", "10/05")
    ]
    assert row is not None
    assert row.due_date == date(2026, 10, 1)


@pytest.mark.parametrize(
    ("day", "weekday", "asks"),
    [("10/07/2026", "Wednesday", False), ("10/08/2026", "Thursday", True)],
    ids=["six-days-apart", "seven-days-apart"],
)
def test_a_later_date_under_the_school_name_follows_the_date_rules(
    tmp_path: pathlib.Path, day: str, weekday: str, asks: bool
) -> None:
    """Under a week from the saved date, the school's date is a claim beside it; a week or
    more, the recurring question is asked."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        save(client, chosen(client, Q1_ASSIGNED, mine))
        later = read(client, q1_due_on(day, weekday))

    assert question(later, 0) == ""
    assert ('id="occurrence-question-0"' in later) is asks
    assert names(later, 0).startswith("The school calls this Q1 Check 3")


def test_new_work_under_the_school_name_is_its_own_and_later_cards_ask_which(
    tmp_path: pathlib.Path,
) -> None:
    """A week later the parent says it's new work: the school's name gets an assignment of
    its own. A card for the first date then asks which, listing both, and doesn't land on the
    remembered homework by itself."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        save(client, chosen(client, Q1_ASSIGNED, mine))
        later = read(client, q1_due_on("10/08/2026", "Thursday"))
        made = save(client, later, **{"occurrence-0": "new"})
        rows = sorted(item.assignment_id for item in store_of(client).all_assignments())
        again = read(client, Q1_DUE)

    (theirs,) = [name for name in rows if name != mine]
    assert made.status_code == 303
    assert sorted(choices(again, 0)) == sorted([mine, theirs, "different"])
    assert "Which homework is this?" in question(again, 0)


def test_an_exact_retry_after_another_assignment_of_the_school_name_appears_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        save(client, page)
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        before = tables(client)
        retried = save(client, page)
        after = tables(client)
        repeated = read(client, Q1_ASSIGNED)

    assert retried.status_code == 303
    assert after == before
    assert question(repeated, 0) == ""


# ------------------------------------------------------------------ where the search is offered


def test_no_search_where_a_row_lands_on_homework_by_its_own_name(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        page = read(client, Q1_DUE)

    assert not offered(page, 0)


def test_a_choice_made_stale_by_homework_of_the_school_name_arriving_saves_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        before = tables(client)
        refused = save(client, page)
        after = tables(client)

    assert refused.status_code == 409
    assert after == before
    assert "Q1 Check 3" in html.unescape(refused.text)
    assert "picked-0" not in review_form(refused.text)
    assert 'id="picked-unsaved-0"' in refused.text


def test_a_stale_choice_can_be_chosen_again_and_saved(tmp_path: pathlib.Path) -> None:
    """The chosen homework's date moved after it was chosen: the save refuses, says the choice
    wasn't saved, and offers it again as it stands now; chosen again, it saves."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        store = store_of(client)
        with store._connection:
            store._connection.execute(
                "UPDATE assignments SET due_date = '2026-10-02' WHERE assignment_id = ?", (mine,)
            )
        refused = save(client, page)
        again = press(client, refused.text, choose=f"0:{mine}")
        saved = save(client, again.text)
        recorded = decisions(client)

    assert refused.status_code == 409
    assert found(refused.text, 0) == [mine]
    assert "Friday, October 2, 2026" in html.unescape(refused.text)
    assert again.status_code == 200
    assert "Friday, October 2, 2026" in names(again.text, 0)
    assert saved.status_code == 303
    assert [kind for kind, *_ in recorded] == ["renamed"]


def test_a_second_tab_choosing_other_homework_after_the_first_saved_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = school_row(client, "assignment-unit-1", "08 Algebra", "Unit 1 review", None)
        one = chosen(client, Q1_ASSIGNED, mine)
        two = chosen(client, Q1_ASSIGNED, other, query="unit")
        first = save(client, one)
        before = tables(client)
        second = save(client, two)
        after = tables(client)

    assert first.status_code == 303
    assert second.status_code == 409
    assert after == before


@pytest.mark.parametrize(
    "bent",
    ["another-homework", "the-choice-left-out", "a-choice-on-a-card-without-one"],
)
def test_a_choice_the_page_did_not_sign_is_refused_whole(tmp_path: pathlib.Path, bent: str) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = school_row(client, "assignment-unit-1", "08 Algebra", "Unit 1 review", None)
        page = chosen(client, Q1_ASSIGNED, mine)
        form = review_form(page)
        if bent == "another-homework":
            form["picked-0"] = other
        elif bent == "the-choice-left-out":
            del form["picked-0"]
        else:
            form["picked-3"] = other
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=form)
        after = tables(client)

    assert refused.status_code == 422
    assert after == before
    assert "Q1 Check 3" in html.unescape(refused.text)


def test_a_failed_answer_write_keeps_the_paste_and_says_the_choice_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "record_intake_decision", refuse)
        before = tables(client)
        failed = save(client, page)
        after = tables(client)

    shown = " ".join(html.unescape(re.sub(r"<[^>]+>", "", failed.text)).split())
    assert failed.status_code == 500
    assert after == before
    assert "Q1 Check 3" in shown
    assert f"homework already here under another name, with id {mine}" in shown


def test_a_direct_caller_meets_the_same_rules(tmp_path: pathlib.Path) -> None:
    """``keep`` applies a choice made against the card and the homework as they stand, and
    refuses one on a card that didn't offer it (as never offered) or one made against facts
    that changed since."""
    from blossom.candidates import readings_for
    from blossom.captures import candidate_basis
    from blossom.intake import NotOffered, Pick, keep

    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        store: ProjectStateStore = store_of(client)
        now = store.instruction_moment()
        items = read_text(Q1_ASSIGNED, now=now[0], today=now[1]).items
        row = store.one_assignment(mine)
        assert row is not None
        basis = candidate_basis(readings_for(store, [row]))
        never = keep(
            items, store, picks={0: Pick(mine, basis, "new")}, offered={}, now=now[0], today=now[1]
        )
        stale = keep(
            items,
            store,
            picks={0: Pick(mine, "0" * 64, "new")},
            offered={0: "new"},
            now=now[0],
            today=now[1],
        )
        before = tables(client)
        kept = keep(
            items,
            store,
            picks={0: Pick(mine, basis, "new")},
            offered={0: "new"},
            now=now[0],
            today=now[1],
        )

    assert isinstance(never, NotOffered)
    assert isinstance(stale, ChangedSinceShown)
    assert before["intake_decisions"] == []
    assert isinstance(kept, Kept)


# ------------------------------------------------------------------ a card that asks which


def test_a_question_card_offers_the_search_and_a_choice_takes_the_questions_place(
    tmp_path: pathlib.Path,
) -> None:
    """Her Course Guide asks Same or Different. Choosing homework found by another name
    takes the question's place; Remove brings the question back, and an ordinary answer
    then saves as before."""
    with client_in(tmp_path) as client:
        guide = homework_from_a_note(
            store_of(client), course="Health", title="Course Guide Due", note=HER_NOTE
        )
        packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
        page = read(client, GUIDE_CARD)
        picked = chosen(client, GUIDE_CARD, packet, query="packet")
        removed = press(client, picked, remove="0")
        saved = save(client, removed.text, **{"identity-0": f"same:{guide}"})
        recorded = decisions(client)

    assert offered(page, 0)
    assert choices(page, 0) == [guide, "different"]
    assert question(picked, 0) == ""
    assert "It's saved here as Packet 1, Health" in names(picked, 0)
    assert choices(removed.text, 0) == [guide, "different"]
    assert saved.status_code == 303
    assert [kind for kind, *_ in recorded] == ["same"]


def test_a_choice_sent_beside_an_answer_to_the_question_is_refused_whole(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        guide = homework_from_a_note(
            store_of(client), course="Health", title="Course Guide Due", note=HER_NOTE
        )
        packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
        picked = chosen(client, GUIDE_CARD, packet, query="packet")
        before = tables(client)
        refused = save(client, picked, **{"identity-0": f"same:{guide}"})
        after = tables(client)

    assert refused.status_code == 422
    assert after == before


def test_the_question_says_when_an_earlier_answer_kept_the_homework_apart(
    tmp_path: pathlib.Path,
) -> None:
    """Different was answered on the Course Guide: the school's own assignment was kept apart
    from hers. A later report asks which, and says why they're separate; choosing one adds
    the report there and leaves the other's record as it was."""
    with client_in(tmp_path, today="2026-09-15") as client:
        mine = homework_from_a_note(
            store_of(client),
            course="Health",
            title="Course Guide Due",
            due_date=date(2026, 9, 9),
            note=HER_NOTE,
        )
        first = read(client, GUIDE_CARD)
        theirs = f"assignment-{review_form(first)['creation-0']}"
        save(client, first, **{"identity-0": "different"})
        report = read(
            client, "Assignments:\n09/14 Health - A: Homework: Course Guide Due Grade: Missing\n"
        )
        malformed = save(client, report, **{"identity-0": "same:assignment-no-such"})
        before_hers = (
            store_of(client).deadline_records(mine),
            store_of(client).status_reports(mine),
        )
        placed = save(client, report, **{"identity-0": f"same:{theirs}"})
        after_hers = (
            store_of(client).deadline_records(mine),
            store_of(client).status_reports(mine),
        )
        their_reports = store_of(client).status_reports(theirs)

    said = re.search(r'<p class="note" id="kept-separate-0">(.*?)</p>', report, re.S)
    assert said is not None
    assert " ".join(html.unescape(said.group(1)).split()) == KEPT_SEPARATE
    assert re.search(r'id="identity-question-0"[^>]*aria-describedby="[^"]*kept-separate-0', report)
    assert malformed.status_code == 422
    assert 'id="kept-separate-0"' in malformed.text
    assert placed.status_code == 303
    assert after_hers == before_hers
    assert [item.status for item in their_reports] == ["missing"]


def test_two_homework_of_one_name_with_no_answer_between_them_say_nothing_of_separation(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        school_row(client, "assignment-lab-a", "Science", "Lab Log", date(2026, 10, 1))
        school_row(client, "assignment-lab-b", "Science", "Lab Log", date(2026, 10, 1))
        page = read(
            client,
            "Homework for Wren\n- 10/01/2026 - Thursday\nScience - Due: Lab Log:\n",
        )

    assert question(page, 0) != ""
    assert 'id="kept-separate-0"' not in page


# ------------------------------------------------------------------ more than one card of it


def test_a_missing_line_in_the_same_text_goes_where_the_card_above_goes(
    tmp_path: pathlib.Path,
) -> None:
    """The Missing line is read apart and shown with the card it goes with; once homework is
    chosen on that card, the line's report goes there too, and nothing new is made."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        text = Q1_ASSIGNED + "\n" + Q1_MISSING
        page = read(client, text)
        picked = chosen(client, text, mine)
        saved = save(client, picked)
        rows = store_of(client).all_assignments()
        reports = store_of(client).status_reports(mine)

    assert offered(page, 0)
    assert not offered(page, 1)
    assert saved.status_code == 303
    assert [row.assignment_id for row in rows] == [mine]
    assert [(item.status, item.source_date_text) for item in reports] == [("missing", "10/05")]


def test_a_card_a_week_later_in_the_same_text_follows_the_choice_under_the_date_rules(
    tmp_path: pathlib.Path,
) -> None:
    """A second card of the name a week later asks the recurring question; once homework is
    chosen on the first card, it asks it against that homework's date, both names shown."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        text = Q1_ASSIGNED + "- 10/08/2026 - Thursday\n08 Algebra - Due: Q1 Check 3:\n"
        page = read(client, text)
        picked = chosen(client, text, mine)

    assert offered(page, 0)
    assert not offered(page, 1)
    assert 'id="occurrence-question-1"' in picked
    assert names(picked, 1) == (
        "The school calls this Q1 Check 3, 08 Algebra, due Thursday, October 8, 2026. "
        f"{SAVED_AS_QUIZ}"
    )


def test_a_typed_entry_can_be_pointed_at_homework_already_here(tmp_path: pathlib.Path) -> None:
    entry = {
        "course": "08 Algebra",
        "title": "Quiz 1",
        "assigned_on": "",
        "due_date": "2026-10-01",
        "kind": "HOMEWORK",
        "note": "",
    }
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = client.post("/parent/inbox/enter", data=entry).text
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        picked = press(client, looked.text, choose=f"0:{mine}")
        saved = save(client, picked.text)
        recorded = decisions(client)

    assert offered(page, 0)
    assert names(picked.text, 0) == (
        f"This entry names it Quiz 1, 08 Algebra, due Thursday, October 1, 2026. {SAVED_AS_QUIZ}"
    )
    assert saved.status_code == 303
    assert recorded == [("renamed", "08 Algebra", "Quiz 1", "2026-10-01", mine, None)]


def test_the_answer_kept_is_for_the_school_name_exactly_as_the_paste_pairs_it(
    tmp_path: pathlib.Path,
) -> None:
    """The search reads words in any case, but the answer it leads to is kept for the class
    and title as the paste pairs them, case kept: a row naming "q1 check 3" is other work."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        save(client, chosen(client, Q1_ASSIGNED, mine, query="PATTERNS"))
        other = read(
            client, "Homework for Wren\n- 10/01/2026 - Thursday\n08 Algebra - Due: q1 check 3:\n"
        )

    assert names(other, 0) == ""
    assert offered(other, 0)
    assert ">New</span>" in other


def test_choose_takes_nothing_when_the_homework_changed_since_the_results(
    tmp_path: pathlib.Path,
) -> None:
    """The homework's date moved after the results were shown: Choose picks nothing, says it
    changed, and shows it again as it stands now, to choose again."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = read(client, Q1_ASSIGNED)
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        store = store_of(client)
        with store._connection:
            store._connection.execute(
                "UPDATE assignments SET due_date = '2026-10-02' WHERE assignment_id = ?", (mine,)
            )
        refused = press(client, looked.text, choose=f"0:{mine}")
        again = press(client, refused.text, choose=f"0:{mine}")

    shown = html.unescape(refused.text)
    assert refused.status_code == 200
    assert "picked-0" not in review_form(refused.text)
    assert "That homework changed after it was shown here. It wasn't chosen." in shown
    assert found(refused.text, 0) == [mine]
    assert "Friday, October 2, 2026" in shown
    assert review_form(again.text)["picked-0"] == mine


def test_a_choice_made_on_a_question_that_changed_since_saves_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Homework was chosen on the Course Guide's question; before Save, another assignment of
    that name arrived, so the question isn't the one the choice was made on."""
    with client_in(tmp_path) as client:
        homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
        picked = chosen(client, GUIDE_CARD, packet, query="packet")
        school_row(client, "assignment-guide-copy", "Health", "Course Guide Due", date(2026, 9, 30))
        before = tables(client)
        refused = save(client, picked)
        after = tables(client)

    assert refused.status_code == 409
    assert after == before
    assert 'id="picked-unsaved-0"' in refused.text


def test_find_on_a_card_the_page_did_not_offer_changes_nothing(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        page = read(client, Q1_DUE)
        landed = press(client, page, find="0", **{"search-0": "check"})
        missing = press(client, page, find="7", **{"search-7": "check"})

    assert not offered(page, 0)
    assert landed.status_code == 422
    assert missing.status_code == 422
    assert found(landed.text, 0) == []


def test_choosing_a_result_takes_the_place_of_an_answer_ticked_on_the_question(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        guide = homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
        page = read(client, GUIDE_CARD)
        looked = press(
            client, page, find="0", **{"search-0": "packet", "identity-0": f"same:{guide}"}
        )
        picked = press(client, looked.text, choose=f"0:{packet}")

    assert review_form(looked.text)["identity-0"] == f"same:{guide}"
    assert review_form(picked.text)["picked-0"] == packet
    assert "identity-0" not in review_form(picked.text)


def test_the_search_leaves_out_homework_of_the_cards_own_name(tmp_path: pathlib.Path) -> None:
    """The card's question lists homework of its own class and title already; the search
    finds the rest."""
    with client_in(tmp_path) as client:
        homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        other = school_row(client, "assignment-guide-to-health", "Health", "Guide to health", None)
        page = read(client, GUIDE_CARD)
        looked = press(client, page, find="0", **{"search-0": "guide"})

    assert found(looked.text, 0) == [other]


def test_a_direct_caller_is_held_to_the_rules_for_a_choice(tmp_path: pathlib.Path) -> None:
    """``keep`` refuses a choice of homework with the card's own name, a choice beside an
    answer to the card's question, and any choice with no page's offers to hold it to,
    whether or not the card and the homework bear it out."""
    from blossom.candidates import readings_for
    from blossom.captures import candidate_basis
    from blossom.intake import IdentityAnswer, NotOffered, Pick, changes_for, keep

    with client_in(tmp_path) as client:
        guide = homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
        store: ProjectStateStore = store_of(client)
        now = store.instruction_moment()
        items = read_text(GUIDE_CARD, now=now[0], today=now[1]).items
        (card,) = changes_for(items, store)
        assert card.offers is not None
        rows = {row.assignment_id: row for row in store.all_assignments()}
        basis_of = {
            name: candidate_basis(readings_for(store, [rows[name]])) for name in (guide, packet)
        }
        own_name = keep(
            items,
            store,
            picks={0: Pick(guide, basis_of[guide], card.offers)},
            offered={0: card.offers},
            now=now[0],
            today=now[1],
        )
        beside = keep(
            items,
            store,
            identities={0: IdentityAnswer(guide, card.identity_shown, card.identity_basis)},
            picks={0: Pick(packet, basis_of[packet], card.offers)},
            offered={0: card.offers},
            now=now[0],
            today=now[1],
        )
        no_page = keep(
            items,
            store,
            picks={0: Pick(packet, "0" * 64, card.offers)},
            now=now[0],
            today=now[1],
        )
        sound_but_no_page = keep(
            items,
            store,
            picks={0: Pick(packet, basis_of[packet], card.offers)},
            now=now[0],
            today=now[1],
        )
        after = tables(client)

    assert isinstance(own_name, ChangedSinceShown)
    assert isinstance(beside, NotOffered)
    assert isinstance(no_page, NotOffered)
    assert isinstance(sound_but_no_page, NotOffered)
    assert after["intake_decisions"] == []


# ------------------------------------------------------------------ review of b3eb9bf

WEEKLY = (
    "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\nShow all steps.\n"
    "Tuesday 9/15/2026\nMath\nDue: Weekly practice:\nUse a pencil.\n"
    "Thursday 10/1/2026\nArt\nDue: Picture:\n"
)


def unavailable_text(page: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", page)).split())


@pytest.mark.parametrize(
    ("method", "problem"),
    [
        ("deadline_records_by_assignment", "claim"),
        ("intake_decisions", "decision"),
        ("school_instruction_readings", "instruction"),
    ],
)
def test_a_record_that_cant_be_read_while_the_review_is_redrawn_keeps_the_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, method: str, problem: str
) -> None:
    """The review is redrawn after a press and the comparison meets a record that can't be
    read: the page that reads no store keeps the paste, the other answers, and the homework
    chosen under another name, and nothing is written."""
    from blossom import intake
    from blossom.routes import inbox
    from blossom.stores.intake_decisions import UnreadableDecision
    from blossom.stores.project_state import UnreadableClaim
    from blossom.stores.school_instructions import UnreadableInstruction

    raised = {
        "claim": UnreadableClaim,
        "decision": UnreadableDecision,
        "instruction": UnreadableInstruction,
    }[problem]
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        before = tables(client)
        real = intake.changes_for

        def comparing(*args: object, **kwargs: object) -> object:
            # The redrawn page's comparison is the one that carries the instruction answers.
            if "instruction_answers" in kwargs:
                msg = "cannot be read"
                raise raised(msg)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(inbox, "changes_for", comparing)
        failed = press(client, page, find="0", **{"search-0": "patterns"})
        monkeypatch.undo()
        after = tables(client)

    shown = unavailable_text(failed.text)
    assert failed.status_code == 500
    assert after == before
    assert "Q1 Check 3" in shown
    assert f"homework already here under another name, with id {mine}" in shown
    assert method


@pytest.mark.parametrize("renamed_to", ["another-name", "the-cards-own-name"])
def test_choose_on_a_result_renamed_since_shows_it_as_it_stands(
    tmp_path: pathlib.Path, renamed_to: str
) -> None:
    """The quiz was renamed after the results were shown, so the words searched don't find
    it now. Choose picks nothing, and the quiz is shown under its new name all the same; it
    can be chosen again only while the card still offers it and it has another name."""
    title = "Unit One quiz" if renamed_to == "another-name" else "Q1 Check 3"
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = read(client, Q1_ASSIGNED)
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        store = store_of(client)
        with store._connection:
            store._connection.execute(
                "UPDATE assignments SET title = ? WHERE assignment_id = ?", (title, mine)
            )
        before = tables(client)
        refused = press(client, looked.text, choose=f"0:{mine}")
        after = tables(client)
        again = (
            press(client, refused.text, choose=f"0:{mine}")
            if renamed_to == "another-name"
            else None
        )

    shown = html.unescape(refused.text)
    assert refused.status_code == 200
    assert after == before
    assert "picked-0" not in review_form(refused.text)
    assert "That homework changed after it was shown here. It wasn't chosen." in shown
    assert title in shown
    assert mine in shown
    if again is None:
        assert found(refused.text, 0) == []
    else:
        assert found(refused.text, 0) == [mine]
        assert review_form(again.text)["picked-0"] == mine


@pytest.mark.parametrize("answer", [{"none-0": "1"}, {"apply-0-0": "1"}])
@pytest.mark.parametrize(
    ("pressed", "status"),
    [({"find": "2", "search-2": "picture"}, 200), ({"remove": "2"}, 422)],
    ids=["find", "a-press-the-page-refuses"],
)
def test_a_press_never_turns_an_answer_the_page_didnt_ask_into_one_it_did(
    tmp_path: pathlib.Path, answer: dict[str, str], pressed: dict[str, str], status: int
) -> None:
    """An instruction question carried over from another review, with the original page's
    signature, is refused by Save. Sent with a press instead, one the page takes or one it
    refuses, it stays unsaved: the page returned doesn't sign it as asked, and the Save
    after it writes nothing."""
    with client_in(tmp_path) as client:
        page = read(client, WEEKLY)
        original = review_form(page)
        second = client.post("/parent/inbox/keep", data={**original, "occurrence-1": "update"})
        asked = {
            name: value
            for name, value in review_form(second.text).items()
            if name.startswith(("instructions-", "instruction-", "row-"))
        }
        carried = {**original, **asked, "occurrence-1": "update", **answer}
        before = tables(client)
        direct = client.post("/parent/inbox/keep", data=carried)
        looked = client.post("/parent/inbox/find", data={**carried, **pressed})
        saved = client.post("/parent/inbox/keep", data=review_form(looked.text))
        after = tables(client)

    assert asked
    assert direct.status_code == 422
    assert looked.status_code == status
    assert saved.status_code != 303
    assert after == before


def test_an_answer_the_page_asked_survives_a_search_and_saves_once(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        page = read(client, WEEKLY)
        second = client.post(
            "/parent/inbox/keep", data={**review_form(page), "occurrence-1": "update"}
        )
        ticked = {**review_form(second.text), "apply-0-0": "1"}
        looked = client.post(
            "/parent/inbox/find", data={**ticked, "find": "2", "search-2": "picture"}
        )
        saved = client.post("/parent/inbox/keep", data=review_form(looked.text))
        before = tables(client)
        again = client.post("/parent/inbox/keep", data=review_form(looked.text))
        after = tables(client)

    assert second.status_code == 200
    assert looked.status_code == 200
    assert saved.status_code == 303
    assert again.status_code == 303
    assert after == before


def test_a_refused_save_keeps_the_search_words_and_the_choice_can_be_saved_after(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        store = store_of(client)
        with store._connection:
            store._connection.execute(
                "UPDATE assignments SET due_date = '2026-10-02' WHERE assignment_id = ?", (mine,)
            )
        before = tables(client)
        refused = save(client, page)
        after_refusal = tables(client)
        again = press(client, refused.text, choose=f"0:{mine}")
        saved = save(client, again.text)
        settled = tables(client)
        retried = save(client, again.text)
        after_retry = tables(client)

    assert refused.status_code == 409
    assert after_refusal == before
    assert review_form(refused.text)["search-0"] == "patterns"
    assert 'id="search-results-0"' in refused.text
    assert review_form(again.text)["search-0"] == "patterns"
    assert saved.status_code == 303
    assert retried.status_code == 303
    assert after_retry == settled


Q1_AND_PICTURE = Q1_ASSIGNED + "\nThursday 10/1/2026\nArt\nDue: Picture:\n"


@pytest.mark.parametrize("change", ["date-moved", "school-name-arrived"])
@pytest.mark.parametrize("searched_on", [0, 1], ids=["this-card", "another-card"])
def test_a_refused_choice_is_offered_again_on_its_own_card_whatever_another_card_shows(
    tmp_path: pathlib.Path, change: str, searched_on: int
) -> None:
    """Results shown on another card neither hide a card's refused choice nor offer it on a
    card that can't take it."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        looked = press(client, page, find=str(searched_on), **{"search-1": "patterns"})
        if change == "date-moved":
            store = store_of(client)
            with store._connection:
                store._connection.execute(
                    "UPDATE assignments SET due_date = '2026-10-02' WHERE assignment_id = ?",
                    (mine,),
                )
        else:
            school_row(
                client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1)
            )
        before = tables(client)
        refused = save(client, looked.text)
        after_refusal = tables(client)
        again = press(client, refused.text, choose=f"0:{mine}")
        after_press = tables(client)
        saved = save(client, again.text) if change == "date-moved" else None
        settled = tables(client)
        retried = save(client, again.text) if change == "date-moved" else None
        after_retry = tables(client)
        recorded = decisions(client)

    shown = html.unescape(refused.text)
    assert looked.status_code == 200
    assert refused.status_code == 409
    assert after_refusal == before
    assert after_press == before
    assert "picked-0" not in review_form(refused.text)
    assert review_form(refused.text)["search-0"] == "patterns"
    assert review_form(refused.text)["search-1"] == "patterns"
    assert found(refused.text, 1) == ([mine] if searched_on == 1 else [])
    if change == "date-moved":
        assert found(refused.text, 0) == [mine]
        assert "doesn't offer it as a choice now" not in shown
        assert ("choose it again" if searched_on == 1 else "among the results below") in shown
        assert again.status_code == 200
        assert review_form(again.text)["picked-0"] == mine
        assert saved is not None
        assert saved.status_code == 303
        assert retried is not None
        assert retried.status_code == 303
        assert after_retry == settled
        assert [kind for kind, *_ in recorded] == ["renamed"]
    else:
        assert found(refused.text, 0) == []
        assert "doesn't offer it as a choice now" in shown
        assert again.status_code == 422
        assert "picked-0" not in review_form(again.text)
        assert after_retry == before


def test_search_words_on_one_card_survive_a_question_left_open_on_another(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        patterns_quiz(client)
        page = read(client, GUIDE_CARD + Q1_ASSIGNED.split("\n", 1)[1])
        looked = press(client, page, find="1", **{"search-1": "patterns"})
        unanswered = save(client, looked.text)

    assert unanswered.status_code == 200
    assert review_form(unanswered.text)["search-1"] == "patterns"


def test_a_choice_the_card_no_longer_offers_is_shown_without_a_choose_press(
    tmp_path: pathlib.Path,
) -> None:
    """Homework of the school's own name arrived before Save: the card lands there now, so
    the choice made on it is said as not saved, with no press that can't succeed."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        refused = save(client, page)

    assert refused.status_code == 409
    assert 'id="picked-unsaved-0"' in refused.text
    assert mine in refused.text
    assert found(refused.text, 0) == []


@pytest.mark.parametrize("damage", ["decision", "instruction", "file"])
def test_a_press_that_meets_a_record_it_cant_read_keeps_the_paste_and_the_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Find or Choose meets an answer or an instruction that can't be read, or a file that
    refuses the read: the page that reads no store keeps the paste and the choice, and
    nothing is written. Repaired, the paste can be reviewed and saved afresh."""
    from blossom.stores.school_instructions import UnreadableInstruction

    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_ASSIGNED, mine)
        looked = press(client, page, find="0", **{"search-0": "patterns"})
        store = store_of(client)
        if damage == "decision":
            keep_answer(store, a_renamed_answer(mine))
            with store._connection:
                store._connection.execute("UPDATE intake_decisions SET basis = 'bent'")
        elif damage == "instruction":

            def unreadable(*_: object, **__: object) -> object:
                msg = "cannot be read"
                raise UnreadableInstruction(msg)

            monkeypatch.setattr(store, "school_instruction_readings", unreadable)
        else:

            def refused_read(*_: object, **__: object) -> object:
                msg = "the disk refused"
                raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store, "one_assignment", refused_read)
        before = tables(client)
        failed = (
            press(client, looked.text, choose=f"0:{mine}")
            if damage == "file"
            else press(client, page, find="0", **{"search-0": "patterns"})
        )
        after = tables(client)
        monkeypatch.undo()
        if damage == "decision":
            with store._connection:
                store._connection.execute("DELETE FROM intake_decisions")
        fresh = chosen(client, Q1_ASSIGNED, mine)
        saved = save(client, fresh)

    shown = unavailable_text(failed.text)
    assert failed.status_code == 500
    assert after == before
    assert "Q1 Check 3" in shown
    assert mine in shown
    assert saved.status_code == 303


def test_next_and_previous_take_only_the_pages_the_page_offered(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        for place in range(41):
            school_row(
                client, f"assignment-warmup-{place:02}", "Warmups", f"Warmup {place:02}", None
            )
        page = read(client, Q1_ASSIGNED)
        first = press(client, page, find="0", **{"search-0": "warmup"})
        before = tables(client)
        jumped = press(client, first.text, results_page="0:3")
        refused = [
            press(client, first.text, results_page=value).status_code
            for value in ("0:0", "0:-1", "1:2", "0:x")
        ]
        second = press(client, first.text, results_page="0:2")
        third = press(client, second.text, results_page="0:3")
        back = press(client, third.text, results_page="0:2")
        after = tables(client)

    assert 'name="results_page" value="0:3"' not in first.text
    assert jumped.status_code == 422
    assert review_form(jumped.text)["search-0"] == "warmup"
    assert refused == [422, 422, 422, 422]
    assert second.status_code == third.status_code == back.status_code == 200
    assert found(third.text, 0) == ["assignment-warmup-40"]
    assert found(back.text, 0) == found(second.text, 0)
    assert after == before


def move_due_date(client: TestClient, target: str, day: str = "2026-10-02") -> None:
    store = store_of(client)
    with store._connection:
        store._connection.execute(
            "UPDATE assignments SET due_date = ? WHERE assignment_id = ?", (day, target)
        )


def picture_project(client: TestClient) -> str:
    """Other homework of hers, found on the Picture card by the word "picture"."""
    return homework_from_a_note(
        store_of(client), course="Art", title="Picture project", due_date=date(2026, 10, 1)
    )


def pressed_on_card_1(client: TestClient, looked: str, then: str, other: str) -> Answer:
    """A press or a refused save on card 1, from a page showing card 1's results."""
    if then == "press-refused":
        return press(client, looked, find="9")
    if then == "save-refused":
        return save(client, looked, **{"identity-1": "different"})
    if then == "find-on-another-card":
        return press(client, looked, find="1", **{"search-1": "picture"})
    picked = press(client, looked, choose=f"1:{other}")
    if then == "remove-on-another-card":
        return press(client, picked.text, remove="1")
    return picked


@pytest.mark.parametrize(
    "then",
    [
        "find-on-another-card",
        "choose-on-another-card",
        "changed-on-another-card",
        "remove-on-another-card",
        "press-refused",
        "save-refused",
    ],
)
def test_a_refused_choice_stays_on_its_card_through_later_presses(
    tmp_path: pathlib.Path, then: str
) -> None:
    """A choice the save refused is shown again on its own card after later presses on
    another card and saves refused again, until it's chosen again and saved."""
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = picture_project(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        move_due_date(client, mine)
        refused = save(client, page)
        looked = press(client, refused.text, find="1", **{"search-1": "picture"})
        if then == "changed-on-another-card":
            move_due_date(client, other, "2026-10-03")
        before = tables(client)
        later = pressed_on_card_1(client, looked.text, then, other)
        forged = press(client, later.text, choose=f"1:{mine}")
        after_presses = tables(client)
        again = press(client, later.text, choose=f"0:{mine}")
        saved = save(client, again.text)
        settled = tables(client)
        retried = save(client, again.text)
        after_retry = tables(client)

    assert refused.status_code == 409
    assert 'id="picked-unsaved-0"' in looked.text
    assert later.status_code == (422 if then.endswith("-refused") else 200)
    assert after_presses == before
    assert 'id="picked-unsaved-0"' in later.text
    assert "Friday, October 2, 2026" in html.unescape(later.text)
    assert found(later.text, 0) == [mine]
    assert forged.status_code == 422
    assert review_form(again.text)["picked-0"] == mine
    assert saved.status_code == 303
    assert retried.status_code == 303
    assert after_retry == settled


def test_a_refused_choice_stays_through_saves_that_leave_a_question_open(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
        mine = patterns_quiz(client)
        page = chosen(client, GUIDE_CARD + Q1_ASSIGNED.split("\n", 1)[1], mine, key=1)
        move_due_date(client, mine)
        before = tables(client)
        first = save(client, page)
        second = save(client, first.text)
        third = save(client, second.text)
        after = tables(client)

    assert after == before
    assert 'id="picked-unsaved-1"' in first.text
    for answered in (second, third):
        assert answered.status_code == 200
        assert 'id="picked-unsaved-1"' in answered.text
        assert found(answered.text, 1) == [mine]


def test_a_choice_the_card_cant_take_stays_said_without_a_press_on_later_pages(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        school_row(client, "assignment-q1-check-3", "08 Algebra", "Q1 Check 3", date(2026, 10, 1))
        refused = save(client, page)
        later = press(client, refused.text, find="1", **{"search-1": "patterns"})
        forged = press(client, later.text, choose=f"0:{mine}")

    assert refused.status_code == 409
    assert later.status_code == 200
    assert 'id="picked-unsaved-0"' in later.text
    assert "doesn't offer it as a choice now" in html.unescape(later.text)
    assert found(later.text, 0) == []
    assert found(later.text, 1) == [mine]
    assert forged.status_code == 422


def test_a_choose_that_found_a_change_stays_on_its_card_after_a_press_elsewhere(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = picture_project(client)
        looked = press(client, read(client, Q1_AND_PICTURE), find="0", **{"search-0": "patterns"})
        move_due_date(client, mine)
        missed = press(client, looked.text, choose=f"0:{mine}")
        later = press(client, missed.text, find="1", **{"search-1": "picture"})
        again = press(client, later.text, choose=f"0:{mine}")
        saved = save(client, again.text)
        settled = tables(client)
        retried = save(client, again.text)
        after_retry = tables(client)

    changed = "That homework changed after it was shown here. It wasn't chosen."
    assert changed in html.unescape(missed.text)
    assert changed in html.unescape(later.text)
    assert found(later.text, 0) == [mine]
    assert found(later.text, 1) == [other]
    assert review_form(again.text)["picked-0"] == mine
    assert saved.status_code == 303
    assert retried.status_code == 303
    assert after_retry == settled


@pytest.mark.parametrize("taken", ["another-choice", "an-answer"])
def test_a_refused_choice_goes_once_its_card_takes_a_choice_or_an_answer(
    tmp_path: pathlib.Path, taken: str
) -> None:
    with client_in(tmp_path) as client:
        if taken == "another-choice":
            mine = patterns_quiz(client)
            other = picture_project(client)
            page = chosen(client, Q1_AND_PICTURE, mine)
            move_due_date(client, mine)
            refused = save(client, page)
            looked = press(client, refused.text, find="0", **{"search-0": "picture"})
            taken_page = press(client, looked.text, choose=f"0:{other}")
            later = press(client, taken_page.text, remove="0")
        else:
            homework_from_a_note(store_of(client), course="Health", title="Course Guide Due")
            packet = school_row(client, "assignment-health-packet", "Health", "Packet 1", None)
            page = chosen(client, GUIDE_CARD, packet, query="packet")
            school_row(
                client, "assignment-guide-copy", "Health", "Course Guide Due", date(2026, 9, 30)
            )
            refused = save(client, page)
            taken_page = press(
                client, refused.text, find="0", **{"search-0": "packet", "identity-0": "different"}
            )
            later = press(client, taken_page.text, find="0", **{"search-0": "packet"})

    assert refused.status_code == 409
    assert 'id="picked-unsaved-0"' in refused.text
    assert taken_page.status_code == later.status_code == 200
    assert 'id="picked-unsaved-0"' not in taken_page.text
    assert 'id="picked-unsaved-0"' not in later.text


@pytest.mark.parametrize("fails", ["the-file", "a-claim", "a-decision", "an-instruction"])
def test_the_page_that_reads_no_store_says_a_refused_choice_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    from blossom import intake
    from blossom.routes import inbox
    from blossom.stores.project_state import UnreadableClaim
    from blossom.stores.school_instructions import UnreadableInstruction

    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        move_due_date(client, mine)
        refused = save(client, page)
        before = tables(client)
        if fails == "the-file":

            def refused_read(*_: object, **__: object) -> object:
                msg = "the disk refused"
                raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store_of(client), "one_assignment", refused_read)
        else:
            real = intake.changes_for
            raised = {
                "a-claim": UnreadableClaim,
                "a-decision": UnreadableDecision,
                "an-instruction": UnreadableInstruction,
            }[fails]

            def comparing(*args: object, **kwargs: object) -> object:
                if "instruction_answers" in kwargs:
                    msg = "cannot be read"
                    raise raised(msg)
                return real(*args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(inbox, "changes_for", comparing)
        failed = press(client, refused.text, find="1", **{"search-1": "patterns"})
        monkeypatch.undo()
        after = tables(client)

    assert failed.status_code == 500
    assert after == before
    said = f"Not saved: the homework with id {mine}, chosen under another name."
    assert said in unavailable_text(failed.text)


def test_a_choose_that_finds_a_change_takes_the_place_of_the_choice_carried_on_its_card(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        other = picture_project(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        move_due_date(client, mine)
        refused = save(client, page)
        looked = press(client, refused.text, find="0", **{"search-0": "picture"})
        move_due_date(client, other, "2026-10-03")
        missed = press(client, looked.text, choose=f"0:{other}")

    assert sorted(found(looked.text, 0)) == sorted([mine, other])
    assert missed.status_code == 200
    assert "That homework changed after it was shown here." in html.unescape(missed.text)
    assert found(missed.text, 0) == [other]


def test_the_page_that_reads_no_store_names_a_card_sent_with_only_a_choice_not_taken(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blossom import intake
    from blossom.routes import inbox
    from blossom.stores.project_state import UnreadableClaim

    with client_in(tmp_path) as client:
        mine = patterns_quiz(client)
        page = chosen(client, Q1_AND_PICTURE, mine)
        move_due_date(client, mine)
        refused = save(client, page)
        sent = {
            name: value for name, value in review_form(refused.text).items() if name != "kind-0"
        }
        real = intake.changes_for

        def comparing(*args: object, **kwargs: object) -> object:
            if "instruction_answers" in kwargs:
                msg = "cannot be read"
                raise UnreadableClaim(msg)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(inbox, "changes_for", comparing)
        failed = client.post("/parent/inbox/find", data={**sent, "find": "1"})
        monkeypatch.undo()

    assert failed.status_code == 500
    said = f"Card 0: no answer about the assignment. Not saved: the homework with id {mine}"
    assert said in unavailable_text(failed.text)
