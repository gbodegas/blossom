# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade pages: adding a report by pasting it, its review, its save, the save's outcome,
Grades, and class details.

Every family grade route is a parent's. With sign-in on, the gate holds them to a signed-in
parent. With sign-in off, the router's own dependency holds them to the computer running
Blossom, named as itself: a loopback client, and a single Host header naming a loopback address
or ``localhost``, so a page that renames itself to this computer can't reach them. No route here
uses ``FormRoute`` or a ``Form()`` parameter, so that dependency runs before any body is read;
each POST reads its fields with ``fields_of`` inside the endpoint.

The review and the check write nothing, and the paste travels only in a form's body, never in an
address or a log. A save checks the page against the record and writes once or not at all; its
outcome is read again by acceptance ID behind the same checks as any family request. The student
line is shown as read and compared only through its keyed form, under the key drawn from the
household secret: read at startup with sign-in on, and at the first review with sign-in off.

Grades and class details read alone. Her pages, under ``/student/grades``, are any viewer's; the
family's, under ``/parent/grades``, are a parent's, with the controls a parent reads. Each viewer
keeps one remembered term, which steers only the term Grades opens on; class details names its
term by its place in the class's year in its address. A parent's choice of the current term
changes the household's context alone, compared and set, and leaves every remembered term.

Homework class names is a parent's page on the family routes: each class name homework
carries, with the class of the current school year it is. A parent's answer for a name is
compared and set alone, and it rewrites no homework.
"""

import asyncio
import hashlib
import ipaddress
import logging
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, tzinfo
from functools import partial
from typing import Annotated, Final, Literal, NamedTuple, cast, get_args

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request as StarletteRequest

from blossom.agent.runs import worker_to_the_end
from blossom.dependencies import ApplicationState, get_application_state
from blossom.grades.dates import due_date_of
from blossom.grades.draft import (
    HEX_KEY,
    GradeCategory,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    ReportHeader,
    capture_key,
    folded,
    is_school_year,
)
from blossom.grades.identity import IdentityStatus, name_form_key
from blossom.grades.review import (
    ANSWERS_FOR,
    CLASS_NAME_LIMIT,
    RESULT_FIELDS,
    TERM_LIMIT,
    AlreadyRecorded,
    Cell,
    ChoiceFacts,
    ClassReport,
    CurrentValue,
    CurrentValues,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    GradeValueUndecodable,
    HomeworkLinksConflict,
    IdentityAnswer,
    ItemStatus,
    Label,
    MatchAnswer,
    NotHers,
    RecordedSave,
    ReportUse,
    ReturnReason,
    ReviewItem,
    ReviewPage,
    SaveOutcome,
    StillAsked,
    cells_of,
    class_asked,
    first_month_asked,
    identity_asked,
    item_keys,
    labels_too_long,
    matches_asked,
    setup_asked,
    still_asked,
    use_asked,
)
from blossom.grades.school_scores import Link, linked_results
from blossom.grades.text_reader import NotRead, read_grade_report, reading_complete
from blossom.grades.wording import (
    absent_alike,
    as_said,
    described,
    presence_words,
    report_names,
    score_line,
    spoken_date,
)
from blossom.homework_search import PAGE_SIZE
from blossom.household import UnreadableHouseholdSecret, read_authority, secret_beside
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes.captures import (
    HELP_FIELDS,
    HELP_NOT_ASKED,
    HELP_WITHOUT_THE_PAGE,
    QUESTION_TOO_LONG,
)
from blossom.routes.forms import fields_of
from blossom.routes.homework_scores import (
    FoundState,
    GradesToCheck,
    HomeworkFound,
    HomeworkRow,
    NoList,
    OtherResult,
    PairStanding,
    ResultHomework,
    Withheld,
    account_read,
    checks_left_out,
    cues_left_out,
    grades_to_check,
    homework_found,
    other_results,
    read_grade_facts,
    read_links,
    result_homework,
    result_own,
    results_read,
    rule_values,
    title_of,
    waiting_in,
)
from blossom.routes.navigation import (
    CLASS_AT,
    FAMILY_PAGE,
    GRADES,
    HER_GRADES,
    RESULT_HOMEWORK,
    RESULT_HOMEWORK_AT,
    WEEK_PAGE,
    address,
    asked_address,
    segment,
)
from blossom.routes.store_refusals import RECORDS_UNREAD, busy_file
from blossom.routes.student import (
    FORM_SENT_OTHER_WORDS,
    FORM_USED,
    HELP_FORM_NOT_WHOLE,
    NOT_HERS_TO_ASK,
    NOT_HERS_TO_UPDATE,
    ReturnLink,
    parent_reads,
    templates,
    unavailable_page,
    viewer_of,
)
from blossom.stores.gradebook import (
    ACCEPTANCE_ID,
    CLASS_ID,
    RESULT_ID,
    REVISION_MAX,
    UNCONNECTED,
    ClassTermRead,
    ConfirmedBy,
    ContextChanged,
    ContextNotOnRecord,
    GradebookRecords,
    GradeReportNotSaved,
    GradeTransactionLost,
    HomeworkClassChanged,
    HomeworkClassNotOnRecord,
    HomeworkLinkChanged,
    HomeworkLinkClassNameChanged,
    HomeworkLinkHomeworkNotOnRecord,
    HomeworkLinkResultNotOnRecord,
    HomeworkYearNotCurrent,
    LinkDecision,
    NamedClass,
    RequiredIndexUnavailable,
    Term,
    TermNotOnRecord,
    Viewer,
    ViewNotOnRecord,
    class_for,
    name_key,
)
from blossom.stores.help_requests import (
    NOTE_MAX_LENGTH,
    AskOutcome,
    HelpAlreadyAsked,
    HelpAsked,
    HelpFormChanged,
    HelpFormUsed,
    NotARequestId,
    UnknownResultReference,
    new_request_id,
    request_id_from,
)
from blossom.stores.paths import UnsafeCheckpointPath
from blossom.stores.project_state import ProjectStateStore, normalize_note

logger = logging.getLogger(__name__)

ADD: Final = "/parent/grades/add"
REVIEW: Final = "/parent/grades/add/review"
EDIT: Final = "/parent/grades/add/edit"
SAVE: Final = "/parent/grades/add/save"
CHECK: Final = "/parent/grades/add/check"
SAVED_AT: Final = "/parent/grades/saved/{acceptance_id}"
TEXT: Final = frozenset({"report_text"})


def one_of(values: Iterable[str]) -> re.Pattern[str]:
    """A grammar that takes exactly one of ``values``."""
    return re.compile("|".join(re.escape(value) for value in values))


REVISION: Final = r"none|[1-9][0-9]{0,18}"
NONE_OR_REVISION: Final = re.compile(REVISION)
NONE_OR_HEX_KEY: Final = re.compile(rf"none|{HEX_KEY.pattern}")
MONTH_ANSWER: Final = re.compile(r"(?:unsure|[1-9]|1[0-2])?")
CLASS_ANSWER: Final = re.compile(rf"new|{CLASS_ID.pattern}:(?:{REVISION})")
TICK: Final = re.compile("1")
RESULT_IDS: Final = re.compile(rf"{RESULT_ID.pattern}(?: {RESULT_ID.pattern})*")
MATCH_ANSWER: Final = re.compile(rf"different|choose|{RESULT_ID.pattern}")
EMPTY_OR_RESULT_ID: Final = re.compile(rf"(?:{RESULT_ID.pattern})?")


def text_key(text: str) -> str:
    """The key of a report text as a review form sends it back: the SHA-256 of the text in hex,
    each line break read as a line feed, since a browser's form sends every one as a carriage
    return and a line feed, and a null character read as the replacement character the page's
    parser puts in its place."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Field:
    """One name the review form may send: whether the page fills it or the parent types it,
    where the page sends it, and the grammar a page-filled value fits whole."""

    owner: Literal["page", "typed"]
    where: Literal["always", "answer", "item", "row"]
    value: re.Pattern[str] | None


FORM: Final[Mapping[str, Field]] = {
    "report_text": Field("typed", "always", None),
    "acceptance_id": Field("page", "always", ACCEPTANCE_ID),
    "revision": Field("page", "always", NONE_OR_REVISION),
    "source_key": Field("page", "always", HEX_KEY),
    "text_key": Field("page", "always", HEX_KEY),
    "identity_form": Field("page", "always", NONE_OR_HEX_KEY),
    "identity": Field("page", "answer", one_of(answer.value for answer in IdentityAnswer)),
    "setup": Field("page", "answer", one_of(("report", "other"))),
    "setup_year": Field("typed", "answer", None),
    "setup_term": Field("typed", "answer", None),
    "first_month": Field("page", "answer", MONTH_ANSWER),
    "class": Field("page", "answer", CLASS_ANSWER),
    "class_name": Field("typed", "answer", None),
    "use": Field("page", "answer", one_of(get_args(ReportUse))),
    "select": Field("page", "item", TICK),
    "candidates": Field("page", "row", RESULT_IDS),
    "choices": Field("page", "row", RESULT_IDS),
    "match": Field("page", "row", MATCH_ANSWER),
    "choose": Field("page", "row", EMPTY_OR_RESULT_ID),
}
"""Every name the review form may send. An item's and a row's are sent once per position, as
``select.3``, and every page sends each "always" field."""
ALWAYS: Final = frozenset(name for name, field in FORM.items() if field.where == "always")
POSITION_FREE: Final = frozenset(
    name for name, field in FORM.items() if field.where in ("always", "answer")
)
"""The names no row's place decides: each sent once at most, whatever the text."""
BY_A_ROW: Final = frozenset(name for name, field in FORM.items() if field.where in ("item", "row"))
"""The bases of the names a row's place decides, as ``select`` in ``select.3``."""
TEXT_AND_KEY: Final = frozenset({"report_text", "text_key"})


def not_text_or_key(name: str) -> bool:
    """Whether ``name`` is any but the text's and its key's, which the form's first read
    passes over."""
    return name not in TEXT_AND_KEY


def by_a_row(name: str) -> bool:
    """Whether ``name`` is on the base of a name a row's place decides, whatever follows it."""
    return name.split(".", 1)[0] in BY_A_ROW


PASTE_HINT: Final = "Copy one class's grade report from the school's gradebook, then paste it here."
NOTHING_PASTED: Final = "Nothing was pasted. Nothing was saved. Paste a grade report."
TOO_LONG: Final = (
    "This text exceeds the size limit. Nothing was saved. Paste one class's report at a time."
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
    "Blossom couldn't check that this report is hers. Nothing was saved. See the household guide "
    "before trying again."
)
TEXT_KEPT: Final = "Your text is kept."
TEXT_KEPT_ANSWER_AGAIN: Final = "Your text is kept. Check the answers again."
ONLY_HERE: Final = (
    "Grade reports can be added only on the computer running Blossom, or with sign-in on."
)
NO_STUDENT_LINE: Final = "No student name was found in this copy."
NOTHING_SAVED: Final = "Nothing was saved."
BOTH_KEPT: Final = "Your text and answers are kept."
ANSWER_THE_NAME: Final = "Answer whether this is her report before saving."
CORRECT_THE_MARKED: Final = "Nothing was saved. Correct the marked answer."
YEAR_FORM: Final = "The school year needs a form like 2026-2027."
TERM_BLANK: Final = "The term is blank."
CLASS_BLANK: Final = "The class name is blank."
CHOOSE_CLASS: Final = "Choose the class, or name a new one."
CHOOSE_WHICH: Final = "Choose which assignment this is."
TOO_LONG_LABEL: Final[dict[Label, str]] = {
    "class_name": f"The class name is longer than its limit of {CLASS_NAME_LIMIT} characters.",
    "term": f"The term is longer than its limit of {TERM_LIMIT} characters.",
}
CHANGED_WHILE_REVIEWING: Final = (
    "These grades changed while you were reviewing. Nothing was saved. Check the review again."
)
ANSWER_DOESNT_FIT: Final = (
    "An answer doesn't fit this report now: another page saved it, or the name check changed. "
    "Nothing was saved."
)
NOT_NEW: Final = "A ticked result isn't new any more. Nothing was saved. Check the ticks again."
TEXT_CHANGED: Final = "The report text changed. Nothing was saved. Review the text again."
RETURNED: Final[dict[ReturnReason, str]] = {
    ReturnReason.REVISION: CHANGED_WHILE_REVIEWING,
    ReturnReason.ANSWERS: ANSWER_DOESNT_FIT,
    ReturnReason.SELECTION: NOT_NEW,
    ReturnReason.SOURCE: TEXT_CHANGED,
}
"""What a review a save returned says first, by why it returned."""
NOT_SAVED: Final = "Blossom couldn't save this. Nothing was saved. Try again."
OUTCOME_UNKNOWN: Final = (
    "Blossom couldn't tell whether this report was saved. Saving again is safe: it won't save "
    "anything twice."
)
NOT_HERS: Final = 'You answered "Not hers". Nothing was saved.'
SAVED_ELSEWHERE: Final = "This report was saved from another page or tab."
REVIEW_GRADE_REPORT: Final = "Review grade report"
"""The heading of a report's review, and of what the review sent when it can't be rebuilt."""
LEFT_OUT: Final = "These results weren't part of that save:"
NOT_ON_RECORD: Final = "This save's outcome isn't on record."
NO_VALUES_CHANGED: Final = "No grade values changed."
ALL_ALREADY_SAVED: Final = "Every result in this report was already saved."
NAME_CONFIRMED: Final = "Her name was confirmed with this report."
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
NOT_CAPTURED_BY_KIND: Final = {
    "term": "No percent or letter grade in this copy",
    "category": "No weight or average in this copy",
    "row": "No score in this copy",
}
""""Value not captured" by the kind of value, each named by its own fields."""
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
            secret = await worker_to_the_end(partial(secret_beside, state.settings.database_path))
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

    def reviewed() -> Response:
        review = state.project_state.review_grade_report(
            draft, capture_key(draft), key=key, complete=complete
        )
        shown = review_context(review, draft, text, list(reading.unrecognized), state)
        return templates.TemplateResponse(
            request, "grade_review.html", {**shown, "complete": complete}
        )

    try:
        return await asyncio.to_thread(reviewed)
    except sqlite3.Error as error:
        logger.warning("%s could not be read: %s", ADD_GRADE_REPORT, type(error).__name__)
        return paste_page(request, state, text=text, said=RECORDS_UNREAD, status_code=503)


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


SAVED_LABELS: Final = {
    "points": "school score",
    "max_points": "maximum",
    "percent": "percent",
    "letter": "letter grade",
    "average": "average",
}
"""The fields a changed value's saved line shows, in its order, each by its own name."""


def cell(value: GradeValue, label: str | None = None, *, named: bool = False) -> str:
    """A draft's cell as the review reads it out, short after the page's own label for it, and
    under ``label`` where it stands alone."""
    return described(value.presence, value.text, label, named=named)


def saved_now(cells: Mapping[str, Cell]) -> list[str]:
    """A changed value's saved cells as the review reads them out, each under its field's name
    where the record has no value for it."""
    return [
        described(Presence(presence), text, SAVED_LABELS[field])
        for field, (presence, text) in cells.items()
        if field in SAVED_LABELS
    ]


ADJUSTING: Final = ("curve", "bonus", "penalty")
"""The school record's cells that adjust a score."""
RECORD_FIELDS: Final = (
    ("curve", "Curve"),
    ("bonus", "Bonus"),
    ("penalty", "Penalty"),
    ("weight", "Weight"),
    ("note", "Note"),
)
"""The school record details of a result, by field, with their labels."""
REVIEW_RECORD_FIELDS: Final = (("average", "Average"), *RECORD_FIELDS)
"""A review row's school record details: its average as the report wrote it, then the
record cells, since the row shows its score, maximum, status and due date already."""


def record_text(cell: Cell) -> str:
    """A school record cell after its label: as written, or the words for its presence."""
    return cell[1] if cell[0] is Presence.REPORTED else presence_words("", cell).strip()


def adjusts(text: str) -> bool:
    """Whether an adjusting cell's text says it changed a score: anything but a zero."""
    try:
        return float(text) != 0
    except ValueError:
        return bool(text.strip())


@dataclass(frozen=True)
class SchoolRecord:
    """A review row's school record details: each record cell its save stores, under its label,
    and the cues its summary names."""

    lines: list[str]
    cues: list[str]


def school_record(category: GradeCategory, row: GradeRow) -> SchoolRecord:
    """The school record details of ``row``, each cell as written or in the words for its
    presence, with "Adjusted" when a curve, bonus or penalty is reported and not zero, and
    "Teacher's note" when the note has text."""
    cells = cells_of(RESULT_FIELDS, (category.name, *row.cells()))
    cues = []
    if any(
        cells[field][0] is Presence.REPORTED and adjusts(cells[field][1]) for field in ADJUSTING
    ):
        cues.append("Adjusted")
    if cells["note"][0] is Presence.REPORTED and cells["note"][1].strip():
        cues.append("Teacher's note")
    lines = [f"{label}: {record_text(cells[field])}" for field, label in REVIEW_RECORD_FIELDS]
    return SchoolRecord(lines, cues)


