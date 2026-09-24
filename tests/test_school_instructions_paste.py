"""The school's instructions through the paste review, from the pasted text to the record.

Every case here runs the whole path a parent's paste takes: the reader, the
review page and its own form, the save, and what the record keeps. A card's
instruction is kept once for its assignment whichever card or paste brings
it; a new, different one asks, once for the assignment, which apply; keeping
what applies still keeps what the school said; and an answer made against
another revision, or beside a stale card, writes nothing of the paste.
"""

import html
import pathlib
import re
import sqlite3
from datetime import UTC, date, datetime
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.intake import ChangedSinceShown, keep, read_text
from blossom.routes.inbox import (
    CHANGED_SINCE_SHOWN,
    INSTRUCTION_FORM_UNREADABLE,
    INSTRUCTIONS_CONTRADICT,
    STORE_REFUSED,
)
from blossom.school_instructions import (
    InstructionChoice,
    InstructionSeen,
    InstructionsStanding,
    to_wire,
)
from blossom.settings import Settings
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    SAME_ORIGIN,
    Answer,
    as_a_browser_sends,
    fixture_settings,
    store_of,
)

A = "Patterns, if-then statements, first proofs."
B = "Patterns and if-then statements only."
QUESTION = "The school's instructions for Q1 Check 3: which apply now?"

ASSIGNED_WEEK = f"""Homework for Wren
- 09/23/2026 - Wednesday
07 Algebra - Assigned: Q1 Check 3: (Due:10/01/2026)
{A}
"""
DUE_WEEK = f"""Homework for Wren
- 10/01/2026 - Thursday
07 Algebra - Due: Q1 Check 3:
{A}
"""
DUE_WEEK_CHANGED = f"""Homework for Wren
- 10/01/2026 - Thursday
07 Algebra - Due: Q1 Check 3:
{B}
"""
BOTH_CARDS = f"""Homework for Wren
- 09/23/2026 - Wednesday
07 Algebra - Assigned: Q1 Check 3: (Due:10/01/2026)
{A}
- 10/01/2026 - Thursday
07 Algebra - Due: Q1 Check 3:
{B}
"""
BOTH_CARDS_REVERSED = f"""Homework for Wren
- 10/01/2026 - Thursday
07 Algebra - Due: Q1 Check 3:
{B}
- 09/23/2026 - Wednesday
07 Algebra - Assigned: Q1 Check 3: (Due:10/01/2026)
{A}
"""
ANOTHER = """Homework for Wren
- 09/24/2026 - Thursday
Health - Assigned: Course Guide Due: (Due:09/29/2026)
Return the course guide signed by a parent after reading it.
"""
ANOTHER_LATER = """- 10/13/2026 - Tuesday
Health - Assigned: Course Guide Due: (Due:10/20/2026)
"""


