# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The homework screenshot save's reads and writes of a review's draft tables, on the record
store's own connection.

A save of homework from a screenshot review is one transaction: the review's version, answers,
readings and earlier acceptances are read, the homework is written, and the acceptance and the
answer deletes land, all inside the caller's ``comparing_and_writing`` block. The draft store
and the record store share the household file, so reading and writing the draft's tables here,
on the connection that holds the writer, puts the whole save in that one transaction.
"""

import json
import sqlite3
import threading
from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from blossom.stores.gradebook import ADD_ACCEPTANCE

if TYPE_CHECKING:
    from blossom.stores.import_drafts import StoredReading

HOMEWORK: Final = "homework_screenshot"


class DraftForSave(NamedTuple):
    """A review's draft tables as the save reads them, on the writer's connection."""

    draft_id: str
    student_id: str
    version: int
    expires_at: datetime
    closed_at: datetime | None
    readings: tuple["StoredReading", ...]
    unread: int
    """Images present with no reading: a Read not done, under way or failed."""
    discards: frozenset[str]
    answers: Mapping[tuple[str, str], tuple[str, str]]
    """(kind, subject) to (value, digest)."""
    saved: Mapping[str, str]
    """Each piece name an acceptance of this draft names, with its assignment id."""


class HomeworkAcceptance(NamedTuple):
    """The acceptance a homework save writes: its id, the pieces' pairs, the identity the page
    established, and the counts it records."""

    acceptance_id: str
    student_id: str
    draft_id: str
    pairs: tuple[tuple[str, str], ...]
    identity_status: str
    identity_answer: str
    added: int
    updated: int
    already_saved: int
    left_to_check: int
    shown: int
    answers_kept: int
    complete: bool
    corrected: int
    role: str


class HomeworkSaveRecords:
    """The part of the record's store that reads a screenshot review's draft tables and writes
    its homework acceptance, inside the caller's transaction."""

    _connection: sqlite3.Connection
    _lock: "threading.RLock"

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def homework_acceptance(self, acceptance_id: str) -> tuple[tuple[str, str], ...] | None:
        """The pairs a homework acceptance with this id recorded, or None when there is none."""
        with self._lock:
            row = self._connection.execute(
                "SELECT accepted FROM grade_acceptances WHERE acceptance_id = ? AND kind = ?",
                (acceptance_id, HOMEWORK),
            ).fetchone()
        if row is None:
            return None
        return tuple((str(name), str(target)) for name, target in json.loads(str(row[0])))

    def homework_counts(self, acceptance_id: str) -> tuple[int, int, int, int] | None:
        """What a homework acceptance with this id counted: assignments added, updated and
        already saved, and pieces left to check; None when there is none."""
        with self._lock:
            row = self._connection.execute(
                "SELECT added, updated, already_saved, left_to_check FROM grade_acceptances "
                "WHERE acceptance_id = ? AND kind = ?",
                (acceptance_id, HOMEWORK),
            ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    def draft_for_save(self, student_id: str, draft_id: str) -> DraftForSave | None:
        """Her homework draft's tables, or None when she has no such draft."""
        # The draft store's statements, read here on the writer's connection; imported here
        # because the draft store's module reaches the record store's through the agent.
        from blossom.stores.import_drafts import (
            ACCEPTED_KEYS,
            ANSWERS_OF,
            DRAFT_OF,
            IMAGES_OF,
            StoredReading,
            moment,
        )

        with self._lock:
            connection = self._connection
            cursor = connection.execute(DRAFT_OF, (draft_id, student_id))
            draft = cursor.fetchone()
            if draft is None:
                return None
            fields = dict(zip([str(item[0]) for item in cursor.description], draft, strict=True))
            if fields["kind"] != HOMEWORK:
                return None
            cursor = connection.execute(IMAGES_OF, (draft_id,))
            columns = [str(item[0]) for item in cursor.description]
            rows = [dict(zip(columns, image, strict=True)) for image in cursor.fetchall()]
            readings = tuple(
                sorted(
                    (
                        StoredReading(
                            sha256=str(row["sha256"]),
                            kind=str(row["reading_kind"]),
                            prompt_version=str(row["prompt_version"]),
                            schema_version=str(row["schema_version"]),
                            body=str(row["reading"]),
                        )
                        for row in rows
                        if row["reading"] is not None
                    ),
                    key=lambda item: item.sha256,
                )
            )
            discards = frozenset(
                str(key)
                for (key,) in connection.execute(
                    "SELECT row_key FROM import_draft_discards WHERE draft_id = ?", (draft_id,)
                )
            )
            answers = {
                (str(kind), str(subject)): (str(value), str(digest))
                for kind, subject, value, digest in connection.execute(ANSWERS_OF, (draft_id,))
            }
            saved: dict[str, str] = {}
            for (accepted,) in connection.execute(ACCEPTED_KEYS, (student_id, HOMEWORK, draft_id)):
                for name, target in json.loads(str(accepted)):
                    saved[str(name)] = str(target)
        closed = fields["closed_at"]
        return DraftForSave(
            draft_id,
            student_id,
            int(fields["version"]),
            moment(fields["expires_at"]),
            None if closed is None else moment(closed),
            readings,
            sum(1 for row in rows if row["reading"] is None),
            discards,
            answers,
            saved,
        )

    def add_homework_acceptance(
        self, acceptance: HomeworkAcceptance, accepted_at: datetime
    ) -> None:
        """Write the acceptance with its ``[piece name, id]`` pairs, in the caller's transaction."""
        with self._lock, self._writing():
            self._connection.execute(
                ADD_ACCEPTANCE,
                (
                    acceptance.acceptance_id,
                    acceptance.student_id,
                    HOMEWORK,
                    acceptance.draft_id,
                    None,
                    None,
                    None,
                    json.dumps([list(pair) for pair in acceptance.pairs]),
                    acceptance.identity_status,
                    acceptance.identity_answer,
                    None,
                    acceptance.added,
                    acceptance.updated,
                    acceptance.already_saved,
                    acceptance.left_to_check,
                    acceptance.shown,
                    acceptance.answers_kept,
                    int(acceptance.complete),
                    accepted_at.isoformat(),
                    acceptance.role,
                ),
            )
            self._connection.execute(
                "UPDATE grade_acceptances SET corrected = ? WHERE acceptance_id = ?",
                (acceptance.corrected, acceptance.acceptance_id),
            )

    def drop_saved_answers(self, draft_id: str, answers: Collection[tuple[str, str]]) -> int:
        """Delete the saved pieces' answers, in the caller's transaction; how many went."""
        with self._lock, self._writing():
            gone = 0
            for kind, subject in sorted(answers):
                gone += self._connection.execute(
                    "DELETE FROM import_draft_answers "
                    "WHERE draft_id = ? AND kind = ? AND subject = ?",
                    (draft_id, kind, subject),
                ).rowcount
        return gone
