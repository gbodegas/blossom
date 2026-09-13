"""The way in for assignments: the school's text pasted, or one typed by hand, shown first.

Two forms on the family page post here. The paste box takes the portal's
homework page, its weekly summary, or the school's "Missing" email as text;
the entry form takes one assignment typed by a parent. Either is read into
readings and shown on a page of its own, each reading a card saying what it
would do to the record, before anything is written: from that page a parent
puts them on record, or goes back. The keeping reads the same text again
rather than trusting what was shown, so what is kept is measured against
the record as it is at that moment, and a reading already on record is
never made twice.
"""

from datetime import date
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.dependencies import ApplicationState, get_application_state
from blossom.intake import (
    KNOWN,
    TEXT_MAX_LENGTH,
    Read,
    by_hand,
    changes_for,
    keep,
    read_text,
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
NOTHING_PASTED: Final = (
    "Nothing was pasted. Copy the homework page, the weekly summary, or the email as "
    "text, and paste it whole."
)
TOO_LONG: Final = (
    f"That is more than {TEXT_MAX_LENGTH:,} characters. Paste one page or one week at a time."
)
NEEDS_BOTH: Final = "An assignment needs a course and a title."
TOO_LONG_NAMES: Final = (
    f"A course is at most {COURSE_MAX_LENGTH} characters and a title at most {TITLE_MAX_LENGTH}."
)
NOT_A_DATE: Final = "A date is entered as the calendar gives it, year, month, and day."
FAR_DATE: Final = "A date is within a year of today; check the year."
NOT_A_KIND: Final = "The kind is homework or a task."


def entry_from(
    state: ApplicationState, course: str, title: str, due_date: str, assigned_on: str, kind: str
) -> Read | str:
    """A reading from the entry form's fields, or the sentence saying what is wrong."""
    course, title = course.strip(), title.strip()
    if not course or not title:
        return NEEDS_BOTH
    if len(course) > COURSE_MAX_LENGTH or len(title) > TITLE_MAX_LENGTH:
        return TOO_LONG_NAMES
    try:
        due = date.fromisoformat(due_date.strip()) if due_date.strip() else None
        assigned = date.fromisoformat(assigned_on.strip()) if assigned_on.strip() else None
    except ValueError:
        return NOT_A_DATE
    today = state.clock.today()
    if any(when is not None and not within_a_school_year(when, today) for when in (due, assigned)):
        return FAR_DATE
    try:
        chosen = AssignmentKind(kind)
    except ValueError:
        return NOT_A_KIND
    reading = by_hand(course, title, due, assigned, chosen, now=state.clock.now())
    return Read(items=(reading,), unread=())


def preview_page(
    request: Request, state: ApplicationState, read: Read, fields: dict[str, str]
) -> HTMLResponse:
    """What was read, card by card, against the record as it is, with the way to keep it."""
    changes = changes_for(read.items, state.project_state)
    return templates.TemplateResponse(
        request,
        "inbox_preview.html",
        {
            "changes": changes,
            "to_keep": sum(1 for change in changes if change.state != KNOWN),
            "known": sum(1 for change in changes if change.state == KNOWN),
            "unread": read.unread,
            "fields": fields,
            "sample": state.settings.sample,
        },
    )


@router.post("/read", response_class=HTMLResponse, include_in_schema=False)
def read_paste(request: Request, state: State, text: Annotated[str, Form()] = "") -> Response:
    """Read the pasted text and show what was read, without writing anything."""
    if not text.strip():
        return review_page(
            request,
            state,
            problem=NOTHING_PASTED,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if len(text) > TEXT_MAX_LENGTH:
        return review_page(
            request, state, problem=TOO_LONG, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    read = read_text(text, now=state.clock.now(), today=state.clock.today())
    return preview_page(request, state, read, {"text": text})


@router.post("/enter", response_class=HTMLResponse, include_in_schema=False)
def read_entry(
    request: Request,
    state: State,
    course: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
    assigned_on: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = AssignmentKind.HOMEWORK.value,
) -> Response:
    """Read one assignment typed by hand and show it, without writing anything."""
    read = entry_from(state, course, title, due_date, assigned_on, kind)
    if isinstance(read, str):
        return review_page(
            request, state, problem=read, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    fields = {
        "course": course,
        "title": title,
        "due_date": due_date,
        "assigned_on": assigned_on,
        "kind": kind,
    }
    return preview_page(request, state, read, fields)


@router.post("/keep", response_class=HTMLResponse, include_in_schema=False)
def keep_readings(
    request: Request,
    state: State,
    text: Annotated[str, Form()] = "",
    course: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
    assigned_on: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = AssignmentKind.HOMEWORK.value,
) -> Response:
    """Read the same text or entry again, put what is new on record, and say how much."""
    if text.strip():
        if len(text) > TEXT_MAX_LENGTH:
            return review_page(
                request, state, problem=TOO_LONG, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
            )
        read: Read | str = read_text(text, now=state.clock.now(), today=state.clock.today())
    else:
        read = entry_from(state, course, title, due_date, assigned_on, kind)
    if isinstance(read, str):
        return review_page(
            request, state, problem=read, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    kept = keep(read.items, state.project_state)
    return RedirectResponse(f"/parent?kept={kept}", status_code=status.HTTP_303_SEE_OTHER)