def settings_in(tmp_path: pathlib.Path) -> Settings:
    return fixture_settings(
        BLOSSOM_TODAY="2026-09-24",
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


def client_in(tmp_path: pathlib.Path) -> TestClient:
    return TestClient(
        create_app(settings_in(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
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
    assert text is not None
    fields["text"] = html.unescape(text.group(1))
    return fields


def boxes(page: str, key: str) -> list[str]:
    """The labels of the instruction boxes on one card, in the order shown."""
    fieldset = page.split(f'name="instructions-{key}"', 1)[1].split("</fieldset>", 1)[0]
    return [
        html.unescape(label)
        for label in re.findall(
            rf'<input type="checkbox" name="apply-{key}-\d+" value="1"[^>]*> '
            r'<span><q class="authored-text">(.*?)</q>',
            fieldset,
            re.S,
        )
    ]


def ticked(page: str, key: str) -> list[str]:
    """The labels of the instruction boxes ticked on one card."""
    fieldset = page.split(f'name="instructions-{key}"', 1)[1].split("</fieldset>", 1)[0]
    return [
        html.unescape(label)
        for label in re.findall(
            rf'<input type="checkbox" name="apply-{key}-\d+" value="1" checked> '
            r'<span><q class="authored-text">(.*?)</q>',
            fieldset,
            re.S,
        )
    ]


def unsaved(page: str, key: str) -> list[str]:
    """The words of a choice the page says was not saved, on one card."""
    found = re.search(rf'<p class="problem" id="instructions-problem-{key}">(.*?)</p>', page, re.S)
    assert found is not None
    return [html.unescape(text) for text in re.findall(r"<q[^>]*>(.*?)</q>", found.group(1))]


def saved(client: TestClient, text: str, **answers: str) -> Answer:
    page = client.post("/parent/inbox/read", data={"text": text}).text
    return client.post("/parent/inbox/keep", data={**review_form(page), **answers})


def standing(client: TestClient) -> InstructionsStanding:
    store = store_of(client)
    rows = [row for row in store.all_assignments() if row.title == "Q1 Check 3"]
    assert len(rows) == 1
    return store.school_instruction_readings([rows[0].assignment_id]).readable[
        rows[0].assignment_id
    ]


def tables(client: TestClient) -> dict[str, list[tuple[object, ...]]]:
    connection = store_of(client)._connection
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in ("assignments", "date_claims", "status_reports", "school_instructions")
    }


@pytest.mark.parametrize("first", ["assigned", "due"])
def test_an_assigned_card_and_its_due_card_keep_one_instruction_in_either_order(
    tmp_path: pathlib.Path, first: str
) -> None:
    order = [ASSIGNED_WEEK, DUE_WEEK] if first == "assigned" else [DUE_WEEK, ASSIGNED_WEEK]
    with client_in(tmp_path) as client:
        one = saved(client, order[0])
        two = saved(client, order[1])
        found = standing(client)
        again = saved(client, order[1])

    assert one.status_code == two.status_code == again.status_code == 303
    assert found.texts == (A,)
    assert found.history == found.awaiting == ()
    assert found.current[0].card == first
    assert found.current[0].imported_by == "household"
    assert again.headers["location"].endswith("added=0&updated=0&unchanged=1")


def test_a_different_instruction_asks_once_and_keeping_the_saved_one_keeps_the_new_one(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = review_form(page)
        before = tables(client)
        unanswered = client.post("/parent/inbox/keep", data=form)
        after_unanswered = tables(client)
        kept = client.post("/parent/inbox/keep", data={**form, "apply-0-0": "1"})
        found = standing(client)
        after_kept = tables(client)
        retried = client.post("/parent/inbox/keep", data={**form, "apply-0-0": "1"})
        after_retry = tables(client)

    assert page.count(QUESTION) == 1
    assert boxes(page, "0") == [A, B]
    assert 'name="none-0" value="1">' in page
    assert "checked" not in page.split(QUESTION, 1)[1].split("</fieldset>", 1)[0]
    assert "Read on the school's card for October 1" in page
    assert unanswered.status_code == 200
    assert after_unanswered == before
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=0&updated=1&unchanged=0")
    assert found.texts == (A,)
    assert [item.text for item in found.history] == [B]
    assert found.revision == 2
    assert found.history[0].settled_by == "household"
    assert retried.status_code == 303
    assert retried.headers["location"].endswith("added=0&updated=0&unchanged=1")
    assert after_retry == after_kept


@pytest.mark.parametrize("text", [BOTH_CARDS, BOTH_CARDS_REVERSED])
def test_both_cards_of_one_assignment_in_one_paste_are_one_decision(
    tmp_path: pathlib.Path, text: str
) -> None:
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text
        form = review_form(page)
        key = form_key(form)
        shown = boxes(page, key)
        answers = {f"apply-{key}-{shown.index(A)}": "1", f"apply-{key}-{shown.index(B)}": "1"}
        kept = client.post("/parent/inbox/keep", data={**form, **answers})
        found = standing(client)

    assert page.count(QUESTION) == 1
    assert sorted(shown) == sorted([A, B])
    assert kept.status_code == 303
    assert found.texts == tuple(sorted([A, B]))


def form_key(form: dict[str, str]) -> str:
    keys = [
        name.removeprefix("instructions-")
        for name in form
        if re.fullmatch(r"instructions-\d+", name)
    ]
    assert len(keys) == 1, keys
    return keys[0]


def test_none_applies_is_an_answer_and_ticking_it_beside_one_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = review_form(page)
        before = tables(client)
        both = client.post("/parent/inbox/keep", data={**form, "apply-0-1": "1", "none-0": "1"})
        after_both = tables(client)
        none = client.post("/parent/inbox/keep", data={**form, "none-0": "1"})
        found = standing(client)

    assert both.status_code == 422
    assert str(escape(INSTRUCTIONS_CONTRADICT)) in both.text
    assert ticked(both.text, "0") == [B]
    assert 'name="none-0" value="1" checked>' in both.text
    assert after_both == before
    assert none.status_code == 303
    assert found.texts == ()
    assert sorted(item.text for item in found.history) == sorted([A, B])


def test_an_old_form_after_a_change_back_is_refused_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A current and B new: the form is made. B is chosen, then A again, elsewhere. The old
    form, whose texts and states match what stands, asks for B and is refused."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        old = {**review_form(page), "apply-0-1": "1"}
        saved(client, DUE_WEEK_CHANGED, **{"apply-0-0": "1"})
        store = store_of(client)
        name = next(
            row.assignment_id for row in store.all_assignments() if row.title == "Q1 Check 3"
        )
        now, today = datetime(2026, 9, 24, 20, 0, tzinfo=UTC), date(2026, 9, 24)
        store.settle_school_instructions(
            name,
            [],
            InstructionChoice(2, (A, B), frozenset({B})),
            authored_by="parent",
            now=now,
            today=today,
        )
        store.settle_school_instructions(
            name,
            [],
            InstructionChoice(3, (A, B), frozenset({A})),
            authored_by="parent",
            now=now,
            today=today,
        )
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=old)
        after = tables(client)
        found = standing(client)

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_SHOWN)) in refused.text
    assert unsaved(refused.text, "0") == [B]
    assert ticked(refused.text, "0") == []
    assert after == before
    assert found.texts == (A,)
    assert found.revision == 4


def test_a_valid_card_beside_a_stale_one_commits_nothing(tmp_path: pathlib.Path) -> None:
    """The paste holds a new assignment that needs nothing and a card whose instruction
    choice another action overtook: nothing of the paste is kept."""
    both = ANOTHER + DUE_WEEK_CHANGED.removeprefix("Homework for Wren\n")
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": both}).text
        form = review_form(page)
        key = form_key(form)
        answered = {**form, f"apply-{key}-1": "1"}
        store = store_of(client)
        name = next(
            row.assignment_id for row in store.all_assignments() if row.title == "Q1 Check 3"
        )
        store.settle_school_instructions(
            name,
            [InstructionSeen("Bring a calculator.", None)],
            InstructionChoice(1, (A, "Bring a calculator."), frozenset({A})),
            authored_by="parent",
            now=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
            today=date(2026, 9, 24),
        )
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=answered)
        after = tables(client)
        titles = {row.title for row in store.all_assignments()}

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_SHOWN)) in refused.text
    assert after == before
    assert "Course Guide Due" not in titles


