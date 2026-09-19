"""One assignment's details: its current record by id, her update through the forms her
cards use, and a way back that is data and never an address handed in.

The fixture week through the app, a pinned clock, and forms read from the
page's own HTML. Nothing here needs a script in the browser, and no model is
asked.
"""

import pathlib
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.noticing import week_from
from blossom.plan_reading import anchor_for
from blossom.reconciliation import SourceChannel
from blossom.routes import student as student_routes
from blossom.routes.student import (
    BAD_FORM,
    BAD_RETURN,
    CANNOT_UNDO,
    CHOOSE_ONE,
    GONE,
    NO_PLAN_NOW,
    NOT_HERS_TO_UPDATE,
    NOT_SAVED,
    NOT_THIS_CARDS,
    NOTE_TOO_LONG,
    SAVED_ELSEWHERE,
    UPDATE_ALREADY_SAVED,
    UPDATE_SAVED,
    UPDATE_UNDONE,
)
from tests.support import (
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    browser,
    hidden,
    report,
    school_said,
    signed_in_household,
    state_of,
    walkthrough,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
SYLLABUS_ID = "assignment-signed-syllabus"
QUIZ_ID = "assignment-vocabulary-quiz"


def form_fields(html: str, action: str) -> dict[str, str]:
    """The hidden fields of the form with this action, as the page wrote them."""
    start = html.index(f'action="{action}"')
    form = html[start : html.index("</form>", start)]
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))


def save(client: TestClient, page: str, status: str | None, note: str = "", **over: str) -> Answer:
    """Send the report form of a details page as the page wrote it, with her choice."""
    fields = form_fields(page, f"{DETAILS_ACTIONS}/report")
    chosen = {} if status is None else {"status": status}
    return client.post(
        f"{DETAILS_ACTIONS}/report",
        data={**fields, **chosen, "note": note, **over},
        headers=PAGE_HEADERS,
    )


DETAILS_ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"


# ------------------------------------------------------------- the page


def test_details_show_the_current_record_with_its_facts_her_update_and_the_way_back() -> None:
    """The way back first, one heading, the sentence that says this is the current record,
    the date with its year and where it came from, what each school channel says, her
    update with its day and year, the parent's standing check, and the history fold."""
    with browser() as client:
        store = state_of(client).project_state
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)]
        )
        report(client, ESSAY_ID, "done", "Handed in Tuesday.")
        family = client.get("/parent", headers=PAGE_HEADERS).text
        checked = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={"basis": hidden(family, "basis"), "expected_check_id": "", "note": "Seen."},
        )
        assert checked.status_code == 303
        page = client.get(f"{DETAILS}?return_to=week&week={FIXTURE_WEEK}", headers=PAGE_HEADERS)
        checks = store.family_checks(ESSAY_ID)

    text = page.text
    assert page.status_code == 200
    assert text.count("<h1>") == 1
    assert f"<h1>{ESSAY_TITLE}</h1>" in text
    assert text.index('<p class="return">') < text.index("<h1>")
    assert (
        f'<a href="/student/due-this-week?week={FIXTURE_WEEK}&amp;show={ESSAY_ID}'
        f'#assignment-{ESSAY_ID}">Back to the week</a>'
    ) in text
    assert "Current assignment record. Updates shown are the latest on record." in text
    assert "Friday, August 21, 2026" in text
    assert "<strong>The school reports this missing.</strong>" in text
    assert '<h2 class="update-heading">Your update</h2>' in text
    assert '<span class="pill">Your update: Done</span>' in text
    assert "Reported August 19, 2026" in text
    assert "You wrote: <q>Handed in Tuesday.</q>" in text
    assert "A parent marked this checked with you on August 19, 2026." in text
    assert "The note with it: <q>Seen.</q>" in text
    assert "<summary>Update history<span" in text
    assert "/parent/actions/checks/" not in text
    assert "Mark checked" not in text
    assert len(checks) == 1


