# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Saving homework from a screenshot review.

A press of "Save selected homework" writes in this order, each step its own commit:

1. the press's answers, through the draft store (``ImportDraftsStore.keep_answers``);
2. the homework transaction, here: one ``comparing_and_writing`` block on the record store's
   connection that checks the press, writes the homework, the earlier-pick decisions, the
   acceptance with its ``[piece name, id]`` pairs, and deletes the saved pieces' answers;
3. closing the review when the save finished it.

"Nothing was saved" is true only when the homework transaction never committed. Once it has, a
failure closing the review, or any later step, leaves the save whole and is reported beside
it, and a resent press replays the save by its acceptance id.
"""

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Final, cast

from blossom import intake
from blossom.candidates import readings_for
from blossom.captures import candidate_basis
from blossom.clock import Clock, day_at
from blossom.intake import (
    KNOWN,
    UPDATE,
    Change,
    IdentityAnswer,
    Kept,
    Reading,
    changes_for,
    keep,
)
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import InstructionChoice
from blossom.screenshot_review import (
    DATE_FIELDS,
    KINDS,
    PIECE_DATES,
    Corrections,
    Destination,
    EarlierPick,
    ListEntry,
    Piece,
    PreviewEntry,
    SavedRow,
    Validation,
    answer_names,
    date_target,
    dates_key,
    defaults,
    destination_kind,
    entries,
    instruction_field,
    instruction_index,
    pieces_from,
    preview,
    slot_id,
    slot_occurrence,
    subject_for,
    subject_now,
    typed_day,
    unit_name,
    usable_correction,
    validate,
    value_json,
    value_of,
    wordless_card,
    wordless_field,
    written_dates,
)
from blossom.screenshots import (
    CardPlace,
    HomeworkReading,
    PlacedCard,
    ScreenshotRow,
    cards_placed,
    homework_rows,
)
from blossom.stores.gradebook import (
    AnswerNotAsked,
    ConfirmedBy,
    GradeTransactionLost,
    NameFormNotSaved,
)
from blossom.stores.homework_saves import DraftForSave, HomeworkAcceptance
from blossom.stores.import_drafts import ImportDraftsStore, StoredReading
from blossom.stores.intake_decisions import DecisionToKeep
from blossom.stores.project_state import Assignment, AssignmentKind, ProjectStateStore

# ------------------------------------------------------------------ the seam for crash tests


class SaveHooks:
    """A test-only seam: each method runs at a named point of a save and does nothing. It holds
    no state and decides nothing; production never passes one."""

    def after_keep(self, assignment_id: str) -> None:
        """After one destination's homework is written, inside the transaction."""

    def after_renamed(self, assignment_id: str) -> None:
        """After an earlier-pick decision is written, inside the transaction."""

    def before_acceptance(self) -> None:
        """Before the acceptance and the answer deletes, inside the transaction."""

    def after_commit(self) -> None:
        """After the homework transaction commits, before the review is closed."""


NO_HOOKS = SaveHooks()


# ------------------------------------------------------------------ a press and its outcome


@dataclasses.dataclass(frozen=True)
class SavePress:
    """A "Save selected homework" press, after its answers are kept: the review, its version
    after them, the page's acceptance id, the pressed units and their signed digests, the
    defaults the page showed, the typed corrections, and what each read screenshot's student
    line is against her record. The answers to the name question are read from the review's
    own stored answers, inside the save."""

    student_id: str
    draft_id: str
    version: int
    acceptance_id: str
    units: tuple[str, ...]
    digests: Mapping[str, tuple[str, ...]]
    defaults_posted: Mapping[str, str]
    corrections: Corrections
    names: Mapping[str, tuple[str, str | None]]
    """Each read image's student line, by its SHA-256: its status against her record
    (``matches``, ``first_use``, ``not_confirmed``, ``confirm_again`` or ``missing``) and its
    keyed form, which keys the stored answer to the name question."""
    name_key: bytes
    """The key name forms are made under, for "Yes, this is her name" to add the keyed form."""
    role: str = "parent"


@dataclasses.dataclass(frozen=True)
class Saved:
    """The homework transaction committed: what it saved, which pressed units it didn't, and
    what after it didn't complete."""

    acceptance_id: str
    pairs: tuple[tuple[str, str], ...]
    saved_units: tuple[str, ...]
    changed_units: tuple[str, ...] = ()
    already_units: tuple[str, ...] = ()
    closed: bool = False
    incomplete: tuple[str, ...] = ()
    counts: tuple[int, int, int, int] = (0, 0, 0, 0)
    """Assignments added, updated and already saved by this save, and what is left to check:
    open pieces, rows waiting on their split question and "couldn't read" items."""


@dataclasses.dataclass(frozen=True)
class Replayed:
    """The acceptance id was recorded already: the earlier save's outcome, and nothing written."""

    acceptance_id: str
    pairs: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class AlreadySaved:
    """Every pressed unit was saved by another press of this review; nothing written."""

    units: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class NotSaved:
    """The homework transaction didn't commit, so nothing of the homework was written."""

    reason: str
    """``absent``, ``closed``, ``expired``, ``stale``, ``gate``, ``open`` (a pressed unit still
    has a question, a field to confirm or the name question to answer), ``names`` (pressed units
    from screenshots whose names need different answers), ``changed``, ``busy`` or
    ``failed``."""
    changed_units: tuple[str, ...] = ()
    already_units: tuple[str, ...] = ()


SaveOutcome = Saved | Replayed | AlreadySaved | NotSaved


class _Changed(Exception):
    """Inside the transaction: the record answered a write differently from the preview."""


