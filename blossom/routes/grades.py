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
"""

import asyncio
import hashlib
import ipaddress
import logging
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, tzinfo
from typing import Annotated, Final, Literal, cast, get_args

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request as StarletteRequest

from blossom.dependencies import ApplicationState, get_application_state
from blossom.grades.dates import due_date_of
from blossom.grades.draft import (
    HEX_KEY,
    GradeCategory,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    capture_key,
    folded,
    is_school_year,
)
from blossom.grades.identity import IdentityStatus, name_form_key
from blossom.grades.review import (
    CLASS_NAME_LIMIT,
    RESULT_FIELDS,
    TERM_LIMIT,
    AlreadyRecorded,
    Cell,
    ChoiceFacts,
    CurrentValue,
    CurrentValues,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
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
from blossom.grades.text_reader import NotRead, read_grade_report, reading_complete
from blossom.household import UnreadableHouseholdSecret, read_authority, secret_beside
from blossom.intake import TEXT_MAX_LENGTH
from blossom.routes.forms import fields_of
from blossom.routes.student import parent_reads, templates, viewer_of
from blossom.stores.gradebook import (
    ACCEPTANCE_ID,
    CLASS_ID,
    RESULT_ID,
    REVISION_MAX,
    ClassReport,
    ConfirmedBy,
    ContextChanged,
    ContextNotOnRecord,
    GradebookRecords,
    GradeReportNotSaved,
    GradeTransactionLost,
    Term,
    TermNotOnRecord,
    Viewer,
    ViewNotOnRecord,
)
from blossom.stores.paths import UnsafeCheckpointPath

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


SAVED_LABELS: Final = {
    "points": "school score",
    "max_points": "maximum",
    "percent": "percent",
    "letter": "letter grade",
    "average": "average",
}
"""The fields a changed value's saved line shows, in its order, each by its own name."""


def described(presence: Presence, text: str, label: str | None, *, named: bool = False) -> str:
    """A cell's text and presence as the review reads them out: as written, or what the presence
    says, short after a label the page writes and under ``label`` where no label introduces it.
    ``named`` puts the label before a reported value too."""
    if presence is Presence.REPORTED:
        return f"{label} {text}" if named and label else text
    said = {
        Presence.BLANK: ("blank", "left blank"),
        Presence.UNREADABLE: (f"couldn't be read: {text}", f"couldn't be read: {text}"),
        Presence.NOT_CAPTURED: ("not in the copy", "not in the copy"),
    }[presence]
    return said[0] if label is None else f"{label} {said[1]}"


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


def presence_words(what: str, cell: Cell) -> str:
    """A cell that wasn't reported, in the words for its presence."""
    presence, text = cell
    if presence is Presence.BLANK:
        return f"{what} left blank"
    if presence is Presence.UNREADABLE:
        return f"{what} couldn't be read: {text}"
    return f"{what} not in the copy"


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


TOLD_APART_BY: Final[tuple[Callable[[ChoiceFacts], str], ...]] = (
    lambda one: (
        f"saved score {described(*one.points, None)} / {described(*one.max_points, 'maximum')}"
    ),
    lambda one: described(*one.category, "Category name"),
    lambda one: f"report added {one.report_added:%B} {one.report_added.day}",
    lambda one: f"row {one.position} of that report",
)
"""What a choice's name adds, in order, while it is still the same as another's."""


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


def choice_labels(ids: Iterable[str], offered: Mapping[str, ChoiceFacts]) -> dict[str, str]:
    """How a control names each result it offers, never by its ID: its title and due date, then,
    only while labels are the same, the saved score, the category, the day its report was added,
    its row there, and its saved entry among those still the same."""
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
                labels[result] = f"{labels[result]} · {told_apart_by(facts[result])}"
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


def all_kept(kept: StillAsked, answers: GradeAnswers, selection: Collection[str]) -> bool:
    """Whether every answer and tick the page sent still answers the review, none dropped."""
    return kept.selection == frozenset(selection) and (
        kept.identity,
        kept.setup,
        kept.first_month,
        kept.new_class,
        kept.same_class,
        kept.matches,
        kept.use,
    ) == (
        answers.identity,
        answers.setup,
        answers.first_month,
        answers.new_class,
        answers.same_class,
        answers.matches,
        answers.use,
    )


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
        "choice_labels": choice_labels,
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


def still_typos(review: GradeReview, answers: GradeAnswers) -> list[str] | None:
    """For a review a save returned over its answers: what the parent must correct when every
    answer that fails still answers a question asked now, or None when one answers a question
    not asked now."""
    if not identity_asked(review, answers) or not first_month_asked(review, answers):
        return None
    if not matches_asked(review, answers.matches) or not use_asked(review, answers.use):
        return None
    problems: list[str] = []
    if not setup_asked(review, answers):
        if (answers.setup is None) != (review.setup is None):
            return None
        problems.append(YEAR_FORM)
    if not class_asked(review, answers):
        question = review.class_question
        if question.matched is not None or answers.same_class is not None:
            return None
        problems.append(CLASS_BLANK if answers.new_class is not None else CHOOSE_CLASS)
    return problems or None


def kept_fields(
    kept: StillAsked, review: GradeReview, draft: GradeReportDraft, positions: Positions
) -> tuple[dict[str, str], frozenset[int]]:
    """The fields and ticks a returned review shows from what still answers it."""
    fields: dict[str, str] = {}
    if kept.identity is not None:
        fields["identity"] = kept.identity.value
    if kept.setup is not None:
        header = draft.header
        if kept.setup == (header.year_label, header.term_label):
            fields["setup"] = "report"
        else:
            fields.update(setup="other", setup_year=kept.setup[0], setup_term=kept.setup[1])
    if kept.first_month is not None:
        fields["first_month"] = str(kept.first_month[1])
    if kept.new_class is not None:
        fields.update({"class": "new", "class_name": kept.new_class})
    elif kept.same_class is not None:
        for class_id, _, revision in review.class_question.existing:
            if class_id == kept.same_class:
                fields["class"] = f"{class_id}:{'none' if revision is None else revision}"
    at = {key: position for position, key in enumerate(positions.keys)}
    for answer in kept.matches:
        position = at[answer.row_key]
        if answer.chosen:
            fields[f"choose.{position}"] = answer.result_id or ""
            item = next(item for item in review.rows if item.key == answer.row_key)
            if item.question is not None:
                fields[f"match.{position}"] = "choose"
        else:
            fields[f"match.{position}"] = answer.result_id or "different"
    if kept.use is not None:
        fields["use"] = kept.use
    return fields, frozenset(at[key] for key in kept.selection)


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
) -> Response:
    """The review of the form's text, with what wasn't processed said first, and the fields and
    ticks it keeps."""
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
        },
        status_code=status_code,
    )