def test_a_direct_caller_meets_the_same_batch_rule(tmp_path: pathlib.Path) -> None:
    """``keep`` itself refuses a stale choice beside a valid card, whole, and hands back what
    changed; it keeps the first instruction of a new assignment without a choice."""
    from tests.support import fixture_clock

    store = ProjectStateStore.open(tmp_path / "record.sqlite3", fixture_clock())
    now, today = datetime(2026, 9, 24, 1, 0, tzinfo=UTC), date(2026, 9, 23)
    first = read_text(ASSIGNED_WEEK, now=now, today=today)
    keep(first.items, store, imported_by="parent", now=now, today=today)
    later = read_text(
        ANOTHER + DUE_WEEK_CHANGED.removeprefix("Homework for Wren\n"), now=now, today=today
    )
    keys = [index for index, item in enumerate(later.items) if item.title == "Q1 Check 3"]
    stale = InstructionChoice(shown_revision=0, shown=(A, B), applies=frozenset({B}))
    before = {
        name: store._connection.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
        for name in ("assignments", "school_instructions")
    }

    outcome = keep(
        later.items,
        store,
        instruction_answers={keys[0]: stale},
        imported_by="parent",
        now=now,
        today=today,
    )
    after = {
        name: store._connection.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
        for name in before
    }

    assert isinstance(outcome, ChangedSinceShown)
    assert after == before