# ------------------------------------------------------------------ the record as read


class OneTarget:
    """The record with every assignment but one hidden, so a reading lands on that one, or on a
    new row when none is shown. Everything else is the store's own."""

    def __init__(self, store: ProjectStateStore, shown: str | None) -> None:
        self._store = store
        self._shown = shown

    def all_assignments(self) -> list[Assignment]:
        """The shown assignment alone, or nothing."""
        return [a for a in self._store.all_assignments() if a.assignment_id == self._shown]

    def record_intake_decision(self, *_: object, **__: object) -> None:
        """Nothing: the parent's choice of the shown assignment answers the record's question of
        which homework a card is about only to land the card there. A review of screenshots
        keeps no such answer as a decision; its only decisions are the ``renamed`` ones the
        save writes itself."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the store's own members
        return getattr(self._store, name)


def _view(store: ProjectStateStore, shown: str | None) -> ProjectStateStore:
    """The record as ``keep`` and ``changes_for`` read it, with one assignment shown."""
    return cast("ProjectStateStore", OneTarget(store, shown))


def chosen_answers(
    readings: tuple[Reading, ...],
    view: ProjectStateStore,
    target: str | None,
    kinds: Mapping[int, AssignmentKind] | None = None,
) -> dict[int, IdentityAnswer]:
    """The parent's choice of ``target`` as the answer to each card's question of which
    homework it is about, where the record asks one that lists it, as it asks before landing a
    card on homework made from her note."""
    if target is None:
        return {}
    return {
        change.key: IdentityAnswer(target, change.identity_shown, change.identity_basis)
        for change in changes_for(readings, view, kinds=kinds)
        if change.identity_asked and target in change.identity_shown
    }


def saved_rows(record: ProjectStateStore) -> dict[str, SavedRow]:
    """Every saved assignment as the review compares pieces with it."""
    everything = record.all_assignments()
    readable = record.school_instruction_readings([a.assignment_id for a in everything]).readable
    out = {}
    for item in everything:
        standing = readable.get(item.assignment_id)
        words = frozenset() if standing is None else frozenset(i.text for i in standing.kept)
        current = () if standing is None else standing.texts
        out[item.assignment_id] = SavedRow(
            item.assignment_id,
            item.course,
            item.title,
            item.due_date,
            item.assigned_on,
            words,
            current[0] if current else None,
            max(0, len(current) - 1),
            str(item.kind.value),
        )
    return out


def earlier_picks(record: ProjectStateStore, pieces: Sequence[Piece]) -> list[EarlierPick]:
    """The record's ``renamed`` decisions for the pieces' names."""
    decided = record.intake_decisions({p.pair for p in pieces})
    return [
        EarlierPick(item.course, item.title, item.lands_on, item.sequence)
        for answers in decided.values()
        for item in answers
        if item.kind == "renamed"
    ]


def landing(record: ProjectStateStore, reading: Reading, target: str) -> Reading:
    """The reading as it lands on ``target``: under the target's class and title when they
    differ, as for a linked or found row."""
    row = next((a for a in record.all_assignments() if a.assignment_id == target), None)
    if row is not None and intake.pair(row.course, row.title) != reading.pair:
        return dataclasses.replace(reading, course=row.course, title=row.title)
    return reading


def writes_nothing_test(
    record: ProjectStateStore, kinds: Mapping[str, str]
) -> Callable[[Piece, str], bool]:
    """The record's own test of whether landing a piece on an assignment writes nothing: every
    claim, report and instruction kept there already, and no kind or date to change."""

    def check(piece: Piece, target: str) -> bool:
        if piece.reading is None or not any(
            a.assignment_id == target for a in record.all_assignments()
        ):
            return False
        chosen = {0: AssignmentKind(kinds[piece.name])} if piece.name in kinds else None
        view = _view(record, target)
        readings = (landing(record, piece.reading, target),)
        answers = chosen_answers(readings, view, target, chosen)
        [change] = changes_for(readings, view, kinds=chosen, identities=answers or None)
        return (
            change.on_record is not None
            and change.on_record.assignment_id == target
            and change.state == KNOWN
        )

    return check


def instruction_question(
    record: ProjectStateStore, dest: tuple[str, ...], members: Sequence[Piece]
) -> Change | None:
    """The change carrying the record's question about which instructions apply, for every
    piece going to an existing destination; None when nothing is asked."""
    if dest[0] == "new" or not members:
        return None
    readings = tuple(
        landing(record, p.reading, dest[1])
        for p in sorted(members, key=lambda q: q.name)
        if p.reading
    )
    view = _view(record, dest[1])
    changes = changes_for(
        readings,
        view,
        occurrences=dict.fromkeys(range(len(readings)), UPDATE),
        identities=chosen_answers(readings, view, dest[1]) or None,
    )
    return next((c for c in changes if c.instructions_asked), None)


def instruction_subject(dest: tuple[str, ...], names: Sequence[str], revision: int) -> str:
    """The instruction answer's subject: the destination, its sorted pieces and the revision
    shown."""
    return json.dumps([list(dest), sorted(names), revision], separators=(",", ":"))


CONFIRMABLE: Final = frozenset({"badge", "course", "title", "instruction", "footer", "heading"})
"""The fields a reader can mark uncertain that the parent confirms as read or corrects."""
UNCONFIRMED: Final = ("first_use", "not_confirmed", "confirm_again")
"""The statuses of a student line whose name question asks "Yes, this is her name"."""


CORRECTABLE: Final = (CONFIRMABLE - {"badge"}) | frozenset(PIECE_DATES)
"""The fields a parent may type a correction for: class and title, dates, and an instruction,
besides each instruction text by its index (``correctable``). The due or assigned label is
confirmed as read, never typed."""
MANY_LINES: Final = frozenset({"instruction"})
"""The fields whose correction is typed in a box of several lines, each instruction text's
among them."""


def correctable(field: str) -> bool:
    """Whether a field takes a typed correction."""
    return field in CORRECTABLE or instruction_field(field)


def correction_text(field: str, value: str) -> str:
    """A typed correction as it is kept: in a box of several lines, which a browser posts with
    a carriage return and a line feed, every line ending made one line feed, as a paste's are;
    otherwise as typed."""
    if field not in MANY_LINES and not instruction_field(field):
        return value
    return value.replace("\r\n", "\n").replace("\r", "\n")


def canonical(text: str) -> str:
    """A text as a box and its value with no correction are compared: line endings made line
    feeds, then stripped."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def read_digest(text: str) -> str:
    """The digest a box carries beside it of its value with no correction, which a box posted
    unchanged matches once canonical: read only to give typing back from a review that has
    gone."""
    return hashlib.sha256(canonical(text).encode()).hexdigest()[:16]


