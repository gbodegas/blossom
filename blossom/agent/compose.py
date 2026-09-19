"""Turn a plan, and what was found about it, into the text she reads on her page.

The draft is text, and one text serves both readers: she reads it as her plan
for the evening, and a parent reads the same words when reviewing it, so
nothing is said to one that the other cannot see. It is written to her. This is
where the plan stops being data and becomes a page a person can read in one
look: the blocks in order with their reasons, what was put off and why, the
due dates that are not settled, and what the reviewer thought. The reviewer's
notes travel with the plan rather than deciding for it, because tier two
informs the person reading and never decides for them.

One composition makes two things from the same frozen inputs: the text, and
the snapshot saved beside it, the plan as data with the assignments it names
and the sentences written around it. Each sentence is worded once, here, and
both are derived from that wording, so the two cannot drift apart and neither
is ever read back to make the other.

The draft is made through ``create_draft``, the one registered way anything
leaves the agent, called directly rather than through a tool loop. A model
never chooses to call it; the graph calls it once, after the checks.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from blossom.clock import spoken_time
from blossom.drafts import Draft
from blossom.heuristic_relevance import CriticVerdict, Judgment
from blossom.noticing import Noticing
from blossom.plan_checks import PlanVerification
from blossom.plan_snapshot import (
    SNAPSHOT_VERSION,
    PlanSnapshot,
    SavedAssignment,
    SavedClarification,
    SavedFinding,
    SavedReview,
)
from blossom.plan_text import one_line
from blossom.plans import DailyPlan
from blossom.reconciliation import SourceConfidence
from blossom.stores.project_state import Assignment
from blossom.tools import create_draft

CLARIFY = "Dates needing clarification:"
NOTHING_TONIGHT = "Nothing is scheduled tonight."
WAITING = "Waiting for another day:"
REVIEW = "The reviewer's notes:"
UNSETTLED = "The reviewer did not settle on this plan. Its notes are at the end."

JUDGMENT_WORDS = {
    Judgment.PASSES: "passes",
    Judgment.FAILS: "does not pass",
    Judgment.CANNOT_TELL: "could not assess",
}
"""The reviewer's verdict on one criterion, in words she reads rather than a token."""


class MissingPlanMetadata(ValueError):
    """A plan speaks about an assignment the run did not read, so there is no title to save
    beside it. The checks refuse such a plan before it is composed; composing one anyway is
    a failure, never leave to save an id where a title belongs."""

    def __init__(self, assignment_ids: Sequence[str]) -> None:
        super().__init__(
            "the plan names assignments the run did not read: " + ", ".join(assignment_ids)
        )


@dataclass(frozen=True)
class Composition:
    """One composition: the text she reads and the snapshot saved beside it, from the same
    frozen inputs and the same wording."""

    draft: Draft
    snapshot: PlanSnapshot


@dataclass(frozen=True)
class Wording:
    """The sentences composed around a plan, worded once: what the text is written from and
    what the snapshot keeps."""

    intro: list[str]
    clarifications: list[SavedClarification]
    review: SavedReview | None


def spoken_date(value: date) -> str:
    """A date the way a person says it, with the year: ``Wednesday, August 19, 2026``."""
    return f"{value:%A, %B} {value.day}, {value.year}"


def short_date(value: date) -> str:
    """A date short enough for a parenthesis: ``Aug 21``."""
    return f"{value:%b} {value.day}"


def named(saved: SavedAssignment | None, assignment_id: str) -> str:
    """An assignment as the text names it: title, course, and when it is due. The id alone
    when nothing was read about it, which only a caller composing text by hand can meet."""
    if saved is None:
        return assignment_id
    when = (
        "no due date on record" if saved.due_date is None else f"due {short_date(saved.due_date)}"
    )
    return f"{saved.title} ({saved.course}, {when})"


def as_saved(item: Assignment) -> SavedAssignment:
    """What is kept of an assignment beside a plan: its title, course, and due date."""
    return SavedAssignment(title=item.title, course=item.course, due_date=item.due_date)


def wording(
    *,
    assignments: Sequence[Assignment],
    verdict: CriticVerdict | None,
    settled: bool,
    noticings: Sequence[Noticing],
    confidence: Mapping[str, SourceConfidence],
    too_much: bool,
    budget_minutes: int | None,
) -> Wording:
    """Word everything said around the plan, once.

    A date is listed for clarification only when something is missing or
    contested: no date on record, sources that give different dates, or
    sources that give a date the record does not match. A date one channel
    gives and nothing disputes is not a task for anyone, and neither is a date
    only the family's record has, or one the sources spoke about in words the
    comparator could not read: lack of corroboration alone does not call for a
    word with the school. How far a date can be trusted stays on her page
    beside the item.
    """
    noticed_by_id = {item.assignment_id: item for item in noticings}
    intro: list[str] = []
    if too_much:
        intro.append(
            f"You said today was too much, so this plan is kept to {budget_minutes} minutes."
        )
    if not settled:
        intro.append(UNSETTLED)

    def clarification(item: Assignment) -> str | None:
        """Why this item's date needs a word with someone, or ``None`` when it does not."""
        noticed = noticed_by_id.get(item.assignment_id)
        claims = "; ".join(noticed.spoken) if noticed is not None else ""
        if item.due_date is None:
            if noticed is not None and noticed.contradicted:
                return f"the date needs asking about; the sources say {claims}"
            return "the date needs asking about"
        if noticed is not None and noticed.contradicted:
            return f"recorded as due {short_date(item.due_date)}, but the sources say {claims}"
        if confidence.get(item.assignment_id) == SourceConfidence.SOURCES_DISAGREE:
            return f"the sources give different dates: {claims}"
        return None

    clarifications = [
        SavedClarification(assignment_id=item.assignment_id, text=reason)
        for item in assignments
        if (reason := clarification(item)) is not None
    ]
    review = None
    if verdict is not None:
        review = SavedReview(
            intro=[f"The reviewer did not consider: {', '.join(verdict.missing)}."]
            if verdict.missing
            else [],
            findings=[
                SavedFinding(
                    label=f"{finding.criterion} ({JUDGMENT_WORDS[finding.judgment]})",
                    text=one_line(finding.critique),
                )
                for finding in verdict.findings
            ],
        )
    return Wording(intro=intro, clarifications=clarifications, review=review)


