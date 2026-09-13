"""Where observations come from, behind one protocol.

``StateSource`` exists so the rest of the system cannot tell a fixture from a
school platform, and that is a design stance, not a testing convenience. School
platforms are built for administrators, automated access is often unavailable,
and anything reading their interface breaks when the vendor changes it, so
manual entry and fixtures are first class sources rather than a fallback.

``FixtureSource`` is the only working implementation, and it seeds the
household's record rather than serving it: ``seed`` copies a source into the
store once, and from then on assignments and the claims about their dates are
read from the household's file. ``LMSSource`` and
``EmailSource`` raise ``NotImplementedError`` and mark where credentialed
access would attach if approved. For email, filtering after reading still
reads the whole mailbox, a parent's mailbox, so selection must happen before
access (an approved sender list or a dedicated folder), not after it.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from blossom.reconciliation import SourceRecord
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.reflections import Reflection, ReflectionSubject
from blossom.stores.support_rules import SupportRule


class DateClaims(Protocol):
    """Anything that can report every channel's claim about one assignment's due date.

    The household's record answers this for the page and the plan graph; a
    fixture answers it while seeding; a test double answers it to say what a
    school would."""

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Return every channel's claim about one assignment's due date.

        An empty list is a valid answer and means nothing corroborates the
        date. Callers must handle it; it is not an error.
        """
        ...


class StateSource(DateClaims, Protocol):
    """Anything that can report assignments, the claims about their dates, and the
    two small corpora the planner reads whole."""

    def assignments(self) -> list[Assignment]:
        """Return every assignment this source knows about."""
        ...

    def support_rules(self) -> list[SupportRule]:
        """Return the standing rules about how she works. Empty is a valid answer."""
        ...

    def reflections(self) -> list[Reflection]:
        """Return the planner's notes about its own past plans. Empty is a valid answer."""
        ...


def seed(store: ProjectStateStore, source: StateSource) -> None:
    """Put a source's assignments, and the claims about their dates, on record.

    Meant for an empty record: the household's file is seeded once from a
    fixture, when one is named, and what the family enters afterward is not
    written over by the fixture at the next start.
    """
    assignments = source.assignments()
    store.upsert_assignments(assignments)
    for assignment in assignments:
        store.record_claims(
            assignment.assignment_id, source.deadline_records(assignment.assignment_id)
        )


class FixtureSource:
    """Reads a synthetic set from disk: the seed for the sample and the tests, offline."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def assignments(self) -> list[Assignment]:
        """Load every assignment from ``assignments.json``."""
        data = json.loads((self._root / "assignments.json").read_text())
        return [Assignment.model_validate(item) for item in data]

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Load the claims about one assignment's date, dropping the join key."""
        data = json.loads((self._root / "deadline_sources.json").read_text())
        return [
            SourceRecord.model_validate(
                {key: value for key, value in item.items() if key != "assignment_id"}
            )
            for item in data
            if item["assignment_id"] == assignment_id
        ]

    def support_rules(self) -> list[SupportRule]:
        """Load ``support_rules.json``. A fixture set without one has no rules."""
        return [
            SupportRule(
                rule_id=str(item["rule_id"]),
                instruction=str(item["instruction"]),
                asserted_at=datetime.fromisoformat(str(item["asserted_at"])),
            )
            for item in self._optional("support_rules.json")
        ]

    def reflections(self) -> list[Reflection]:
        """Load ``reflections.json``. A fixture set without one has no reflections."""
        return [
            Reflection(
                reflection_id=str(item["reflection_id"]),
                subject=ReflectionSubject(str(item["subject"])),
                observation=str(item["observation"]),
                observed_at=datetime.fromisoformat(str(item["observed_at"])),
            )
            for item in self._optional("reflections.json")
        ]

    def _optional(self, name: str) -> list[dict[str, object]]:
        """A corpus file that a fixture set may leave out.

        Assignments and their sources are what a fixture set is; the two
        corpora are context for the planner, and a set written to exercise the
        page alone need not carry them.
        """
        path = self._root / name
        if not path.exists():
            return []
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return [dict(item) for item in loaded]


class LMSSource:
    """Real LMS polling belongs here when credentialed connectors are allowed.

    The rules the adapter will have to follow, from how a school portal lays a
    week out. One item appears under an assigned date in one week and under a
    due date in another: it is one assignment, matched by course and title,
    with ``assigned_on`` and ``due_date`` as its two dates. A due date may be
    shown in a day's header, inline in the title, or nowhere; the first two are
    separate claims from the same channel, recorded with ``seen_in`` so a
    reader can tell them apart, and the third is an undated assignment.
    Titles carry punctuation and course codes that belong to the portal, not
    to the item. Forms to sign and books to cover are listed beside essays and
    are ``TASK``, not ``HOMEWORK``.
    """

    def assignments(self) -> list[Assignment]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def support_rules(self) -> list[SupportRule]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def reflections(self) -> list[Reflection]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError


class EmailSource:
    """Inbound email import belongs here if a local, non-transmitting source is approved."""

    def assignments(self) -> list[Assignment]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def support_rules(self) -> list[SupportRule]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def reflections(self) -> list[Reflection]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError
