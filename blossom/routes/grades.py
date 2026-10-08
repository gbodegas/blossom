# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The grade pages: adding a report by pasting it, its review, its save, and the save's outcome.

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
"""

import asyncio
import ipaddress
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request as StarletteRequest

from blossom.dependencies import ApplicationState, get_application_state
from blossom.grades.draft import (
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
    TERM_LIMIT,
    AlreadyRecorded,
    Cell,
    GradeAnswers,
    GradeReportSaved,
    GradeReview,
    IdentityAnswer,
    ItemStatus,
    Label,
    MatchAnswer,
    NotHers,
    RecordedSave,
    ReturnReason,
    ReviewItem,
    ReviewPage,
    SaveOutcome,
    StillAsked,
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
from blossom.routes.student import templates
from blossom.stores.gradebook import ConfirmedBy, GradeTransactionLost
from blossom.stores.paths import UnsafeCheckpointPath

logger = logging.getLogger(__name__)

ADD: Final = "/parent/grades/add"
REVIEW: Final = "/parent/grades/add/review"
EDIT: Final = "/parent/grades/add/edit"
SAVE: Final = "/parent/grades/add/save"
CHECK: Final = "/parent/grades/add/check"
SAVED_AT: Final = "/parent/grades/saved/{acceptance_id}"
ACCEPTANCE_ID: Final = re.compile(r"acceptance-[0-9a-f]{32}")
"""The shape of an acceptance ID, checked before any lookup."""
TEXT: Final = frozenset({"report_text"})
FIXED: Final = frozenset(
    {"report_text", "acceptance_id", "revision", "source_key", "identity_form"}
)
"""The review form's fields a page always sends."""
ANSWERED: Final = frozenset(
    {"identity", "setup", "setup_year", "setup_term", "first_month", "class", "class_name", "use"}
)
"""The review form's answers, each of which a browser may leave out."""
ROW_FIELDS: Final = ("match", "candidates", "choose", "choices")
"""Each row's own fields, named with its position."""

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
    "Nothing was saved. Answer again where asked."
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
NAME_CONFIRMED: Final = "Her name is confirmed for her grade reports."
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


def status_word(shown: Shown) -> str:
    """How the review names a value's status: a value the copy didn't capture by its own
    fields, any other by its status alone."""
    if shown.item.status is ItemStatus.VALUE_NOT_CAPTURED:
        return NOT_CAPTURED_BY_KIND[shown.kind]
    return STATUS_WORDS[shown.item.status]