def body_of(plan: DailyPlan, read: Mapping[str, SavedAssignment], said: Wording) -> str:
    """The text she reads: the evening, then the dates to clarify, then the review."""
    lines = [f"Plan for {spoken_date(plan.plan_date)}", *said.intro, ""]
    for block in sorted(plan.blocks, key=lambda item: item.starts_at):
        lines.append(
            f"{spoken_time(block.starts_at)} to {spoken_time(block.ends_at)}, set aside for "
            f"{named(read.get(block.assignment_id), block.assignment_id)}"
        )
        lines.append(f"    {one_line(block.rationale)}")
    if not plan.blocks:
        lines.append(NOTHING_TONIGHT)
    if plan.deferred:
        lines.extend(["", WAITING])
        lines.extend(
            f"- {named(read.get(item.assignment_id), item.assignment_id)}: {one_line(item.reason)}"
            for item in plan.deferred
        )
    if said.clarifications:
        lines.extend(["", CLARIFY])
        lines.extend(
            f"- {named(read.get(item.assignment_id), item.assignment_id)}: {item.text}"
            for item in said.clarifications
        )
    if said.review is not None:
        lines.extend(["", REVIEW])
        lines.extend(f"- {line}" for line in said.review.intro)
        lines.extend(f"- {finding.label}: {finding.text}" for finding in said.review.findings)
    return "\n".join(lines)


def compose(
    *,
    draft_id: str,
    plan: DailyPlan,
    assignments: Sequence[Assignment],
    verification: PlanVerification,
    verdict: CriticVerdict | None,
    settled: bool,
    noticings: Sequence[Noticing] = (),
    confidence: Mapping[str, SourceConfidence] | None = None,
    too_much: bool = False,
    budget_minutes: int | None = None,
) -> Composition:
    """The draft she reads and the snapshot saved beside it, from one set of frozen inputs.

    ``settled`` is whether the reviewer accepted the plan. When it did not, the
    heading says so, and the notes below show why, so the plan is presented as
    a proposal with a dissent attached rather than as a recommendation.

    The snapshot keeps the plan as it is, the title, course, and due date of
    each assignment it speaks about, copied from what the run read, and the
    sentences composed around it. An assignment the plan names and the run
    did not read is ``MissingPlanMetadata``, and a snapshot that does not
    agree with itself is refused by its own type: either is a failure of the
    composition, and no draft comes of it.

    ``draft_id`` is given rather than generated because the graph derives it
    from its thread: a node that runs twice must produce the same draft, and
    the drafts table keys on it.
    """
    read = {item.assignment_id: as_saved(item) for item in assignments}
    unread = sorted(set(plan.assignment_ids) - read.keys())
    if unread:
        raise MissingPlanMetadata(unread)
    said = wording(
        assignments=assignments,
        verdict=verdict,
        settled=settled,
        noticings=noticings,
        confidence=confidence or {},
        too_much=too_much,
        budget_minutes=budget_minutes,
    )
    snapshot = PlanSnapshot(
        version=SNAPSHOT_VERSION,
        plan=plan,
        assignments={name: read[name] for name in sorted(set(plan.assignment_ids))},
        intro=said.intro,
        clarifications=said.clarifications,
        review=said.review,
    )
    draft = create_draft({"body": body_of(plan, read, said)}).model_copy(
        update={"draft_id": draft_id}
    )
    return Composition(draft=draft, snapshot=snapshot)


def compose_draft(
    *,
    draft_id: str,
    plan: DailyPlan,
    assignments: Sequence[Assignment],
    verification: PlanVerification,
    verdict: CriticVerdict | None,
    settled: bool,
    noticings: Sequence[Noticing] = (),
    confidence: Mapping[str, SourceConfidence] | None = None,
    too_much: bool = False,
    budget_minutes: int | None = None,
) -> Draft:
    """The text alone, for a caller with no snapshot to save: the same wording and the same
    text as ``compose``, and an assignment nothing was read about is named by its id."""
    said = wording(
        assignments=assignments,
        verdict=verdict,
        settled=settled,
        noticings=noticings,
        confidence=confidence or {},
        too_much=too_much,
        budget_minutes=budget_minutes,
    )
    read = {item.assignment_id: as_saved(item) for item in assignments}
    return create_draft({"body": body_of(plan, read, said)}).model_copy(
        update={"draft_id": draft_id}
    )
