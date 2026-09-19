"""Where observations come from, behind one protocol.

``StateSource`` exists so the rest of the system cannot tell a fixture from a
school platform, and that is a design stance, not a testing convenience. School
platforms are built for administrators, automated access is often unavailable,
and anything reading their interface breaks when the vendor changes it, so
manual entry and fixtures are first class sources rather than a fallback.

``FixtureSource`` is the only working implementation, and it seeds the
household's record rather than serving it: ``read_whole`` reads a source for
the record's first start, and from then on assignments and the claims about
their dates are read from the household's file. ``LMSSource`` and
``EmailSource`` raise ``NotImplementedError`` and mark where credentialed
access would attach if approved. For email, filtering after reading still
reads the whole mailbox, a parent's mailbox, so selection must happen before
access (an approved sender list or a dedicated folder), not after it.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from blossom.reconciliation import SourceRecord
from blossom.stores.project_state import Assignment, Seed, StudentReport
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

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> Mapping[str, Sequence[SourceRecord]]:
        """Return the claims about each assignment named, or about all with none named.

        One answer for the whole page, in the order the single read gives
        for each, so a reader never asks once per assignment. An assignment
        nothing was claimed about may have no entry; that is the empty list.
        """
        ...


class StateSource(DateClaims, Protocol):
    """Anything that can report assignments, the claims about their dates, and the
    two small corpora the planner reads whole."""

    def assignments(self) -> list[Assignment]:
        """Return every assignment this source knows about."""
        ...

    def claims_by_assignment(self) -> Mapping[str, Sequence[SourceRecord]]:
        """Return every claim the source holds, checked, grouped by the assignment it
        is about, none dropped: a claim about an assignment the source does not
        list is here for the reader to refuse by name."""
        ...

    def support_rules(self) -> list[SupportRule]:
        """Return the standing rules about how she works. Empty is a valid answer."""
        ...

    def reflections(self) -> list[Reflection]:
        """Return the planner's notes about its own past plans. Empty is a valid answer."""
        ...

    def student_reports(self) -> list[StudentReport]:
        """Return the reports she is taken to have made already. Empty is the usual answer;
        only the sample supplies any."""
        ...


def read_whole(source: StateSource) -> Seed:
    """A source's assignments, and every claim about their dates, read and checked whole.

    What a blank file is seeded with, read before anything is written, so a
    set that cannot be read leaves the file as it was. Every claim in the set
    is checked, and a claim about an assignment the set does not list is
    refused by name rather than dropped: in a set written by hand that is a
    mistyped id, and a claim never shown is a claim lost.
    """
    assignments = source.assignments()
    claims = source.claims_by_assignment()
    unknown = sorted(set(claims) - {assignment.assignment_id for assignment in assignments})
    if unknown:
        msg = f"the set claims dates for assignments it does not list: {', '.join(unknown)}"
        raise ValueError(msg)
    known = {assignment.assignment_id for assignment in assignments}
    reports = source.student_reports()
    unplaced = sorted({report.assignment_id for report in reports} - known)
    if unplaced:
        msg = f"the set reports on assignments it does not list: {', '.join(unplaced)}"
        raise ValueError(msg)
    return Seed(
        assignments,
        {
            assignment.assignment_id: list(claims.get(assignment.assignment_id, []))
            for assignment in assignments
        },
        reports,
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
        """The claims about one assignment's date, from the whole file read and checked."""
        return list(self.claims_by_assignment().get(assignment_id, []))

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[SourceRecord]]:
        """The claims about each assignment named, from the whole file read and checked once.

        Asked about no assignments, the answer is nothing and the file is not
        read, as the record runs no statement for the same question.
        """
        wanted = None if assignment_ids is None else set(assignment_ids)
        if wanted is not None and not wanted:
            return {}
        return {
            assignment_id: list(claims)
            for assignment_id, claims in self.claims_by_assignment().items()
            if claims and (wanted is None or assignment_id in wanted)
        }

    def claims_by_assignment(self) -> dict[str, list[SourceRecord]]:
        """Every claim in ``deadline_sources.json``, checked, grouped by the assignment it
        is about, and none dropped, the join key taken off each."""
        data = json.loads((self._root / "deadline_sources.json").read_text(encoding="utf-8"))
        grouped: dict[str, list[SourceRecord]] = {}
        for item in data:
            record = SourceRecord.model_validate(
                {key: value for key, value in item.items() if key != "assignment_id"}
            )
            grouped.setdefault(str(item["assignment_id"]), []).append(record)
        return grouped

    def student_reports(self) -> list[StudentReport]:
        """Load ``student_reports.json``, the reports she is taken to have made: a file only
        the sample carries, read with the same checks as any of her reports."""
        return [
            StudentReport.model_validate(item) for item in self._optional("student_reports.json")
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

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[SourceRecord]]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def claims_by_assignment(self) -> dict[str, list[SourceRecord]]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def support_rules(self) -> list[SupportRule]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def reflections(self) -> list[Reflection]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def student_reports(self) -> list[StudentReport]:
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

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[SourceRecord]]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def claims_by_assignment(self) -> dict[str, list[SourceRecord]]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def support_rules(self) -> list[SupportRule]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def reflections(self) -> list[Reflection]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def student_reports(self) -> list[StudentReport]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError
