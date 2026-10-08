# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade pages: adding a report by pasting it, and its review.

Every family grade route is a parent's. With sign-in on, the gate holds them to a signed-in
parent. With sign-in off, the router's own dependency holds them to the computer running
Blossom, named as itself: a loopback client, and a single Host header naming a loopback address
or ``localhost``, so a page that renames itself to this computer can't reach them. No route here
uses ``FormRoute`` or a ``Form()`` parameter, so that dependency runs before any body is read;
each POST reads its fields with ``fields_of`` inside the endpoint.

The review writes nothing, and the paste travels only in a form's body, never in an address or a
log. The student line is shown as read and compared only through its keyed form, under the key
drawn from the household secret: read at startup with sign-in on, and at the first review with
sign-in off.
"""

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from starlette.requests import Request as StarletteRequest

from blossom.dependencies import ApplicationState, get_application_state
from blossom.grades.draft import (
    GradeCategory,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    capture_key,
)
from blossom.grades.identity import IdentityStatus, name_form_key
from blossom.grades.review import (
    CLASS_NAME_LIMIT,
    TERM_LIMIT,
    GradeReview,
    ItemStatus,
    ReviewItem,
    item_keys,
)
from blossom.grades.text_reader import NotRead, read_grade_report, reading_complete
from blossom.household import UnreadableHouseholdSecret, read_authority, secret_beside
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes.forms import fields_of
from blossom.routes.student import templates
from blossom.stores.paths import UnsafeCheckpointPath

logger = logging.getLogger(__name__)

ADD: Final = "/parent/grades/add"
REVIEW: Final = "/parent/grades/add/review"
EDIT: Final = "/parent/grades/add/edit"
SAVE: Final = "/parent/grades/add/save"
"""Where the review's answers go; the save itself is another page's."""
TEXT: Final = frozenset({"report_text"})

PASTE_HINT: Final = "Copy one class's grade report from the school's gradebook, then paste it here."
NOTHING_PASTED: Final = "Nothing was pasted. Nothing was saved. Paste a grade report."
TOO_LONG: Final = (
    "This is longer than one grade report. Nothing was saved. Paste one class's report at a time."
)
NOT_A_REPORT: Final = (
    "Blossom couldn't find a grade report in this text. Nothing was saved. Copy the report "
    "again from the school's gradebook."
)
SEVERAL_REPORTS: Final = (
    "This text has more than one report. Nothing was saved. Paste one class's report at a time."
)
NOT_WHOLE: Final = (
    "This form came in incomplete, so it wasn't read. Nothing was saved. Paste the report again."
)
NO_NAME_CHECK: Final = (
    "Blossom can't read the household secret, so this report can't be checked against her "
    "name. Nothing was saved. The household guide says how to replace the secret file."
)
TEXT_KEPT: Final = "Your text is kept."
ONLY_HERE: Final = (
    "Grade reports can be added only on the computer running Blossom, or with sign-in on."
)
NO_STUDENT_LINE: Final = "This report has no student name."
STATUS_WORDS: Final[dict[ItemStatus, str]] = {
    ItemStatus.NEW: "New",
    ItemStatus.CHANGED: "Changed",
    ItemStatus.SAVED: "Saved",
    ItemStatus.MATCHES_EARLIER: "Matches an earlier saved value",
    ItemStatus.COVERED: "Shown in a newer report",
    ItemStatus.NEEDS_ANSWER: "Needs your answer",
    ItemStatus.UNREADABLE: "Couldn't read",
    ItemStatus.DUE_NOT_CAPTURED: "Due date not captured",
    ItemStatus.VALUE_NOT_CAPTURED: "No score in this copy",
}
"""Each value's status as the review names it."""
NOT_READ: Final[dict[NotRead, str]] = {
    NotRead.NO_HEADER: NOT_A_REPORT,
    NotRead.SEVERAL_REPORTS: SEVERAL_REPORTS,
}
MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class NotOnThisComputer(Exception):
    """A family grade route asked with sign-in off from anywhere but the computer running
    Blossom; nothing was read and nothing was written."""


def loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``address`` is this computer's own, an IPv4-mapped IPv6 address read as its IPv4
    address."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def from_this_computer(request: StarletteRequest) -> bool:
    """Whether a request comes from a loopback client and names this computer as its one Host:
    a loopback address, brackets of an IPv6 one stripped, or ``localhost``."""
    if request.client is None:
        return False
    try:
        client = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    hosts = request.headers.getlist("host")
    if not loopback(client) or len(hosts) != 1:
        return False
    authority = read_authority(hosts[0], request.url.scheme)
    if authority is None:
        return False
    name = authority[0]
    if name == "localhost":
        return True
    try:
        named = ipaddress.ip_address(name.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return loopback(named)


def on_this_computer(request: Request) -> None:
    """The family grade routes' dependency: with sign-in off, only this computer."""
    state = get_application_state(request)
    if not state.settings.household_sign_in and not from_this_computer(request):
        raise NotOnThisComputer


family_router = APIRouter(prefix="/parent/grades", dependencies=[Depends(on_this_computer)])
State = Annotated[ApplicationState, Depends(get_application_state)]


def refused_here(marks: object) -> Callable[[Request, Exception], Response]:
    """The answer to ``NotOnThisComputer``: a page saying where grade reports can be added."""

    def answer(request: Request, error: Exception) -> Response:
        return templates.TemplateResponse(
            request,
            "grade_refused.html",
            {"said": ONLY_HERE, "marks": marks},
            status_code=403,
        )

    return answer


async def grade_name_key(request: Request, state: ApplicationState) -> bytes:
    """The key name forms are made under: read with the sign-in keys at startup, or, with
    sign-in off, from the secret at the first review, made then if none exists."""
    held: bytes | None = getattr(request.app.state, "grade_name_key", None)
    if held is not None:
        return held
    async with state.decision_lock:
        held = getattr(request.app.state, "grade_name_key", None)
        if held is None:
            secret = await asyncio.to_thread(secret_beside, state.settings.database_path)
            held = name_form_key(secret)
            request.app.state.grade_name_key = held
    return held


def paste_page(
    request: Request,
    state: ApplicationState,
    *,
    text: str = "",
    said: str | None = None,
    status_code: int = 200,
) -> Response:
    """The paste page, holding ``text``, with what wasn't processed first when ``said``."""
    return templates.TemplateResponse(
        request,
        "grade_add.html",
        {
            "text": text,
            "said": said,
            "kept": TEXT_KEPT if said is not None and text.strip() else None,
            "hint": PASTE_HINT,
            "review": REVIEW,
            "text_max_length": TEXT_MAX_LENGTH,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@family_router.get("/add", response_class=HTMLResponse)
def add_report(request: Request, state: State) -> Response:
    """Route 1: the paste page, empty."""
    return paste_page(request, state)


@family_router.post("/add/edit", response_class=HTMLResponse)
async def edit_text(request: Request, state: State) -> Response:
    """Route 2b: the paste page holding the review's text, read from its own small form."""
    fields, whole = await fields_of(request, TEXT)
    text = fields.get("report_text", "")
    if not whole:
        return paste_page(request, state, text=text, said=NOT_WHOLE, status_code=422)
    return paste_page(request, state, text=text)


@family_router.post("/add/review", response_class=HTMLResponse)
async def review_report(request: Request, state: State) -> Response:
    """Route 2: the review of the pasted text, or the paste page saying why there is none."""
    fields, whole = await fields_of(request, TEXT)
    text = fields.get("report_text", "")
    if not whole:
        return paste_page(request, state, text=text, said=NOT_WHOLE, status_code=422)
    if not text.strip():
        return paste_page(request, state, said=NOTHING_PASTED, status_code=422)
    if len(text) > TEXT_MAX_LENGTH:
        return paste_page(request, state, text=text, said=TOO_LONG, status_code=422)
    reading = read_grade_report(text)
    draft = reading.draft
    if draft is None:
        said = NOT_READ[reading.not_read or NotRead.NO_HEADER]
        return paste_page(request, state, text=text, said=said, status_code=422)
    try:
        key = await grade_name_key(request, state)
    except (UnreadableHouseholdSecret, UnsafeCheckpointPath, OSError) as error:
        logger.warning("The household secret couldn't be read: %s", type(error).__name__)
        return paste_page(request, state, text=text, said=NO_NAME_CHECK, status_code=500)
    complete = reading_complete(reading)
    review = state.project_state.review_grade_report(
        draft, capture_key(draft), key=key, complete=complete
    )
    shown = review_context(review, draft, text, list(reading.unrecognized), state)
    return templates.TemplateResponse(request, "grade_review.html", {**shown, "complete": complete})


@dataclass(frozen=True)
class Shown:
    """One value of the review as its page shows it: its position, its review item, what kind
    of value it is, and the draft's cells for it."""

    position: int
    item: ReviewItem
    kind: str
    category: GradeCategory | None = None
    row: GradeRow | None = None
    selectable: bool = False
    ticked: bool = False


def shown_items(review: GradeReview, draft: GradeReportDraft) -> list[Shown]:
    """Each value of ``review`` beside the draft's cells for it, named by its position."""
    pieces: list[tuple[str, GradeCategory | None, GradeRow | None]] = []
    if review.term is not None:
        pieces.append(("term", None, None))
    pieces.extend(("category", category, None) for category in draft.categories)
    pieces.extend(("row", category, row) for category in draft.categories for row in category.rows)
    keys = item_keys(draft)
    shown = []
    for position, (item, (kind, category, row), key) in enumerate(
        zip(review.items, pieces, keys, strict=True)
    ):
        if item.key != key:
            msg = "a review's items and its draft's positions disagree"
            raise RuntimeError(msg)
        offered = item.key in review.ready
        selectable = offered or item.status is ItemStatus.NEEDS_ANSWER or bool(item.choices)
        shown.append(
            Shown(position, item, kind, category, row, selectable=selectable, ticked=offered)
        )
    return shown


def cell(value: GradeValue) -> str:
    """A cell as the review reads it out: as written, or what its presence says."""
    if value.presence is Presence.REPORTED:
        return value.text
    if value.presence is Presence.BLANK:
        return "blank"
    if value.presence is Presence.UNREADABLE:
        return f"couldn't be read: {value.text}"
    return "not in the copy"


def review_context(
    review: GradeReview,
    draft: GradeReportDraft,
    text: str,
    unrecognized: list[str],
    state: ApplicationState,
) -> dict[str, object]:
    """What the review page shows and carries."""
    header = draft.header
    items = shown_items(review, draft)
    counts: dict[str, int] = {}
    for shown in items:
        word = STATUS_WORDS[shown.item.status].lower()
        counts[word] = counts.get(word, 0) + 1
    return {
        "review": review,
        "items": items,
        "counts": counts,
        "status_words": STATUS_WORDS,
        "cell": cell,
        "student_line": header.student_line,
        "identity": review.identity.status.value,
        "statuses": IdentityStatus,
        "no_student_line": NO_STUDENT_LINE,
        "term": draft.term,
        "term_label": header.term_label,
        "year_label": header.year_label,
        "class_name_limit": CLASS_NAME_LIMIT,
        "term_limit": TERM_LIMIT,
        "months": MONTHS,
        "unrecognized": unrecognized,
        "text": text,
        "save": SAVE,
        "edit": EDIT,
        "add": ADD,
        "marks": state.settings.page_marks,
    }
