"""Review school instructions: the family's later choice of which of the school's instructions
apply to an assignment, and her reading of them without the control.

The page lists every instruction kept for the assignment, those that apply,
those said before, and any waiting for review, and saves an answer through
the store's one rule: a choice made against what stands, or a no-op when
what it asks for already stands. Opening it writes nothing. A parent, or the
household with the sign-in off coming from the family's pages, reaches it;
her sign-in reads the instructions and never the control.
"""

import hashlib
import json
import pathlib
import re
import sqlite3
from datetime import UTC, date, datetime
from html import unescape
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.reconciliation import SourceChannel
from blossom.routes.instruction_answers import review_answer
from blossom.routes.navigation import (
    details_href,
    instructions_action_href,
    instructions_review_href,
)
from blossom.routes.school_instructions import (
    ALREADY_STOOD,
    CHANGED_SINCE_OPENED,
    CHOICES_CONTRADICT,
    FORM_UNREADABLE,
    INSTRUCTIONS_UNREADABLE,
    NOT_ON_RECORD,
    NOT_SAVED,
    NOTHING_CHOSEN,
    SAVED_AS_CHOSEN,
    SAVED_NONE_APPLIES,
    SAVED_SINCE_CHANGED,
    STOOD_NONE_APPLIES,
    STOOD_SINCE_CHANGED,
    result_token,
)
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
    ESSAY_ID,
    ESSAY_TITLE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    THEIRS,
    Answer,
    as_a_browser_sends,
    client_for,
    fixture_settings,
    signed_in,
    signed_in_household,
    state_of,
    store_of,
    whole_form,
)

A = "Outline three causes before drafting."
B = "Compare two canals in the conclusion."
C = "Cite the map handout."
NOW = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
TODAY = date(2026, 8, 19)
PAGE = instructions_review_href(ESSAY_ID)
ACTION = instructions_action_href(ESSAY_ID)


def open_household(tmp_path: pathlib.Path) -> Settings:
    """The pinned day with the sign-in off, its files under ``tmp_path``."""
    return fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )


def lands_on_result(location: str, kind: str) -> bool:
    """Whether a save's redirect names this result for the essay and lands on it."""
    return (
        re.fullmatch(rf"{re.escape(PAGE)}\?result={kind}\.\d+\.[0-9a-f]{{16}}#result", location)
        is not None
    )


def a_current_b_earlier(store: ProjectStateStore) -> None:
    """A applies, read on the Assigned card; B was pasted later and kept as history."""
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(A, SourceChannel.LMS, "assigned", date(2026, 8, 17))],
        None,
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(B, SourceChannel.LMS, "due", date(2026, 8, 21))],
        InstructionChoice(1, (A, B), frozenset({A})),
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )


def with_awaiting(settings: Settings, text: str) -> None:
    """A school note written into the old note field after the upgrade, as a release from
    before the instructions were kept apart would write it; the next start finds it."""
    connection = sqlite3.connect(settings.database_path)
    with connection:
        (origins,) = connection.execute(
            "SELECT origins FROM assignments WHERE assignment_id = ?", (ESSAY_ID,)
        ).fetchone()
        marks = {**(json.loads(origins) if origins else {}), "note": "LMS"}
        connection.execute(
            "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
            (text, json.dumps(marks), ESSAY_ID),
        )
    connection.close()


def standing(client: TestClient) -> InstructionsStanding:
    return store_of(client).school_instruction_readings([ESSAY_ID]).readable[ESSAY_ID]


def tables(client: TestClient) -> dict[str, list[tuple[object, ...]]]:
    connection = store_of(client)._connection
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in ("assignments", "school_instructions")
    }


def boxes(page: str) -> list[tuple[str, bool, str]]:
    """Each instruction box on the page: its words, whether it is ticked, and what the page
    says of it beside the words."""
    found = re.findall(
        r'<input type="checkbox" name="apply-\d+" value="1"( checked)?> '
        r'<span><q class="authored-text">(.*?)</q> <span class="source">(.*?)</span></span>',
        page,
        re.S,
    )
    return [(unescape(text), bool(on), unescape(said)) for on, text, said in found]


def the_form(page: str, *ticked: str, none: bool = False) -> dict[str, str]:
    """The page's own form as a browser sends it, with these boxes ticked by their words."""
    form = whole_form(page, ACTION)
    for number, (text, _, _) in enumerate(boxes(page)):
        name = f"apply-{number}"
        if text in ticked:
            form[name] = "1"
        else:
            form.pop(name, None)
    if none:
        form["none"] = "1"
    else:
        form.pop("none", None)
    return form