def test_the_update_comes_before_the_long_evidence_and_every_fact_is_there_once() -> None:
    """Sources that disagree, a school Missing, a long instruction from the teacher, and a
    history of updates: the title, the date with its warning, and then her update come
    first; the sources' claims, the school's word, and the note follow, each once; the
    history closes the page. The warning points at the claims below it. On an ordinary
    arrival at an assignment with no update the form is there with nothing chosen, the
    note folded, and no field asking for the cursor."""
    instruction = "Bring the annotated map and cite two of the readings. " * 40
    with browser() as client:
        store = state_of(client).project_state
        store.record_status_reports(
            ESSAY_ID, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)]
        )
        item = store.one_assignment(ESSAY_ID)
        assert item is not None
        store.put_on_record([item.model_copy(update={"note": instruction.strip()})], {})
        report(client, ESSAY_ID, "not_yet", "Half of it.")
        report(client, ESSAY_ID, "done")
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        untouched = client.get(f"/student/assignments/{QUIZ_ID}", headers=PAGE_HEADERS).text

    in_order = [
        "<h1>",
        "Current assignment record.",
        "<strong>Check this date.</strong>",
        '<h2 class="update-heading">Your update</h2>',
        '<span class="pill">Your update: Done</span>',
        ">Change</button>",
        '<section class="evidence" id="evidence" tabindex="-1">',
        "What the sources say</h3>",
        "<strong>The school reports this missing.</strong>",
        "Bring the annotated map",
        "<summary>Update history<span",
    ]
    places = [page.index(piece) for piece in in_order]
    assert places == sorted(places)
    for once in (
        "<strong>Check this date.</strong>",
        "What the sources say</h3>",
        "<strong>The school reports this missing.</strong>",
        instruction.strip(),
        "<summary>Update history<span",
        '<p class="due">',
    ):
        assert page.count(once) == 1, once
    assert '<a href="#evidence">What the sources say is below.</a>' in page
    assert "Half of it." in page[page.index("<summary>Update history<span") :]
    assert "<legend>Your update<span" in untouched
    assert "autofocus" not in untouched
    assert "checked" not in untouched[untouched.index("<legend>") : untouched.index("</fieldset>")]
    assert '<details class="steps note-fold">' in untouched
    assert untouched.index("<legend>Your update<span") < untouched.index('id="evidence"')


