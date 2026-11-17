# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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

import asyncio
import hmac
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from blossom.agent.runs import worker_to_the_end
from blossom.authored_text import TextRefused
from blossom.candidates import readings_for
from blossom.captures import CaptureNotSaved, UnreadableCapture, candidate_basis
from blossom.dependencies import ApplicationState, get_application_state
from blossom.homework_search import (
    QUERY_MAX_LENGTH,
    ResultsPage,
    found,
    page_number,
    page_of,
    terms_of,
)
from blossom.intake import (
    ANOTHER_DATE,
    CLAIMED,
    DIFFERENT,
    FOLDED,
    KEEP_DATE,
    KNOWN,
    NEW,
    NEW_WORK,
    NO_DUE_DATE,
    NOTE_MAX_LENGTH,
    REVIEW,
    TEXT_MAX_LENGTH,
    UPDATE,
    Change,
    ChangedSinceShown,
    IdentityAnswer,
    Kept,
    NotAsked,
    NotOffered,
    Pick,
    Read,
    Reading,
    TaskAnswer,
    answers_to_no_question,
    by_hand,
    by_week,
    changes_for,
    conflicting_choices,
    join_effects,
    joins_moved_from,
    keep,
    notes_apart,
    read_text,
    saved_joined_notes,
    spoken_day,
    spoken_report,
    task_said,
    tasks_moved,
    with_answers,
    with_joined,
    with_notes,
    within_a_school_year,
)
from blossom.pairing import pair
from blossom.reconciliation import CHANNEL_NAMES, SourceChannel
from blossom.routes.forms import form_of
from blossom.routes.instruction_answers import (
    CarriedAccount,
    SaidBack,
    UnsavedChoice,
    carried_accounts,
    paste_answers,
    review_key,
    said_back,
)
from blossom.routes.navigation import instructions_review_href
from blossom.routes.note_details import choice_from, choice_value
from blossom.routes.note_links import (
    NO_SUCH_PAGE,
    NOTHING_FOUND,
    QUERY_REFUSED,
    SEARCH_WORDS_NEEDED,
    FoundRow,
    row_of,
)
from blossom.routes.parent import review_page
from blossom.routes.store_refusals import RECORDS_UNREAD
from blossom.routes.student import parent_reads, viewer_of
from blossom.school_instructions import SubmittedChoice
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.intake_decisions import UnreadableDecision
from blossom.stores.project_state import Assignment, AssignmentKind, UnreadableClaim
from blossom.stores.school_instructions import UnreadableInstruction
from blossom.templating import page_templates

logger = logging.getLogger(__name__)
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
NAMED_TOO_FAR: Final = (
    "The note names {day}. It can't be saved as the due date because it's more than a year {way}."
)
"""What a note card says of a date the note names that the due-date check refuses."""
NOT_A_KIND: Final = "Choose Homework or Task."
LOOK_AGAIN: Final = (
    "The saved assignments changed since this preview, and one of these needs your answer "
    "now. Look it over again before saving."
)
NEEDS_ANSWER: Final = (
    "A question above still needs your answer. Answer it, then save; what you chose on the "
    "other cards is kept."
)
CHOOSE_INSTRUCTIONS: Final = "Choose which school instructions apply. Nothing was saved."
IDENTIFY_FIRST: Final = (
    "A card needs your answer about which homework it is before its school instructions can "
    "be chosen. Nothing was saved; the choice made on it is shown there as not saved."
)
HOMEWORK_CHANGED: Final = (
    "The saved homework or a note of hers changed since this review was shown, so nothing was "
    "saved. Look at it again before saving."
)
SELECTIONS_CHANGE: Final = (
    "Your selections change what saving would do for a card below, so nothing was saved. "
    "Check the review, then save."
)
REVIEW_NO_LONGER_MATCHES: Final = (
    "This review does not match what saving would do, so nothing was saved. "
    "Check the review, then save."
)
REVIEW_UNVERIFIED: Final = (
    "This review could not be verified, so nothing was saved. Check it again, then save."
)
CHECK_THIS_ASSIGNMENT: Final = "Check this assignment"
EDITS_IN_REVIEW: Final = (
    "Your changes to a title or due date were not reviewed, so nothing was saved. "
    "Check them, then press Review changes."
)
SENT_TWICE: Final = (
    "A choice on this review was sent twice with different answers, so nothing was saved. "
    "Choose it once, then save."
)
TICK_TWICE: Final = (
    'The choice "Save this as homework" was sent twice with different answers. Choose one.'
)
JOIN_TWICE: Final = (
    'The choice "Add this note to the assignment" was sent twice with different answers. '
    "Choose one."
)
TITLE_TWICE: Final = "The title was sent twice with different answers: {values}. Type the title."
WHEN_TWICE: Final = "The due date was sent twice with different answers: {values}. Choose one."
OTHER_TWICE: Final = (
    "The date beside Another date was sent twice with different answers: {values}. Choose one."
)
KIND_TWICE: Final = "The type was sent twice with different answers. Choose one."
SEARCH_TWICE: Final = "The search words were sent twice with different answers. Type them again."
OCCURRENCE_TWICE: Final = (
    "The answer to which it is was sent twice with different answers. Choose one."
)
IDENTITY_TWICE: Final = (
    "The answer to which homework this is was sent twice with different answers. Choose one."
)
LINK_TWICE: Final = 'The choice "Link her note" was sent twice with different answers. Choose one.'
APPLY_TWICE: Final = (
    "The choice for this instruction was sent twice with different answers. Choose one."
)
NONE_TWICE: Final = (
    'The choice "No school instruction applies now" was sent twice with different answers. '
    "Choose one."
)
UNANSWERED_TWICE: Final = (
    "A choice sent twice with different answers needs your answer, so nothing was saved. "
    "Choose it, then save."
)
IDENTITY_FORM_UNREADABLE: Final = (
    "The answers about which homework a card is could not be read, so nothing was saved. "
    "Look at them again below."
)
PICK_FORM_UNREADABLE: Final = (
    "The choice of homework already here under another name could not be read, so nothing "
    "was saved. Look at it again below."
)
PRESS_UNREADABLE: Final = (
    "That press isn't one this review offered, so nothing changed. The review is below as it was."
)
LINK_FORM_UNREADABLE: Final = (
    "The choices to link her notes could not be read, so nothing was saved. Look at them "
    "again below."
)
NOTE_UNREADABLE: Final = (
    "A note of hers ticked to link cannot be read right now, so nothing was saved. Your "
    "input is still here."
)
DECISION_UNREADABLE: Final = (
    "A saved answer about which homework a row is cannot be read right now, so nothing was "
    "saved. Your input is still here."
)
CLAIM_UNREADABLE: Final = (
    "A saved claim about a date cannot be read right now, so nothing was saved. Your input "
    "is still here."
)
INSTRUCTION_UNREADABLE: Final = (
    "The school's saved instructions for an assignment here cannot be read right now, so "
    "nothing was saved. Your input is still here."
)
INSTRUCTIONS_CONTRADICT: Final = (
    "Choose the school's instructions that apply, or that none applies, not both. Nothing was "
    "saved; your choices are still here."
)
CHANGED_SINCE_SHOWN: Final = (
    "The school's instructions saved for an assignment here changed since this review was "
    "shown, so nothing was saved. Look at them again before saving."
)
INSTRUCTION_FORM_UNREADABLE: Final = (
    "The answers about the school's instructions could not be read, so nothing was saved. "
    "Look at them again below."
)
UNREAD_HEADING: Final = "This can't be shown right now"
READ_REFUSALS: Final = (sqlite3.Error, StoreBusy, WriterBusy)
"""What a read of the household's records raises when the file, or the drafts store's own
lock, refuses it."""
STORE_REFUSED: Final = (
    "The text could not be saved: the file refused it. Nothing of it was saved; the text and "
    "the answers given on the cards are below."
)
"""A card's key is a count from the reader, in plain digits; nothing longer is one."""
ANSWER_FIELDS: Final = frozenset({"occurrence", "kind", "suggested", "shown", "chosen", "folded"})
"""The fields the review page writes beside a card, under its key: the answers, and the
page's own notes of what each select suggested and showed, what was chosen, and what was
folded into what."""
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


MADE_WITH: Final = "made_with"
"""The field where a review page writes what it was made with, signed: the answers to which
homework each card is, each question it asked about the school's instructions, each question
it asked about which homework a card is about, what it said a typed note would do, and what
it said each reading made from a teacher's note does when selected, so a save can tell what
the page asked and said from what a form claims."""


@dataclass(frozen=True)
class AskedIdentity:
    """A question a review page asked about which homework a card is about: the homework it
    listed, in order, the fingerprint of what it showed, and the creation token it made for
    different homework."""

    shown: tuple[str, ...]
    basis: str
    creation: str


