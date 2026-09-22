"""The way in for assignments: the school's text pasted, or one typed by hand, reviewed first.

Two forms on the family page post here. The paste box takes the portal's
homework page, its weekly summary, or the school's "Missing" email as text;
the entry form takes one assignment typed by a parent. Either is read into
readings and shown on a review page, week by week, each reading a card
saying what saving it would do, before anything is written: from that page
a parent saves, goes back to edit with the draft kept, or cancels. The
saving reads the same text again and compares it with the record as it is
at that moment, so what is saved is what the page said, or, when the record
has changed enough to leave a question open, the page again rather than a
silent difference. A form that fails to validate comes back with every
field as it was, the failing field named, and the section open.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.dependencies import ApplicationState, get_application_state
from blossom.intake import (
    CLAIMED,
    FOLDED,
    KNOWN,
    NEW,
    NEW_WORK,
    NOTE_MAX_LENGTH,
    REVIEW,
    TEXT_MAX_LENGTH,
    UPDATE,
    Held,
    Kept,
    Read,
    by_hand,
    by_week,
    changes_for,
    conflicting_choices,
    held_rows,
    keep,
    read_text,
    spoken_day,
    spoken_report,
    within_a_school_year,
)
from blossom.routes.parent import review_page
from blossom.stores.project_state import AssignmentKind, UnreadableClaim
from blossom.templating import page_templates

router = APIRouter(prefix="/parent/inbox", tags=["parent"])
templates = page_templates()
State = Annotated[ApplicationState, Depends(get_application_state)]

COURSE_MAX_LENGTH: Final = 60
TITLE_MAX_LENGTH: Final = 200
ENTRY_FIELDS: Final = ("course", "title", "assigned_on", "due_date", "kind", "note")
"""What the entry form carries, in its order; the review page carries the same names."""
KIND_CHOICES: Final = (
    (AssignmentKind.HOMEWORK.value, "Homework"),
    (AssignmentKind.TASK.value, "Task"),
)

NOTHING_PASTED: Final = (
    "Nothing was pasted. Copy the homework page, the weekly summary, or the Missing "
    "email as text, and paste it whole."
)
TOO_LONG: Final = (
    f"That is more than {TEXT_MAX_LENGTH:,} characters. Paste one page or one week at a time."
)
NEEDS_COURSE: Final = "Enter the course, as the portal names it."
NEEDS_TITLE: Final = "Enter the title of the assignment."
LONG_COURSE: Final = f"A course is at most {COURSE_MAX_LENGTH} characters."
LONG_TITLE: Final = f"A title is at most {TITLE_MAX_LENGTH} characters."
LONG_NOTE: Final = f"A note is at most {NOTE_MAX_LENGTH} characters."
NOT_A_DUE_DATE: Final = "Check the due date. Enter it as year, month, and day."
NOT_AN_ASSIGNED_DATE: Final = "Check the assigned date. Enter it as year, month, and day."
FAR_DUE_DATE: Final = "Check the due-date year. Enter a date within one year of today."
FAR_ASSIGNED_DATE: Final = "Check the assigned-date year. Enter a date within one year of today."
NOT_A_KIND: Final = "Choose Homework or Task."
LOOK_AGAIN: Final = (
    "The saved assignments changed since this preview, and one of these needs your answer "
    "now. Look it over again before saving."
)
NEEDS_ANSWER: Final = (
    "A question above still needs your answer. Answer it, then save; what you chose on the "
    "other cards is kept."
)
HELD_BY_A_NOTE: Final = (
    "Nothing was saved. Some of this is about homework she added from a note, and Blossom "
    "cannot yet tell the school's version from hers. The rows are named below. Take them "
    "out to save the rest, or leave this for now; the text is kept."
)
CLAIM_UNREADABLE: Final = (
    "A saved claim about a date cannot be read right now, so nothing was saved. Your input "
    "is still here."
)
ANSWER_KEY_MAX_LENGTH: Final = 6
"""A card's key is a count from the reader, in plain digits; nothing longer is one."""
CHOOSE_ONE_TYPE: Final = (
    "Two cards about the same assignment choose different types. Pick one type for it, then save."
)


