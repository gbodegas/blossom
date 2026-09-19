"""One saved plan as a page reads it: by assignment when it can be, as saved text when not.

A page shows a plan in one of two readings. The structured reading is built
from the snapshot saved beside the text: the blocks in time order with their
reasons, what was put off, the dates to clarify, and the reviewer's notes,
each row naming its assignment by the title, course, and due date the run
read, and linking to that assignment's current record by id. The text reading
is the saved text taken apart along the composer's shapes, for a plan from
before snapshots and for one whose snapshot cannot be used; nothing in it is
linked or annotated, because a line of text does not say which assignment it
is about.

What is saved and what stands now are kept apart here. The rows come from the
snapshot and never change. A row of the plan a page presents as today's
working plan can carry one current fact beside it, that she reports its
assignment as Done, with the day of that report; a plan shown as history
carries none, whatever her updates say now. Nothing here reads a store or
decides which plan is current: a view hands in the record, the marks, and
the ids on record, and gets back what the template shows.
"""

import re
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from blossom.clock import spoken_time
from blossom.plan_snapshot import PlanSnapshot, SavedReview, read_snapshot
from blossom.plan_text import one_line, plain
from blossom.stores.drafts import DraftRecord

Reader = Literal["student", "family"]
"""Whose page the plan is on, which decides only the wording beside it."""


@dataclass(frozen=True)
class DoneMark:
    """That she reports an assignment as Done as things stand, and when she said so."""

    reported_on: date
    """The household day of the report whose words stand; not a day the work was finished."""
    restored_on: date | None = None
    """The day an undo put that report back, when one did."""


@dataclass(frozen=True)
class NamedWork:
    """An assignment as a saved plan names it, from what the run read, with the way to its
    current record when it has one."""

    assignment_id: str
    title: str
    course: str
    due_date: date | None
    reference: str | None
    """The assignment's own id, shown in small text only when another assignment in the
    plan has the same title, course, and due date, so the two can be told apart."""
    href: str | None
    """The assignment's details, by id; ``None`` when no such assignment is on record now,
    and then no link is guessed."""
    unavailable: bool
    """Whether a link was wanted and no such assignment is on record now, which the row
    says in a sentence beside the label it keeps."""
    link_name: str
    """What a reader who hears the page is told the link is: the title and the course, and
    the due date or the id too when other links in the plan would read the same."""
    needs_date: bool = False
    """Whether another assignment in the plan has the same title, so a line that names
    this one by title says its course and its due date."""


@dataclass(frozen=True)
class BlockRow:
    """One reserved stretch of the evening, as saved, with a current mark when it has one."""

    dom_id: str
    span: str
    work: NamedWork
    rationale: str
    done: DoneMark | None = None


@dataclass(frozen=True)
class DeferralRow:
    """One assignment the plan put off, as saved, with a current mark when it has one."""

    dom_id: str
    work: NamedWork
    reason: str
    done: DoneMark | None = None


@dataclass(frozen=True)
class ClarificationRow:
    """One date the plan asked someone to clarify, in the words composed then."""

    work: NamedWork
    text: str


@dataclass(frozen=True)
class PlanReading:
    """What a page shows for one saved plan."""

    draft_id: str
    anchor: str
    """The id of the plan's container on the page, made from the draft's own id, so a link
    can land on this plan and no other."""
    reader: Reader
    current: bool
    """Whether the page presents this as today's working plan, the one reading that shows
    her updates beside the rows."""
    structured: bool
    unavailable: bool
    """Whether a snapshot is saved and cannot be used, which the page says in a sentence."""
    body: str
    """The text as composed, unchanged, for the text reading and the original-text fold."""
    title: str = ""
    intro: list[str] = field(default_factory=list)
    blocks: list[BlockRow] = field(default_factory=list)
    deferrals: list[DeferralRow] = field(default_factory=list)
    clarifications: list[ClarificationRow] = field(default_factory=list)
    review: SavedReview | None = None

    @property
    def annotated(self) -> bool:
        """Whether any row carries a current mark, which the plan then explains once."""
        return any(row.done for row in self.blocks) or any(row.done for row in self.deferrals)

    @property
    def done_work(self) -> list[NamedWork]:
        """The distinct assignments whose rows are marked, in the plan's own order."""
        marked = [row.work for row in self.blocks if row.done is not None]
        marked += [row.work for row in self.deferrals if row.done is not None]
        found: dict[str, NamedWork] = {}
        for work in marked:
            found.setdefault(work.assignment_id, work)
        return list(found.values())


def anchor_for(draft_id: str) -> str:
    """A page id for a plan's container, from the draft's id and nothing else."""
    return "plan-" + re.sub(r"[^A-Za-z0-9]+", "-", draft_id).strip("-")


