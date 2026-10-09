# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What a pasted grade report would do against her record, and what a save of it did.

Every value of a report has a key made from what it says, never from its place: the term result,
each category by its name, and each result row by its evidence (category, title and due date)
with its occurrence among the rows alike. A row resolves to one of her results first through this
capture's own acceptance and row records, which settle which result it is, never that its value
was accepted; then by evidence: only equal evidence, unique in the report and in one result, that
no stored decision gives another result, matches without asking; then by the parent's explicit
answer for the same evidence, reused only when nothing makes it ambiguous. Otherwise a parent
answers which result it is, or that it is a different assignment, which stands for the same
evidence and the same candidates until changed (for a row whose due date wasn't captured, for the
same capture and row only); an open row may also be one of her results the parent chooses.

Each value's status compares it with its target's current value, as accepted school values:
"Saved", equal to it; "Matches an earlier saved value", equal to a value a current report
holds that a newer one replaced, so it was current once (a value only reports kept as earlier
hold was never current, and reads Changed or New);
"Changed", it differs; "New", no target; "Needs your answer", a matching question is open;
"Couldn't read", a cell is unreadable; "Due date not captured", the copy left out the due cell
of a row that resolves to none of her results; "Shown in a newer report", a current report newer
than the one its capture's rest joins supplied or showed its target; a value of which the copy
captured only the cells that say which target it is compares nothing, so it is neither Saved
nor offered. The comparison leaves out every cell the incoming copy didn't capture, on both
sides, as neither the same nor a change; a cell it captured is compared even when the saved
value lacks it, so it can be new information. A row with a value that can't be read but whose
identity reads, or whose category and title read but whose due date wasn't captured, still
asks its question, or offers her results to choose from; an answer records which result it
is, never its value. A due date that wasn't captured is never evidence, so such a row resolves
only through its own capture's records or the parent's answer or choice; then its value takes
its status as any other does, while a value that can't be read stays "Couldn't read". Only a
New or Changed value can be selected, and a value that matches an earlier saved one only under
the parent's choice of current for the new report a save makes. That choice starts on "Keep as
an earlier report" when any value repeats one a newer report replaced, and on current
otherwise.

The questions a review asks are the identity of the student line, the first setup, the first
month of a year not on record, and the class when no alias matches. The answers a page sends are
claims: a save holds them against the review it computes again, and any that answer no question
asked now, or a question left unanswered, saves nothing.
"""

import json
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Final, Literal, get_args

from blossom.grades.draft import (
    Evidence,
    GradeCategory,
    GradeReportDraft,
    GradeRow,
    GradeValue,
    Presence,
    folded,
    is_school_year,
    row_evidence,
)
from blossom.grades.identity import Identity, IdentityStatus


def _key(*parts: object) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


TERM_KEY: Final = _key("term")
"""The key of the report's term result."""


def category_key(category: GradeCategory, occurrence: int) -> str:
    """A category's key: its name with its presence, folded, and its place among categories
    with the same name, counting from 1."""
    return _key("category", [category.name.presence.value, folded(category.name.text)], occurrence)


def evidence_text(evidence: Evidence) -> str:
    """A row's evidence written out in one fixed form, as a match decision keeps it."""
    return _key(*([presence.value, text] for presence, text in evidence))


def row_key(category_name: GradeValue, row: GradeRow) -> str:
    """A result row's key: its evidence and its occurrence, never its position or its score."""
    evidence = row_evidence(category_name, row)
    return _key("row", [[presence.value, text] for presence, text in evidence], row.occurrence)


def category_keys(draft: GradeReportDraft) -> tuple[str, ...]:
    """The key of each category of ``draft``, in order."""
    seen: Counter[str] = Counter()
    keys = []
    for category in draft.categories:
        name = category_key(category, 1)
        seen[name] += 1
        keys.append(category_key(category, seen[name]))
    return tuple(keys)


Cell = tuple[Presence, str]
"""One cell as an observation keeps it: its presence and its text as written."""
TERM_FIELDS: Final = ("percent", "letter")
"""The cells of the term result, in order."""
CATEGORY_FIELDS: Final = ("name", "weight", "average")
"""The cells of a category, in order."""
RESULT_FIELDS: Final = (
    "category",
    "assignment",
    "points",
    "max_points",
    "average",
    "status",
    "due",
    "curve",
    "bonus",
    "penalty",
    "weight",
    "note",
)
"""The cells of a result row, its category's name first, then Assignment to Note."""
WHICH_FIELDS: Final = frozenset({"name", "category", "assignment", "due"})
"""The cells that say which target a value is, a category's name and a row's evidence; every
other cell is a value cell."""
Compared = tuple[tuple[str, str, str], ...]
"""A value as statuses compare it."""


def cells_of(fields: Iterable[str], values: Iterable[GradeValue]) -> dict[str, Cell]:
    """Each value of a draft as a cell, by its field."""
    return {
        field: (value.presence, value.text) for field, value in zip(fields, values, strict=True)
    }


def compared(cells: Mapping[str, Cell]) -> Compared:
    """A value as statuses compare accepted school values: each field with its presence and its
    text, spaces folded as the capture key folds them. Every status comparison goes through it."""
    return tuple((field, presence.value, folded(text)) for field, (presence, text) in cells.items())


@dataclass(frozen=True)
class ReportAt:
    """A report of the class and term, with its acceptance order."""

    report_id: str
    order: int


@dataclass(frozen=True)
class DueFrom:
    """A result's due cell as an observation captured it, blank or unreadable included, and the
    report that observation belongs to."""

    cell: Cell
    report: ReportAt


@dataclass(frozen=True)
class CurrentValue:
    """A target's value as one accepted report gave it: its cells by field, that report (its
    source) and its acceptance order. For a result's current value, also the newest current
    report that showed it, the newest report that supports "Not shown in this report", if any,
    and its due date: the newest current observation that captured its Due cell, whatever
    report supplied the value, or None when none did."""

    cells: Mapping[str, Cell]
    report_id: str
    order: int
    last_shown: ReportAt | None = None
    not_shown: ReportAt | None = None
    due: DueFrom | None = None


@dataclass(frozen=True)
class CurrentValues:
    """The current value of each target of a class and term: the term result, each category by its
    key and each result by its ID, each from the current report with the highest acceptance
    order that supplied it. An earlier report supplies none."""

    term: CurrentValue | None
    categories: Mapping[str, CurrentValue]
    results: Mapping[str, CurrentValue]


class ItemStatus(StrEnum):
    """What one value of a report is against her record."""

    NEW = "new"
    SAVED = "saved"
    MATCHES_EARLIER = "matches_earlier"
    """"Matches an earlier saved value": equal to a value a newer one replaced. Not offered."""
    CHANGED = "changed"
    NEEDS_ANSWER = "needs_answer"
    """"Needs your answer": a matching question is open."""
    UNREADABLE = "unreadable"
    """"Couldn't read": a cell is unreadable. Never offered, whether or not the row asks."""
    DUE_NOT_CAPTURED = "due_not_captured"
    """"Due date not captured": the row's category and title read, its due cell wasn't in the
    copy, and it resolves to none of her results. Never offered."""
    COVERED = "covered"
    """"Shown in a newer report": a New, Changed or matching-earlier value this capture's rest
    can't change, since a current report newer than the one it joins supplied or showed its
    target. Not offered."""
    VALUE_NOT_CAPTURED = "value_not_captured"
    """The copy captured none of the value's cells, only those that say which target it is, so
    nothing is compared: neither Saved nor offered."""


class QuestionKind(StrEnum):
    """What a matching question asks about a row."""

    DUE_CHANGED = "due_changed"
    """"Same assignment, due date changed from A to B?": one candidate, the same title, the
    row's due date and the candidate's both read."""
    RENAMED = "renamed"
    """"Same assignment, renamed from 'X'?": one candidate, another title."""
    WHICH = "which"
    """Which of the candidates it is, or "A different assignment"."""


@dataclass(frozen=True)
class Candidate:
    """A result a row may be: its ID and its latest observation, with its last score and status,
    and its due text as matching reads it."""

    result_id: str
    last: CurrentValue


