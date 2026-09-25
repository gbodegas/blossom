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
from blossom.intake import Change, ChangedSinceShown, Held, Kept, keep, read_text
from blossom.reconciliation import SourceChannel
from blossom.routes.inbox import (
    CHANGED_SINCE_SHOWN,
    CHOOSE_INSTRUCTIONS,
    HELD_BY_A_NOTE,
    IDENTIFY_FIRST,
    INSTRUCTION_FORM_UNREADABLE,
    INSTRUCTIONS_CONTRADICT,
    STORE_REFUSED,
    unsaved_on,
)
from blossom.routes.navigation import instructions_action_href, instructions_review_href
from blossom.school_instructions import (
    INSTRUCTION_MAX_LENGTH,
    InstructionChoice,
    InstructionSeen,
    InstructionsStanding,
    SubmittedChoice,
    to_wire,
)
from blossom.settings import Settings
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    SAME_ORIGIN,
    Answer,
    as_a_browser_sends,
    fixture_settings,
    homework_from_a_note,
    store_of,
    whole_form,
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


# ------------------------------------------------------------------ an answer at odds with itself

KEEP_NOW = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
KEEP_TODAY = date(2026, 9, 24)
WEEKLY_REPEATED = WEEKLY_FIRST_BARE.replace("Use a pencil.", "Show your work.")


@pytest.mark.parametrize(
    "where", ["new-work", "saved-work", "beside-other-work", "on-another-card"]
)
def test_an_answer_that_contradicts_itself_writes_nothing_from_any_caller(
    tmp_path: pathlib.Path, where: str
) -> None:
    """An answer ticking an instruction and that none applies is no answer the rule can take,
    from a page or any caller: whether nothing needed a choice, the instruction stands already,
    other work is beside it, or the answer rides on another card of the assignment, nothing of
    the text is written and the question is handed back."""
    with client_in(tmp_path) as client:
        store = store_of(client)
        text: str = ASSIGNED_WEEK
        occurrences: dict[int, str] = {}
        answers = {0: SubmittedChoice(0, (A,), frozenset({A}), True)}
        if where == "saved-work":
            saved(client, ASSIGNED_WEEK)
            answers = {0: SubmittedChoice(1, (A,), frozenset({A}), True)}
        elif where == "beside-other-work":
            text = ASSIGNED_WEEK + ANOTHER.removeprefix("Homework for Wren\n")
        elif where == "on-another-card":
            saved(client, WEEKLY_KEPT)
            text, occurrences = WEEKLY_REPEATED, {0: "update", 1: "update"}
            words = "Show your work."
            answers = {1: SubmittedChoice(1, (words,), frozenset({words}), True)}
        reading = read_text(text, now=KEEP_NOW, today=KEEP_TODAY)
        before = tables(client)
        outcome = keep(
            reading.items,
            store,
            occurrences=occurrences,
            instruction_answers=answers,
            imported_by="parent",
            now=KEEP_NOW,
            today=KEEP_TODAY,
        )
        after = tables(client)

    assert not isinstance(outcome, Kept)
    assert after == before


def test_an_answer_left_out_lets_the_first_instruction_stand_and_a_true_retry_is_nothing_new(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        store = store_of(client)
        reading = read_text(ASSIGNED_WEEK, now=KEEP_NOW, today=KEEP_TODAY)
        first = keep(reading.items, store, imported_by="parent", now=KEEP_NOW, today=KEEP_TODAY)
        retry = keep(
            reading.items,
            store,
            instruction_answers={0: SubmittedChoice(1, (A,), frozenset({A}))},
            imported_by="parent",
            now=KEEP_NOW,
            today=KEEP_TODAY,
        )
        final = standing(client)

    assert first == Kept(added=1, updated=0, unchanged=0)
    assert retry == Kept(added=0, updated=0, unchanged=1)
    assert final.texts == (A,)


# ------------------------------------------------------------------ a card that cannot be read

GUIDE_KEPT = "Return the course guide signed by a parent after reading it."
GUIDE_NEW = "Bring the signed guide on Tuesday."
HEALTH_DUE = f"- 09/29/2026 - Tuesday\nHealth - Due: Course Guide Due:\n{GUIDE_NEW}\n"


def two_questions(client: TestClient) -> str:
    """A text whose two cards each ask which instructions apply: Q1 Check 3 and the course
    guide, each with one instruction saved and one new."""
    saved(client, ASSIGNED_WEEK)
    saved(client, ANOTHER)
    return client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED + HEALTH_DUE}).text


def answered_on_both(page: str) -> dict[str, str]:
    return {
        **review_form(page),
        f"apply-0-{boxes(page, '0').index(A)}": "1",
        f"apply-1-{boxes(page, '1').index(GUIDE_NEW)}": "1",
    }


def with_damage(page: str, form: dict[str, str], damage: str) -> list[tuple[str, str]]:
    """The form as the page wrote it, with one field of the first card as it never writes it."""
    fields = list(form.items())
    if damage == "a-revision-not-a-count":
        return [(name, "bad" if name == "instructions-0" else value) for name, value in fields]
    if damage == "a-padded-revision":
        return [
            (name, "0" + value if name == "instructions-0" else value) for name, value in fields
        ]
    if damage == "another-box-ticked-another-way":
        return [*fields, (f"apply-0-{boxes(page, '0').index(B)}", "yes")]
    return [*fields, ("instructions-0", form["instructions-0"])]


@pytest.mark.parametrize(
    "damage",
    [
        "a-revision-not-a-count",
        "a-padded-revision",
        "another-box-ticked-another-way",
        "the-revision-twice",
    ],
)
def test_a_card_that_cannot_be_read_says_back_its_choice_and_leaves_the_other_as_chosen(
    tmp_path: pathlib.Path, damage: str
) -> None:
    """The first card's answer cannot be read whole: nothing of the text is written, the words
    it ticked that can be read are said back as its unsaved choice with no box ticked, and the
    other card's answer comes back ticked as it was made."""
    with client_in(tmp_path) as client:
        page = two_questions(client)
        start = tables(client)
        refused = keep_as_sent(client, with_damage(page, answered_on_both(page), damage))
        after = tables(client)

    account = refused.text.split('id="instructions-unsaved-0"', 1)[1]
    account = account.split('id="instructions-question-0"', 1)[0]
    assert refused.status_code == 422
    assert after == start
    assert '<h3 class="update-heading">Your unsaved choice</h3>' in account
    assert unsaved(refused.text, "0") == [A]
    assert ticked(refused.text, "0") == []
    assert ticked(refused.text, "1") == [GUIDE_NEW]
    assert refused.text.count("autofocus") == 1
    assert '<a href="#instructions-question-0">' in refused.text