def test_the_review_lists_every_kept_instruction_and_opening_it_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    with_awaiting(settings, C)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        before = tables(client)
        page = client.get(PAGE, headers=PAGE_HEADERS)
        after = tables(client)

    assert page.status_code == 200
    assert after == before
    assert "<h1>Review school instructions</h1>" in page.text
    assert str(escape(ESSAY_TITLE)) in page.text
    shown = boxes(page.text)
    assert [text for text, _, _ in shown] == [A, B, C]
    assert not any(on for _, on, _ in shown)
    assert shown[0][2].startswith("Applies now.")
    assert "Read on the school's card for August 17" in shown[0][2]
    assert shown[1][2].startswith("Earlier, does not apply now.")
    assert shown[2][2].startswith("Waiting for review.")
    assert 'name="none" value="1">' in page.text
    assert "These are the school's words" in page.text
    assert "the school's record is not changed" in page.text


def test_restoring_an_earlier_one_retires_the_current_and_settles_the_awaiting(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    with_awaiting(settings, C)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        was = standing(client).revision
        saved = client.post(ACTION, data=the_form(page, B))
        found = standing(client)
        landed = client.get(saved.headers["location"], headers=PAGE_HEADERS)

    assert saved.status_code == 303
    assert lands_on_result(saved.headers["location"], "saved")
    assert found.texts == (B,)
    assert sorted(item.text for item in found.history) == sorted([A, C])
    assert found.awaiting == ()
    assert found.revision == was + 1
    assert {item.settled_by for item in found.current + found.history} == {"parent"}
    assert str(escape(SAVED_AS_CHOSEN)) in landed.text


def test_nothing_ticked_is_no_answer_and_none_beside_one_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        a_current_b_earlier(store_of(client))
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        before = tables(client)
        nothing = client.post(ACTION, data=the_form(page))
        both = client.post(ACTION, data=the_form(page, B, none=True))
        after = tables(client)
        none = client.post(ACTION, data=the_form(page, none=True))
        found = standing(client)

    assert nothing.status_code == 422
    assert str(escape(NOTHING_CHOSEN)) in nothing.text
    assert both.status_code == 422
    assert str(escape(CHOICES_CONTRADICT)) in both.text
    assert [text for text, on, _ in boxes(both.text) if on] == [B]
    assert 'name="none" value="1" checked>' in both.text
    assert after == before
    assert none.status_code == 303
    assert found.texts == ()
    assert sorted(item.text for item in found.history) == sorted([A, B])


def test_an_old_form_after_a_change_back_is_refused_and_one_asking_what_stands_is_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    """The form is made with A applying. B is chosen elsewhere, then A again. The old form's
    texts and states match what stands, and it asks for B: refused, with the choice not
    saved shown beside the facts as they stand. The same old form asking for A asks for
    what already stands, and saves nothing."""
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        store = store_of(client)
        a_current_b_earlier(store)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(2, (A, B), frozenset({B})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(3, (A, B), frozenset({A})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        before = tables(client)
        refused = client.post(ACTION, data=the_form(page, B))
        after_refused = tables(client)
        stood = client.post(ACTION, data=the_form(page, A))
        after_stood = tables(client)
        found = standing(client)

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_OPENED)) in refused.text
    not_saved = re.search(
        r'<p class="problem" id="not-saved" tabindex="-1">(.*?)</p>', refused.text, re.S
    )
    assert not_saved is not None
    assert [unescape(text) for text in re.findall(r"<q[^>]*>(.*?)</q>", not_saved.group(1))] == [B]
    assert whole_form(refused.text, ACTION)["revision"] == "4"
    assert not any(on for _, on, _ in boxes(refused.text))
    assert after_refused == before
    assert stood.status_code == 303
    assert lands_on_result(stood.headers["location"], "stood")
    assert after_stood == before
    assert found.texts == (A,)
    assert found.revision == 4


def test_a_retry_after_saving_saves_nothing_more(tmp_path: pathlib.Path) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        a_current_b_earlier(store_of(client))
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        first = client.post(ACTION, data=the_form(page, A, B))
        after_first = tables(client)
        again = client.post(ACTION, data=the_form(page, A, B))
        after_again = tables(client)
        landed = client.get(again.headers["location"], headers=PAGE_HEADERS)

    assert lands_on_result(first.headers["location"], "saved")
    assert again.status_code == 303
    assert lands_on_result(again.headers["location"], "stood")
    assert after_again == after_first
    assert str(escape(ALREADY_STOOD)) in landed.text


def test_her_sign_in_reads_the_instructions_and_never_the_control(tmp_path: pathlib.Path) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    details = details_href(ESSAY_ID)
    with client_for(settings) as client:
        signed_in(client, HERS)
        hers = client.get(details, headers=PAGE_HEADERS)
        before = tables(client)
        opened = client.get(PAGE, headers=PAGE_HEADERS)
        pressed = client.post(ACTION, data={"revision": "2", "instruction-0": A, "apply-0": "1"})
        after = tables(client)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        theirs = client.get(details_href(ESSAY_ID, return_to="family"), headers=PAGE_HEADERS)

    assert hers.status_code == 200
    assert "From the school:" in hers.text
    assert str(escape(A)) in hers.text
    assert "Review school instructions" not in hers.text
    assert PAGE not in hers.text
    assert opened.status_code == 403
    assert pressed.status_code == 403
    assert after == before
    assert theirs.status_code == 200
    assert f'<a href="{PAGE}">Review school instructions</a>' in theirs.text


def test_the_open_household_reaches_the_control_through_the_familys_pages(
    tmp_path: pathlib.Path,
) -> None:
    settings = open_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
        as_hers = client.get(details_href(ESSAY_ID, return_to="week"), headers=PAGE_HEADERS)
        as_family = client.get(details_href(ESSAY_ID, return_to="family"), headers=PAGE_HEADERS)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        saved = client.post(ACTION, data=the_form(page, B))
        found = standing(client)

    assert "Review school instructions" not in as_hers.text
    assert f'<a href="{PAGE}">Review school instructions</a>' in as_family.text
    assert saved.status_code == 303
    assert found.texts == (B,)
    assert found.current[0].settled_by == "household"


def test_an_assignment_not_on_record_has_no_review(tmp_path: pathlib.Path) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        before = tables(client)
        opened = client.get(instructions_review_href("assignment-nowhere"), headers=PAGE_HEADERS)
        pressed = client.post(
            instructions_action_href("assignment-nowhere"),
            data={"revision": "0", "instruction-0": A, "apply-0": "1"},
        )
        after = tables(client)

    assert opened.status_code == 404
    assert str(escape(NOT_ON_RECORD)) in opened.text
    assert pressed.status_code == 404
    assert after == before


def test_an_assignment_with_no_instruction_offers_nothing_to_review(tmp_path: pathlib.Path) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        details = client.get(details_href(ESSAY_ID, return_to="family"), headers=PAGE_HEADERS)
        page = client.get(PAGE, headers=PAGE_HEADERS)

    assert "Review school instructions</a>" not in details.text
    assert "From the school:" not in details.text
    assert page.status_code == 200
    assert "No instruction from the school is saved for this assignment." in page.text
    assert f'action="{ACTION}"' not in page.text


def test_an_instruction_waiting_for_review_is_named_on_the_family_page(
    tmp_path: pathlib.Path,
) -> None:
    settings = signed_in_household(tmp_path)
    with client_for(settings) as client:
        a_current_b_earlier(store_of(client))
    with_awaiting(settings, C)
    with client_for(settings) as client:
        signed_in(client, THEIRS)
        waiting = client.get("/parent", headers=PAGE_HEADERS).text
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        client.post(ACTION, data=the_form(page, A, C))
        settled = client.get("/parent", headers=PAGE_HEADERS).text
        found = standing(client)

    section = waiting.split('id="school-instructions-to-review"', 1)[1].split("</section>", 1)[0]
    assert str(escape(ESSAY_TITLE)) in section
    assert str(escape(C)) in section
    assert f'<a href="{PAGE}">Review school instructions</a>' in section
    assert 'id="school-instructions-to-review"' not in settled
    assert found.texts == tuple(sorted([A, C]))


def test_the_answer_is_read_with_none_left_out_ticked_alone_or_refused_when_malformed() -> None:
    """A browser leaves an unticked box out of the form: a tick with no ``none`` field is an
    answer, ``none`` alone is one, and ``none`` with any other value is no form this page
    made."""
    shown = {"revision": "2", "instruction-0": to_wire(A), "instruction-1": to_wire(B)}

    assert review_answer({**shown, "apply-1": "1"}) == SubmittedChoice(
        shown_revision=2, shown=(A, B), applies=frozenset({B}), none_applies=False
    )
    assert review_answer({**shown, "none": "1"}) == SubmittedChoice(
        shown_revision=2, shown=(A, B), applies=frozenset(), none_applies=True
    )
    assert review_answer({**shown, "apply-1": "1", "none": "on"}) is None
    assert review_answer({**shown, "none": ""}) is None


# ------------------------------------------------------------ the words as a browser sends them

MULTI = "First paragraph.\nSecond paragraph."
QUOTED = 'Say "only the odd ones" \U0001f33c.\nThen stop.'


def multiline_kept(store: ProjectStateStore) -> None:
    """MULTI was said first and is kept as history; QUOTED applies."""
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(MULTI, SourceChannel.LMS)],
        None,
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(QUOTED, SourceChannel.LMS)],
        InstructionChoice(1, (MULTI, QUOTED), frozenset({QUOTED})),
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )


@pytest.mark.parametrize("choice", ["the-earlier-one", "the-one-that-applies", "none"])
def test_a_browser_sends_multiline_instructions_back_as_they_were(
    tmp_path: pathlib.Path, choice: str
) -> None:
    """A browser sends every line break as a carriage return and a line feed; the words ride
    on the wire, so the choice is saved as made, and the same submission again stands."""
    with client_for(open_household(tmp_path)) as client:
        multiline_kept(store_of(client))
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        picked = {"the-earlier-one": MULTI, "the-one-that-applies": QUOTED}.get(choice)
        form = the_form(page, picked) if picked else the_form(page, none=True)
        sent = as_a_browser_sends(form)
        first = client.post(ACTION, data=sent)
        after_first = tables(client)
        again = client.post(ACTION, data=sent)
        after_again = tables(client)
        found = standing(client)

    assert lands_on_result(
        first.headers["location"], "stood" if choice == "the-one-that-applies" else "saved"
    )
    assert lands_on_result(again.headers["location"], "stood")
    assert after_again == after_first
    assert found.texts == (() if picked is None else (picked,))
    assert sorted(item.text for item in found.kept) == sorted([MULTI, QUOTED])


# ------------------------------------------------------------------ a contradiction gone stale


@pytest.mark.parametrize("elsewhere", ["another-chosen", "a-change-back"])
def test_a_contradiction_made_against_changed_instructions_is_not_put_right_onto_them(
    tmp_path: pathlib.Path, elsewhere: str
) -> None:
    """A applies and B was said before; the page ticks A and that none applies. Before it is
    sent, B is chosen elsewhere, or B and then A again. The page says the instructions
    changed, says the answer as not saved, and ticks nothing, so undoing the contradiction
    alone chooses nothing, and what was chosen elsewhere stands."""
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        old = the_form(page, A, none=True)
        picks = (
            [frozenset({B})] if elsewhere == "another-chosen" else [frozenset({B}), frozenset({A})]
        )
        for number, pick in enumerate(picks):
            store.settle_school_instructions(
                ESSAY_ID,
                [],
                InstructionChoice(2 + number, (A, B), pick),
                authored_by="parent",
                now=NOW,
                today=TODAY,
            )
        chosen_elsewhere = standing(client).texts
        refused = client.post(ACTION, data=old)
        fresh = whole_form(refused.text, ACTION)
        corrected = client.post(ACTION, data=fresh)
        found = standing(client)

    not_saved = re.search(
        r'<p class="problem" id="not-saved" tabindex="-1">(.*?)</p>', refused.text, re.S
    )
    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_OPENED)) in refused.text
    assert not_saved is not None
    assert (
        unescape(not_saved.group(1))
        == "Not saved: "
        + f'<q class="authored-text">{A}</q>'
        + ", and that no school instruction applies"
    )
    assert not any(on for _, on, _ in boxes(refused.text))
    assert 'name="none" value="1" checked' not in refused.text
    assert not any(name.startswith(("apply-", "none")) for name in fresh)
    assert corrected.status_code == 422
    assert str(escape(NOTHING_CHOSEN)) in corrected.text
    assert found.texts == chosen_elsewhere


# ------------------------------------------------------------------ a choice that is not saved

MARKED_UP = "Use the <b>blue</b> sheet.\nNot the red one."


@pytest.mark.parametrize("failure", ["write", "write-then-read", "unreadable", "missing"])
def test_a_choice_not_saved_is_kept_whole_and_tried_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A write the file refuses, a reading back that fails too, instructions that cannot be
    read, or an assignment gone: the choice is said as not saved, its words shown as written,
    with the focus on what happened and a way to what is there. The write is tried once, the
    page read back once at most, and the page that reads no store reads nothing."""
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(A, SourceChannel.LMS)],
            None,
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        store.settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(MARKED_UP, SourceChannel.LMS)],
            InstructionChoice(1, (A, MARKED_UP), frozenset({A})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        form = the_form(client.get(PAGE, headers=PAGE_HEADERS).text, MARKED_UP)
        tries: list[str] = []

        def refuse_the_write(*_: object, **__: object) -> None:
            tries.append("write")
            if failure == "write-then-read":
                monkeypatch.setattr(store, "one_assignment", refuse_the_reading)
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        def refuse_the_reading(*_: object, **__: object) -> None:
            tries.append("read")
            msg = "the disk refused again"
            raise sqlite3.OperationalError(msg)

        if failure.startswith("write"):
            monkeypatch.setattr(store, "settle_school_instructions", refuse_the_write)
        elif failure == "unreadable":
            store._connection.execute(
                "UPDATE school_instructions SET state = 'bent' WHERE text = ?", (MARKED_UP,)
            )
            store._connection.commit()
        else:
            store._connection.execute(
                "DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,)
            )
            store._connection.commit()
        before = tables(client)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        answer = client.post(ACTION, data=form)
        store._connection.set_trace_callback(None)
        after = tables(client)

    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', answer.text, re.S
    )
    assert (
        answer.status_code
        == {"write": 500, "write-then-read": 500, "unreadable": 500, "missing": 404}[failure]
    )
    assert after == before
    assert (
        '<q class="authored-text">Use the &lt;b&gt;blue&lt;/b&gt; sheet.\nNot the red one.</q>'
        in answer.text
    )
    assert 'id="not-saved"' in answer.text
    assert answer.text.count("autofocus") == 1
    assert summary is not None
    target = re.search(r'<a href="#([^"]+)">', summary.group(1))
    assert target is not None
    assert f'id="{target.group(1)}"' in answer.text
    if failure == "write":
        assert tries == ["write"]
        assert str(escape(NOT_SAVED)) in answer.text
        assert target.group(1) == "instruction-choice"
        assert [text for text, on, _ in boxes(answer.text) if on] == [MARKED_UP]
    if failure == "write-then-read":
        assert tries == ["write", "read"]
        assert f'action="{ACTION}"' not in answer.text
        assert target.group(1) == "not-saved"
        assert sum(text.startswith("BEGIN") for text in statements) == 1
    if failure == "unreadable":
        assert str(escape(INSTRUCTIONS_UNREADABLE)) in answer.text
        assert f'action="{ACTION}"' not in answer.text
    if failure == "missing":
        assert str(escape(NOT_ON_RECORD)) in answer.text


# ------------------------------------------------------------------ what a save says afterward


def test_a_result_says_a_choice_applies_only_while_its_revision_stands(
    tmp_path: pathlib.Path,
) -> None:
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)
        saved = client.post(ACTION, data=the_form(client.get(PAGE, headers=PAGE_HEADERS).text, B))
        where = saved.headers["location"]
        landed = client.get(where, headers=PAGE_HEADERS).text
        visited_again = client.get(where, headers=PAGE_HEADERS).text
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(3, (A, B), frozenset({A})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        after_another = client.get(where, headers=PAGE_HEADERS).text
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(4, (A, B), frozenset({B})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        after_a_change_back = client.get(where, headers=PAGE_HEADERS).text

    assert lands_on_result(where, "saved")
    for page in (landed, visited_again):
        assert str(escape(SAVED_AS_CHOSEN)) in page
        assert '<p class="note" role="status" id="result" tabindex="-1">' in page
        assert "autofocus" not in page
    for page in (after_another, after_a_change_back):
        assert str(escape(SAVED_AS_CHOSEN)) not in page
        assert str(escape(SAVED_SINCE_CHANGED)) in page


@pytest.mark.parametrize(
    "result",
    [
        "?saved=1",
        "?saved=0",
        "?result=saved.3.0000000000000000",
        "?result=saved.3",
        "?result=kept.3.0000000000000000",
        "another-assignments",
        "a-revision-not-reached",
    ],
    ids=[
        "an-old-flag",
        "the-other-old-flag",
        "a-made-up-check",
        "no-check",
        "a-made-up-kind",
        "another-assignments",
        "a-revision-not-reached",
    ],
)
def test_a_result_the_save_did_not_write_says_nothing(tmp_path: pathlib.Path, result: str) -> None:
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(2, (A, B), frozenset({B})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        key = state_of(client).result_key
        if result == "another-assignments":
            result = "?result=" + result_token(key, "assignment-another-one", "saved", 3)
        elif result == "a-revision-not-reached":
            result = "?result=" + result_token(key, ESSAY_ID, "saved", 9)
        page = client.get(PAGE + result, headers=PAGE_HEADERS).text

    assert 'id="result"' not in page
    assert str(escape(SAVED_AS_CHOSEN)) not in page
    assert str(escape(ALREADY_STOOD)) not in page


# ------------------------------------------------------------------ words never kept


def test_a_choice_of_words_never_kept_is_refused_and_claims_nothing(tmp_path: pathlib.Path) -> None:
    """Every instruction is retired; the form names words never kept and ticks them. Nothing
    stands to match, so nothing is claimed and nothing is written."""
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(2, (A, B), frozenset(), True),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        form = whole_form(client.get(PAGE, headers=PAGE_HEADERS).text, ACTION)
        form["instruction-0"] = to_wire("Never kept.")
        form["apply-0"] = "1"
        before = tables(client)
        refused = client.post(ACTION, data=form)
        after = tables(client)

    assert refused.status_code == 409
    assert str(escape(CHANGED_SINCE_OPENED)) in refused.text
    assert str(escape(ALREADY_STOOD)) not in refused.text
    assert after == before


# ------------------------------------------------------------------ where the focus lands


@pytest.mark.parametrize(
    "refusal", ["nothing-ticked", "contradiction", "stale", "unreadable", "missing"]
)
def test_a_refused_choice_puts_the_focus_on_one_summary_that_leads_to_what_is_there(
    tmp_path: pathlib.Path, refusal: str
) -> None:
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        form = {
            "nothing-ticked": the_form(page),
            "contradiction": the_form(page, B, none=True),
        }.get(refusal, the_form(page, B))
        if refusal == "stale":
            store.settle_school_instructions(
                ESSAY_ID,
                [],
                InstructionChoice(2, (A, B), frozenset({B})),
                authored_by="parent",
                now=NOW,
                today=TODAY,
            )
            store.settle_school_instructions(
                ESSAY_ID,
                [],
                InstructionChoice(3, (A, B), frozenset({A})),
                authored_by="parent",
                now=NOW,
                today=TODAY,
            )
        if refusal == "unreadable":
            store._connection.execute("UPDATE school_instructions SET state = 'bent'")
            store._connection.commit()
        if refusal == "missing":
            store._connection.execute(
                "DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,)
            )
            store._connection.commit()
        refused = client.post(ACTION, data=form)

    summary = re.search(
        r'<p class="problem" role="alert" id="problem-summary"[^>]*>(.*?)</p>', refused.text, re.S
    )
    assert refused.text.count("autofocus") == 1
    assert summary is not None
    assert 'tabindex="-1" autofocus' in summary.group(0)
    target = re.search(r'<a href="#([^"]+)">', summary.group(1))
    assert target is not None
    assert f'id="{target.group(1)}"' in refused.text
    if refusal in ("nothing-ticked", "contradiction", "stale"):
        assert target.group(1) == "instruction-choice"
        assert (
            'id="instruction-choice" tabindex="-1" aria-describedby="problem-summary"'
            in refused.text
        )
    else:
        assert target.group(1) == "not-saved"


def test_an_ordinary_visit_asks_for_no_focus(tmp_path: pathlib.Path) -> None:
    with client_for(open_household(tmp_path)) as client:
        a_current_b_earlier(store_of(client))
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        gone = client.get(instructions_review_href("assignment-nowhere"), headers=PAGE_HEADERS).text

    assert "autofocus" not in page
    assert "autofocus" not in gone


# ------------------------------------------------------------------ where an instruction was read


def test_where_an_instruction_was_read_is_said_as_far_as_it_is_known(
    tmp_path: pathlib.Path,
) -> None:
    """An instruction a save kept with no card is from somewhere not known; one carried from
    the old note field says so; one read on a card names the card's day."""
    carried = open_household(tmp_path / "carried")
    (tmp_path / "carried").mkdir()
    with client_for(carried):
        pass
    with_awaiting(carried, C)
    with client_for(carried) as client:
        from_the_field = client.get(PAGE, headers=PAGE_HEADERS).text
    (tmp_path / "saved").mkdir()
    with client_for(open_household(tmp_path / "saved")) as client:
        store_of(client).settle_school_instructions(
            ESSAY_ID,
            [InstructionSeen(A, SourceChannel.LMS)],
            None,
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        cardless = client.get(PAGE, headers=PAGE_HEADERS).text

    assert boxes(from_the_field)[0][2] == (
        "Applies now. Kept from the note saved before the school's instructions were kept apart."
    )
    assert boxes(cardless)[0][2] == "Applies now. Where it was read is not known."


# ------------------------------------------------------------------ a result only a save writes


def test_a_result_this_process_did_not_sign_says_nothing(tmp_path: pathlib.Path) -> None:
    """A result's check is keyed by the running process: one worked out from what the address
    shows, or signed before a restart, says nothing; one this process signed says what the save
    did."""
    with client_for(open_household(tmp_path)) as client:
        a_current_b_earlier(store_of(client))
        revision = standing(client).revision
        unkeyed = hashlib.sha256(f"{ESSAY_ID}\nsaved\n{revision}".encode()).hexdigest()[:16]
        tokens = {
            "unkeyed": f"saved.{revision}.{unkeyed}",
            "another-process": result_token(b"a key from before a restart", ESSAY_ID, "saved", 2),
            "signed": result_token(state_of(client).result_key, ESSAY_ID, "saved", revision),
        }
        pages = {
            name: client.get(PAGE, params={"result": token}, headers=PAGE_HEADERS).text
            for name, token in tokens.items()
        }

    assert 'id="result"' not in pages["unkeyed"]
    assert 'id="result"' not in pages["another-process"]
    assert str(escape(SAVED_AS_CHOSEN)) in pages["signed"]


@pytest.mark.parametrize(
    "token",
    [
        "saved.2.\u00e9",
        "saved.2.\U0001f600",
        "saved.2.000000000000000\u00e9",
        "saved.2." + "\U00010400" * 16,
        "saved.2." + "0" * 64,
        "saved.2.{check}.more",
        "saved..{check}",
        "saved.02.{check}",
        "saved.-2.{check}",
        "saved.\u0662.{check}",
        "saved.2.{upper}",
        "saved.2.{check}\n",
    ],
    ids=[
        "an-accent",
        "an-emoji",
        "mixed",
        "beyond-the-plane",
        "oversized",
        "a-separator-more",
        "no-revision",
        "a-padded-revision",
        "a-signed-revision",
        "another-scripts-digit",
        "upper-case",
        "a-line-after",
    ],
)
def test_a_result_address_of_any_other_shape_says_nothing_and_writes_nothing(
    tmp_path: pathlib.Path, token: str
) -> None:
    with client_for(open_household(tmp_path)) as client:
        a_current_b_earlier(store_of(client))
        check = result_token(state_of(client).result_key, ESSAY_ID, "saved", 2).rsplit(".", 1)[1]
        address = token.replace("{check}", check).replace("{upper}", check.upper())
        before = tables(client)
        page = client.get(PAGE, params={"result": address}, headers=PAGE_HEADERS)
        after = tables(client)

    assert page.status_code == 200
    assert 'id="result"' not in page.text
    assert after == before


# ------------------------------------------------------------------ none applying, said as none


def test_none_applying_is_said_in_its_own_words_on_its_result(tmp_path: pathlib.Path) -> None:
    """A save that none applies says so, and sending it again says nothing changed; a choice
    of some keeps its own sentence; either result, opened after the instructions changed
    again, says they have changed since."""
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        a_current_b_earlier(store)

        def press(*ticked: str, none: bool = False) -> str:
            page = client.get(PAGE, headers=PAGE_HEADERS).text
            answer = client.post(ACTION, data=the_form(page, *ticked, none=none))
            assert answer.status_code == 303, answer.text
            return str(answer.headers["location"])

        none_saved = press(none=True)
        none_landed = client.get(none_saved, headers=PAGE_HEADERS).text
        none_again = press(none=True)
        none_again_landed = client.get(none_again, headers=PAGE_HEADERS).text
        some_saved = press(A)
        some_landed = client.get(some_saved, headers=PAGE_HEADERS).text
        some_again = press(A)
        some_again_landed = client.get(some_again, headers=PAGE_HEADERS).text
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(standing(client).revision, (A, B), frozenset({B})),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        none_later = client.get(none_saved, headers=PAGE_HEADERS).text
        none_again_later = client.get(none_again, headers=PAGE_HEADERS).text
        store.settle_school_instructions(
            ESSAY_ID,
            [],
            InstructionChoice(standing(client).revision, (A, B), none_applies=True),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
        none_after_none_again = client.get(none_saved, headers=PAGE_HEADERS).text

    assert lands_on_result(none_saved, "saved")
    assert lands_on_result(none_again, "stood")
    assert str(escape(SAVED_NONE_APPLIES)) in none_landed
    assert str(escape(SAVED_AS_CHOSEN)) not in none_landed
    assert str(escape(STOOD_NONE_APPLIES)) in none_again_landed
    assert str(escape(ALREADY_STOOD)) not in none_again_landed
    assert str(escape(SAVED_AS_CHOSEN)) in some_landed
    assert str(escape(SAVED_NONE_APPLIES)) not in some_landed
    assert str(escape(ALREADY_STOOD)) in some_again_landed
    assert str(escape(SAVED_SINCE_CHANGED)) in none_later
    assert str(escape(STOOD_SINCE_CHANGED)) in none_again_later
    assert str(escape(SAVED_SINCE_CHANGED)) in none_after_none_again
    for page in (none_later, none_again_later, none_after_none_again):
        assert str(escape(SAVED_NONE_APPLIES)) not in page
        assert str(escape(STOOD_NONE_APPLIES)) not in page


# ------------------------------------------------------------------ a form the page did not write


def marked_up_earlier(store: ProjectStateStore) -> None:
    """A applies; the words with markup and a line break were said before, as history."""
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(A, SourceChannel.LMS)],
        None,
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )
    store.settle_school_instructions(
        ESSAY_ID,
        [InstructionSeen(MARKED_UP, SourceChannel.LMS)],
        InstructionChoice(1, (A, MARKED_UP), frozenset({A})),
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )


def post_as_sent(client: TestClient, fields: list[tuple[str, str]]) -> Answer:
    return client.post(
        ACTION,
        content=urlencode(fields),
        headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )


def damaged(form: dict[str, str], damage: str) -> list[tuple[str, str]]:
    """The page's own form, as a browser would send it, with one thing the page never writes."""
    fields = list(form.items())
    if damage == "the-revision-twice":
        return [*fields, ("revision", form["revision"])]
    if damage == "a-box-twice":
        return [*fields, ("apply-1", "1")]
    if damage == "a-box-twice-the-second-another-way":
        return [*fields, ("apply-1", "0")]
    if damage == "a-field-the-page-never-writes":
        return [*fields, ("unexpected", "yes")]
    if damage == "a-revision-not-a-count":
        return [(name, "invalid" if name == "revision" else value) for name, value in fields]
    if damage == "a-padded-revision":
        return [(name, "0" + value if name == "revision" else value) for name, value in fields]
    return [*fields, ("apply-0", "yes")]


def not_saved_words(page: str) -> list[str]:
    said = re.search(r'<p class="problem" id="not-saved"[^>]*>(.*?)</p>', page, re.S)
    return (
        []
        if said is None
        else re.findall(r'<q class="authored-text">(.*?)</q>', said.group(1), re.S)
    )