def retry_page(request: Request, state: ApplicationState, form: ReviewForm, said: str) -> Response:
    """The review form sent again as it came, under the same acceptance ID, for a save whose
    outcome isn't known or that the record refused; the store isn't read."""
    return templates.TemplateResponse(
        request,
        "grade_retry.html",
        {
            "said": said,
            "kept_sentence": BOTH_KEPT,
            "text": form.text,
            "fields": {name: value for name, value in form.fields.items() if name != "report_text"},
            "save": SAVE,
            "edit": EDIT,
            "add": ADD,
            "marks": state.settings.page_marks,
        },
        status_code=500,
    )


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
        kept = kept_of(state, form, review, answers, outcome.uncovered or posted.selection, key)
        fields, ticks = kept_fields(kept, review, form.draft, form.positions)
        left = titles(review, form.draft, outcome.uncovered) if outcome.uncovered else None
        return review_page(
            request,
            state,
            form,
            review,
            said=SAVED_ELSEWHERE,
            fields=fields,
            ticks=ticks,
            recorded=outcome_address(outcome.saved.acceptance_id),
            left_out=left,
            kept_sentence=(
                BOTH_KEPT if all_kept(kept, answers, posted.selection) else TEXT_KEPT_ANSWER_AGAIN
            ),
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
        typos = still_typos(review, answers)
        if typos is not None:
            return review_page(
                request,
                state,
                form,
                review,
                said=CORRECT_THE_MARKED,
                problems=typos,
                fields=form.fields,
                ticks=ticks_of(form),
                status_code=422,
            )
    kept = kept_of(state, form, review, answers, posted.selection, key)
    fields, ticks = kept_fields(kept, review, form.draft, form.positions)
    every = outcome.why is not ReturnReason.SOURCE and all_kept(kept, answers, posted.selection)
    kept_sentence = BOTH_KEPT if every else TEXT_KEPT_ANSWER_AGAIN
    return review_page(
        request,
        state,
        form,
        review,
        said=RETURNED[outcome.why],
        fields=fields,
        ticks=ticks,
        kept_sentence=kept_sentence,
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
    answers that still answer it, or with no answer about the name the fields as sent, every
    row as a fresh review shows it, and nothing written."""
    answers = form.posted.answers
    review = state.project_state.review_grade_report(
        form.draft,
        capture_key(form.draft),
        key=key,
        complete=form.complete,
        same_class=form.free.same_class,
    )
    fields = form.fields
    if answers is not None:
        kept = kept_of(state, form, review, answers, form.posted.selection, key)
        fields, _ = kept_fields(kept, review, form.draft, form.positions)
    return review_page(
        request,
        state,
        form,
        review,
        said=TEXT_CHANGED,
        fields=fields,
        kept_sentence=TEXT_KEPT_ANSWER_AGAIN,
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
    return review_page(
        request,
        state,
        form,
        review,
        said=unanswered if posted.answers is None else CORRECT_THE_MARKED,
        problems=posted.problems,
        to_resolve=posted.to_resolve,
        fields=form.fields,
        ticks=ticks_of(form),
        status_code=422,
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
    if form.text_changed:
        return changed_text(request, state, form, key)
    answers = refused_answers(request, state, form, key)
    if isinstance(answers, Response):
        return answers
    try:
        async with state.decision_lock:
            outcome = state.project_state.save_grade_report(
                form.draft,
                capture_key(form.draft),
                key=key,
                page=form.posted.page,
                answers=answers,
                selection=form.posted.selection,
                role=role_of(state),
                complete=form.complete,
            )
    except GradeTransactionLost:
        logger.warning("A grade report's save may not have finished: GradeTransactionLost")
        return retry_page(request, state, form, OUTCOME_UNKNOWN)
    except Exception as error:
        logger.warning("A grade report wasn't saved: %s", type(error).__name__)
        return retry_page(request, state, form, NOT_SAVED)
    if isinstance(outcome, AlreadyRecorded) and not outcome.uncovered:
        return RedirectResponse(outcome_address(outcome.saved.acceptance_id), status_code=303)
    return returned_page(request, state, form, answers, outcome, key)


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
    if form.text_changed:
        return changed_text(request, state, form, key)
    answers = refused_answers(request, state, form, key)
    if isinstance(answers, Response):
        return answers
    outcome = state.project_state.check_grade_answers(
        form.draft,
        capture_key(form.draft),
        key=key,
        complete=form.complete,
        page=form.posted.page,
        answers=answers,
        selection=form.posted.selection,
    )
    return returned_page(request, state, form, answers, outcome, key)


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
    recorded = None
    if ACCEPTANCE_ID.fullmatch(acceptance_id):
        recorded = state.project_state.recorded_save(acceptance_id)
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
            "class_link": class_link(state, recorded),
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

GRADES: Final = "/parent/grades"
HER_GRADES: Final = "/student/grades"
CURRENT_TERM: Final = "/parent/grades/current-term"
CLASS_AT: Final = "{base}/classes/{class_id}/terms/{n}"
FOLLOW_CURRENT: Final = "current"
"""The chooser's value for following the current term."""
VIEW_FIELDS: Final = frozenset({"view"})
CONTEXT_FIELDS: Final = frozenset({"shown", "term"})
POSITION: Final = re.compile(r"[1-9][0-9]{0,3}")
"""A term's place in its class's year, from 1, as an address writes it."""

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
ORDINALS: Final = (
    "",
    "second ",
    "third ",
    "fourth ",
    "fifth ",
    "sixth ",
    "seventh ",
    "eighth ",
    "ninth ",
    "tenth ",
)
"""How a report's name says its place among the reports added the same day."""

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


def spoken_date(day: date) -> str:
    """A day as a page says it: ``September 30``."""
    return f"{day.strftime('%B')} {day.day}"


def spoken_day(moment: str, zone: tzinfo) -> str:
    """A stored moment's day where the household is: ``September 30``."""
    return spoken_date(datetime.fromisoformat(moment).astimezone(zone).date())


def report_names(reports: tuple[ClassReport, ...], zone: tzinfo) -> dict[str, str]:
    """Each report as a sentence names it: "report added September 30", with its place among
    the reports added that day when it shares one, by acceptance order. A report an action made
    carries its capture's name and the day it was made current."""
    imported = [report for report in reports if report.acted_at is None]
    days = {report.report_id: spoken_day(report.imported_at, zone) for report in imported}
    names: dict[str, str] = {}
    of_capture: dict[str, str] = {}
    for report in imported:
        day = days[report.report_id]
        same_day = [other.report_id for other in imported if days[other.report_id] == day]
        place = same_day.index(report.report_id)
        ordinal = ORDINALS[place] if place < len(ORDINALS) else f"report {place + 1}, "
        names[report.report_id] = f"{ordinal}report added {day}"
        of_capture[report.latest_of_capture] = names[report.report_id]
    for report in reports:
        if report.acted_at is not None:
            source = of_capture.get(
                report.latest_of_capture, f"report added {spoken_day(report.imported_at, zone)}"
            )
            made = spoken_day(report.acted_at, zone)
            names[report.report_id] = f"{source}, made current {made}"
    return names


def as_line(name: str) -> str:
    """A report's name as a line of its own: "Report added September 30 · Made current
    October 8"."""
    line = name.replace(", made current ", " · Made current ")
    return line[:1].upper() + line[1:]


def as_percent(text: str) -> str:
    """A percent the school reported, as written, with its sign."""
    return text if text.endswith("%") else f"{text}%"


def cell_text(cell: Cell, what: str) -> str:
    """A cell as written, or the words for its presence."""
    return cell[1] if cell[0] is Presence.REPORTED else presence_words(what, cell)


def term_grade(value: CurrentValue) -> str:
    """The school-reported term grade as written: the percent and the letter."""
    percent, letter = value.cells["percent"], value.cells["letter"]
    if percent[0] is not Presence.REPORTED and letter[0] is not Presence.REPORTED:
        return presence_words("Term grade", percent)
    parts = []
    if percent[0] is Presence.REPORTED:
        parts.append(as_percent(percent[1]))
    if letter[0] is Presence.REPORTED:
        parts.append(letter[1])
    return "School-reported grade " + " · ".join(parts)


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
    """A class on Grades: its name, its details' address when the term is on record, and its
    term grade when a current report supplied one."""

    name: str
    link: str | None
    reported: bool
    summary: TermSummary | None


def class_rows(store: GradebookRecords, term: Term, base: str, zone: tzinfo) -> list[ClassRow]:
    """Each class of the open term's year, by name, with its term grade in that term."""
    year, label = term
    terms = store.terms_of_year(year)
    position = terms.index(label) + 1 if label in terms else None
    rows = []
    for one in store.classes_in(year, label):
        link = None
        if position is not None:
            link = CLASS_AT.format(base=base, class_id=one.class_id, n=position)
        summary = None
        if one.reported:
            read = store.class_term(one.class_id, label)
            summary = term_summary(read.current, report_names(read.reports, zone))
        rows.append(ClassRow(one.name, link, one.reported, summary))
    return rows


def grades_page(
    request: Request, state: ApplicationState, *, said: str | None = None, status_code: int = 200
) -> Response:
    """Grades for whoever reads: the term the viewer chose, else the current one, each class's
    term grade in it, the chooser, and a parent's controls in a parent's voice. A read alone."""
    store = state.project_state
    contexts = store.grade_contexts()
    view = store.view_of(cast(Viewer, viewer_of(request)))
    open_term = view or contexts.current
    family = parent_reads(request)
    base = GRADES if family else HER_GRADES
    terms = list(contexts.terms)
    if contexts.current is not None and contexts.current not in terms:
        terms.append(contexts.current)
    rows = class_rows(store, open_term, base, state.clock.zone) if open_term else []
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
            "reported": any(row.reported for row in rows),
            "chooser": chooser,
            "following": view is None,
            "view_action": f"{base}/view",
            "showing": showing,
            "current_term": term_named(contexts.current) if contexts.current else None,
            "make_current": make_current,
            "add": ADD,
            "current_action": CURRENT_TERM,
            "follow": FOLLOW_CURRENT,
            "no_reports": NO_REPORTS_YET,
            "add_one": ADD_ONE,
            "no_report_added": NO_REPORT_ADDED,
            "marks": state.settings.page_marks,
        },
        status_code=status_code,
    )