TOLD_APART_BY: Final[tuple[Callable[[ChoiceFacts, Mapping[str, str]], str], ...]] = (
    lambda one, _: (
        f"saved score {described(*one.points, None)} / {described(*one.max_points, 'maximum')}"
    ),
    lambda one, _: described(*one.category, "Category name"),
    lambda one, names: names[one.report_id],
    lambda one, _: f"row {one.position} of that report",
)
"""What a choice's name adds, in order, while it is still the same as another's; its report
by the name ``report_names`` gives that report ID."""


def numbered(
    labels: dict[str, str], facts: Mapping[str, ChoiceFacts], groups: list[list[str]]
) -> None:
    """Each label of ``groups`` with its place among its group, in one order that a refresh
    keeps: the report's acceptance order, then the row's position, then the result."""
    for group in groups:
        ordered = sorted(group, key=lambda one: (facts[one].report_order, facts[one].position, one))
        for place, result in enumerate(ordered, start=1):
            labels[result] = f"{labels[result]} · saved entry {place} of {len(ordered)}"


def tied(labels: Mapping[str, str]) -> list[list[str]]:
    """The groups of results whose labels are the same, across the whole control."""
    groups: dict[str, list[str]] = {}
    for result, label in labels.items():
        groups.setdefault(label, []).append(result)
    return [group for group in groups.values() if len(group) > 1]


def choice_labels(
    ids: Iterable[str], offered: Mapping[str, ChoiceFacts], *, names: Mapping[str, str]
) -> dict[str, str]:
    """How a control names each result it offers, never by its ID: its title and due date, then,
    only while labels are the same, the saved score, the category, its report by its name in
    ``names``, its row there, and its saved entry among those still the same."""
    facts = {result: offered[result] for result in ids}
    labels = {
        result: " · ".join(
            (
                described(*one.title, "Assignment"),
                STATUS_WORDS[ItemStatus.DUE_NOT_CAPTURED]
                if one.due is None
                else described(*one.due, "due", named=True),
            )
        )
        for result, one in facts.items()
    }
    for told_apart_by in TOLD_APART_BY:
        for group in tied(labels):
            for result in group:
                labels[result] = f"{labels[result]} · {told_apart_by(facts[result], names)}"
    before = dict(labels)
    numbered(labels, facts, tied(labels))
    if tied(labels):
        labels = before
        numbered(labels, facts, [list(labels)])
    if tied(labels):
        msg = "a control's choices share a name"
        raise RuntimeError(msg)
    return labels


def status_word(shown: Shown) -> str:
    """How the review names a value's status: a value the copy didn't capture by its own
    fields, any other by its status alone."""
    if shown.item.status is ItemStatus.VALUE_NOT_CAPTURED:
        return NOT_CAPTURED_BY_KIND[shown.kind]
    return STATUS_WORDS[shown.item.status]


def review_context(
    review: GradeReview,
    draft: GradeReportDraft,
    text: str,
    unrecognized: list[str],
    state: ApplicationState,
) -> dict[str, object]:
    """What the review page shows and carries, each choice's report named as class details name
    it."""
    header = draft.header
    items = shown_items(review, draft)
    counts: dict[str, int] = {}
    for shown in items:
        word = status_word(shown).lower()
        counts[word] = counts.get(word, 0) + 1
    return {
        "review": review,
        "items": items,
        "counts": counts,
        "status_word": status_word,
        "cell": cell,
        "saved_now": saved_now,
        "school_record": school_record,
        "choice_labels": partial(
            choice_labels, names=report_names(review.reports, state.clock.zone)
        ),
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
        "text_key": text_key(text),
        "save": SAVE,
        "check": CHECK,
        "edit": EDIT,
        "add": ADD,
        "marks": state.settings.page_marks,
        "said": None,
        "problems": [],
        "to_resolve": frozenset(),
        "choose_which": CHOOSE_WHICH,
        "posted": None,
        "ticks": None,
        "recorded": None,
        "left_out": None,
    }


# ------------------------------------------------------------- the save, the check, the outcome


@dataclass(frozen=True)
class Positions:
    """A draft's item keys by position, and the position its first row starts at."""

    keys: tuple[str, ...]
    first_row: int

    @property
    def rows(self) -> range:
        """The positions of the draft's rows."""
        return range(self.first_row, len(self.keys))

    @property
    def names(self) -> frozenset[str]:
        """Every field name the review's form may send for this draft."""
        names = set(POSITION_FREE)
        for position in range(len(self.keys)):
            for name, field in FORM.items():
                if field.where == "item" or (field.where == "row" and position in self.rows):
                    names.add(f"{name}.{position}")
        return frozenset(names)


def positions_of(draft: GradeReportDraft) -> Positions:
    """Where ``draft``'s items stand on its review page."""
    keys = item_keys(draft)
    rows = sum(len(category.rows) for category in draft.categories)
    return Positions(keys, len(keys) - rows)


class Damaged(ValueError):
    """A field the page itself fills came back outside its form."""


@dataclass(frozen=True)
class RowValues:
    """A row's page-filled fields as sent: the candidates its question offered, the results its
    list offered, its radio answer, and its list's pick (None where it has no list)."""

    candidates: tuple[str, ...]
    choices: tuple[str, ...]
    match: str | None
    choose: str | None


@dataclass(frozen=True)
class PositionFree:
    """The page-filled values of a review form that no row's place decides, read against their
    grammar."""

    acceptance_id: str
    revision: int | None
    source_key: str
    text_key: str
    identity_form: str | None
    identity: IdentityAnswer | None
    setup: str | None
    first_month: str | None
    chosen_class: Literal["new"] | tuple[str, int | None] | None
    use: ReportUse | None

    @property
    def same_class(self) -> str | None:
        """The year's class the page chose as this report's, or None."""
        return self.chosen_class[0] if isinstance(self.chosen_class, tuple) else None


@dataclass(frozen=True)
class PageValues:
    """Every page-filled value of a review form, read against its grammar: those no row's place
    decides, the ticked positions, and each row's fields."""

    free: PositionFree
    ticked: frozenset[int]
    rows: Mapping[int, RowValues]


def revision_of(text: str) -> int | None:
    """A revision its grammar took: None for ``none``, else a number the store's column holds."""
    if text == "none":
        return None
    if int(text) > REVISION_MAX:
        raise Damaged(text)
    return int(text)


def listed(fields: Mapping[str, str], name: str) -> tuple[str, ...]:
    """The result IDs a row's list field carries, none where the row has no such list."""
    return tuple(fields[name].split(" ")) if name in fields else ()


def row_values(fields: Mapping[str, str], position: int) -> RowValues:
    """A row's fields as sent, ``Damaged`` where they disagree with the lists the row carries:
    a repeated result, a pick without a list or a list without a pick, a pick or a radio answer
    its list didn't offer, and an answer that needs a list the row doesn't carry."""
    candidates = listed(fields, f"candidates.{position}")
    choices = listed(fields, f"choices.{position}")
    match = fields.get(f"match.{position}")
    choose = fields.get(f"choose.{position}")
    if len(set(candidates)) != len(candidates) or len(set(choices)) != len(choices):
        raise Damaged(position)
    if (choose is None) != (not choices) or (choose and choose not in choices):
        raise Damaged(position)
    if match in ("different", "choose") and not candidates:
        raise Damaged(position)
    if match == "choose" and not choices:
        raise Damaged(position)
    if match not in (None, "different", "choose") and match not in candidates:
        raise Damaged(position)
    return RowValues(candidates, choices, match, choose)


def position_free(fields: Mapping[str, str]) -> PositionFree:
    """The page-filled values no row's place decides, each checked whole against its grammar
    before anything reads one; ``Damaged`` at the first that fails. Any other name is left
    unread."""
    for name, value in fields.items():
        grammar = FORM[name].value if name in POSITION_FREE else None
        if grammar is not None and not grammar.fullmatch(value):
            raise Damaged(name)
    chosen_class: Literal["new"] | tuple[str, int | None] | None = None
    if fields.get("class") == "new":
        chosen_class = "new"
    elif "class" in fields:
        class_id, _, class_revision = fields["class"].partition(":")
        chosen_class = (class_id, revision_of(class_revision))
    identity = fields.get("identity")
    return PositionFree(
        acceptance_id=fields["acceptance_id"],
        revision=revision_of(fields["revision"]),
        source_key=fields["source_key"],
        text_key=fields["text_key"],
        identity_form=None if fields["identity_form"] == "none" else fields["identity_form"],
        identity=None if identity is None else IdentityAnswer(identity),
        setup=fields.get("setup"),
        first_month=fields.get("first_month") or None,
        chosen_class=chosen_class,
        use=cast("ReportUse | None", fields.get("use")),
    )


def page_values(fields: Mapping[str, str], positions: Positions) -> PageValues:
    """Every page-filled value of a review form checked whole against its grammar, and each row
    against its own lists, before anything else reads one; ``Damaged`` at the first that
    fails."""
    free = position_free(fields)
    for name, value in fields.items():
        field = FORM.get(name.partition(".")[0])
        if field is None:
            raise Damaged(name)
        if field.value is not None and not field.value.fullmatch(value):
            raise Damaged(name)
    return PageValues(
        free=free,
        ticked=frozenset(
            position for position in range(len(positions.keys)) if f"select.{position}" in fields
        ),
        rows={position: row_values(fields, position) for position in positions.rows},
    )


@dataclass(frozen=True)
class Posted:
    """A review form as sent: the page it came from, its answers (None with no answer about the
    name), the ticked keys, what the parent must correct, and the rows to resolve."""

    page: ReviewPage
    answers: GradeAnswers | None
    selection: frozenset[str]
    problems: tuple[str, ...]
    to_resolve: frozenset[int] = frozenset()


RESOLVE: Final = "resolve"
"""A row whose two answer controls disagree, or whose "Choose an existing assignment" names
nothing: the parent resolves it."""


def row_answer(key: str, row: RowValues) -> MatchAnswer | Literal["resolve"] | None:
    """A row's one answer from its radio and its list: a pick alone, or beside "Choose an
    existing assignment" or the same candidate, is that result; a pick beside "A different
    assignment" or another candidate contradicts it."""
    pick = row.choose or None
    if row.match is None:
        return None if pick is None else MatchAnswer(key, row.choices, pick, chosen=True)
    if row.match == "choose":
        return RESOLVE if pick is None else MatchAnswer(key, row.choices, pick, chosen=True)
    if row.match == "different":
        return RESOLVE if pick is not None else MatchAnswer(key, row.candidates, None)
    if pick is not None and pick != row.match:
        return RESOLVE
    return MatchAnswer(key, row.candidates, row.match)


def matches_of(
    values: PageValues, positions: Positions
) -> tuple[list[MatchAnswer], frozenset[int]]:
    """Each row's one answer, by position, and the rows the parent must resolve: one whose
    controls disagree or name nothing, and every row whose answer names a result another row's
    answer names too."""
    answers: dict[int, MatchAnswer] = {}
    to_resolve: set[int] = set()
    for position, row in values.rows.items():
        answer = row_answer(positions.keys[position], row)
        if isinstance(answer, MatchAnswer):
            answers[position] = answer
        elif answer == RESOLVE:
            to_resolve.add(position)
    named: dict[str, list[int]] = {}
    for position, answer in answers.items():
        if answer.result_id is not None:
            named.setdefault(answer.result_id, []).append(position)
    to_resolve.update(position for rows in named.values() if len(rows) > 1 for position in rows)
    kept = [answer for position, answer in answers.items() if position not in to_resolve]
    return kept, frozenset(to_resolve)


def posted_of(
    values: PageValues, fields: Mapping[str, str], draft: GradeReportDraft, positions: Positions
) -> Posted:
    """The answers and ticks a whole review form sends: the page's own values as
    ``page_values`` read them, and the parent's typed text from ``fields``."""
    header = draft.header
    free = values.free
    page = ReviewPage(free.acceptance_id, free.revision, free.source_key)
    problems: list[str] = []
    setup: tuple[str, str] | None = None
    if free.setup == "report":
        setup = (header.year_label, header.term_label)
    elif free.setup == "other":
        setup = (folded(fields.get("setup_year", "")), folded(fields.get("setup_term", "")))
        if not is_school_year(setup[0]):
            problems.append(YEAR_FORM)
        if not setup[1]:
            problems.append(TERM_BLANK)
    first_month: tuple[str, int] | None = None
    if free.first_month is not None and free.first_month != "unsure":
        first_month = (header.year_label, int(free.first_month))
    new_class = same_class = None
    same_revision: int | None = None
    if free.chosen_class == "new":
        new_class = folded(fields.get("class_name", ""))
        if not new_class:
            problems.append(CLASS_BLANK)
    elif free.chosen_class is not None:
        same_class, same_revision = free.chosen_class
    matches, to_resolve = matches_of(values, positions)
    if to_resolve:
        problems.append(CHOOSE_WHICH)
    selection = frozenset(positions.keys[position] for position in values.ticked)
    answers = None
    if free.identity is not None:
        answers = GradeAnswers(
            identity=free.identity,
            identity_form=free.identity_form,
            setup=setup,
            first_month=first_month,
            new_class=new_class,
            same_class=same_class,
            same_class_revision=same_revision,
            matches=tuple(matches),
            use=free.use,
        )
        problems.extend(TOO_LONG_LABEL[label] for label in labels_too_long(answers))
    elif new_class is not None and len(new_class) > CLASS_NAME_LIMIT:
        problems.append(TOO_LONG_LABEL["class_name"])
    return Posted(page, answers, selection, tuple(dict.fromkeys(problems)), to_resolve)


@dataclass(frozen=True)
class ReviewForm:
    """A review form read whole: its text, the reading and draft of that text, the positions,
    the fields as sent, what they answer, and whether the text isn't the one its page carried."""

    text: str
    unrecognized: list[str]
    complete: bool
    draft: GradeReportDraft
    positions: Positions
    fields: dict[str, str]
    posted: Posted
    free: PositionFree
    text_changed: bool = False


async def review_form_of(request: Request, state: ApplicationState) -> ReviewForm | Response:
    """The review form a save or a check sent, or the paste page saying why it can't be read:
    the text and its key first, read alone; then, for the text its page carried, every field by
    the names its draft allows; for any other text, only the fields no row's place decides, so
    nothing a row sent is read against rows the text may not hold."""
    first, whole = await fields_of(request, TEXT_AND_KEY, ignored=not_text_or_key)
    text = first.get("report_text", "")
    if not text.strip():
        return paste_page(request, state, said=NOTHING_PASTED, status_code=422)
    if len(text) > TEXT_MAX_LENGTH:
        return paste_page(request, state, text=text, said=TOO_LONG, status_code=422)
    sent_key = first.get("text_key", "")
    if not whole or not HEX_KEY.fullmatch(sent_key):
        return paste_page(request, state, text=text, said=NOT_WHOLE, status_code=422)
    reading = read_grade_report(text)
    draft = reading.draft
    if draft is None:
        said = NOT_READ[reading.not_read or NotRead.NO_HEADER]
        return paste_page(request, state, text=text, said=said, status_code=422)
    positions = positions_of(draft)
    changed = sent_key != text_key(text)
    try:
        if changed:
            fields, whole = await fields_of(
                request,
                POSITION_FREE,
                may_be_absent=POSITION_FREE - ALWAYS,
                ignored=by_a_row,
            )
            if not whole:
                raise Damaged(NOT_WHOLE)
            values = PageValues(position_free(fields), ticked=frozenset(), rows={})
        else:
            names = positions.names
            fields, whole = await fields_of(request, names, may_be_absent=names - ALWAYS)
            if not whole:
                raise Damaged(NOT_WHOLE)
            values = page_values(fields, positions)
        posted = posted_of(values, fields, draft, positions)
    except Damaged:
        return paste_page(request, state, text=text, said=NOT_WHOLE, status_code=422)
    return ReviewForm(
        text,
        list(reading.unrecognized),
        reading_complete(reading),
        draft,
        positions,
        fields,
        posted,
        values.free,
        text_changed=changed,
    )


# ------------------------------------------------------------- what a returned review keeps

ANSWER_NAMES: Final = frozenset(
    {
        "identity",
        "setup",
        "setup_year",
        "setup_term",
        "first_month",
        "class",
        "class_name",
        "use",
        "match",
        "choose",
        "select",
    }
)
"""The bases of the names whose values answer the review: each answer no row's place decides,
each row's radio and pick, and each tick. A row's offered lists are drawn again from the
review, and an empty pick is its placeholder."""
RowNow = tuple[bool, str | None, str | None]
"""An item as a review page writes it now: whether it is selectable, and its row's candidates
and choices as the page sends them, None where it sends none."""