def confirm_fields(piece: Piece) -> frozenset[str]:
    """The fields of a piece the parent confirms or corrects. An unsure day heading names the
    date of the card it is over: its due date over a due card, which the due line names too, so
    one due-date field (``footer``) stands for both; its assigned date over an assigned card
    (``heading``)."""
    fields = {f for f in piece.uncertain if f in CONFIRMABLE}
    if "heading" not in fields:
        return frozenset(fields)
    unsure = [one for one in piece.seen if one.heading_unsure]
    if not unsure:
        if date_target(piece.reading, "heading") == "due":
            fields = (fields - {"heading"}) | {"footer"}
        return frozenset(fields)
    fields.discard("heading")
    if any(one.card == "due" for one in unsure):
        fields.add("footer")
    if any(one.card == "assigned" for one in unsure):
        fields.add("heading")
    return frozenset(fields)


def fold_answers(piece: Piece, field: str, corrections: Corrections) -> bool:
    """Whether a correction Blossom can use answers an unsure field: the row's box for its
    class, title or instructions, and the piece's own box for its due date (``footer``) or
    assigned date (``heading``), an assigned date given as none among them. The instructions
    are answered card by card: each of the piece's cards whose instructions the reader wasn't
    sure of only by its own correction, a card that read words by a replacement or removal of
    the text it read (one text read by two cards answers for both), a card read with none by
    words typed in its own empty box; nothing typed for another card stands for it. Where no
    card of the piece is marked so, an instruction correction answers where it takes effect:
    for a text the row has, or for one of the row's cards read with no words. In saved
    progress of shape 3, the field's own correction answers it."""
    row = corrections.get(piece.key, {})
    dates = corrections.get(dates_key(piece.name), {})
    if usable_correction(field, row.get(field)):
        return True
    if field == "instruction":
        own = own_wordless(piece)
        read_words = own_unsure_texts(piece)
        if own or read_words:
            boxes = [f"instruction:{index}" for index in read_words]
            return all(
                name in row and (row[name] is None or usable_correction(name, row[name]))
                for name in boxes
            ) and all(usable_correction(f, row.get(f)) for f in map(wordless_field, own))
        read = piece.as_read
        texts = 0 if read is None else len(read.texts)
        cards = () if read is None else read.wordless
        for name, value in row.items():
            index = instruction_index(name)
            if index is not None and index < texts:
                if value is None or usable_correction(name, value):
                    return True
            elif wordless_card(name) in cards and usable_correction(name, value):
                return True
        return False
    if field == "footer":
        return usable_correction("due", dates.get("due"))
    if field == "heading":
        given = dates.get("assigned")
        return "assigned" in dates and (given is None or usable_correction("assigned", given))
    return False


def own_unsure_texts(piece: Piece) -> tuple[int, ...]:
    """The indexes of the texts the piece's own cards read whose instructions the reader wasn't
    sure of, each once."""
    read = piece.as_read
    if read is None:
        return ()
    return tuple(dict.fromkeys(index for card, index in read.unsure_texts if card in piece.cards))


def own_wordless(piece: Piece) -> tuple[CardPlace, ...]:
    """The piece's own cards read with no instruction words whose instructions the reader
    wasn't sure of: each offers one empty Instructions box, among its row's boxes, whose words
    go to this piece."""
    read = piece.as_read
    return () if read is None else tuple(c for c in read.wordless if c in piece.cards)


def fold_reads(state: "ReviewState") -> dict[tuple[str, str], str | None]:
    """Each box the review's open cards offer, by key and field, with its value with no
    correction as a box is compared with it: a row's class, title and each instruction text,
    canonical, and the empty box of each open card read with no words it wasn't sure of; a
    piece's due date (none while which due date waits) and assigned date, as ISO dates or
    None."""
    out: dict[tuple[str, str], str | None] = {}
    for piece in state.open:
        read = piece.as_read
        if read is None:
            continue
        out[(piece.key, "course")] = canonical(read.course)
        out[(piece.key, "title")] = canonical(read.title)
        for index, text in enumerate(read.texts):
            out[(piece.key, f"instruction:{index}")] = canonical(text)
        for card in own_wordless(piece):
            out[(piece.key, wordless_field(card))] = ""
        key = dates_key(piece.name)
        out[(key, "due")] = None if read.due is None else read.due.isoformat()
        out[(key, "assigned")] = None if read.assigned is None else read.assigned.isoformat()
    return out


