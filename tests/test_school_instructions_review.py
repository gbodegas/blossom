"""Review school instructions: the family's later choice of which of the school's instructions
apply to an assignment, and her reading of them without the control.

The page lists every instruction kept for the assignment, those that apply,
those said before, and any waiting for review, and saves an answer through
the store's one rule: a choice made against what stands, or a no-op when
what it asks for already stands. Opening it writes nothing. A parent, or the
household with the sign-in off coming from the family's pages, reaches it;
her sign-in reads the instructions and never the control.
"""

import json
import pathlib
import re
import sqlite3
from datetime import UTC, date, datetime
from html import unescape

from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.reconciliation import SourceChannel
from blossom.routes.navigation import (
    details_href,
    instructions_action_href,
    instructions_review_href,
)
from blossom.routes.school_instructions import (
    ALREADY_STOOD,
    CHANGED_SINCE_OPENED,
    CHOICES_CONTRADICT,
    NOT_ON_RECORD,
    NOTHING_CHOSEN,
    SAVED_AS_CHOSEN,
)
from blossom.school_instructions import (
    InstructionChoice,
    InstructionSeen,
    InstructionsStanding,
)
from blossom.settings import Settings
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    fixture_settings,
    signed_in_household,
    state_of,
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


def client_for(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN)


def signed_in(client: TestClient, passphrase: str) -> None:
    came_in = client.post("/sign-in", data={"passphrase": passphrase})
    assert came_in.status_code == 303, came_in.text


def store_of(client: TestClient) -> ProjectStateStore:
    return state_of(client).project_state


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
    assert saved.headers["location"] == f"{PAGE}?saved=1"
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
    not_saved = re.search(r'<p class="problem" id="not-saved">(.*?)</p>', refused.text, re.S)
    assert not_saved is not None
    assert [unescape(text) for text in re.findall(r"<q[^>]*>(.*?)</q>", not_saved.group(1))] == [B]
    assert whole_form(refused.text, ACTION)["revision"] == "4"
    assert not any(on for _, on, _ in boxes(refused.text))
    assert after_refused == before
    assert stood.status_code == 303
    assert stood.headers["location"] == f"{PAGE}?saved=0"
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

    assert first.headers["location"] == f"{PAGE}?saved=1"
    assert again.status_code == 303
    assert again.headers["location"] == f"{PAGE}?saved=0"
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