def test_a_card_that_cannot_be_read_then_a_reading_that_fails_keeps_every_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client_in(tmp_path) as client:
        page = two_questions(client)
        fields = with_damage(page, answered_on_both(page), "a-revision-not-a-count")
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        refused = keep_as_sent(client, fields)

    kept = html.unescape(refused.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert refused.status_code == 500
    assert A in kept
    assert GUIDE_NEW in kept
    assert B not in kept


# ------------------------------------------------------------------ a question left unanswered


@pytest.mark.parametrize("partial", [False, True], ids=["one-question", "one-of-two-answered"])
def test_a_question_left_unanswered_takes_the_focus_and_is_named(
    tmp_path: pathlib.Path, partial: bool
) -> None:
    """Saved with a question about the school's instructions left unanswered, the page puts the
    focus on one summary naming the first question still unanswered, keeps every answer given
    on the other cards, and saves nothing; the page as first shown asks for no focus."""
    with client_in(tmp_path) as client:
        text = BOTH_CARDS
        if partial:
            text += "\n" + BOTH_CARDS.replace("Q1 Check 3", "Q1 Check 4")
        page = client.post("/parent/inbox/read", data={"text": text}).text
        keys = re.findall(r'id="instructions-question-(\d+)"', page)
        form = review_form(page)
        if partial:
            form[f"apply-{keys[0]}-0"] = "1"
        refused = client.post("/parent/inbox/keep", data=form)
        nothing = store_of(client).all_assignments()

    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', refused.text, re.S
    )
    target = keys[1] if partial else keys[0]
    assert "autofocus" not in page
    assert refused.status_code == 200
    assert refused.text.count("autofocus") == 1
    assert summary is not None
    assert str(escape(CHOOSE_INSTRUCTIONS)) in summary.group(1)
    assert f'<a href="#instructions-question-{target}">Go to the question.</a>' in summary.group(1)
    assert (
        f'id="instructions-question-{target}" tabindex="-1" aria-describedby="problem-summary"'
        in refused.text
    )
    if partial:
        assert ticked(refused.text, keys[0]) == [boxes(page, keys[0])[0]]
    assert nothing == []


# ------------------------------------------------------------------ a note longer than a paste

LONG_NOTE = "Read every chapter, then answer each question in full. " * 730


def test_a_long_instruction_kept_from_before_is_asked_beside_a_new_one_by_its_row(
    tmp_path: pathlib.Path,
) -> None:
    """A school instruction longer than any paste, kept from before, is listed whole beside a
    new one and travels in the form by its row; choosing it keeps it and the new one as
    history."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A + "\n", ""))
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        store.put_on_record(
            [
                row.model_copy(
                    update={
                        "note": LONG_NOTE,
                        "origins": {**row.origins, "note": SourceChannel.LMS},
                    }
                )
            ],
            {},
        )
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = review_form(page)
        place = boxes(page, "0").index(LONG_NOTE)
        answer = client.post("/parent/inbox/keep", data={**form, f"apply-0-{place}": "1"})
        final = standing(client)

    assert f"row-0-{place}" in form
    assert f"instruction-0-{place}" not in form
    assert f"instruction-0-{1 - place}" in form
    assert answer.status_code == 303
    assert final.texts == (LONG_NOTE,)
    assert [item.text for item in final.history] == [B]


# ------------------------------------------------------------------ a question the page never put

PENCIL = "Use a pencil."
WORK = "Show your work."


@pytest.mark.parametrize("where", ["new-work", "saved-work", "another-card", "no-such-card"])
def test_an_answer_to_a_question_the_page_never_put_refuses_the_paste(
    tmp_path: pathlib.Path, where: str
) -> None:
    """An answer about the school's instructions where the page put no such question, a new
    assignment's only instruction, one saved already, a card whose assignment's question is
    on its first card, or a card the text does not have, is no form the page wrote: the paste
    is refused whole, and nothing is written."""
    with client_in(tmp_path) as client:
        text = ASSIGNED_WEEK
        extra: dict[str, str] = {}
        if where == "new-work":
            extra = {"instructions-0": "0", "instruction-0-0": to_wire(A), "none-0": "1"}
        elif where == "saved-work":
            saved(client, ASSIGNED_WEEK)
            extra = {"instructions-0": "1", "instruction-0-0": to_wire(A), "none-0": "1"}
        elif where == "another-card":
            saved(client, WEEKLY_KEPT)
            text = WEEKLY_FIRST_BARE
            extra = {
                "occurrence-0": "update",
                "occurrence-1": "update",
                "instructions-1": "1",
                "instruction-1-0": to_wire(WORK),
                "instruction-1-1": to_wire(PENCIL),
                "apply-1-1": "1",
            }
        else:
            extra = {"instructions-7": "0", "instruction-7-0": to_wire(A), "apply-7-0": "1"}
        page = client.post("/parent/inbox/read", data={"text": text}).text
        start = tables(client)
        answer = client.post("/parent/inbox/keep", data={**review_form(page), **extra})
        after = tables(client)

    assert answer.status_code == 422
    assert str(escape(INSTRUCTION_FORM_UNREADABLE)) in answer.text
    assert after == start


def test_the_same_answer_sent_twice_saves_once_and_then_nothing_more(
    tmp_path: pathlib.Path,
) -> None:
    """A double press: the second finds its answer standing and saves nothing more, though
    the text would not put the question it answered again."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = {**review_form(page), f"apply-0-{boxes(page, '0').index(B)}": "1"}
        first = client.post("/parent/inbox/keep", data=form)
        after_first = tables(client)
        second = client.post("/parent/inbox/keep", data=form)
        after_second = tables(client)
        final = standing(client)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert after_second == after_first
    assert final.texts == (B,)


FAR_DUE = "Homework for Wren\n- 11/05/2026 - Thursday\n07 Algebra - Due: Q1 Check 3:\n"