@dataclass(frozen=True)
class Returning:
    """What a returned review keeps a form's fields against: the form, the review as it reads
    now, what of the answers and ticks still answers it where a save checked them (None where
    nothing was checked against the record), whether a chosen class whose revision moved is
    shown at its current one, and each item as the page writes it now."""

    form: ReviewForm
    review: GradeReview
    still: StillAsked | None
    rewrite_class: bool
    rows_now: Mapping[int, RowNow]

    @property
    def answered(self) -> dict[str, MatchAnswer]:
        """Each row's answer as sent, by the row's key."""
        answers = self.form.posted.answers
        return {} if answers is None else {answer.row_key: answer for answer in answers.matches}


def names_offered(status: IdentityStatus) -> frozenset[str]:
    """The answers about the line a review page offers: those a save takes, and "Not hers"
    wherever the page asks."""
    offered = set(ANSWERS_FOR[status])
    if status is not IdentityStatus.MATCHES:
        offered.add(IdentityAnswer.NOT_HERS)
    return frozenset(answer.value for answer in offered)


def identity_kept(at: Returning, name: str, value: str) -> str | None:
    """The answer about the name, while the line it answered is the line read now and the page
    offers that answer."""
    identity = at.review.identity
    if at.form.free.identity_form != identity.form:
        return None
    return value if value in names_offered(identity.status) else None


def setup_kept(at: Returning, name: str, value: str) -> str | None:
    """The setup's choice and its year and term as typed, while the setup is asked."""
    return value if at.review.setup is not None else None


def month_kept(at: Returning, name: str, value: str) -> str | None:
    """The month as sent, untouched or "Not sure yet" alike, while the month is asked."""
    return value if at.review.first_month is not None else None


def class_kept(at: Returning, name: str, value: str) -> str | None:
    """The class chosen, while the class is asked and the choice offered: a new one as sent, and
    the same class as sent at its revision now, or at its current one where ``rewrite_class``."""
    question = at.review.class_question
    if question.matched is not None:
        return None
    if value == "new":
        return value
    chosen = value.partition(":")[0]
    for class_id, _, revision in question.existing:
        if class_id == chosen:
            now = f"{class_id}:{'none' if revision is None else revision}"
            return now if now == value or at.rewrite_class else None
    return None


def class_name_kept(at: Returning, name: str, value: str) -> str | None:
    """The class's name as typed, while the class is asked, whatever class is chosen."""
    return value if at.review.class_question.matched is None else None


def use_kept(at: Returning, name: str, value: str) -> str | None:
    """The use chosen, while the use is asked."""
    return value if at.review.use is not None else None


def row_kept(at: Returning, name: str, value: str) -> str | None:
    """A row's field or an item's tick: never after a changed text; where nothing was checked
    against the record, a row's fields while its lists as sent are the review's now and a tick
    while its item is selectable; otherwise exactly when what it carries still answers."""
    base, _, place = name.partition(".")
    position = int(place)
    if at.form.text_changed:
        return None
    selectable, candidates, choices = at.rows_now.get(position, (False, None, None))
    if at.still is None:
        if base == "select":
            return value if selectable else None
        fields = at.form.fields
        sent = (fields.get(f"candidates.{position}"), fields.get(f"choices.{position}"))
        return value if sent == (candidates, choices) else None
    key = at.form.positions.keys[position]
    if base == "select":
        return value if key in at.still.selection else None
    answer = at.answered.get(key)
    return value if answer is not None and answer in at.still.matches else None


KEPT_BY: Final[Mapping[str, Callable[[Returning, str, str], str | None]]] = {
    "identity": identity_kept,
    "setup": setup_kept,
    "setup_year": setup_kept,
    "setup_term": setup_kept,
    "first_month": month_kept,
    "class": class_kept,
    "class_name": class_name_kept,
    "use": use_kept,
    "select": row_kept,
    "candidates": row_kept,
    "choices": row_kept,
    "match": row_kept,
    "choose": row_kept,
}
"""What decides whether a returned review keeps each name the form sends, but those the review
mints again: the question it answers, asked now and still offering it."""


def rows_now(review: GradeReview, draft: GradeReportDraft) -> dict[int, RowNow]:
    """Each item of ``review`` by its position, as its page writes it."""
    now: dict[int, RowNow] = {}
    for shown in shown_items(review, draft):
        item = shown.item
        asks = shown.selectable
        now[shown.position] = (
            asks,
            " ".join(item.question.ids) if asks and item.question is not None else None,
            " ".join(item.choices) if asks and item.choices else None,
        )
    return now


def answers_of(fields: Mapping[str, str]) -> dict[str, str]:
    """The answers among ``fields``: every name on an answer's base but an empty pick, a class
    by its ID alone."""
    return {
        name: value.partition(":")[0] if name == "class" else value
        for name, value in fields.items()
        if name.partition(".")[0] in ANSWER_NAMES and not (name.startswith("choose.") and not value)
    }


@dataclass(frozen=True)
class Kept:
    """What a returned review shows of a form: the fields it keeps, the ticks (None for the
    review's own), and what it says of the answers."""

    fields: dict[str, str]
    ticks: frozenset[int] | None
    sentence: str


def kept_on_return(
    form: ReviewForm,
    review: GradeReview,
    still: StillAsked | None,
    *,
    rewrite_class: bool,
    source: bool,
) -> Kept:
    """The form's fields a returned review keeps, each as sent while the question it answers is
    asked now and still offers it, and dropped otherwise; "Your text and answers are kept."
    only when every answer sent is kept and ``source`` doesn't say the text changed."""
    at = Returning(form, review, still, rewrite_class, rows_now(review, form.draft))
    fields: dict[str, str] = {}
    for name, value in form.fields.items():
        decides = KEPT_BY.get(name.partition(".")[0])
        kept = None if decides is None else decides(at, name, value)
        if kept is not None:
            fields[name] = kept
    ticks = None
    if not form.text_changed:
        ticks = frozenset(
            int(name.partition(".")[2]) for name in fields if name.startswith("select.")
        )
    every = not source and answers_of(fields) == answers_of(form.fields)
    return Kept(fields, ticks, BOTH_KEPT if every else TEXT_KEPT_ANSWER_AGAIN)