def all_kept(kept: StillAsked, answers: GradeAnswers) -> bool:
    """Whether every answer the page sent still answers the review, none dropped."""
    return (
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
        "check": CHECK,
        "edit": EDIT,
        "add": ADD,
        "marks": state.settings.page_marks,
        "said": None,
        "problems": [],
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
    def names(self) -> frozenset[str]:
        """Every field name the review's form may send for this draft."""
        names = set(FIXED | ANSWERED)
        for position in range(len(self.keys)):
            names.add(f"select.{position}")
            if position >= self.first_row:
                names.update(f"{field}.{position}" for field in ROW_FIELDS)
        return frozenset(names)


def positions_of(draft: GradeReportDraft) -> Positions:
    """Where ``draft``'s items stand on its review page."""
    keys = item_keys(draft)
    rows = sum(len(category.rows) for category in draft.categories)
    return Positions(keys, len(keys) - rows)


class Damaged(ValueError):
    """A field the page itself fills came back outside its form."""


@dataclass(frozen=True)
class Posted:
    """A review form as sent: the page it came from, its answers (None with no answer about the
    name), the ticked keys, and what the parent must correct."""

    page: ReviewPage
    answers: GradeAnswers | None
    selection: frozenset[str]
    problems: tuple[str, ...]


def revision_of(text: str) -> int | None:
    """A revision as a page carries it: ``none``, or a whole number."""
    if text == "none":
        return None
    if not text.isdigit():
        raise Damaged(text)
    return int(text)


def matches_of(fields: dict[str, str], positions: Positions) -> tuple[list[MatchAnswer], list[str]]:
    """Each row's one answer, by position, and the rows whose "Choose an existing assignment"
    names none."""
    matches: list[MatchAnswer] = []
    problems: list[str] = []
    for position in range(positions.first_row, len(positions.keys)):
        key = positions.keys[position]
        candidates = tuple(fields.get(f"candidates.{position}", "").split())
        choices = tuple(fields.get(f"choices.{position}", "").split())
        chosen = fields.get(f"choose.{position}", "")
        match = fields.get(f"match.{position}")
        if match is None:
            if chosen and not candidates:
                matches.append(MatchAnswer(key, choices, chosen, chosen=True))
            continue
        if match == "different":
            matches.append(MatchAnswer(key, candidates, None))
        elif match == "choose":
            if not chosen:
                problems.append(CHOOSE_WHICH)
                continue
            matches.append(MatchAnswer(key, choices, chosen, chosen=True))
        elif match in candidates:
            matches.append(MatchAnswer(key, candidates, match))
        else:
            raise Damaged(match)
    return matches, problems


def posted_of(fields: dict[str, str], draft: GradeReportDraft, positions: Positions) -> Posted:
    """The answers and ticks a whole review form sends, read by the grammar of its names alone;
    ``Damaged`` when a field the page fills is outside its form."""
    header = draft.header
    page = ReviewPage(
        fields["acceptance_id"], revision_of(fields["revision"]), fields["source_key"]
    )
    problems: list[str] = []
    setup: tuple[str, str] | None = None
    if fields.get("setup") == "report":
        setup = (header.year_label, header.term_label)
    elif fields.get("setup") == "other":
        setup = (folded(fields.get("setup_year", "")), folded(fields.get("setup_term", "")))
        if not is_school_year(setup[0]):
            problems.append(YEAR_FORM)
        if not setup[1]:
            problems.append(TERM_BLANK)
    elif "setup" in fields:
        raise Damaged(fields["setup"])
    month = fields.get("first_month", "")
    first_month: tuple[str, int] | None = None
    if month in {str(number) for number in range(1, 13)}:
        first_month = (header.year_label, int(month))
    elif month not in ("", "unsure"):
        raise Damaged(month)
    new_class = same_class = None
    same_revision: int | None = None
    chosen_class = fields.get("class")
    if chosen_class == "new":
        new_class = folded(fields.get("class_name", ""))
        if not new_class:
            problems.append(CLASS_BLANK)
    elif chosen_class is not None:
        class_id, _, revision = chosen_class.rpartition(":")
        if not class_id:
            raise Damaged(chosen_class)
        same_class, same_revision = class_id, revision_of(revision)
    use = fields.get("use")
    if use not in (None, "current", "earlier"):
        raise Damaged(use)
    matches, unchosen = matches_of(fields, positions)
    problems.extend(unchosen)
    selection = set()
    for position, key in enumerate(positions.keys):
        ticked = fields.get(f"select.{position}")
        if ticked is not None and ticked != "1":
            raise Damaged(ticked)
        if ticked is not None:
            selection.add(key)
    identity = fields.get("identity")
    if identity is not None and identity not in set(IdentityAnswer):
        raise Damaged(identity)
    answers = None
    if identity is not None:
        answers = GradeAnswers(
            identity=IdentityAnswer(identity),
            identity_form=None if fields["identity_form"] == "none" else fields["identity_form"],
            setup=setup,
            first_month=first_month,
            new_class=new_class,
            same_class=same_class,
            same_class_revision=same_revision,
            matches=tuple(matches),
            use=use,  # type: ignore[arg-type]
        )
        problems.extend(TOO_LONG_LABEL[label] for label in labels_too_long(answers))
    elif new_class is not None and len(new_class) > CLASS_NAME_LIMIT:
        problems.append(TOO_LONG_LABEL["class_name"])
    return Posted(page, answers, frozenset(selection), tuple(dict.fromkeys(problems)))


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
    the fields as sent, and what they answer."""

    text: str
    unrecognized: list[str]
    complete: bool
    draft: GradeReportDraft
    positions: Positions
    fields: dict[str, str]
    posted: Posted


async def review_form_of(request: Request, state: ApplicationState) -> ReviewForm | Response:
    """The review form a save or a check sent, or the paste page saying why it can't be read:
    the text first, then every field by the names the text's own draft allows."""
    first, _ = await fields_of(request, TEXT)
    text = first.get("report_text", "")
    if not text.strip():
        return paste_page(request, state, said=NOTHING_PASTED, status_code=422)
    if len(text) > TEXT_MAX_LENGTH:
        return paste_page(request, state, text=text, said=TOO_LONG, status_code=422)
    reading = read_grade_report(text)
    draft = reading.draft
    if draft is None:
        said = NOT_READ[reading.not_read or NotRead.NO_HEADER]
        return paste_page(request, state, text=text, said=said, status_code=422)
    positions = positions_of(draft)
    names = positions.names
    fields, whole = await fields_of(request, names, may_be_absent=names - FIXED)
    try:
        if not whole:
            raise Damaged(NOT_WHOLE)
        posted = posted_of(fields, draft, positions)
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
    )


def review_page(
    request: Request,
    state: ApplicationState,
    form: ReviewForm,
    review: GradeReview,
    *,
    said: str | None = None,
    problems: tuple[str, ...] | list[str] = (),
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
            form.draft, capture_key(form.draft), key=key, complete=form.complete
        )
        kept = still_asked(review, answers, outcome.uncovered or posted.selection)
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
            kept_sentence=BOTH_KEPT if all_kept(kept, answers) else TEXT_KEPT_ANSWER_AGAIN,
        )
    if isinstance(outcome, GradeReview):
        return review_page(request, state, form, outcome, fields=form.fields, ticks=ticks_of(form))
    review = outcome.review
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
    kept = still_asked(review, answers, posted.selection)
    fields, ticks = kept_fields(kept, review, form.draft, form.positions)
    every = outcome.why is not ReturnReason.SOURCE and all_kept(kept, answers)
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
        form.draft, capture_key(form.draft), key=key, complete=form.complete
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
        },
    )