def test_an_answer_on_a_card_that_lands_nowhere_yet_is_not_refused(tmp_path: pathlib.Path) -> None:
    """The page asked which instructions apply on a new assignment's card; before the save, a
    row under the same name was saved due weeks away, so the card now asks whether it is the
    same work. Its answer about the instructions is not refused: the page asks the new
    question, and nothing is saved."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": BOTH_CARDS}).text
        form = {**review_form(page), "apply-0-0": "1"}
        saved(client, FAR_DUE)
        start = tables(client)
        answer = client.post("/parent/inbox/keep", data=form)
        after = tables(client)

    assert answer.status_code == 200
    assert 'name="occurrence-0"' in answer.text
    assert str(escape(INSTRUCTION_FORM_UNREADABLE)) not in answer.text
    assert after == start


# ------------------------------------------------------------------ every answer on the cards


def weekly(store: ProjectStateStore) -> str:
    """The id of the weekly practice, the one assignment the weekly texts land on."""
    (row,) = [item for item in store.all_assignments() if item.title == "Weekly practice"]
    return row.assignment_id


def pencil_said_since(client: TestClient) -> str:
    """The weekly practice saved with one instruction, then another said beside it and kept
    as history, at revision 2."""
    saved(client, WEEKLY_KEPT)
    store = store_of(client)
    name = weekly(store)
    store.settle_school_instructions(
        name,
        [InstructionSeen(PENCIL, SourceChannel.LMS)],
        InstructionChoice(1, (WORK, PENCIL), frozenset({WORK})),
        authored_by="parent",
        now=KEEP_NOW,
        today=KEEP_TODAY,
    )
    return name


def none_chosen_since(store: ProjectStateStore, name: str) -> None:
    store.settle_school_instructions(
        name,
        [],
        InstructionChoice(2, (WORK, PENCIL), none_applies=True),
        authored_by="parent",
        now=KEEP_NOW,
        today=KEEP_TODAY,
    )


def keep_both(
    store: ProjectStateStore, answers: dict[int, SubmittedChoice]
) -> Kept | Held | ChangedSinceShown | list[Change]:
    """The weekly practice's two cards, both said to be the saved homework, kept with these
    answers by a caller other than the page."""
    reading = read_text(WEEKLY_TWICE, now=KEEP_NOW, today=KEEP_TODAY)
    return keep(
        reading.items,
        store,
        occurrences={0: "update", 1: "update"},
        instruction_answers=answers,
        imported_by="parent",
        now=KEEP_NOW,
        today=KEEP_TODAY,
    )


@pytest.mark.parametrize("old_first", [True, False], ids=["old-answer-first", "old-answer-last"])
def test_an_answer_made_before_the_last_change_on_either_card_refuses_the_whole_text(
    tmp_path: pathlib.Path, old_first: bool
) -> None:
    """Two cards of one assignment each carry an answer, one made before the choice that stands
    and one after it. Whichever card has the old one, nothing is written, what changed is
    handed back, and no box comes back ticked."""
    with client_in(tmp_path) as client:
        name = pencil_said_since(client)
        store = store_of(client)
        none_chosen_since(store, name)
        old = SubmittedChoice(2, (WORK, PENCIL), frozenset({PENCIL}))
        fresh = SubmittedChoice(3, (WORK, PENCIL), none_applies=True)
        answers = {0: old, 1: fresh} if old_first else {0: fresh, 1: old}
        before = tables(client)
        outcome = keep_both(store, answers)
        after = tables(client)

    assert isinstance(outcome, ChangedSinceShown)
    assert after == before
    (carrier,) = [change for change in outcome.changes if change.instructions is not None]
    assert carrier.instructions_stale
    assert carrier.instructions_revision == 3
    assert carrier.instructions_ticked == frozenset()
    assert not carrier.instructions_none_ticked
    assert carrier.instructions_unsaved == (answers[0], answers[1])


@pytest.mark.parametrize("same", [False, True], ids=["answers-differ", "one-answer-twice"])
def test_answers_on_two_cards_that_differ_choose_neither_and_are_kept_to_say_back(
    tmp_path: pathlib.Path, same: bool
) -> None:
    """Both answers are made against what stands and ask for different things: nothing is
    written, the question stays open with nothing ticked, and both are kept to say back as not
    saved. The same answer on both cards is one answer, and it is saved."""
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        store = store_of(client)
        pencil = SubmittedChoice(1, (WORK, PENCIL), frozenset({PENCIL}))
        none = SubmittedChoice(1, (WORK, PENCIL), none_applies=True)
        before = tables(client)
        outcome = keep_both(store, {0: pencil, 1: pencil if same else none})
        after = tables(client)
        found = store.school_instruction_readings([weekly(store)]).readable[weekly(store)]

    if same:
        assert isinstance(outcome, Kept)
        assert found.texts == (PENCIL,)
    else:
        assert isinstance(outcome, list)
        assert after == before
        (carrier,) = [change for change in outcome if change.instructions is not None]
        account = unsaved_on(outcome, {})[carrier.key]
        assert carrier.instructions_asked
        assert carrier.instructions_answer is None
        assert carrier.instructions_ticked == frozenset()
        assert not carrier.instructions_none_ticked
        assert carrier.instructions_unsaved == (pencil, none)
        assert account.why == "differ"
        assert [(said.words, said.none_applies) for said in account.said] == [
            ((PENCIL,), False),
            ((), True),
        ]


def test_answers_on_two_cards_that_ask_for_what_stands_are_one_answer_and_nothing_new(
    tmp_path: pathlib.Path,
) -> None:
    """Both cards ask for what stands, one made before the last save and one after it: they
    ask for the same thing, so they are one answer, and nothing new is written."""
    with client_in(tmp_path) as client:
        name = pencil_said_since(client)
        store = store_of(client)
        none_chosen_since(store, name)
        before = tables(client)["school_instructions"]
        outcome = keep_both(
            store,
            {
                0: SubmittedChoice(2, (WORK, PENCIL), none_applies=True),
                1: SubmittedChoice(3, (WORK, PENCIL), none_applies=True),
            },
        )
        after = tables(client)["school_instructions"]

    assert isinstance(outcome, Kept)
    assert after == before


@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside-an-answer-made-since"])
def test_an_answer_that_ticks_nothing_made_before_the_last_change_is_refused(
    tmp_path: pathlib.Path, beside: bool
) -> None:
    """An answer that ticks nothing, made before another instruction was kept elsewhere, was
    made against other facts: alone or beside an answer made since, nothing is written and
    what changed is handed back."""
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        store = store_of(client)
        blank = SubmittedChoice(1, (WORK, PENCIL))
        store.settle_school_instructions(
            weekly(store),
            [InstructionSeen(PENCIL, SourceChannel.LMS)],
            InstructionChoice(1, (WORK, PENCIL), frozenset({WORK})),
            authored_by="parent",
            now=KEEP_NOW,
            today=KEEP_TODAY,
        )
        answers = {0: blank}
        if beside:
            answers[1] = SubmittedChoice(2, (WORK, PENCIL), frozenset({WORK}))
        before = tables(client)
        outcome = keep_both(store, answers)
        after = tables(client)

    assert isinstance(outcome, ChangedSinceShown)
    assert after == before


def test_a_page_refused_for_an_old_answer_and_sent_back_as_returned_saves_no_old_choice(
    tmp_path: pathlib.Path,
) -> None:
    """A page made before the choice that stands comes back with answers on both cards, the old
    one on the first: nothing is written, and the page it returns, sent back as it is, saves
    nothing of the old choice."""
    with client_in(tmp_path) as client:
        name = pencil_said_since(client)
        store = store_of(client)
        page = client.post("/parent/inbox/read", data={"text": WEEKLY_TWICE}).text
        form = {**review_form(page), "occurrence-0": "update", "occurrence-1": "update"}
        none_chosen_since(store, name)
        for key, (revision, tick) in {0: ("2", "apply-0-1"), 1: ("3", "none-1")}.items():
            form[f"instructions-{key}"] = revision
            form[f"instruction-{key}-0"] = to_wire(WORK)
            form[f"instruction-{key}-1"] = to_wire(PENCIL)
            form[tick] = "1"
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=form)
        after_refusal = tables(client)
        again = client.post("/parent/inbox/keep", data=review_form(refused.text))
        found = store.school_instruction_readings([name]).readable[name]

    assert refused.status_code in (409, 422)
    assert after_refusal == before
    assert ticked(refused.text, "0") == []
    assert again.status_code == 303
    assert found.texts == ()


# ------------------------------------------------------------------ a choice whose question is gone


@pytest.mark.parametrize("damage", ["a-revision-not-a-count", "the-revision-twice"])
@pytest.mark.parametrize("since", ["kept-as-history", "then-none-applies"])
def test_a_choice_not_saved_is_said_back_after_its_question_is_gone(
    tmp_path: pathlib.Path, damage: str, since: str
) -> None:
    """B was chosen on a page; another tab then kept the text with A applying, so the text asks
    nothing about the instructions. The page's form comes back damaged: nothing is written, the
    choice of B is still said back as not saved, the focus is on a summary that leads to it,
    and the way to review the instructions is offered beside it."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        form = {**review_form(page), f"apply-0-{boxes(page, '0').index(B)}": "1"}
        saved(client, DUE_WEEK_CHANGED, **{f"apply-0-{boxes(page, '0').index(A)}": "1"})
        head = standing(client)
        name = head.kept[0].assignment_id
        if since == "then-none-applies":
            store_of(client).settle_school_instructions(
                name,
                [],
                InstructionChoice(head.revision, (A, B), none_applies=True),
                authored_by="parent",
                now=KEEP_NOW,
                today=KEEP_TODAY,
            )
        before = tables(client)
        refused = keep_as_sent(client, with_damage(page, form, damage))
        after = tables(client)

    account = refused.text.split('id="instructions-unsaved-0" tabindex="-1">', 1)[1]
    account = account.split("</div>", 1)[0]
    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', refused.text, re.S
    )
    assert refused.status_code == 422
    assert after == before
    assert 'name="instructions-0"' not in refused.text
    assert '<h3 class="update-heading">Your unsaved choice</h3>' in account
    assert unsaved(refused.text, "0") == [B]
    assert "Not saved, since the answer could not be read" in account
    assert f'<a href="{instructions_review_href(name)}">Review school instructions</a>' in account
    assert " checked>" not in refused.text
    assert refused.text.count("autofocus") == 1
    assert summary is not None
    assert 'tabindex="-1" autofocus' in summary.group(0)
    assert '<a href="#instructions-unsaved-0">See the choice not saved.</a>' in summary.group(1)
    assert "#instructions-question-" not in refused.text