def unreadable_dates(piece: Piece, corrections: Corrections) -> tuple[str, ...]:
    """The piece's own dates typed as text Blossom can't read as a date: kept as typed, they
    leave the piece open, so nothing of them is saved."""
    dates = corrections.get(dates_key(piece.name), {})
    return tuple(
        field
        for field in PIECE_DATES
        if isinstance(dates.get(field), str) and not usable_correction(field, dates.get(field))
    )


def confirm_subject(key: str, field: str) -> str:
    """A confirmation's subject: the row key and the field."""
    return json.dumps([key, field], separators=(",", ":"))


def student_line(body: str) -> str | None:
    """The selected student's name as a stored homework reading holds it; it is passed to the
    record only to make its keyed form, and never kept elsewhere."""
    try:
        return HomeworkReading.model_validate_json(body).student.name
    except ValueError:
        return None


def _confirmed_by(role: str) -> ConfirmedBy:
    """Who answered "Yes, this is her name": a parent, or the household with the sign-in off."""
    return "household" if role == "household" else "parent"


def correction_digest(
    fields: Mapping[str, str | None] | None, dates: Mapping[str, str | None] | None = None
) -> str:
    """The digest of the corrections an answer was stored with: its row's, and for a
    destination its piece's own dates under ``date``. With none it is the digest of nothing."""
    merged: dict[str, object] = dict(sorted((fields or {}).items()))
    if dates:
        merged["date"] = dict(sorted(dates.items()))
    text = json.dumps(merged, separators=(",", ":")) if merged else ""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def piece_digest(piece: Piece, corrections: Corrections) -> str:
    """The digest a piece's destination answer is stored with: its row's corrections and its
    own dates, so an edit of another piece's date, or words typed for another piece's card read
    with none, leaves it."""
    row = {
        field: value
        for field, value in (corrections.get(piece.key) or {}).items()
        if (card := wordless_card(field)) is None or card in piece.cards
    }
    return correction_digest(row, corrections.get(dates_key(piece.name)))


# ------------------------------------------------------------------ the review's state


Rework = Callable[[Mapping[str, Destination]], tuple[Validation, tuple[PreviewEntry, ...]]]
"""Another mapping's checks and preview, against the same record and answers."""


@dataclasses.dataclass(frozen=True)
class ReviewState:
    """Everything a review page and a save work from, built from the draft's tables and the
    record as one read sees them."""

    pieces: tuple[Piece, ...]
    asking: tuple[ScreenshotRow, ...]
    saved: Mapping[str, str]
    rows: Mapping[str, SavedRow]
    picks: tuple[EarlierPick, ...]
    stored: Mapping[str, Destination]
    lost: frozenset[str]
    defaults: Mapping[str, Destination]
    defaulted: frozenset[str]
    mapping: Mapping[str, Destination]
    kinds: Mapping[str, str]
    values: Mapping[tuple[str, str], str]
    validation: Validation
    entries: Mapping[str, list[ListEntry]]
    preview: tuple[PreviewEntry, ...]
    asked: Mapping[tuple[str, ...], Change | None]
    rework: "Rework" = dataclasses.field(default=lambda _: (Validation((), {}), ()), compare=False)
    """The checks and preview another mapping of these pieces would have, as an offer shows it,
    against the same record and answers."""

    @property
    def by_name(self) -> dict[str, Piece]:
        """The pieces by name."""
        return {p.name: p for p in self.pieces}

    @property
    def open(self) -> list[Piece]:
        """The pieces this review hasn't saved."""
        return [p for p in self.pieces if p.name not in self.saved]