@dataclass(frozen=True)
class PageMade:
    """What a review page was made with, as it signed it: the answers to which homework each
    card is, for each card that asked which of the school's instructions apply the revision
    it showed and the assignment it asked about, each question about which homework a card
    is about, what it said each typed note would do, the notes of hers it offered to link,
    and the words of each, so a page that reads no store can say which were ticked."""

    occurrences: dict[int, str] = field(default_factory=dict)
    asked: dict[int, tuple[int, str]] = field(default_factory=dict)
    identities: dict[int, AskedIdentity] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    links: dict[int, tuple[tuple[str, int, str | None], ...]] = field(default_factory=dict)
    words: dict[str, str] = field(default_factory=dict)
    offers: dict[int, str] = field(default_factory=dict)
    """The cards that offered to find homework already here under another name, with how
    each stood then."""
    found: dict[int, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    """For each card, the homework the page showed to choose from, with each one's
    fingerprint as shown."""
    query: tuple[int, str, int, tuple[int, ...]] | None = None
    """The card whose search the page showed, its words, the page of results shown, and the
    pages its Previous and Next go to, the only ones a press may ask for."""
    picks: dict[int, Pick] = field(default_factory=dict)
    """Homework chosen under another name on each card, as the page showed it."""
    unsaved_picks: dict[int, tuple[str, str]] = field(default_factory=dict)
    """Homework chosen on each card that wasn't taken, with why, as the page said it, so later
    pages say it again until the card takes a choice or an answer."""
    tasks: dict[int, str] = field(default_factory=dict)
    """For each reading made from a teacher's note, whether the page showed it ticked, then
    what it does when selected, as ``task_said`` puts it."""
    edits: dict[int, tuple[str, str]] = field(default_factory=dict)
    """For each reading made from a teacher's note, the title and due date the page showed
    for it, as ``edits_of`` puts them."""
    joined: dict[tuple[int, int], bool] = field(default_factory=dict)
    """For each note joined to a card, by the card's key and the note's place, whether the
    page showed its tick ticked."""
    join_effects: dict[tuple[int, int], tuple[str, str]] = field(default_factory=dict)
    """For each note joined to a card, what the page said saving does with its tick as
    shown, then with it the other way, as ``join_effects`` puts them."""
    verified: bool = field(default=True, compare=False)
    """Whether the form carried this process's signature for its draft. A form without one,
    such as a page made before the application started again, is read as a page that asked
    and said nothing."""


def made_with_field(
    key: bytes,
    draft: Mapping[str, str],
    occurrences: Mapping[int, str] | None,
    asked: Mapping[int, tuple[int, str]] | None = None,
    identities: Mapping[int, AskedIdentity] | None = None,
    notes: Mapping[int, str] | None = None,
    links: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    words: Mapping[str, str] | None = None,
    offers: Mapping[int, str] | None = None,
    found_rows: Mapping[int, Sequence[tuple[str, str]]] | None = None,
    query: tuple[int, str, int, tuple[int, ...]] | None = None,
    picks: Mapping[int, Pick] | None = None,
    unsaved_picks: Mapping[int, tuple[str, str]] | None = None,
    tasks: Mapping[int, str] | None = None,
    edits: Mapping[int, tuple[str, str]] | None = None,
    joined: Mapping[tuple[int, int], bool] | None = None,
    join_effects: Mapping[tuple[int, int], tuple[str, str]] | None = None,
) -> str:
    """What a page was made with, and a check signed with the running process's key that
    ties it to the page's draft. What the page said of the readings made from teachers'
    notes (``tasks``), and the title and due date it showed for each (``edits``), are
    written only when there are such readings; whether each note joined to a card was shown
    ticked (``joined``), and what saving does with its tick either way (``join_effects``),
    only when there are such notes."""
    answers = ",".join(f"{card}:{value}" for card, value in sorted((occurrences or {}).items()))
    context = {
        "asked": sorted([card, *question] for card, question in (asked or {}).items()),
        "identities": sorted(
            [card, list(question.shown), question.basis, question.creation]
            for card, question in (identities or {}).items()
        ),
        "notes": sorted([card, outcome] for card, outcome in (notes or {}).items()),
        "links": sorted(
            [card, [list(note) for note in offered]] for card, offered in (links or {}).items()
        ),
        "words": sorted([name, text] for name, text in (words or {}).items()),
        "offers": sorted([card, situation] for card, situation in (offers or {}).items()),
        "found": sorted(
            [card, [list(row) for row in rows]] for card, rows in (found_rows or {}).items()
        ),
        "query": None if query is None else [*query[:3], list(query[3])],
        "picks": sorted(
            [card, pick.target, pick.basis, pick.situation] for card, pick in (picks or {}).items()
        ),
        "unsaved_picks": sorted(
            [card, target, why] for card, (target, why) in (unsaved_picks or {}).items()
        ),
    }
    if tasks:
        context["tasks"] = sorted([card, said] for card, said in tasks.items())
        context["edits"] = sorted([card, *shown] for card, shown in (edits or {}).items())
    if joined:
        context["joined"] = sorted(
            [card, place, int(ticked)] for (card, place), ticked in joined.items()
        )
        context["join_effects"] = sorted(
            [card, place, *effects] for (card, place), effects in (join_effects or {}).items()
        )
    made = f"{answers};{json.dumps(context, separators=(',', ':'))}"
    return f"{made}.{_made_with_check(key, draft, made)}"


def made_with(key: bytes, form: Mapping[str, str], draft: Mapping[str, str]) -> PageMade:
    """What the page was made with, as it signed it. A form without that field, or with one
    this process didn't sign for this draft, is read as a page made before any answer to
    which homework a card is, one that asked nothing, and one that said nothing of a note."""
    made, _, check = form.get(MADE_WITH, "").rpartition(".")
    expected = _made_with_check(key, draft, made)
    if not hmac.compare_digest(check.encode("utf-8"), expected.encode("utf-8")):
        return PageMade(verified=False)
    answers, _, rest = made.partition(";")
    occurrences: dict[int, str] = {}
    for part in filter(None, answers.split(",")):
        card, _, value = part.partition(":")
        occurrences[int(card)] = value
    context = json.loads(rest)
    asked = {
        card: (revision, assignment) for card, revision, assignment in context.get("asked", [])
    }
    identities = {
        card: AskedIdentity(tuple(shown), basis, creation)
        for card, shown, basis, creation in context.get("identities", [])
    }
    notes = {int(card): str(outcome) for card, outcome in context.get("notes", [])}
    links = {
        int(card): tuple(
            (str(name), int(revision), None if lands is None else str(lands))
            for name, revision, lands in offered
        )
        for card, offered in context.get("links", [])
    }
    words = {str(name): str(text) for name, text in context.get("words", [])}
    offers = {int(card): str(situation) for card, situation in context.get("offers", [])}
    found_rows = {
        int(card): tuple((str(name), str(basis)) for name, basis in rows)
        for card, rows in context.get("found", [])
    }
    shown_query = context.get("query")
    query = (
        None
        if shown_query is None
        else (
            int(shown_query[0]),
            str(shown_query[1]),
            int(shown_query[2]),
            tuple(int(number) for number in shown_query[3]),
        )
    )
    picks = {
        int(card): Pick(str(target), str(basis), str(situation))
        for card, target, basis, situation in context.get("picks", [])
    }
    unsaved_picks = {
        int(card): (str(target), str(why)) for card, target, why in context.get("unsaved_picks", [])
    }
    tasks = {int(card): str(said) for card, said in context.get("tasks", [])}
    edits = {int(card): (str(title), str(due)) for card, title, due in context.get("edits", [])}
    joined = {
        (int(card), int(place)): bool(ticked) for card, place, ticked in context.get("joined", [])
    }
    join_effects = {
        (int(card), int(place)): (str(shown), str(other))
        for card, place, shown, other in context.get("join_effects", [])
    }
    return PageMade(
        occurrences,
        asked,
        identities,
        notes,
        links,
        words,
        offers,
        found_rows,
        query,
        picks,
        unsaved_picks,
        tasks,
        edits,
        joined,
        join_effects,
    )


def still_unsaved(
    made: PageMade | None,
    picks: Mapping[int, Pick],
    identities: Mapping[int, IdentityAnswer] | None,
) -> dict[int, tuple[str, str]]:
    """The choices the page said weren't taken that still stand: each stays until its card
    takes a choice or an answer about which homework it is."""
    return {
        key: unsaved
        for key, unsaved in (made or PageMade()).unsaved_picks.items()
        if key not in picks and key not in (identities or {})
    }


def identity_answers(
    fields: Iterable[tuple[str, object]], page: PageMade
) -> tuple[dict[int, IdentityAnswer], bool]:
    """The answers to which homework each card is about, as the page asked, read from every
    field as sent: each names a homework the signed question listed, or different homework,
    with the creation token the page made for that card. The second value says whether
    anything was sent that the page did not ask or give: an answer on a card it did not ask,
    a homework it did not list, a token it did not make, a token sent twice, or a field sent
    as a file, which leaves that card with no answer. Such a form is refused whole. An answer
    sent twice with the same value is that answer; sent twice with different values it leaves
    the card with no answer, and ``sent_twice`` names it at its question."""
    answers: dict[int, IdentityAnswer] = {}
    unreadable = False
    seen: dict[str, str] = {}
    bent: set[int] = set()
    for name, value in fields:
        head, _, card = name.rpartition("-")
        if head not in ("identity", "creation") or not review_key(card):
            continue
        if head == "identity" and name in seen and isinstance(value, str):
            if value != seen[name]:
                bent.add(int(card))
            continue
        if name in seen or not isinstance(value, str):
            unreadable = True
            bent.add(int(card))
            continue
        seen[name] = value
        question = page.identities.get(int(card))
        if question is None:
            unreadable = True
            continue
        if head == "creation":
            if value != question.creation:
                unreadable = True
                bent.add(int(card))
            continue
        chosen = None if value == DIFFERENT else homework_named(value)
        if value != DIFFERENT and (chosen is None or chosen not in question.shown):
            unreadable = True
            continue
        answers[int(card)] = IdentityAnswer(
            chosen, question.shown, question.basis, question.creation
        )
    return {card: answer for card, answer in answers.items() if card not in bent}, unreadable


def link_answers(
    fields: Iterable[tuple[str, object]], page: PageMade
) -> tuple[dict[int, list[tuple[str, int, str | None]]], bool]:
    """The notes of hers ticked to link, as the page offered them, read from every field as
    sent: ``link-<card>-<place>`` names the note the page signed at that place on that
    card, with the homework the card landed on when the page was made, or ``None`` where
    it asked which. The second value says whether anything was sent that the page did not
    offer, a box sent as a file among them, or one note ticked more than once, by a box
    sent twice or on more than one card, since repeated cards for one homework are shown
    as one; either refuses the form whole."""
    ticked: dict[int, list[tuple[str, int, str | None]]] = {}
    unreadable = False
    for name, value in fields:
        head, _, rest = name.partition("-")
        if head != "link":
            continue
        card, _, place = rest.partition("-")
        offered = page.links.get(int(card), ()) if review_key(card) else ()
        if value != "1" or not review_key(place) or int(place) >= len(offered):
            unreadable = True
            continue
        ticked.setdefault(int(card), []).append(offered[int(place)])
    names = [name for notes in ticked.values() for name, _, _ in notes]
    if len(names) != len(set(names)):
        unreadable = True
    return ticked, unreadable


def pick_answers(
    fields: Iterable[tuple[str, object]], page: PageMade
) -> tuple[dict[int, Pick], bool]:
    """The homework chosen under another name on each card, as the page signed it, read from
    every field as sent. Each choice the page showed comes back as ``picked-<card>`` naming
    the homework it showed; the second value says whether the form holds anything else: a
    choice changed, left out, sent twice or as a file, or one on a card the page showed none
    on. Such a form is refused whole."""
    sent: dict[int, str] = {}
    unreadable = False
    seen: set[str] = set()
    for name, value in fields:
        head, _, card = name.rpartition("-")
        if head != "picked" or not review_key(card):
            continue
        if name in seen or not isinstance(value, str):
            unreadable = True
            continue
        seen.add(name)
        sent[int(card)] = value
    for key, value in sent.items():
        pick = page.picks.get(key)
        if pick is None or pick.target != value:
            unreadable = True
    if set(page.picks) - set(sent):
        unreadable = True
    return {
        key: pick for key, pick in page.picks.items() if sent.get(key) == pick.target
    }, unreadable


def homework_named(value: str) -> str | None:
    """The homework an identity choice names, as the page writes it with ``choice_value``,
    or ``None`` for anything else."""
    read = choice_from(value)
    return read[1] if read is not None and read[0] == "same" else None


def came_from(candidate: Assignment) -> str | None:
    """Where homework an identity question lists came from, as its record says: the school
    portal or email, a family entry, or her report; ``None`` when the record doesn't say.
    The page says homework made from her note apart."""
    channel = candidate.origins.get("record")
    if channel is None:
        return None
    if channel is SourceChannel.STUDENT_REPORT:
        return "From her report."
    return f"From the {CHANNEL_NAMES[channel]}."


def _made_with_check(key: bytes, draft: Mapping[str, str], answers: str) -> str:
    # A browser sends a textarea's line breaks as CRLF and drops its first line break.
    words = {name: value.replace("\r\n", "\n").strip() for name, value in draft.items()}
    signed = json.dumps([sorted(words.items()), answers]).encode("utf-8")
    return hmac.new(key, signed, "sha256").hexdigest()[:16]


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
        if not review_key(number):
            continue
        if head == "occurrence" and value in (UPDATE, NEW_WORK, KEEP_DATE):
            occurrences[int(number)] = value
        elif head == "folded" and review_key(value):
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
        if head == "asked" and review_key(number):
            asked.add(int(number))
    return asked


def unasked_for(state: ApplicationState, read: Read) -> dict[int, set[AssignmentKind]]:
    """For each card, the types that are no choice when a form carries no note of what the
    page showed: what the page would show for it now, and what the reader suggests."""
    return {
        change.key: {change.kind, change.reading.kind}
        for change in changes_for(read.items, state.project_state, left_out=read.left_out)
    }


MAY_BE_HOMEWORK: Final = "Notes that may be homework"
MAY_BE_HOMEWORK_HELPER: Final = "These start unselected. Select any that are homework."
NO_HOMEWORK: Final = "Notes with no homework"
NO_HOMEWORK_HELPER: Final = (
    "The school's note says no homework is assigned. These start unselected."
)


@dataclass(frozen=True)
class NoteSection:
    """The cards of one kind of teacher's note that the review shows apart from the weeks:
    its heading, the line that says why none is selected from the start, and its cards."""

    label: str
    helper: str
    changes: list[Change]

    def count(self, state: str) -> int:
        """How many of the section's changes are in one state."""
        return sum(1 for change in self.changes if change.state == state)


def selected(form: Mapping[str, str], read: Read) -> Read:
    """What was read with the ticks a form sends for the readings made from teachers' notes.

    The page sends ``offered-<key>`` beside each tick it shows and ``take-<key>`` for a
    ticked one. A note reading the form says was offered is left out of the save unless
    its tick came with it; one the form does not name keeps its place from the start. A
    key that is no note reading's is ignored. A note joined to a card is selected when the
    form sends ``join-<card>-<place>`` for it, ticked; a name that is no joined note's is
    ignored.
    """
    left_out = set(read.left_out)
    for name in form:
        head, _, number = name.rpartition("-")
        if head != "offered" or not review_key(number) or int(number) >= len(read.items):
            continue
        key = int(number)
        if read.items[key].teacher_note is None:
            continue
        if form.get(f"take-{key}") == "1":
            left_out.discard(key)
        else:
            left_out.add(key)
    joined = frozenset(
        (card, place)
        for card, place in joins_sent(form)
        if card < len(read.items) and place < len(read.items[card].joined_notes)
    )
    if left_out == read.left_out and joined == read.joined:
        return read
    return replace(read, left_out=frozenset(left_out), joined=joined)


def joins_sent(form: Mapping[str, str]) -> set[tuple[int, int]]:
    """Each joined note's tick a form sends ticked, ``join-<card>-<place>``, by the card's
    key and the note's place, whatever the page showed."""
    sent: set[tuple[int, int]] = set()
    for name, value in form.items():
        head, _, rest = name.partition("-")
        card, _, place = rest.partition("-")
        if head == "join" and value == "1" and review_key(card) and review_key(place):
            sent.add((int(card), int(place)))
    return sent


def joins_moved(read: Read, page: PageMade) -> frozenset[int]:
    """The keys of the cards with a joined note whose tick, as sent, is not the one the page
    signed for it: ticked where the page showed it unticked or did not show it, or unticked
    where the page showed it ticked. A form this process did not sign showed none ticked."""
    signed = {join for join, ticked in page.joined.items() if ticked and page.verified}
    return frozenset(card for card, _ in read.joined.symmetric_difference(signed))


def joins_shown(read: Read, page: PageMade) -> dict[int, tuple[str, dict[tuple[int, int], str]]]:
    """For each card whose joined notes' ticks, as sent, differ from what a signed page
    showed, what the page said saving does with the ticks as it showed them, then, for each
    note of that card whose tick changed, what it said saving does with that one tick the
    other way: the ``join_effects`` it signed for that note."""
    signed = {join for join, ticked in page.joined.items() if ticked}
    changed = read.joined.symmetric_difference(signed)
    shown: dict[int, tuple[str, dict[tuple[int, int], str]]] = {}
    for card in sorted({card for card, _ in changed}):
        here = sorted(join for join in changed if join[0] == card)
        as_shown = page.join_effects.get(here[0], ("", ""))[0]
        shown[card] = (as_shown, {join: page.join_effects.get(join, ("", ""))[1] for join in here})
    return shown


def joins_alone(
    read: Read, joins: Mapping[int, tuple[str, Mapping[tuple[int, int], str]]], *, now: datetime
) -> dict[int, list[tuple[tuple[Reading, ...], str]]]:
    """For each card with more than one joined note's tick changed, each of those notes with
    the readings as the save makes them with that one tick changed and the card's other
    notes as the page showed them, and what the page said saving does with that tick."""
    return {
        card: [
            (with_joined(read, read.joined ^ set(each) ^ {join}, now=now).items, said)
            for join, said in each.items()
        ]
        for card, (_, each) in joins.items()
        if len(each) > 1
    }


def joined_of(read: Read) -> dict[int, tuple[int, ...]]:
    """For each card with a joined note selected, by its key, the places of those notes,
    counted from one, as a page that reads no store says them."""
    places: dict[int, list[int]] = {}
    for card, place in sorted(read.joined):
        places.setdefault(card, []).append(place + 1)
    return {card: tuple(each) for card, each in places.items()}


def ticks_of(read: Read) -> dict[int, bool]:
    """For each reading made from a teacher's note, by its key, whether it is selected."""
    return {
        key: key not in read.left_out
        for key, item in enumerate(read.items)
        if item.teacher_note is not None
    }


def with_edits(form: Mapping[str, str], read: Read, *, now: datetime, today: date) -> Read:
    """What was read with the title and due-date answers a form sends for the readings made
    from teachers' notes applied, as ``with_answers`` applies them: ``title-<key>``,
    ``when-<key>`` and ``when_other-<key>``. A note reading the form sends none of them for
    stays as read; a key that is no note reading's is ignored."""
    answers = [
        TaskAnswer(
            key,
            form.get(f"title-{key}", ""),
            form.get(f"when-{key}", ""),
            form.get(f"when_other-{key}", ""),
        )
        for key, item in enumerate(read.items)
        if item.teacher_note is not None
        and any(f"{head}-{key}" in form for head in ("title", "when", "when_other"))
    ]
    return with_answers(read, answers, now=now, today=today)


def edits_of(read: Read) -> dict[int, tuple[str, str]]:
    """For each reading made from a teacher's note, by its key, the title and due date it
    has, as a page shows and signs them: the date in year, month and day, or empty."""
    return {
        key: (item.title, "" if item.due_date is None else item.due_date.isoformat())
        for key, item in enumerate(read.items)
        if item.teacher_note is not None
    }


def edited(read: Read, page: PageMade) -> frozenset[int]:
    """The keys of the selected readings made from teachers' notes whose title or due date,
    with the form's answers applied, differs from what a signed page showed for them, or
    whose due-date answer names no date a paste can mean. A reading left out of the save
    writes nothing, so its edits give none. A form this process did not sign is checked
    elsewhere, and gives none here."""
    if not page.verified:
        return frozenset()
    return frozenset(
        key
        for key, shown in edits_of(read).items()
        if key not in read.left_out and (key in read.dates_unread or page.edits.get(key) != shown)
    )


def typing_refused(read: Read) -> tuple[int, Problem] | None:
    """The first selected reading made from a teacher's note, by key, whose typed title is
    longer than a title may be or whose due-date answer names no date a paste can mean, with
    the problem named by the field it is about and said in the entry form's own sentence:
    one that is no date, an empty box among them, or one outside a school year. A reading
    left out of the save writes nothing, so nothing typed on it is refused."""
    for answer in sorted(read.answers, key=lambda answer: answer.key):
        key = answer.key
        if key in read.left_out:
            continue
        if len(" ".join(answer.title.split())) > TITLE_MAX_LENGTH:
            return key, Problem(f"title-{key}", LONG_TITLE)
        if key in read.dates_unread:
            given = answer.other if answer.when == ANOTHER_DATE else answer.when
            day = _a_date(given, Problem(f"when-{key}", NOT_A_DUE_DATE))
            far = isinstance(day, date)
            return key, Problem(f"when-{key}", FAR_DUE_DATE if far else NOT_A_DUE_DATE)
    return None


@dataclass(frozen=True)
class NoteFields:
    """The title box and the due-date choices of one card read from a teacher's note: the
    title the box holds, each choice as a value, its words and whether it is chosen,
    whether another date is chosen, the date typed beside it, what the card says of each
    date the note gives that can't be the due date, and the dates the note leaves open that
    can, as said."""

    title: str
    choices: list[tuple[str, str, bool]]
    other_chosen: bool
    other: str
    named_too_far: tuple[str, ...] = ()
    left_open: tuple[str, ...] = ()

    @property
    def chosen(self) -> str:
        """The value of the due-date choice made, or empty for none."""
        if self.other_chosen:
            return ANOTHER_DATE
        return next((value for value, _, on in self.choices if on), "")


def note_fields(changes: Sequence[Change], read: Read, today: date) -> dict[int, NoteFields]:
    """The fields of each card read from a teacher's note, by its key. The choices are the
    date as read, each date the note leaves open, each date it names, and no due date, each
    date once. Any of these dates that the due-date check refuses, as of ``today``, is no
    choice: the card says it, with its year and why it can't be the due date. A card whose
    answers were sent shows them as typed. Otherwise the date as read is chosen, or no due
    date where none was read; on a card that lands on a row whose saved due date is none, or
    another than the one read, or whose date as read is refused, nothing is chosen."""
    sent = {answer.key: answer for answer in read.answers}
    fields: dict[int, NoteFields] = {}
    for change in changes:
        task = change.reading.teacher_note
        if task is None:
            continue
        given: dict[date, str] = {}
        if task.deadline is not None:
            given[task.deadline] = f"{spoken_day(task.deadline)}, as the note says"
        for day in task.choices:
            given.setdefault(day, spoken_day(day))
        for day in task.named:
            given.setdefault(day, f"{spoken_day(day)}, named in the note")
        too_far = tuple(day for day in given if not within_a_school_year(day, today))
        dates = {day: words for day, words in given.items() if day not in too_far}
        answer = sent.get(change.key)
        if answer is not None:
            title, when, other = answer.title, answer.when, answer.other
        else:
            title, other = read.items[change.key].title, ""
            when = NO_DUE_DATE if task.deadline is None else task.deadline.isoformat()
            row = change.on_record
            if task.deadline in too_far or (
                row is not None and (row.due_date is None or row.due_date != task.deadline)
            ):
                when = ""
        values = [day.isoformat() for day in dates]
        if when and when not in (*values, NO_DUE_DATE, ANOTHER_DATE):
            when, other = ANOTHER_DATE, when
        fields[change.key] = NoteFields(
            title,
            [
                *(
                    (day.isoformat(), words, day.isoformat() == when)
                    for day, words in dates.items()
                ),
                (NO_DUE_DATE, "No due date yet", when == NO_DUE_DATE),
            ],
            when == ANOTHER_DATE,
            other,
            tuple(
                NAMED_TOO_FAR.format(day=spoken_day(day), way="away" if day > today else "ago")
                for day in too_far
            ),
            tuple(spoken_day(day) for day in task.choices if day not in too_far),
        )
    return fields


def read_draft(state: ApplicationState, draft: Mapping[str, str]) -> Read | Problem:
    """Read what a draft holds: the pasted text, with its teachers' notes read as tasks, or
    the entry, the same way every time, as of the moment it was first read."""
    now, today = moment_of(state, draft)
    text = draft.get("text", "")
    if text.strip():
        if len(text) > TEXT_MAX_LENGTH:
            return Problem("text", TOO_LONG)
        return with_notes(read_text(text, now=now, today=today), now=now)
    return entry_from(draft, now=now, today=today)


def problem_page(
    request: Request,
    state: ApplicationState,
    draft: Mapping[str, str],
    problem: Problem,
    answers: "Sequence[AnswerKept]" = (),
) -> HTMLResponse:
    """The family page again, with the draft as it was, the failing field named, and the
    section open. When the household's records can't be read for it, the page that reads no
    store, with the draft and ``answers``, the answers given on the cards, kept."""
    try:
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
    except READ_REFUSALS as error:
        return read_refused(request, state, draft, list(answers), error)


@dataclass(frozen=True)
class AnswerKept:
    """One answer given on a review card, as unsaved input to copy: which card, whether the
    row is the same assignment or new work, the type chosen, and which of the school's
    instructions were ticked to apply, or that none does. ``unshown`` counts the
    instructions ticked by reference to a row, whose text a page that reads no store cannot
    read. ``homework`` is the id of the homework chosen as the one the card is about, or
    ``different`` says it's other homework with the same title; ``notes`` are the words of
    her notes ticked to link, as the page showed them."""

    key: str
    occurrence: str | None
    kind: str | None
    instructions: tuple[str, ...] = ()
    none_applies: bool = False
    unshown: int = 0
    homework: str | None = None
    different: bool = False
    notes: tuple[str, ...] = ()
    renamed: bool = False
    """Whether ``homework`` was found and chosen under another name."""
    unsaved_homework: str | None = None
    """The id of homework chosen on the card under another name that wasn't taken."""
    selected: bool | None = None
    """For a reading made from a teacher's note, whether it was selected to save; ``None``
    for any other card."""
    title: str | None = None
    """For a reading made from a teacher's note, the title typed in its box, as sent;
    ``None`` when none was sent."""
    due: str | None = None
    """For a reading made from a teacher's note, the due date chosen, or typed beside
    another date, as sent; ``None`` when none was sent."""
    undated: bool = False
    """Whether no due date was chosen for a reading made from a teacher's note."""
    joined: tuple[int, ...] = ()
    """For a card, the places, counted from one, of the notes joined to it that were
    selected to add to it."""


TASK_FIELDS: Final = frozenset({"title", "when", "when_other"})
"""The fields the review page writes on a card read from a teacher's note, under its key: the
title box, the due-date choice, and the date typed beside another date."""


def said_typed(answer: TaskAnswer) -> tuple[str | None, str | None, bool]:
    """The title and due date an answer for a note's card was sent with, as a page that
    reads no store says them: the title typed, or ``None`` for an empty box; the date
    chosen, or typed beside another date, or ``None``; and whether no due date was chosen."""
    undated = answer.when == NO_DUE_DATE
    given = answer.other if answer.when == ANOTHER_DATE else answer.when
    return answer.title or None, None if undated or not given else given, undated


def answers_kept(
    form: Mapping[str, str],
    instructions: Mapping[int, SubmittedChoice] | None = None,
    unsaved: Mapping[int, UnsavedChoice] | None = None,
    carried: Mapping[int, CarriedAccount] | None = None,
    page: PageMade | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    linked: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    picks: Mapping[int, Pick] | None = None,
) -> list[AnswerKept]:
    """The answers a review form carried, read by the one reader the save uses and with no
    record read: the page's own notes of what each select suggested and showed, of what was
    chosen, and of what was folded into what decide what is an answer, as they would have
    on the save. Only the fields the page writes, under a key the page could have written,
    are read; anything else the form holds is no answer and takes nothing else down. Which
    homework each card is about and the notes ticked to link are the save's own reading of
    them (``identities``, ``linked``), made against what the page signed (``page``), so
    only choices it offered, each sent once, are said back. A tick on a reading made from a
    teacher's note is read from the two fields the page writes for it: the note it says it
    offered is selected when its tick came with it. Its title and due date are read from
    the three fields the page writes for them, under a key the page could have written. A
    note joined to a card is selected when its tick came ticked."""
    safe: dict[str, str] = {}
    for name, value in form.items():
        head, _, key = name.rpartition("-")
        if head not in ANSWER_FIELDS or not review_key(key):
            continue
        if head == "folded" and not review_key(value):
            continue
        if head == "chosen" and value != "1":
            continue
        safe[name] = value
    occurrences, kinds = answers_from(safe, {})
    words = (page or PageMade()).words
    not_taken = still_unsaved(page, picks or {}, identities)
    ticks = {
        int(key): form.get(f"take-{key}") == "1"
        for name in form
        for head, _, key in [name.rpartition("-")]
        if head == "offered" and review_key(key)
    }
    typed_tasks = {
        int(key): TaskAnswer(
            int(key),
            form.get(f"title-{key}", ""),
            form.get(f"when-{key}", ""),
            form.get(f"when_other-{key}", ""),
        )
        for name in form
        for head, _, key in [name.rpartition("-")]
        if head in TASK_FIELDS and review_key(key)
    }
    joined: dict[int, list[int]] = {}
    for card, place in sorted(joins_sent(form)):
        joined.setdefault(card, []).append(place + 1)
    return answers_shown(
        occurrences,
        kinds,
        instructions,
        unsaved,
        carried,
        identities,
        linked,
        words,
        picks,
        {key: target for key, (target, _) in not_taken.items()},
        ticks,
        typed_tasks,
        {card: tuple(places) for card, places in joined.items()},
    )


def answers_sent(
    state: ApplicationState,
    form: Mapping[str, str],
    fields: Sequence[tuple[str, object]],
    draft: Mapping[str, str],
) -> list[AnswerKept]:
    """The answers a review form carried, read as the keep press reads them, for a page that
    reads no store: nothing on record is read, and only what the page wrote is an answer."""
    read_answers = paste_answers(fields)
    made = {key: UnsavedChoice.of(answer) for key, answer in read_answers.answers.items()}
    chosen = {key for key, answer in {**made, **read_answers.unsaved}.items() if answer.chooses}
    carried = {
        key: account for key, account in carried_accounts(fields).items() if key not in chosen
    }
    page_made = made_with(state.result_key, form, draft)
    identities, _ = identity_answers(fields, page_made)
    linked, _ = link_answers(fields, page_made)
    picks, _ = pick_answers(fields, page_made)
    return answers_kept(
        form,
        read_answers.answers,
        read_answers.unsaved,
        carried,
        page_made,
        identities,
        linked,
        picks,
    )


def answers_shown(
    occurrences: Mapping[int, str] | None,
    kinds: Mapping[int, AssignmentKind] | None,
    instructions: Mapping[int, SubmittedChoice] | None = None,
    unsaved: Mapping[int, UnsavedChoice] | None = None,
    carried: Mapping[int, CarriedAccount] | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    linked: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    words: Mapping[str, str] | None = None,
    picks: Mapping[int, Pick] | None = None,
    unsaved_picks: Mapping[int, str] | None = None,
    ticks: Mapping[int, bool] | None = None,
    typed_tasks: Mapping[int, TaskAnswer] | None = None,
    joined: Mapping[int, tuple[int, ...]] | None = None,
) -> list[AnswerKept]:
    """The answers a review page was made with, in the same shape: an answer about the
    school's instructions by the words ticked, read off the wire before anything was
    written, so a page that reads no store can still say them; what a card whose answer
    could not be read chose, the same way; an instruction ticked by reference to a row,
    counted; a choice a card carries from a page that showed it as not saved, every answer
    of it together; which homework the card was said to be about; her notes ticked to
    link, in the words the page signed; homework chosen under another name that wasn't
    taken, by id; and for each reading made from a teacher's note, whether it was selected
    to save (``ticks``) and the title and due date sent for it (``typed_tasks``); and for
    each card, the notes joined to it that were selected to add to it (``joined``)."""
    made = {key: UnsavedChoice.of(answer) for key, answer in (instructions or {}).items()}
    made.update(unsaved or {})
    chosen = {
        key: SaidBack(tuple(sorted(answer.applies)), len(answer.rows), answer.none_applies)
        for key, answer in made.items()
        if answer.chooses
    }
    for key, account in (carried or {}).items():
        answers = account.said(None)
        chosen.setdefault(
            key,
            SaidBack(
                tuple(sorted({text for said in answers for text in said.words})),
                sum(said.unshown for said in answers),
                any(said.none_applies for said in answers),
            ),
        )
    chosen = {
        key: said for key, said in chosen.items() if said.words or said.unshown or said.none_applies
    }
    identities = identities or {}
    linked = linked or {}
    picks = picks or {}
    unsaved_picks = unsaved_picks or {}
    ticks = ticks or {}
    joined = joined or {}
    as_typed = {key: said_typed(answer) for key, answer in (typed_tasks or {}).items()}
    keys = sorted(
        {
            *(occurrences or {}),
            *(kinds or {}),
            *chosen,
            *identities,
            *linked,
            *picks,
            *unsaved_picks,
            *ticks,
            *as_typed,
            *joined,
        }
    )
    return [
        AnswerKept(
            str(key),
            (occurrences or {}).get(key),
            None if kinds is None or key not in kinds else kinds[key].value,
            chosen[key].words if key in chosen else (),
            key in chosen and chosen[key].none_applies,
            chosen[key].unshown if key in chosen else 0,
            picks[key].target
            if key in picks
            else None
            if key not in identities
            else identities[key].choice,
            key in identities and identities[key].choice is None and key not in picks,
            tuple((words or {}).get(name, "") for name, _, _ in linked.get(key, ())),
            key in picks,
            unsaved_picks.get(key),
            ticks.get(key),
            *as_typed.get(key, (None, None, False)),
            joined.get(key, ()),
        )
        for key in keys
    ]


@dataclass(frozen=True)
class SearchShown:
    """The search a review press asks for on one card: its words, the page of results, and a
    sentence to say in place of results or above them, when there's one."""

    key: int
    words: str
    number: int = 1
    problem: str | None = None
    attempted: str | None = None
    """Homework a press chose that changed or left since the page showed it, shown again as
    it stands, whether or not the words still find it."""


@dataclass(frozen=True)
class UnsavedPick:
    """A choice of homework on a card that wasn't taken: the homework as it stands now, or
    ``None`` when it's gone; why (``stale`` at a save, ``changed`` at a Choose); and whether
    the card can take it now, so the page offers a press only where one can succeed."""

    row: FoundRow | None
    why: str
    can_choose: bool


@dataclass(frozen=True)
class FoundView:
    """One card's search as the page shows it: the words, the page of results, each result
    as a row to choose, and what to say, a problem or a hint, when there is something."""

    key: int
    words: str
    results: ResultsPage | None
    rows: tuple[FoundRow, ...]
    said: str | None
    problem: bool


def found_view(
    state: ApplicationState, change: Change, search: SearchShown, *, parent: bool
) -> FoundView:
    """The homework the words find for a card, twenty to a page, by the search's rules:
    everything on record by its class or title, Done and older work included, except
    homework with the card's own class and title, which the card's question already lists.
    One reading of the rows serves the page and the fingerprints its choices carry."""
    try:
        terms = terms_of(search.words)
    except TextRefused:
        return FoundView(search.key, search.words, None, (), QUERY_REFUSED, problem=True)
    if not terms:
        hint = search.problem or SEARCH_WORDS_NEEDED
        return FoundView(
            search.key, search.words, None, (), hint, problem=search.problem is not None
        )
    store = state.project_state
    rows = [
        item
        for item in store.all_assignments()
        if pair(item.course, item.title) != change.reading.pair
    ]
    results = page_of(found(rows, terms), search.number)
    if results is None:
        return FoundView(search.key, search.words, None, (), NO_SUCH_PAGE, problem=True)
    shown = tuple(row_of(item, parent=parent) for item in readings_for(store, list(results.items)))
    said = search.problem or (NOTHING_FOUND if results.total == 0 else None)
    return FoundView(
        search.key, search.words, results, shown, said, problem=search.problem is not None
    )


@dataclass(frozen=True)
class UnsavedAccount:
    """What a card says back of answers about the school's instructions that were not saved:
    why, each answer as the page says it, and where the assignment's instructions can be
    reviewed when no question about them is put."""

    why: str
    said: tuple[SaidBack, ...]
    review: str | None = None


def unsaved_on(
    changes: Sequence[Change],
    unsaved: Mapping[int, UnsavedChoice],
    carried: Mapping[int, CarriedAccount] | None = None,
) -> dict[int, UnsavedAccount]:
    """What each card shown says back as the parent's unsaved choice, and why, whether or not
    it puts the question now: what a card whose answer could not be read chose, answers made
    against instructions that changed since, and answers that ask for different things, in
    words, with the kept instructions this reading found. A choice made on a card before it
    was known which homework the card is, given now or carried from before, is said with no
    kept instruction read for it, since no row it names can be placed. A card with nothing
    new to say shows again the choice it carries from the page before, with its reason, and
    the choice each card folded into it carries. Where the assignment's question is not
    put, the account names the page that reviews its instructions."""
    carriers = {
        change.assignment_id: change for change in changes if change.instructions is not None
    }
    folded: dict[int, list[int]] = {}
    for change in changes:
        if change.folded_into is not None:
            folded.setdefault(change.folded_into, []).append(change.key)
    said: dict[int, UnsavedAccount] = {}
    for change in changes:
        if change.state == FOLDED:
            continue
        carrier = None if change.ambiguous else carriers.get(change.assignment_id)
        kept = None if carrier is None else carrier.instructions_kept
        # A folded card isn't shown, so what couldn't be read on it is said on its anchor.
        unread = [key for key in (change.key, *folded.get(change.key, ())) if key in unsaved]
        if unread:
            why = "unreadable"
            answers = [said_back(unsaved[key], kept) for key in unread]
        elif change.ambiguous:
            why = "waiting"
            answers = [
                said_back(UnsavedChoice.of(answer), None) for answer in change.instructions_unsaved
            ]
        else:
            why = "stale" if change.instructions_stale else "differ"
            answers = [
                said_back(UnsavedChoice.of(answer), change.instructions_kept)
                for answer in change.instructions_unsaved
            ]
        accounts = tuple(account for account in answers if account is not None)
        if not accounts and carried is not None:
            # A folded card isn't shown, so the choice it carries is shown on its anchor.
            keys = (change.key, *folded.get(change.key, ()))
            held = [carried[key] for key in keys if key in carried]
            if held:
                why = held[0].why
                accounts = tuple(said for account in held for said in account.said(kept))
        if not accounts:
            continue
        review = None
        if carrier is not None and carrier.instructions_kept and not carrier.instructions_question:
            review = instructions_review_href(carrier.assignment_id)
        said[change.key] = UnsavedAccount(why, accounts, review)
    return said


def intake_unavailable(
    request: Request,
    state: ApplicationState,
    draft: Mapping[str, str],
    answers: list[AnswerKept],
    problem: str = CLAIM_UNREADABLE,
    *,
    unread: bool = False,
) -> HTMLResponse:
    """The page for a review or a save refused because a claim about a date on record, or a
    saved instruction of the school's, cannot be read; with ``unread``, for a press whose read
    of the household's records was refused, headed that it can't be shown, with 503. It reads
    no store, tries nothing again, and says nothing was saved; with ``unread``, its own
    sentence says nothing about a save, though answers kept are still labeled not saved.
    It keeps the text as pasted, or the entry as typed, and the answers given on the
    cards, to copy. The way on is a press through the same reading, never a retry made here:
    the paste's for a draft with a text field, even an empty one, else the entry's."""
    text = draft.get("text") or None
    entry = None
    if text is None:
        entry = {name: draft.get(name, "") for name in ENTRY_FIELDS}
        entry["kind"] = dict(KIND_CHOICES).get(entry["kind"], entry["kind"])
    return templates.TemplateResponse(
        request,
        "inbox_unavailable.html",
        {
            "heading": UNREAD_HEADING if unread else "Nothing was saved",
            "problem": problem,
            "text": text,
            "entry": entry,
            "entry_fields": ENTRY_FIELDS,
            "answers": answers,
            "again": "/parent/inbox/read" if "text" in draft else "/parent/inbox/enter",
            "draft": {
                name: value for name, value in draft.items() if name in (*ENTRY_FIELDS, "text")
            },
            "marks": state.settings.page_marks,
        },
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE if unread else status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
    )


def read_refused(
    request: Request,
    state: ApplicationState,
    draft: Mapping[str, str],
    answers: list[AnswerKept],
    error: sqlite3.Error | StoreBusy | WriterBusy,
) -> HTMLResponse:
    """The page for a press whose read of the household's records was refused, busy or not,
    with what was sent kept; the refusal is logged by its kind alone, never its words."""
    logger.warning("an inbox page could not be read: %s", type(error).__name__)
    return intake_unavailable(request, state, draft, answers, RECORDS_UNREAD, unread=True)


def first_review(
    request: Request, state: ApplicationState, read: Read, draft: Mapping[str, str]
) -> HTMLResponse:
    """What a paste or an entry was read as, before any answer is given on its cards; when the
    household's records can't be read for its review, the page that reads no store, with the
    text as pasted or the entry as typed. A note left out from the start that is saved
    already is shown selected, as it was saved."""
    try:
        return preview_page(request, state, saved_selected(state, read), draft)
    except READ_REFUSALS as error:
        return read_refused(request, state, draft, [], error)


def saved_selected(state: ApplicationState, read: Read) -> Read:
    """What was read, with each reading made from a teacher's note that is left out from the
    start but saved already selected: a paste read again shows a note the family selected
    and saved as saved, and leaves out only the notes not saved. So does each note joined
    to a card that the record shows saved where the card lands (``saved_joined_notes``),
    which selecting adds nothing to. A record that cannot be read leaves the paste as it
    is, for the review to answer."""
    if not read.left_out and not any(item.joined_notes for item in read.items):
        return read
    try:
        changes = changes_for(read.items, state.project_state, left_out=frozenset())
        joined = saved_joined_notes(read.items, changes, state.project_state)
    except (UnreadableClaim, UnreadableDecision, UnreadableInstruction):
        return read
    saved = {change.key for change in changes if change.state == KNOWN} & read.left_out
    if not saved and joined <= read.joined:
        return read
    return replace(read, left_out=read.left_out - saved, joined=read.joined | joined)


def preview_page(
    request: Request,
    state: ApplicationState,
    read: Read,
    draft: Mapping[str, str],
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    instruction_answers: Mapping[int, SubmittedChoice] | None = None,
    unsaved: Mapping[int, UnsavedChoice] | None = None,
    carried: Mapping[int, CarriedAccount] | None = None,
    made: PageMade | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    linked: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    picks: Mapping[int, Pick] | None = None,
    search: SearchShown | None = None,
    typed: Mapping[int, str] | None = None,
    focus: str | None = None,
    refused: str | None = None,
    check: Collection[int] | None = None,
    typing: tuple[int, Problem] | None = None,
    editing: Collection[int] = frozenset(),
    twice: Mapping[str, tuple[str, ...]] | None = None,
    unresolved: Mapping[str, tuple[str, ...]] | None = None,
    notice: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """What was read, week by week against the record as it is, with the way to save it.
    A card that could be about more than one homework, or about homework made from her
    note that no parent has settled yet, asks which. Each such question gets a creation
    token for different homework, made once: a page returned keeps the token its form was
    signed with (``made``), and an answer in ``identities`` that still holds lands its card
    and rides along to the save. A claim on record that cannot be read refuses the
    comparison, and the page that reads no store keeps the draft.

    A reading made from a teacher's note carries a tick, set unless ``read.left_out`` holds
    its key; so does each note joined to a card, set when ``read.joined`` holds it, and the
    page signs each as shown. The notes that may be homework and the notes that say none is
    assigned are shown after the weeks, each kind under its own heading, and every count is
    of the selected readings only. A note not selected that would ask, once selected, whether it is
    the same assignment as a card of its name in the text asks it on its card, with both
    dates, and an answer given there stays chosen.

    ``refused`` says the page comes back for an answer about the school's
    instructions that was not saved: ``contradict``, ``malformed``, ``stale``,
    ``unanswered``, a question left without an answer, or ``waiting``, a
    choice made on a card that now asks which homework it is. ``unsaved`` is
    what each card whose answer could not be read chose, said back on its card
    as the parent's unsaved choice, whether or not the card puts the question
    now, with nothing ticked; ``carried`` is each choice a card carries from a
    page that showed it as not saved, shown again the same way. When an
    answer on any card was made against instructions that changed since, from
    this reading of the record, the page says so first, with a 409, whatever
    else was wrong. The summary of a refusal takes the focus and links to the
    first question it is about, or, where that card puts no question now, to
    the choice said back on it.

    A card that asks, or that would save new homework, offers to find homework
    already here under another name. ``search`` is the search one press asked
    for, shown under its card with the words ``typed`` on each card kept;
    ``picks`` is the homework chosen on cards, shown under both names, and one
    made against a card or a homework that has changed since is said as not saved, with a
    way to choose it again where the card still offers it. The page signs each choice it
    says wasn't taken, and pages after it say it again until its card takes a choice or an
    answer. ``focus`` names what takes the focus when no refusal is said first.

    ``check`` holds the cards a refusal is about when it is about what the page said of
    the readings made from teachers' notes. The summary then links to no question; it
    links to the card, with an id given to that card for it, only when there is exactly
    one and the page shows it. ``typing`` holds the card and the problem of a title or due
    date typed on a note that a refusal is about; the summary then links to that field
    when the page shows its card.

    Each note's card shows its title and due date as reviewed text, with an Edit details
    press, unless ``editing`` holds its key: then the card shows its title box and due-date
    choices, and the page offers Review changes and no Save. ``twice`` holds each field sent
    twice with different answers, with the values sent: a note's tick, title box, due date or
    joined note's tick says so at that field (``twice_said``), and a card with another of its
    fields sent twice says so above its fields. ``unresolved`` holds each note field sent
    twice and not answered since, with the values sent: its control offers the actual
    choice with nothing chosen for the family, and the page carries a mark of it with those
    values (``unresolved-<field>``) until it is answered. The page signs the title and
    due date of the cards it shows as reviewed only, so a save sent from a page with details
    open writes nothing until Review changes shows them."""
    picks = picks or {}
    held = still_unsaved(made, picks, identities)
    if search is not None and search.attempted is not None:
        held[search.key] = (search.attempted, "changed")
    not_taken = {key: target for key, (target, _) in held.items()}
    joined_here = [
        (key, place)
        for key, item in enumerate(read.items)
        for place in range(len(item.joined_notes))
    ]
    now = moment_of(state, draft)[0]
    if joined_here:
        # Each card carries exactly what its selected joined notes add, as the save applies it.
        read = with_joined(read, read.joined, now=now)
    join_said: dict[tuple[int, int], tuple[str, str]] = {}
    join_labels: dict[tuple[int, int], str] = {}
    try:
        changes = changes_for(
            read.items,
            state.project_state,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            identities=identities,
            picks=picks,
            left_out=read.left_out,
        )
        # What each note not selected does once it is selected is read from a second
        # comparison with every note selected, so its card can say it and the page sign it.
        # An answer given on a note not selected is left out of it, so the card that asks
        # the question keeps asking it until the note is selected.
        if_selected = changes
        if read.left_out:
            if_selected = changes_for(
                read.items,
                state.project_state,
                occurrences={
                    key: value
                    for key, value in (occurrences or {}).items()
                    if key not in read.left_out
                },
                kinds=kinds,
                instruction_answers=instruction_answers,
                identities=identities,
                picks=picks,
                left_out=frozenset(),
            )
        # What saving does with each joined note's tick the other way is read from one more
        # comparison for each, so its card can say it and the page sign it beside the tick as
        # shown: a tick changed and pressed with Save then saves in one press.
        for join in joined_here:
            other = changes_for(
                with_joined(read, read.joined ^ {join}, now=now).items,
                state.project_state,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                identities=identities,
                picks=picks,
                left_out=read.left_out,
            )
            join_said[join] = join_effects(changes, other, join[0])
            join_labels[join] = next(
                (change.label for change in other if change.key == join[0]), ""
            )
    except UnreadableClaim:
        return intake_unavailable(
            request,
            state,
            draft,
            answers_shown(
                occurrences,
                kinds,
                instruction_answers,
                unsaved,
                carried,
                identities,
                linked,
                (made or PageMade()).words,
                picks,
                not_taken,
                ticks_of(read),
                {answer.key: answer for answer in read.answers},
                joined_of(read),
            ),
        )
    except UnreadableDecision:
        return intake_unavailable(
            request,
            state,
            draft,
            answers_shown(
                occurrences,
                kinds,
                instruction_answers,
                unsaved,
                carried,
                identities,
                linked,
                (made or PageMade()).words,
                picks,
                not_taken,
                ticks_of(read),
                {answer.key: answer for answer in read.answers},
                joined_of(read),
            ),
            DECISION_UNREADABLE,
        )
    except UnreadableInstruction:
        return intake_unavailable(
            request,
            state,
            draft,
            answers_shown(
                occurrences,
                kinds,
                instruction_answers,
                unsaved,
                carried,
                identities,
                linked,
                (made or PageMade()).words,
                picks,
                not_taken,
                ticks_of(read),
                {answer.key: answer for answer in read.answers},
                joined_of(read),
            ),
            INSTRUCTION_UNREADABLE,
        )
    when_selected = {change.key: change for change in if_selected}
    # A note not selected that, once selected, asks whether it is the same assignment as a
    # card of its name in this text puts that question on its card before it is selected,
    # with both dates.
    cards_here = {
        change.reading.pair
        for change in changes
        if change.reading.teacher_note is None and not change.left_out
    }
    unselected_asks = {
        change.key: when_selected[change.key]
        for change in changes
        if change.left_out
        and change.reading.pair in cards_here
        and when_selected[change.key].ambiguous
        and when_selected[change.key].on_record is None
    }
    tasks = {
        change.key: f"{0 if change.left_out else 1}:{task_said(would)}"
        for change in changes
        if change.reading.teacher_note is not None
        for would in [when_selected[change.key] if change.left_out else change]
    }
    said = unsaved_on(changes, unsaved or {}, carried)
    problem_target = None
    problem_link = None
    if refused is not None and check is None and typing is None:
        stale = [change.key for change in changes if change.instructions_stale]
        asked = [change.key for change in changes if change.instructions_question]
        unanswered = [change.key for change in changes if change.instructions_asked]
        unreadable = [key for key, account in said.items() if account.why == "unreadable"]
        which = [change.key for change in changes if change.ambiguous]
        identity = [change.key for change in changes if change.identity_asked]
        held_back = [key for key in which if key in said]
        contradicting = [
            change.key
            for change in changes
            if change.instructions_submitted is not None
            and change.instructions_submitted.contradicts
        ]
        target = None
        if stale:
            notice, status_code = CHANGED_SINCE_SHOWN, status.HTTP_409_CONFLICT
            target = stale[0]
        elif refused == "contradict" and contradicting:
            target = contradicting[0]
        elif refused == "unanswered" and unanswered:
            target = unanswered[0]
        elif refused == "malformed" and unreadable:
            target = unreadable[0]
        elif refused == "waiting" and held_back:
            target = held_back[0]
        elif refused in ("identify", "stale") and identity:
            target = identity[0]
        elif asked:
            target = asked[0]
        # The link names only what the page shows: the question, the question of which
        # homework a card is, or the choice said back on a card that puts neither now.
        if target in asked:
            problem_target = target
            problem_link = (f"instructions-question-{target}", "Go to the question.")
        elif target in which:
            problem_target = target
            problem_link = (f"occurrence-question-{target}", "Go to the question.")
        elif target in identity:
            problem_target = target
            problem_link = (f"identity-question-{target}", "Go to the question.")
        elif target in said:
            problem_link = (f"instructions-unsaved-{target}", "See the choice not saved.")
    if notice is None and conflicting_choices(changes):
        notice = CHOOSE_ONE_TYPE
    shown = [change for change in changes if change.state != FOLDED]
    in_weeks, may_be, none_assigned = notes_apart(changes)
    note_sections = (
        NoteSection(MAY_BE_HOMEWORK, MAY_BE_HOMEWORK_HELPER, may_be),
        NoteSection(NO_HOMEWORK, NO_HOMEWORK_HELPER, none_assigned),
    )
    parent = parent_reads(request)
    view = None
    choosable: dict[int, list[FoundRow]] = {}
    cards = {change.key: change for change in shown}
    check_card = None
    if check is not None and len(check) == 1 and next(iter(check)) in cards:
        check_card = next(iter(check))
        problem_link = (f"card-{check_card}", CHECK_THIS_ASSIGNMENT)
    if typing is not None and typing[0] in cards:
        problem_link = (typing[1].field, CHECK_THIS_ASSIGNMENT)
    if search is not None and search.key in cards and cards[search.key].offers:
        view = found_view(state, cards[search.key], search, parent=parent)
        choosable[search.key] = list(view.rows)
        if focus is None and refused is None:
            focus = f"search-results-{search.key}" if view.rows else f"search-hint-{search.key}"
    # A choice not taken, one the save refused, one a Choose found changed, or one an earlier
    # page said that still stands, is said as not saved with the homework as it stands now,
    # a new one on a card in place of the earlier. It's offered again only where the card
    # offers the search now, the homework has another name than the card's, and the card's
    # own results don't offer it already. Results shown on another card don't count.
    unsaved_picks: dict[int, UnsavedPick] = {}
    wanted = {change.key: (change.pick_refused, "stale") for change in shown if change.pick_refused}
    for key, not_taken_here in held.items():
        if key in cards:
            wanted.setdefault(key, not_taken_here)
    for key, (name, why) in wanted.items():
        row = state.project_state.one_assignment(name)
        item = None
        if row is not None:
            item = row_of(readings_for(state.project_state, [row])[0], parent=parent)
        card = cards[key]
        can_choose = (
            row is not None
            and item is not None
            and bool(card.offers)
            and pair(row.course, row.title) != card.reading.pair
            and name not in {listed.row.assignment_id for listed in choosable.get(key, ())}
        )
        unsaved_picks[key] = UnsavedPick(item, why, can_choose)
        if can_choose and item is not None:
            choosable.setdefault(key, []).append(item)
    picked = {change.key: picks[change.key] for change in shown if change.picked}
    # A card the parent folded carries that answer on the page; a report shown on another
    # card carries nothing.
    folded = [
        change
        for change in changes
        if change.state == FOLDED and change.occurrence in (UPDATE, KEEP_DATE)
    ]
    # The note cards whose details are open: shown cards only, as a folded card has none.
    opened = frozenset(editing) & {
        change.key
        for change in changes
        if change.reading.teacher_note is not None
        and change.key not in {other.key for other in folded}
    }
    counts = {
        "new": sum(1 for change in shown if change.state == NEW),
        "updated": sum(1 for change in shown if change.state == CLAIMED),
        "unchanged": sum(1 for change in shown if change.state == KNOWN),
        "review": sum(1 for change in shown if change.state == REVIEW),
    }
    to_save = counts["new"] + counts["updated"] + counts["review"]
    before = made or PageMade()

    def token(key: int) -> str:
        """The creation token the form was signed with for this card, or a new one."""
        return (
            before.identities[key].creation if key in before.identities else secrets.token_hex(16)
        )

    # An answer that landed its card rides along, signed as its question was, so a page
    # returned for another question still has it at the save.
    given = identities or {}
    answered = {
        change.key: given[change.key]
        for change in shown
        if change.key in given and not change.identity_asked and not change.identity_stale
    }
    signed = {
        **{
            change.key: AskedIdentity(
                change.identity_shown, change.identity_basis, token(change.key)
            )
            for change in shown
            if change.identity_asked
        },
        **{
            key: AskedIdentity(answer.shown, answer.basis, token(key))
            for key, answer in answered.items()
        },
    }
    return templates.TemplateResponse(
        request,
        "inbox_preview.html",
        {
            "weeks": by_week([change for change in changes if not change.left_out]),
            "sections": [
                *by_week(in_weeks),
                *(section for section in note_sections if section.changes),
            ],
            "changes": shown,
            "folded": folded,
            "counts": counts,
            "to_save": to_save,
            "unread": read.unread,
            "draft": draft,
            "made_with": made_with_field(
                state.result_key,
                draft,
                occurrences,
                {
                    change.key: (change.instructions_revision, change.assignment_id)
                    for change in shown
                    if change.instructions_question
                },
                signed,
                {
                    change.key: change.note_shown
                    for change in shown
                    if change.note_shown is not None
                },
                {
                    change.key: tuple(
                        (
                            note.capture_id,
                            note.revision,
                            None if change.lands_nowhere else change.assignment_id,
                        )
                        for note in change.waiting
                    )
                    for change in shown
                    if change.waiting
                },
                {note.capture_id: note.text for change in shown for note in change.waiting},
                {change.key: change.offers for change in shown if change.offers},
                {
                    key: [(item.row.assignment_id, item.basis) for item in rows]
                    for key, rows in choosable.items()
                },
                None
                if view is None
                else (
                    view.key,
                    view.words,
                    view.results.number if view.results is not None else 1,
                    tuple(
                        number
                        for number in (
                            (view.results.previous, view.results.next)
                            if view.results is not None
                            else ()
                        )
                        if number is not None
                    ),
                ),
                picked,
                wanted,
                tasks,
                {key: shown for key, shown in edits_of(read).items() if key not in opened},
                {
                    (change.key, place): (change.key, place) in read.joined
                    for change in changes
                    for place in range(len(change.reading.joined_notes))
                },
                join_said,
            ),
            "joined_ticked": read.joined,
            "join_labels": join_labels,
            "note_tasks": bool(tasks),
            # The notes read, told apart from the ones selected to save.
            "notes_found": (
                len(tasks) + sum(len(change.reading.joined_notes) for change in changes),
                sum(
                    1
                    for change in changes
                    if change.reading.teacher_note is not None and not change.left_out
                )
                + sum(
                    1
                    for change in changes
                    for place in range(len(change.reading.joined_notes))
                    if (change.key, place) in read.joined
                ),
            ),
            "note_fields": note_fields(changes, read, moment_of(state, draft)[1]),
            "editing": opened,
            "twice_said": twice_said(twice or {}),
            "unresolved": unresolved_at(unresolved or {}),
            # A folded note's card with a carried answer sent twice is shown again so that
            # field can be answered, the answer that folded it kept as given.
            "folded_open": {change.key for change in folded}
            & {
                int(which.split("-")[1])
                for which in unresolved or {}
                if which.split("-")[0] in ("take", "join", *BOX_FIELDS)
            },
            # The answer to which it is of a note whose tick waits for the family's answer,
            # carried as given, so the answer that folded it stays.
            "occurrence_kept": {
                key: value
                for key, value in (occurrences or {}).items()
                if f"take-{key}" in (unresolved or {}) and key in read.left_out
            },
            # The note cards whose title or due-date answers were sent, which a card shown as
            # reviewed carries as sent.
            "answered_tasks": frozenset(answer.key for answer in read.answers),
            "title_max_length": TITLE_MAX_LENGTH,
            "check_card": check_card,
            "if_selected": {
                change.key: when_selected[change.key].label for change in shown if change.left_out
            },
            "unselected_asks": unselected_asks,
            "occurrence_given": {
                key: value for key, value in (occurrences or {}).items() if key in unselected_asks
            },
            "creations": {card: question.creation for card, question in signed.items()},
            "answered": {
                card: DIFFERENT if answer.choice is None else choice_value(answer.choice)
                for card, answer in answered.items()
            },
            "same_as": choice_value,
            "came_from": came_from,
            "kept_note": any(change.note_change == "kept" for change in shown),
            "waiting": any(change.waiting for change in shown),
            "links_ticked": {
                card: [(name, revision) for name, revision, _ in notes]
                for card, notes in (linked or {}).items()
            },
            "kind_choices": KIND_CHOICES,
            "notice": notice,
            "marks": state.settings.page_marks,
            "spoken_report": spoken_report,
            "spoken_day": spoken_day,
            "refused": refused,
            "problem_target": problem_target,
            "problem_link": problem_link,
            "unsaved_on": said,
            "search_view": view,
            "picked_on": {key: pick.target for key, pick in picked.items()},
            "choosable_on": {
                key: {item.row.assignment_id for item in rows} for key, rows in choosable.items()
            },
            "unsaved_picks": unsaved_picks,
            "typed": typed or {},
            "focus": focus,
            "query_max_length": QUERY_MAX_LENGTH,
        },
        status_code=status_code,
    )


def preview_or_recovery(
    request: Request,
    state: ApplicationState,
    read: Read,
    draft: Mapping[str, str],
    kept_answers: list[AnswerKept],
    *,
    occurrences: Mapping[int, str] | None = None,
    kinds: Mapping[int, AssignmentKind] | None = None,
    instruction_answers: Mapping[int, SubmittedChoice] | None = None,
    unsaved: Mapping[int, UnsavedChoice] | None = None,
    carried: Mapping[int, CarriedAccount] | None = None,
    made: PageMade | None = None,
    identities: Mapping[int, IdentityAnswer] | None = None,
    linked: Mapping[int, Sequence[tuple[str, int, str | None]]] | None = None,
    picks: Mapping[int, Pick] | None = None,
    search: SearchShown | None = None,
    typed: Mapping[int, str] | None = None,
    focus: str | None = None,
    refused: str | None = None,
    check: Collection[int] | None = None,
    typing: tuple[int, Problem] | None = None,
    editing: Collection[int] = frozenset(),
    twice: Mapping[str, tuple[str, ...]] | None = None,
    unresolved: Mapping[str, tuple[str, ...]] | None = None,
    notice: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The review page once more after a save that did not land; when the file cannot be
    read back either, the page that reads no store, with the text and the answers as they
    were sent. Nothing is tried again."""
    try:
        return preview_page(
            request,
            state,
            read,
            draft,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            unsaved=unsaved,
            carried=carried,
            made=made,
            identities=identities,
            linked=linked,
            picks=picks,
            search=search,
            typed=typed,
            focus=focus,
            refused=refused,
            check=check,
            typing=typing,
            editing=editing,
            twice=twice,
            unresolved=unresolved,
            notice=notice,
            status_code=status_code,
        )
    except sqlite3.Error:
        logger.exception("the paste review could not be read back")
        return intake_unavailable(request, state, draft, kept_answers, STORE_REFUSED)


TICK_FIELDS: Final = frozenset({"offered", "take", "join"})
"""The fields a review sends for the ticks of readings made from teachers' notes: the note
offered, its tick, and a joined note's tick."""

BOX_FIELDS: Final = frozenset({"title", "when", "when_other"})
"""The fields a review sends for the title and due date of a reading made from a teacher's
note: its title box, its due-date choice, and the date beside Another date."""

BOX_HEADS: Final = frozenset({"link", "apply", "none"})
"""The heads of a card's boxes that their own readers read: a box to link her note
(``link-<card>-<place>``), a box of an instruction that applies (``apply-<card>-<place>``),
and the box that no instruction applies (``none-<card>``)."""

CARD_FIELDS: Final = frozenset(
    {
        "occurrence",
        "kind",
        "suggested",
        "shown",
        "chosen",
        "folded",
        "asked",
        "which",
        "editing",
        "search",
        "unsaved_why",
        "unsaved",
        "unsaved_row",
        "unsaved_unshown",
        "unsaved_none",
    }
)
"""The other fields a review sends once for a card, each one answer read as one value: the
answer to which it is and its type, with what the page showed and suggested beside them, the
card it is folded into, the marks of a question asked and of details open, the search words,
and a choice said back as not saved."""

PAGE_FIELDS: Final = frozenset({"text", *ENTRY_FIELDS, *STAMPS, "made_with"})
"""The fields a review sends once for the whole page, each one answer: the draft, the day and
moment it was first read, and the page's signature."""

ANSWER: Final = "answer"
"""A family answer: a control the family sets, or one such answer the page carries hidden."""
PAGE: Final = "page"
"""A page field: signed state, a technical field of a card, or a mark the page writes."""
PRESS: Final = "press"
"""A press: the name of a submit button."""
VISIBLE: Final = "visible"
HIDDEN: Final = "hidden"

NUMBER: Final = "(?:0|[1-9][0-9]{0,5})"
"""A key or a place as the page writes it, spelled as ``review_key`` reads it."""


def _numbered(heads: str, places: int = 1) -> str:
    """The exact grammar of a card's field: one of ``heads``, then that many numbers."""
    return f"(?:{heads})" + f"-{NUMBER}" * places


BOX_MARKS: Final = "|".join((_numbered("link|apply", 2), _numbered("none")))
"""The grammar of a card's boxes that their own readers read (``BOX_HEADS``)."""

MARKED: Final = "|".join(
    (
        _numbered("take|title|when|when_other|kind|occurrence|identity"),
        _numbered("join", 2),
        BOX_MARKS,
    )
)
"""The grammar of the fields an ``unresolved-<field>`` mark names."""


@dataclass(frozen=True)
class ReviewField:
    """One family of the fields the School text review's own form posts, to Save and to the
    review's own presses: the exact grammar of its names, how the page renders it
    (``visible``, ``hidden``, or ``press`` for a submit button), its class (``answer``,
    ``page`` or ``press``), and its shape (``one``: each name carries one value)."""

    grammar: str
    rendering: str
    role: str
    shape: str = "one"


REVIEW_FIELDS: Final = (
    ReviewField("text", HIDDEN, PAGE),
    ReviewField("|".join(ENTRY_FIELDS), HIDDEN, PAGE),
    ReviewField("|".join(STAMPS), HIDDEN, PAGE),
    ReviewField(MADE_WITH, HIDDEN, PAGE),
    ReviewField(_numbered("offered"), HIDDEN, PAGE),
    ReviewField(_numbered("take|title|when|when_other"), VISIBLE, ANSWER),
    ReviewField(_numbered("take|title|when|when_other"), HIDDEN, ANSWER),
    ReviewField(_numbered("join", 2), VISIBLE, ANSWER),
    ReviewField(_numbered("join", 2), HIDDEN, ANSWER),
    ReviewField(_numbered("editing"), HIDDEN, PAGE),
    ReviewField(f"unresolved-(?:{MARKED})", HIDDEN, PAGE),
    ReviewField(_numbered("occurrence|kind|search"), VISIBLE, ANSWER),
    ReviewField(_numbered("occurrence|kind|search"), HIDDEN, PAGE),
    ReviewField(_numbered("suggested|shown|chosen|folded|asked|which"), HIDDEN, PAGE),
    ReviewField(_numbered("identity"), VISIBLE, ANSWER),
    ReviewField(_numbered("identity"), HIDDEN, ANSWER),
    ReviewField(_numbered("creation|picked|instructions|unsaved_why"), HIDDEN, PAGE),
    ReviewField(_numbered("link|apply", 2), VISIBLE, ANSWER),
    ReviewField(_numbered("none"), VISIBLE, ANSWER),
    ReviewField(_numbered("row|instruction|unsaved_unshown|unsaved_none", 2), HIDDEN, PAGE),
    ReviewField(_numbered("unsaved|unsaved_row", 3), HIDDEN, PAGE),
    ReviewField("recheck|details|find|results_page|choose|remove", PRESS, PRESS),
)
"""Every field of the School text review's own form, each family once per rendering: the
draft, the day and moment it was read, and the signature; a note's offered mark, its tick,
title, due date and date box (answers where shown and where a closed or folded card carries
them), a joined note's tick, the mark of details open and of a field sent twice; which it is,
its type and its search words (answers where shown, page fields where carried); what the page
showed and suggested beside them; which homework it is, shown or carried, with its signed
token; homework chosen under another name; her notes to link; the school's instructions asked
about and the choices said back as not saved; and the presses."""


def review_field(name: str, rendering: str) -> ReviewField | None:
    """The entry of ``REVIEW_FIELDS`` whose grammar matches the whole of ``name`` as rendered
    so, or ``None``: no prefix of a name matches."""
    return next(
        (
            entry
            for entry in REVIEW_FIELDS
            if entry.rendering == rendering and re.fullmatch(entry.grammar, name)
        ),
        None,
    )


@dataclass(frozen=True)
class Shown:
    """What a review showed of the fields a page renders as a control on one card and hidden
    on another, worked out again from the read and the answers sent with it: the cards shown
    with their Type select, and the cards that ask which it is."""

    selects: frozenset[int]
    asks: frozenset[int]


def shown_of(changes: Sequence[Change], when_selected: Sequence[Change], read: Read) -> Shown:
    """The cards a review shows with their Type select (selected and not folded) and those
    that ask which it is: a card shown that asks, and a note not selected that asks once
    selected beside a card of its name in the text."""
    shown = [change for change in changes if change.state != FOLDED]
    here = {
        change.reading.pair
        for change in shown
        if change.reading.teacher_note is None and not change.left_out
    }
    return Shown(
        frozenset(change.key for change in shown if not change.left_out),
        frozenset(change.key for change in shown if not change.left_out and change.ambiguous)
        | frozenset(
            change.key
            for change in when_selected
            if change.key in read.left_out
            and change.reading.pair in here
            and change.ambiguous
            and change.on_record is None
        ),
    )


def field_role(name: str, read: Read, made: PageMade, shown: Shown) -> str | None:
    """The class the server takes for a field a form sent, from the page it signed or worked
    out again from the read, never from the name alone: ``answer``, ``page``, ``press``, or
    ``None`` for a name outside ``REVIEW_FIELDS``. A note's tick, title and due date are
    answers on a note's card, shown or carried; a card's type is one where the card shows its
    select, its answer to which it is where it asks and the signed page carries no answer
    for it, its search words where the signed page offered the search, its answer to which
    homework it is where the signed page asked or carried it, a box to link her note where
    the signed page offered that note at that place, and a box of the instructions where the
    signed page asked which apply. Any of these anywhere else is a page field."""
    roles = {entry.role for entry in REVIEW_FIELDS if re.fullmatch(entry.grammar, name)}
    if not roles:
        return None
    head, _, rest = name.partition("-")
    if not rest or head not in {"take", "join", *BOX_FIELDS, *MARKED_HEADS, *BOX_HEADS, "search"}:
        return next(iter(roles)) if len(roles) == 1 else PAGE
    numbers = [int(number) for number in rest.split("-")]
    key = numbers[0]
    if head in ("take", *BOX_FIELDS):
        on_note = key < len(read.items) and read.items[key].teacher_note is not None
        return ANSWER if on_note else PAGE
    if head == "join":
        joined = key < len(read.items) and numbers[1] < len(read.items[key].joined_notes)
        return ANSWER if joined else PAGE
    if head == KIND_FIELD:
        return ANSWER if key in shown.selects else PAGE
    if head == "occurrence":
        carried = key in made.occurrences and key not in read.left_out
        return ANSWER if not carried and key in shown.asks else PAGE
    if head == "search":
        return ANSWER if key in made.offers else PAGE
    if head == "identity":
        return ANSWER if key in made.identities else PAGE
    if head == "link":
        offered = made.links.get(key, ())
        return ANSWER if len(numbers) == 2 and numbers[1] < len(offered) else PAGE
    if head in ("apply", "none"):
        return ANSWER if key in made.asked else PAGE
    return next(iter(roles)) if len(roles) == 1 else PAGE


def twice_roles(
    state: ApplicationState,
    read: Read,
    made: PageMade,
    twice: Mapping[str, int],
    *,
    occurrences: Mapping[int, str],
    kinds: Mapping[int, AssignmentKind],
    identities: Mapping[int, IdentityAnswer],
    picks: Mapping[int, Pick],
) -> dict[str, str | None]:
    """The class the server takes for each field ``sent_twice`` found (``field_role``). The
    review is compared with the record again only when a card's type or its answer to which
    it is was sent twice, as only those take their class from what the review shows."""
    shown = Shown(frozenset(), frozenset())
    if any(name.partition("-")[0] in (KIND_FIELD, "occurrence") for name in twice):
        store = state.project_state
        changes = changes_for(
            read.items,
            store,
            occurrences=occurrences,
            kinds=kinds,
            identities=identities,
            picks=picks,
            left_out=read.left_out,
        )
        when_selected = changes
        if read.left_out:
            when_selected = changes_for(
                read.items,
                store,
                occurrences={
                    key: value for key, value in occurrences.items() if key not in read.left_out
                },
                kinds=kinds,
                identities=identities,
                picks=picks,
                left_out=frozenset(),
            )
        shown = shown_of(changes, when_selected, read)
    return {name: field_role(name, read, made, shown) for name in twice}


TWICE_HEADS: Final = TICK_FIELDS | BOX_FIELDS | CARD_FIELDS | BOX_HEADS | {"identity"}
"""The heads of the fields of a card ``sent_twice`` reads, beside the marks of them."""


def sent_twice(fields: Iterable[tuple[str, object]]) -> dict[str, int]:
    """Each field a form sends once for a card that came more than once with different
    values, read from every field as sent, with the key of the card it is on: the ticks
    ``offered-<key>``, ``take-<key>`` and ``join-<card>-<place>``, the note boxes
    ``title-<key>``, ``when-<key>`` and ``when_other-<key>``, the ``CARD_FIELDS``, the answer
    ``identity-<key>``, the boxes of ``BOX_HEADS``, and the mark ``unresolved-<field>`` of any
    of them. A value sent as a file differs from every text. The same value sent twice is one
    answer."""
    values: dict[str, set[str | None]] = {}
    keys: dict[str, int] = {}
    for name, value in fields:
        head, _, rest = name.partition("-")
        if head == UNRESOLVED:
            head, _, rest = rest.partition("-")
        card = rest.partition("-")[0]
        if head in TWICE_HEADS and review_key(card):
            values.setdefault(name, set()).add(value if isinstance(value, str) else None)
            keys[name] = int(card)
    return {name: keys[name] for name, sent in values.items() if len(sent) > 1}


def boxes_as_read(fields: Sequence[tuple[str, object]]) -> list[tuple[str, object]]:
    """The fields the readers of the boxes of ``BOX_HEADS`` take: without a box sent twice
    with different answers (``sent_twice``), which the review asks again, and without a box
    a mark names (``unresolved-<box>``) answered ``0``, which is that box left unticked."""
    twice = sent_twice(fields)
    marked = {
        name.partition("-")[2]
        for name, _ in fields
        if name.partition("-")[0] == UNRESOLVED
        and re.fullmatch(BOX_MARKS, name.partition("-")[2]) is not None
    }
    return [
        (name, value)
        for name, value in fields
        if re.fullmatch(BOX_MARKS, name) is None
        or not (name in twice or (name in marked and value == "0"))
    ]


def twice_notice(twice: Mapping[str, int]) -> str:
    """What the summary says of a review sent back for choices sent twice: that a choice needs
    an answer when none was sent twice this time, that the answers about the school's
    instructions could not be read when only their boxes were, and otherwise that a choice
    was sent twice."""
    if not twice:
        return UNANSWERED_TWICE
    if all(name.partition("-")[0] in ("apply", "none") for name in twice):
        return INSTRUCTION_FORM_UNREADABLE
    return SENT_TWICE


def page_twice(fields: Iterable[tuple[str, object]]) -> frozenset[str]:
    """Each of the ``PAGE_FIELDS`` a form sends more than once with different values, a
    value sent as a file differing from every text."""
    values: dict[str, set[str | None]] = {}
    for name, value in fields:
        if name in PAGE_FIELDS:
            values.setdefault(name, set()).add(value if isinstance(value, str) else None)
    return frozenset(name for name, sent in values.items() if len(sent) > 1)


def boxes_twice(twice: Mapping[str, int]) -> frozenset[int]:
    """The keys of the cards whose title box, due-date choice or date box ``sent_twice``
    found sent twice with different answers."""
    return frozenset(key for name, key in twice.items() if name.partition("-")[0] in BOX_FIELDS)


def twice_values(
    fields: Iterable[tuple[str, object]], twice: Mapping[str, int]
) -> dict[str, tuple[str, ...]]:
    """For each field ``sent_twice`` found, the values it came with, each once, in the order
    sent; a value sent as a file is said as empty."""
    values: dict[str, list[str]] = {name: [] for name in twice}
    for name, value in fields:
        text = value if isinstance(value, str) else ""
        if name in values and text not in values[name]:
            values[name].append(text)
    return {name: tuple(sent) for name, sent in values.items()}


def said_date(value: str) -> str:
    """A due-date answer as the card says it: a date as its day, no due date and another date
    in the choice's words, anything else in quotes as sent."""
    if value == NO_DUE_DATE:
        return "No due date yet"
    if value == ANOTHER_DATE:
        return "Another date"
    try:
        return spoken_day(date.fromisoformat(value))
    except ValueError:
        return f'"{value}"'


FIELD_TWICE: Final = {
    "join": JOIN_TWICE,
    "kind": KIND_TWICE,
    "search": SEARCH_TWICE,
    "occurrence": OCCURRENCE_TWICE,
    "identity": IDENTITY_TWICE,
    "link": LINK_TWICE,
    "apply": APPLY_TWICE,
    "none": NONE_TWICE,
}
"""What a joined note's tick, a card's type, its search words, its answer to which it is, its
answer to which homework it is, a box to link her note, a box of an instruction and the box
that none applies say at their field when sent twice with different answers."""


def twice_said(twice: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """For each family answer sent twice with different answers, the lines said at the field
    they sit at: ``take-<key>`` beside the note's tick, ``title-<key>`` in the title's label,
    ``when-<key>`` in the due date's fieldset, ``join-<card>-<place>`` beside a joined note's
    tick, ``kind-<key>`` in the type's label, ``search-<key>`` above the search box, and
    ``occurrence-<key>`` and ``identity-<key>`` in their question, and each box of
    ``BOX_HEADS`` at the box. Each line says which choice it is and, for a title or a date,
    each value sent."""
    said: dict[str, list[str]] = {}
    for name, values in sorted(twice.items()):
        head, _, rest = name.partition("-")
        if head == "take":
            at, line = name, TICK_TWICE
        elif head in FIELD_TWICE:
            at, line = name, FIELD_TWICE[head]
        elif head == "title":
            at = name
            line = TITLE_TWICE.format(values=" and ".join(f'"{value}"' for value in values))
        elif head in ("when", "when_other"):
            at = f"when-{rest}"
            line = (WHEN_TWICE if head == "when" else OTHER_TWICE).format(
                values=" and ".join(said_date(value) for value in values)
            )
        else:
            continue
        if line not in said.get(at, []):
            said.setdefault(at, []).append(line)
    return said


UNRESOLVED: Final = "unresolved"
"""The head of the field a returned review carries for each choice sent twice with different
answers, ``unresolved-<field>``, holding the values sent, until the family answers it: a
note's tick, a joined note's tick, a title box, a due-date choice, a date box, a card's type,
its answer to which it is, its answer to which homework it is, or a box of ``BOX_HEADS``."""

KIND_FIELD: Final = "kind"
"""The field of a card's Homework or Task choice, which a conflict leaves open until the
family chooses one, on any card."""

MARKED_HEADS: Final = frozenset({KIND_FIELD, "occurrence", "identity"})
"""The answers of any card, beside a note's fields, that a conflict leaves marked until the
family answers them."""


def unresolved_from(twice: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """The family answers among those sent twice that the family answers again on the page,
    with the values sent: the note fields, a card's type, its answer to which it is, its
    answer to which homework it is, and the boxes of ``BOX_HEADS``."""
    found: dict[str, tuple[str, ...]] = {}
    for name, values in twice.items():
        head = name.partition("-")[0]
        if head in {"take", "join", *BOX_FIELDS, *MARKED_HEADS, *BOX_HEADS}:
            found[name] = values
    return found


def unresolved_of(fields: Iterable[tuple[str, object]], read: Read) -> dict[str, tuple[str, ...]]:
    """The choices a form marks as sent twice with different answers (``UNRESOLVED``), each
    with the values the marks hold: a note field, a card's type, answer to which it is or
    answer to which homework it is, or a box of ``BOX_HEADS`` on a card of this read; a mark
    on no such field of this read, or sent as a file, is ignored, and values that cannot be
    read are none."""
    notes = {key for key, item in enumerate(read.items) if item.teacher_note is not None}
    joined = {
        f"join-{key}-{place}"
        for key, item in enumerate(read.items)
        for place in range(len(item.joined_notes))
    }
    typed = {f"{head}-{key}" for head in MARKED_HEADS for key in range(len(read.items))}
    marked: dict[str, list[str]] = {}
    for name, value in fields:
        head, _, which = name.partition("-")
        kind, _, key = which.partition("-")
        on_note = kind in ("take", *BOX_FIELDS) and review_key(key) and int(key) in notes
        on_box = re.fullmatch(BOX_MARKS, which) is not None and int(which.split("-")[1]) < len(
            read.items
        )
        on_card = on_note or on_box or which in joined or which in typed
        if head != UNRESOLVED or not isinstance(value, str) or not on_card:
            continue
        try:
            said = json.loads(value)
        except ValueError:
            said = []
        values = marked.setdefault(which, [])
        if isinstance(said, list):
            values.extend(text for text in said if isinstance(text, str) and text not in values)
    return {which: tuple(values) for which, values in marked.items()}


def answered(form: Mapping[str, str], which: str) -> bool:
    """Whether a form answers a choice marked as sent twice: a tick by either choice, a title
    box by a title, a due date by a choice, another date with its date, a card's type by
    Homework or Task, its answer to which it is by one of its choices, and its answer to
    which homework it is by any answer, which the save then checks against the question, and a
    box of ``BOX_HEADS`` by either choice."""
    head, _, key = which.partition("-")
    if head in ("take", "join", *BOX_HEADS):
        return form.get(which) in ("1", "0")
    if head == KIND_FIELD:
        return form.get(which) in dict(KIND_CHOICES)
    if head == "occurrence":
        return form.get(which) in (UPDATE, NEW_WORK, KEEP_DATE)
    if head == "identity":
        return bool(form.get(which))
    if head == "title":
        return bool(form.get(which, "").strip())
    when = form.get(f"when-{key}", "")
    return bool(when) and (when != ANOTHER_DATE or bool(form.get(f"when_other-{key}", "").strip()))


def unanswered_marks(
    form: Mapping[str, str], fields: Iterable[tuple[str, object]], read: Read
) -> dict[str, tuple[str, ...]]:
    """The choices a form marks as sent twice that it does not answer."""
    return {
        which: values
        for which, values in unresolved_of(fields, read).items()
        if not answered(form, which)
    }


def unresolved_cards(unresolved: Mapping[str, object]) -> frozenset[int]:
    """The keys of the cards with an unresolved choice, a joined note's by its card."""
    return frozenset(int(which.split("-")[1]) for which in unresolved)


def unresolved_boxes(unresolved: Mapping[str, object]) -> frozenset[int]:
    """The keys of the cards whose unresolved field is a title box or a due date, whose
    details are open for it."""
    return frozenset(
        int(which.split("-")[1]) for which in unresolved if which.split("-")[0] in BOX_FIELDS
    )


def unresolved_at(unresolved: Mapping[str, tuple[str, ...]]) -> dict[str, list[tuple[str, str]]]:
    """For each control an unresolved note field is answered with (a date box by its due
    date), the marks it carries: each field's name and the values sent, as the page writes
    them."""
    at: dict[str, list[tuple[str, str]]] = {}
    for which, values in sorted(unresolved.items()):
        head, _, rest = which.partition("-")
        anchor = f"when-{rest}" if head == "when_other" else which
        at.setdefault(anchor, []).append((which, json.dumps(list(values))))
    return at


async def submitted(request: Request) -> dict[str, str]:
    """The form as submitted, every value as text. A field sent once for a card or for the
    page that came more than once with different values is left out (``sent_twice``,
    ``page_twice``), so no answer is taken by the order the values came in."""
    form = await form_of(request)
    twice = {*sent_twice(form.multi_items()), *page_twice(form.multi_items())}
    return {
        name: str(value)
        for name, value in form.items()
        if isinstance(value, str) and name not in twice
    }


@router.post("/read", response_class=HTMLResponse, include_in_schema=False)
async def read_paste(request: Request, state: State) -> Response:
    """Read the pasted text and show what was read, without writing anything."""
    form = await submitted(request)
    if not form.get("text", "").strip():
        return await asyncio.to_thread(
            problem_page, request, state, {"text": ""}, Problem("text", NOTHING_PASTED)
        )
    draft = stamped(state, draft_of(form))
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return await asyncio.to_thread(problem_page, request, state, draft, read)
    return await asyncio.to_thread(first_review, request, state, read, draft)


@router.post("/enter", response_class=HTMLResponse, include_in_schema=False)
async def read_entry(request: Request, state: State) -> Response:
    """Read one assignment typed by hand and show it, without writing anything."""
    form = await submitted(request)
    draft = stamped(state, {name: form.get(name, "") for name in ENTRY_FIELDS})
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        return await asyncio.to_thread(problem_page, request, state, draft, read)
    return await asyncio.to_thread(first_review, request, state, read, draft)


@router.post("/edit", response_class=HTMLResponse, include_in_schema=False)
async def edit_draft(request: Request, state: State) -> Response:
    """Back to the family page with the draft as it was, to change it; nothing is written.
    When the household's records can't be read for it, the page that reads no store, with the
    draft and the answers given on the cards kept."""
    form = await submitted(request)
    draft = draft_of(form)
    try:
        return await asyncio.to_thread(
            review_page,
            request,
            state,
            paste=draft.get("text") or None,
            entry=None if draft.get("text") else draft,
            entry_open=True,
        )
    except READ_REFUSALS as error:
        answers = answers_sent(state, form, (await form_of(request)).multi_items(), draft)
        return await asyncio.to_thread(read_refused, request, state, draft, answers, error)


SEARCH_PRESSES: Final = ("find", "results_page", "choose", "remove", "recheck", "details")
"""The presses that change only the review: Find, Next and Previous, Choose, Remove, Review
changes, and Edit details."""

EDITING: Final = "editing"
"""The field a note card with its details open sends, so the presses after it keep them
open until Review changes shows them as reviewed."""


def details_open(form: Mapping[str, str], read: Read) -> frozenset[int]:
    """The keys of the readings made from teachers' notes whose details a form says are
    open (``editing-<key>``)."""
    return frozenset(
        key
        for key, item in enumerate(read.items)
        if item.teacher_note is not None and f"{EDITING}-{key}" in form
    )


def one_press(fields: Iterable[tuple[str, object]]) -> tuple[str, str] | None:
    """The one review press a form was sent with, or ``None`` for none, more than one, or
    one sent as a file."""
    pressed = [(name, value) for name, value in fields if name in SEARCH_PRESSES]
    if len(pressed) != 1 or not isinstance(pressed[0][1], str):
        return None
    return pressed[0][0], pressed[0][1]


def card_of(value: str) -> int | None:
    """A card's key as a press names it, or ``None``."""
    return int(value) if review_key(value) else None


@router.post("/find", response_class=HTMLResponse, include_in_schema=False)
async def search_review(request: Request, state: State) -> Response:
    """Find homework already here under another name for one card, page through what was
    found, choose one, or take a choice back, and show the review again; or, for Review
    changes, show it again with the selections as sent and every note card's details as
    reviewed; or, for Edit details, with that card's title box and due-date choices open.
    Details open stay open through the other presses. Nothing is written: Save is the one
    press that saves. The draft and every answer on the cards come back as
    sent, questions still open among them, and a choice made on a card stays until Remove,
    whatever is searched. Choose takes only homework the page showed for that card, and holds
    it to the row as shown; a press the page didn't offer changes nothing. The answers carried
    are read as Save reads them: an answer about the school's instructions on a card the page
    didn't ask is shown as not saved, never signed as asked. A record that can't be read, or
    a file that refuses a read, is answered by the page that reads no store, with the paste
    and every answer kept."""
    form = await submitted(request)
    draft = draft_of(form)
    read = read_draft(state, draft)
    if isinstance(read, Problem):
        answers = answers_sent(state, form, (await form_of(request)).multi_items(), draft)
        return await asyncio.to_thread(problem_page, request, state, draft, read, answers)
    now, today = moment_of(state, draft)
    read = with_edits(form, selected(form, read), now=now, today=today)
    fields = (await form_of(request)).multi_items()
    read_answers = paste_answers(boxes_as_read(fields))
    instruction_answers = read_answers.answers
    made = {key: UnsavedChoice.of(answer) for key, answer in instruction_answers.items()}
    chosen = {key for key, answer in {**made, **read_answers.unsaved}.items() if answer.chooses}
    carried = {
        key: account for key, account in carried_accounts(fields).items() if key not in chosen
    }
    page_made = made_with(state.result_key, form, draft)
    identities, _ = identity_answers(fields, page_made)
    linked, link_unreadable = link_answers(boxes_as_read(fields), page_made)
    if link_unreadable:
        linked = {}
    picks = dict(page_made.picks)
    typed = {
        int(card): value
        for name, value in form.items()
        for head, _, card in [name.rpartition("-")]
        if head == "search" and review_key(card)
    }
    kept_answers = answers_kept(
        form,
        instruction_answers,
        read_answers.unsaved,
        carried,
        page_made,
        identities,
        linked,
        picks,
    )
    store = state.project_state
    search: SearchShown | None = None
    focus: str | None = None
    refused = False

    def searched(key: int) -> SearchShown | None:
        """The search the page showed for this card, kept through Choose and Remove, so a
        page handed back later can show it again."""
        shown = page_made.query
        return None if shown is None or shown[0] != key else SearchShown(*shown[:3])

    pressed = one_press(fields)
    action, value = pressed if pressed is not None else ("", "")
    editing = details_open(form, read)
    try:
        occurrences, kinds = answers_from(form, await asyncio.to_thread(unasked_for, state, read))
        # An answer about the school's instructions is held to the question the page asked,
        # as at Save: one the page never asked is said back as not saved, never signed.
        never_put = answers_to_no_question(
            await asyncio.to_thread(
                changes_for,
                read.items,
                store,
                occurrences=occurrences,
                kinds=kinds,
                identities=identities,
                picks=page_made.picks,
                left_out=read.left_out,
            ),
            instruction_answers,
            page_made.asked,
        )
        unsaved = {
            **read_answers.unsaved,
            **{key: UnsavedChoice.of(instruction_answers[key]) for key in never_put},
        }
        instruction_answers = {
            key: answer for key, answer in instruction_answers.items() if key not in never_put
        }
        twice = sent_twice(fields)
        unmet = unanswered_marks(form, fields, read)
        roles = await asyncio.to_thread(
            twice_roles,
            state,
            read,
            page_made,
            twice,
            occurrences=occurrences,
            kinds=kinds,
            identities=identities,
            picks=page_made.picks,
        )
        if page_twice(fields) or any(role != ANSWER for role in roles.values()):
            # A page field sent twice with different answers is no form the page wrote: the
            # whole press is refused as a review that could not be verified, before any press
            # is taken, naming no field, the marks of unanswered choices kept.
            shown = page_made.query
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks,
                search=None if shown is None else SearchShown(shown[0], shown[1], shown[2]),
                typed=typed,
                refused="stale",
                check=frozenset(),
                twice=unmet,
                unresolved=unmet,
                editing=editing | unresolved_boxes(unmet),
                notice=REVIEW_UNVERIFIED,
                status_code=status.HTTP_409_CONFLICT,
            )
        if twice or unmet:
            # A family answer sent once for a card that came twice with different answers, or
            # one marked as sent twice and not answered since: before any press is taken, no
            # answer is taken by the order its values came in, and the review comes back
            # saying so at each field, as Save does, each offering its actual choice with
            # nothing chosen and marked until it is answered, the details of a note box
            # concerned open.
            sent = twice_values(fields, twice)
            unresolved = {**unmet, **unresolved_from(sent)}
            shown = page_made.query
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks,
                search=None if shown is None else SearchShown(shown[0], shown[1], shown[2]),
                typed=typed,
                refused="malformed",
                check=frozenset(twice.values()) | unresolved_cards(unresolved),
                twice={**unmet, **sent},
                unresolved=unresolved,
                editing=editing | boxes_twice(twice) | unresolved_boxes(unresolved),
                notice=twice_notice(twice),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        if action == "find":
            key = card_of(value)
            if key is None or key not in page_made.offers:
                refused = True
            else:
                search = SearchShown(key, typed.get(key, ""))
        elif action == "results_page":
            card, _, number = value.partition(":")
            key, page = card_of(card), page_number(number) if number else None
            shown = page_made.query
            # Only a page the returned page's Previous or Next went to; results that shrank
            # since are said on the page, never taken as a page it offered.
            if (
                key is None
                or page is None
                or shown is None
                or shown[0] != key
                or (page not in shown[3])
            ):
                refused = True
            else:
                search = SearchShown(key, shown[1], page)
        elif action == "choose":
            card, _, target = value.partition(":")
            key = card_of(card)
            offered_rows = dict(page_made.found.get(key, ())) if key is not None else {}
            if key is None or key not in page_made.offers or target not in offered_rows:
                refused = True
            else:
                row = await asyncio.to_thread(store.one_assignment, target)
                readings = (
                    None if row is None else await asyncio.to_thread(readings_for, store, [row])
                )
                basis = None if readings is None else candidate_basis(readings)
                if basis == offered_rows[target]:
                    picks[key] = Pick(target, offered_rows[target], page_made.offers[key])
                    identities.pop(key, None)
                    focus = f"picked-{key}"
                    search = searched(key)
                else:
                    # The homework changed or left since the page showed it: nothing is
                    # chosen, and the page shows it again as it stands now, whether or not
                    # the words still find it.
                    shown = page_made.query
                    words = shown[1] if shown is not None and shown[0] == key else ""
                    at = shown[2] if shown is not None and shown[0] == key else 1
                    search = SearchShown(key, words, at, attempted=target)
        elif action == "remove":
            key = card_of(value)
            if key is None or key not in picks:
                refused = True
            else:
                del picks[key]
                focus = f"rename-{key}"
                search = searched(key)
        elif action == "recheck":
            # Review changes: the review again with the selections as sent and the search
            # the page showed.
            shown = page_made.query
            if value != "1":
                refused = True
            elif shown is not None:
                search = SearchShown(shown[0], shown[1], shown[2])
        elif action == "details":
            # Edit details: the review again with that card's title box and due-date
            # choices open, and those of any card open already, the search kept.
            key = card_of(value)
            if key is None or key >= len(read.items) or read.items[key].teacher_note is None:
                refused = True
            else:
                editing |= {key}
                shown = page_made.query
                if shown is not None:
                    search = SearchShown(shown[0], shown[1], shown[2])
        else:
            refused = True
        if refused:
            shown = page_made.query
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks,
                search=None if shown is None else SearchShown(shown[0], shown[1], shown[2]),
                typed=typed,
                refused="malformed",
                editing=details_open(form, read),
                notice=PRESS_UNREADABLE,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        typing = typing_refused(read) if action == "recheck" else None
        if typing is not None:
            # Review changes with a title or due date typed on a selected note that cannot
            # be taken: the review again, said why, with everything as typed and the details
            # concerned open.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="typed",
                typing=typing,
                editing=editing | {typing[0]} | edited(read, page_made),
                notice=typing[1].message,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        # Review changes shows every card's details as reviewed; any other press keeps open
        # the details that were open.
        return await asyncio.to_thread(
            preview_or_recovery,
            request,
            state,
            read,
            draft,
            kept_answers,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            unsaved=unsaved,
            carried=carried,
            made=page_made,
            identities=identities,
            linked=linked,
            picks=picks,
            search=search,
            typed=typed,
            focus=focus,
            editing=frozenset() if action == "recheck" else editing,
        )
    except UnreadableClaim:
        return await asyncio.to_thread(intake_unavailable, request, state, draft, kept_answers)
    except UnreadableInstruction:
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, INSTRUCTION_UNREADABLE
        )
    except UnreadableDecision:
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, DECISION_UNREADABLE
        )
    except UnreadableCapture:
        logger.exception("a note offered on the review could not be read")
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, NOTE_UNREADABLE
        )
    except sqlite3.Error:
        logger.exception("the review could not be read for a press")
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, STORE_REFUSED
        )