def test_a_page_returned_for_another_question_keeps_the_choice_by_its_words(
    tmp_path: pathlib.Path,
) -> None:
    """The instruction is answered and a card beside it still asks whether it is the same
    homework: nothing is saved, and the page returned shows the answer as made."""
    text = DUE_WEEK_CHANGED + ANOTHER_LATER
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        saved(client, ANOTHER)
        page = client.post("/parent/inbox/read", data={"text": text}).text
        form = review_form(page)
        key = form_key(form)
        before = tables(client)
        returned = client.post("/parent/inbox/keep", data={**form, f"apply-{key}-1": "1"})
        after = tables(client)

    assert "Which is it?" in page
    assert ticked(page, key) == []
    assert returned.status_code == 200
    assert after == before
    assert ticked(returned.text, key) == [B]


def test_a_stale_choice_beside_an_open_question_is_refused_and_not_shown_as_made(
    tmp_path: pathlib.Path,
) -> None:
    """Another question is still open on the page, and the instruction choice was overtaken:
    the choice is refused, and the page asks it afresh with nothing ticked."""
    text = DUE_WEEK_CHANGED + ANOTHER_LATER
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        saved(client, ANOTHER)
        page = client.post("/parent/inbox/read", data={"text": text}).text
        form = review_form(page)
        key = form_key(form)
        store = store_of(client)
        name = next(
            row.assignment_id for row in store.all_assignments() if row.title == "Q1 Check 3"
        )
        store.settle_school_instructions(
            name,
            [InstructionSeen("Bring a calculator.", None)],
            InstructionChoice(1, (A, "Bring a calculator."), frozenset({A})),
            authored_by="parent",
            now=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
            today=date(2026, 9, 24),
        )
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data={**form, f"apply-{key}-1": "1"})
        after = tables(client)

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_SHOWN)) in refused.text
    assert after == before
    assert boxes(refused.text, key) == [A, "Bring a calculator.", B]
    assert ticked(refused.text, key) == []
    assert unsaved(refused.text, key) == [B]
    assert "Needs your answer" in refused.text