@pytest.mark.parametrize(
    "damage",
    [
        "the-revision-twice",
        "a-box-twice",
        "a-box-twice-the-second-another-way",
        "a-field-the-page-never-writes",
        "a-revision-not-a-count",
        "a-padded-revision",
        "another-box-ticked-another-way",
    ],
)
def test_a_form_that_cannot_be_read_whole_says_back_the_choice_it_could_read(
    tmp_path: pathlib.Path, damage: str
) -> None:
    """Whatever makes the form one the page did not write, nothing is written and no box comes
    ticked; the choice that can be read is said back, escaped, as the parent's unsaved choice,
    with the focus on what happened. A box ticked another way is no choice and is not said."""
    with client_for(open_household(tmp_path)) as client:
        marked_up_earlier(store_of(client))
        form = the_form(client.get(PAGE, headers=PAGE_HEADERS).text, MARKED_UP)
        before = tables(client)
        refused = post_as_sent(client, damaged(form, damage))
        after = tables(client)

    assert refused.status_code == 422
    assert after == before
    assert str(escape(FORM_UNREADABLE)) in refused.text
    assert '<h2 class="update-heading">Your unsaved choice</h2>' in refused.text
    assert not_saved_words(refused.text) == [str(escape(MARKED_UP))]
    assert [text for text, on, _ in boxes(refused.text) if on] == []
    assert refused.text.count("autofocus") == 1


