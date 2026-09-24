"""The part of the record's store that keeps the school's instructions.

Mixed into the store of the record, which supplies the connection, the lock,
the clock, and the transaction that reserves the writer before it reads. The
rule of ``blossom.school_instructions`` is enforced here, inside that
transaction, for every writer: the paste review, a family's correction, a seed,
and any caller that writes an assignment with a school note.

The table replaces the old note field for the school's words. Every school
note that field held is moved here once, at startup, in one transaction with
the table itself: the words and the school channel its mark named are kept, no
moment or person is made up for it, the move is checked, and only then is the
old field cleared. Her notes and a parent's stay where they are.
"""

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, cast

from blossom.captures import Author
from blossom.clock import Clock
from blossom.reconciliation import SourceChannel
from blossom.school_instructions import (
    CARDS,
    STATES,
    Card,
    InstructionChoice,
    InstructionSeen,
    InstructionsNeedAChoice,
    InstructionsOutcome,
    InstructionsSettled,
    InstructionsStanding,
    InstructionState,
    SchoolInstruction,
    carried_state,
    revision_of,
    settle,
    standing_of,
)

logger = logging.getLogger(__name__)

CREATE_SCHOOL_INSTRUCTIONS: Final = """
    CREATE TABLE IF NOT EXISTS school_instructions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id TEXT NOT NULL,
        text TEXT NOT NULL,
        channel TEXT,
        card TEXT,
        card_day TEXT,
        first_seen_at_utc TEXT,
        first_seen_on TEXT,
        imported_by TEXT,
        state TEXT NOT NULL,
        settled_by TEXT,
        settled_at_utc TEXT,
        settled_on TEXT,
        revision INTEGER NOT NULL,
        UNIQUE (assignment_id, text)
    )
"""
EVERY_INSTRUCTION: Final = """
    SELECT sequence, assignment_id, text, channel, card, card_day, first_seen_at_utc,
        first_seen_on, imported_by, state, settled_by, settled_at_utc, settled_on, revision
    FROM school_instructions
    ORDER BY sequence
"""
INSTRUCTIONS_OF: Final = """
    SELECT sequence, assignment_id, text, channel, card, card_day, first_seen_at_utc,
        first_seen_on, imported_by, state, settled_by, settled_at_utc, settled_on, revision
    FROM school_instructions
    WHERE assignment_id = ? ORDER BY sequence
"""
INSERT_INSTRUCTION: Final = """
    INSERT INTO school_instructions (
        assignment_id, text, channel, card, card_day, first_seen_at_utc, first_seen_on,
        imported_by, state, settled_by, settled_at_utc, settled_on, revision
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
AUTHORED_MARKS: Final = frozenset(
    {SourceChannel.PARENT_ENTRY.value, SourceChannel.STUDENT_REPORT.value}
)
"""The marks that say a note is someone's own: a parent's entry, or her report. Any other
mark, and no mark at all, says the note is the school's, as the ``note_by`` rule reads it."""
SCHOOL_MARKS: Final = frozenset({SourceChannel.LMS.value, SourceChannel.EMAIL.value})
AUTHORS: Final = frozenset({"student", "parent", "household"})


class UnreadableInstruction(ValueError):
    """Raised for a kept instruction held as nothing this store writes. The assignment's
    instructions are then unavailable, which a page says; nothing is guessed in their
    place."""


class SchoolInstructionsNeedAChoice(ValueError):
    """Raised when a write of an assignment with a school note brings a new instruction that
    no rule may place. Nothing of that write is kept; the outcome says what needs a choice."""

    def __init__(self, assignment_id: str, outcome: InstructionsNeedAChoice) -> None:
        super().__init__(
            f"the school's instructions for {assignment_id!r} need a choice before a new one "
            "is kept"
        )
        self.assignment_id = assignment_id
        self.outcome = outcome


@dataclass(frozen=True)
class InstructionsForNoAssignment:
    """A write about an assignment that is not on record: nothing is kept for it."""

    assignment_id: str


@dataclass(frozen=True)
class InstructionReadings:
    """The kept instructions of the assignments asked about, from one statement: each one's
    standing, and the assignments with a row that cannot be read."""

    readable: dict[str, InstructionsStanding]
    unreadable: frozenset[str]


def instruction_from(row: Sequence[object]) -> SchoolInstruction:
    """One kept instruction from a row in the order ``EVERY_INSTRUCTION`` reads it, or
    ``UnreadableInstruction`` for a row this store never writes."""
    try:
        state = str(row[9])
        card = None if row[4] is None else str(row[4])
        if state not in STATES or (card is not None and card not in CARDS):
            msg = f"instruction {row[0]!r} holds {state!r} and {card!r}"
            raise UnreadableInstruction(msg)
        for who in (row[8], row[10]):
            if who is not None and str(who) not in AUTHORS:
                msg = f"instruction {row[0]!r} names {who!r}"
                raise UnreadableInstruction(msg)
        return SchoolInstruction(
            sequence=int(cast(int, row[0])),
            assignment_id=str(row[1]),
            text=str(row[2]),
            channel=None if row[3] is None else SourceChannel(str(row[3])),
            card=cast(Card | None, card),
            card_day=None if row[5] is None else date.fromisoformat(str(row[5])),
            first_seen_at=None if row[6] is None else datetime.fromisoformat(str(row[6])),
            first_seen_on=None if row[7] is None else date.fromisoformat(str(row[7])),
            imported_by=cast(Author | None, None if row[8] is None else str(row[8])),
            state=cast(InstructionState, state),
            settled_by=cast(Author | None, None if row[10] is None else str(row[10])),
            settled_at=None if row[11] is None else datetime.fromisoformat(str(row[11])),
            settled_on=None if row[12] is None else date.fromisoformat(str(row[12])),
            revision=int(cast(int, row[13])),
        )
    except UnreadableInstruction:
        raise
    except (ValueError, TypeError) as error:
        msg = f"instruction {row[0]!r} cannot be read: {error}"
        raise UnreadableInstruction(msg) from error