def long_date(value: date) -> str:
    """A date with its year, as a row says it: ``September 21, 2026``."""
    return f"{value:%B} {value.day}, {value.year}"


def plan_title(value: date) -> str:
    """The plan's heading, as the composer writes it."""
    return f"Plan for {value:%A, %B} {value.day}, {value.year}"


def read_plan(
    record: DraftRecord,
    *,
    reader: Reader,
    current: bool = False,
    link_for: Callable[[str], str] | None = None,
    on_record: Collection[str] | None = None,
    done: Mapping[str, DoneMark] | None = None,
) -> PlanReading:
    """The reading of one draft for one page.

    ``current`` is the view's word that this is today's working plan; only
    then are ``done`` marks put beside rows, each occurrence of an assignment
    getting the same mark. ``link_for`` makes the address of an assignment's
    details and ``on_record`` is every assignment id on record now: an id not
    among them gets no link. A draft without a usable snapshot gets the text
    reading, with no links and no marks, whatever else is handed in.
    """
    found = read_snapshot(
        record.draft_id,
        record.plan_snapshot,
        plan_date=record.plan_date,
        plan_assignment_ids=record.plan_assignment_ids,
    )
    anchor = anchor_for(record.draft_id)
    if found.snapshot is None:
        return PlanReading(
            draft_id=record.draft_id,
            anchor=anchor,
            reader=reader,
            current=current,
            structured=False,
            unavailable=found.unavailable,
            body=record.body,
        )
    snapshot = found.snapshot
    marks = dict(done or {}) if current else {}
    names = named_work(snapshot, link_for, on_record)
    ordered = sorted(enumerate(snapshot.plan.blocks), key=lambda pair: pair[1].starts_at)
    return PlanReading(
        draft_id=record.draft_id,
        anchor=anchor,
        reader=reader,
        current=current,
        structured=True,
        unavailable=False,
        body=record.body,
        title=plan_title(snapshot.plan.plan_date),
        intro=[plain(line) for line in snapshot.intro],
        blocks=[
            BlockRow(
                dom_id=f"{anchor}-block-{index}",
                span=f"{spoken_time(block.starts_at)} to {spoken_time(block.ends_at)}",
                work=names[block.assignment_id],
                rationale=one_line(block.rationale),
                done=marks.get(block.assignment_id),
            )
            for index, block in ordered
        ],
        deferrals=[
            DeferralRow(
                dom_id=f"{anchor}-deferral-{index}",
                work=names[item.assignment_id],
                reason=one_line(item.reason),
                done=marks.get(item.assignment_id),
            )
            for index, item in enumerate(snapshot.plan.deferred)
        ],
        clarifications=[
            ClarificationRow(work=names[item.assignment_id], text=plain(item.text))
            for item in snapshot.clarifications
        ],
        review=None
        if snapshot.review is None
        else SavedReview(
            intro=[plain(line) for line in snapshot.review.intro],
            findings=[
                finding.model_copy(update={"text": plain(finding.text)})
                for finding in snapshot.review.findings
            ],
        ),
    )


def named_work(
    snapshot: PlanSnapshot,
    link_for: Callable[[str], str] | None,
    on_record: Collection[str] | None,
) -> dict[str, NamedWork]:
    """Each assignment the plan speaks about as its rows name it.

    Two assignments are told apart by id and never by their words. What a
    reader sees is the frozen title with its course and due date; when two
    in one plan agree on all three, each also shows its id. What a reader
    hears for a link is the title and course, with the due date added when
    another assignment in the plan has the same title, and the id when the
    course and the date are the same too.
    """
    saved = snapshot.assignments
    same_label = Counter((item.title, item.course, item.due_date) for item in saved.values())
    same_title = Counter(item.title for item in saved.values())
    names: dict[str, NamedWork] = {}
    for assignment_id, item in saved.items():
        title, course = plain(item.title), plain(item.course)
        collides = same_label[(item.title, item.course, item.due_date)] > 1
        heard = f"{title}, {course}"
        if same_title[item.title] > 1:
            when = "no due date" if item.due_date is None else f"due {long_date(item.due_date)}"
            heard = f"{heard}, {when}"
        if collides:
            heard = f"{heard}, {assignment_id}"
        gone = link_for is not None and on_record is not None and assignment_id not in on_record
        names[assignment_id] = NamedWork(
            assignment_id=assignment_id,
            title=title,
            course=course,
            due_date=item.due_date,
            reference=assignment_id if collides else None,
            href=link_for(assignment_id) if link_for is not None and not gone else None,
            unavailable=gone,
            link_name=heard,
            needs_date=same_title[item.title] > 1,
        )
    return names