# ------------------------------------------------------------------ a choice by reference, unread


@pytest.mark.parametrize("rows", [1, 2])
def test_a_choice_by_reference_the_paste_cannot_read_back_is_said_as_unread(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, rows: int
) -> None:
    """Boxes ticked by reference to a row, in a form that cannot be read whole, and a record that
    cannot be read back: the page that reads no store says what was selected by reference and
    that its text could not be read, and claims nothing about the assignment."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": BOTH_CARDS}).text
        form = {**review_form(page), "instructions-0": "bad"}
        for place in range(rows):
            form.pop(f"instruction-0-{place}")
            form[f"row-0-{place}"] = str(999999 - place)
            form[f"apply-0-{place}"] = "1"
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        refused = client.post("/parent/inbox/keep", data=form)

    kept = html.unescape(refused.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert refused.status_code == 500
    assert "kept for this assignment" not in refused.text
    if rows == 1:
        assert "one instruction selected by reference; its text could not be read" in kept
    else:
        assert "2 instructions selected by reference; their text could not be read" in kept


def test_a_choice_by_reference_the_page_can_read_is_said_back_in_its_words(
    tmp_path: pathlib.Path,
) -> None:
    """A form that cannot be read whole chose a long instruction kept from before, by its row:
    the page reads what is kept and says the choice back in its words."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A + "\n", ""))
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        store.put_on_record(
            [
                row.model_copy(
                    update={
                        "note": LONG_NOTE,
                        "origins": {**row.origins, "note": SourceChannel.LMS},
                    }
                )
            ],
            {},
        )
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        place = boxes(page, "0").index(LONG_NOTE)
        form = {**review_form(page), f"apply-0-{place}": "1", "instructions-0": "bad"}
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=form)
        after = tables(client)

    assert f"row-0-{place}" in form
    assert refused.status_code == 422
    assert after == before
    assert unsaved(refused.text, "0") == [LONG_NOTE]
    assert "selected by reference" not in refused.text


# ------------------------------------------------------------------ a question put on another card

WEEKLY_MOVED = "Tuesday 9/15/2026\nMath\nDue: Weekly practice:\nUse a pencil.\n"
WEEKLY_STILL = "Tuesday 9/1/2026\nMath\nDue: Weekly practice:\nUse a pencil.\n"


def the_question_on(page: str) -> str:
    """The key of the one card that puts the question about the school's instructions."""
    return form_key(review_form(page))


@pytest.mark.parametrize("choice", ["pencil", "none"])
@pytest.mark.parametrize("moved_first", [True, False], ids=["moved-first", "moved-last"])
def test_an_answer_whose_question_moves_to_another_card_of_its_assignment_saves_once(
    tmp_path: pathlib.Path, choice: str, moved_first: bool
) -> None:
    """The page asks which instructions apply on the one card that lands, and which homework
    the other card is on it; said to be the same, that card carries the question, and the
    answer made where it was asked saves once, then a retry saves nothing more."""
    text = WEEKLY_MOVED + WEEKLY_STILL if moved_first else WEEKLY_STILL + WEEKLY_MOVED
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        page = client.post("/parent/inbox/read", data={"text": text}).text
        key = the_question_on(page)
        answer = (
            {f"apply-{key}-{boxes(page, key).index(PENCIL)}": "1"}
            if choice == "pencil"
            else {f"none-{key}": "1"}
        )
        moved = "0" if moved_first else "1"
        form = {**review_form(page), f"occurrence-{moved}": "update", **answer}
        first = client.post("/parent/inbox/keep", data=form)
        after_first = tables(client)
        store = store_of(client)
        name = weekly(store)
        row = store.one_assignment(name)
        found = store.school_instruction_readings([name]).readable[name]
        again = client.post("/parent/inbox/keep", data=form)
        after_again = tables(client)

    assert key == ("1" if moved_first else "0")
    assert first.status_code == 303
    assert row is not None
    assert row.due_date == date(2026, 9, 15)
    assert found.texts == ((PENCIL,) if choice == "pencil" else ())
    assert again.status_code == 303
    assert again.headers["location"] == "/parent?added=0&updated=0&unchanged=1"
    assert after_again == after_first


def test_an_answer_under_a_moved_question_made_before_a_change_is_refused_and_said_back(
    tmp_path: pathlib.Path,
) -> None:
    """The same answer, after another choice was saved for the assignment: nothing of the
    text is saved, and the answer is said back as not saved, with nothing ticked."""
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        page = client.post("/parent/inbox/read", data={"text": WEEKLY_MOVED + WEEKLY_STILL}).text
        key = the_question_on(page)
        form = {
            **review_form(page),
            "occurrence-0": "update",
            f"apply-{key}-{boxes(page, key).index(PENCIL)}": "1",
        }
        store = store_of(client)
        store.settle_school_instructions(
            weekly(store),
            [InstructionSeen(PENCIL, SourceChannel.LMS)],
            InstructionChoice(1, (WORK, PENCIL), none_applies=True),
            authored_by="parent",
            now=KEEP_NOW,
            today=KEEP_TODAY,
        )
        before = tables(client)
        refused = client.post("/parent/inbox/keep", data=form)
        after = tables(client)

    assert refused.status_code == 409
    assert after == before
    carrier = the_question_on(refused.text)
    assert ticked(refused.text, carrier) == []
    said = re.findall(r'<p class="problem" id="instructions-problem-\d+">(.*?)</p>', refused.text)
    assert any(PENCIL in html.unescape(item) for item in said)


# ------------------------------------------------------------------ a card that asks which it is

CHECK_5 = (
    "09/23/2026 - Wednesday\n07 Algebra - Assigned: Q1 Check 5: (Due:10/01/2026)\n"
    "Show each step.\n"
    "10/01/2026 - Thursday\n07 Algebra - Due: Q1 Check 5:\nOnly the first proof.\n"
)
CHECK_5_LATER = "11/05/2026 - Thursday\n07 Algebra - Due: Q1 Check 5:\n"


def said_back_on(page: str, key: str) -> str:
    found = re.search(rf'<p class="problem" id="instructions-problem-{key}">(.*?)</p>', page, re.S)
    assert found is not None
    return html.unescape(found.group(1))


@pytest.mark.parametrize("choice", ["none", "one"])
def test_a_choice_on_a_card_that_now_asks_which_homework_it_is_is_said_back_until_replaced(
    tmp_path: pathlib.Path, choice: str
) -> None:
    """A row under the same name was saved from elsewhere after the page asked which
    instructions apply, so the card now asks which homework it is. The choice is said back as
    not saved, through another press, and after the answer, beside the question asked
    afresh; nothing is saved until an explicit choice is made."""
    words = "that no school instruction applies" if choice == "none" else "Show each step."
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5}).text
        key = the_question_on(page)
        answer = (
            {f"none-{key}": "1"}
            if choice == "none"
            else {f"apply-{key}-{boxes(page, key).index('Show each step.')}": "1"}
        )
        saved(client, CHECK_5_LATER)
        start = tables(client)
        returned = client.post("/parent/inbox/keep", data={**review_form(page), **answer})
        pressed = client.post("/parent/inbox/keep", data=review_form(returned.text))
        answered = client.post(
            "/parent/inbox/keep", data={**review_form(pressed.text), f"occurrence-{key}": "new"}
        )
        after = tables(client)
        chosen = client.post(
            "/parent/inbox/keep",
            data={**review_form(answered.text), f"none-{the_question_on(answered.text)}": "1"},
        )

    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', returned.text, re.S
    )
    assert returned.status_code == pressed.status_code == answered.status_code == 200
    assert summary is not None
    assert str(escape(IDENTIFY_FIRST)) in summary.group(1)
    assert f'<a href="#occurrence-question-{key}">Go to the question.</a>' in summary.group(1)
    assert f'id="occurrence-question-{key}" tabindex="-1"' in returned.text
    assert "kept" not in summary.group(1)
    for shown in (returned.text, pressed.text, answered.text):
        assert words in said_back_on(shown, key)
        assert "Not saved, since it was chosen before" in said_back_on(shown, key)
    assert f'name="occurrence-{key}"' in pressed.text
    assert ticked(answered.text, the_question_on(answered.text)) == []
    assert 'value="1" checked' not in answered.text
    assert after == start
    assert chosen.status_code == 303


