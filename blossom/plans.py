"""What a daily plan is: blocks of time, and the work left out of them on purpose.

A plan is a proposal about her evening, so it is written down as data rather
than prose. That is what lets a deterministic check decide most of what
"good" means before any judgment is involved, and it is what lets the page
show her the same thing the checks read.

Two shapes matter. A block says when she will work on one assignment. A
deferral says an assignment in the window is not in tonight's plan and why,
which exists so that leaving something out is a statement rather than an
omission: nothing in this system silently drops work.

Times are wall clock in the household's zone, not instants, because a block is
a future local event. "Six to seven on Thursday" survives a change to the zone
rules; an instant computed from it does not. The duration is worked out in UTC
when it is needed, which is what makes it right across a daylight-saving
night.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanBlock(BaseModel):
    """One stretch of time set aside for one assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    starts_at: time
    ends_at: time
    rationale: str = Field(
        description="Why this assignment sits here, in a sentence she would recognize."
    )

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "PlanBlock":
        if self.ends_at <= self.starts_at:
            msg = f"a block must end after it starts, got {self.starts_at} to {self.ends_at}"
            raise ValueError(msg)
        return self

    def minutes(self, on: date, zone: ZoneInfo) -> int:
        """How long this block runs, measured through UTC.

        Subtracting two wall-clock times is right only when the day has
        twenty-four hours. On the night the clocks move it does not, so both
        ends are placed in the household's zone and compared as instants.
        """
        # Both ends are converted before subtracting. Python subtracts two aware
        # datetimes that share a tzinfo in wall-clock terms, ignoring the zone,
        # so leaving them in local time would give back the reading that is
        # wrong on exactly the two nights this exists for.
        start = datetime.combine(on, self.starts_at, tzinfo=zone).astimezone(UTC)
        end = datetime.combine(on, self.ends_at, tzinfo=zone).astimezone(UTC)
        return round((end - start) / timedelta(minutes=1))

    def overlaps(self, other: "PlanBlock") -> bool:
        """True when two blocks claim any of the same minute."""
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


class Deferral(BaseModel):
    """An assignment in the window that tonight's plan leaves for another day."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    reason: str = Field(description="Why it can wait, in a sentence she would recognize.")


class DailyPlan(BaseModel):
    """A plan for one day: what she works on, when, and what waits.

    Every assignment due in the window appears here exactly once, either as a
    block or as a deferral. A hard check enforces that; the type only makes it
    expressible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_date: date
    blocks: list[PlanBlock] = Field(default_factory=list)
    deferred: list[Deferral] = Field(default_factory=list)

    @property
    def assignment_ids(self) -> tuple[str, ...]:
        """Every assignment this plan speaks about, blocked or deferred."""
        return tuple(
            [block.assignment_id for block in self.blocks]
            + [item.assignment_id for item in self.deferred]
        )

    def total_minutes(self, zone: ZoneInfo) -> int:
        """Minutes of work the plan asks for, across every block."""
        return sum(block.minutes(self.plan_date, zone) for block in self.blocks)

    def overlapping_pairs(self) -> list[tuple[PlanBlock, PlanBlock]]:
        """Every pair of blocks that claim the same minute, in plan order."""
        ordered = sorted(self.blocks, key=lambda block: block.starts_at)
        return [
            (earlier, later)
            for index, earlier in enumerate(ordered)
            for later in ordered[index + 1 :]
            if earlier.overlaps(later)
        ]