def old_first(
    answers: Mapping[tuple[str, str], tuple[str, str]],
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """A draft's stored answers, those naming a new slot by its class and title first, so an
    answer whose subject is in the form ``subject_for`` gives wins where both read alike."""
    return sorted(answers.items(), key=lambda item: subject_now(item[0][1]) == item[0][1])


def review_state(
    record: ProjectStateStore,
    tables: DraftForSave,
    *,
    now: datetime,
    corrections: Corrections,
    defaults_posted: Mapping[str, str] | None = None,
) -> ReviewState:
    """The review's pieces, destinations, checks and preview from its tables and the record.
    A default the press posted, and its answers kept, still counts as the default it was. A
    piece a change of screenshots marked, or whose answer depended on a correction that wasn't
    kept, is given no default: it waits for an answer."""
    values = {
        (kind, subject_now(subject)): value
        for (kind, subject), (value, _) in old_first(tables.answers)
    }
    split = {
        subject: json.loads(value) for (kind, subject), value in values.items() if kind == "split"
    }
    due_read = {subject: value for (kind, subject), value in values.items() if kind == "due_read"}
    pieces, asking, _ = pieces_from(
        tables.readings, split, tables.discards, now, corrections, due_read
    )
    by_name = {p.name: p for p in pieces}
    saved = {name: target for name, target in tables.saved.items() if name in by_name}
    rows = saved_rows(record)
    picks = earlier_picks(record, pieces)
    stored: dict[str, Destination] = {}
    lost: set[str] = set()
    for (kind, subject), (value, digest) in tables.answers.items():
        if kind != "destination" or subject in saved or subject not in by_name:
            continue
        if digest != piece_digest(by_name[subject], corrections):
            lost.add(subject)
            continue
        stored[subject] = value_of(value)
    open_pieces = [p for p in pieces if p.name not in saved]
    groups: dict[tuple[str, str], list[Piece]] = {}
    for piece in open_pieces:
        groups.setdefault(piece.pair, []).append(piece)
    by_row = {s: v for (k, s), v in values.items() if k == "kind" and v in KINDS}
    kinds = {p.name: by_row[p.key] for p in pieces if p.key in by_row}
    check = writes_nothing_test(record, kinds)
    asks = tables.marks | lost
    chosen = {
        name: value
        for name, value in defaults(open_pieces, groups, rows, picks, stored, check).items()
        if name not in asks
    }
    mapping: dict[str, Destination] = {**chosen, **stored}
    defaulted = set(chosen)
    for name, value in (defaults_posted or {}).items():
        if name in stored and value_json(stored[name]) == value:
            defaulted.add(name)
    dates = {
        key: value
        for key, value in values.items()
        if key[0] in ("due", "assigned") or (key[0] == "kind" and key[1].startswith("["))
    }
    listed = {p.name: entries(p, groups[p.pair], rows, picks, stored) for p in open_pieces}
    entry_ids = {
        name: [e.stands_for for e in found if e.kind in ("listed", "linked") and e.stands_for]
        for name, found in listed.items()
    }

    def work(
        chosen: Mapping[str, Destination],
        chosen_defaulted: Iterable[str],
        asked: dict[tuple[str, ...], Change | None],
    ) -> tuple[Validation, tuple[PreviewEntry, ...]]:
        def instruction_ready(dest: tuple[str, ...], members: Sequence[Piece]) -> bool:
            change = instruction_question(record, dest, members)
            asked[dest] = change
            if change is None:
                return True
            names = [m.name for m in members]
            return (
                "instructions",
                instruction_subject(dest, names, change.instructions_revision),
            ) in values

        def instructions(
            dest: tuple[str, ...], members: Sequence[Piece]
        ) -> tuple[int | None, tuple[str, ...] | None]:
            change = asked.get(dest)
            if change is None:
                return None, None
            names = [m.name for m in members]
            subject = instruction_subject(dest, names, change.instructions_revision)
            got = values.get(("instructions", subject))
            return change.instructions_revision, None if got is None else tuple(json.loads(got))

        found = validate(pieces, chosen, rows, picks, saved, dates, instruction_ready, kinds)
        return found, tuple(
            preview(
                found,
                pieces,
                chosen,
                rows,
                dates,
                chosen_defaulted,
                entry_ids,
                instructions,
                picks,
                kinds,
                check,
            )
        )

    asked: dict[tuple[str, ...], Change | None] = {}
    validation, shown = work(mapping, defaulted, asked)

    def rework(other: Mapping[str, Destination]) -> tuple[Validation, tuple[PreviewEntry, ...]]:
        kept = {n for n in defaulted if n in other and other[n] == mapping.get(n)}
        return work(other, kept, {})

    return ReviewState(
        tuple(pieces),
        tuple(asking),
        saved,
        rows,
        tuple(picks),
        stored,
        frozenset(lost),
        chosen,
        frozenset(defaulted),
        mapping,
        kinds,
        values,
        validation,
        listed,
        shown,
        asked,
        rework,
    )


def unit_digests(state: ReviewState) -> dict[str, tuple[str, ...]]:
    """Each unit's preview digests, as a page signs them and a save compares them."""
    out: dict[str, list[str]] = {}
    for entry in state.preview:
        out.setdefault(unit_name(entry.unit), []).append(entry.digest)
    return {name: tuple(sorted(found)) for name, found in out.items()}


# ------------------------------------------------------------------ the save


def save_homework(
    record: ProjectStateStore,
    drafts: ImportDraftsStore,
    press: SavePress,
    *,
    clock: Clock,
    household: Clock,
    hooks: SaveHooks = NO_HOOKS,
) -> SaveOutcome:
    """The homework transaction, then closing the review when the save finished it. The real
    clock is read once, after the store's lock and the file's writer are held, and that one
    instant, with the household's date at it, serves every check and write of the
    transaction: a save that waited past the review's expiry is refused, and one whose checks
    passed completes even if the review expires before it commits. A failure after the commit
    is reported in ``incomplete`` and never undoes or hides the save."""
    try:
        with record.comparing_and_writing():
            now = clock.now()
            outcome = _homework_transaction(record, press, now, day_at(household, now), hooks)
    except (_Changed, AnswerNotAsked):
        return NotSaved("changed")
    except (NameFormNotSaved, GradeTransactionLost):
        return NotSaved("failed")
    except sqlite3.OperationalError as error:
        busy = error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        return NotSaved("busy" if busy else "failed")
    except sqlite3.Error:
        return NotSaved("failed")
    if not isinstance(outcome, Saved):
        return outcome
    closed = False
    incomplete: list[str] = []
    try:
        hooks.after_commit()
        if drafts.snapshot(press.student_id, press.draft_id).finished:
            closed = drafts.clean_up(press.student_id, press.draft_id)
    except Exception:
        incomplete.append("close")
    return dataclasses.replace(outcome, closed=closed, incomplete=tuple(incomplete))


def _homework_transaction(
    record: ProjectStateStore, press: SavePress, now: datetime, today: date, hooks: SaveHooks
) -> SaveOutcome:
    """Inside the writer's transaction: a recorded acceptance id first, the draft's checks, the
    comparison, then the homework, its decisions, the acceptance and the answer deletes."""
    recorded = record.homework_acceptance(press.student_id, press.draft_id, press.acceptance_id)
    if recorded is not None:
        return Replayed(press.acceptance_id, recorded)
    tables = record.draft_for_save(press.student_id, press.draft_id)
    if tables is None:
        return NotSaved("absent")
    if tables.closed_at is not None:
        return NotSaved("closed")
    if now >= tables.expires_at:
        return NotSaved("expired")
    if tables.version != press.version:
        return NotSaved("stale")
    if tables.unread:
        return NotSaved("gate")
    state = review_state(
        record,
        tables,
        now=now,
        corrections=press.corrections,
        defaults_posted=press.defaults_posted,
    )
    digests = unit_digests(state)
    ready: list[tuple[str, ...]] = []
    changed: list[str] = []
    already: list[str] = []
    still_open: list[str] = []
    for name in press.units:
        if all(n in state.saved for n in name.split("+")):
            already.append(name)
            continue
        unit = next((u for u in state.validation.units if unit_name(u.unit) == name), None)
        if (
            unit is None
            or unit.refusals
            or unit.stale
            or tuple(sorted(press.digests.get(name, ()))) != digests.get(name, ())
        ):
            changed.append(name)
            continue
        if not unit.ready or _unconfirmed(state, unit.unit, tables.answers, press.corrections):
            still_open.append(name)
            continue
        ready.append(unit.unit)
    if still_open:
        return NotSaved("open", tuple(still_open), tuple(already))
    identity = _identity(state, ready, press.names, tables.answers)
    if isinstance(identity, NotSaved):
        return identity
    if not ready:
        if already and not changed:
            return AlreadySaved(tuple(already))
        return NotSaved("changed", tuple(changed), tuple(already))
    pairs: list[tuple[str, str]] = []
    by_entry = {entry.dest: entry for entry in state.preview}
    added = updated = unchanged = 0
    written: list[str] = []
    for chosen in ready:
        for dest, names in sorted(state.validation.destinations.items()):
            if not set(names) <= set(chosen):
                continue
            target = _write(record, state, dest, names, now, today, hooks)
            pairs += [(n, target) for n in sorted(names)]
            entry = by_entry[dest]
            if dest[0] == "new":
                added += 1
            elif entry.nothing_new:
                unchanged += 1
            else:
                updated += 1
            if dest[0] == "new" or not entry.nothing_new:
                written += names
    status, answer, form = identity
    if answer == "hers":
        line = next(
            (
                student_line(r.body)
                for r in tables.readings
                if press.names.get(r.sha256, ("", None)) == (status, form)
            ),
            None,
        )
        if line is None:
            raise _Changed
        if status == "confirm_again":
            record.confirm_name_again(press.name_key, line, _confirmed_by(press.role))
        else:
            record.add_name_form(press.name_key, line, _confirmed_by(press.role))
    hooks.before_acceptance()
    saved_now = {name for name, _ in pairs} | set(state.saved)
    whole_rows = {
        p.key
        for p in state.pieces
        if all(q.name in saved_now for q in state.pieces if q.key == p.key)
    }
    done = saved_now | whole_rows

    def saved_with(kind: str, subject: str) -> bool:
        """Whether an answer goes with this save: every answer about the saved pieces, their
        rows' confirmations among them; never the split, nor the name answer, which stays."""
        if kind in ("split", "name"):
            return False
        if kind == "confirm":
            return str(json.loads(subject)[0]) in whole_rows
        about = answer_names(kind, subject)
        return bool(about) and set(about) <= done

    consumed = [(kind, subject) for kind, subject in tables.answers if saved_with(kind, subject)]
    left = _open_units(state, saved_now, tables.readings, tables.discards)
    corrected, confirmed = _correction_counts(
        state, written, tables.answers, press.corrections, tables.readings
    )
    record.add_homework_acceptance(
        HomeworkAcceptance(
            acceptance_id=press.acceptance_id,
            student_id=press.student_id,
            draft_id=press.draft_id,
            pairs=tuple(pairs),
            identity_status=status,
            identity_answer=answer,
            identity_form=form,
            added=added,
            updated=updated,
            already_saved=unchanged,
            left_to_check=left,
            shown=_open_units(state, state.saved, tables.readings, tables.discards),
            answers_kept=len(consumed),
            complete=left == 0,
            corrected=corrected,
            confirmed=confirmed,
            role=press.role,
        ),
        now,
    )
    record.drop_saved_answers(press.draft_id, consumed)
    return Saved(
        press.acceptance_id,
        tuple(pairs),
        tuple(unit_name(u) for u in ready),
        tuple(changed),
        tuple(already),
        counts=(added, updated, unchanged, left),
    )


def _open_units(
    state: ReviewState,
    saved: Collection[str],
    readings: Sequence[StoredReading],
    discards: Collection[str],
) -> int:
    """What still needs checking with the pieces named saved: each other piece, each row
    waiting on its split question, and each "couldn't read" item not set aside. It is 0
    exactly when the review has no open row."""
    pieces = sum(1 for piece in state.pieces if piece.name not in saved)
    items = sum(
        1 for row in homework_rows(readings) if not row.readable and row.key not in discards
    )
    return pieces + len(state.asking) + items


def _correction_counts(
    state: ReviewState,
    written: Collection[str],
    answers: Mapping[tuple[str, str], tuple[str, str]],
    corrections: Corrections,
    readings: Sequence[StoredReading],
) -> tuple[int, int]:
    """The two counts of the rows whose destination this save wrote, leaving out rows another
    save accepted: the boxes corrected, and the uncertain fields confirmed as read and not
    corrected. A box counts once, when Blossom can use its correction and it differs from
    every value the screenshot gave for its field, so a date chosen among the dates read is
    none; a removed instruction text or assigned date always counts."""
    taken = {state.by_name[name].key for name in state.saved if name in state.by_name}
    placed = cards_placed(readings)
    rows: dict[str, Piece] = {}
    pieces: list[Piece] = []
    unsure: dict[tuple[str, str], Piece] = {}
    for name in sorted(written):
        piece = state.by_name[name]
        if piece.key in taken:
            continue
        rows.setdefault(piece.key, piece)
        pieces.append(piece)
        for field in sorted(confirm_fields(piece)):
            unsure.setdefault((piece.key, field), piece)
    corrected = 0
    fixed: set[tuple[str, str]] = set()
    boxes = [
        (key, piece, field, value)
        for key, piece in rows.items()
        for field, value in sorted(corrections.get(key, {}).items())
    ]
    boxes += [
        (piece.key, piece, field, value)
        for piece in pieces
        for field, value in sorted(corrections.get(dates_key(piece.name), {}).items())
    ]
    for key, piece, field, value in boxes:
        if _corrects(piece, field, value, placed):
            corrected += 1
            fixed.add((key, CONFIRMED_BY.get(field, field.partition(":")[0])))
    confirmed = sum(
        1
        for key, field in unsure
        if (key, field) not in fixed and ("confirm", confirm_subject(key, field)) in answers
    )
    return corrected, confirmed


CONFIRMED_BY: Final = {"due": "footer", "assigned": "heading"}
"""The unsure field a piece's own date correction answers."""


def _corrects(
    piece: Piece, field: str, value: str | None, placed: Mapping[CardPlace, PlacedCard]
) -> bool:
    """Whether one box's correction counts as corrected: a removal always; else one Blossom
    can use whose value the screenshot gave nowhere for the field."""
    if value is None:
        return True
    if not usable_correction(field, value):
        return False
    got: object = typed_day(value) if field in DATE_FIELDS else value.strip()
    return got not in _values_read(piece, field, placed)


def _values_read(
    piece: Piece, field: str, placed: Mapping[CardPlace, PlacedCard]
) -> frozenset[object]:
    """Every value the screenshot gave for the date or words a piece's field sets: for the due
    date, every due line and each due card's day heading, since both name its due date; for
    the assigned date, each assigned card's day heading; for text, the card's own words."""
    target = date_target(piece.reading, field)
    if target == "due":
        days = {one.line_day for one in piece.seen}
        days |= {one.heading_day for one in piece.seen if one.card == "due"}
        return frozenset(days - {None})
    if target == "assigned":
        return frozenset({one.heading_day for one in piece.seen if one.card == "assigned"} - {None})
    attribute = "instruction" if instruction_field(field) else field
    cards = [placed[c].card for c in piece.cards if c in placed]
    return frozenset(getattr(card, attribute) for card in cards if card is not None)


def _unconfirmed(
    state: ReviewState,
    unit: Sequence[str],
    answers: Mapping[tuple[str, str], tuple[str, str]],
    corrections: Corrections,
) -> bool:
    """Whether a field the reader wasn't sure of, on a row of the unit, is neither confirmed as
    read nor answered by a correction Blossom can use in the fold's box, a piece's due date
    waits for which due date Blossom uses, or a piece's date was typed as text Blossom can't
    read."""
    pieces = [state.by_name[name] for name in unit]
    return (
        any(
            ("confirm", confirm_subject(piece.key, field)) not in answers
            and not fold_answers(piece, field, corrections)
            for piece in pieces
            for field in confirm_fields(piece)
        )
        or any(not piece.due_resolved for piece in pieces)
        or any(unreadable_dates(piece, corrections) for piece in pieces)
    )


def _identity(
    state: ReviewState,
    ready: Sequence[tuple[str, ...]],
    names: Mapping[str, tuple[str, str | None]],
    answers: Mapping[tuple[str, str], tuple[str, str]],
) -> tuple[str, str, str | None] | NotSaved:
    """The status, answer and keyed form the save carries for the screenshots its units came
    from, the answer read from the review's stored name answers: one unconfirmed name,
    answered, with its form; else screenshots with no name, confirmed, with none; else every
    name matching, with their form when they show one. A unit whose screenshot has no answer
    yet is open, and units needing two different answers are refused. A unit drawing on both
    one unconfirmed name and screenshots with no name needs both answers, and carries the
    name's status, answer and form; a unit of unnamed screenshots alone, pressed beside it,
    is still refused."""
    images: dict[str, set[str]] = {}
    for unit in ready:
        for name in unit:
            for card in state.by_name[name].cards:
                images.setdefault(card.sha256, set()).add(unit_name(unit))
    seen = {sha: names.get(sha, ("unknown", None)) for sha in images}

    def units_where(test: Callable[[tuple[str, str | None]], bool]) -> set[str]:
        return {u for sha, units in images.items() if test(seen[sha]) for u in units}

    waiting = units_where(lambda line: line[0] == "unknown")
    forms = {form for status, form in seen.values() if status in UNCONFIRMED}
    missing = any(status == "missing" for status, _ in seen.values())
    merged = units_where(lambda line: line[0] == "missing") <= units_where(
        lambda line: line[0] in UNCONFIRMED
    )
    if forms and (len(forms) > 1 or (missing and not merged)):
        return NotSaved("names", tuple(sorted(units_where(lambda _: True))))
    if forms:
        [form] = forms
        status = next(s for s, f in seen.values() if f == form and s in UNCONFIRMED)
        got = answers.get(("name", str(form)), ("", ""))[0]
        if got not in ("hers", "misread"):
            waiting |= units_where(lambda line: line[1] == form)
        if missing and answers.get(("name", "missing"), ("", ""))[0] != "confirmed":
            waiting |= units_where(lambda line: line[0] == "missing")
        if waiting:
            return NotSaved("open", tuple(sorted(waiting)))
        return status, got, str(form)
    if missing and answers.get(("name", "missing"), ("", ""))[0] != "confirmed":
        waiting |= units_where(lambda line: line[0] == "missing")
    if waiting:
        return NotSaved("open", tuple(sorted(waiting)))
    if missing:
        return "missing", "confirmed", None
    matched = {form for status, form in seen.values() if status == "matches" and form}
    return "matches", "shown", next(iter(matched)) if len(matched) == 1 else None


def _write(
    record: ProjectStateStore,
    state: ReviewState,
    dest: tuple[str, ...],
    names: Sequence[str],
    now: datetime,
    today: date,
    hooks: SaveHooks,
) -> str:
    """One destination's homework, through ``keep``: a new slot's row made under its id, or the
    existing row updated, with the chosen dates, kind and instructions; then, for each class
    and title of its cards that isn't the row's, the earlier-pick decision, unless one for that
    name already lands there."""
    by_name = state.by_name
    members = sorted((by_name[n] for n in names), key=lambda p: p.name)
    row = state.rows.get(dest[1]) if dest[0] != "new" else None
    unit = state.validation.of(names[0])
    asked = [q for q in unit.questions if q.subject == subject_for(dest, names)]
    due, assigned, _ = written_dates(members, row, asked, state.values)
    written_kind, _ = destination_kind(dest, members, row, state.kinds, state.values)

    def kinds(count: int) -> dict[int, AssignmentKind]:
        if written_kind is None:
            return {}
        return dict.fromkeys(range(count), AssignmentKind(written_kind))

    if dest[0] == "new":
        lead, rest = members[0], members[1:]
        on_record = {a.assignment_id for a in record.all_assignments()}
        target = slot_id(lead.course, lead.title, names, on_record)
        tag = slot_occurrence(lead.course, lead.title, names, on_record)
        if lead.reading is None:
            raise _Changed
        first = dataclasses.replace(lead.reading, occurrence=tag)
        _keep(record, None, (first,), None, {0: (due, assigned)}, kinds(1), now, today)
        if rest:
            readings = tuple(landing(record, p.reading, target) for p in rest if p.reading)
            dates = dict.fromkeys(range(len(readings)), (due, assigned))
            _keep(record, target, readings, None, dates, kinds(len(readings)), now, today)
        hooks.after_keep(target)
        return target
    target = dest[1]
    choice = None
    change = state.asked.get(dest)
    if change is not None:
        subject = instruction_subject(dest, names, change.instructions_revision)
        got = state.values.get(("instructions", subject))
        shown = tuple(s.text for s in change.instructions_shown)
        applies = frozenset(json.loads(got)) if got is not None else frozenset()
        choice = InstructionChoice(
            change.instructions_revision, shown, applies, none_applies=not applies
        )
    readings = tuple(landing(record, p.reading, target) for p in members if p.reading)
    dates = dict.fromkeys(range(len(readings)), (due, assigned))
    _keep(record, target, readings, choice, dates, kinds(len(readings)), now, today)
    hooks.after_keep(target)
    named = {p.pair: p for p in members if p.reading is not None}
    for pair, piece in sorted(named.items()):
        if row is None or pair == row.pair:
            continue
        if any(
            p.pair == pair and p.assignment_id == target for p in earlier_picks(record, [piece])
        ):
            continue
        current = next(a for a in record.all_assignments() if a.assignment_id == target)
        record.record_intake_decision(
            DecisionToKeep(
                kind="renamed",
                course=pair[0],
                title=pair[1],
                due_date=due,
                lands_on=target,
                shown=(target,),
                basis=candidate_basis(readings_for(record, [current])),
            ),
            authored_by="parent",
            channel=SourceChannel.PARENT_ENTRY,
            now=now,
            today=today,
        )
        hooks.after_renamed(target)
    return target


def _keep(
    record: ProjectStateStore,
    target: str | None,
    readings: tuple[Reading, ...],
    choice: InstructionChoice | None,
    dates: Mapping[int, tuple[date | None, date | None]],
    kinds: Mapping[int, AssignmentKind],
    now: datetime,
    today: date,
) -> None:
    """``keep`` on the record with only ``target`` shown (or nothing, for a new slot), joined to
    the save's transaction, with the parent's choice of ``target`` as the answer where the
    record asks which homework a card is about: as written, then with each card's occurrence
    answered ``update`` and the instruction choice when the record asks. Anything but a save
    is a changed effect."""
    view = _view(record, target)
    answers = chosen_answers(readings, view, target, kinds or None) or None
    kept = keep(
        readings,
        view,
        kinds=kinds or None,
        identities=answers,
        dates=dates,
        imported_by="parent",
        now=now,
        today=today,
    )
    if not isinstance(kept, Kept):
        occurrences = dict.fromkeys(range(len(readings)), UPDATE)
        changes = changes_for(
            readings, view, occurrences=occurrences, kinds=kinds or None, identities=answers
        )
        chosen: dict[int, InstructionChoice] = {}
        carrier = next((c for c in changes if c.instructions_asked), None)
        if carrier is not None:
            shown = tuple(s.text for s in carrier.instructions_shown)
            chosen = {
                carrier.key: choice
                or InstructionChoice(carrier.instructions_revision, shown, frozenset(shown))
            }
        kept = keep(
            readings,
            view,
            occurrences=occurrences,
            kinds=kinds or None,
            instruction_answers=chosen,
            identities=answers,
            dates=dates,
            imported_by="parent",
            now=now,
            today=today,
        )
    if not isinstance(kept, Kept):
        raise _Changed
