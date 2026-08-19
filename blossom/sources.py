import json
from pathlib import Path
from typing import Protocol

from blossom.reconciliation import SourceRecord
from blossom.stores.project_state import Assignment


class StateSource(Protocol):
    def assignments(self) -> list[Assignment]:
        ...

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        ...


class FixtureSource:
    def __init__(self, root: Path) -> None:
        self._root = root

    def assignments(self) -> list[Assignment]:
        data = json.loads((self._root / "assignments.json").read_text())
        return [Assignment.model_validate(item) for item in data]

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
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
        raise NotImplementedError

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        raise NotImplementedError


class EmailSource:
    """Inbound email import belongs here if a local, non-transmitting source is approved."""

    def assignments(self) -> list[Assignment]:
        raise NotImplementedError

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        raise NotImplementedError