def test_a_choice_carried_from_before_its_card_was_known_is_kept_when_the_file_refuses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5}).text
        key = the_question_on(page)
        answer = {f"apply-{key}-{boxes(page, key).index('Show each step.')}": "1"}
        saved(client, CHECK_5_LATER)
        returned = client.post("/parent/inbox/keep", data={**review_form(page), **answer})
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        refused = client.post("/parent/inbox/keep", data=review_form(returned.text))

    kept = html.unescape(refused.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert refused.status_code == 500
    assert "Show each step." in kept


def test_an_answer_refused_on_a_card_never_asked_is_said_back_as_not_saved(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        saved(client, WEEKLY_KEPT)
        page = client.post("/parent/inbox/read", data={"text": WEEKLY_FIRST_BARE}).text
        extra = {
            "occurrence-0": "update",
            "occurrence-1": "update",
            "instructions-1": "1",
            "instruction-1-0": to_wire(WORK),
            "instruction-1-1": to_wire(PENCIL),
            "apply-1-1": "1",
        }
        refused = client.post("/parent/inbox/keep", data={**review_form(page), **extra})

    assert refused.status_code == 422
    assert unsaved(refused.text, "1") == [PENCIL]


@pytest.mark.parametrize("which", ["update", "new"])
@pytest.mark.parametrize("choice", ["none", "one"])
@pytest.mark.parametrize("marks", ["kept", "removed", "renamed", "none-left", "forged"])
def test_a_choice_sent_beside_the_answer_to_which_homework_it_is_is_refused(
    tmp_path: pathlib.Path, which: str, choice: str, marks: str
) -> None:
    """The page asks only which homework the card is; a choice added beside that answer is
    refused, even where the card then asks which instructions apply, and whatever becomes of
    the marks the page wrote. The page that asks it saves the same choice, once."""
    with client_in(tmp_path) as client:
        saved(client, CHECK_5_LATER)
        page = client.post("/parent/inbox/read", data={"text": CHECK_5}).text
        form = {**review_form(page), "occurrence-0": which}
        asked = client.post("/parent/inbox/keep", data=form).text
        words = boxes(asked, "0")
        pick = (
            {"none-0": "1"}
            if choice == "none"
            else {f"apply-0-{words.index('Show each step.')}": "1"}
        )
        added = {
            name: value
            for name, value in review_form(asked).items()
            if name.partition("-")[0] in ("instructions", "instruction", "row")
        }
        sent = {**form, **added, **pick}
        if marks != "kept":
            sent.pop("which-0")
        if marks == "renamed":
            sent["which-00"] = "1"
        if marks == "none-left":
            sent.pop("made_with")
        if marks == "forged":
            sent["made_with"] = f"0:{which}.0123456789abcdef"
        start = tables(client)
        refused = client.post("/parent/inbox/keep", data=sent)
        after = tables(client)
        answered = client.post("/parent/inbox/keep", data={**review_form(asked), **pick})
        saved_once = tables(client)
        again = client.post("/parent/inbox/keep", data={**review_form(asked), **pick})
        final = tables(client)

    assert 'name="which-0"' in page
    assert 'name="instructions-0"' not in page
    assert the_question_on(asked) == "0"
    assert refused.status_code == 422
    assert after == start
    assert ticked(refused.text, "0") == []
    assert answered.status_code == again.status_code == 303
    assert saved_once != start
    assert final == saved_once


def test_an_answer_that_shows_no_instruction_refuses_the_paste(tmp_path: pathlib.Path) -> None:
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5}).text
        key = the_question_on(page)
        form = {
            name: value
            for name, value in review_form(page).items()
            if not name.startswith(f"instruction-{key}-")
        }
        start = tables(client)
        refused = client.post("/parent/inbox/keep", data={**form, f"none-{key}": "1"})
        after = tables(client)

    assert refused.status_code == 422
    assert after == start


def question_fields(page: str) -> dict[str, str]:
    """The fields a page writes for its question about the school's instructions."""
    return {
        name: value
        for name, value in review_form(page).items()
        if name.partition("-")[0] in ("instructions", "instruction", "row")
    }


@pytest.mark.parametrize("pick", ["none", "one"])
def test_an_answer_to_a_question_raised_after_the_page_was_made_is_refused(
    tmp_path: pathlib.Path, pick: str
) -> None:
    """A page that asked nothing about the school's instructions can't answer a question
    another tab's save raised since; the page that asks it saves the same answer once."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ASSIGNED_WEEK}).text
        original = review_form(page)
        other = saved(client, DUE_WEEK_CHANGED)
        fresh = client.post("/parent/inbox/read", data={"text": ASSIGNED_WEEK}).text
        choice = (
            {"none-0": "1"} if pick == "none" else {f"apply-0-{boxes(fresh, '0').index(A)}": "1"}
        )
        start = tables(client)
        refused = client.post(
            "/parent/inbox/keep", data={**original, **question_fields(fresh), **choice}
        )
        after = tables(client)
        unchanged = client.post("/parent/inbox/keep", data=original)
        answered = client.post("/parent/inbox/keep", data={**review_form(fresh), **choice})
        saved_once = tables(client)
        again = client.post("/parent/inbox/keep", data={**review_form(fresh), **choice})
        final = tables(client)

    assert 'name="instructions-0"' not in page
    assert other.status_code == 303
    assert the_question_on(fresh) == "0"
    assert refused.status_code == 422
    assert after == start
    assert ticked(refused.text, "0") == []
    assert "Not saved, since the answer could not be read" in said_back_on(refused.text, "0")
    assert unchanged.status_code == 200
    assert answered.status_code == again.status_code == 303
    assert saved_once != start
    assert final == saved_once


def test_an_answer_sent_at_a_revision_its_page_did_not_show_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A page that asked at one revision can't send the revision another tab's save made
    since; sent as the page wrote it, the answer is refused as stale."""
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK)
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        choice = {f"apply-0-{boxes(page, '0').index(B)}": "1"}
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        review = instructions_review_href(row.assignment_id)
        action = instructions_action_href(row.assignment_id)
        other = client.post(
            action, data={**whole_form(client.get(review).text, action), "none": "1"}
        )
        fresh = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        start = tables(client)
        crafted = client.post(
            "/parent/inbox/keep",
            data={**review_form(page), **question_fields(fresh), **choice},
        )
        after = tables(client)
        stale = client.post("/parent/inbox/keep", data={**review_form(page), **choice})
        final = tables(client)

    assert other.status_code == 303
    assert review_form(page)["instructions-0"] != review_form(fresh)["instructions-0"]
    assert crafted.status_code == 422
    assert after == start
    assert ticked(crafted.text, "0") == []
    assert stale.status_code == 409
    assert final == start


# ------------------------------------------------------------------ a card folded into another

PRACTICE = (
    "Tuesday 9/1/2026\nMath\nAssigned: Practice sheet: (Due:09/02/2026)\nShow all steps.\n"
    "Wednesday 9/2/2026\nMath\nDue: Practice sheet:\nUse a pencil.\n"
    "Monday 9/14/2026\nMath\nAssigned: Practice sheet: (Due:09/15/2026)\nShow all steps.\n"
    "Tuesday 9/15/2026\nMath\nDue: Practice sheet:\nUse a pencil.\n"
    "Tuesday 9/15/2026\nScience\nDue: Lab safety form:\n"
)
PRACTICE_SHOWN = ("Show all steps.", "Use a pencil.")
STEPS = SubmittedChoice(0, PRACTICE_SHOWN, frozenset({"Show all steps."}))


def keep_folded(
    store: ProjectStateStore, folded: SubmittedChoice
) -> Kept | Held | ChangedSinceShown | list[Change]:
    """The practice sheet's second card folded into its first, and an unrelated form, kept
    with an answer on each card of the practice sheet."""
    reading = read_text(PRACTICE, now=KEEP_NOW, today=KEEP_TODAY)
    return keep(
        reading.items,
        store,
        occurrences={1: "update"},
        instruction_answers={0: STEPS, 1: folded},
        imported_by="parent",
        now=KEEP_NOW,
        today=KEEP_TODAY,
    )


@pytest.mark.parametrize(
    ("folded", "stale"),
    [
        (SubmittedChoice(0, PRACTICE_SHOWN, none_applies=True), False),
        (SubmittedChoice(9, PRACTICE_SHOWN, frozenset({"Show all steps."})), True),
        (SubmittedChoice(0, ("Show all steps.",), frozenset({"Show all steps."})), True),
    ],
    ids=["contradicts", "another-revision", "another-set"],
)
def test_an_answer_on_a_folded_card_is_checked_with_every_other(
    tmp_path: pathlib.Path, folded: SubmittedChoice, stale: bool
) -> None:
    """A card folded into another still answers for the assignment: an answer on it that
    differs, or was made against another revision or set, writes nothing of the text, and is
    said back on the card that carries the question."""
    with client_in(tmp_path) as client:
        store = store_of(client)
        before = tables(client)
        outcome = keep_folded(store, folded)
        after = tables(client)

    assert after == before
    changes = outcome.changes if isinstance(outcome, ChangedSinceShown) else outcome
    assert isinstance(changes, list)
    assert isinstance(outcome, ChangedSinceShown) is stale
    (carrier,) = [change for change in changes if change.instructions is not None]
    assert carrier.key == 0
    assert unsaved_on(changes, {})[0].said