@router.post("/keep", response_class=HTMLResponse, include_in_schema=False)
async def keep_readings(request: Request, state: State) -> Response:
    """Read the draft again, save what is new against the record as it is, and say what
    was added, updated, and unchanged; or show the review again if a question is open,
    saying whether the question is one the page put and the parent left, or one the record
    raised since. A save with no reading selected writes nothing and shows the review again,
    which says no assignment is selected. Whatever was chosen on the cards comes back chosen.

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
        answers = answers_sent(state, form, (await form_of(request)).multi_items(), draft)
        return await asyncio.to_thread(problem_page, request, state, draft, read, answers)
    now, today = moment_of(state, draft)
    ticked = selected(form, read)
    read = with_edits(form, ticked, now=now, today=today)
    # Every answer about the school's instructions is read from the fields as sent, before
    # any is collapsed, and kept, contradictions included, in words a page that reads no
    # store can show: all of it before anything touches the store.
    fields = (await form_of(request)).multi_items()
    read_answers = paste_answers(boxes_as_read(fields))
    instruction_answers = read_answers.answers
    # A choice the page showed as not saved is shown again until a new one is ticked.
    made = {key: UnsavedChoice.of(answer) for key, answer in instruction_answers.items()}
    chosen = {key for key, answer in {**made, **read_answers.unsaved}.items() if answer.chooses}
    carried = {
        key: account for key, account in carried_accounts(fields).items() if key not in chosen
    }
    page_made = made_with(state.result_key, form, draft)
    # Which homework each card is and the notes ticked are read from every field as sent too:
    # a field sent twice or as a file is no answer, for the save or for a page that says the
    # answers back.
    identities, identity_unreadable = identity_answers(fields, page_made)
    linked, link_unreadable = link_answers(boxes_as_read(fields), page_made)
    picks, pick_unreadable = pick_answers(fields, page_made)
    # The words typed in each card's search, and the search the page showed, come back with
    # every page the save hands back, so a refusal never costs the parent their search.
    typed = {
        int(card): value
        for name, value in form.items()
        for head, _, card in [name.rpartition("-")]
        if head == "search" and review_key(card)
    }
    shown_query = page_made.query
    search = (
        None if shown_query is None else SearchShown(shown_query[0], shown_query[1], shown_query[2])
    )
    # The note cards whose details the form says are open stay open on every page the save
    # hands back, with what was typed in them.
    held_open = details_open(form, read)
    kept_answers = answers_kept(
        form,
        instruction_answers,
        read_answers.unsaved,
        carried,
        page_made,
        identities,
        linked,
        picks,
    )
    # A claim on record that cannot be read refuses the comparison, before the write or
    # inside its transaction, which is rolled back whole before anything is answered. The
    # answer reads no store and keeps the draft and the answers given on the cards, which
    # homework each was said to be and the notes ticked among them; so does a write the
    # file refuses, which is never tried again.
    joins: dict[int, tuple[str, dict[tuple[int, int], str]]] = {}
    try:
        occurrences, kinds = answers_from(form, await asyncio.to_thread(unasked_for, state, read))
        twice = sent_twice(fields)
        unmet = unanswered_marks(form, fields, read)
        roles = await asyncio.to_thread(
            twice_roles,
            state,
            read,
            page_made,
            twice,
            occurrences=occurrences,
            kinds=kinds,
            identities=identities,
            picks=picks,
        )
        if page_twice(fields) or any(role != ANSWER for role in roles.values()):
            # A page field sent twice with different answers is no form the page wrote:
            # before any other refusal, nothing is written, and the whole form is refused as a
            # review that could not be verified, naming no field, the marks of unanswered
            # choices kept.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks if pick_unreadable else picks,
                search=search,
                typed=typed,
                refused="stale",
                check=frozenset(),
                twice=unmet,
                unresolved=unmet,
                editing=held_open | unresolved_boxes(unmet),
                notice=REVIEW_UNVERIFIED,
                status_code=status.HTTP_409_CONFLICT,
            )
        if twice or unmet:
            # A family answer sent once for a card that came twice with different answers is
            # no form the page wrote, and no answer is taken by the order its values came in;
            # nor is one marked as sent twice and not answered since: before any other
            # refusal, nothing is written, and the review comes back with every other answer
            # and the typing as sent, that field saying so where it is, a note field offering
            # its actual choice with nothing chosen and marked until it is answered, the
            # details of a note box concerned open, and a link to its card where there is one
            # card.
            sent = twice_values(fields, twice)
            unresolved = {**unmet, **unresolved_from(sent)}
            return await asyncio.to_thread(
                preview_page,
                request,
                state,
                read,
                draft,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks if pick_unreadable else picks,
                search=search,
                typed=typed,
                refused="malformed",
                check=frozenset(twice.values()) | unresolved_cards(unresolved),
                twice={**unmet, **sent},
                unresolved=unresolved,
                notice=twice_notice(twice),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                editing=held_open | boxes_twice(twice) | unresolved_boxes(unresolved),
            )
        if identity_unreadable:
            # An answer about which homework a card is that the page did not ask, a homework
            # or token it did not give, or one sent twice, is no form the page wrote. The
            # answers that were read, on other cards and in the notes ticked, are kept.
            return await asyncio.to_thread(
                preview_page,
                request,
                state,
                read,
                draft,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="malformed",
                notice=IDENTITY_FORM_UNREADABLE,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                editing=held_open,
            )
        if link_unreadable:
            # A box the page did not offer, a box sent twice, as a file, or with another value,
            # or a note ticked twice is no form the page wrote. The notice says only that the
            # choices could not be read, since it holds for every one of these.
            return await asyncio.to_thread(
                preview_page,
                request,
                state,
                read,
                draft,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="malformed",
                notice=LINK_FORM_UNREADABLE,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                editing=held_open,
            )
        if pick_unreadable:
            # A choice of homework under another name the page did not show, or one changed,
            # left out, sent twice, or sent beside an answer to the card's own question, is no
            # form the page wrote. The page comes back with the choices it showed.
            return await asyncio.to_thread(
                preview_page,
                request,
                state,
                read,
                draft,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=page_made.picks,
                search=search,
                typed=typed,
                refused="malformed",
                notice=PICK_FORM_UNREADABLE,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                editing=held_open,
            )
        if read.items and len(read.left_out) == len(read.items):
            # Nothing is selected to save: nothing is written, and the review comes back
            # saying no assignment is selected, with the ticks and the typing as sent, so it
            # never reads as a reading that failed or found nothing.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                unsaved=read_answers.unsaved,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                editing=held_open,
            )
        # An answer where the page puts no such question is no form the page wrote. The
        # questions the page signed say where it asked, at which revision, and about which
        # assignment, as the answers about which homework each card is land it.
        changes = await asyncio.to_thread(
            changes_for,
            read.items,
            state.project_state,
            occurrences=occurrences,
            kinds=kinds,
            identities=identities,
            picks=picks,
            left_out=read.left_out,
        )
        never_put = answers_to_no_question(changes, instruction_answers, page_made.asked)
        unreadable = read_answers.malformed or bool(never_put)
        contradicted = any(answer.contradicts for answer in instruction_answers.values())
        if unreadable or contradicted:
            # Nothing of the text is written until the answers are put right; each comes back
            # as made when it was made against what stands, and in words when it was not.
            return await asyncio.to_thread(
                preview_page,
                request,
                state,
                read,
                draft,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers={
                    key: answer
                    for key, answer in instruction_answers.items()
                    if key not in never_put
                },
                unsaved={
                    **read_answers.unsaved,
                    **{key: UnsavedChoice.of(instruction_answers[key]) for key in never_put},
                },
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="malformed" if unreadable else "contradict",
                notice=INSTRUCTION_FORM_UNREADABLE if unreadable else INSTRUCTIONS_CONTRADICT,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                editing=held_open,
            )
        typing = typing_refused(read)
        if typing is not None:
            # A title or due date typed on a selected note that cannot be taken: nothing is
            # written, and the review comes back saying why, with everything as typed.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="typed",
                typing=typing,
                editing=held_open | edited(read, page_made) | {typing[0]},
                notice=typing[1].message,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        changed = edited(read, page_made)
        if changed:
            # A title or due date no review showed, typed by hand or sent from a page with
            # details open: nothing is written, and the review comes back with those details
            # open as typed, for Review changes to show them.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="edited",
                check=changed,
                editing=held_open | changed,
                notice=EDITS_IN_REVIEW,
            )
        joins_changed = joins_moved(read, page_made)
        # A joined note ticked or unticked other than the page showed saves in this press
        # when it does what the page said it does that way; the save checks it as it writes.
        joins = joins_shown(read, page_made) if page_made.verified else {}
        if joins_changed and not page_made.verified:
            # A page this process did not sign said nothing of what a tick does, so nothing
            # is written, and the review comes back with the ticks as sent, signed.
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="stale",
                check=frozenset(),
                notice=REVIEW_UNVERIFIED,
                status_code=status.HTTP_409_CONFLICT,
                editing=held_open,
            )

        def saved() -> Kept | ChangedSinceShown | NotAsked | NotOffered | list[Change]:
            return keep(
                read.items,
                state.project_state,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                asked=page_made.asked,
                identities=identities,
                shown_identities=set(page_made.identities),
                shown_notes=page_made.notes,
                # A note of hers ticked on a reading left out of the save is linked nowhere.
                links={key: notes for key, notes in linked.items() if key not in read.left_out},
                picks=picks,
                offered=page_made.offers,
                left_out=read.left_out,
                shown_tasks=page_made.tasks,
                shown_joins={
                    card: next(iter(each.values()))
                    for card, (_, each) in joins.items()
                    if len(each) == 1
                },
                joins_each=joins_alone(read, joins, now=now),
                imported_by="parent" if viewer_of(request) == "parent" else "household",
                now=now,
                today=today,
            )

        async with state.decision_lock:
            kept = await worker_to_the_end(saved)
    except UnreadableClaim:
        return await asyncio.to_thread(intake_unavailable, request, state, draft, kept_answers)
    except UnreadableInstruction:
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, INSTRUCTION_UNREADABLE
        )
    except UnreadableDecision:
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, DECISION_UNREADABLE
        )
    except UnreadableCapture:
        logger.exception("a note ticked to link could not be read")
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, NOTE_UNREADABLE
        )
    except CaptureNotSaved:
        logger.exception("a note could not be linked from the paste")
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, STORE_REFUSED
        )
    except sqlite3.Error:
        logger.exception("a paste could not be saved")
        return await asyncio.to_thread(
            intake_unavailable, request, state, draft, kept_answers, STORE_REFUSED
        )
    if isinstance(kept, NotOffered):
        # Checked again where the save writes: a choice on a card the page didn't offer it
        # on, or beside the card's own answer, is no form the page wrote.
        return await asyncio.to_thread(
            preview_or_recovery,
            request,
            state,
            read,
            draft,
            kept_answers,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            carried=carried,
            made=page_made,
            identities=identities,
            linked=linked,
            picks=page_made.picks,
            search=search,
            typed=typed,
            refused="malformed",
            notice=PICK_FORM_UNREADABLE,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            editing=held_open,
        )
    if isinstance(kept, NotAsked):
        # The same check as above, made again where the save writes: another connection's
        # save can move a card to other homework after the first check.
        return await asyncio.to_thread(
            preview_or_recovery,
            request,
            state,
            read,
            draft,
            kept_answers,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers={
                key: answer for key, answer in instruction_answers.items() if key not in kept.cards
            },
            unsaved={
                **read_answers.unsaved,
                **{key: UnsavedChoice.of(instruction_answers[key]) for key in kept.cards},
            },
            carried=carried,
            made=page_made,
            identities=identities,
            linked=linked,
            picks=picks,
            search=search,
            typed=typed,
            refused="malformed",
            notice=INSTRUCTION_FORM_UNREADABLE,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            editing=held_open,
        )
    if isinstance(kept, ChangedSinceShown):
        instructions_moved = any(change.instructions_stale for change in kept.changes)
        if kept.moved:
            # A selected note would do something other than the page said for it. A form
            # this process did not sign says nothing of that, so the review is checked
            # again. Otherwise the selections are named only when they are established as
            # the cause: with the ticks as the page signed them every note does what the
            # page said. Any other mismatch is said without a cause. A title or date typed on
            # a note left out writes nothing, so the comparison takes each note's title and
            # date as the page signed them.
            answered = {*occurrences, *identities, *picks, *instruction_answers}
            as_shown = with_answers(
                ticked,
                [
                    TaskAnswer(key, title, due or NO_DUE_DATE)
                    for key, (title, due) in page_made.edits.items()
                ],
                now=now,
                today=today,
            )

            def by_the_selections() -> bool:
                as_signed = frozenset(
                    key for key, said in page_made.tasks.items() if not said.startswith("1:")
                )
                # The joined notes as the page showed them ticked; each card whose joined
                # tick was changed must then do exactly what the page said, with no retry.
                joined_signed = frozenset(
                    join for join, ticked in page_made.joined.items() if ticked
                )
                if as_signed == read.left_out and joined_signed == read.joined:
                    return False
                try:
                    before = changes_for(
                        with_joined(as_shown, joined_signed, now=now).items,
                        state.project_state,
                        occurrences=occurrences,
                        kinds=kinds,
                        instruction_answers=instruction_answers,
                        identities=identities,
                        picks=picks,
                        left_out=as_signed,
                    )
                except (
                    UnreadableClaim,
                    UnreadableDecision,
                    UnreadableInstruction,
                    UnreadableCapture,
                    sqlite3.Error,
                ):
                    return False
                return not tasks_moved(before, page_made.tasks, answered) and not (
                    joins_moved_from(
                        before,
                        {card: as_shown for card, (as_shown, _) in joins.items()},
                        retry=False,
                    )
                )

            if not page_made.verified:
                notice, code, about = REVIEW_UNVERIFIED, status.HTTP_409_CONFLICT, frozenset[int]()
            elif await asyncio.to_thread(by_the_selections):
                notice, code, about = SELECTIONS_CHANGE, status.HTTP_200_OK, kept.moved
            else:
                notice, code, about = (
                    REVIEW_NO_LONGER_MATCHES,
                    status.HTTP_409_CONFLICT,
                    kept.moved,
                )
            return await asyncio.to_thread(
                preview_or_recovery,
                request,
                state,
                read,
                draft,
                kept_answers,
                occurrences=occurrences,
                kinds=kinds,
                instruction_answers=instruction_answers,
                carried=carried,
                made=page_made,
                identities=identities,
                linked=linked,
                picks=picks,
                search=search,
                typed=typed,
                refused="stale",
                check=about,
                notice=notice,
                status_code=code,
                editing=held_open,
            )
        return await asyncio.to_thread(
            preview_or_recovery,
            request,
            state,
            read,
            draft,
            kept_answers,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            carried=carried,
            made=page_made,
            identities=identities,
            linked=linked,
            picks=picks,
            search=search,
            typed=typed,
            refused="stale",
            notice=CHANGED_SINCE_SHOWN if instructions_moved else HOMEWORK_CHANGED,
            status_code=status.HTTP_409_CONFLICT,
            editing=held_open,
        )
    if not isinstance(kept, Kept):
        open_questions = {change.key for change in kept if change.state == REVIEW}
        # A question about the school's instructions left without an answer: the page says
        # so first, with the focus on it, and every answer given elsewhere stays as given.
        unanswered = any(change.instructions_asked for change in kept)
        # A choice made on a card that now asks which homework it is: the page says so first,
        # with the focus on that question, and says the choice back as not saved.
        held_back = any(
            change.ambiguous and (change.instructions_unsaved or change.key in carried)
            for change in kept
        )
        conflicting = conflicting_choices(kept)
        if conflicting:
            notice = CHOOSE_ONE_TYPE
        elif held_back and not unanswered:
            notice = IDENTIFY_FIRST
        elif open_questions <= asked_on(form) | set(joins):
            # A question on a card whose joined tick changed is one the page said that tick
            # asks, which the save checked, so it is put as the page's own.
            notice = CHOOSE_INSTRUCTIONS if unanswered else NEEDS_ANSWER
        else:
            notice = LOOK_AGAIN
        refused = None
        if not conflicting and unanswered:
            refused = "unanswered"
        elif not conflicting and held_back:
            refused = "waiting"
        elif not conflicting and any(change.identity_asked for change in kept):
            refused = "identify"
        return await asyncio.to_thread(
            preview_or_recovery,
            request,
            state,
            read,
            draft,
            kept_answers,
            occurrences=occurrences,
            kinds=kinds,
            instruction_answers=instruction_answers,
            carried=carried,
            made=page_made,
            identities=identities,
            linked=linked,
            picks=picks,
            search=search,
            typed=typed,
            refused=refused,
            notice=notice,
            editing=held_open,
        )
    note = "&kept_note=1" if kept.kept_notes else ""
    note += f"&linked={kept.linked}" if kept.linked else ""
    return RedirectResponse(
        f"/parent?added={kept.added}&updated={kept.updated}&unchanged={kept.unchanged}{note}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