def test_a_card_that_asks_nothing_says_how_each_instruction_it_brings_stands(
    tmp_path: pathlib.Path,
) -> None:
    """A applies and B was said before; a text brings both again, one under each card of
    the assignment, which the reader reads as one. No question is put, and the card lists
    both with how each stands and where this text shows it."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        saved(client, DUE_WEEK_CHANGED, **{"apply-0-0": "1"})
        before = tables(client)
        page = client.post("/parent/inbox/read", data={"text": BOTH_CARDS}).text
        again = client.post("/parent/inbox/keep", data=review_form(page))
        after = tables(client)

    assert QUESTION not in page
    assert (
        f'From the school: <q class="authored-text">{A}</q> '
        '<span class="source">Saved, applies now. Read on the school\'s card for September 23.'
    ) in page
    assert (
        f'From the school: <q class="authored-text">{B}</q> '
        '<span class="source">Saved earlier, does not apply. Read on the school\'s card for '
        "October 1."
    ) in page
    assert page.count("From the school:") == 2
    assert again.headers["location"].endswith("added=0&updated=0&unchanged=1")
    assert after == before


WEEKLY_SAVED = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\n"
WEEKLY_TWICE = (
    "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\nShow your work.\n"
    "Monday 9/14/2026\nMath\nAssigned: Weekly practice: (Due:09/15/2026)\nUse a pencil.\n"
)


def test_two_cards_said_to_be_the_same_homework_are_one_question_on_the_first(
    tmp_path: pathlib.Path,
) -> None:
    """Two cards a week apart, each with its own instruction, both said to be the saved
    homework: one question lists both, on the first card, and the second points to it."""
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_SAVED)
        page = client.post("/parent/inbox/read", data={"text": WEEKLY_TWICE}).text
        same = {"occurrence-0": "update", "occurrence-1": "update"}
        asked = client.post("/parent/inbox/keep", data={**review_form(page), **same})
        form = review_form(asked.text)
        key = form_key(form)
        shown = boxes(asked.text, key)
        answer = {f"apply-{key}-{shown.index('Use a pencil.')}": "1"}
        kept = client.post("/parent/inbox/keep", data={**form, **same, **answer})
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Weekly practice")
        found = store.school_instruction_readings([row.assignment_id]).readable[row.assignment_id]

    assert asked.status_code == 200
    assert key == "0"
    assert sorted(shown) == ["Show your work.", "Use a pencil."]
    assert asked.text.count("which apply now?") == 1
    assert "shown on the first card for this assignment" in asked.text
    assert kept.status_code == 303
    assert found.texts == ("Use a pencil.",)
    assert [item.text for item in found.history] == ["Show your work."]


# ------------------------------------------------------------ the words as a browser sends them

MULTI_A = "Patterns, if-then statements, first proofs.\nShow each step."
MULTI_B = 'Only the "if-then" part.\nSkip the proofs \U0001f33c.'


@pytest.mark.parametrize("choice", ["saved", "new", "none"])
def test_a_browser_sends_multiline_instructions_back_as_they_were(
    tmp_path: pathlib.Path, choice: str
) -> None:
    """A browser sends every line break as a carriage return and a line feed. The words ride
    on the wire, so a saved instruction, a new one, or none applying is saved as chosen, the
    words kept exactly as the school wrote them, and the same submission again adds nothing."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A, MULTI_A))
        page = client.post(
            "/parent/inbox/read", data={"text": DUE_WEEK_CHANGED.replace(B, MULTI_B)}
        ).text
        shown = boxes(page, "0")
        picked = {"saved": MULTI_A, "new": MULTI_B}.get(choice)
        answer = {"none-0": "1"} if picked is None else {f"apply-0-{shown.index(picked)}": "1"}
        sent = as_a_browser_sends({**review_form(page), **answer})
        first = client.post("/parent/inbox/keep", data=sent)
        after_first = tables(client)
        again = client.post("/parent/inbox/keep", data=sent)
        after_again = tables(client)
        found = standing(client)

    assert sorted(shown) == sorted([MULTI_A, MULTI_B])
    assert first.status_code == 303
    assert again.status_code == 303
    assert again.headers["location"].endswith("added=0&updated=0&unchanged=1")
    assert after_again == after_first
    assert found.texts == (() if picked is None else (picked,))
    assert sorted(item.text for item in found.kept) == sorted([MULTI_A, MULTI_B])


# ------------------------------------------------------------------ a contradiction gone stale


@pytest.mark.parametrize("elsewhere", ["another-chosen", "a-change-back"])
def test_a_contradiction_made_against_changed_instructions_is_not_put_right_onto_them(
    tmp_path: pathlib.Path, elsewhere: str
) -> None:
    """The page ticks A and that none applies, which contradict; before it is sent, the
    instructions change elsewhere. The page says they changed, says the answer as not saved,
    and ticks nothing, so undoing the contradiction alone chooses nothing, and what was
    chosen elsewhere stands."""
    third = DUE_WEEK_CHANGED.replace(B, "Third instruction.")
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        saved(client, DUE_WEEK_CHANGED, **{"apply-0-0": "1"})
        page = client.post("/parent/inbox/read", data={"text": third}).text
        form = review_form(page)
        key = form_key(form)
        shown = boxes(page, key)
        old = {**form, f"apply-{key}-{shown.index(A)}": "1", f"none-{key}": "1"}
        store = store_of(client)
        head = standing(client)
        name = head.current[0].assignment_id
        picks = (
            [frozenset({B})] if elsewhere == "another-chosen" else [frozenset({B}), frozenset({A})]
        )
        for number, pick in enumerate(picks):
            store.settle_school_instructions(
                name,
                [],
                InstructionChoice(head.revision + number, (A, B), pick),
                authored_by="parent",
                now=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
                today=date(2026, 9, 24),
            )
        chosen_elsewhere = standing(client).texts
        refused = client.post("/parent/inbox/keep", data=old)
        fresh = review_form(refused.text)
        corrected = client.post("/parent/inbox/keep", data=fresh)
        found = standing(client)

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_SHOWN)) in refused.text
    assert unsaved(refused.text, key) == [A]
    assert "that no school instruction applies" in refused.text
    assert ticked(refused.text, key) == []
    assert f'name="none-{key}" value="1" checked' not in refused.text
    assert not any(name.startswith(("apply-", "none-")) for name in fresh)
    assert corrected.status_code == 200
    assert found.texts == chosen_elsewhere