@student_router.get("", response_class=HTMLResponse)
def her_grades(request: Request, state: State) -> Response:
    """Route 5: Grades, in the voice of whoever reads."""
    return grades_page(request, state)


@family_router.get("", response_class=HTMLResponse)
def family_grades(request: Request, state: State) -> Response:
    """Route 5p: Grades in a parent's voice."""
    return grades_page(request, state)


async def chosen_view(request: Request, state: ApplicationState, base: str) -> Response:
    """A viewer's term for Grades, kept as their own row: a term on record, or the current one
    followed. Anything else is refused and nothing is written."""
    fields, whole = await fields_of(request, VIEW_FIELDS)
    value = fields.get("view", "")
    view = None if value == FOLLOW_CURRENT else term_posted(value)
    if not whole or (value != FOLLOW_CURRENT and view is None):
        return grades_page(request, state, said=TERM_NOT_ON_RECORD, status_code=422)
    try:
        async with state.decision_lock:
            state.project_state.choose_view(cast(Viewer, viewer_of(request)), view)
    except ViewNotOnRecord:
        return grades_page(request, state, said=TERM_NOT_ON_RECORD, status_code=422)
    return RedirectResponse(base, status_code=303)


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
    if not whole or shown is None or chosen is None:
        return grades_page(request, state, said=TERM_NOT_ON_RECORD, status_code=422)
    try:
        async with state.decision_lock:
            outcome = state.project_state.set_current_context(shown, chosen, role_of(state))
    except TermNotOnRecord:
        return grades_page(request, state, said=TERM_NOT_ON_RECORD, status_code=422)
    except GradeTransactionLost as error:
        logger.warning("The current term may not have been set: %s", type(error).__name__)
        return grades_page(request, state, said=CONTEXT_UNKNOWN, status_code=500)
    except GradeReportNotSaved as error:
        logger.warning("The current term was not set: %s", type(error).__name__)
        return grades_page(request, state, said=NOT_SAVED, status_code=500)
    if isinstance(outcome, ContextChanged):
        return grades_page(request, state, said=CONTEXT_CHANGED, status_code=409)
    if isinstance(outcome, ContextNotOnRecord):
        return grades_page(request, state, said=TERM_NOT_ON_RECORD, status_code=422)
    return RedirectResponse(GRADES, status_code=303)


