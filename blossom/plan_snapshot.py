"""The saved plan as data: one versioned snapshot kept beside the text she reads.

A plan is composed once, from what a run read, and saved as the text both
readers see. The text alone cannot say which assignment a line is about: two
assignments can share a title, and one assignment can hold several blocks. So
the same composition also saves what it was made from, as one JSON document:
the plan itself, the title, course, and due date of each assignment it speaks
about as the run read them, and the sentences the composer wrote around the
plan. A page reads that back to show the plan by assignment, and never reads
the text to guess at one.

The document carries a version, and the reader here decides, one record at a
time, what a page can show: a supported snapshot that agrees with its draft;
no snapshot, for a draft from before plans carried one; or a snapshot that
cannot be used, for which the saved text is shown whole. Nothing here repairs,
guesses, or writes, and the frozen values are what the plan said when it was
made, never the assignment's record as it stands.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError, model_validator

from blossom.plans import DailyPlan

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION: Final = 1
DIAGNOSTIC_LIMIT: Final = 300
"""How much is logged about a snapshot that cannot be used: where it failed and how, and
never what it says, which is text about her."""


class SavedAssignment(BaseModel):
    """One assignment as the plan named it: what the run read, kept as it was."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    course: str
    due_date: date | None = None


class SavedClarification(BaseModel):
    """One date the plan asked someone to clarify, and why, in the words composed then."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    text: str


class SavedFinding(BaseModel):
    """One of the reviewer's notes as the plan showed it: the criterion with its verdict,
    and the critique."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    text: str


class SavedReview(BaseModel):
    """The reviewer's notes that travel with the plan: what it did not consider, then each
    finding, in the order composed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intro: list[str] = []
    findings: list[SavedFinding] = []


class PlanSnapshot(BaseModel):
    """Version 1 of what is saved beside a plan's text, enough to show the plan without the
    text and without reading any assignment again.

    The plan keeps its own order and every field. The metadata is keyed by
    assignment id and covers exactly the assignments the plan speaks about,
    worked on or put off, each once; a clarification names one of them. A
    snapshot that says otherwise is not one this version wrote, and is
    refused where it is made and where it is read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    plan: DailyPlan
    assignments: dict[str, SavedAssignment]
    intro: list[str] = []
    """The sentences under the heading: a shorter evening, a review that did not settle."""
    clarifications: list[SavedClarification] = []
    review: SavedReview | None = None
    """``None`` when no reviewer's notes were composed."""

    @model_validator(mode="after")
    def _agrees_with_itself(self) -> Self:
        if self.version != SNAPSHOT_VERSION:
            msg = f"snapshot version {self.version} is not one this reader knows"
            raise ValueError(msg)
        spoken_about = set(self.plan.assignment_ids)
        if set(self.assignments) != spoken_about:
            msg = "the saved assignments are not the ones the plan speaks about"
            raise ValueError(msg)
        astray = [item for item in self.clarifications if item.assignment_id not in spoken_about]
        if astray:
            msg = "a clarification names an assignment the plan does not speak about"
            raise ValueError(msg)
        return self

    @property
    def assignment_ids(self) -> list[str]:
        """Every assignment the plan speaks about, each once, sorted: the draft's own list."""
        return sorted(set(self.plan.assignment_ids))


@dataclass(frozen=True)
class SnapshotReading:
    """What one draft's snapshot lets a page show."""

    snapshot: PlanSnapshot | None
    """The plan as data, when it can be used; ``None`` otherwise."""
    unavailable: bool = False
    """Whether a snapshot is saved and cannot be used. False for a draft that never had
    one, which is an earlier plan and no failure."""


def read_snapshot(
    draft_id: str,
    saved: str | None,
    *,
    plan_date: date,
    plan_assignment_ids: Sequence[str] | None,
) -> SnapshotReading:
    """Decide what one draft's saved snapshot lets a page show, and never raise.

    No snapshot is an earlier plan. A snapshot is used only when it is
    version 1, whole, and about the same evening and the same assignments as
    the draft it sits beside. Anything else, text that is not JSON, another
    version, a missing or stray field, metadata that does not match, another
    evening's date, another list of assignments, is unavailable: the page
    shows the saved text whole, and the draft id is logged with where the
    snapshot failed, not with what it says.
    """
    if saved is None:
        return SnapshotReading(None)
    try:
        snapshot = PlanSnapshot.model_validate(json.loads(saved))
    except ValidationError as error:
        places = ", ".join(
            f"{'.'.join(str(part) for part in found['loc'])}: {found['type']}"
            for found in error.errors(include_input=False, include_url=False)[:5]
        )
        return unavailable(draft_id, f"not a version {SNAPSHOT_VERSION} snapshot ({places})")
    except (ValueError, TypeError, RecursionError) as error:
        return unavailable(draft_id, f"not readable as JSON ({type(error).__name__})")
    if snapshot.plan.plan_date != plan_date:
        return unavailable(draft_id, "its plan is for another evening than its draft")
    if plan_assignment_ids is None or list(plan_assignment_ids) != snapshot.assignment_ids:
        return unavailable(draft_id, "its assignments are not the ones its draft lists")
    return SnapshotReading(snapshot)


def unavailable(draft_id: str, why: str) -> SnapshotReading:
    """Log, in a bounded line, that a draft's structured view cannot be shown, and say so."""
    logger.warning(
        "the structured view of draft %s is unavailable: %s", draft_id, why[:DIAGNOSTIC_LIMIT]
    )
    return SnapshotReading(None, unavailable=True)