# ------------------------------------------------------------------ a paste the file refuses


@pytest.mark.parametrize(
    "failure", ["before-the-instructions", "during-the-instructions", "unreadable"]
)
def test_a_paste_not_saved_keeps_the_text_and_every_answer_and_is_tried_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A write the file refuses, before or while the school's instructions are written, or
    instructions that cannot be read, leave the whole text unsaved: the page that reads no
    store keeps the text and the words chosen, markup and line breaks shown as written, with
    the focus on what happened. Nothing is tried again, and nothing is read after."""
    words = "Use the <b>blue</b> sheet.\nNot the red one."
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post(
            "/parent/inbox/read", data={"text": DUE_WEEK_CHANGED.replace(B, words)}
        ).text
        form = {**review_form(page), f"apply-0-{boxes(page, '0').index(words)}": "1"}
        store = store_of(client)
        calls: list[str] = []

        def refuse(*_: object, **__: object) -> None:
            calls.append(failure)
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        if failure == "before-the-instructions":
            monkeypatch.setattr(store, "put_on_record", refuse)
        elif failure == "during-the-instructions":
            monkeypatch.setattr(store, "settle_school_instructions", refuse)
        else:
            store._connection.execute("UPDATE school_instructions SET state = 'bent'")
            store._connection.commit()
        before = tables(client)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        answer = client.post("/parent/inbox/keep", data=form)
        store._connection.set_trace_callback(None)
        left_open = store._connection.in_transaction
        after = tables(client)

    # The write is rolled back, or the refusing read came before any write began; either way
    # the page that answers reads nothing after it.
    last = statements[-1].strip().upper()
    assert answer.status_code == 500
    assert after == before
    assert not left_open
    assert calls == ([] if failure == "unreadable" else [failure])
    if failure == "unreadable":
        assert "FROM SCHOOL_INSTRUCTIONS" in last
    else:
        assert last == "ROLLBACK"
    assert answer.text.count("autofocus") == 1
    assert '<a href="#kept-answers">' in answer.text
    assert 'id="kept-answers"' in answer.text
    assert (
        '<q class="authored-text">Use the &lt;b&gt;blue&lt;/b&gt; sheet.\nNot the red one.</q>'
        in answer.text
    )
    if failure != "unreadable":
        assert str(escape(STORE_REFUSED)) in answer.text


# ------------------------------------------------------------------ fields the page did not write


def keep_as_sent(client: TestClient, fields: list[tuple[str, str]]) -> Answer:
    return client.post(
        "/parent/inbox/keep",
        content=urlencode(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.parametrize("before", [True, False], ids=["first", "last"])
@pytest.mark.parametrize(
    "extra",
    [
        [("instructions-0", "1")],
        [("instruction-0-0", to_wire(A))],
        [("apply-0-1", "0")],
        [("none-0", "1"), ("none-0", "1")],
        [("apply-0-7", "1")],
        [("apply-0", "1")],
        [("instruction-0-01", to_wire("Bring a calculator."))],
        [("instruction-0-2", "Words not on the wire.")],
        [("none-0", "yes")],
    ],
    ids=[
        "the-revision-twice",
        "the-words-twice",
        "a-box-twice",
        "none-twice",
        "a-box-without-words",
        "a-name-the-page-never-writes",
        "a-padded-place",
        "words-not-on-the-wire",
        "none-ticked-another-way",
    ],
)
def test_instruction_fields_the_page_did_not_write_refuse_the_whole_paste(
    tmp_path: pathlib.Path, extra: list[tuple[str, str]], before: bool
) -> None:
    """Whichever of the fields comes first, a field of the instruction question sent twice,
    under a name the page never writes, or with a value it never writes refuses the paste
    whole, with nothing written; the paste comes back to be answered again."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = list({**review_form(page), "apply-0-1": "1"}.items())
        fields = [*extra, *form] if before else [*form, *extra]
        start = tables(client)
        answer = keep_as_sent(client, fields)
        after = tables(client)

    assert answer.status_code == 422
    assert str(escape(INSTRUCTION_FORM_UNREADABLE)) in answer.text
    assert after == start