@dataclass(frozen=True)
class ResultShown:
    """A result on class details: its title and category, its score as reported, its due date,
    the school's status for it, where it was last shown, and its school record details."""

    title: str
    category: str
    score: str
    due: str
    status: str | None
    showing: list[str]
    record: list[str]
    cues: list[str]


def score_of(value: CurrentValue) -> str:
    """The school score as written: points out of max, with the percent the school reported,
    or the words for a score that wasn't reported."""
    points, most = value.cells["points"], value.cells["max_points"]
    average = value.cells["average"]
    if points[0] is not Presence.REPORTED:
        return presence_words("Score", points)
    score = f"School score: {points[1]}"
    if most[0] is Presence.REPORTED:
        score += f" / {most[1]}"
    if average[0] is Presence.REPORTED:
        score += f" · {as_percent(average[1])}"
    return score


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
    score = score_of(value)
    if value.last_shown is not None and value.last_shown.report_id != value.report_id:
        score += f", from the {names.get(value.report_id, 'report')}"
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
        score=score,
        due=due_of(value, resolved, names),
        status=f"Gradebook status: {status[1]}" if status[0] is Presence.REPORTED else None,
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


def class_page(request: Request, state: ApplicationState, class_id: str, n: str) -> Response:
    """Class details of her class in the term at place ``n`` of its year: the term grade, each
    result by due date, newest first, the dates not confirmed apart, and the term's reports. A
    read alone; a class or place not on record is a page saying so."""
    store = state.project_state
    base = GRADES if parent_reads(request) else HER_GRADES
    context: dict[str, object] = {
        "family_tree": request.url.path.startswith(GRADES),
        "grades": base,
        "marks": state.settings.page_marks,
    }
    named = store.class_named(class_id)
    terms = store.terms_of_year(named.year) if named is not None else ()
    if named is None or not POSITION.fullmatch(n) or int(n) > len(terms):
        return templates.TemplateResponse(
            request,
            "grade_class.html",
            {**context, "missing": CLASS_NOT_ON_RECORD},
            status_code=404,
        )
    term = terms[int(n) - 1]
    read = store.class_term(class_id, term)
    reports, values = read.reports, read.current
    names = report_names(reports, state.clock.zone)
    newest = newest_current(reports)
    first_month = store.first_month_of(named.year)
    dated: list[tuple[date, str, ResultShown]] = []
    undated: list[ResultShown] = []
    for value in values.results.values():
        resolved, shown = result_shown(
            value, year=named.year, first_month=first_month, newest=newest, names=names
        )
        if resolved is None:
            undated.append(shown)
        else:
            dated.append((resolved, folded(shown.title).casefold(), shown))
    dated.sort(key=lambda item: (-item[0].toordinal(), item[1]))
    undated.sort(key=lambda shown: folded(shown.title).casefold())
    uses = {report.report_id: report.use for report in reports}
    captures: dict[str, ClassReport] = {}
    for report in reports:
        captures.setdefault(report.latest_of_capture, report)
    return templates.TemplateResponse(
        request,
        "grade_class.html",
        {
            **context,
            "missing": None,
            "name": named.name,
            "term": term_named((named.year, term)),
            "reported": bool(reports),
            "summary": term_summary(values, names),
            "no_report_added": NO_REPORT_ADDED,
            "dated": [shown for _, _, shown in dated],
            "undated": undated,
            "categories": [category_line(value) for value in values.categories.values()],
            "reports": [
                (
                    as_line(names.get(first.report_id, "report")),
                    "Current" if uses[latest] == "current" else "Kept as an earlier report",
                )
                for latest, first in captures.items()
            ],
        },
    )


@student_router.get("/classes/{class_id}/terms/{n}", response_class=HTMLResponse)
def her_class(class_id: str, n: str, request: Request, state: State) -> Response:
    """Route 9: class details, in the voice of whoever reads."""
    return class_page(request, state, class_id, n)


@family_router.get("/classes/{class_id}/terms/{n}", response_class=HTMLResponse)
def family_class(class_id: str, n: str, request: Request, state: State) -> Response:
    """Route 9p: class details in a parent's voice."""
    return class_page(request, state, class_id, n)