def test_a_box_whose_words_cannot_be_read_is_never_said(tmp_path: pathlib.Path) -> None:
    with client_for(open_household(tmp_path)) as client:
        marked_up_earlier(store_of(client))
        form = the_form(client.get(PAGE, headers=PAGE_HEADERS).text, MARKED_UP)
        form["instruction-1"] = "Words the page never wrote."
        refused = client.post(ACTION, data=form, headers=PAGE_HEADERS)

    assert refused.status_code == 422
    assert "Words the page never wrote." not in refused.text
    assert not_saved_words(refused.text) == []


def test_a_form_that_cannot_be_read_then_a_reading_that_fails_says_back_the_same_choice(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form is refused and the page cannot read the record back: the page that reads no
    store says back the same choice, and nothing more is read."""
    with client_for(open_household(tmp_path)) as client:
        store = store_of(client)
        marked_up_earlier(store)
        form = the_form(client.get(PAGE, headers=PAGE_HEADERS).text, MARKED_UP)

        def refuse_the_reading(*_: object, **__: object) -> None:
            msg = "the disk refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "one_assignment", refuse_the_reading)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        refused = post_as_sent(client, damaged(form, "a-padded-revision"))
        store._connection.set_trace_callback(None)

    assert refused.status_code == 422
    assert f'action="{ACTION}"' not in refused.text
    assert not_saved_words(refused.text) == [str(escape(MARKED_UP))]
    assert not any(text.lstrip().upper().startswith("SELECT") for text in statements)
    assert '<a href="#not-saved">' in refused.text


# ------------------------------------------------------------------ a note longer than a paste

LONG_NOTE = "Read every chapter, then answer each question in full. " * 730


def test_a_long_instruction_kept_from_before_is_chosen_through_the_page_by_its_row(
    tmp_path: pathlib.Path,
) -> None:
    """A school note longer than any paste, kept from before, is listed whole and travels in the
    form by its row: the parent chooses none, then restores it, with the page's own form; an
    ordinary instruction on another assignment travels in its words and saves as before."""
    assert len(LONG_NOTE) > INSTRUCTION_MAX_LENGTH
    settings = open_household(tmp_path)
    with client_for(settings) as client:
        store = store_of(client)
        other = next(
            row.assignment_id for row in store.all_assignments() if row.assignment_id != ESSAY_ID
        )
        store.settle_school_instructions(
            other,
            [InstructionSeen(C, SourceChannel.LMS)],
            None,
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )
    with_awaiting(settings, LONG_NOTE)
    with client_for(settings) as client:
        page = client.get(PAGE, headers=PAGE_HEADERS).text
        form = whole_form(page, ACTION)
        none = client.post(ACTION, data=the_form(page, none=True))
        none_standing = standing(client)
        restored = client.post(
            ACTION, data=the_form(client.get(PAGE, headers=PAGE_HEADERS).text, LONG_NOTE)
        )
        final = standing(client)
        ordinary_page = client.get(instructions_review_href(other), headers=PAGE_HEADERS).text
        ordinary_form = whole_form(ordinary_page, instructions_action_href(other))
        ordinary = client.post(instructions_action_href(other), data={**ordinary_form, "none": "1"})

    assert "row-0" in form
    assert "instruction-0" not in form
    assert LONG_NOTE in unescape(page)
    assert none.status_code == 303
    assert none_standing.texts == ()
    assert restored.status_code == 303
    assert final.texts == (LONG_NOTE,)
    assert "instruction-0" in ordinary_form
    assert ordinary.status_code == 303