@dataclass(frozen=True)
class MatchQuestion:
    """The question a row asks before it can be selected, with its candidates."""

    kind: QuestionKind
    candidates: tuple[Candidate, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        """The candidates' result IDs, in order: what an answer is bound to."""
        return tuple(candidate.result_id for candidate in self.candidates)


@dataclass(frozen=True)
class MatchAnswer:
    """A parent's answer to a row's question, bound to the candidates it was asked with: the
    result it is, or None for "A different assignment". With ``chosen``, "Choose an existing
    assignment", bound to the results the row offered."""

    row_key: str
    candidates: tuple[str, ...]
    result_id: str | None
    chosen: bool = False


MatchedHow = Literal["same_capture", "exact", "reused", "answer", "chosen"]
"""How a row resolved: through its own capture's records, by equal evidence, by a parent's
explicit answer for the same evidence reused, by an answer now, or by an existing assignment
chosen now."""


@dataclass(frozen=True)
class ReviewItem:
    """One value of the report: its key and status; for a row, the result it resolved to and how,
    or its open question; and its target's current value, the "from" of a Changed value."""

    key: str
    status: ItemStatus
    result_id: str | None = None
    current: CurrentValue | None = None
    question: MatchQuestion | None = None
    how: MatchedHow | None = None
    covered: bool = False
    """A value read "Shown in a newer report": a current report newer than the one this capture's
    rest joins supplied or showed its target. Not offered."""
    choices: tuple[str, ...] = ()
    """For a row no record, equal evidence or reuse resolved, the results "Choose an existing
    assignment" offers: her results in the class and term no such row resolved to."""
    remembered: bool = False
    """A stored "A different assignment" answers the row's question, which stays open to change."""
    due_not_captured: bool = False
    """The copy left out the row's due cell: "Due date not captured" shows beside its value,
    whatever its status."""


@dataclass(frozen=True)
class ClassQuestion:
    """The class: the one an alias matched, or the name offered for a new class and the year's
    classes it may be the same as, each an ID, a display name and the scope revision of its
    class and term (None when they hold nothing yet)."""

    matched: str | None
    offered_name: str
    existing: tuple[tuple[str, str, int | None], ...]


ReportUse = Literal["current", "earlier"]
"""A report's use: current, or kept as an earlier report, which supplies no current value."""


@dataclass(frozen=True)
class ChoiceFacts:
    """What tells a result offered for matching apart from the others: its title, category and
    due cell from its newest observation (``due`` None when no observation captured one), its
    score from its current value, else that observation, and that observation's report, by the
    day it was added in the household's zone and its acceptance order, and the row's position."""

    title: Cell
    category: Cell
    due: Cell | None
    points: Cell
    max_points: Cell
    report_added: date
    report_order: int
    position: int


@dataclass(frozen=True)
class UseChoice:
    """The report-level choice for the new report a save makes: "Use this as the current school
    record" ("Use these values where this report provides them" for a partial reading) or "Keep
    as an earlier report", starting on ``default``. ``repeats`` holds the keys of the values that
    repeat one a newer report replaced: "This report repeats values a newer report replaced."
    """

    default: ReportUse
    repeats: tuple[str, ...]


@dataclass(frozen=True)
class GradeReview:
    """What a report would do: a fresh acceptance ID for the page, the questions, the scope
    revision of the class and term (None when they hold nothing yet), each value's status in
    the report's order, and the report-level choice, None when a save joins the capture's
    latest report or making the new report current would change nothing."""

    acceptance_id: str
    source_key: str
    identity: Identity
    setup: tuple[str, str] | None
    first_month: str | None
    class_question: ClassQuestion
    revision: int | None
    term: ReviewItem | None
    categories: tuple[ReviewItem, ...]
    rows: tuple[ReviewItem, ...]
    use: UseChoice | None = None
    offered: Mapping[str, ChoiceFacts] = field(default_factory=dict)
    """By result ID, what tells apart each result a row may choose or a question asks about: the
    results no row resolved to before any answer applies, and every question's candidates."""

    @property
    def items(self) -> tuple[ReviewItem, ...]:
        """Every value: the term result when the copy has one, the categories, then the rows."""
        return (*(() if self.term is None else (self.term,)), *self.categories, *self.rows)

    @property
    def ready(self) -> frozenset[str]:
        """The keys of the values a save may select: the New and Changed ones no newer report
        covers."""
        return frozenset(
            item.key for item in self.items if item.status in OFFERED and not item.covered
        )

    @property
    def back_to(self) -> frozenset[str]:
        """The keys of the values that match an earlier saved one, which a save may also select
        under the parent's choice of current: each goes back to its value from its "from"."""
        return frozenset(
            item.key
            for item in self.items
            if self.use is not None and item.status is ItemStatus.MATCHES_EARLIER
        )


class IdentityAnswer(StrEnum):
    """The page's answer about the student line."""

    SHOWN = "shown"
    """The line matched a confirmed form, and the page showed it."""
    HERS = "hers"
    """"Yes, this is her name"."""
    MISREAD = "misread"
    """"The name was misread": the import continues and no form is added."""
    CONFIRMED = "confirmed"
    """"These are her grades", for a report with no student line."""
    NOT_HERS = "not_hers"
    """"Not hers": the review ends and nothing is saved."""


ANSWERS_FOR: Final[Mapping[IdentityStatus, frozenset[IdentityAnswer]]] = {
    IdentityStatus.MATCHES: frozenset({IdentityAnswer.SHOWN}),
    IdentityStatus.FIRST_USE: frozenset({IdentityAnswer.HERS, IdentityAnswer.MISREAD}),
    IdentityStatus.NOT_CONFIRMED: frozenset({IdentityAnswer.HERS, IdentityAnswer.MISREAD}),
    IdentityStatus.CONFIRM_AGAIN: frozenset({IdentityAnswer.HERS, IdentityAnswer.MISREAD}),
    IdentityStatus.MISSING: frozenset({IdentityAnswer.CONFIRMED}),
}
"""The answers a save takes for each question about the line; "Not hers" saves nothing."""


@dataclass(frozen=True)
class GradeAnswers:
    """The answers a review page sends: about the line, with the form of the line it answered;
    the year and term confirmed at the first setup; the year and its first month; the class, as a
    new one's display name or the ID of the class it is the same as, with the scope revision the
    page showed for it; and the matching answers."""

    identity: IdentityAnswer
    identity_form: str | None
    setup: tuple[str, str] | None = None
    first_month: tuple[str, int] | None = None
    new_class: str | None = None
    same_class: str | None = None
    same_class_revision: int | None = None
    matches: tuple[MatchAnswer, ...] = ()
    use: ReportUse | None = None
    """The report-level choice, or None for its default."""


@dataclass(frozen=True)
class ReviewPage:
    """What a page carries besides its answers: its acceptance ID, the revision it was built
    on, and the capture key of the report it reviewed."""

    acceptance_id: str
    revision: int | None
    source_key: str


@dataclass(frozen=True)
class GradeReportSaved:
    """A committed save: its acceptance, the report it went into (None when it recorded nothing
    new), its counts, each accepted key with the result it resolved to, and how many rows it
    recorded as shown, with the explicit answers among them it kept."""

    acceptance_id: str
    report_id: str | None
    added: int
    updated: int
    already_saved: int
    left: int
    accepted: tuple[tuple[str, str | None], ...]
    shown: int = 0
    answers_kept: int = 0


@dataclass(frozen=True)
class AlreadyRecorded:
    """The acceptance ID was saved before: its recorded outcome, and the selected keys that save
    didn't cover, to offer again under a fresh ID. Nothing was written."""

    saved: GradeReportSaved
    uncovered: frozenset[str]


@dataclass(frozen=True)
class RecordedSave:
    """A committed save of a pasted report as recorded: its outcome, the question about the line
    and its answer, the class it went into (ID, display name, school year) and the term, and the
    current year and term as they stand now, None before the first setup."""

    saved: GradeReportSaved
    identity_status: IdentityStatus
    identity_answer: IdentityAnswer
    class_id: str
    class_name: str
    year: str
    term: str
    context: tuple[str, str] | None


class ReturnReason(StrEnum):
    """Why a save returned the review and wrote nothing."""

    SOURCE = "source"
    """The page reviewed another reading of the report: its capture key differs."""
    REVISION = "revision"
    """The class and term, or the class the page chose as the same, changed while the page was
    open."""
    ANSWERS = "answers"
    """An answer answers no question asked now, or a question is unanswered."""
    SELECTION = "selection"
    """A selected key is not a New value of the report now."""


@dataclass(frozen=True)
class ReviewReturned:
    """Nothing was written; the review as it reads now, under a fresh acceptance ID. For ticks
    a save can't take, the keys refused, and those of them on rows still asking."""

    review: GradeReview
    why: ReturnReason
    refused: frozenset[str] = frozenset()
    still_asking: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NotHers:
    """The answer was "Not hers": the review ends, and nothing was written."""


SaveOutcome = GradeReportSaved | AlreadyRecorded | ReviewReturned | NotHers


@dataclass(frozen=True)
class ClassRecord:
    """What the class and term reviewed hold: each target's current value, every value accepted
    for each target as compared, each result's latest observation (its evidence), the results
    stored decisions gave each row evidence, and the order of the newest current report that
    supplied or showed each target."""

    current: CurrentValues
    once_current: Mapping[str, frozenset[Compared]]
    """Each value a current report holds for each target, as compared: each was current once,
    until a newer current report replaced it. Reports kept as earlier hold none of these."""
    latest: Mapping[str, CurrentValue]
    """Each result's observation in the highest acceptance order, in any report, with its due
    date from the newest observation in any report that captured its Due cell: what matching
    reads."""
    decided: Mapping[str, frozenset[str]]
    newest: Mapping[str, int]
    explicit: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """The results the parent's explicit answers (``answer`` or ``chosen``) gave each row
    evidence, shown or accepted."""
    different: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """The candidates each stored "A different assignment" for a row evidence turned down, as
    ``rejected_text`` writes them."""


NOTHING_HELD: Final = ClassRecord(CurrentValues(None, {}, {}), {}, {}, {}, {})
"""The record of a class and term that hold no report, or of no class yet."""


@dataclass(frozen=True)
class OnRecord:
    """What her record says about one report, read under her student ID: the line, the context,
    the year, the class, the scope revision, what the class reviewed holds, the keys this
    capture's acceptance records name, each with its result, the rows its row records showed
    without accepting, each with its result, the order of the report its rest joins, and the
    candidates each of its rows' "A different assignment" turned down."""

    identity: Identity
    context: bool
    year_known: bool
    matched: str | None
    existing: tuple[tuple[str, str, int | None], ...]
    revision: int | None
    held: ClassRecord
    saved: Mapping[str, str | None]
    shown: Mapping[str, str] = field(default_factory=dict)
    joins: int | None = None
    turned_down: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """By row key, the candidates each "A different assignment" this capture's row records keep
    turned down in the class and term reviewed, as ``rejected_text`` writes them."""
    places: Mapping[tuple[str, str], tuple[int, date]] = field(default_factory=dict)
    """By report and result, the row position of each observation in the class and term
    reviewed, and the day its report was added in the household's zone."""

    def covers(self, target: str) -> bool:
        """Whether a current report newer than the one this capture's rest joins supplied or
        showed ``target``."""
        return self.joins is not None and self.held.newest.get(target, 0) > self.joins


OFFERED: Final = frozenset({ItemStatus.NEW, ItemStatus.CHANGED})
"""The statuses a save may select."""
COVERABLE: Final = OFFERED | {ItemStatus.MATCHES_EARLIER}
"""The statuses a newer current report's coverage takes precedence over, for a value its capture
hasn't saved: a capture's rest shows "Shown in a newer report", never the status its value would
have."""
CURRENT_COULD_CHANGE: Final = frozenset(
    {ItemStatus.NEW, ItemStatus.CHANGED, ItemStatus.MATCHES_EARLIER, ItemStatus.NEEDS_ANSWER}
)
"""The statuses of values a current report could make current, an answer given first."""


def _unreadable(*values: GradeValue) -> bool:
    return any(value.presence is Presence.UNREADABLE for value in values)


UNREAD: Final = frozenset({Presence.UNREADABLE, Presence.NOT_CAPTURED})
"""The presences of evidence a row's identity can't be read from."""


def captures_a_value(cells: Mapping[str, Cell]) -> bool:
    """Whether the copy captured any of a value's cells besides those that say which target it
    is."""
    return any(
        presence is not Presence.NOT_CAPTURED
        for field, (presence, _) in cells.items()
        if field not in WHICH_FIELDS
    )


def status_against(
    held: ClassRecord, target: str, cells: Mapping[str, Cell], current: CurrentValue | None
) -> ItemStatus:
    """A value's status against its target, a cell the copy didn't capture compared on neither
    side: Value not captured when no value cell is left, Saved when equal to the current value,
    Matches an earlier saved value when equal to another value a current report holds, which a
    newer one replaced, else Changed, or New with no current value. A value only reports kept
    as earlier hold was never current, so nothing replaced it: it reads Changed or New, and is
    offered."""
    if not captures_a_value(cells):
        return ItemStatus.VALUE_NOT_CAPTURED
    left_out = {
        field for field, (presence, _) in cells.items() if presence is Presence.NOT_CAPTURED
    }

    def kept(value: Compared) -> Compared:
        return tuple(one for one in value if one[0] not in left_out)

    value = kept(compared(cells))
    if current is not None and kept(compared(_as_shown(current, cells))) == value:
        return ItemStatus.SAVED
    if value in {kept(one) for one in held.once_current.get(target, frozenset())}:
        return ItemStatus.MATCHES_EARLIER
    return ItemStatus.NEW if current is None else ItemStatus.CHANGED


def _as_shown(current: CurrentValue, cells: Mapping[str, Cell]) -> Mapping[str, Cell]:
    """The current value's cells as the comparison with an incoming copy reads them. When the
    value's own observation didn't capture its Due cell and the copy did, its Due cell is the
    result's shown due date, by value and presence, blank or unreadable as captured; with no
    shown due date it stays not captured. Nothing is written, the date keeps its own report,
    and values once current are compared as they are."""
    own, incoming = current.cells.get("due"), cells.get("due")
    if (
        own is None
        or incoming is None
        or current.due is None
        or own[0] is not Presence.NOT_CAPTURED
        or incoming[0] is Presence.NOT_CAPTURED
    ):
        return current.cells
    return {**current.cells, "due": current.due.cell}


def evidence_of(value: CurrentValue) -> Evidence:
    """A result's evidence, derived from an observation of it: its category and title, and its
    due date from ``value.due`` when set, else from the observation."""
    cells = {**value.cells, **({} if value.due is None else {"due": value.due.cell})}

    def one(field: str) -> tuple[Presence, str]:
        return cells[field][0], folded(cells[field][1])

    return one("category"), one("assignment"), one("due")


def rejected_text(question: MatchQuestion) -> str:
    """The candidates "A different assignment" to ``question`` turns down, each result ID with its
    matching evidence, in one fixed form: what a ``different`` record keeps."""
    ordered = sorted(question.candidates, key=lambda candidate: candidate.result_id)
    return _key(
        *(
            [one.result_id, [[presence.value, text] for presence, text in evidence_of(one.last)]]
            for one in ordered
        )
    )


def _title(evidence: Evidence) -> tuple[Presence, str]:
    """A title as candidates are found by it: its presence and its folded text, case folded."""
    presence, text = evidence[1]
    return presence, text.casefold()


@dataclass(frozen=True)
class _Row:
    key: str
    evidence: Evidence
    cells: dict[str, Cell]
    unreadable: bool
    identified: bool
    """Its category, title and due date were read, so it can match though a value can't."""
    undated: bool
    """Its category and title were read and its due date wasn't captured: it resolves only
    through its capture's records or the parent's answer or choice, and can't be selected until
    then."""


class _Matching:
    """The rows of one report against the results of the class and term reviewed."""

    def __init__(self, rows: list[_Row], on_record: OnRecord) -> None:
        self.held = on_record.held
        self.turned_down = on_record.turned_down
        self.alike = Counter(row.evidence for row in rows)
        self.taken = {
            result
            for row in rows
            if (result := on_record.saved.get(row.key) or on_record.shown.get(row.key)) is not None
        }

    def candidates(self, row: _Row) -> list[str]:
        """Her results the row may be, among those no row of this report has resolved to: those
        with its title; for a row no title matches, those that share its category and its due
        text or its max points. A due date that wasn't read shares nothing, not even with
        another. A result another row may still be stays a candidate, since either row may be it
        until one resolves to it."""
        latest = self.held.latest
        free = {
            result: evidence_of(latest[result]) for result in latest if result not in self.taken
        }
        by_title = [result for result, seen in free.items() if _title(seen) == _title(row.evidence)]
        if by_title:
            return by_title
        most = compared({"max": row.cells["max_points"]})
        dated = row.evidence[2][0] not in UNREAD
        return [
            result
            for result, seen in free.items()
            if seen[0] == row.evidence[0]
            and (
                (dated and seen[2] == row.evidence[2])
                or compared({"max": latest[result].cells["max_points"]}) == most
            )
        ]

    def exact(self, row: _Row) -> str | None:
        """The one result whose evidence equals the row's, unique in the report, that no stored
        decision for that evidence contradicts."""
        if self.alike[row.evidence] != 1:
            return None
        latest = self.held.latest
        equal = [
            result for result in self.candidates(row) if evidence_of(latest[result]) == row.evidence
        ]
        stored = self.held.decided.get(evidence_text(row.evidence), frozenset())
        if len(equal) != 1 or not stored <= {equal[0]}:
            return None
        return equal[0]

    def reusable(self, row: _Row) -> str | None:
        """The one result every explicit answer stored for the row's evidence names, when it
        still exists, no row of this report has resolved to it, no stored decision of another
        kind names another result for that evidence, no other result's evidence equals the
        row's now, and no other row of this report has the same evidence."""
        if self.alike[row.evidence] != 1:
            return None
        text = evidence_text(row.evidence)
        answered = self.held.explicit.get(text, frozenset())
        if len(answered) != 1:
            return None
        (result,) = answered
        latest = self.held.latest
        if result not in latest or result in self.taken:
            return None
        if not self.held.decided.get(text, frozenset()) <= {result}:
            return None
        if any(
            other != result and evidence_of(seen) == row.evidence for other, seen in latest.items()
        ):
            return None
        return result

    def remembered(self, row: _Row, asked: MatchQuestion) -> bool:
        """Whether a stored "A different assignment" answers ``asked``: every one stored for the
        row's evidence turned down exactly the candidates asked now, with the same evidence, no
        stored decision names a result for that evidence, and no other row has it. For a row
        whose due date wasn't captured, only this capture's record for the same row counts."""
        if row.undated:
            return self.turned_down.get(row.key, frozenset()) == {rejected_text(asked)}
        if not row.identified or self.alike[row.evidence] != 1:
            return False
        text = evidence_text(row.evidence)
        if self.held.decided.get(text):
            return False
        return self.held.different.get(text, frozenset()) == {rejected_text(asked)}

    def question(self, row: _Row) -> MatchQuestion | None:
        """The question the row asks, or None when no result may be it."""
        found = self.candidates(row)
        if not found:
            return None
        latest = self.held.latest
        kind = QuestionKind.WHICH
        if len(found) == 1:
            seen = evidence_of(latest[found[0]])
            if _title(seen) != _title(row.evidence):
                kind = QuestionKind.RENAMED
            elif (
                row.identified
                and seen[2][0] not in UNREAD
                and seen[0] == row.evidence[0]
                and seen[2] != row.evidence[2]
            ):
                kind = QuestionKind.DUE_CHANGED
        return MatchQuestion(kind, tuple(Candidate(result, latest[result]) for result in found))


def _rows_of(draft: GradeReportDraft) -> list[_Row]:
    rows: list[_Row] = []
    for category in draft.categories:
        for row in category.rows:
            evidence = row_evidence(category.name, row)
            named = not any(presence in UNREAD for presence, _ in evidence[:2])
            rows.append(
                _Row(
                    key=row_key(category.name, row),
                    evidence=evidence,
                    cells=cells_of(RESULT_FIELDS, (category.name, *row.cells())),
                    unreadable=_unreadable(category.name, *row.cells()),
                    identified=named and evidence[2][0] not in UNREAD,
                    undated=named and evidence[2][0] is Presence.NOT_CAPTURED,
                )
            )
    return rows


def _kept(row: _Row) -> ItemStatus | None:
    """The status a row keeps while it resolves to none of her results: Couldn't read when a
    value can't be read, kept once it resolves too, else Due date not captured; None otherwise."""
    if row.unreadable:
        return ItemStatus.UNREADABLE
    if row.undated:
        return ItemStatus.DUE_NOT_CAPTURED
    return None


def _binds(answer: MatchAnswer, question: MatchQuestion) -> bool:
    """Whether ``answer`` answers ``question``: the same candidates, and one of them or none."""
    return answer.candidates == question.ids and (
        answer.result_id is None or answer.result_id in question.ids
    )


def _chooses(answer: MatchAnswer, choices: tuple[str, ...]) -> bool:
    """Whether ``answer`` chooses among ``choices``: the same results offered, and one of them."""
    return answer.chosen and answer.candidates == choices and answer.result_id in choices


def _reused(rows: list[_Row], matching: _Matching, settled: set[str]) -> dict[str, str]:
    """The rows an explicit answer is reused for, after this capture's records and the exact
    matches: a result two rows would reuse is reused by neither, and a reuse waits while a row
    left unresolved has that result among its candidates."""
    open_rows = [row for row in rows if row.key not in settled and row.identified]
    proposed = {row.key: found for row in open_rows if (found := matching.reusable(row))}
    twice = {result for result, count in Counter(proposed.values()).items() if count > 1}
    proposed = {key: result for key, result in proposed.items() if result not in twice}
    while True:
        waiting = {
            key
            for key, result in proposed.items()
            if any(
                result in matching.candidates(row)
                for row in rows
                if row.key != key and row.key not in settled and row.key not in proposed
            )
        }
        if not waiting:
            return proposed
        proposed = {key: result for key, result in proposed.items() if key not in waiting}


def _resolved_rows(
    on_record: OnRecord, rows: list[_Row], matches: Collection[MatchAnswer]
) -> tuple[tuple[ReviewItem, ...], tuple[str, ...]]:
    """Each row resolved, with the results no row resolved to before any answer applies: through
    this capture's acceptance or row records (identity only for a row it showed), by equal
    evidence, by a reused answer, by an answer bound to the question
    asked now or a choice bound to the results offered, as a new result, or with its question
    open. A row whose identity reads but a value doesn't asks as any row does and stays
    Couldn't read, a row missing only its due date asks the same way and stays Due date not
    captured until it resolves, and a row with an unreadable cell whose identity doesn't read
    asks nothing."""
    held = on_record.held
    matching = _Matching(rows, on_record)
    answered = {answer.row_key: answer for answer in matches}

    def resolved(
        row: _Row, result: str, how: MatchedHow, asked: MatchQuestion | None
    ) -> ReviewItem:
        current = held.current.results.get(result)
        if row.unreadable:
            return ReviewItem(row.key, ItemStatus.UNREADABLE, result, current, asked, how)
        status = status_against(held, result, row.cells, current)
        if row.key in on_record.saved and status in OFFERED:
            status = ItemStatus.SAVED
        covered = (
            row.key not in on_record.saved and status in COVERABLE and on_record.covers(result)
        )
        if covered:
            status = ItemStatus.COVERED
        return ReviewItem(row.key, status, result, current, asked, how, covered)

    exact = {}
    for row in rows:
        if row.key not in on_record.saved and row.key not in on_record.shown and row.identified:
            found = matching.exact(row)
            if found is not None:
                exact[row.key] = found
    matching.taken |= set(exact.values())
    reused = _reused(rows, matching, settled={*on_record.saved, *on_record.shown, *exact})
    matching.taken |= set(reused.values())
    free = tuple(result for result in held.latest if result not in matching.taken)

    def opened(row: _Row) -> ReviewItem:
        item = asking(row)
        kept = _kept(row)
        if kept is not None and item.result_id is None:
            return replace(item, status=kept)
        return item

    def asking(row: _Row) -> ReviewItem:
        asked = matching.question(row)
        choices = free if row.identified or row.undated else ()
        answer = answered.get(row.key)
        # A new result's value is compared with nothing, so it needs a value cell to be offered.
        new = ItemStatus.NEW if captures_a_value(row.cells) else ItemStatus.VALUE_NOT_CAPTURED
        if answer is not None and answer.chosen:
            if answer.result_id is not None and _chooses(answer, choices):
                return resolved(row, answer.result_id, "chosen", asked)
            answer = None
        if asked is None:
            return ReviewItem(row.key, new, choices=choices)
        if answer is not None and _binds(answer, asked):
            if answer.result_id is not None:
                return resolved(row, answer.result_id, "answer", asked)
            return ReviewItem(row.key, new, question=asked, how="answer", choices=choices)
        if matching.remembered(row, asked):
            return ReviewItem(
                row.key,
                new,
                question=asked,
                how="answer",
                choices=choices,
                remembered=True,
            )
        return ReviewItem(row.key, ItemStatus.NEEDS_ANSWER, question=asked, choices=choices)

    items = []
    for row in rows:
        saved_as = on_record.saved.get(row.key) or on_record.shown.get(row.key)
        if saved_as is not None:
            items.append(resolved(row, saved_as, "same_capture", None))
        elif row.key in exact:
            items.append(resolved(row, exact[row.key], "exact", None))
        elif row.key in reused:
            items.append(resolved(row, reused[row.key], "reused", None))
        elif row.unreadable and not (row.identified or row.undated):
            items.append(ReviewItem(row.key, ItemStatus.UNREADABLE))
        else:
            items.append(opened(row))
    resolved_rows = tuple(
        replace(item, due_not_captured=True)
        if row.cells["due"][0] is Presence.NOT_CAPTURED
        else item
        for row, item in zip(rows, items, strict=True)
    )
    return resolved_rows, free


def choice_facts(on_record: OnRecord, results: Iterable[str]) -> dict[str, ChoiceFacts]:
    """What tells each of ``results`` apart, from its newest observation and its current value."""
    held = on_record.held
    offered = {}
    for result in results:
        seen = held.latest[result]
        current = held.current.results.get(result)
        score = (current or seen).cells
        position, added = on_record.places[(seen.report_id, result)]
        offered[result] = ChoiceFacts(
            title=seen.cells["assignment"],
            category=seen.cells["category"],
            due=None if seen.due is None else seen.due.cell,
            points=score["points"],
            max_points=score["max_points"],
            report_added=added,
            report_order=seen.order,
            position=position,
        )
    return offered


def review_from(
    draft: GradeReportDraft,
    source_key: str,
    acceptance_id: str,
    on_record: OnRecord,
    matches: Collection[MatchAnswer] = (),
    *,
    complete: bool,
) -> GradeReview:
    """The review of ``draft`` against what her record says, in the report's order, with
    ``matches`` applied to the questions they answer; ``complete`` is whether its reading was."""
    held = on_record.held

    def item(key: str, cells: Mapping[str, Cell], current: CurrentValue | None) -> ReviewItem:
        if key not in on_record.saved and any(
            presence is Presence.UNREADABLE for presence, _ in cells.values()
        ):
            return ReviewItem(key, ItemStatus.UNREADABLE)
        status = status_against(held, key, cells, current)
        if key in on_record.saved and status in OFFERED:
            status = ItemStatus.SAVED
        covered = key not in on_record.saved and status in COVERABLE and on_record.covers(key)
        if covered:
            status = ItemStatus.COVERED
        return ReviewItem(key, status, current=current, covered=covered)

    header = draft.header
    term = draft.term
    captured = (term.percent.presence, term.letter.presence) != (
        Presence.NOT_CAPTURED,
        Presence.NOT_CAPTURED,
    )
    categories = tuple(
        item(
            key,
            cells_of(CATEGORY_FIELDS, (category.name, category.weight, category.average)),
            held.current.categories.get(key),
        )
        for key, category in zip(category_keys(draft), draft.categories, strict=True)
    )
    term_cells = cells_of(TERM_FIELDS, (term.percent, term.letter))
    term_item = item(TERM_KEY, term_cells, held.current.term) if captured else None
    rows, free = _resolved_rows(on_record, _rows_of(draft), matches)
    asked = [one for item in rows if item.question is not None for one in item.question.ids]
    offered = choice_facts(on_record, dict.fromkeys([*free, *asked]))
    values = (*(() if term_item is None else (term_item,)), *categories, *rows)
    return GradeReview(
        acceptance_id=acceptance_id,
        source_key=source_key,
        identity=on_record.identity,
        setup=None if on_record.context else (header.year_label, header.term_label),
        first_month=None if on_record.year_known else header.year_label,
        class_question=ClassQuestion(
            matched=on_record.matched,
            offered_name=header.class_name or header.class_code,
            existing=on_record.existing,
        ),
        revision=on_record.revision,
        term=term_item,
        categories=categories,
        rows=rows,
        use=None if on_record.joins is not None else _use_choice(held, values, rows, complete),
        offered=offered,
    )


def _use_choice(
    held: ClassRecord,
    values: tuple[ReviewItem, ...],
    rows: tuple[ReviewItem, ...],
    complete: bool,
) -> UseChoice | None:
    """The choice for the new report a save makes, or None when making it current would change
    nothing: no value it could make current, no current result whose last showing it would
    move, and no current result it could show as absent. Absence takes a complete reading with
    at least one row, every row naming a result: a copy with no rows makes no report."""
    current = held.current.results
    named = {item.result_id for item in rows if item.result_id is not None}
    resolved = bool(rows) and all(item.result_id is not None for item in rows)
    if not (
        any(item.status in CURRENT_COULD_CHANGE for item in values)
        or not named.isdisjoint(current)
        or (complete and resolved and not current.keys() <= named)
    ):
        return None
    repeats = tuple(item.key for item in values if item.status is ItemStatus.MATCHES_EARLIER)
    return UseChoice("earlier" if repeats else "current", repeats)


CLASS_NAME_LIMIT: Final = 60
"""The most characters a class name a page sends may hold, its spaces folded."""
TERM_LIMIT: Final = 20
"""The most characters a term a page confirms as current may hold, its spaces folded."""
Label = Literal["class_name", "term"]
"""A label a page sends that a limit bounds: a new class's name, or the setup's term."""


def is_current_context(setup: tuple[str, str]) -> bool:
    """Whether a year and term a parent confirms as current are a school year's label and a
    term within its limit."""
    year, term = setup
    return is_school_year(year) and 0 < len(folded(term)) <= TERM_LIMIT


def labels_too_long(answers: GradeAnswers) -> tuple[Label, ...]:
    """Which of the labels ``answers`` sends exceed their limits, offered or typed: the new
    class's name, then the setup's term."""
    over: list[Label] = []
    if answers.new_class is not None and len(folded(answers.new_class)) > CLASS_NAME_LIMIT:
        over.append("class_name")
    if answers.setup is not None and len(folded(answers.setup[1])) > TERM_LIMIT:
        over.append("term")
    return tuple(over)


def identity_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether the identity answer answers the line's own question, for the form of the line
    read now."""
    identity = review.identity
    return (
        answers.identity_form == identity.form and answers.identity in ANSWERS_FOR[identity.status]
    )


def setup_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether the setup is answered exactly when it is asked, with a school year and a term
    within its limit (the report's offered, any other allowed)."""
    if (answers.setup is None) != (review.setup is None):
        return False
    return answers.setup is None or is_current_context(answers.setup)


def first_month_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether a first month, when sent, is a month of the year asked about; none leaves it
    unconfirmed."""
    if answers.first_month is None:
        return True
    year, month = answers.first_month
    return year == review.first_month and 1 <= month <= 12


def class_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether the class has one answer when no alias matched it, and none when one did: a new
    class's name within its limit, or one of the year's classes offered."""
    question = review.class_question
    if question.matched is not None:
        return answers.new_class is None and answers.same_class is None
    if answers.same_class is not None:
        offered = {class_id for class_id, _, _ in question.existing}
        return answers.new_class is None and answers.same_class in offered
    return answers.new_class is not None and 0 < len(folded(answers.new_class)) <= CLASS_NAME_LIMIT


def answers_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether ``answers`` answer the questions ``review`` asks and no other: the line's own
    question, the setup, the year's first month and the class, each checked alone."""
    return (
        identity_asked(review, answers)
        and setup_asked(review, answers)
        and first_month_asked(review, answers)
        and class_asked(review, answers)
    )


def use_asked(review: GradeReview, use: str | None) -> bool:
    """Whether ``use`` answers the report-level choice ``review`` offers now: one of its two
    answers, or None for its default; and only None when it offers none."""
    return use is None or (review.use is not None and use in get_args(ReportUse))


def matches_asked(review: GradeReview, matches: Collection[MatchAnswer]) -> bool:
    """Whether each matching answer answers a question ``review`` asks now, bound to the same
    candidates, or chooses among the results a row offers now, at most one per row, and no two
    naming the same result."""
    by_key = {item.key: item for item in review.rows}
    rows = [answer.row_key for answer in matches]
    named = [answer.result_id for answer in matches if answer.result_id is not None]
    if len(set(rows)) != len(rows) or len(set(named)) != len(named):
        return False
    for answer in matches:
        item = by_key.get(answer.row_key)
        if item is None:
            return False
        if answer.chosen:
            if not _chooses(answer, item.choices):
                return False
        elif item.question is None or not _binds(answer, item.question):
            return False
    return True


def rows_still_asking(review: GradeReview) -> frozenset[str]:
    """The keys of the rows that still ask: an open question or choices offered, and no answer
    bound to the row, whatever status the row shows meanwhile."""
    return frozenset(
        item.key
        for item in review.rows
        if item.how is None and (item.question is not None or bool(item.choices))
    )


def item_keys(draft: GradeReportDraft) -> tuple[str, ...]:
    """The key of each value ``draft`` gives, in a review's item order: the term result when the
    copy has one, the categories, then the rows. A page names its items by position here."""
    term = draft.term
    captured = (term.percent.presence, term.letter.presence) != (
        Presence.NOT_CAPTURED,
        Presence.NOT_CAPTURED,
    )
    rows = tuple(row.key for row in _rows_of(draft))
    return (*((TERM_KEY,) if captured else ()), *category_keys(draft), *rows)


@dataclass(frozen=True)
class StillAsked:
    """The parts of a page's answers and ticks that still answer what a review asks now. A part
    dropped is None or left out, so its control shows unanswered."""

    identity: IdentityAnswer | None
    setup: tuple[str, str] | None
    first_month: tuple[str, int] | None
    new_class: str | None
    same_class: str | None
    matches: tuple[MatchAnswer, ...]
    use: ReportUse | None
    selection: frozenset[str]


def still_asked(
    review: GradeReview, answers: GradeAnswers, selection: Collection[str]
) -> StillAsked:
    """What of ``answers`` and ``selection`` still answers ``review``, each part checked alone:
    each match answer in the page's order, unless an earlier kept one took its row or result;
    each tick a save may select now."""
    matches: list[MatchAnswer] = []
    for answer in answers.matches:
        if matches_asked(review, (*matches, answer)):
            matches.append(answer)
    use = answers.use if use_asked(review, answers.use) else None
    allowed = review.ready | (review.back_to if use == "current" else frozenset())
    kept_class = class_asked(review, answers)
    return StillAsked(
        identity=answers.identity if identity_asked(review, answers) else None,
        setup=answers.setup if setup_asked(review, answers) else None,
        first_month=answers.first_month if first_month_asked(review, answers) else None,
        new_class=answers.new_class if kept_class else None,
        same_class=answers.same_class if kept_class else None,
        matches=tuple(matches),
        use=use,
        selection=frozenset(selection) & allowed,
    )