def rows_changed(form: ReviewForm, review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether a row answer ``review`` doesn't take was sent from a row whose lists aren't the
    ones the review offers now."""
    now = rows_now(review, form.draft)
    at = {key: position for position, key in enumerate(form.positions.keys)}
    fields = form.fields
    for answer in answers.matches:
        if matches_asked(review, (answer,)):
            continue
        position = at[answer.row_key]
        sent = (fields.get(f"candidates.{position}"), fields.get(f"choices.{position}"))
        if sent != now[position][1:]:
            return True
    return False


def answers_why(form: ReviewForm, review: GradeReview, answers: GradeAnswers) -> str:
    """Why the answers don't fit the review a save returned, from each check they fail in the
    save's order: one whose question the page asked otherwise than the review asks it now
    doesn't fit; then a setup both ask, sent unanswered, is a damaged form; then a class both
    ask, left unanswered, is to be chosen. A failure none of these names doesn't fit."""
    fields = form.fields
    question = review.class_question
    failing: list[str | None] = []
    if not identity_asked(review, answers):
        failing.append(ANSWER_DOESNT_FIT)
    if not setup_asked(review, answers):
        asked = "setup_year" in fields
        if asked != (review.setup is not None):
            failing.append(ANSWER_DOESNT_FIT)
        else:
            failing.append(NOT_WHOLE if asked and answers.setup is None else None)
    if not first_month_asked(review, answers):
        failing.append(ANSWER_DOESNT_FIT if "first_month" in fields else None)
    if not class_asked(review, answers):
        asked = "class_name" in fields
        offered = {class_id for class_id, _, _ in question.existing}
        unanswered = answers.new_class is None and answers.same_class is None
        if asked != (question.matched is None) or answers.same_class not in {None, *offered}:
            failing.append(ANSWER_DOESNT_FIT)
        else:
            failing.append(CHOOSE_CLASS if asked and unanswered else None)
    if not matches_asked(review, answers.matches):
        failing.append(ANSWER_DOESNT_FIT if rows_changed(form, review, answers) else None)
    if not use_asked(review, answers.use):
        failing.append(ANSWER_DOESNT_FIT)
    if not failing or None in failing or ANSWER_DOESNT_FIT in failing:
        return ANSWER_DOESNT_FIT
    return NOT_WHOLE if NOT_WHOLE in failing else CHOOSE_CLASS


def review_page(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    review: GradeReview,
    *,
    said: str | None = None,
    problems: tuple[str, ...] | list[str] = (),
    to_resolve: frozenset[int] = frozenset(),
    fields: dict[str, str] | None = None,
    ticks: frozenset[int] | None = None,
    recorded: str | None = None,
    left_out: list[str] | None = None,
    kept_sentence: str = BOTH_KEPT,
    status_code: int = 200,
    about_the_name: bool = False,
) -> Response:
    """The review of the form's text, with what wasn't processed said first, and the fields and
    ticks it keeps. ``about_the_name`` says that line asks for the answer about whose report
    this is, so the question carries it as its description."""
    shown = review_context(review, form.draft, form.text, form.unrecognized, state)
    return templates.TemplateResponse(
        request,
        "grade_review.html",
        {
            **shown,
            "complete": form.complete,
            "said": said,
            "problems": list(problems),
            "to_resolve": to_resolve,
            "kept_sentence": kept_sentence if said is not None else None,
            "posted": fields,
            "ticks": ticks,
            "recorded": recorded,
            "left_out": left_out,
            "left_out_said": LEFT_OUT,
            "about_the_name": about_the_name,
        },
        status_code=status_code,
    )


IDENTITY_SENT: Final[Mapping[str, str]] = {
    "confirmed": "These are her grades",
    "hers": "Yes, this is her name",
    "misread": "The name was misread",
    "not_hers": "Not hers",
}
"""The review page's own label for each answer about the name, by the value the form sends."""
TERM_ITEM: Final = "Term grade on this report"
SAME_ON_RECORD: Final = "The same assignment as one on record"
DIFFERENT: Final = "A different assignment"
CHOOSE_EXISTING: Final = "Choose an existing assignment"
CHOSEN_ON_RECORD: Final = "Chosen: an assignment on record"
SAME_CLASS_ON_RECORD: Final = "It's the same class as a class on record"


@dataclass(frozen=True)
class SentLine:
    """One answer a review form sent, as the page shows it: the review page's own label, and
    after it the words the answer carries, as written, where it carries any."""

    label: str
    authored: str | None = None


@dataclass(frozen=True)
class SentItem:
    """One result a review form ticked, or sent an answer for: its title, as the report wrote
    it (``authored``) or as the review names it, and each answer it sent."""

    title: str
    authored: bool
    answers: tuple[str, ...]


@dataclass(frozen=True)
class SentView:
    """What a review form sent, from its fields alone: the answers about the report, the
    results ticked to save, and the rows not ticked that sent an answer. A choice known only by
    a record's ID is named generically, and a field not sent shows nothing."""

    about: tuple[SentLine, ...]
    ticked: tuple[SentItem, ...]
    unticked: tuple[SentItem, ...]


def about_sent(
    fields: Mapping[str, str], header: ReportHeader, *, complete: bool
) -> tuple[SentLine, ...]:
    """The answers about the report a form sent, each by the review page's own label."""
    lines = []
    identity = fields.get("identity", "")
    if identity == IdentityAnswer.SHOWN:
        lines.append(SentLine("Shows", header.student_line))
    elif identity in IDENTITY_SENT:
        lines.append(SentLine(IDENTITY_SENT[identity]))
    setup = fields.get("setup")
    if setup == "report":
        lines.append(SentLine("This report's:", f"{header.term_label} · {header.year_label}"))
    elif setup == "other":
        lines.append(SentLine("Another:"))
        if "setup_year" in fields:
            lines.append(SentLine("School year:", fields["setup_year"]))
        if "setup_term" in fields:
            lines.append(SentLine("Term:", fields["setup_term"]))
    month = fields.get("first_month", "")
    if month:
        said = "Not sure yet" if month == "unsure" else MONTHS[int(month) - 1]
        lines.append(SentLine(f"Month the {header.year_label} school year starts:", said))
    chosen = fields.get("class")
    if chosen == "new":
        lines.append(SentLine("Name for this class:", fields.get("class_name", "")))
    elif chosen:
        lines.append(SentLine(SAME_CLASS_ON_RECORD))
    use = fields.get("use")
    if use == "current":
        lines.append(
            SentLine(
                "Use this as the current school record"
                if complete
                else "Use these values where this report provides them"
            )
        )
    elif use == "earlier":
        lines.append(SentLine("Keep as an earlier report"))
    return tuple(lines)


def item_titles(draft: GradeReportDraft) -> list[tuple[str, bool]]:
    """Each item's title by its place on the review page, and whether the report wrote it."""
    categories = [(category.name.text.strip(), True) for category in draft.categories]
    rows = [
        (row.assignment.text.strip(), True)
        for category in draft.categories
        for row in category.rows
    ]
    term = [(TERM_ITEM, False)] if len(item_keys(draft)) > len(categories) + len(rows) else []
    return [*term, *categories, *rows]


def row_sent(fields: Mapping[str, str], position: int) -> tuple[str, ...]:
    """Each answer a row sent about which assignment it is, conflicting ones included."""
    answers = []
    match = fields.get(f"match.{position}")
    if match == "different":
        answers.append(DIFFERENT)
    elif match == "choose":
        answers.append(CHOOSE_EXISTING)
    elif match:
        answers.append(SAME_ON_RECORD)
    if fields.get(f"choose.{position}"):
        answers.append(CHOSEN_ON_RECORD)
    return tuple(answers)


def sent_view(
    fields: Mapping[str, str],
    draft: GradeReportDraft,
    *,
    complete: bool,
    text_changed: bool,
) -> SentView:
    """What a review form sent, read from its fields and its text alone. A form whose text
    changed lays nothing a row sent against the rows of the text it carries."""
    about = about_sent(fields, draft.header, complete=complete)
    if text_changed:
        return SentView(about, (), ())
    first_row = positions_of(draft).first_row
    ticked, unticked = [], []
    for position, (title, authored) in enumerate(item_titles(draft)):
        answers = row_sent(fields, position) if position >= first_row else ()
        item = SentItem(title, authored, answers)
        if fields.get(f"select.{position}") == "1":
            ticked.append(item)
        elif answers:
            unticked.append(item)
    return SentView(about, tuple(ticked), tuple(unticked))


def retry_page(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    said: str,
    *,
    status_code: int = 500,
    check: bool = False,
) -> Response:
    """The review form sent again as it came, under the same acceptance ID, for a save whose
    outcome isn't known or that the record refused, or for a press whose read before any
    write was refused; with ``check``, offered again as a check, with no save. The store
    isn't read."""
    return templates.TemplateResponse(
        request,
        "grade_retry.html",
        {
            "said": said,
            "kept_sentence": TEXT_KEPT_ANSWER_AGAIN if form.text_changed else BOTH_KEPT,
            "text": form.text,
            "fields": {name: value for name, value in form.fields.items() if name != "report_text"},
            "save": SAVE,
            "check": CHECK if check else None,
            "edit": EDIT,
            "add": ADD,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


def unread_before_writing(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    error: sqlite3.Error,
    *,
    check: bool,
) -> Response:
    """A save or a check whose read was refused before anything was written: the form kept as
    sent, under the records sentence, offered again as the same press."""
    logger.warning("%s could not be read: %s", REVIEW_GRADE_REPORT, type(error).__name__)
    return retry_page(request, state, form, RECORDS_UNREAD, status_code=503, check=check)


def saved_before_page(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    outcome: AlreadyRecorded,
    error: sqlite3.Error,
) -> Response:
    """A page whose acceptance ID a save recorded, whose review can't then be rebuilt: the
    earlier save's notice and its link, known without a read, and what the page sent, read
    only, with nothing to press and nothing more read."""
    logger.warning("%s could not be read: %s", REVIEW_GRADE_REPORT, type(error).__name__)
    return templates.TemplateResponse(
        request,
        "grade_retry.html",
        {
            "said": f"{SAVED_ELSEWHERE} {RECORDS_UNREAD}",
            "recorded": outcome_address(outcome.saved.acceptance_id),
            "text": form.text,
            "sent": sent_view(
                form.fields, form.draft, complete=form.complete, text_changed=form.text_changed
            ),
            "add": ADD,
            "marks": state.settings.page_marks,
        },
        status_code=503,
    )


async def returned_or_retried(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    answers: GradeAnswers,
    outcome: SaveOutcome | GradeReview,
    key: bytes,
    *,
    check: bool,
) -> Response:
    """``returned_page``, or, when the review it rebuilds can't be read, the earlier save's
    page for a recorded acceptance ID and the press offered again for anything else."""
    try:
        return await asyncio.to_thread(returned_page, request, state, form, answers, outcome, key)
    except sqlite3.Error as error:
        if isinstance(outcome, AlreadyRecorded):
            return saved_before_page(request, state, form, outcome, error)
        return unread_before_writing(request, state, form, error, check=check)


def role_of(state: ApplicationState) -> ConfirmedBy:
    """Who a grade write records: a signed-in parent, or the household with sign-in off."""
    return "parent" if state.settings.household_sign_in else "household"


def outcome_address(acceptance_id: str) -> str:
    """The outcome page of the save recorded under ``acceptance_id``."""
    return SAVED_AT.format(acceptance_id=acceptance_id)


def titles(review: GradeReview, draft: GradeReportDraft, keys: frozenset[str]) -> list[str]:
    """How the review names each item of ``keys``, in its order."""
    named = []
    for shown in shown_items(review, draft):
        if shown.item.key not in keys:
            continue
        if shown.kind == "term":
            named.append("Term grade")
        elif shown.kind == "category" and shown.category is not None:
            named.append(cell(shown.category.name, "Category name"))
        elif shown.row is not None:
            named.append(cell(shown.row.assignment, "Assignment"))
    return named


def kept_of(
    state: ApplicationState,
    form: ReviewForm,
    review: GradeReview,
    answers: GradeAnswers,
    selection: Collection[str],
    key: bytes,
) -> StillAsked:
    """What of ``answers`` and ``selection`` still answers ``review``, each tick judged as a
    save would judge it, with the kept matching answers applied."""

    def settle(matches: tuple[MatchAnswer, ...]) -> GradeReview:
        return state.project_state.review_grade_report(
            form.draft,
            capture_key(form.draft),
            key=key,
            complete=form.complete,
            same_class=form.free.same_class,
            matches=matches,
        )

    return still_asked(review, answers, selection, settle)


def ticks_of(form: ReviewForm) -> frozenset[int]:
    """The positions the form ticked."""
    return frozenset(
        position for position, key in enumerate(form.positions.keys) if key in form.posted.selection
    )


def returned_page(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    answers: GradeAnswers,
    outcome: SaveOutcome | GradeReview,
    key: bytes,
) -> Response:
    """The answer to a save or a check: its outcome by acceptance ID, a recorded save's notice,
    "Not hers", or the review as it reads now with what still answers it kept and what changed
    said first."""
    posted = form.posted
    if isinstance(outcome, GradeReportSaved):
        return RedirectResponse(outcome_address(outcome.acceptance_id), status_code=303)
    if isinstance(outcome, NotHers):
        return templates.TemplateResponse(
            request,
            "grade_refused.html",
            {"said": NOT_HERS, "marks": state.settings.page_marks, "add": ADD},
        )
    if isinstance(outcome, AlreadyRecorded):
        review = state.project_state.review_grade_report(
            form.draft,
            capture_key(form.draft),
            key=key,
            complete=form.complete,
            same_class=form.free.same_class,
        )
        still = kept_of(state, form, review, answers, outcome.uncovered or posted.selection, key)
        kept = kept_on_return(form, review, still, rewrite_class=True, source=False)
        left = titles(review, form.draft, outcome.uncovered) if outcome.uncovered else None
        return review_page(
            request,
            state,
            form,
            review,
            said=SAVED_ELSEWHERE,
            fields=kept.fields,
            ticks=kept.ticks,
            recorded=outcome_address(outcome.saved.acceptance_id),
            left_out=left,
            kept_sentence=kept.sentence,
        )
    if isinstance(outcome, GradeReview):
        return review_page(request, state, form, outcome, fields=form.fields, ticks=ticks_of(form))
    review = outcome.review
    refused = outcome.refused
    if outcome.why is ReturnReason.SELECTION and refused and refused <= outcome.still_asking:
        at = {item_key: position for position, item_key in enumerate(form.positions.keys)}
        return review_page(
            request,
            state,
            form,
            replace(review, acceptance_id=posted.page.acceptance_id),
            said=CORRECT_THE_MARKED,
            problems=(CHOOSE_WHICH,),
            to_resolve=frozenset(at[item_key] for item_key in outcome.still_asking),
            fields=form.fields,
            ticks=ticks_of(form),
            status_code=422,
        )
    if outcome.why is ReturnReason.ANSWERS:
        why = answers_why(form, review, answers)
        if why == NOT_WHOLE:
            return paste_page(request, state, text=form.text, said=NOT_WHOLE, status_code=422)
        if why == CHOOSE_CLASS:
            return review_page(
                request,
                state,
                form,
                review,
                said=CORRECT_THE_MARKED,
                problems=(CHOOSE_CLASS,),
                fields=form.fields,
                ticks=ticks_of(form),
                status_code=422,
            )
    still = kept_of(state, form, review, answers, posted.selection, key)
    kept = kept_on_return(
        form,
        review,
        still,
        rewrite_class=outcome.why is ReturnReason.REVISION,
        source=outcome.why is ReturnReason.SOURCE,
    )
    return review_page(
        request,
        state,
        form,
        review,
        said=RETURNED[outcome.why],
        fields=kept.fields,
        ticks=kept.ticks,
        kept_sentence=kept.sentence,
        status_code=409,
    )


async def key_or_retry(
    request: Request, state: ApplicationState, form: ReviewForm
) -> bytes | Response:
    """The name-form key, or the retry page when the household secret can't be read."""
    try:
        return await grade_name_key(request, state)
    except (UnreadableHouseholdSecret, UnsafeCheckpointPath, OSError) as error:
        logger.warning("The household secret couldn't be read: %s", type(error).__name__)
        return retry_page(request, state, form, NO_NAME_CHECK)


def changed_text(
    request: Request, state: ApplicationState, form: ReviewForm, key: bytes
) -> Response:
    """The answer to a form whose text isn't the one its page carried, as a save answers a
    changed report: the text as sent reviewed fresh in the class the page chose, keeping the
    answers no row decides that still answer it, every row as a fresh review shows it, and
    nothing written."""
    review = state.project_state.review_grade_report(
        form.draft,
        capture_key(form.draft),
        key=key,
        complete=form.complete,
        same_class=form.free.same_class,
    )
    kept = kept_on_return(form, review, None, rewrite_class=False, source=True)
    return review_page(
        request,
        state,
        form,
        review,
        said=TEXT_CHANGED,
        fields=kept.fields,
        ticks=kept.ticks,
        kept_sentence=kept.sentence,
        status_code=409,
    )


def refused_answers(
    request: Request, state: ApplicationState, form: ReviewForm, key: bytes
) -> GradeAnswers | Response:
    """The form's answers, or, when the parent must answer or correct something before anything
    is checked, the review as posted under the page's own acceptance ID, revision and capture
    key. "Not hers" needs nothing else answered."""
    posted = form.posted
    answers = posted.answers
    if answers is not None and (not posted.problems or answers.identity is IdentityAnswer.NOT_HERS):
        return answers
    review = state.project_state.review_grade_report(
        form.draft,
        capture_key(form.draft),
        key=key,
        complete=form.complete,
        same_class=form.free.same_class,
    )
    review = replace(
        review,
        acceptance_id=posted.page.acceptance_id,
        revision=posted.page.revision,
        source_key=posted.page.source_key,
    )
    unanswered = f"{NOTHING_SAVED} {ANSWER_THE_NAME}"
    kept = kept_on_return(form, review, None, rewrite_class=False, source=False)
    return review_page(
        request,
        state,
        form,
        review,
        said=unanswered if posted.answers is None else CORRECT_THE_MARKED,
        problems=posted.problems,
        to_resolve=posted.to_resolve,
        fields=kept.fields,
        ticks=kept.ticks,
        kept_sentence=kept.sentence,
        status_code=422,
        about_the_name=posted.answers is None,
    )


@family_router.post("/add/save", response_class=HTMLResponse)
async def save_report(request: Request, state: State) -> Response:
    """Route 3: the save of the review's answers and ticks, then its outcome by acceptance ID;
    or, having written nothing, the page saying why."""
    form = await review_form_of(request, state)
    if isinstance(form, Response):
        return form
    key = await key_or_retry(request, state, form)
    if isinstance(key, Response):
        return key
    try:
        if form.text_changed:
            return await asyncio.to_thread(changed_text, request, state, form, key)
        answers = await asyncio.to_thread(refused_answers, request, state, form, key)
    except sqlite3.Error as error:
        return unread_before_writing(request, state, form, error, check=False)
    if isinstance(answers, Response):
        return answers
    try:
        async with state.decision_lock:
            outcome = await worker_to_the_end(
                partial(
                    state.project_state.save_grade_report,
                    form.draft,
                    capture_key(form.draft),
                    key=key,
                    page=form.posted.page,
                    answers=answers,
                    selection=form.posted.selection,
                    role=role_of(state),
                    complete=form.complete,
                )
            )
    except GradeTransactionLost:
        logger.warning("A grade report's save may not have finished: GradeTransactionLost")
        return retry_page(request, state, form, OUTCOME_UNKNOWN)
    except Exception as error:
        logger.warning("A grade report wasn't saved: %s", type(error).__name__)
        return retry_page(request, state, form, NOT_SAVED)
    if isinstance(outcome, AlreadyRecorded) and not outcome.uncovered:
        return RedirectResponse(outcome_address(outcome.saved.acceptance_id), status_code=303)
    return await returned_or_retried(request, state, form, answers, outcome, key, check=False)


@family_router.post("/add/check", response_class=HTMLResponse)
async def check_answers(request: Request, state: State) -> Response:
    """Route 2a: the review with the answers' effect shown under the page's own acceptance ID
    and revision, when the page is current; otherwise what a save would answer, writing
    nothing."""
    form = await review_form_of(request, state)
    if isinstance(form, Response):
        return form
    key = await key_or_retry(request, state, form)
    if isinstance(key, Response):
        return key
    try:
        if form.text_changed:
            return await asyncio.to_thread(changed_text, request, state, form, key)
        answers = await asyncio.to_thread(refused_answers, request, state, form, key)
        if isinstance(answers, Response):
            return answers
        outcome = await asyncio.to_thread(
            partial(
                state.project_state.check_grade_answers,
                form.draft,
                capture_key(form.draft),
                key=key,
                complete=form.complete,
                page=form.posted.page,
                answers=answers,
                selection=form.posted.selection,
            )
        )
    except sqlite3.Error as error:
        return unread_before_writing(request, state, form, error, check=True)
    return await returned_or_retried(request, state, form, answers, outcome, key, check=True)


def counted(number: int, one: str, many: str) -> str:
    """``number`` with the word for one or for many."""
    return f"{number} {one if number == 1 else many}"


def outcome_lines(saved: GradeReportSaved) -> list[str]:
    """What a committed save did, from its report and its counts: "Saved." only when values
    were added or updated, never "Nothing was saved", and each count only when above zero."""
    changed = saved.added + saved.updated
    lines = []
    if saved.report_id is not None and changed:
        parts = (
            f"{saved.added} added" if saved.added else "",
            f"{saved.updated} updated" if saved.updated else "",
        )
        lines.append("Saved. " + ", ".join(part for part in parts if part) + ".")
    else:
        lines.append(NO_VALUES_CHANGED)
    if not changed and not saved.left and saved.already_saved:
        lines.append(ALL_ALREADY_SAVED)
    elif saved.already_saved:
        were = "was" if saved.already_saved == 1 else "were"
        lines.append(f"{saved.already_saved} {were} already saved.")
    if saved.left:
        lines.append(f"{saved.left} {'is' if saved.left == 1 else 'are'} left to check.")
    kept = []
    if saved.shown:
        kept.append(counted(saved.shown, "assignment", "assignments") + " recorded as shown")
    if saved.answers_kept:
        kept.append(counted(saved.answers_kept, "answer", "answers") + " kept")
    if kept:
        lines.append(", ".join(kept) + ".")
    return lines


def name_confirmed(recorded: RecordedSave) -> bool:
    """Whether the stored answer confirmed her name: "Yes, this is her name" to a line not yet
    confirmed, or confirmed again."""
    return recorded.identity_answer is IdentityAnswer.HERS and recorded.identity_status in (
        IdentityStatus.FIRST_USE,
        IdentityStatus.NOT_CONFIRMED,
        IdentityStatus.CONFIRM_AGAIN,
    )


@family_router.get("/saved/{acceptance_id}", response_class=HTMLResponse)
def saved_report(acceptance_id: str, request: Request, state: State) -> Response:
    """Route 4: a recorded save's outcome, read by its acceptance ID behind the same checks as
    any family request; a page saying it isn't on record otherwise."""
    recorded = link = None
    try:
        if ACCEPTANCE_ID.fullmatch(acceptance_id):
            recorded = state.project_state.recorded_save(acceptance_id)
        if recorded is not None:
            link = class_link(state, recorded)
    except sqlite3.Error as error:
        return unshown(
            request,
            state,
            error,
            heading=ADD_GRADE_REPORT,
            said=SAVE_OUTCOME_UNSHOWN,
            again=asked_address(
                SAVED_AT.format(acceptance_id=segment(acceptance_id)),
                request.scope["query_string"],
            ),
            ways_back=[ReturnLink(FAMILY_PAGE, BACK_TO_FAMILY)],
        )
    context: dict[str, object] = {"add": ADD, "marks": state.settings.page_marks}
    if recorded is None:
        return templates.TemplateResponse(
            request,
            "grade_saved.html",
            {**context, "recorded": None, "said": NOT_ON_RECORD},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "grade_saved.html",
        {
            **context,
            "recorded": recorded,
            "lines": outcome_lines(recorded.saved),
            "name_said": NAME_CONFIRMED if name_confirmed(recorded) else None,
            "class_link": link,
            "grades": GRADES,
        },
    )


def class_link(state: ApplicationState, recorded: RecordedSave) -> str | None:
    """The address of class details of the class and term a save went into, by the term's place
    in the class's year; None when the term isn't on record."""
    terms = state.project_state.terms_of_year(recorded.year)
    if recorded.term not in terms:
        return None
    n = terms.index(recorded.term) + 1
    return CLASS_AT.format(base=GRADES, class_id=recorded.class_id, n=n)


# ------------------------------------------------------------- Grades and class details

CURRENT_TERM: Final = "/parent/grades/current-term"
FOLLOW_CURRENT: Final = "current"
"""The chooser's value for following the current term."""
VIEW_FIELDS: Final = frozenset({"view"})
CONTEXT_FIELDS: Final = frozenset({"shown", "term"})
POSITION: Final = re.compile(r"[1-9][0-9]{0,3}")
"""A term's place in its class's year, from 1, as an address writes it."""

HOMEWORK_CLASSES: Final = "/parent/grades/homework-classes"
NAME_AT: Final = "/parent/grades/homework-classes#name-{n}"
"""Where a save lands: its own name's heading, named by its place on the page alone."""
NAME_FIELDS: Final = frozenset({"year", "name", "shown", "class"})
UNANSWERED: Final = "unanswered"
"""What a name's form sends as the answer shown when none is on record for it."""
REMOVE_CONNECTION: Final = "Not connected"
"""The chooser's one more option on a name that has a class: it removes the connection."""
HOMEWORK_NAMES_INTRODUCTION: Final = (
    "Homework and grade reports sometimes name one class differently. Choose which of her "
    "classes each homework name is. You choose once for each name. Homework pages still show "
    "each class name as it was written."
)
ONE_NAME_WAITS: Final = "1 homework class name isn't connected to a class yet."
NAMES_WAIT: Final = "{n} homework class names aren't connected to a class yet."
CLASSES_COME_FROM_REPORTS: Final = (
    "Classes come from grade reports. A name can wait until a report for its class is added."
)
ADD_A_REPORT_TO_CONNECT: Final = (
    "Add a grade report to connect these homework names to her classes."
)
"""Said once, after the count, where there is no class to choose."""
MATCHED_BY_NAME: Final = "Matched by name to {name}."
CONNECTED_TO: Final = "Connected to {name}."
NOT_CONNECTED: Final = "Not connected yet."
NAME_CHANGED: Final = (
    "This class name changed while you were choosing. Nothing was saved. Check it again."
)
CHOOSE_A_CLASS: Final = "Choose a class. Nothing was saved."
SAVE_UNKNOWN: Final = "Blossom couldn't tell whether this was saved. Choosing again is safe."

NO_REPORTS_YET: Final = "No grade reports yet."
ADD_ONE: Final = "Add one to start."
TERM_NOT_ON_RECORD: Final = (
    "That term isn't on record. Nothing was saved. Choose a term from the list."
)
CONTEXT_CHANGED: Final = (
    "The current term changed while this page was open. Nothing was saved. "
    "Check the current term again."
)
CONTEXT_UNKNOWN: Final = (
    "Blossom couldn't tell whether the current term changed. Check the current term again."
)
CLASS_NOT_ON_RECORD: Final = "Blossom has no record for this class and term."
NO_REPORT_ADDED: Final = "No grade report added"

GRADES_UNSHOWN: Final = "Grades can't be shown right now."
CLASS_DETAILS: Final = "Class details"
CLASS_UNSHOWN: Final = "Class details can't be shown right now."
ADD_GRADE_REPORT: Final = "Add grade report"
SAVE_OUTCOME_UNSHOWN: Final = "This save's outcome can't be shown right now."
HOMEWORK_CLASS_NAMES: Final = "Homework class names"
HOMEWORK_NAMES_UNSHOWN: Final = "Homework class names can't be shown right now."
ASK_ABOUT_A_RESULT: Final = "Ask a parent about this result"
RESULT_UNSHOWN: Final = "This result can't be shown right now."
RESULT_HOMEWORK_UNSHOWN: Final = "This result's homework can't be shown right now."
BACK_TO_GRADES: Final = "Back to Grades"
BACK_TO_FAMILY: Final = "Back to Family review"
"""What a grade page says when its record can't be read as it is made, by page."""


student_router = APIRouter(prefix="/student/grades")


def term_named(term: Term) -> str:
    """A term as a page names it: ``T1 · 2026-2027``."""
    return f"{term[1]} · {term[0]}"


def term_sent(term: Term) -> str:
    """A term as a form sends it: the school year, a space, the term."""
    return f"{term[0]} {term[1]}"


def term_posted(value: str) -> Term | None:
    """The term a form sent, or None when the value isn't one: a school year, then a term."""
    year, _, term = value.partition(" ")
    return (year, term) if is_school_year(year) and term else None


def as_line(name: str) -> str:
    """A report's name as a line of its own: "Report added September 30 · Made current
    October 8"."""
    line = name.replace(", made current ", " · Made current ")
    return line[:1].upper() + line[1:]


def cell_text(cell: Cell, what: str) -> str:
    """A cell as written, or the words for its presence."""
    return cell[1] if cell[0] is Presence.REPORTED else presence_words(what, cell)


def term_grade(value: CurrentValue) -> str:
    """The school-reported term grade: the percent and the letter, each as written or in the
    review's words for its presence; one presence for the line when both share a blank or
    uncaptured one."""
    percent, letter = value.cells["percent"], value.cells["letter"]
    alike = absent_alike(percent, letter)
    if alike is not None:
        return presence_words("Term grade", alike)
    said = (as_said(percent, "percent"), described(*letter, "letter grade"))
    return "School-reported grade " + " · ".join(said)


def newest_current(reports: tuple[ClassReport, ...]) -> ClassReport | None:
    """The current report with the highest acceptance order, if any."""
    current = [report for report in reports if report.use == "current"]
    return current[-1] if current else None


@dataclass(frozen=True)
class TermSummary:
    """A class and term's grade lines: the term grade and the report that supplied it."""

    grade: str
    supplied_by: str


def term_summary(values: CurrentValues, names: dict[str, str]) -> TermSummary | None:
    """The term grade, named by the report that supplied it; None when no current report
    supplied one."""
    if values.term is None:
        return None
    supplied = as_line(names.get(values.term.report_id, "report"))
    return TermSummary(term_grade(values.term), supplied)


@dataclass(frozen=True)
class ClassRow:
    """A class on Grades: its name, its details' address when the term is on record, its term
    grade when a current report supplied one, and its ID."""

    name: str
    link: str | None
    reported: bool
    summary: TermSummary | None
    class_id: str


def class_reads(
    store: GradebookRecords, term: Term, base: str, zone: tzinfo
) -> tuple[list[ClassRow], dict[str, ClassTermRead]]:
    """Each class of the open term's year, by name, with its term grade in that term, and each
    reported class's read of the term, by class ID: the same reads, made once."""
    year, label = term
    terms = store.terms_of_year(year)
    position = terms.index(label) + 1 if label in terms else None
    rows = []
    reads: dict[str, ClassTermRead] = {}
    for one in store.classes_in(year, label):
        link = None
        if position is not None:
            link = CLASS_AT.format(base=base, class_id=one.class_id, n=position)
        summary = None
        if one.reported:
            read = reads[one.class_id] = store.class_term(one.class_id, label)
            summary = term_summary(read.current, report_names(read.reports, zone))
        rows.append(ClassRow(one.name, link, one.reported, summary, one.class_id))
    return rows, reads


@dataclass(frozen=True)
class GradesRead:
    """What Grades reads for its open term: its class rows, and Check together's lists."""

    rows: list[ClassRow]
    to_check: GradesToCheck


def grades_read(store: ProjectStateStore, term: Term, base: str, zone: tzinfo) -> GradesRead:
    """Grades' reads of ``term`` in one hold of the record, in order: its class rows, unguarded;
    then, when a class of it holds a report, Check together's reads and lists, which a covered
    failure leaves unknown with one record. Nothing else is caught."""
    with store.reading():
        rows, reads = class_reads(store, term, base, zone)
        if not reads:
            return GradesRead(rows, GradesToCheck({}, False))
        try:
            links = read_links(store)
            with store.strict_text():
                mapping = store.homework_classes(term[0])
            named = [link.assignment_id for link in links if link.confirmed]
            homework = store.homework_readings(named)
            ids = [one.assignment_id for one in homework.readable]
            # Her two chains for those homework and nothing else: never the school's reports or
            # the family's checks, so a row of theirs that can't be read never reaches Grades.
            updates = store.update_readings(ids)
            hand_ins = store.hand_in_readings(ids)
            to_check = grades_to_check(
                reads, links, mapping, homework, lambda one: account_read(updates, hand_ins, one)
            )
        except GradeValueUndecodable as error:
            checks_left_out(error)
            return GradesRead(rows, GradesToCheck({}, True))
    return GradesRead(rows, to_check)


def grades_page(
    request: Request, state: ApplicationState, *, said: str | None = None, status_code: int = 200
) -> Response:
    """Grades for whoever reads: the term the viewer chose, else the current one, each class's
    term grade and homework to check together in it, the chooser, and a parent's controls in a
    parent's voice. A read alone."""
    store = state.project_state
    contexts = store.grade_contexts()
    view = store.view_of(cast(Viewer, viewer_of(request)))
    open_term = view or contexts.current
    family = parent_reads(request)
    base = GRADES if family else HER_GRADES
    terms = list(contexts.terms)
    if contexts.current is not None and contexts.current not in terms:
        terms.append(contexts.current)
    read = (
        grades_read(store, open_term, base, state.clock.zone)
        if open_term
        else GradesRead([], GradesToCheck({}, False))
    )
    rows = read.rows
    make_current = None
    if (
        family
        and open_term is not None
        and contexts.current is not None
        and open_term != contexts.current
        and open_term in contexts.terms
    ):
        make_current = {
            "shown": term_sent(contexts.current),
            "term": term_sent(open_term),
            "named": term_named(open_term),
        }
    showing = view is not None and contexts.current is not None and view != contexts.current
    chooser = []
    if len(terms) > 1:
        chooser = [(term_sent(term), term_named(term), term == view) for term in terms]
    return templates.TemplateResponse(
        request,
        "grades.html",
        {
            "family_tree": request.url.path.startswith(GRADES),
            "family": family,
            "said": said,
            "open_term": term_named(open_term) if open_term else None,
            "rows": rows,
            "to_check": read.to_check,
            "reported": any(row.reported for row in rows),
            "chooser": chooser,
            "following": view is None,
            "view_action": f"{base}/view",
            "showing": showing,
            "current_term": term_named(contexts.current) if contexts.current else None,
            "make_current": make_current,
            "add": ADD,
            "homework_classes": HOMEWORK_CLASSES,
            "current_action": CURRENT_TERM,
            "follow": FOLLOW_CURRENT,
            "no_reports": NO_REPORTS_YET,
            "add_one": ADD_ONE,
            "no_report_added": NO_REPORT_ADDED,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


def unshown(
    request: Request,
    state: ApplicationState,
    error: sqlite3.Error,
    *,
    heading: str,
    said: str,
    again: str | None,
    ways_back: list[ReturnLink],
    status_code: int = 503,
) -> Response:
    """A grade page whose record can't be read as it is made: the page that reads no store,
    503, saying it can't be shown, with ``again`` to ask for it again and the ways back, in
    the tree it was asked for in, Grades marked in its menu."""
    family = request.url.path.startswith(GRADES)
    return unavailable_page(
        request,
        state,
        error,
        heading=heading,
        alert=said,
        again=again,
        ways_back=ways_back,
        family=family,
        nav="parent-grades" if family else "grades",
        status_code=status_code,
    )


TRY_AGAIN: Final = "Try again."
"""What a designed sentence says when it already asks for the press again."""


def outcome_unshown(
    request: Request,
    state: ApplicationState,
    error: sqlite3.Error,
    *,
    heading: str,
    said: str,
    unshown_said: str,
    again: str,
    ways_back: list[ReturnLink],
    status_code: int,
) -> Response:
    """A press's designed answer whose page can't be read: its own sentence and status, then
    what can't be shown, on the page that reads no store; "Try again" asks for the page again,
    by a read, unless the sentence already says to try again."""
    return unshown(
        request,
        state,
        error,
        heading=heading,
        said=f"{said} {unshown_said}",
        again=None if said.endswith(TRY_AGAIN) else again,
        ways_back=ways_back,
        status_code=status_code,
    )


def grades_answer(
    request: Request, state: ApplicationState, *, said: str, status_code: int
) -> Response:
    """Grades with a press's answer first, or that answer on the page that reads no store when
    Grades can't be read."""
    try:
        return grades_page(request, state, said=said, status_code=status_code)
    except sqlite3.Error as error:
        return outcome_unshown(
            request,
            state,
            error,
            heading="Grades",
            said=said,
            unshown_said=GRADES_UNSHOWN,
            again=GRADES if request.url.path.startswith(GRADES) else HER_GRADES,
            ways_back=[],
            status_code=status_code,
        )


def grades_shown(request: Request, state: ApplicationState) -> Response:
    """Grades, or the page saying it can't be shown when its record can't be read."""
    try:
        return grades_page(request, state)
    except sqlite3.Error as error:
        tree = GRADES if request.url.path.startswith(GRADES) else HER_GRADES
        return unshown(
            request,
            state,
            error,
            heading="Grades",
            said=GRADES_UNSHOWN,
            again=asked_address(tree, request.scope["query_string"]),
            ways_back=[],
        )


@student_router.get("", response_class=HTMLResponse)
def her_grades(request: Request, state: State) -> Response:
    """Route 5: Grades, in the voice of whoever reads."""
    return grades_shown(request, state)


@family_router.get("", response_class=HTMLResponse)
def family_grades(request: Request, state: State) -> Response:
    """Route 5p: Grades in a parent's voice."""
    return grades_shown(request, state)


async def chosen_view(request: Request, state: ApplicationState, base: str) -> Response:
    """A viewer's term for Grades, kept as their own row: a term on record, or the current one
    followed. Anything else is refused and nothing is written."""
    fields, whole = await fields_of(request, VIEW_FIELDS)
    value = fields.get("view", "")
    view = None if value == FOLLOW_CURRENT else term_posted(value)
    if not whole or (value != FOLLOW_CURRENT and view is None):
        return await asyncio.to_thread(
            grades_answer, request, state, said=TERM_NOT_ON_RECORD, status_code=422
        )
    try:
        async with state.decision_lock:
            await worker_to_the_end(
                partial(state.project_state.choose_view, cast(Viewer, viewer_of(request)), view)
            )
    except sqlite3.Error as error:
        busy = busy_file(error)
        logger.warning("The term shown may not have changed: %s", type(error).__name__)
        said = TERM_SHOWN_NOT_CHANGED if busy else TERM_SHOWN_UNKNOWN
        return await asyncio.to_thread(grades_answer, request, state, said=said, status_code=500)
    except ViewNotOnRecord:
        return await asyncio.to_thread(
            grades_answer, request, state, said=TERM_NOT_ON_RECORD, status_code=422
        )
    return RedirectResponse(base, status_code=303)


TERM_SHOWN_NOT_CHANGED: Final = "The term shown wasn't changed. Try again."
TERM_SHOWN_UNKNOWN: Final = "Blossom couldn't tell whether the term shown changed."


@student_router.post("/view", response_class=HTMLResponse)
async def her_view(request: Request, state: State) -> Response:
    """Route 6: the reader's term for Grades, from her pages."""
    return await chosen_view(request, state, HER_GRADES)


@family_router.post("/view", response_class=HTMLResponse)
async def family_view(request: Request, state: State) -> Response:
    """Route 7: the reader's term for Grades, from the family's pages."""
    return await chosen_view(request, state, GRADES)


@family_router.post("/current-term", response_class=HTMLResponse)
async def current_term(request: Request, state: State) -> Response:
    """Route 8: a parent's choice of the current term, applied only while the term the page
    showed as current still is. Every remembered term stays as it was."""
    fields, whole = await fields_of(request, CONTEXT_FIELDS)
    shown = term_posted(fields.get("shown", ""))
    chosen = term_posted(fields.get("term", ""))
    page = partial(grades_answer, request, state)
    if not whole or shown is None or chosen is None:
        return await asyncio.to_thread(page, said=TERM_NOT_ON_RECORD, status_code=422)
    try:
        async with state.decision_lock:
            outcome = await worker_to_the_end(
                partial(state.project_state.set_current_context, shown, chosen, role_of(state))
            )
    except TermNotOnRecord:
        return await asyncio.to_thread(page, said=TERM_NOT_ON_RECORD, status_code=422)
    except GradeTransactionLost as error:
        logger.warning("The current term may not have been set: %s", type(error).__name__)
        return await asyncio.to_thread(page, said=CONTEXT_UNKNOWN, status_code=500)
    except GradeReportNotSaved as error:
        logger.warning("The current term was not set: %s", type(error).__name__)
        return await asyncio.to_thread(page, said=NOT_SAVED, status_code=500)
    if isinstance(outcome, ContextChanged):
        return await asyncio.to_thread(page, said=CONTEXT_CHANGED, status_code=409)
    if isinstance(outcome, ContextNotOnRecord):
        return await asyncio.to_thread(page, said=TERM_NOT_ON_RECORD, status_code=422)
    return RedirectResponse(GRADES, status_code=303)


@dataclass(frozen=True)
class HomeworkName:
    """A homework class name on the page: its folded key, the name as written, the answer on
    record as its form sends it back, the class it is, None while it waits, by a parent's
    removal too, and its state."""

    key: str
    written: str
    shown: str
    class_id: str | None
    state: str


def class_labels(classes: tuple[NamedClass, ...]) -> dict[str, str]:
    """Each class's name as the page writes it: its official name, and, where two classes of
    the year share one but for capitalization and spaces, its earliest alias's class code after
    it, with that alias's class name too where those classes share the code as well."""
    labels = {}
    for one in classes:
        same = [other for other in classes if name_key(other.name) == name_key(one.name)]
        if len(same) == 1 or not one.aliases:
            labels[one.class_id] = one.name
            continue
        first = one.aliases[0]
        shared = any(
            other is not one
            and other.aliases
            and name_key(other.aliases[0].code) == name_key(first.code)
            for other in same
        )
        code = f"{first.code}, {first.name}" if shared and first.name else first.code
        labels[one.class_id] = f"{one.name} ({code})"
    return labels


def names_on_record(courses: Iterable[str]) -> list[tuple[str, str]]:
    """Each class name homework carries, once per folded form and in the order of that form,
    written as the spelling that sorts first among the ones on record."""
    spellings: dict[str, str] = {}
    for course in courses:
        key = name_key(course)
        if key and (key not in spellings or course < spellings[key]):
            spellings[key] = course
    return sorted(spellings.items())


def homework_classes_page(
    request: Request,
    state: ApplicationState,
    *,
    said: str | None = None,
    at: str | None = None,
    status_code: int = 200,
) -> Response:
    """Homework class names for a parent: each name the homework on record carries, with the
    class of the current school year it is and one chooser, which on a name with a class also
    offers to remove the connection, and how many names wait. With no class to choose, every
    name waits and none has a chooser. ``said`` is shown at the name whose key is ``at``, or
    first on the page when no name listed has it. A read alone."""
    store = state.project_state
    current = store.grade_contexts().current
    mapping = None if current is None else store.homework_classes(current[0])
    labels = {} if mapping is None else class_labels(mapping.classes)
    answers = {} if mapping is None else mapping.answers
    options = sorted(
        ((class_id, label) for class_id, label in labels.items()),
        key=lambda option: (name_key(option[1]), option[0]),
    )
    names: list[HomeworkName] = []
    for key, written in names_on_record(one.course for one in store.all_assignments()):
        answered = answers.get(key)
        shown = (answered or UNCONNECTED) if key in answers else UNANSWERED
        class_id = None if mapping is None else class_for(written, mapping)
        if class_id is None:
            words = NOT_CONNECTED
        elif class_id == answered:
            words = CONNECTED_TO.format(name=labels[class_id])
        else:
            words = MATCHED_BY_NAME.format(name=labels[class_id])
        names.append(HomeworkName(key, written, shown, class_id, words))
    waiting = sum(1 for name in names if name.class_id is None)
    places = [place for place, name in enumerate(names, start=1) if name.key == at]
    return templates.TemplateResponse(
        request,
        "grade_homework_classes.html",
        {
            "said": said,
            "said_at": places[0] if said is not None and places else None,
            "year": current[0] if current is not None else "",
            "names": names,
            "options": options,
            "waiting": None
            if not waiting
            else ONE_NAME_WAITS
            if waiting == 1
            else NAMES_WAIT.format(n=waiting),
            "introduction": HOMEWORK_NAMES_INTRODUCTION,
            "classes_come": CLASSES_COME_FROM_REPORTS,
            "add_a_report": ADD_A_REPORT_TO_CONNECT,
            "action": HOMEWORK_CLASSES,
            "unconnected": UNCONNECTED,
            "remove_connection": REMOVE_CONNECTION,
            "no_reports": NO_REPORTS_YET,
            "add_one": ADD_ONE,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@family_router.get("/homework-classes", response_class=HTMLResponse)
def homework_class_names(request: Request, state: State) -> Response:
    """Route 10: Homework class names, a parent's page, which reads nothing from its address."""
    try:
        return homework_classes_page(request, state)
    except sqlite3.Error as error:
        return unshown(
            request,
            state,
            error,
            heading=HOMEWORK_CLASS_NAMES,
            said=HOMEWORK_NAMES_UNSHOWN,
            again=asked_address(HOMEWORK_CLASSES, request.scope["query_string"]),
            ways_back=[],
        )


ANSWER_SAVED: Final = "Your answer for {name} was saved."


def homework_classes_answer(
    request: Request, state: ApplicationState, name: str, *, said: str, status_code: int
) -> Response:
    """Homework class names with a press's answer first at the name it was about, or that
    answer on the page that reads no store when the names can't be read."""
    try:
        return homework_classes_page(
            request, state, said=said, at=name_key(name), status_code=status_code
        )
    except sqlite3.Error as error:
        return outcome_unshown(
            request,
            state,
            error,
            heading=HOMEWORK_CLASS_NAMES,
            said=said,
            unshown_said=HOMEWORK_NAMES_UNSHOWN,
            again=HOMEWORK_CLASSES,
            ways_back=[],
            status_code=status_code,
        )


@family_router.post("/homework-classes", response_class=HTMLResponse)
async def connect_homework_class(request: Request, state: State) -> Response:
    """Route 11: a parent's answer that one homework class name is one class of the current
    school year, or the removal of its connection, applied only while the page's school year
    is the current one and the answer the page showed is the one on record, both decided by
    the record inside its write. A save returns to its own name's heading; a refusal writes
    nothing."""
    fields, whole = await fields_of(request, NAME_FIELDS)
    year, name = fields.get("year", ""), fields.get("name", "")
    shown, chosen = fields.get("shown", ""), fields.get("class", "")
    refused = partial(homework_classes_answer, request, state, name)
    if (
        not whole
        or not is_school_year(year)
        or (chosen != UNCONNECTED and CLASS_ID.fullmatch(chosen) is None)
        or (shown not in (UNANSWERED, UNCONNECTED) and CLASS_ID.fullmatch(shown) is None)
    ):
        return await asyncio.to_thread(refused, said=CHOOSE_A_CLASS, status_code=422)
    store = state.project_state
    try:
        async with state.decision_lock:
            outcome = await worker_to_the_end(
                partial(
                    store.connect_homework_class,
                    year,
                    name,
                    shown=None if shown == UNANSWERED else shown,
                    chosen=chosen,
                    role=role_of(state),
                )
            )
    except HomeworkClassNotOnRecord:
        return await asyncio.to_thread(refused, said=CHOOSE_A_CLASS, status_code=422)
    except GradeTransactionLost as error:
        logger.warning("A homework class name may not be connected: %s", type(error).__name__)
        return await asyncio.to_thread(refused, said=SAVE_UNKNOWN, status_code=500)
    except GradeReportNotSaved as error:
        logger.warning("A homework class name was not connected: %s", type(error).__name__)
        return await asyncio.to_thread(refused, said=NOT_SAVED, status_code=500)
    if isinstance(outcome, HomeworkClassChanged | HomeworkYearNotCurrent):
        return await asyncio.to_thread(refused, said=NAME_CHANGED, status_code=409)
    try:
        assignments = await asyncio.to_thread(store.all_assignments)
    except sqlite3.Error as error:
        return unavailable_page(
            request,
            state,
            error,
            heading=HOMEWORK_CLASS_NAMES,
            alert=f"{ANSWER_SAVED.format(name=name)} {HOMEWORK_NAMES_UNSHOWN}",
            again=HOMEWORK_CLASSES,
            ways_back=[],
            family=True,
            nav="parent-grades",
        )
    listed = [key for key, _ in names_on_record(one.course for one in assignments)]
    key = name_key(name)
    address = NAME_AT.format(n=listed.index(key) + 1) if key in listed else HOMEWORK_CLASSES
    return RedirectResponse(address, status_code=303)


@dataclass(frozen=True)
class ResultShown:
    """A result on class details: its title and category, whether the category is one the
    school reported, its score as reported, its due date, the school's status for it or the
    words for its presence, where it was last shown, and its school record details."""

    title: str
    category: str
    category_reported: bool
    score: str
    due: str
    status: str
    showing: list[str]
    record: list[str]
    cues: list[str]


def due_of(value: CurrentValue, resolved: date | None, names: dict[str, str]) -> str:
    """A result's due date: the day, resolved under the month on record, or as written."""
    if resolved is not None:
        due = f"Due {spoken_date(resolved)}"
    elif value.due is None:
        due = "Due date not captured"
    elif value.due.cell[0] is Presence.REPORTED:
        due = f"Due date shown: {value.due.cell[1]}"
    else:
        due = presence_words("Due date", value.due.cell)
    if value.due is not None and value.due.report.report_id != value.report_id:
        due += f", from the {names.get(value.due.report.report_id, 'report')}"
    return due


def result_shown(
    value: CurrentValue,
    *,
    year: str,
    first_month: int | None,
    newest: ClassReport | None,
    names: dict[str, str],
) -> tuple[date | None, ResultShown]:
    """A result's lines for class details, with its due date when it resolves under the month
    on record."""
    cells = value.cells
    resolved = None if value.due is None else due_date_of(value.due.cell, year, first_month)
    showing = []
    last = value.last_shown
    if last is not None and newest is not None and last.report_id != newest.report_id:
        showing.append(f"Last shown in the {names.get(last.report_id, 'report')}")
    if value.not_shown is not None:
        showing.append(f"Not shown in the {names.get(value.not_shown.report_id, 'report')}")
    record = [f"{label}: {record_text(cells[field])}" for field, label in RECORD_FIELDS]
    cues = []
    if any(
        cells[field][0] is Presence.REPORTED and adjusts(cells[field][1]) for field in ADJUSTING
    ):
        cues.append("Adjusted")
    if cells["note"][0] is Presence.REPORTED and cells["note"][1].strip():
        cues.append("Teacher's note")
    status = cells["status"]
    return resolved, ResultShown(
        title=cell_text(cells["assignment"], "Title"),
        category=cell_text(cells["category"], "Category"),
        category_reported=cells["category"][0] is Presence.REPORTED,
        score=score_line(value, names),
        due=due_of(value, resolved, names),
        status=(
            f"Gradebook status: {status[1]}"
            if status[0] is Presence.REPORTED
            else presence_words("Gradebook status", status)
        ),
        showing=showing,
        record=record,
        cues=cues,
    )


def category_line(value: CurrentValue) -> str:
    """A category as the school reported it: its name, weight and average, each as written or
    in the words for the presence its cell holds."""
    name, weight, average = (value.cells[field] for field in ("name", "weight", "average"))
    said = [
        f"{what} {cell[1]}" if cell[0] is Presence.REPORTED else presence_words(what, cell)
        for what, cell in (("weight", weight), ("average", average))
    ]
    return f"{cell_text(name, 'Category name')}: {', '.join(said)}"


class Said(NamedTuple):
    """A sentence a page says, whether it holds text the school or the family wrote, and a
    school-year range in it, by its place in the text, that a line keeps together if it can."""

    text: str
    authored: bool
    keep: str = ""
    keep_at: int = 0


@dataclass(frozen=True)
class WayIn:
    """A parent's way from a result's card on class details to its homework page: the address,
    the link's accessible name, the homework a link names, when one does, and the line saying
    the result waits for a parent's choice, when it does."""

    address: str
    label: str
    linked: Said | None
    waits: str | None = None


def waiting_way(way: WayIn) -> WayIn:
    """A result's way in when it waits for a parent's choice: the line on its card, and the
    same words at the end of the link's accessible name."""
    return replace(way, label=f"{way.label}. {NOT_MATCHED_ALONE}", waits=NOT_MATCHED_ALONE)


def unchecked_way(way: WayIn) -> WayIn:
    """A result's way in when who waits couldn't be worked out: the link's accessible name with
    a note saying so at its end, and no line on the card."""
    return replace(way, label=f"{way.label}. {NOT_CHECKED_ALONE}")


def ways_in(
    store: ProjectStateStore, links: Iterable[Link], shown: Iterable[tuple[str, ResultShown]]
) -> dict[str, WayIn]:
    """Each result's way to its homework page, by result ID, with the homework its link names
    read for its title alone."""
    named = {one.result_id: one.assignment_id for one in links if one.confirmed}
    ways = {}
    for result_id, item in shown:
        linked = None
        if result_id in named:
            homework = store.one_assignment(named[result_id])
            linked = (
                Said(LINKED_OFF_RECORD, authored=False)
                if homework is None
                else Said(LINKED_TO_HOMEWORK.format(title=homework.title), authored=True)
            )
        ways[result_id] = WayIn(
            address=RESULT_HOMEWORK_AT.format(result_id=result_id),
            label=f"{HOMEWORK_FOR_RESULT}: {item.title}",
            linked=linked,
        )
    return ways


def class_page(request: Request, state: ApplicationState, class_id: str, n: str) -> Response:
    """Class details of her class in the term at place ``n`` of its year: the term grade, each
    result by due date, newest first, the dates not confirmed apart, and the term's reports. A
    read alone; a class or place not on record is a page saying so. Rendered after any hold."""
    family = parent_reads(request)
    context: dict[str, object] = {
        "family_tree": request.url.path.startswith(GRADES),
        "grades": GRADES if family else HER_GRADES,
        "ask_at": None if family else RESULT_ASK,
        "marks": state.settings.page_marks,
    }
    try:
        shown = class_page_read(state, class_id, n, family=family)
    except sqlite3.Error as error:
        # The class's ID and place are read before either is checked, so each is escaped.
        tree = GRADES if request.url.path.startswith(GRADES) else HER_GRADES
        asked = CLASS_AT.format(base=tree, class_id=segment(class_id), n=segment(n))
        return unshown(
            request,
            state,
            error,
            heading=CLASS_DETAILS,
            said=CLASS_UNSHOWN,
            again=asked_address(asked, request.scope["query_string"]),
            ways_back=[ReturnLink(GRADES if family else HER_GRADES, BACK_TO_GRADES)],
        )
    if isinstance(shown, GradeValueUndecodable):
        return grade_page_not_shown(shown)
    if shown is None:
        return templates.TemplateResponse(
            request,
            "grade_class.html",
            {**context, "missing": CLASS_NOT_ON_RECORD},
            status_code=404,
        )
    return templates.TemplateResponse(request, "grade_class.html", {**context, **shown})


def class_page_read(
    state: ApplicationState, class_id: str, n: str, *, family: bool
) -> dict[str, object] | GradeValueUndecodable | None:
    """What class details shows, read alone; for a parent reading, in one hold, with each
    result's way to its homework page read after the page's own values, then which results
    wait for a choice, or that it couldn't be worked out. None for a class or place not on
    record; the error itself when her links, or the rules' own check of them, stop a parent's
    page."""
    store = state.project_state
    with store.reading() if family else nullcontext():
        named = store.class_named(class_id)
        terms = store.terms_of_year(named.year) if named is not None else ()
        if named is None or not POSITION.fullmatch(n) or int(n) > len(terms):
            return None
        term = terms[int(n) - 1]
        read = store.class_term(class_id, term)
        reports, values = read.reports, read.current
        names = report_names(reports, state.clock.zone)
        newest = newest_current(reports)
        first_month = store.first_month_of(named.year)
        dated: list[tuple[date, str, str, ResultShown]] = []
        undated: list[tuple[str, ResultShown]] = []
        for result_id, value in values.results.items():
            resolved, shown = result_shown(
                value, year=named.year, first_month=first_month, newest=newest, names=names
            )
            if resolved is None:
                undated.append((result_id, shown))
            else:
                dated.append((resolved, folded(shown.title).casefold(), result_id, shown))
        dated.sort(key=lambda item: (-item[0].toordinal(), item[1]))
        undated.sort(key=lambda item: folded(item[1].title).casefold())
        uses = {report.report_id: report.use for report in reports}
        captures: dict[str, ClassReport] = {}
        for report in reports:
            captures.setdefault(report.latest_of_capture, report)
        summary = term_summary(values, names)
        categories = [category_line(value) for value in values.categories.values()]
        listed = [
            (
                as_line(names.get(first.report_id, "report")),
                "Current" if uses[latest] == "current" else "Kept as an earlier report",
            )
            for latest, first in captures.items()
        ]
        cards = [(result_id, shown) for _, _, result_id, shown in dated]
        ways: dict[str, WayIn] = {}
        unchecked: str | None = None
        if family and values.results:
            # Her links are read only once the page's own values are all made.
            try:
                links = read_links(store)
            except GradeValueUndecodable as error:
                return error
            ways = ways_in(store, links, [*cards, *undated])
            # Who waits is worked out last, from her links as read above and no link statement.
            # The rules' own check of those links comes first, so a failed read hides no conflict.
            try:
                linked_results(links)
                everyone = store.all_assignments()
                years = read_grade_facts(store, (one.course for one in everyone))
                claims = (
                    store.read_claims([one.assignment_id for one in everyone])
                    if results_read(years)
                    else None
                )
                rules = rule_values(
                    everyone,
                    {} if claims is None else claims.records,
                    () if claims is None else claims.unreadable,
                    years,
                )
                waiting = waiting_in(class_id, term, rules, links=links)
            except HomeworkLinksConflict as error:
                return error
            except GradeValueUndecodable as error:
                # Only this read is left out: the page shows, and says who waits wasn't checked.
                cues_left_out(error)
                unchecked = CUES_NOT_CHECKED
                ways = {result_id: unchecked_way(way) for result_id, way in ways.items()}
            else:
                ways = {
                    result_id: waiting_way(way) if result_id in waiting else way
                    for result_id, way in ways.items()
                }
    return {
        "missing": None,
        "name": named.name,
        "term": term_named((named.year, term)),
        "reported": bool(reports),
        "summary": summary,
        "no_report_added": NO_REPORT_ADDED,
        # Each result with its ID, which its title carries so a link can land on it.
        "dated": cards,
        "undated": undated,
        "ways": ways,
        "unchecked": unchecked,
        "categories": categories,
        "reports": listed,
    }


@student_router.get("/classes/{class_id}/terms/{n}", response_class=HTMLResponse)
def her_class(class_id: str, n: str, request: Request, state: State) -> Response:
    """Route 9: class details, in the voice of whoever reads."""
    return class_page(request, state, class_id, n)


@family_router.get("/classes/{class_id}/terms/{n}", response_class=HTMLResponse)
def family_class(class_id: str, n: str, request: Request, state: State) -> Response:
    """Route 9p: class details in a parent's voice."""
    return class_page(request, state, class_id, n)


RESULT_ASK: Final = HER_GRADES + "/results/{result_id}/ask"
"""Her page that asks a parent about one result: its address holds the result's ID alone."""
ASK_TITLED: Final = "Can you help me understand my result for {title} in {class_name}?"
ASK_UNTITLED: Final = "Can you help me understand this result in {class_name}?"
ASK_PLAIN: Final = "Can you help me understand this result?"


def asked_about(title: str | None, class_name: str) -> str:
    """The sentence her field starts with: the result's title and class, or the class alone
    when there is no title to show or the sentence with it is longer than a request keeps, or
    neither when that is longer too."""
    if title is not None:
        sentence = ASK_TITLED.format(title=title, class_name=class_name)
        if len(sentence) <= NOTE_MAX_LENGTH:
            return sentence
    sentence = ASK_UNTITLED.format(class_name=class_name)
    return sentence if len(sentence) <= NOTE_MAX_LENGTH else ASK_PLAIN


def result_ask_read(state: ApplicationState, result_id: str) -> dict[str, object] | None:
    """What the page that asks about a result shows: the result's card as class details draws
    it, under its class name and term, the way back to its card, and the sentence the field
    starts with. None for an ID of no such shape or no current result of hers. A read."""
    store = state.project_state
    if not RESULT_ID.fullmatch(result_id):
        return None
    with store.reading():
        own = result_own(store, result_id)
        if own is None:
            return None
        place, named, read, value, _ = own
        term = store.terms_of_year(named.year)[own.term_place - 1]
        _, shown = result_shown(
            value,
            year=named.year,
            first_month=store.first_month_of(named.year),
            newest=newest_current(read.reports),
            names=report_names(read.reports, state.clock.zone),
        )
    titled = value.cells["assignment"][0] is Presence.REPORTED
    asked = asked_about(shown.title if titled else None, named.name)
    at = {"class_id": place.class_id, "n": own.term_place}
    return {
        "name": named.name,
        "term": term_named((named.year, term)),
        "result_id": result_id,
        "item": shown,
        "her_back": address(CLASS_AT.format(base=HER_GRADES, **at), result_id),
        "family_back": address(CLASS_AT.format(base=GRADES, **at), result_id),
        "asked": asked,
    }


def result_ask_page(
    request: Request,
    state: ApplicationState,
    shown: dict[str, object],
    *,
    question: str | None = None,
    problem: str | None = None,
    question_error: bool = False,
    status_code: int = 200,
) -> Response:
    """The page that asks a parent about a result, from what ``result_ask_read`` gave, with her
    words in the field when a refusal keeps them, or the sentence it starts with. Its ways back
    go to the reader's own class details. Rendering it sends nothing, and each time it is made
    its form gets a fresh id."""
    return templates.TemplateResponse(
        request,
        "student_result_help.html",
        {
            **shown,
            "back": shown["family_back"] if parent_reads(request) else shown["her_back"],
            "question": shown["asked"] if question is None else question,
            "problem": problem,
            "question_error": question_error,
            "viewer": viewer_of(request),
            "not_hers": NOT_HERS_TO_UPDATE,
            "action": RESULT_ASK.format(result_id=shown["result_id"]),
            "note_max_length": NOTE_MAX_LENGTH,
            "request_id": new_request_id(),
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


def result_help_not_sent(
    request: Request,
    state: ApplicationState,
    problem: str,
    question: str,
    status_code: int,
    *,
    before: AskOutcome | None = None,
    fresh: bool = False,
) -> Response:
    """The page for a request about a result that was refused where no result can be shown
    beside it. It reads no store: what happened, her question as she typed it, and the ways
    back, to the reader's own Grades. A form whose id already asked, with other words or for
    a request since taken back, says that with 409 and links to the help form on her week;
    so does a form refused beside a result that can't be read (``fresh``). A parent is shown
    no question."""
    mine = viewer_of(request) != "parent"
    week = "Back to my week" if mine else "Back to her week"
    fresh_form = address(WEEK_PAGE, "ask-for-help") if fresh else None
    if isinstance(before, HelpFormChanged | HelpFormUsed):
        problem = FORM_USED if isinstance(before, HelpFormUsed) else FORM_SENT_OTHER_WORDS
        status_code = 409
        fresh_form = address(WEEK_PAGE, "ask-for-help")
    grades = GRADES if parent_reads(request) else HER_GRADES
    return templates.TemplateResponse(
        request,
        "student_update_recovery.html",
        {
            "card": None,
            "hand_in_card": None,
            "heading": "Request not sent",
            "note_problem": problem,
            "help_question": question if mine else "",
            "help_fresh_form": fresh_form,
            "ways_back": [ReturnLink(grades, "Back to Grades"), ReturnLink(WEEK_PAGE, week)],
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@student_router.get("/results/{result_id}/ask", response_class=HTMLResponse)
def ask_about_a_result(result_id: str, request: Request, state: State) -> Response:
    """Her page that offers to ask a parent about one result: the result as it stands, and a
    question she can change. Opening it sends nothing. A parent reading it sees no form."""
    try:
        shown = result_ask_read(state, result_id)
    except GradeValueUndecodable as error:
        return grade_page_not_shown(error)
    except sqlite3.Error as error:
        return unshown(
            request,
            state,
            error,
            heading=ASK_ABOUT_A_RESULT,
            said=RESULT_UNSHOWN,
            again=asked_address(
                RESULT_ASK.format(result_id=segment(result_id)), request.scope["query_string"]
            ),
            ways_back=[ReturnLink(HER_GRADES, BACK_TO_GRADES)],
        )
    if shown is None:
        return templates.TemplateResponse(
            request,
            "student_result_help.html",
            {
                "missing": RESULT_NOT_ON_RECORD,
                "grades": HER_GRADES,
                "marks": state.settings.page_marks,
            },
            status_code=404,
        )
    return result_ask_page(request, state, shown)


@student_router.post("/results/{result_id}/ask", response_class=HTMLResponse)
async def ask_a_parent_about_a_result(result_id: str, request: Request, state: State) -> Response:
    """Ask a parent about one result, with her words as she sent them, each line break kept as
    one, so they are counted against the cap as her field counts them.

    Only this, an explicit submit from her, makes a request. It carries the result's id and
    her words, and nothing else of the result. A parent is answered 403 before the form is
    read. Her words are read before the result is, so no refusal loses them. The same form
    sent again lands on the request it made; one that already asked with other words, or
    whose request was taken back or has gone, sends nothing, 409. A form that isn't whole,
    or words too long, is refused first, 422. A result with no current value when it is read,
    or deleted before the request is written, is 404 on the page that needs no result. One
    that stops being current after it is read is asked about all the same, and the request's
    line then says it is not on record. A write the file refuses is 500, on the page she sent
    it from.
    """
    if viewer_of(request) == "parent":
        return result_help_not_sent(request, state, NOT_HERS_TO_ASK, "", 403)
    fields, whole = await fields_of(request, HELP_FIELDS)
    question = fields.get("note", "")
    # A browser counts a line break once against the field's limit and sends it as CR LF.
    words = normalize_note(question) or ""
    try:
        form: str | None = request_id_from(fields.get("request_id", ""))
    except NotARequestId:
        form = None
    sound = whole and form is not None
    refused = (
        None
        if sound and len(words) <= NOTE_MAX_LENGTH
        else (QUESTION_TOO_LONG if sound else HELP_FORM_NOT_WHOLE)
    )
    if not RESULT_ID.fullmatch(result_id):
        return result_help_not_sent(request, state, RESULT_NOT_ON_RECORD, question, 404)
    before: AskOutcome | None = None
    if form is not None and refused is None:
        try:
            before = await asyncio.to_thread(
                partial(
                    state.help_requests.already_asked,
                    form,
                    words or None,
                    grade_result_id=result_id,
                )
            )
        except Exception:
            logger.exception("the form for her request for help about a result was not read")
            return result_help_not_sent(request, state, HELP_NOT_ASKED, question, 500)
        if isinstance(before, HelpAlreadyAsked):
            return RedirectResponse(
                address(WEEK_PAGE, "help-result", asked_again=before.request.request_id),
                status_code=303,
            )
    try:
        shown = await asyncio.to_thread(result_ask_read, state, result_id)
    except Exception as error:
        # The type alone: a stored grade value can ride in the error's message or its cause.
        logger.warning(
            "a result could not be read for her request for help: %s", type(error).__name__
        )
        if refused is not None:
            # The form is refused before the result is weighed, and says so first.
            return result_help_not_sent(
                request, state, HELP_WITHOUT_THE_PAGE[refused], question, 422, fresh=True
            )
        return result_help_not_sent(request, state, HELP_NOT_ASKED, question, 500, before=before)
    if shown is None:
        return result_help_not_sent(
            request, state, RESULT_NOT_ON_RECORD, question, 404, before=before
        )
    if form is None or refused is not None:
        return result_ask_page(
            request,
            state,
            shown,
            question=question,
            problem=refused,
            question_error=sound,
            status_code=422,
        )
    if isinstance(before, HelpFormChanged | HelpFormUsed):
        said = FORM_USED if isinstance(before, HelpFormUsed) else FORM_SENT_OTHER_WORDS
        return result_ask_page(
            request, state, shown, question=question, problem=said, status_code=409
        )
    asking = form

    def once() -> AskOutcome:
        return state.help_requests.ask_once(
            asking, state.clock.today(), words or None, grade_result_id=result_id
        )

    try:
        async with state.decision_lock:
            outcome = await worker_to_the_end(once)
    except UnknownResultReference:
        return result_help_not_sent(request, state, RESULT_NOT_ON_RECORD, question, 404)
    except Exception:
        logger.exception("her request for help about a result could not be sent")
        return result_ask_page(
            request, state, shown, question=question, problem=HELP_NOT_ASKED, status_code=500
        )
    match outcome:
        case HelpAsked(request=asked):
            where = address(WEEK_PAGE, "help-result", asked=asked.request_id)
        case HelpAlreadyAsked(request=asked):
            where = address(WEEK_PAGE, "help-result", asked_again=asked.request_id)
        case HelpFormChanged() | HelpFormUsed():
            said = FORM_USED if isinstance(outcome, HelpFormUsed) else FORM_SENT_OTHER_WORDS
            return result_ask_page(
                request, state, shown, question=question, problem=said, status_code=409
            )
    return RedirectResponse(where, status_code=303)


STATE_LINE: Final = "state"
"""The ID of a result's page's line that says whether it is linked, matched or neither."""
RESULT_NOT_ON_RECORD: Final = "Blossom has no current record of this result."
LINKS_CHANGED: Final = (
    "Grades or links for this class changed while you were choosing. Nothing was saved. "
    "Check this page again."
)
NAME_CONNECTED_DIFFERENTLY: Final = (
    "That homework's class name is connected differently now. Nothing was saved. "
    "Check this page again."
)
HOMEWORK_NOT_ON_RECORD: Final = "That homework is not on record now. Nothing was saved."
CHOOSE_HOMEWORK: Final = "Choose homework. Nothing was saved."
PRESS_UNKNOWN: Final = (
    "Blossom couldn't tell whether this was saved. Pressing the button again is safe."
)
INDEX_UNAVAILABLE: Final = (
    "a homework link wasn't written: the required homework-links index is unavailable"
)
"""The one record of a link write refused while the required index is unavailable."""
DECISION_FIELDS: Final = frozenset({"homework", "decision", "revision"})
A_REVISION: Final = re.compile(r"[0-9]{1,19}")
"""A revision as a decision's form sends it: digits alone."""
HOMEWORK_FOR_RESULT: Final = "Homework for this result"
LINKED_TO_HOMEWORK: Final = "Linked to homework: {title}."
LINKED_TO: Final = "Linked to {title}."
MATCHED_TO: Final = "Matched by class, title and due date to {title}."
NOT_LINKED: Final = "Not linked to homework."
LINKED_OFF_RECORD: Final = "Linked to homework that isn't on record any more."
KEPT_APART_FROM: Final = "Kept apart from {title}. Blossom won't match these two by itself."
KEPT_APART_OFF_RECORD: Final = "Kept apart from homework that isn't on record any more."
SCORE_NOT_SHOWN: Final = "The score isn't shown on her homework:"
WITHHELD_WORDS: Final = {
    Withheld.POINTS_BLANK: "the points cell was left blank.",
    Withheld.POINTS_UNREADABLE: "the points couldn't be read.",
    Withheld.POINTS_NOT_IN_COPY: "the points weren't in the copy.",
    Withheld.NOT_SHOWN: "the newest complete report doesn't show this result.",
    Withheld.NAME_NOT_CONNECTED: "{name} isn't connected to {class_name} now.",
    Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR: (
        "{name} isn't connected to {class_name}, a class of {year}. "
        "The Homework class names page changes connections for the current school year only."
    ),
}
"""Each reason a score stays off her homework, after ``SCORE_NOT_SHOWN``."""
NOT_MATCHED_BY_ITSELF: Final = "Blossom doesn't match these by itself:"
NOT_MATCHED_ALONE: Final = "Blossom doesn't match this result by itself."
CUES_NOT_CHECKED: Final = "Blossom couldn't check which results here need your choice. Some may."
NOT_CHECKED_ALONE: Final = "Blossom couldn't check whether this result needs your choice."
ASSIGNMENTS_COULD_BE: Final = "{n} assignments could be this result."
RESULTS_COULD_BE: Final = "{n} results could be this homework."
ANOTHER_RESULT: Final = (
    "This homework already has another linked school result. You can link this result too or "
    "keep them apart."
)
DATE_NOT_SETTLED: Final = "a due date isn't settled."
MOVES_LINK_FROM: Final = "Linking moves this result's link from {title}."
MOVES_LINK_OFF_RECORD: Final = (
    "Linking moves this result's link from homework that isn't on record any more."
)
LINK_HOMEWORK: Final = ("link", "Link to this homework")
NOT_THIS_HOMEWORK: Final = ("apart", "Not this homework")
REMOVE_LINK: Final = ("apart", "Remove link")
MATCH_AGAIN: Final = ("automatic", "Let Blossom match again")
"""Each button of a homework row: the decision its form sends, and its words."""
RESULT_SEARCH: Final = RESULT_HOMEWORK + "/search"
"""A parent's search of a result's homework: its address holds the result's ID alone."""
FOUND: Final = "found"
"""The ID of the heading over what a search found, where each search's answer lands."""
RESULT_SEARCH_AT: Final = f"{RESULT_SEARCH}#{FOUND}"
SEARCH_FIELDS: Final = frozenset({"words", "page"})
SEARCH_HEADING: Final = "Homework in {class_name}"
SEARCH_LABEL: Final = "Search homework"
SEARCH_HINT: Final = "Type a word or two from the title or the class name."
SEARCH_BUTTON: Final = "Search"
FOUND_HEADING: Final = "Homework found"
NO_SEARCH_WORDS: Final = "Type a word or two to search. Nothing was searched."
NOTHING_MATCHES: Final = (
    "No homework in {class_name} matches every word. Try fewer words, or other ones."
)
SHOWING: Final = "Showing {first} to {last} of {total}."
SEARCH_WORDS_REFUSED: Final = (
    "Search words are one line of up to 200 characters. Nothing was searched, and nothing was "
    "changed."
)
LINKED_TO_THIS: Final = "Linked to this result."
KEPT_APART_FROM_THIS: Final = "Kept apart from this result."
LINK_FOUND: Final = "Link to this homework: {title}, {course}, {due}"
"""A found row's button's accessible name, its due date as ``due_part`` words it."""
NO_SUCH_PAGE: Final = "There is no such page of results."
PREVIOUS_PAGE: Final = "Previous page"
NEXT_PAGE: Final = "Next page"
OTHER_RESULTS: Final = "Other results that could be the same homework:"
NOT_LISTED: Final = {
    NoList.WORDS_REFUSED: SEARCH_WORDS_REFUSED,
    NoList.NO_SUCH_PAGE: NO_SUCH_PAGE,
}
"""The explanation of each search answered 422, with no list."""


@dataclass(frozen=True)
class HomeworkShown:
    """One homework on a result's page: its place on the page, the element that names its row,
    its class name, title and due line as on record or none of them, what its row says, and
    each button as the decision its form sends with its words."""

    number: int
    label: str
    assignment_id: str
    course: str | None
    title: str | None
    due: str | None
    said: list[Said]
    buttons: list[tuple[str, str]]


def due_line(day: date | None) -> str:
    """A homework's due date on its row: "Due" with the weekday, month and day, or the words
    for no date."""
    return "Due date not recorded" if day is None else f"Due {day:%A, %B} {day.day}"


def row_shown(number: int, row: HomeworkRow, linked: HomeworkRow | None) -> HomeworkShown:
    """A homework row of a result's page: what it says, from the pair's decision and the link
    the result already has, and the buttons that decision leaves."""
    said: list[Said] = []
    buttons: list[tuple[str, str]]
    # A link to homework not on record is named by the page's own state line, said once.
    label = f"homework-{number}"
    if row.standing is PairStanding.LINKED:
        if not row.on_record:
            label = STATE_LINE
        buttons = [REMOVE_LINK]
    elif row.standing is PairStanding.APART:
        if row.title is None:
            said.append(Said(KEPT_APART_OFF_RECORD, authored=False))
        else:
            said.append(Said(KEPT_APART_FROM.format(title=row.title), authored=True))
        buttons = [LINK_HOMEWORK, MATCH_AGAIN] if row.on_record else [MATCH_AGAIN]
    else:
        buttons = [LINK_HOMEWORK, NOT_THIS_HOMEWORK]
    if row.standing is not PairStanding.LINKED and row.on_record:
        if row.moves_link and linked is not None:
            said.append(
                Said(MOVES_LINK_OFF_RECORD, authored=False)
                if linked.title is None
                else Said(MOVES_LINK_FROM.format(title=linked.title), authored=True)
            )
        if row.another_result:
            said.append(Said(ANOTHER_RESULT, authored=False))
    return HomeworkShown(
        number=number,
        label=label,
        assignment_id=row.assignment_id,
        course=row.course,
        title=row.title,
        due=due_line(row.due) if row.on_record else None,
        said=said,
        buttons=buttons,
    )


@dataclass(frozen=True)
class FoundShown:
    """One homework a search found, as its row shows it: its place among what was found, its
    class name, title and due line as on record, its one line of state if any, and its button's
    accessible name, none for the homework the result is linked to."""

    number: int
    assignment_id: str
    course: str
    title: str
    due: str
    said: Said | None
    link: str | None


@dataclass(frozen=True)
class SearchShown:
    """What a search's answer shows under its heading: the sentence for no list, or how much is
    shown, the line a link would move from, the rows, and the pages before and after."""

    nothing: Said | None
    showing: str | None
    moves: Said | None
    rows: list[FoundShown]
    previous: int | None
    next: int | None


FOUND_SAID: Final = {
    FoundState.LINKED: Said(LINKED_TO_THIS, authored=False),
    FoundState.APART: Said(KEPT_APART_FROM_THIS, authored=False),
    FoundState.ANOTHER_RESULT: Said(ANOTHER_RESULT, authored=False),
}
"""A found row's one line of state; a row with no decision and no other link says none."""


def due_part(day: date | None) -> str:
    """A found row's due date as its button's accessible name words it."""
    return "no due date" if day is None else f"due {day:%A, %B} {day.day}"


def search_shown(
    found: HomeworkFound | NoList, linked: HomeworkRow | None, class_name: str
) -> SearchShown | None:
    """What a search's answer shows under its heading, from what it found and the homework the
    result is linked to; None for a search answered with an explanation and no list."""
    if not isinstance(found, HomeworkFound):
        if found is not NoList.NO_WORDS:
            return None
        return SearchShown(Said(NO_SEARCH_WORDS, authored=False), None, None, [], None, None)
    page = found.page
    if not page.total:
        nothing = Said(NOTHING_MATCHES.format(class_name=class_name), authored=True)
        return SearchShown(nothing, None, None, [], None, None)
    start = (page.number - 1) * PAGE_SIZE
    rows = [
        FoundShown(
            number=start + place,
            assignment_id=row.item.assignment_id,
            course=row.item.course,
            title=row.item.title,
            due=due_line(row.item.due_date),
            said=FOUND_SAID.get(row.state),
            link=None
            if row.state is FoundState.LINKED
            else LINK_FOUND.format(
                title=row.item.title, course=row.item.course, due=due_part(row.item.due_date)
            ),
        )
        for place, row in enumerate(found.rows, start=1)
    ]
    moves = None
    # W19's first sentence, once above the rows, when a press on one of them would move a link.
    if linked is not None and any(one.link is not None for one in rows):
        moves = (
            Said(MOVES_LINK_OFF_RECORD, authored=False)
            if linked.title is None
            else Said(MOVES_LINK_FROM.format(title=linked.title), authored=True)
        )
    return SearchShown(
        nothing=None,
        showing=SHOWING.format(first=start + 1, last=start + len(rows), total=page.total),
        moves=moves,
        rows=rows,
        previous=page.previous,
        next=page.next,
    )


@dataclass(frozen=True)
class OtherShown:
    """Another result that could be the same homework, as a result's page lists it: the address
    of its page, its title and class name, its due line, and its term and year."""

    address: str
    title: str
    class_name: str
    due: str
    term: str
    year: str


def other_shown(one: OtherResult, names: dict[str, str]) -> OtherShown:
    """Another result as a result's page lists it, its due line as class details words it from
    its class and term's report names ``names``."""
    return OtherShown(
        address=RESULT_HOMEWORK_AT.format(result_id=one.result_id),
        title=title_of(one.value),
        class_name=one.class_name,
        due=due_of(one.value, one.day, names),
        term=one.term,
        year=one.year,
    )


def result_said(view: ResultHomework) -> tuple[Said, list[Said], list[Said]]:
    """What a result's page says of it: whether it is linked, matched or neither; each reason
    its score stays off her homework; and each reason the rules leave it waiting."""
    if view.linked is not None:
        state = (
            Said(LINKED_OFF_RECORD, authored=False)
            if view.linked.title is None
            else Said(LINKED_TO.format(title=view.linked.title), authored=True)
        )
    elif view.matched is not None:
        state = Said(MATCHED_TO.format(title=view.matched.title), authored=True)
    else:
        state = Said(NOT_LINKED, authored=False)
    name = None if view.linked is None else view.linked.course
    withheld = [
        Said(
            WITHHELD_WORDS[reason].format(name=name, class_name=view.class_name, year=view.year),
            authored=reason
            in (Withheld.NAME_NOT_CONNECTED, Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR),
            keep=view.year if reason is Withheld.NAME_NOT_CONNECTED_IN_ANOTHER_YEAR else "",
            # The range's place is where the sentence puts the year, whatever the names hold.
            keep_at=len(
                WITHHELD_WORDS[reason]
                .partition("{year}")[0]
                .format(name=name, class_name=view.class_name)
            ),
        )
        for reason in view.withheld
    ]
    waiting: list[Said] = []
    if view.waiting is not None:
        if view.waiting.assignments:
            waiting.append(Said(ASSIGNMENTS_COULD_BE.format(n=view.waiting.assignments), False))
        if view.waiting.results:
            waiting.append(Said(RESULTS_COULD_BE.format(n=view.waiting.results), False))
        if view.waiting.linked_elsewhere:
            waiting.append(Said(ANOTHER_RESULT, authored=False))
        if view.waiting.unsettled:
            waiting.append(Said(DATE_NOT_SETTLED, authored=False))
    return state, withheld, waiting


def result_page_read(
    state: ApplicationState, result_id: str, *, search: tuple[str, str] | None = None
) -> dict[str, object] | None:
    """What a result's page shows, read in one hold in class details' order: its own class and
    term with its lines, her links, then the rest of the score read; and what a ``search``, its
    words and page field, finds there. None for an ID of no such shape or no current result."""
    store = state.project_state
    if not RESULT_ID.fullmatch(result_id):
        return None
    with store.reading():
        own = result_own(store, result_id)
        if own is None:
            return None
        place, named, read, value, _ = own
        names = report_names(read.reports, state.clock.zone)
        _, shown = result_shown(
            value,
            year=named.year,
            first_month=store.first_month_of(named.year),
            newest=newest_current(read.reports),
            names=names,
        )
        links = read_links(store)
        everyone = store.all_assignments()
        years = read_grade_facts(store, (one.course for one in everyone))
        claims = (
            store.read_claims([one.assignment_id for one in everyone])
            if results_read(years)
            else None
        )
        current = store.grade_contexts().current
        rules = rule_values(
            everyone,
            {} if claims is None else claims.records,
            () if claims is None else claims.unreadable,
            years,
        )
        view = result_homework(
            named,
            place.term,
            result_id,
            value,
            rules,
            links=links,
            current_year=None if current is None else current[0],
        )
    others = other_results(place.class_id, place.term, result_id, rules, years)
    reads = {at: one for year in years for at, one in year.reads.items()}
    found = (
        None
        if search is None
        else homework_found(*search, place.class_id, result_id, rules, links=links)
    )
    state_said, withheld, waiting = result_said(view)
    rows: dict[str, HomeworkRow] = {}
    for row in (view.linked, *view.candidates, *view.apart):
        if row is not None:
            rows.setdefault(row.assignment_id, row)
    return {
        "missing": None,
        "back": CLASS_AT.format(base=GRADES, class_id=place.class_id, n=own.term_place),
        "class_name": named.name,
        "term_name": place.term,
        "year": named.year,
        "result_id": result_id,
        "item": shown,
        "state": state_said,
        "score_not_shown": SCORE_NOT_SHOWN,
        "withheld": withheld,
        "connect": HOMEWORK_CLASSES if Withheld.NAME_NOT_CONNECTED in view.withheld else None,
        "not_matched": NOT_MATCHED_BY_ITSELF,
        "waiting": waiting,
        "rows": [
            row_shown(number, row, view.linked) for number, row in enumerate(rows.values(), start=1)
        ],
        "action": RESULT_HOMEWORK.format(result_id=result_id),
        "revision": "" if place.revision is None else str(place.revision),
        "search_action": RESULT_SEARCH_AT.format(result_id=result_id),
        "search_heading": Said(SEARCH_HEADING.format(class_name=named.name), authored=True),
        "search_label": SEARCH_LABEL,
        "search_hint": SEARCH_HINT,
        "search_button": SEARCH_BUTTON,
        "found_heading": FOUND_HEADING,
        "link_label": LINK_HOMEWORK[1],
        "previous_page": PREVIOUS_PAGE,
        "next_page": NEXT_PAGE,
        "others_heading": OTHER_RESULTS,
        "others": [
            other_shown(
                one, report_names(reads[(one.class_id, one.term)].reports, state.clock.zone)
            )
            for one in others
        ],
        "found": found,
        "search": None if found is None else search_shown(found, view.linked, named.name),
    }


def grade_page_not_shown(error: GradeValueUndecodable) -> Response:
    """The generic answer of a grade page a stored value or her links stop, with one record of
    the failure's type alone: nothing the gradebook holds is written."""
    logger.warning("a grade page wasn't shown: %s", type(error).__name__)
    return Response("Internal Server Error", status_code=500, media_type="text/plain")


def result_page(
    request: Request,
    state: ApplicationState,
    result_id: str,
    *,
    said: str | None = None,
    status_code: int = 200,
    search: tuple[str, str] | None = None,
    words: str = "",
) -> Response:
    """A result's page as it reads now, with ``said`` first when an answer says something, and
    ``words`` in its search field; with ``search``, what it finds or why nothing is listed. Or
    the page saying no current record holds the result. A read alone."""
    context: dict[str, object] = {"grades": GRADES, "marks": state.settings.page_marks}
    shown = result_page_read(state, result_id, search=search)
    if shown is None:
        return templates.TemplateResponse(
            request,
            "grade_result_homework.html",
            {**context, "missing": RESULT_NOT_ON_RECORD},
            status_code=404,
        )
    found = shown["found"]
    if isinstance(found, NoList) and found in NOT_LISTED:
        said, status_code = NOT_LISTED[found], 422
    return templates.TemplateResponse(
        request,
        "grade_result_homework.html",
        {
            **context,
            **shown,
            "problem": said,
            "search_words": words,
            "words_refused": said == SEARCH_WORDS_REFUSED,
        },
        status_code=status_code,
    )


def link_not_written(error: GradeValueUndecodable) -> Response:
    """The generic answer of a decision a stored value or her links stop, with one record of
    the failure's type alone: nothing the gradebook holds is written."""
    logger.warning("a homework link wasn't written: %s", type(error).__name__)
    return Response("Internal Server Error", status_code=500, media_type="text/plain")


@family_router.get("/results/{result_id}/homework", response_class=HTMLResponse)
def homework_for_result(result_id: str, request: Request, state: State) -> Response:
    """Route 12: the homework of one result, a parent's page, with a form for each decision."""
    try:
        return result_page(request, state, result_id)
    except GradeValueUndecodable as error:
        return grade_page_not_shown(error)
    except sqlite3.Error as error:
        asked = asked_address(
            RESULT_HOMEWORK.format(result_id=segment(result_id)), request.scope["query_string"]
        )
        return homework_unshown(request, state, error, asked)


def result_answer(
    request: Request,
    state: ApplicationState,
    result_id: str,
    *,
    said: str | None = None,
    status_code: int = 200,
) -> Response:
    """A result's page with a decision's answer first, or that answer on the page that reads no
    store when the result's page can't be read."""
    try:
        return result_page(request, state, result_id, said=said, status_code=status_code)
    except sqlite3.Error as error:
        again = RESULT_HOMEWORK.format(result_id=segment(result_id))
        if said is None:
            return homework_unshown(request, state, error, again)
        return outcome_unshown(
            request,
            state,
            error,
            heading=HOMEWORK_FOR_RESULT,
            said=said,
            unshown_said=RESULT_HOMEWORK_UNSHOWN,
            again=again,
            ways_back=[ReturnLink(GRADES, BACK_TO_GRADES)],
            status_code=status_code,
        )


def homework_unshown(
    request: Request, state: ApplicationState, error: sqlite3.Error, again: str
) -> Response:
    """A result's homework page, or its search, when its record can't be read."""
    return unshown(
        request,
        state,
        error,
        heading=HOMEWORK_FOR_RESULT,
        said=RESULT_HOMEWORK_UNSHOWN,
        again=again,
        ways_back=[ReturnLink(GRADES, BACK_TO_GRADES)],
    )


@family_router.post("/results/{result_id}/homework", response_class=HTMLResponse)
async def decide_homework(result_id: str, request: Request, state: State) -> Response:
    """Route 13: a parent's decision about the result and one homework, by the store's one
    write. A decision written now or standing already lands on the result's heading. Other
    handled outcomes return a page with an explanation or the plain generic 500."""
    fields, whole = await fields_of(request, DECISION_FIELDS)
    homework, decision = fields.get("homework", "").strip(), fields.get("decision", "")
    revision = fields.get("revision", "")
    answer = partial(result_answer, request, state, result_id)
    store = state.project_state

    def owned() -> None:
        with store.reading():
            result_own(store, result_id)

    try:
        if not RESULT_ID.fullmatch(result_id):
            return await asyncio.to_thread(answer)
        if (
            not whole
            or not homework
            or decision not in get_args(LinkDecision)
            or not A_REVISION.fullmatch(revision)
            or int(revision) > REVISION_MAX
        ):
            return await asyncio.to_thread(answer, said=CHOOSE_HOMEWORK, status_code=422)
        # A read of its own, before the write and outside its transaction: a term not among
        # its year's terms stops the press as it stops the page; no result is the write's 404.
        try:
            await asyncio.to_thread(owned)
        except sqlite3.Error as error:
            return unshown(
                request,
                state,
                error,
                heading=HOMEWORK_FOR_RESULT,
                said=RECORDS_UNREAD,
                again=RESULT_HOMEWORK.format(result_id=segment(result_id)),
                ways_back=[ReturnLink(GRADES, BACK_TO_GRADES)],
            )
        try:
            async with state.decision_lock:
                outcome = await worker_to_the_end(
                    partial(
                        store.decide_homework_link,
                        result_id,
                        homework,
                        decision=cast(LinkDecision, decision),
                        revision=int(revision),
                        role=role_of(state),
                    )
                )
        except GradeTransactionLost:
            return await asyncio.to_thread(answer, said=PRESS_UNKNOWN, status_code=500)
        except GradeReportNotSaved:
            return await asyncio.to_thread(answer, said=NOT_SAVED, status_code=500)
        if isinstance(outcome, RequiredIndexUnavailable):
            logger.warning(INDEX_UNAVAILABLE)
            return Response("Internal Server Error", status_code=500, media_type="text/plain")
        if isinstance(outcome, HomeworkLinkResultNotOnRecord):
            return await asyncio.to_thread(answer)
        if isinstance(outcome, HomeworkLinkChanged):
            return await asyncio.to_thread(answer, said=LINKS_CHANGED, status_code=409)
        if isinstance(outcome, HomeworkLinkClassNameChanged):
            return await asyncio.to_thread(answer, said=NAME_CONNECTED_DIFFERENTLY, status_code=409)
        if isinstance(outcome, HomeworkLinkHomeworkNotOnRecord):
            return await asyncio.to_thread(answer, said=HOMEWORK_NOT_ON_RECORD, status_code=409)
    except GradeValueUndecodable as error:
        return link_not_written(error)
    return RedirectResponse(RESULT_HOMEWORK_AT.format(result_id=result_id), status_code=303)


@family_router.post("/results/{result_id}/homework/search", response_class=HTMLResponse)
async def search_homework(result_id: str, request: Request, state: State) -> Response:
    """Route 14: a parent's search of the result's homework, its words and page number in the
    form's body. The result's page answers with what they find, twenty to a page, or why
    nothing is listed; nothing is written."""
    fields, whole = await fields_of(request, SEARCH_FIELDS)
    words = fields.get("words", "")
    answer = partial(result_page, request, state, result_id, words=words)
    try:
        if not whole:
            return await asyncio.to_thread(answer, said=SEARCH_WORDS_REFUSED, status_code=422)
        return await asyncio.to_thread(answer, search=(words, fields["page"]))
    except GradeValueUndecodable as error:
        return grade_page_not_shown(error)
    except sqlite3.Error as error:
        # Asked for again as the result's page: a search is a press, and its words aren't kept.
        again = RESULT_HOMEWORK.format(result_id=segment(result_id))
        return homework_unshown(request, state, error, again)