def test_a_file_in_place_of_an_instructions_words_refuses_the_whole_paste(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = {**review_form(page), "apply-0-1": "1"}
        words = form.pop("instruction-0-1")
        start = tables(client)
        answer = client.post(
            "/parent/inbox/keep",
            data=form,
            files={"instruction-0-1": ("words.txt", words.encode(), "text/plain")},
        )
        after = tables(client)

    assert answer.status_code == 422
    assert after == start


# ------------------------------------------------------------------ where the question is asked

WEEKLY_KEPT = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\nShow your work.\n"
WEEKLY_FIRST_BARE = (
    "Tuesday 9/8/2026\nMath\nDue: Weekly practice:\n"
    "Monday 9/14/2026\nMath\nAssigned: Weekly practice: (Due:09/15/2026)\nUse a pencil.\n"
)


def test_the_question_is_asked_on_the_first_card_of_the_assignment_even_with_no_words_of_its_own(
    tmp_path: pathlib.Path,
) -> None:
    """The first card of the text that lands on the assignment brings no instruction; the one
    after it brings a new one. The question is asked on the first card, and the second points
    to it."""
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        page = client.post("/parent/inbox/read", data={"text": WEEKLY_FIRST_BARE}).text
        same = {"occurrence-0": "update", "occurrence-1": "update"}
        asked = client.post("/parent/inbox/keep", data={**review_form(page), **same})

    assert asked.status_code == 200
    assert form_key(review_form(asked.text)) == "0"
    assert sorted(boxes(asked.text, "0")) == ["Show your work.", "Use a pencil."]
    assert asked.text.count("which apply now?") == 1
    assert "shown on the first card for this assignment" in asked.text


# ------------------------------------------------------------------ where the focus lands


@pytest.mark.parametrize("refusal", ["contradiction", "stale"])
def test_a_refused_answer_puts_the_focus_on_one_summary_linked_to_its_question(
    tmp_path: pathlib.Path, refusal: str
) -> None:
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = {**review_form(page), "apply-0-1": "1"}
        if refusal == "contradiction":
            form["none-0"] = "1"
        else:
            store = store_of(client)
            name = next(
                row.assignment_id for row in store.all_assignments() if row.title == "Q1 Check 3"
            )
            store.settle_school_instructions(
                name,
                [InstructionSeen("Bring a calculator.", None)],
                InstructionChoice(1, (A, "Bring a calculator."), frozenset({A})),
                authored_by="parent",
                now=datetime(2026, 9, 24, 20, 0, tzinfo=UTC),
                today=date(2026, 9, 24),
            )
        refused = client.post("/parent/inbox/keep", data=form)

    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', refused.text, re.S
    )
    assert refused.status_code == (422 if refusal == "contradiction" else 409)
    assert refused.text.count("autofocus") == 1
    assert summary is not None
    assert 'tabindex="-1" autofocus' in summary.group(0)
    assert '<a href="#instructions-question-0">' in summary.group(1)
    assert (
        'id="instructions-question-0" tabindex="-1" aria-describedby="problem-summary"'
        in refused.text
    )