@pytest.mark.parametrize(
    "assignment_id",
    [ESSAY_ID, SYLLABUS_ID, QUIZ_ID, "assignment-reading-log", "assignment-science-fair-proposal"],
)
def test_details_open_for_any_assignment_on_record_whatever_week_it_is_in(
    assignment_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work due this week, undated work, work due next week, and work she has reported done
    all open by id, and her week is never read to find them."""
    with browser() as client:
        report(client, ESSAY_ID, "done")
        read_weeks: list[object] = []

        def counted(*given: object, **named: object) -> object:
            read_weeks.append(given)
            return week_from(*given, **named)  # type: ignore[arg-type]

        monkeypatch.setattr(student_routes, "week_from", counted)
        page = client.get(f"/student/assignments/{assignment_id}", headers=PAGE_HEADERS)
        monkeypatch.undo()
        item = state_of(client).project_state.one_assignment(assignment_id)

    assert item is not None
    assert page.status_code == 200
    assert f"<h1>{escape(item.title)}</h1>" in page.text
    assert read_weeks == []
    if item.due_date is None:
        assert "Due date not recorded" in page.text
    else:
        assert f"{item.due_date:%B} {item.due_date.day}, {item.due_date.year}" in page.text


def test_a_change_to_the_record_shows_on_the_details_and_not_on_the_saved_plan() -> None:
    """The essay is renamed and given another date after the plan was made: its details say
    the new title and date, the saved plan still names it as the run read it, and the link
    from the plan still opens it. Taken off the record, the plan keeps its saved label,
    says the current record is unavailable, and offers no link; the address answers 404."""
    with browser(key=True) as client:
        walkthrough(client)
        assert client.post("/student/actions/plan").status_code == 303
        store = state_of(client).project_state
        item = store.one_assignment(ESSAY_ID)
        assert item is not None
        store.put_on_record(
            [item.model_copy(update={"title": "Canal essay, second draft"})],
            {},
        )
        renamed = client.get(DETAILS, headers=PAGE_HEADERS).text
        plan = client.get(f"{HER_PAGE}?show_plan=1", headers=PAGE_HEADERS).text
        store._connection.execute("DELETE FROM assignments WHERE assignment_id=?", (ESSAY_ID,))
        store._connection.commit()
        gone_plan = client.get(f"{HER_PAGE}?show_plan=1", headers=PAGE_HEADERS).text
        gone = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS)

    assert "<h1>Canal essay, second draft</h1>" in renamed
    saved_row = plan[plan.index('class="plan-rows"') :]
    assert f">{ESSAY_TITLE}</a>" in saved_row
    assert "second draft" not in saved_row[: saved_row.index("</ol>")]
    gone_rows = gone_plan[gone_plan.index('class="plan-rows"') :]
    assert "Current assignment record unavailable." in gone_rows
    assert f"/student/assignments/{ESSAY_ID}?" not in gone_rows[: gone_rows.index("</ol>")]
    assert ESSAY_TITLE in gone_rows
    assert gone.status_code == 404
    assert GONE in gone.text
    assert "<form" not in gone.text.split('<main id="main">', 1)[1]
    assert "Back to today" in gone.text


def test_an_unknown_or_malformed_id_is_a_small_404_and_shows_no_other_record() -> None:
    with browser() as client:
        answers = [
            client.get(f"/student/assignments/{name}", headers=PAGE_HEADERS)
            for name in ("assignment-nowhere", "%27%20OR%201=1--", "a" * 400, "..%2F..%2Fparent")
        ]

    assert [answer.status_code for answer in answers] == [404, 404, 404, 404]
    assert all(GONE in answer.text for answer in answers[:3])
    assert "parent" not in answers[3].text.lower()
    assert all(ESSAY_TITLE not in answer.text for answer in answers)
    assert all('href="/student/due-this-week' in answer.text for answer in answers[:3])


# ------------------------------------------------------------- the forms, from the page's own HTML


def test_she_saves_changes_and_undoes_on_the_details_and_comes_back_to_them() -> None:
    """From the details of a plan's assignment: save Done and come back to the details with
    the word of it and the way back to today's plan still there; the same update again is
    already saved; Change opens the form there; Keep it as it is closes it with no write;
    Undo restores what stood and never leaves the details."""
    with browser(key=True) as client:
        walkthrough(client)
        assert client.post("/student/actions/plan").status_code == 303
        record = state_of(client).drafts.latest_for(PLAN_DATE)
        assert record is not None
        first = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text
        saved = save(client, first, "done", "Both parts.\r\nOn paper.")
        after = client.get(saved.headers["location"], headers=PAGE_HEADERS).text
        change = form_fields(after, DETAILS)
        editor = client.get(DETAILS, params=change, headers=PAGE_HEADERS).text
        same = save(client, editor, "done", "Both parts.\nOn paper.")
        cancel = re.search(r'<a class="cancel" href="([^"]+)"', editor)
        assert cancel is not None
        kept = client.get(cancel.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS).text
        undo_fields = form_fields(after, f"{DETAILS_ACTIONS}/undo-report")
        undone = client.post(f"{DETAILS_ACTIONS}/undo-report", data=undo_fields)
        finally_ = client.get(undone.headers["location"], headers=PAGE_HEADERS).text
        events = state_of(client).project_state.student_reports(ESSAY_ID)

    assert form_fields(first, f"{DETAILS_ACTIONS}/report") == {
        "expected_report_id": "",
        "report_view": "detail",
        "return_to": "today",
        "week": "",
        "plan_id": "",
    }
    assert saved.status_code == 303
    assert saved.headers["location"] == (
        f"{DETAILS}?said=saved&return_to=today#update-result-{ESSAY_ID}"
    )
    assert (
        f'<p class="note update-result" role="status" id="update-result-{ESSAY_ID}" '
        f'tabindex="-1">{UPDATE_SAVED} <a href="/student/due-this-week?show_plan=1'
        '#todays-plan">Back to today&#39;s plan</a></p>'
    ) in after
    assert after.count("Back to today&#39;s plan</a>") == 2
    assert anchor_for(record.draft_id) not in after
    assert "Back to today&#39;s plan" in after
    assert change == {"return_to": "today", "change": "1"}
    assert "<legend>Your update<span" in editor
    assert 'value="done" checked' in editor
    assert same.status_code == 303
    assert same.headers["location"] == (
        f"{DETAILS}?said=same&return_to=today#update-result-{ESSAY_ID}"
    )
    assert "<legend>Your update<span" not in kept
    assert undone.status_code == 303
    assert undone.headers["location"] == (
        f"{DETAILS}?said=undone&return_to=today#update-result-{ESSAY_ID}"
    )
    assert UPDATE_UNDONE in finally_
    assert finally_.count("Back to today&#39;s plan</a>") == 2
    assert UPDATE_ALREADY_SAVED not in finally_
    assert [(item.operation, item.status) for item in events] == [
        ("report", "done"),
        ("undo", None),
    ]
    assert events[0].note == "Both parts.\nOn paper."


def test_a_refused_save_is_shown_on_the_details_with_what_she_typed() -> None:
    """No choice, a note of 501 code points, a field twice, a field of another form's, an
    uploaded file, a way back these pages do not make, and an update another tab saved first
    are each answered on the details, with her words kept and a link from the top to the
    update; a note of 500 code points with a character outside the basic plane saves; and
    the form that comes back carries the head as it stands, so the next save lands."""
    long_enough = "\U0001f33c" * 500
    with browser() as client:
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        no_choice = save(client, page, None, "kept <words>")
        too_long = save(client, page, "done", "x" * 501)
        fields = form_fields(page, f"{DETAILS_ACTIONS}/report")
        twice = client.post(
            f"{DETAILS_ACTIONS}/report",
            data={**fields, "status": "done", "note": ["one", "two"]},
        )
        stranger = save(client, page, "done", "kept words", channel="LMS")
        uploaded = client.post(
            f"{DETAILS_ACTIONS}/report",
            data={**fields, "status": "done"},
            files={"note": ("note.txt", b"words from a file", "text/plain")},
        )
        astray = save(client, page, "done", "kept words", return_to="https://example.org/")
        other_tab = save(client, page, "not_yet", "From the other tab.")
        behind = save(client, page, "done", "From this tab.")
        again = save(client, behind.text, "done", "From this tab.")
        editor = client.get(f"{DETAILS}?return_to=week&change=1", headers=PAGE_HEADERS).text
        unicode_note = save(client, editor, "done", long_enough)
        events = state_of(client).project_state.student_reports(ESSAY_ID)

    assert no_choice.status_code == 422
    assert CHOOSE_ONE in no_choice.text
    assert f'<a href="#update-problem-{ESSAY_ID}">Go to the update.</a>' in no_choice.text
    assert "kept &lt;words&gt;</textarea>" in no_choice.text
    assert f"<h1>{ESSAY_TITLE}</h1>" in no_choice.text
    assert too_long.status_code == 422
    assert NOTE_TOO_LONG in too_long.text
    assert 'aria-invalid="true" autofocus>' + "x" * 501 in too_long.text
    assert (twice.status_code, stranger.status_code, uploaded.status_code) == (422, 422, 422)
    assert all(BAD_FORM in answer.text for answer in (twice, stranger, uploaded))
    assert "words from a file" not in uploaded.text
    assert astray.status_code == 422
    assert BAD_RETURN in astray.text
    assert "example.org" not in astray.text
    assert "kept words</textarea>" in astray.text
    assert other_tab.status_code == 303
    assert behind.status_code == 409
    assert SAVED_ELSEWHERE in behind.text
    assert "From this tab.</textarea>" in behind.text
    assert "From the other tab." in behind.text
    assert again.status_code == 303
    assert unicode_note.status_code == 303
    assert [item.note for item in events] == ["From the other tab.", "From this tab.", long_enough]


def test_a_stale_undo_a_crossed_update_and_an_assignment_taken_off_are_said_on_the_details() -> (
    None
):
    """An Undo from a page the chain moved past is 409 on the details; an update of another
    assignment named by the form is refused; and when the assignment is taken off the record
    between the page and the save, the answer is a small 404 with her choice and her words
    to copy, a safe way back, and nothing put back on record."""
    with browser() as client:
        report(client, ESSAY_ID, "done")
        report(client, QUIZ_ID, "done")
        store = state_of(client).project_state
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        editor = client.get(f"{DETAILS}?return_to=week&change=1", headers=PAGE_HEADERS).text
        undo_fields = form_fields(page, f"{DETAILS_ACTIONS}/undo-report")
        report(client, ESSAY_ID, "not_yet")
        stale_undo = client.post(f"{DETAILS_ACTIONS}/undo-report", data=undo_fields)
        theirs = store.student_reports(QUIZ_ID)[-1].report_id
        crossed = save(client, editor, "done", "kept words", expected_report_id=theirs)
        fresh = client.get(f"{DETAILS}?return_to=week&change=1", headers=PAGE_HEADERS).text
        store._connection.execute("DELETE FROM assignments WHERE assignment_id=?", (ESSAY_ID,))
        store._connection.commit()
        removed = save(client, fresh, "done", "Typed before it went.")
        still_gone = store.one_assignment(ESSAY_ID)

    assert stale_undo.status_code == 409
    assert CANNOT_UNDO in stale_undo.text
    assert f"<h1>{ESSAY_TITLE}</h1>" in stale_undo.text
    assert crossed.status_code == 422
    assert NOT_THIS_CARDS in crossed.text
    assert "kept words</textarea>" in crossed.text
    assert removed.status_code == 404
    assert GONE in removed.text
    assert "Your choice: Done. It was not saved." in removed.text
    assert "readonly>Typed before it went.</textarea>" in removed.text
    assert 'href="/student/due-this-week' in removed.text
    assert still_gone is None


def test_a_save_the_file_refuses_keeps_her_words_on_the_details_and_on_a_plain_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()
        refused = save(client, page, "not_yet", "kept <words>")

        def unreadable(*_: object, **__: object) -> None:
            msg = "the record could not be read"
            raise RuntimeError(msg)

        monkeypatch.setattr(student_routes, "detail_page", unreadable)
        unread = save(client, page, "not_yet", "kept <words>")
        monkeypatch.undo()
        nothing = store.student_reports(ESSAY_ID)

    assert refused.status_code == 500
    assert NOT_SAVED in refused.text
    assert f"<h1>{ESSAY_TITLE}</h1>" in refused.text
    assert "kept &lt;words&gt;</textarea>" in refused.text
    assert UPDATE_SAVED not in refused.text
    assert unread.status_code == 500
    assert NOT_SAVED in unread.text
    assert "readonly>kept &lt;words&gt;</textarea>" in unread.text
    assert nothing == []


def test_her_week_cards_still_save_as_they_did_and_offer_the_details() -> None:
    with browser() as client:
        week = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        where = report(client, ESSAY_ID, "done")
        after = client.get(where, headers=PAGE_HEADERS).text

    assert (
        f'<a class="assignment-link" href="/student/assignments/{ESSAY_ID}'
        f'?return_to=week&amp;week={FIXTURE_WEEK}" '
        f'aria-label="Details: {ESSAY_TITLE}, World History">Details</a>'
    ) in week
    assert week.count(">Details</a>") == after.count(">Details</a>") == 7
    assert where == f"{HER_PAGE}?week={FIXTURE_WEEK}&saved={ESSAY_ID}#assignment-{ESSAY_ID}"
    assert form_fields(week, f"{DETAILS_ACTIONS}/report") == {
        "expected_report_id": "",
        "week": FIXTURE_WEEK,
    }


# ------------------------------------------------------------- the gate, and the way back


def test_a_parent_reads_the_details_and_cannot_save_and_her_device_is_not_sent_to_the_family_page(
    tmp_path: pathlib.Path,
) -> None:
    """A device with no sign-in is sent to sign in. A parent's device reads the details with
    the way back to the family page, sees no form, and is answered 403 on a save made up by
    hand, on the details it named. Her device asking for a way back to the family page gets
    her week instead; an altered cookie is no sign-in at all."""
    settings = signed_in_household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        anonymous = client.get(DETAILS, headers=PAGE_HEADERS)
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get(f"{DETAILS}?return_to=family", headers=PAGE_HEADERS).text
        by_default = client.get(DETAILS, headers=PAGE_HEADERS).text
        forged = client.post(
            f"{DETAILS_ACTIONS}/report",
            data={
                "status": "done",
                "note": "",
                "expected_report_id": "",
                "report_view": "detail",
                "return_to": "family",
                "week": "",
                "plan_id": "",
            },
            headers=PAGE_HEADERS,
        )
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS})
        hers = client.get(f"{DETAILS}?return_to=family", headers=PAGE_HEADERS).text
        cookie = next(iter(client.cookies.jar))
        client.cookies.set(cookie.name, (cookie.value or "") + "x")
        altered = client.get(DETAILS, headers=PAGE_HEADERS)
        events = state_of(client).project_state.student_reports(ESSAY_ID)

    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/sign-in")
    assert '<h2 class="update-heading">Student update</h2>' in theirs
    assert "Sign in as the student to update." in theirs
    assert "<legend>" not in theirs
    assert f'href="/parent?focus={ESSAY_ID}#update-{ESSAY_ID}">Back to family review</a>' in theirs
    assert "Back to family review" in by_default
    assert forged.status_code == 403
    assert NOT_HERS_TO_UPDATE in forged.text
    assert f"<h1>{ESSAY_TITLE}</h1>" in forged.text
    assert "Back to the week" in hers
    assert "/parent" not in hers.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    assert altered.status_code == 303
    assert events == []


def test_a_link_with_no_rule_of_its_own_takes_the_action_color() -> None:
    """The way back beside a saved update, the link from a date warning to the evidence,
    and the link from a problem to the update are plain links inside sentences, in the
    page's main part. The stylesheet gives every such link the application's action
    color, so none falls back on the browser's default blue, and the links that have a
    rule of their own keep it."""
    with browser() as client:
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        saved = save(client, page, "done")
        after = client.get(saved.headers["location"], headers=PAGE_HEADERS).text
        editor = client.get(f"{DETAILS}?return_to=week&change=1", headers=PAGE_HEADERS).text
        refused = save(client, editor, None)
        css = client.get("/static/blossom.css").text

    inside = after.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    assert '<a href="#evidence">What the sources say is below.</a>' in inside
    assert re.search(r'id="update-result-[^"]+" tabindex="-1">[^<]+<a href="[^"]+">Back to', inside)
    assert f'<a href="#update-problem-{ESSAY_ID}">Go to the update.</a>' in refused.text
    assert "\nmain a {\n  color: var(--blue-action);\n}\n" in css
    assert "a.assignment-link,\na.assignment-link:visited {\n  color: var(--blue-action);" in css


def test_the_way_back_to_a_row_lands_on_the_family_page_when_no_update_is_listed() -> None:
    """Nothing is reported and the school has said nothing, so the family page lists no
    assignment updates. The way back from an assignment's details still names that
    assignment's row, and the page still has a place with that id to land on, once, in the
    section, which says that nothing is listed. An ordinary visit shows no such section."""
    with browser() as client:
        details = client.get(f"{DETAILS}?return_to=family", headers=PAGE_HEADERS).text
        link = re.search(r'<p class="return"><a href="([^"]+)"', details)
        assert link is not None
        href = link.group(1).replace("&amp;", "&")
        landed = client.get(href, headers=PAGE_HEADERS)
        ordinary = client.get("/parent", headers=PAGE_HEADERS).text

    assert href == f"/parent?focus={ESSAY_ID}#update-{ESSAY_ID}"
    assert landed.status_code == 200
    assert landed.text.count(f'id="update-{ESSAY_ID}"') == 1
    section = landed.text[landed.text.index('id="assignment-updates"') :]
    section = section[: section.index("</section>")]
    assert f'<span id="update-{ESSAY_ID}" tabindex="-1"></span>' in section
    assert "No assignment updates to show." in section
    assert "<h3>" not in section
    assert 'id="assignment-updates"' not in ordinary
    assert "No assignment updates to show." not in ordinary


@pytest.mark.parametrize("plan_id", ["", "draft:plan:2026-08-19:abc12345"])
@pytest.mark.parametrize("action", ["report", "undo-report"])
def test_her_device_cannot_send_a_details_form_that_names_the_family_page(
    tmp_path: pathlib.Path, action: str, plan_id: str
) -> None:
    """The family page is not hers to go back to, so no page of hers writes a form that
    names it. A link that does falls back to her week without a word; a save or an Undo
    that does is refused, 422, on the details with her words kept, and nothing is
    written."""
    settings = signed_in_household(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        page = client.get(f"{DETAILS}?return_to=family", headers=PAGE_HEADERS)
        written = form_fields(page.text, f"{DETAILS_ACTIONS}/report")
        if action == "undo-report":
            assert save(client, page.text, "not_yet", "Mine to undo.").status_code == 303
            standing = client.get(DETAILS, headers=PAGE_HEADERS).text
            fields = form_fields(standing, f"{DETAILS_ACTIONS}/undo-report")
        else:
            fields = {**written, "status": "done", "note": "kept words"}
        before = state_of(client).project_state.student_reports(ESSAY_ID)
        forged = client.post(
            f"{DETAILS_ACTIONS}/{action}",
            data={**fields, "return_to": "family", "week": "", "plan_id": plan_id},
            headers=PAGE_HEADERS,
        )
        after = state_of(client).project_state.student_reports(ESSAY_ID)

    assert page.status_code == 200
    assert "Back to the week" in page.text
    assert written["return_to"] == "week"
    assert forged.status_code == 422
    assert BAD_RETURN in forged.text
    assert f"<h1>{ESSAY_TITLE}</h1>" in forged.text
    assert "/parent" not in forged.text.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    if action == "report":
        assert "kept words</textarea>" in forged.text
    assert after == before
    assert len(after) == (1 if action == "undo-report" else 0)


def test_a_form_from_the_details_is_held_to_this_origin_like_any_other() -> None:
    """The details' forms go through the routes her cards use, so a save or an undo sent from
    another origin, or from none, is refused before anything is read, whatever it says
    about where it came from."""
    with browser() as client:
        page = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text
        fields = form_fields(page, f"{DETAILS_ACTIONS}/report")
        elsewhere = client.post(
            f"{DETAILS_ACTIONS}/report",
            data={**fields, "status": "done", "note": ""},
            headers={"Origin": "https://example.org"},
        )
        undo_elsewhere = client.post(
            f"{DETAILS_ACTIONS}/undo-report",
            data={"report_id": "report-x", "report_view": "detail", "week": ""},
            headers={"Origin": "null"},
        )
        looked = client.get(f"{DETAILS}?change=1&said=saved", headers=PAGE_HEADERS)
        events = state_of(client).project_state.student_reports(ESSAY_ID)

    assert (elsewhere.status_code, undo_elsewhere.status_code) == (403, 403)
    assert looked.status_code == 200
    assert events == []


@pytest.mark.parametrize(
    "query",
    [
        "return_to=https://example.org/",
        "return_to=//example.org",
        "return_to=week&week=not-a-day",
        "return_to=week&week=9999-12-31",
        "return_to=today&plan_id=draft:x",
        "return_to=family&plan_id=" + "x" * 300,
        "next=https://example.org/&return_to=elsewhere",
    ],
)
def test_a_way_back_these_pages_do_not_make_is_the_safe_default(query: str) -> None:
    with browser() as client:
        page = client.get(f"{DETAILS}?{query}", headers=PAGE_HEADERS)

    assert page.status_code == 200
    assert "example.org" not in page.text
    assert (
        f'<a href="/student/due-this-week?show={ESSAY_ID}#assignment-{ESSAY_ID}">'
        "Back to the week</a>"
    ) in page.text


def test_every_way_back_lands_where_it_says_and_opens_what_encloses_it() -> None:
    """Her week with the Done fold open around the card. Today's plan unfolded, which is the
    latest plan and not the one that was followed when a newer one took its place, and Today
    with a word when none is left. The family page at the row with its fold open, at the
    section when the row is in no group, and at the plan that was read, its folds open, or
    just the family page when that plan is not on the pages."""
    with browser(key=True) as client:
        namesake = walkthrough(client)
        none_yet = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text
        assert client.post("/student/actions/plan").status_code == 303
        drafts = state_of(client).drafts
        first = drafts.latest_for(PLAN_DATE)
        assert first is not None
        assert client.post("/student/actions/plan").status_code == 303
        second = drafts.latest_for(PLAN_DATE)
        assert second is not None
        report(client, ESSAY_ID, "done")
        to_today = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text
        to_week = client.get(
            f"{DETAILS}?return_to=week&week={FIXTURE_WEEK}", headers=PAGE_HEADERS
        ).text
        week_link = re.search(r'<p class="return"><a href="([^"]+)"', to_week)
        assert week_link is not None
        week = client.get(week_link.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS).text
        to_old = client.get(
            f"{DETAILS}?return_to=family&plan_id={first.draft_id}", headers=PAGE_HEADERS
        ).text
        old_link = re.search(r'<p class="return"><a href="([^"]+)"', to_old)
        assert old_link is not None
        family_at_plan = client.get(
            old_link.group(1).replace("&amp;", "&"), headers=PAGE_HEADERS
        ).text
        to_nowhere = client.get(
            f"{DETAILS}?return_to=family&plan_id=draft:plan:none", headers=PAGE_HEADERS
        ).text
        family_at_row = client.get(f"/parent?focus={ESSAY_ID}", headers=PAGE_HEADERS).text
        family_no_row = client.get(f"/parent?focus={namesake}", headers=PAGE_HEADERS).text

    assert NO_PLAN_NOW in none_yet
    assert 'href="/student/due-this-week#today">Back to Today</a>' in none_yet
    assert "show_plan=1#todays-plan" in to_today
    assert anchor_for(first.draft_id) not in to_today
    assert '<details class="steps reported-done" open>' in week
    assert (
        f"plan={escape(first.draft_id).replace(':', '%3A')}#{anchor_for(first.draft_id)}" in to_old
    )
    assert re.search(
        r'<details class="steps panel-fold" open>\s*<summary>Earlier plans</summary>',
        family_at_plan,
    )
    assert re.search(r"<details open>\s*<summary>The plan as it was", family_at_plan)
    assert '<p class="return"><a href="/parent">Back to family review</a>' in to_nowhere
    assert re.search(r'<details class="steps" open>\s*<summary>Recent updates', family_at_row)
    assert f'<span id="update-{namesake}" tabindex="-1"></span>' in family_no_row
    assert family_no_row.count(f'id="update-{namesake}"') == 1
    assert "Marked checked" not in family_no_row
