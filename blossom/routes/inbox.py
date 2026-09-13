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
from datetime import date
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.dependencies import ApplicationState, get_application_state
from blossom.intake import (
    CLAIMED,
    KNOWN,
    NEW,
    NEW_WORK,
    NOTE_MAX_LENGTH,
    REVIEW,
    TEXT_MAX_LENGTH,
    UPDATE,
    Kept,
    Read,
    by_hand,
    by_week,
    changes_for,
    keep,
    read_text,
    spoken_day,
    spoken_report,
    within_a_school_year,
)
from blossom.routes.parent import review_page
from blossom.stores.project_state import AssignmentKind
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


@dataclass(frozen=True)
class Problem:
    """What is wrong with an entry, and which field it is about."""

    field: str
    message: str


def entry_from(state: ApplicationState, fields: Mapping[str, str]) -> Read | Problem:
    """A reading from the entry form's fields, or the problem with them, named by field."""
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
    today = state.clock.today()
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
    try:
        kind = AssignmentKind(fields.get("kind") or AssignmentKind.HOMEWORK.value)
    except ValueError:
        return Problem("kind", NOT_A_KIND)
    reading = by_hand(course, title, due, assigned, kind, note or None, now=state.clock.now())
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


def draft_of(form: Mapping[str, str]) -> dict[str, str]:
    """The draft a form carries: the pasted text, or the entry's fields, as submitted."""
    text = form.get("text", "")
    if text.strip():
        return {"text": text}
    return {name: form.get(name, "") for name in ENTRY_FIELDS}


def answers_from(form: Mapping[str, str]) -> tuple[dict[int, str], dict[int, AssignmentKind]]:
    """The parent's answers on the review page: new work or an update, and the type chosen."""
    occurrences: dict[int, str] = {}
    kinds: dict[int, AssignmentKind] = {}
    for name, value in form.items():
        head, _, number = name.rpartition("-")
        if not number.isdigit():
            continue
        if head == "occurrence" and value in (UPDATE, NEW_WORK):
            occurrences[int(number)] = value
        elif head == "kind":
            try:
                kinds[int(number)] = AssignmentKind(value)
            except ValueError:
                continue
    return occurrences, kinds


def read_draft(state: ApplicationState, draft: Mapping[str, str]) -> Read | Problem:
    """Read what a draft holds: the pasted text, or the entry, the same way every time."""
    text = draft.get("text", "")
    if text.strip():
        if len(text) > TEXT_MAX_LENGTH:
            return Problem("text", TOO_LONG)
        return read_text(text, now=state.clock.now(), today=state.clock.today())
    return entry_from(state, draft)


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


def preview_page(
    request: Request,
    state: ApplicationState,
    read: Read,
    draft: Mapping[str, str],
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    """What was read, week by week against the record as it is, with the way to save it."""
    changes = changes_for(read.items, state.project_state, occurrences=occurrences, kinds=kinds)
    counts = {
        "new": sum(1 for change in changes if change.state == NEW),
        "updated": sum(1 for change in changes if change.state == CLAIMED),
        "unchanged": sum(1 for change in changes if change.state == KNOWN),
        "review": sum(1 for change in changes if change.state == REVIEW),
    }
    to_save = counts["new"] + counts["updated"] + counts["review"]
    return templates.TemplateResponse(
        request,
        "inbox_preview.html",
        {
            "weeks": by_week(changes),
            "changes": changes,
            "counts": counts,
            "to_save": to_save,
            "unread": read.unread,
            "draft": draft,
            "kind_choices": KIND_CHOICES,
            "notice": notice,
            "sample": state.settings.sample,
            "spoken_report": spoken_report,
            "spoken_day": spoken_day,
        },
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
    draft = draft_of(form)
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return problem_page(request, state, draft, read)
    return preview_page(request, state, read, draft)


@router.post("/enter", response_class=HTMLResponse, include_in_schema=False)
async def read_entry(request: Request, state: State) -> Response:
    """Read one assignment typed by hand and show it, without writing anything."""
    form = await submitted(request)
    draft = {name: form.get(name, "") for name in ENTRY_FIELDS}
    read = entry_from(state, draft)
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
    was added, updated, and unchanged; or show the review again if a question is open.

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
    occurrences, kinds = answers_from(form)
    async with state.decision_lock:
        kept = keep(read.items, state.project_state, occurrences=occurrences, kinds=kinds)
    if not isinstance(kept, Kept):
        return preview_page(
            request, state, read, draft, occurrences=occurrences, kinds=kinds, notice=LOOK_AGAIN
        )
    return RedirectResponse(
        f"/parent?added={kept.added}&updated={kept.updated}&unchanged={kept.unchanged}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