class SchoolInstructionRecords:
    """The part of the record's store that keeps the school's instructions."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"
    _clock: Clock

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    # ------------------------------------------------------------------ the table and the move

    def _create_instruction_table(self) -> None:
        """The table, in the caller's transaction. One row per text for each assignment, so
        the same words read again are the same row."""
        self._connection.execute(CREATE_SCHOOL_INSTRUCTIONS)

    def _carry_school_notes(self) -> int:
        """Move every school note out of the old note field, in the caller's transaction, and
        say how many were moved.

        A note is the school's when its mark is not a parent's entry or her
        report, no mark included, as the ``note_by`` rule has always read it.
        Each is placed by the startup rule: nothing new when its text is kept,
        applying when nothing is kept, and awaiting a parent's review beside
        what is kept otherwise. Its words and the school channel its mark names
        are kept, with no moment or person made up. The move is checked, every
        note kept exactly once, before the old field and its mark are cleared;
        any failure undoes the whole of it with the caller's transaction. A row
        whose origins cannot be read is left as it was, and so is a note beside
        a kept instruction that cannot be read, so a start is never refused
        over it.
        """
        rows = self._connection.execute(
            "SELECT assignment_id, note, origins FROM assignments WHERE note IS NOT NULL"
        ).fetchall()
        moving: list[tuple[str, str, dict[str, object]]] = []
        unmoved = 0
        for assignment_id, note, raw in rows:
            try:
                origins = {} if raw is None else json.loads(str(raw))
            except ValueError:
                continue
            if not isinstance(origins, dict):
                continue
            mark = origins.get("note")
            if mark in AUTHORED_MARKS:
                continue
            if mark is not None and mark not in SCHOOL_MARKS:
                continue
            name, text = str(assignment_id), str(note)
            try:
                kept = self._kept_instructions_locked(name)
            except UnreadableInstruction:
                # Never a start refused over a row it cannot read: the note stays where it
                # is, shown as it always was, and moves at a start that can read the rows.
                unmoved += 1
                continue
            state = carried_state(kept, text)
            if state != "nothing":
                self._insert_instruction_locked(
                    name,
                    InstructionSeen(text, None if mark is None else SourceChannel(str(mark))),
                    state,
                    revision_of(kept) + 1,
                    imported_by=None,
                    settled_by=None,
                    now=None,
                    today=None,
                )
            moving.append(
                (name, text, {key: value for key, value in origins.items() if key != "note"})
            )
        self._verify_carried(moving)
        self._clear_moved_notes(moving)
        if moving:
            logger.info("moved %d school notes out of the old note field", len(moving))
        if unmoved:
            logger.warning(
                "left %d school notes in the old note field: a kept instruction beside each "
                "cannot be read",
                unmoved,
            )
        return len(moving)

    def _verify_carried(self, moving: Sequence[tuple[str, str, dict[str, object]]]) -> None:
        """Every note about to be cleared is kept exactly once for its assignment."""
        for assignment_id, text, _ in moving:
            found = self._connection.execute(
                "SELECT COUNT(*) FROM school_instructions WHERE assignment_id = ? AND text = ?",
                (assignment_id, text),
            ).fetchone()[0]
            if int(found) != 1:
                msg = f"a school note of {assignment_id!r} is not kept once, so none is cleared"
                raise RuntimeError(msg)

    def _clear_moved_notes(self, moving: Sequence[tuple[str, str, dict[str, object]]]) -> None:
        """Clear the old field and its mark for each moved note, and nothing else."""
        for assignment_id, text, rest in moving:
            self._connection.execute(
                "UPDATE assignments SET note = NULL, origins = ? "
                "WHERE assignment_id = ? AND note = ?",
                (json.dumps(rest) if rest else None, assignment_id, text),
            )

    # ------------------------------------------------------------------ reading

    def _kept_instructions_locked(self, assignment_id: str) -> list[SchoolInstruction]:
        rows = self._connection.execute(INSTRUCTIONS_OF, (assignment_id,)).fetchall()
        return [instruction_from(row) for row in rows]

    def school_instruction_readings(self, assignment_ids: Iterable[str]) -> InstructionReadings:
        """The kept instructions of these assignments, in one statement however many are asked
        about. An assignment with a row that cannot be read is named as unavailable, and one
        with nothing kept is not among the readable."""
        wanted = set(assignment_ids)
        if not wanted:
            return InstructionReadings({}, frozenset())
        with self._lock:
            rows = self._connection.execute(EVERY_INSTRUCTION).fetchall()
        kept: dict[str, list[SchoolInstruction]] = {}
        unreadable: set[str] = set()
        for row in rows:
            name = str(row[1])
            if name not in wanted:
                continue
            try:
                kept.setdefault(name, []).append(instruction_from(row))
            except UnreadableInstruction:
                unreadable.add(name)
        return InstructionReadings(
            {name: standing_of(items) for name, items in kept.items() if name not in unreadable},
            frozenset(unreadable),
        )

    # ------------------------------------------------------------------ writing

    def instruction_moment(self) -> tuple[datetime, date]:
        """The store's own moment and household day, for a caller that brings none."""
        return self._clock.now(), self._clock.today()

    def settle_school_instructions(
        self,
        assignment_id: str,
        seen: Sequence[InstructionSeen],
        choice: InstructionChoice | None,
        *,
        authored_by: Author | None,
        now: datetime,
        today: date,
    ) -> InstructionsOutcome | InstructionsForNoAssignment:
        """Keep these readings for one assignment, with this choice or none, in one
        transaction that reserves the writer before it reads."""
        with self._lock, self._writing():
            return self._settle_instructions_locked(
                assignment_id, seen, choice, authored_by=authored_by, now=now, today=today
            )

    def _settle_instructions_locked(
        self,
        assignment_id: str,
        seen: Sequence[InstructionSeen],
        choice: InstructionChoice | None,
        *,
        authored_by: Author | None,
        now: datetime,
        today: date,
    ) -> InstructionsOutcome | InstructionsForNoAssignment:
        """The rule, applied and written inside the caller's transaction: what stands is read
        here, the outcome is decided against it, and only a settled outcome writes. A new
        row keeps who pasted it and when; a state chosen keeps who chose it and when."""
        on_record = self._connection.execute(
            "SELECT 1 FROM assignments WHERE assignment_id = ?", (assignment_id,)
        ).fetchone()
        if on_record is None:
            return InstructionsForNoAssignment(assignment_id)
        kept = self._kept_instructions_locked(assignment_id)
        outcome = settle(kept, seen, choice)
        if isinstance(outcome, InstructionsSettled):
            chooser = authored_by if choice is not None else None
            for item, state in outcome.inserted:
                self._insert_instruction_locked(
                    assignment_id,
                    item,
                    state,
                    outcome.revision,
                    imported_by=authored_by,
                    settled_by=chooser,
                    now=now,
                    today=today,
                )
            for row, state in outcome.changed:
                self._connection.execute(
                    """
                    UPDATE school_instructions
                    SET state = ?, settled_by = ?, settled_at_utc = ?, settled_on = ?, revision = ?
                    WHERE sequence = ?
                    """,
                    (
                        state,
                        chooser,
                        now.isoformat(),
                        today.isoformat(),
                        outcome.revision,
                        row.sequence,
                    ),
                )
        return outcome

    def _insert_instruction_locked(
        self,
        assignment_id: str,
        item: InstructionSeen,
        state: InstructionState,
        revision: int,
        *,
        imported_by: Author | None,
        settled_by: Author | None,
        now: datetime | None,
        today: date | None,
    ) -> None:
        self._connection.execute(
            INSERT_INSTRUCTION,
            (
                assignment_id,
                item.text,
                None if item.channel is None else item.channel.value,
                item.card,
                None if item.card_day is None else item.card_day.isoformat(),
                None if now is None else now.isoformat(),
                None if today is None else today.isoformat(),
                imported_by,
                state,
                settled_by,
                None if settled_by is None or now is None else now.isoformat(),
                None if settled_by is None or today is None else today.isoformat(),
                revision,
            ),
        )

    def _keep_note_as_instruction_locked(
        self,
        assignment_id: str,
        note: str,
        mark: object,
        *,
        initial: bool,
    ) -> None:
        """A school note written with an assignment, kept as the school's instruction: by the
        startup rule when a blank file is seeded, and by the ordinary rule otherwise, which
        refuses the whole write when a new text needs a choice."""
        channel = None if mark is None else SourceChannel(str(mark))
        if initial:
            kept = self._kept_instructions_locked(assignment_id)
            state = carried_state(kept, note)
            if state != "nothing":
                self._insert_instruction_locked(
                    assignment_id,
                    InstructionSeen(note, channel),
                    state,
                    revision_of(kept) + 1,
                    imported_by=None,
                    settled_by=None,
                    now=None,
                    today=None,
                )
            return
        outcome = self._settle_instructions_locked(
            assignment_id,
            [InstructionSeen(note, channel)],
            None,
            authored_by=None,
            now=self._clock.now(),
            today=self._clock.today(),
        )
        if isinstance(outcome, InstructionsNeedAChoice):
            raise SchoolInstructionsNeedAChoice(assignment_id, outcome)


def school_note(note: str | None, origins: Mapping[str, object]) -> bool:
    """Whether an assignment's note is the school's, by the ``note_by`` rule: a note whose
    mark is neither a parent's entry nor her report, no mark included."""
    return note is not None and origins.get("note") not in AUTHORED_MARKS
