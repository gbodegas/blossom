# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What a pasted grade report would do against her record, and what a save of it did.

Every value of a report has a key made from what it says, never from its place: the term result,
each category by its name, and each result row by its evidence (category, title and due date)
with its occurrence among the rows alike. Each value then has a status. Saved: this capture's own
acceptance records name it. Couldn't read: a cell is unreadable. Needs matching: the class and
term hold another capture's results. New: none of these. Only a New value can be selected.

The questions a review asks are the identity of the student line, the first setup, the first
month of a year not on record, and the class when no alias matches. The answers a page sends are
claims: a save holds them against the review it computes again, and any that answer no question
asked now, or a question left unanswered, saves nothing.
"""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

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


class ItemStatus(StrEnum):
    """What one value of a report is against her record."""

    NEW = "new"
    SAVED = "saved"
    UNREADABLE = "unreadable"
    NEEDS_MATCHING = "needs_matching"


@dataclass(frozen=True)
class ReviewItem:
    """One value of the report: its key, its status, and for a saved row the result it resolved
    to."""

    key: str
    status: ItemStatus
    result_id: str | None = None


@dataclass(frozen=True)
class ClassQuestion:
    """The class: the one an alias matched, or the name offered for a new class and the year's
    classes it may be the same as, each an ID, a display name and the scope revision of its
    class and term (None when they hold nothing yet)."""

    matched: str | None
    offered_name: str
    existing: tuple[tuple[str, str, int | None], ...]


@dataclass(frozen=True)
class GradeReview:
    """What a report would do: a fresh acceptance ID for the page, the questions, the scope
    revision of the class and term (None when they hold nothing yet), and each value's status in
    the report's order."""

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

    @property
    def items(self) -> tuple[ReviewItem, ...]:
        """Every value: the term result when the copy has one, the categories, then the rows."""
        return (*(() if self.term is None else (self.term,)), *self.categories, *self.rows)

    @property
    def ready(self) -> frozenset[str]:
        """The keys of the values a save may select: the New ones."""
        return frozenset(item.key for item in self.items if item.status is ItemStatus.NEW)


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
    the year and term confirmed at the first setup; the year and its first month; and the class,
    as a new one's display name or the ID of the class it is the same as, with the scope revision
    the page showed for it."""

    identity: IdentityAnswer
    identity_form: str | None
    setup: tuple[str, str] | None = None
    first_month: tuple[str, int] | None = None
    new_class: str | None = None
    same_class: str | None = None
    same_class_revision: int | None = None


@dataclass(frozen=True)
class ReviewPage:
    """What a page carries besides its answers: its acceptance ID, the revision it was built
    on, and the capture key of the report it reviewed."""

    acceptance_id: str
    revision: int | None
    source_key: str


@dataclass(frozen=True)
class GradeReportSaved:
    """A committed save: its acceptance, the report it went into (None when no value was
    accepted), its counts, and each accepted key with the result it resolved to."""

    acceptance_id: str
    report_id: str | None
    added: int
    updated: int
    already_saved: int
    left: int
    accepted: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class AlreadyRecorded:
    """The acceptance ID was saved before: its recorded outcome, and the selected keys that save
    didn't cover, to offer again under a fresh ID. Nothing was written."""

    saved: GradeReportSaved
    uncovered: frozenset[str]


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
    """Nothing was written; the review as it reads now, under a fresh acceptance ID."""

    review: GradeReview
    why: ReturnReason


@dataclass(frozen=True)
class NotHers:
    """The answer was "Not hers": the review ends, and nothing was written."""


SaveOutcome = GradeReportSaved | AlreadyRecorded | ReviewReturned | NotHers


@dataclass(frozen=True)
class OnRecord:
    """What her record says about one report, read under her student ID: the line, the context,
    the year, the class, the scope revision, whether the class reviewed holds another capture's
    report, and the keys this capture's acceptance records name, each with its result."""

    identity: Identity
    context: bool
    year_known: bool
    matched: str | None
    existing: tuple[tuple[str, str, int | None], ...]
    revision: int | None
    others: bool
    saved: Mapping[str, str | None]


def _unreadable(*values: GradeValue) -> bool:
    return any(value.presence is Presence.UNREADABLE for value in values)


def review_from(
    draft: GradeReportDraft, source_key: str, acceptance_id: str, on_record: OnRecord
) -> GradeReview:
    """The review of ``draft`` against what her record says, in the report's order."""

    def item(key: str, unreadable: bool) -> ReviewItem:
        if key in on_record.saved:
            return ReviewItem(key, ItemStatus.SAVED, on_record.saved[key])
        if unreadable:
            return ReviewItem(key, ItemStatus.UNREADABLE)
        if on_record.others:
            return ReviewItem(key, ItemStatus.NEEDS_MATCHING)
        return ReviewItem(key, ItemStatus.NEW)

    header = draft.header
    term = draft.term
    captured = (term.percent.presence, term.letter.presence) != (
        Presence.NOT_CAPTURED,
        Presence.NOT_CAPTURED,
    )
    categories = tuple(
        item(key, _unreadable(category.name, category.weight, category.average))
        for key, category in zip(category_keys(draft), draft.categories, strict=True)
    )
    rows = tuple(
        item(row_key(category.name, row), _unreadable(category.name, *row.cells()))
        for category in draft.categories
        for row in category.rows
    )
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
        term=item(TERM_KEY, _unreadable(term.percent, term.letter)) if captured else None,
        categories=categories,
        rows=rows,
    )


def is_current_context(setup: tuple[str, str]) -> bool:
    """Whether a year and term a parent confirms as current are a school year's label and a
    term."""
    year, term = setup
    return is_school_year(year) and bool(folded(term))


def answers_asked(review: GradeReview, answers: GradeAnswers) -> bool:
    """Whether ``answers`` answer the questions ``review`` asks and no other: the line's own
    question with the form of the line read now, a school year and term confirmed as current
    when the setup is asked (the report's offered, any other allowed), the first month of the
    year asked about (left unconfirmed when absent), and one answer for a class no alias matched."""
    identity = review.identity
    if answers.identity_form != identity.form:
        return False
    if answers.identity not in ANSWERS_FOR[identity.status]:
        return False
    if (answers.setup is None) != (review.setup is None):
        return False
    if answers.setup is not None and not is_current_context(answers.setup):
        return False
    if answers.first_month is not None:
        year, month = answers.first_month
        if year != review.first_month or not 1 <= month <= 12:
            return False
    question = review.class_question
    if question.matched is not None:
        return answers.new_class is None and answers.same_class is None
    if answers.same_class is not None:
        offered = {class_id for class_id, _, _ in question.existing}
        return answers.new_class is None and answers.same_class in offered
    return answers.new_class is not None and bool(folded(answers.new_class))