@dataclass(frozen=True)
class Problem:
    """What is wrong with an entry, and which field it is about."""

    field: str
    message: str


def entry_from(fields: Mapping[str, str], *, now: datetime, today: date) -> Read | Problem:
    """A reading from the entry form's fields, or the problem with them, named by field,
    read as of ``now`` and ``today``: the moment the entry was first reviewed."""
    course = " ".join(fields.get("course", "").split())
    title = " ".join(fields.get("title", "").split())
    note = fields.get("note", "").strip()
    if not course:
        return Problem("course", NEEDS_COURSE)
    if not title:
        return Problem("title", NEEDS_TITLE)
    if len(course) > COURSE_MAX_LENGTH:
        return Problem("course", LONG_COURSE)
    if len(title) > TITLE_MAX_LENGTH:
        return Problem("title", LONG_TITLE)
    if len(note) > NOTE_MAX_LENGTH:
        return Problem("note", LONG_NOTE)
    due = _a_date(fields.get("due_date", ""), Problem("due_date", NOT_A_DUE_DATE))
    if isinstance(due, Problem):
        return due
    if due is not None and not within_a_school_year(due, today):
        return Problem("due_date", FAR_DUE_DATE)
    assigned = _a_date(fields.get("assigned_on", ""), Problem("assigned_on", NOT_AN_ASSIGNED_DATE))
    if isinstance(assigned, Problem):
        return assigned
    if assigned is not None and not within_a_school_year(assigned, today):
        return Problem("assigned_on", FAR_ASSIGNED_DATE)
    given = fields.get("kind") or ""
    kind: AssignmentKind | None = None
    if given:
        try:
            kind = AssignmentKind(given)
        except ValueError:
            return Problem("kind", NOT_A_KIND)
    reading = by_hand(course, title, due, assigned, kind, note or None, now=now)
    return Read(items=(reading,), unread=())


def _a_date(given: str, wrong: Problem) -> date | None | Problem:
    """The date a field holds: ``None`` for an empty field, ``wrong`` for one that is no date."""
    text = given.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return wrong


STAMPS: Final = ("read_on", "read_at")
"""The day and the moment a draft was first read, carried with it through the review, so
what is saved is dated as it was reviewed: a review that spans midnight keeps the day the
text was pasted, which is the day the page said."""


def draft_of(form: Mapping[str, str]) -> dict[str, str]:
    """The draft a form carries: the pasted text, or the entry's fields, as submitted, and
    the day and moment it was first read when the form carries them."""
    text = form.get("text", "")
    draft = {"text": text} if text.strip() else {name: form.get(name, "") for name in ENTRY_FIELDS}
    for name in STAMPS:
        if form.get(name):
            draft[name] = form[name]
    return draft


def stamped(state: ApplicationState, draft: Mapping[str, str]) -> dict[str, str]:
    """The draft with the day and the moment it is first read, by the household's clock."""
    return {
        **draft,
        "read_on": state.clock.today().isoformat(),
        "read_at": state.clock.now().isoformat(),
    }


def moment_of(state: ApplicationState, draft: Mapping[str, str]) -> tuple[datetime, date]:
    """When a draft is read as of: the day and moment it was first read, when the draft
    carries them and they read as a day within a school year of today; else now."""
    now, today = state.clock.now(), state.clock.today()
    try:
        read_on = date.fromisoformat(draft.get("read_on", ""))
        read_at = datetime.fromisoformat(draft.get("read_at", ""))
    except ValueError:
        return now, today
    if read_at.tzinfo is None or not within_a_school_year(read_on, today):
        return now, today
    return read_at, read_on


