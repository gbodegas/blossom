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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Final, cast

from blossom import intake
from blossom.candidates import readings_for
from blossom.captures import candidate_basis
from blossom.intake import KNOWN, UPDATE, Change, Kept, Reading, changes_for, keep
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import InstructionChoice
from blossom.screenshot_review import (
    KINDS,
    Destination,
    EarlierPick,
    ListEntry,
    Piece,
    PreviewEntry,
    SavedRow,
    Validation,
    answer_names,
    defaults,
    destination_kind,
    entries,
    pieces_from,
    preview,
    slot_id,
    slot_occurrence,
    subject_for,
    unit_name,
    validate,
    value_json,
    value_of,
    written_dates,
)
from blossom.screenshots import HomeworkReading, ScreenshotRow
from blossom.stores.gradebook import (
    AnswerNotAsked,
    ConfirmedBy,
    GradeTransactionLost,
    NameFormNotSaved,
)
from blossom.stores.homework_saves import DraftForSave, HomeworkAcceptance
from blossom.stores.import_drafts import ImportDraftsStore
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
    corrections: Mapping[str, Mapping[str, str]]
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
    """Assignments added, updated and already saved by this save, and pieces left to check."""


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

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the store's own members
        return getattr(self._store, name)


def _view(store: ProjectStateStore, shown: str | None) -> ProjectStateStore:
    """The record as ``keep`` and ``changes_for`` read it, with one assignment shown."""
    return cast("ProjectStateStore", OneTarget(store, shown))


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
        [change] = changes_for((landing(record, piece.reading, target),), view, kinds=chosen)
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
    changes = changes_for(
        readings,
        _view(record, dest[1]),
        occurrences=dict.fromkeys(range(len(readings)), UPDATE),
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


def correction_digest(fields: Mapping[str, str] | None) -> str:
    """The digest of a row's typed corrections an answer was stored with."""
    text = json.dumps(dict(sorted(fields.items())), separators=(",", ":")) if fields else ""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


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


def review_state(
    record: ProjectStateStore,
    tables: DraftForSave,
    *,
    now: datetime,
    corrections: Mapping[str, Mapping[str, str]],
    defaults_posted: Mapping[str, str] | None = None,
) -> ReviewState:
    """The review's pieces, destinations, checks and preview from its tables and the record.
    A default the press posted, and its answers kept, still counts as the default it was."""
    values = {key: value for key, (value, _) in tables.answers.items()}
    split = {
        subject: json.loads(value) for (kind, subject), value in values.items() if kind == "split"
    }
    pieces, asking, _ = pieces_from(tables.readings, split, tables.discards, now, corrections)
    by_name = {p.name: p for p in pieces}
    saved = {name: target for name, target in tables.saved.items() if name in by_name}
    rows = saved_rows(record)
    picks = earlier_picks(record, pieces)
    stored: dict[str, Destination] = {}
    lost: set[str] = set()
    for (kind, subject), (value, digest) in tables.answers.items():
        if kind != "destination" or subject in saved or subject not in by_name:
            continue
        if digest != correction_digest(corrections.get(by_name[subject].key)):
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
    chosen = defaults(open_pieces, groups, rows, picks, stored, check)
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
    now: datetime,
    today: date,
    hooks: SaveHooks = NO_HOOKS,
) -> SaveOutcome:
    """The homework transaction, then closing the review when the save finished it. A failure
    after the commit is reported in ``incomplete`` and never undoes or hides the save."""
    try:
        with record.comparing_and_writing():
            outcome = _homework_transaction(record, press, now, today, hooks)
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
    recorded = record.homework_acceptance(press.acceptance_id)
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
    for chosen in ready:
        for dest, names in sorted(state.validation.destinations.items()):
            if not set(names) <= set(chosen):
                continue
            target = _write(record, state, dest, names, now, today, hooks)
            pairs += [(n, target) for n in sorted(names)]
            entry = by_entry[dest]
            if dest[0] == "new":
                added += len(names)
            elif entry.nothing_new:
                unchanged += len(names)
            else:
                updated += len(names)
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
    left = len(state.open) - len({name for name, _ in pairs})
    record.add_homework_acceptance(
        HomeworkAcceptance(
            press.acceptance_id,
            press.student_id,
            press.draft_id,
            tuple(pairs),
            status,
            answer,
            added,
            updated,
            unchanged,
            left,
            len(state.open),
            len(consumed),
            left == 0 and not state.asking,
            len({state.by_name[name].key for name, _ in pairs} & set(press.corrections)),
            press.role,
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


def _unconfirmed(
    state: ReviewState,
    unit: Sequence[str],
    answers: Mapping[tuple[str, str], tuple[str, str]],
    corrections: Mapping[str, Mapping[str, str]],
) -> bool:
    """Whether a field the reader wasn't sure of, on a row of the unit, is neither confirmed as
    read nor corrected."""
    return any(
        ("confirm", confirm_subject(piece.key, field)) not in answers
        and not corrections.get(piece.key, {}).get(field)
        for piece in (state.by_name[name] for name in unit)
        for field in piece.uncertain
        if field in CONFIRMABLE
    )


def _identity(
    state: ReviewState,
    ready: Sequence[tuple[str, ...]],
    names: Mapping[str, tuple[str, str | None]],
    answers: Mapping[tuple[str, str], tuple[str, str]],
) -> tuple[str, str, str | None] | NotSaved:
    """The status and answer the save carries for the screenshots its units came from, the
    answer read from the review's stored name answers: one unconfirmed name, answered; else
    screenshots with no name, confirmed; else every name matching. A unit whose screenshot
    has no answer yet is open, and units needing two different answers are refused."""
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
    if forms and (len(forms) > 1 or missing):
        return NotSaved("names", tuple(sorted(units_where(lambda _: True))))
    if forms:
        [form] = forms
        status = next(s for s, f in seen.values() if f == form and s in UNCONFIRMED)
        got = answers.get(("name", str(form)), ("", ""))[0]
        if got not in ("hers", "misread"):
            waiting |= units_where(lambda line: line[1] == form)
        if waiting:
            return NotSaved("open", tuple(sorted(waiting)))
        return status, got, str(form)
    if missing and answers.get(("name", "missing"), ("", ""))[0] != "confirmed":
        waiting |= units_where(lambda line: line[0] == "missing")
    if waiting:
        return NotSaved("open", tuple(sorted(waiting)))
    return ("missing", "confirmed", None) if missing else ("matches", "shown", None)


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
    existing row updated, with the chosen dates, kind and instructions; then the earlier-pick
    decision for a row of another name, unless one for that name already lands there."""
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
    lead = members[0]
    if lead.reading is not None and row is not None and lead.pair != row.pair:
        landed = any(
            p.pair == lead.pair and p.assignment_id == target for p in earlier_picks(record, [lead])
        )
        if not landed:
            current = next(a for a in record.all_assignments() if a.assignment_id == target)
            record.record_intake_decision(
                DecisionToKeep(
                    kind="renamed",
                    course=lead.pair[0],
                    title=lead.pair[1],
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
    the save's transaction: as written, then with each card's occurrence answered ``update`` and
    the instruction choice when the record asks. Anything but a save is a changed effect."""
    view = _view(record, target)
    kept = keep(
        readings, view, kinds=kinds or None, dates=dates, imported_by="parent", now=now, today=today
    )
    if not isinstance(kept, Kept):
        occurrences = dict.fromkeys(range(len(readings)), UPDATE)
        changes = changes_for(readings, view, occurrences=occurrences, kinds=kinds or None)
        answers: dict[int, InstructionChoice] = {}
        carrier = next((c for c in changes if c.instructions_asked), None)
        if carrier is not None:
            shown = tuple(s.text for s in carrier.instructions_shown)
            answers = {
                carrier.key: choice
                or InstructionChoice(carrier.instructions_revision, shown, frozenset(shown))
            }
        kept = keep(
            readings,
            view,
            occurrences=occurrences,
            kinds=kinds or None,
            instruction_answers=answers,
            dates=dates,
            imported_by="parent",
            now=now,
            today=today,
        )
    if not isinstance(kept, Kept):
        raise _Changed
