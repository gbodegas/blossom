"""Where observations come from, behind one protocol.

``StateSource`` exists so the rest of the system cannot tell a fixture from a
school platform. That is not only a testing convenience: school platforms are
built for administrators, automated access is often unavailable, and anything
that reads their interface breaks when the vendor changes it. Manual entry and
fixtures are first-class sources by design rather than a fallback bolted on
after automated access fails.

``FixtureSource`` is the only working implementation. ``LMSSource`` and
``EmailSource`` raise ``NotImplementedError`` and are here to mark where
credentialed access would attach if it is ever approved.

``EmailSource`` carries an unresolved constraint worth stating before anyone
implements it. Filtering messages after reading them still requires reading
them all, which is a materially different privacy position from the one
intended, especially for a parent's mailbox. Selection has to happen before
access -- an approved sender list, or a dedicated folder -- not afterwards.
"""

import json
from pathlib import Path
from typing import Protocol

from blossom.reconciliation import SourceRecord
from blossom.stores.project_state import Assignment


class StateSource(Protocol):
    """Anything that can report assignments and the claims made about their dates."""

    def assignments(self) -> list[Assignment]:
        """Return every assignment this source knows about."""
        ...

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        """Return every channel's claim about one assignment's due date.

        An empty list is a valid answer and means nothing corroborates the
        date. Callers must handle it; it is not an error.
        """
        ...


class FixtureSource:
    """Reads synthetic fixtures from disk. The default source, and fully offline."""

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


class LMSSource:
    """Real LMS polling belongs here when credentialed connectors are allowed."""

    def assignments(self) -> list[Assignment]:
        """Not implemented. See the class docstring."""
        raise NotImplementedError

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
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