def answers_from(
    form: Mapping[str, str], unasked: Mapping[int, set[AssignmentKind]]
) -> tuple[dict[int, str], dict[int, AssignmentKind]]:
    """The parent's answers on the review page: new work or an update, and the type chosen.

    The page sends back, beside each select, what it suggested for the card
    before any choice and what the select showed when the page was made. A
    type is a choice when the select was changed from what it showed, a
    change back to the suggestion included, or when it carries a choice
    from a page before, which the page marks beside the select, so a choice
    once made stays one through every page that comes back until it is
    saved, whatever the values; a select left as it was showing, and never
    chosen, is no answer, so it can never undo a choice made on another
    card about the same assignment, nor a change made from elsewhere
    meanwhile. A card folded into another, which the page sends
    back unshown, carries its choice only while the card shown for the
    assignment makes none. A form sent without the page's own notes is read
    the careful way: a type in ``unasked`` for the card, what the page would
    show now or what the reader suggests, is no choice either.
    """
    occurrences: dict[int, str] = {}
    kinds: dict[int, AssignmentKind] = {}
    suggested: dict[int, AssignmentKind] = {}
    shown: dict[int, AssignmentKind] = {}
    folded: dict[int, int] = {}
    marked: set[int] = set()
    for name, value in form.items():
        head, _, number = name.rpartition("-")
        if not number.isdigit():
            continue
        if head == "occurrence" and value in (UPDATE, NEW_WORK):
            occurrences[int(number)] = value
        elif head == "folded" and value.isdigit():
            folded[int(number)] = int(value)
        elif head == "chosen":
            marked.add(int(number))
        elif head in ("kind", "suggested", "shown"):
            try:
                kind = AssignmentKind(value)
            except ValueError:
                continue
            {"kind": kinds, "suggested": suggested, "shown": shown}[head][int(number)] = kind
    chosen = {}
    for key, kind in kinds.items():
        if key in marked:
            chosen[key] = kind
        elif key in suggested:
            edited = key in shown and kind != shown[key]
            if edited or kind != suggested[key]:
                chosen[key] = kind
        elif kind not in unasked.get(key, set()):
            chosen[key] = kind
    # A card folded into another carries its choice only while the card
    # shown for the assignment makes none: a choice on the card shown, a
    # change back included, stands over what a folded card carried.
    for key, into in folded.items():
        if into in chosen:
            chosen.pop(key, None)
    return occurrences, chosen


def asked_on(form: Mapping[str, str]) -> set[int]:
    """The cards the page put a question to, which the page sends back beside them; a
    question on any other card is one the record raised since the page was made."""
    asked: set[int] = set()
    for name in form:
        head, _, number = name.rpartition("-")
        if head == "asked" and number.isdigit():
            asked.add(int(number))
    return asked


def unasked_for(state: ApplicationState, read: Read) -> dict[int, set[AssignmentKind]]:
    """For each card, the types that are no choice when a form carries no note of what the
    page showed: what the page would show for it now, and what the reader suggests."""
    return {
        change.key: {change.kind, change.reading.kind}
        for change in changes_for(read.items, state.project_state)
    }


def read_draft(state: ApplicationState, draft: Mapping[str, str]) -> Read | Problem:
    """Read what a draft holds: the pasted text, or the entry, the same way every time, as
    of the moment it was first read."""
    now, today = moment_of(state, draft)
    text = draft.get("text", "")
    if text.strip():
        if len(text) > TEXT_MAX_LENGTH:
            return Problem("text", TOO_LONG)
        return read_text(text, now=now, today=today)
    return entry_from(draft, now=now, today=today)


def problem_page(
    request: Request, state: ApplicationState, draft: Mapping[str, str], problem: Problem
) -> HTMLResponse:
    """The family page again, with the draft as it was, the failing field named, and the
    section open."""
    return review_page(
        request,
        state,
        problem=problem.message,
        problem_field=problem.field,
        paste=draft.get("text") or None,
        entry=None if draft.get("text") else dict(draft),
        entry_open=True,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


@dataclass(frozen=True)
class AnswerKept:
    """One answer given on a review card, as unsaved input to copy: which card, whether the
    row is the same assignment or new work, and the type chosen."""

    key: str
    occurrence: str | None
    kind: str | None


def answers_kept(form: Mapping[str, str]) -> list[AnswerKept]:
    """The answers a review form carried, read by the page's own rules for a choice and by
    its own field names, nothing else: new work or the same assignment where the card asked,
    and a type where the select was changed from what it showed or carries a choice from a
    page before. A card's key is plain digits. Anything else the form holds is no answer."""
    by_key: dict[str, AnswerKept] = {}
    types = {kind.value for kind in AssignmentKind}
    for name, value in form.items():
        head, _, key = name.rpartition("-")
        if head not in ("occurrence", "kind") or not key.isdigit():
            continue
        if len(key) > ANSWER_KEY_MAX_LENGTH:
            continue
        found = by_key.get(key, AnswerKept(key, None, None))
        if head == "occurrence":
            if value in ("update", "new") and form.get(f"asked-{key}") == "1":
                found = AnswerKept(key, value, found.kind)
        elif value in types:
            showed = form.get(f"shown-{key}", form.get(f"suggested-{key}"))
            if form.get(f"chosen-{key}") == "1" or value != showed:
                found = AnswerKept(key, found.occurrence, value)
        by_key[key] = found
    return [
        answer
        for _, answer in sorted(by_key.items(), key=lambda item: int(item[0]))
        if answer.occurrence or answer.kind
    ]


def answers_shown(
    occurrences: Mapping[int, str] | None, kinds: Mapping[int, AssignmentKind] | None
) -> list[AnswerKept]:
    """The answers a review page was made with, in the same shape."""
    keys = sorted({*(occurrences or {}), *(kinds or {})})
    return [
        AnswerKept(
            str(key),
            (occurrences or {}).get(key),
            None if kinds is None or key not in kinds else kinds[key].value,
        )
        for key in keys
    ]


def intake_unavailable(
    request: Request,
    state: ApplicationState,
    draft: Mapping[str, str],
    answers: list[AnswerKept],
) -> HTMLResponse:
    """The page for a review or a save refused because a claim about a date on record cannot
    be read. It reads no store, tries nothing again, and says nothing was saved. It keeps
    the text as pasted, or the entry as typed, and the answers given on the cards, to copy.
    The way on is a press through the same reading, never a retry made here."""
    text = draft.get("text") or None
    entry = None
    if text is None:
        entry = {name: draft.get(name, "") for name in ENTRY_FIELDS}
        entry["kind"] = dict(KIND_CHOICES).get(entry["kind"], entry["kind"])
    return templates.TemplateResponse(
        request,
        "inbox_unavailable.html",
        {
            "problem": CLAIM_UNREADABLE,
            "text": text,
            "entry": entry,
            "entry_fields": ENTRY_FIELDS,
            "answers": answers,
            "again": "/parent/inbox/read" if text else "/parent/inbox/enter",
            "draft": {
                name: value for name, value in draft.items() if name in (*ENTRY_FIELDS, "text")
            },
            "sample": state.settings.sample,
        },
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def preview_page(
    request: Request,
    state: ApplicationState,
    read: Read,
    draft: Mapping[str, str],
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    notice: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """What was read, week by week against the record as it is, with the way to save it.
    A text with a row about homework made from a homework note says so, names the rows, and
    offers no save: the text stays to be edited. A claim on record that cannot be read
    refuses the comparison, and the page that reads no store keeps the draft."""
    try:
        changes = changes_for(read.items, state.project_state, occurrences=occurrences, kinds=kinds)
        held = held_rows(read.items, state.project_state)
    except UnreadableClaim:
        return intake_unavailable(request, state, draft, answers_shown(occurrences, kinds))
    if held is not None:
        notice = HELD_BY_A_NOTE
    if notice is None and conflicting_choices(changes):
        notice = CHOOSE_ONE_TYPE
    shown = [change for change in changes if change.state != FOLDED]
    folded = [change for change in changes if change.state == FOLDED]
    counts = {
        "new": sum(1 for change in shown if change.state == NEW),
        "updated": sum(1 for change in shown if change.state == CLAIMED),
        "unchanged": sum(1 for change in shown if change.state == KNOWN),
        "review": sum(1 for change in shown if change.state == REVIEW),
    }
    to_save = counts["new"] + counts["updated"] + counts["review"]
    return templates.TemplateResponse(
        request,
        "inbox_preview.html",
        {
            "weeks": by_week(changes),
            "changes": shown,
            "folded": folded,
            "counts": counts,
            "to_save": to_save,
            "unread": read.unread,
            "draft": draft,
            "kind_choices": KIND_CHOICES,
            "notice": notice,
            "held": held,
            "sample": state.settings.sample,
            "spoken_report": spoken_report,
            "spoken_day": spoken_day,
        },
        status_code=status_code,
    )


async def submitted(request: Request) -> dict[str, str]:
    """The form as submitted, every value as text."""
    form = await request.form()
    return {name: str(value) for name, value in form.items() if isinstance(value, str)}


@router.post("/read", response_class=HTMLResponse, include_in_schema=False)
async def read_paste(request: Request, state: State) -> Response:
    """Read the pasted text and show what was read, without writing anything."""
    form = await submitted(request)
    if not form.get("text", "").strip():
        return problem_page(request, state, {"text": ""}, Problem("text", NOTHING_PASTED))
    draft = stamped(state, draft_of(form))
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return problem_page(request, state, draft, read)
    return preview_page(request, state, read, draft)


@router.post("/enter", response_class=HTMLResponse, include_in_schema=False)
async def read_entry(request: Request, state: State) -> Response:
    """Read one assignment typed by hand and show it, without writing anything."""
    form = await submitted(request)
    draft = stamped(state, {name: form.get(name, "") for name in ENTRY_FIELDS})
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return problem_page(request, state, draft, read)
    return preview_page(request, state, read, draft)


@router.post("/edit", response_class=HTMLResponse, include_in_schema=False)
async def edit_draft(request: Request, state: State) -> Response:
    """Back to the family page with the draft as it was, to change it; nothing is written."""
    form = await submitted(request)
    draft = draft_of(form)
    return review_page(
        request,
        state,
        paste=draft.get("text") or None,
        entry=None if draft.get("text") else draft,
        entry_open=True,
    )


@router.post("/keep", response_class=HTMLResponse, include_in_schema=False)
async def keep_readings(request: Request, state: State) -> Response:
    """Read the draft again, save what is new against the record as it is, and say what
    was added, updated, and unchanged; or show the review again if a question is open,
    saying whether the question is one the page put and the parent left, or one the record
    raised since. Whatever was chosen on the cards comes back chosen.

    The saving runs under the decision lock, as a signal does: a plan's
    approval checks the week the plan was made from against the week as it
    stands, and the week must not change between that check and the
    decision landing. A saving during a decision waits the moment it takes,
    and a saving before it leaves the decision refused as stale.
    """
    form = await submitted(request)
    draft = draft_of(form)
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return problem_page(request, state, draft, read)
    # A claim on record that cannot be read refuses the comparison, before the write or
    # inside its transaction, which is rolled back whole before anything is answered. The
    # answer reads no store and keeps the draft and the answers given on the cards.
    try:
        occurrences, kinds = answers_from(form, unasked_for(state, read))
        async with state.decision_lock:
            kept = keep(read.items, state.project_state, occurrences=occurrences, kinds=kinds)
    except UnreadableClaim:
        return intake_unavailable(request, state, draft, answers_kept(form))
    if isinstance(kept, Held):
        return preview_page(
            request,
            state,
            read,
            draft,
            occurrences=occurrences,
            kinds=kinds,
            status_code=status.HTTP_409_CONFLICT,
        )
    if not isinstance(kept, Kept):
        open_questions = {change.key for change in kept if change.state == REVIEW}
        if conflicting_choices(kept):
            notice = CHOOSE_ONE_TYPE
        elif open_questions <= asked_on(form):
            notice = NEEDS_ANSWER
        else:
            notice = LOOK_AGAIN
        return preview_page(
            request, state, read, draft, occurrences=occurrences, kinds=kinds, notice=notice
        )
    return RedirectResponse(
        f"/parent?added={kept.added}&updated={kept.updated}&unchanged={kept.unchanged}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
