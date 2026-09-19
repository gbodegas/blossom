"""What a saved plan's pages must still do when the record, the plan, or the day moves.

A snapshot whose text cannot be shown falls back one record at a time, a page
reads the record once for everything it says about a plan, an assignment's
details list every claim about its date, a reviewer's reason ends once, and
the way back to today's plan is a place that is there whichever plan is.
"""

import dataclasses
import html
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.clock import spoken_time
from blossom.dependencies import STATE_ATTRIBUTE
from blossom.plan_reading import anchor_for
from blossom.plan_snapshot import read_snapshot
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.student import ASSIGNMENTS_CHANGED, NO_PLAN_NOW
from blossom.settings import REPOSITORY_ROOT
from blossom.stores.drafts import DraftRecord
from tests.support import (
    ESSAY,
    ESSAY_ID,
    FIXTURE_TIMEZONE,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    browser,
    card_for,
    composed_plan,
    plan_on,
    planned,
    report,
    state_of,
    walkthrough,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
READING_LOG_ID = "assignment-reading-log"
ALGEBRA_ID = "assignment-algebra-set"
UNAVAILABLE = "The saved text is shown because this plan's structured view is unavailable."
SLOT = 'id="todays-plan"'
SAID_WHEN_PLANNED = (
    "the outline first, while she is fresh",
    "nobody has confirmed this date, so it gets done tonight",
    "the first two paragraphs after a break",
    "the other essay comes first",
)
FIELDS = [
    "title",
    "course",
    "intro",
    "clarification",
    "review label",
    "review text",
    "review intro",
]
LONE = ["\ud800", "\udc00"]


def spoiled(saved: dict[str, object], field: str, words: str) -> str:
    """The snapshot with ``words`` in one of the strings a page shows, written as JSON with
    every character outside ASCII as its escape, which is how a lone surrogate reaches a
    file."""
    copy = json.loads(json.dumps(saved))
    review = {
        "intro": ["what it did not consider"],
        "findings": [{"label": "Order: fails", "text": "the hard one comes late"}],
    }
    if field in ("title", "course"):
        copy["assignments"][ESSAY_ID][field] = words
    elif field == "intro":
        copy["intro"] = [words]
    elif field == "clarification":
        copy["clarifications"] = [{"assignment_id": ESSAY_ID, "text": words}]
    elif field == "review intro":
        review["intro"] = [words]
    elif field == "review label":
        review["findings"] = [{"label": words, "text": "the hard one comes late"}]
    else:
        review["findings"] = [{"label": "Order: fails", "text": words}]
    if field.startswith("review"):
        copy["review"] = review
    return json.dumps(copy)


# ------------------------------------------------------------- text a page cannot show


@pytest.mark.parametrize("lone", LONE)
@pytest.mark.parametrize("field", FIELDS)
def test_a_lone_surrogate_in_any_saved_string_makes_the_snapshot_unavailable(
    field: str, lone: str
) -> None:
    """Half of a surrogate pair, written as its JSON escape, is text no page can send. The
    reader refuses the snapshot where it is decoded, whichever string holds it."""
    made = composed_plan()
    whole = made.snapshot.model_dump(mode="json")
    assert ESSAY.assignment_id == ESSAY_ID

    reading = read_snapshot(
        "draft:lone",
        spoiled(whole, field, f"saved{lone}words"),
        plan_date=PLAN_DATE,
        plan_assignment_ids=made.snapshot.assignment_ids,
    )

    assert (reading.snapshot, reading.unavailable) == (None, True)


def test_text_that_can_be_shown_still_reads_whatever_way_it_was_written() -> None:
    """Accents, another script, and an emoji read the same written as themselves and written
    as escapes, a surrogate pair included. A block's reason holding half a pair is refused,
    as it was."""
    made = composed_plan()
    whole = made.snapshot.model_dump(mode="json")
    words = "café 中文 \U0001f600"
    ids = made.snapshot.assignment_ids

    for field in FIELDS:
        copy = json.loads(spoiled(whole, field, words))
        for saved in (json.dumps(copy), json.dumps(copy, ensure_ascii=False)):
            reading = read_snapshot(
                "draft:fine", saved, plan_date=PLAN_DATE, plan_assignment_ids=ids
            )
            assert reading.snapshot is not None, field
            assert words in reading.snapshot.model_dump_json(), field
    assert "\\ud83d\\ude00" in json.dumps(json.loads(spoiled(whole, "intro", words)))
    halved = json.loads(json.dumps(whole))
    halved["plan"]["blocks"][0]["rationale"] = "first\ud800"
    reading = read_snapshot(
        "draft:halved", json.dumps(halved), plan_date=PLAN_DATE, plan_assignment_ids=ids
    )
    assert (reading.snapshot, reading.unavailable) == (None, True)


@pytest.mark.parametrize("lone", LONE)
@pytest.mark.parametrize("field", FIELDS)
def test_a_snapshot_no_page_can_send_falls_back_to_the_saved_text_on_both_pages(
    field: str, lone: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Today's plan has half a surrogate pair in one saved string, beside an earlier plan
    that is whole. Both pages answer 200 with today's saved text, all of it, and the
    sentence that says why, no mark beside any row; the earlier plan still reads by its
    rows; nothing is written; and what is logged says nothing the plan says."""
    with browser(key=True) as client:
        walkthrough(client)
        good = planned(client)
        bad = planned(client)
        drafts = state_of(client).drafts
        assert bad.plan_snapshot is not None
        drafts._connection.execute(
            "UPDATE drafts SET plan_snapshot=? WHERE draft_id=?",
            (spoiled(json.loads(bad.plan_snapshot), field, f"saved{lone}words"), bad.draft_id),
        )
        drafts._connection.commit()
        report(client, ESSAY_ID, "done")
        before = (drafts.get(good.draft_id), drafts.get(bad.draft_id))
        with caplog.at_level(logging.WARNING, logger="blossom.plan_snapshot"):
            hers = client.get(HER_PAGE, headers=PAGE_HEADERS)
            family = client.get("/parent", headers=PAGE_HEADERS)
        after = (drafts.get(good.draft_id), drafts.get(bad.draft_id))

    assert (hers.status_code, family.status_code) == (200, 200)
    for page in (hers.text, family.text):
        text = plan_on(page, bad)
        assert UNAVAILABLE in text
        assert "set aside for" in text
        for said in SAID_WHEN_PLANNED:
            assert escape(said) in text
        assert "Reported done" not in text
        assert "skip this block" not in text
    earlier = family.text[family.text.index(f'id="{anchor_for(good.draft_id)}"') :]
    assert '<ol class="plan-rows">' in earlier[: earlier.index("</pre>")]
    assert after == before
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert bad.draft_id in logged
    assert "Canal Era" not in logged
    assert "saved" not in logged.replace("saved snapshot", "")
    for said in SAID_WHEN_PLANNED:
        assert said not in logged


def test_what_is_logged_about_a_snapshot_names_the_part_and_never_an_assignment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An assignment's id is made from its title. A fault inside one saved assignment, or
    inside one block, is logged as a fault in that part of the envelope, with no id and no
    place inside the part."""
    made = composed_plan()
    whole = made.snapshot.model_dump(mode="json")
    ids = made.snapshot.assignment_ids
    no_date = json.loads(json.dumps(whole))
    del no_date["assignments"][ESSAY_ID]["due_date"]
    stray = json.loads(json.dumps(whole))
    stray["plan"]["blocks"][1]["assignment_id"] = 7

    with caplog.at_level(logging.WARNING, logger="blossom.plan_snapshot"):
        for saved in (no_date, stray):
            reading = read_snapshot(
                "draft:named", json.dumps(saved), plan_date=PLAN_DATE, plan_assignment_ids=ids
            )
            assert (reading.snapshot, reading.unavailable) == (None, True)

    first, second = (record.getMessage() for record in caplog.records)
    assert "assignments: missing" in first
    assert "plan: " in second
    for message in (first, second):
        assert "draft:named" in message
        assert not any(name in message for name in ids)
        assert "canal" not in message.lower()
        assert "blocks" not in message
        assert "due_date" not in message


# ------------------------------------------------------------- the record, read once for a page


@pytest.mark.parametrize("path", [HER_PAGE, "/parent"])
def test_a_page_with_a_waiting_plan_reads_her_reports_once(path: str) -> None:
    """Today's plan waits for a review, so the page says what she reports done in it, marks
    its rows, and measures it against the week as it stands. All three, and the rest of the
    page, come from one reading of her reports."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        report(client, ESSAY_ID, "done")
        store = state_of(client).project_state
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        page = client.get(path, headers=PAGE_HEADERS)
        store._connection.set_trace_callback(None)

    assert record.waiting
    assert record.inputs_digest is not None
    assert page.status_code == 200
    assert plan_on(page.text, record).count("skip this block.") == 2
    stale = ASSIGNMENTS_CHANGED if path == HER_PAGE else THEIR_ASSIGNMENTS_CHANGED
    assert escape(stale) in page.text
    assert len([text for text in statements if "FROM student_reports" in text]) == 1
    assert len([text for text in statements if "FROM family_checks" in text]) == 1


# ------------------------------------------------------------- every claim about a date


def test_the_details_list_every_claim_about_the_date_and_a_card_stays_short() -> None:
    """Two channels that agree, one channel alone, and channels that disagree: the details
    list each claim as it was given, once. A card lists claims only when the date is in
    doubt, as it always has."""
    with browser() as client:
        store = state_of(client).project_state
        claims = {
            name: [record.spoken() for record in store.deadline_records(name)]
            for name in (ALGEBRA_ID, READING_LOG_ID, ESSAY_ID)
        }
        pages = {
            name: client.get(f"/student/assignments/{name}", headers=PAGE_HEADERS).text
            for name in claims
        }
        week = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    assert [len(found) for found in claims.values()] == [2, 1, 2]
    for name, found in claims.items():
        evidence = pages[name][pages[name].index('id="evidence"') :]
        assert evidence.count("What the sources say</h3>") == 1
        for claim in found:
            assert evidence.count(f"<li>{escape(claim)}</li>") == 1, claim
        assert pages[name].count("What the sources say</h3>") == 1
    for settled in (ALGEBRA_ID, READING_LOG_ID):
        assert "What the sources say" not in card_for(week, settled)
    assert card_for(week, ESSAY_ID).count("What the sources say</h3>") == 1


# ------------------------------------------------------------- a reason ends once


@pytest.mark.parametrize(
    ("reason", "shown"),
    [
        ("Start with the outline.", r"Start with the outline\."),
        ("Why so late?", r"Why so late\?"),
        ("Good!", "Good!"),
        ("Start with the outline", r"Start with the outline\."),
    ],
)
def test_a_reviewers_reason_ends_once_wherever_it_is_shown(reason: str, shown: str) -> None:
    """A reason that ends its own sentence gets no second mark, and one that does not gets a
    period: under today's reviewed plan, and among the earlier plans once another plan
    takes its place."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        decided = client.post(
            f"/parent/actions/decide/{record.draft_id}",
            data={"decision": "approve", "reason": reason},
        )
        assert decided.status_code == 303
        todays = client.get("/parent", headers=PAGE_HEADERS).text
        planned(client)
        later = client.get("/parent", headers=PAGE_HEADERS).text

    ends_once = re.compile(rf"Reason: {shown}\s*<span class=\"when\">")
    reviewed = todays[todays.index("<h2>Today's reviewed plan</h2>") :]
    earlier = later[later.index("<summary>Earlier plans</summary>") :]
    assert ends_once.search(reviewed[: reviewed.index("</article>")])
    assert ends_once.search(earlier)
    assert "Today's reviewed plan" not in later


# ------------------------------------------------------------- history that wraps on its own


def test_the_history_wraps_and_keeps_its_lines_wherever_it_sits() -> None:
    """The history is under her update on a card and below the evidence on the details, so
    the rules that wrap a long unbroken note and keep its line breaks belong to the history
    itself, and none of its spacing waits for an ancestor."""
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")

    assert ".assignment-updates,\n.update,\n.history {\n  overflow-wrap: anywhere;\n}" in css
    assert ".assignment-updates q,\n.update q,\n.history q {\n  white-space: pre-line;\n}" in css
    assert "\n.history {\n  margin-top: 0.5rem;\n}" in css
    assert "\n.history ol {" in css
    assert "\n.history li {" in css
    assert ".update .history" not in css


def test_an_old_note_is_in_the_history_as_she_wrote_it_on_a_card_and_on_the_details() -> None:
    """A long address with no break in it and two lines, replaced by a short note: the long
    one is in the history alone, whole, inside the history's own container on both pages."""
    long_note = "See the notes\nhttps://example.org/" + "a" * 300
    with browser() as client:
        report(client, ESSAY_ID, "not_yet", long_note)
        report(client, ESSAY_ID, "not_yet", "Short now.")
        card = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
        details = client.get(DETAILS, headers=PAGE_HEADERS).text

    for page in (card, details):
        history = page[page.index('<details class="steps history">') :]
        assert page.count(escape(long_note)) == 1
        assert escape(long_note) in history[: history.index("</details>")]
    assert details.index('id="evidence"') < details.index('<details class="steps history">')


# ------------------------------------------------------------- a way back that is always there


class SetClock:
    """A clock whose household day is whatever the test last set."""

    def __init__(self, day: date, at: datetime) -> None:
        self.day = day
        self.at = at
        self._zone = ZoneInfo(FIXTURE_TIMEZONE)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        return self.at

    def today(self) -> date:
        return self.day


def with_clock(client: TestClient, clock: SetClock) -> None:
    state = state_of(client)
    setattr(client.app.state, STATE_ATTRIBUTE, dataclasses.replace(state, clock=clock))  # type: ignore[attr-defined]


def return_href(page: str, label: str) -> str:
    """The literal address of the link a page offers under ``label``, as a browser reads it."""
    found = re.search(rf'<a href="([^"]+)">{re.escape(label)}</a>', page)
    assert found is not None, label
    return html.unescape(found.group(1))


def slot_of(page: str) -> str:
    """Today's plan slot on her week: from its stable id to the help form after it."""
    start = page.index(SLOT)
    return page[start : page.index('id="ask-for-help"', start)]


def made_at(record: DraftRecord) -> str:
    return f"made at {spoken_time(record.created_at.astimezone(ZoneInfo(FIXTURE_TIMEZONE)))}"


@pytest.mark.parametrize("rendered", ["the details", "a saved update"])
def test_the_way_back_to_today_lands_on_whichever_plan_is_todays_when_it_is_followed(
    rendered: str,
) -> None:
    """The details, or the word of a save with the way back beside it, were rendered under
    one plan. Another plan takes its place before the link is followed. The link as it was
    written still lands on today's plan slot, open, holding the newer plan."""
    with browser(key=True) as client:
        walkthrough(client)
        first = planned(client)
        if rendered == "the details":
            page = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text
        else:
            saved = client.post(
                f"{ACTIONS}/report",
                data={
                    "status": "not_yet",
                    "note": "Half of it.",
                    "expected_report_id": "",
                    "week": "",
                    "report_view": "detail",
                    "return_to": "today",
                    "plan_id": "",
                },
                follow_redirects=True,
                headers=PAGE_HEADERS,
            )
            assert saved.status_code == 200
            page = saved.text[saved.text.index('id="update-result-') :]
        href = return_href(page, "Back to today&#39;s plan")
        later = SetClock(PLAN_DATE, datetime(2026, 8, 19, 22, 41, tzinfo=UTC))
        with_clock(client, later)
        second = planned(client)
        landed = client.get(href, headers=PAGE_HEADERS)
        ordinary = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    assert first.draft_id != second.draft_id
    assert href == f"{HER_PAGE}?show_plan=1#todays-plan"
    assert landed.status_code == 200
    assert landed.text.count(SLOT) == 1
    slot = slot_of(landed.text)
    assert slot.startswith('id="todays-plan" tabindex="-1"')
    assert '<details class="plan" open ' in landed.text[: landed.text.index(SLOT) + len(SLOT)]
    assert f'id="{anchor_for(second.draft_id)}"' in slot
    assert anchor_for(first.draft_id) not in landed.text
    assert made_at(second) in slot
    assert slot_of(ordinary) == slot


def test_view_todays_plan_points_at_the_slot_too() -> None:
    """The notice above the plan links to the same stable place as the way back."""
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        report(client, ESSAY_ID, "done")
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text

    assert return_href(page, "View today's plan") == f"{HER_PAGE}?show_plan=1#todays-plan"
    assert page.count(SLOT) == 1


def test_a_way_back_followed_on_a_day_with_no_plan_lands_on_a_place_that_says_so() -> None:
    """The link was written while today had a plan. Followed after the household day has
    moved on, with no plan for the new day, it lands on the same slot, which says that no
    plan is saved for today. An ordinary visit to a day with no plan gains no such word."""
    with browser(key=True) as client:
        walkthrough(client)
        record = planned(client)
        page = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text
        href = return_href(page, "Back to today&#39;s plan")
        with_clock(
            client,
            SetClock(PLAN_DATE + timedelta(days=1), datetime(2026, 8, 20, 12, 0, tzinfo=UTC)),
        )
        landed = client.get(href, headers=PAGE_HEADERS)
        ordinary = client.get(HER_PAGE, headers=PAGE_HEADERS)
        known = client.get(f"{DETAILS}?return_to=today", headers=PAGE_HEADERS).text

    assert landed.status_code == 200
    assert landed.text.count(SLOT) == 1
    assert anchor_for(record.draft_id) not in landed.text
    slot = landed.text[landed.text.index(SLOT) :][:400]
    assert slot.startswith('id="todays-plan" tabindex="-1"')
    assert NO_PLAN_NOW in slot
    assert SLOT not in ordinary.text
    assert NO_PLAN_NOW not in ordinary.text
    assert return_href(known, "Back to Today") == f"{HER_PAGE}#today"
    assert NO_PLAN_NOW in known
