# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The part of the record's store that keeps which earlier work she chose for a day's plan.

Mixed into the store of the record, which supplies the connection, the lock, and
the transaction that reserves the writer before it reads. A choice is one row,
the day it is for and the assignment, and nothing else: it says the work may be
planned that day as catch-up work. It never touches the assignment, its dates,
her reports, the school's record, or anyone's note, and a new day starts with no
choices without anything being written or swept.
"""

import sqlite3
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from typing import Final

CREATE_CATCH_UP_CHOICES: Final = """
CREATE TABLE IF NOT EXISTS catch_up_choices (
    plan_date TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    PRIMARY KEY (plan_date, assignment_id)
)
"""
EVERY_CHOICE: Final = "SELECT plan_date, assignment_id FROM catch_up_choices"
CHOICE_KEPT: Final = "SELECT 1 FROM catch_up_choices WHERE plan_date = ? AND assignment_id = ?"
KEEP_CHOICE: Final = "INSERT INTO catch_up_choices (plan_date, assignment_id) VALUES (?, ?)"
DROP_CHOICE: Final = "DELETE FROM catch_up_choices WHERE plan_date = ? AND assignment_id = ?"
ON_RECORD: Final = "SELECT 1 FROM assignments WHERE assignment_id = ?"


class NotOnRecord(LookupError):
    """A choice named an assignment the record does not have; nothing was written."""


class ChoiceNotSaved(RuntimeError):
    """The file refused a choice; whatever was begun was rolled back with it."""


@dataclass(frozen=True)
class ChoiceMade:
    """The choice was written, or taken back, now."""


@dataclass(frozen=True)
class ChoiceStood:
    """The choice already stood as asked; nothing was written."""


ChoiceOutcome = ChoiceMade | ChoiceStood


class CatchUpRecords:
    """The part of the record's store that keeps her catch-up choices, by day."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def _create_catch_up_table(self) -> None:
        """The table, in the caller's transaction."""
        self._connection.execute(CREATE_CATCH_UP_CHOICES)

    def catch_up_choices(self) -> Mapping[date, frozenset[str]]:
        """Every choice kept, by the day it is for. Read in the caller's transaction, so a
        page reads them in the same snapshot as the work they name. A row whose day can't
        be read is left out: it can be no day's choice."""
        with self._lock:
            rows = self._connection.execute(EVERY_CHOICE).fetchall()
        chosen: dict[date, set[str]] = {}
        for day, assignment_id in rows:
            try:
                when = date.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            if when.isoformat() == day and isinstance(assignment_id, str):
                chosen.setdefault(when, set()).add(assignment_id)
        return {day: frozenset(names) for day, names in chosen.items()}

    def choose_catch_up(
        self, assignment_id: str, plan_date: date, *, include: bool
    ) -> ChoiceOutcome:
        """Keep, or take back, her choice of one assignment for ``plan_date``, once.

        The writer is reserved before anything is read. An assignment not on
        record is ``NotOnRecord``. A choice that already stands as asked writes
        nothing. A write the file refuses is rolled back and raised as
        ``ChoiceNotSaved``.
        """
        day = plan_date.isoformat()
        try:
            with self._lock, self._writing():
                if self._connection.execute(ON_RECORD, (assignment_id,)).fetchone() is None:
                    msg = f"no assignment {assignment_id!r} is on record"
                    raise NotOnRecord(msg)
                kept = self._connection.execute(CHOICE_KEPT, (day, assignment_id)).fetchone()
                if (kept is not None) == include:
                    return ChoiceStood()
                self._connection.execute(
                    KEEP_CHOICE if include else DROP_CHOICE, (day, assignment_id)
                )
                return ChoiceMade()
        except sqlite3.Error as error:
            msg = f"the choice on {assignment_id!r} could not be saved: {type(error).__name__}"
            raise ChoiceNotSaved(msg) from error