def test_the_same_answer_on_a_folded_card_saves_once_and_then_nothing_more(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        store = store_of(client)
        first = keep_folded(store, STEPS)
        after_first = tables(client)
        second = keep_folded(store, STEPS)
        after_second = tables(client)

    assert first == Kept(added=2, updated=0, unchanged=0)
    assert isinstance(second, Kept)
    assert (second.added, second.updated) == (0, 0)
    assert after_second == after_first


def test_a_folded_card_that_answers_differently_through_the_page_saves_nothing(
    tmp_path: pathlib.Path,
) -> None:
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": PRACTICE}).text
        key = the_question_on(page)
        shown = boxes(page, key)
        form = {
            **review_form(page),
            "occurrence-1": "update",
            f"apply-{key}-{shown.index('Show all steps.')}": "1",
            "instructions-1": "0",
            "instruction-1-0": to_wire(shown[0]),
            "instruction-1-1": to_wire(shown[1]),
            "none-1": "1",
        }
        answer = client.post("/parent/inbox/keep", data=form)
        nothing = store_of(client).all_assignments()

    assert key == "0"
    assert answer.status_code == 200
    assert "Not saved, since the cards for this assignment answer differently" in answer.text
    assert nothing == []


# ------------------------------------------------------------------ an answer never asked for

PRACTICE_TWICE = (
    "Tuesday 9/1/2026\nMath\nDue: Practice sheet:\nShow each step.\n"
    "Tuesday 9/15/2026\nMath\nDue: Practice sheet:\nShow each step.\n"
)


@pytest.mark.parametrize("on", ["0", "1"], ids=["on-the-card-shown", "on-the-folded-card"])
def test_an_unasked_answer_on_any_card_is_refused_and_the_page_as_sent_saves(
    tmp_path: pathlib.Path, on: str
) -> None:
    """The one instruction of new work needs no choice, so the page asks none; an answer
    that would retire it is refused whether it is sent on the card shown or on the card
    folded into it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": PRACTICE_TWICE}).text
        form = {**review_form(page), "occurrence-1": "update"}
        start = tables(client)
        refused = client.post(
            "/parent/inbox/keep",
            data={
                **form,
                f"instructions-{on}": "0",
                f"instruction-{on}-0": to_wire("Show each step."),
                f"none-{on}": "1",
            },
        )
        after = tables(client)
        kept = client.post("/parent/inbox/keep", data=form)
        store = store_of(client)
        (row,) = store.all_assignments()
        found = store.school_instruction_readings([row.assignment_id]).readable[row.assignment_id]

    assert not any(name.startswith("instructions-") for name in form)
    assert refused.status_code == 422
    assert "that no school instruction applies" in said_back_on(refused.text, "0")
    assert after == start
    assert kept.status_code == 303
    assert found.texts == ("Show each step.",)


# ------------------------------------------------------------------ a carried choice unanswered


@pytest.mark.parametrize("which", ["new", "update"])
@pytest.mark.parametrize("choice", ["none", "one"])
def test_a_carried_choice_stays_until_a_fresh_one_is_made(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, which: str, choice: str
) -> None:
    """Once the card is known, the question is asked afresh with nothing ticked; saves that
    leave it unanswered, a form that cannot be read, and a reading that fails all keep the
    choice carried from before, and nothing is written."""
    words = "that no school instruction applies" if choice == "none" else "Show each step."
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5}).text
        key = the_question_on(page)
        answer = (
            {f"none-{key}": "1"}
            if choice == "none"
            else {f"apply-{key}-{boxes(page, key).index('Show each step.')}": "1"}
        )
        saved(client, CHECK_5_LATER)
        start = tables(client)
        returned = client.post("/parent/inbox/keep", data={**review_form(page), **answer})
        answered = client.post(
            "/parent/inbox/keep", data={**review_form(returned.text), f"occurrence-{key}": which}
        )
        again = client.post("/parent/inbox/keep", data=review_form(answered.text))
        asked = the_question_on(again.text)
        bent = keep_as_sent(client, [*review_form(again.text).items(), (f"none-{asked}", "yes")])
        after = tables(client)
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        failed = client.post("/parent/inbox/keep", data=review_form(again.text))

    assert answered.status_code == again.status_code == 200
    assert bent.status_code == 422
    assert after == start
    for shown in (answered.text, again.text, bent.text):
        assert words in said_back_on(shown, key)
        assert "Not saved, since it was chosen before" in said_back_on(shown, key)
        assert ticked(shown, the_question_on(shown)) == []
        assert 'value="1" checked' not in shown
    kept = html.unescape(failed.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert failed.status_code == 500
    assert ("none applies" if choice == "none" else words) in kept


# ------------------------------------------------------------------ a paste held by a note

CHECK_5_AND_A_FORM = CHECK_5 + "10/01/2026 - Thursday\nScience - Due: Lab safety form:\n"
CHECK_5_ASSIGNED = (
    "09/23/2026 - Wednesday\n07 Algebra - Assigned: Q1 Check 5: (Due:10/01/2026)\nShow each step.\n"
)


@pytest.mark.parametrize("since", ["unchanged", "changed"])
@pytest.mark.parametrize("choice", ["none", "one"])
def test_a_paste_held_by_a_note_keeps_the_choices_on_its_other_cards(
    tmp_path: pathlib.Path, choice: str, since: str
) -> None:
    """Homework made from a note since the page was read holds the whole paste; the choice on
    another card comes back ticked when its instructions still stand as shown, and is said
    back as not saved, with nothing ticked, when they changed."""
    words = "that no school instruction applies" if choice == "none" else "Show each step."
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5_AND_A_FORM}).text
        key = the_question_on(page)
        answer = (
            {f"none-{key}": "1"}
            if choice == "none"
            else {f"apply-{key}-{boxes(page, key).index('Show each step.')}": "1"}
        )
        if since == "changed":
            saved(client, CHECK_5_ASSIGNED)
        homework_from_a_note(store_of(client), course="Science", title="Lab safety form")
        before = tables(client)
        held = client.post("/parent/inbox/keep", data={**review_form(page), **answer})
        after = tables(client)

    assert held.status_code == 409
    assert str(escape(HELD_BY_A_NOTE)) in held.text
    assert after == before
    carrier = the_question_on(held.text)
    if since == "changed":
        assert ticked(held.text, carrier) == []
        assert 'value="1" checked' not in held.text
        assert words in said_back_on(held.text, carrier)
    elif choice == "none":
        assert f'name="none-{carrier}" value="1" checked' in held.text
    else:
        assert ticked(held.text, carrier) == ["Show each step."]


def test_a_paste_held_by_a_note_keeps_the_choices_when_the_reading_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": CHECK_5_AND_A_FORM}).text
        key = the_question_on(page)
        answer = {f"apply-{key}-{boxes(page, key).index('Show each step.')}": "1"}
        store = store_of(client)
        homework_from_a_note(store, course="Science", title="Lab safety form")

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        def keep_then_fail(*args: object, **kwargs: object) -> object:
            outcome = keep(*args, **kwargs)  # type: ignore[arg-type]
            monkeypatch.setattr(store, "all_assignments", refuse)
            return outcome

        monkeypatch.setattr("blossom.routes.inbox.keep", keep_then_fail)
        failed = client.post("/parent/inbox/keep", data={**review_form(page), **answer})

    kept = html.unescape(failed.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert failed.status_code == 500
    assert "Show each step." in kept


# ------------------------------------------------------------------ a stale choice unanswered

RESUBMISSION = "Tuesday 9/1/2026\nMath\nDue: Resubmission check:\n"


def resubmission_standing(client: TestClient) -> InstructionsStanding:
    store = store_of(client)
    rows = [row for row in store.all_assignments() if row.title == "Resubmission check"]
    assert len(rows) == 1
    return store.school_instruction_readings([rows[0].assignment_id]).readable[
        rows[0].assignment_id
    ]


@pytest.mark.parametrize("choice", ["none", "one"])
def test_a_stale_choice_stays_through_unanswered_saves_until_a_fresh_one_is_made(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    """A choice refused because the instructions changed since is shown again, with why,
    through saves that leave the fresh question unanswered, a form that cannot be read, and
    a reading that fails; nothing is written, and a fresh choice replaces it and saves."""
    words = "that no school instruction applies" if choice == "none" else "Include a graph."
    with client_in(tmp_path) as client:
        saved(client, RESUBMISSION + "Show the calculation.\n")
        page = client.post("/parent/inbox/read", data={"text": RESUBMISSION + "Include a graph.\n"})
        key = the_question_on(page.text)
        answer = (
            {f"none-{key}": "1"}
            if choice == "none"
            else {f"apply-{key}-{boxes(page.text, key).index('Include a graph.')}": "1"}
        )
        table = client.post("/parent/inbox/read", data={"text": RESUBMISSION + "Use the table.\n"})
        other = the_question_on(table.text)
        kept = client.post(
            "/parent/inbox/keep",
            data={
                **review_form(table.text),
                f"apply-{other}-{boxes(table.text, other).index('Show the calculation.')}": "1",
            },
        )
        start = tables(client)
        refused = client.post("/parent/inbox/keep", data={**review_form(page.text), **answer})
        again = client.post("/parent/inbox/keep", data=review_form(refused.text))
        asked = the_question_on(again.text)
        bent = keep_as_sent(client, [*review_form(again.text).items(), (f"none-{asked}", "yes")])
        after = tables(client)
        store = store_of(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        failed = client.post("/parent/inbox/keep", data=review_form(again.text))
        monkeypatch.undo()
        graph = f"apply-{asked}-{boxes(again.text, asked).index('Include a graph.')}"
        both = client.post(
            "/parent/inbox/keep", data={**review_form(again.text), graph: "1", f"none-{asked}": "1"}
        )
        fresh = client.post("/parent/inbox/keep", data={**review_form(again.text), graph: "1"})
        found = resubmission_standing(client)

    assert kept.status_code == 303
    assert refused.status_code == 409
    assert again.status_code == 200
    assert bent.status_code == 422
    assert after == start
    for shown in (refused.text, again.text, bent.text):
        assert words in said_back_on(shown, key)
        assert "Not saved, since what is saved changed" in said_back_on(shown, key)
        assert ticked(shown, the_question_on(shown)) == []
        assert 'value="1" checked' not in shown
    assert str(escape(CHOOSE_INSTRUCTIONS)) in again.text
    kept_words = html.unescape(failed.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert failed.status_code == 500
    assert ("none applies" if choice == "none" else words) in kept_words
    assert both.status_code == 422
    assert str(escape(INSTRUCTIONS_CONTRADICT)) in both.text
    assert f'id="instructions-problem-{asked}"' not in both.text
    assert fresh.status_code == 303
    assert found.texts == ("Include a graph.",)


# ------------------------------------------------------------------ a long choice carried


def long_words(length: int) -> str:
    return "Long instruction: " + "x" * (length - len("Long instruction: "))


@pytest.mark.parametrize("length", [INSTRUCTION_MAX_LENGTH, INSTRUCTION_MAX_LENGTH + 1])
@pytest.mark.parametrize("mixed", [False, True], ids=["alone", "with-a-short-one"])
def test_a_long_saved_instruction_chosen_on_an_old_page_stays_shown_until_a_fresh_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, length: int, mixed: bool
) -> None:
    """A saved instruction of any length, chosen on a page made before another tab saved,
    is shown as not saved through unanswered saves, a form that cannot be read, and a
    reading that fails; nothing is written, and a fresh choice replaces it and saves."""
    words = long_words(length)
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A + "\n", ""))
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        store.put_on_record(
            [
                row.model_copy(
                    update={"note": words, "origins": {**row.origins, "note": SourceChannel.LMS}}
                )
            ],
            {},
        )
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        picks = (words, B) if mixed else (words,)
        old = {**review_form(page), **{f"apply-0-{boxes(page, '0').index(p)}": "1" for p in picks}}
        review = instructions_review_href(row.assignment_id)
        action = instructions_action_href(row.assignment_id)
        other = client.post(
            action, data={**whole_form(client.get(review).text, action), "none": "1"}
        )
        start = tables(client)
        refused = client.post("/parent/inbox/keep", data=old)
        again = client.post("/parent/inbox/keep", data=review_form(refused.text))
        bent = keep_as_sent(client, [*review_form(again.text).items(), ("none-0", "yes")])
        after = tables(client)

        def refuse(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "all_assignments", refuse)
        failed = client.post("/parent/inbox/keep", data=review_form(again.text))
        monkeypatch.undo()
        fresh = client.post(
            "/parent/inbox/keep",
            data={**review_form(again.text), f"apply-0-{boxes(again.text, '0').index(B)}": "1"},
        )
        found = standing(client)

    assert other.status_code == 303
    assert refused.status_code == 409
    assert again.status_code == 200
    assert bent.status_code == 422
    assert after == start
    for shown in (refused.text, again.text, bent.text):
        assert "Not saved, since what is saved changed" in said_back_on(shown, "0")
        assert unsaved(shown, "0") == sorted(picks)
        assert ticked(shown, "0") == []
    kept = html.unescape(failed.text.split('id="kept-answers"', 1)[1].split("</ul>", 1)[0])
    assert failed.status_code == 500
    assert (words in kept) == (length == INSTRUCTION_MAX_LENGTH)
    assert ("one instruction selected by reference" in kept) == (length > INSTRUCTION_MAX_LENGTH)
    assert (B in kept) == mixed
    assert fresh.status_code == 303
    assert found.texts == (B,)


@pytest.mark.parametrize("length", [INSTRUCTION_MAX_LENGTH, INSTRUCTION_MAX_LENGTH + 1])
@pytest.mark.parametrize("mixed", [False, True], ids=["alone", "with-a-short-one"])
def test_a_long_choice_carried_through_the_question_of_which_homework_keeps_its_words(
    tmp_path: pathlib.Path, length: int, mixed: bool
) -> None:
    """A long saved instruction shown as not saved, then carried through a page that asks
    which homework the card is, is shown in its words again once the card is the same
    assignment; nothing is written and nothing is ticked on the way."""
    words = long_words(length)
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A + "\n", ""))
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        store.put_on_record(
            [
                row.model_copy(
                    update={"note": words, "origins": {**row.origins, "note": SourceChannel.LMS}}
                )
            ],
            {},
        )
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        picks = (words, B) if mixed else (words,)
        old = {**review_form(page), **{f"apply-0-{boxes(page, '0').index(p)}": "1" for p in picks}}
        review = instructions_review_href(row.assignment_id)
        action = instructions_action_href(row.assignment_id)
        client.post(action, data={**whole_form(client.get(review).text, action), "none": "1"})
        refused = client.post("/parent/inbox/keep", data=old)
        moved = saved(client, FAR_DUE, **{"occurrence-0": "update"})
        start = tables(client)
        which = client.post("/parent/inbox/keep", data=review_form(refused.text))
        same = client.post(
            "/parent/inbox/keep", data={**review_form(which.text), "occurrence-0": "update"}
        )
        again = client.post("/parent/inbox/keep", data=review_form(same.text))
        after = tables(client)

    assert refused.status_code == 409
    assert moved.status_code == 303
    assert which.status_code == 200
    assert 'name="occurrence-0"' in which.text
    assert ("unsaved_row-0-0-" in which.text) == (length > INSTRUCTION_MAX_LENGTH)
    for shown in (same.text, again.text):
        assert unsaved(shown, "0") == sorted(picks)
        assert "could not be read" not in said_back_on(shown, "0")
        assert ticked(shown, "0") == []
    assert same.status_code == again.status_code == 200
    assert after == start


def test_a_long_choice_made_before_the_question_of_which_homework_keeps_its_words(
    tmp_path: pathlib.Path,
) -> None:
    """A long saved instruction chosen on a card that then asks which homework it is is shown
    in its words once the card is the same assignment; nothing is written on the way."""
    words = long_words(INSTRUCTION_MAX_LENGTH + 1)
    with client_in(tmp_path) as client:
        saved(client, ASSIGNED_WEEK.replace(A + "\n", ""))
        store = store_of(client)
        row = next(item for item in store.all_assignments() if item.title == "Q1 Check 3")
        store.put_on_record(
            [
                row.model_copy(
                    update={"note": words, "origins": {**row.origins, "note": SourceChannel.LMS}}
                )
            ],
            {},
        )
        page = client.post("/parent/inbox/read", data={"text": DUE_WEEK_CHANGED}).text
        old = {**review_form(page), f"apply-0-{boxes(page, '0').index(words)}": "1"}
        saved(client, FAR_DUE, **{"occurrence-0": "update"})
        start = tables(client)
        which = client.post("/parent/inbox/keep", data=old)
        same = client.post(
            "/parent/inbox/keep", data={**review_form(which.text), "occurrence-0": "update"}
        )
        after = tables(client)

    assert which.status_code == same.status_code == 200
    assert "unsaved_row-0-0-" in which.text
    assert unsaved(same.text, "0") == [words]
    assert ticked(same.text, "0") == []
    assert after == start


# ------------------------------------------------------------------ a carried choice folded


@pytest.mark.parametrize("choice", ["none", "one"])
def test_a_choice_carried_on_a_card_that_folds_is_shown_on_the_card_it_joins(
    tmp_path: pathlib.Path, choice: str
) -> None:
    """A choice shown as not saved on the second card of the practice sheet stays shown on
    the first card once the second is folded into it, through another unanswered save;
    nothing is written, and a fresh choice on the first card saves."""
    words = "that no school instruction applies" if choice == "none" else PRACTICE_SHOWN[0]
    with client_in(tmp_path) as client:
        read = client.post("/parent/inbox/read", data={"text": PRACTICE}).text
        apart = client.post(
            "/parent/inbox/keep", data={**review_form(read), "occurrence-1": "new"}
        ).text
        pick = {"none-1": "1"} if choice == "none" else {"apply-1-0": "1"}
        start = tables(client)
        bad = client.post(
            "/parent/inbox/keep", data={**review_form(apart), **pick, "instructions-1": "bad"}
        )
        folded = client.post(
            "/parent/inbox/keep", data={**review_form(bad.text), "occurrence-1": "update"}
        )
        again = client.post("/parent/inbox/keep", data=review_form(folded.text))
        after = tables(client)
        fresh = client.post(
            "/parent/inbox/keep",
            data={**review_form(again.text), "apply-0-0": "1"},
        )

    assert 'name="instructions-1"' in apart
    assert bad.status_code == 422
    assert words in said_back_on(bad.text, "1")
    assert folded.status_code == again.status_code == 200
    for shown in (folded.text, again.text):
        assert the_question_on(shown) == "0"
        assert words in said_back_on(shown, "0")
        assert ticked(shown, "0") == []
    assert after == start
    assert fresh.status_code == 303


def test_a_choice_asked_about_other_homework_is_refused_when_its_card_folds(
    tmp_path: pathlib.Path,
) -> None:
    """A choice made on a card asked about as new work isn't saved onto the assignment the
    card is said to be in the same save; the page that asks about that assignment saves."""
    with client_in(tmp_path) as client:
        read = client.post("/parent/inbox/read", data={"text": PRACTICE}).text
        apart = client.post(
            "/parent/inbox/keep", data={**review_form(read), "occurrence-1": "new"}
        ).text
        start = tables(client)
        moved = client.post(
            "/parent/inbox/keep",
            data={**review_form(apart), "occurrence-1": "update", "apply-1-0": "1"},
        )
        after = tables(client)
        fresh = client.post(
            "/parent/inbox/keep", data={**review_form(moved.text), "apply-0-0": "1"}
        )

    assert 'name="instructions-0"' in apart
    assert 'name="instructions-1"' in apart
    assert moved.status_code == 422
    assert after == start
    assert ticked(moved.text, "0") == []
    assert PRACTICE_SHOWN[0] in said_back_on(moved.text, "0")
    assert fresh.status_code == 303
