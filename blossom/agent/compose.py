"""Turn a plan, and what was found about it, into the text a parent reads at the gate.

The gate pauses with a draft, and a draft is text. This is where the plan
stops being data and becomes a page a person can read in one look: the blocks
in order with their reasons, what was put off and why, the due dates that are
not settled, and what the reviewer thought. The reviewer's notes travel with
the plan rather than deciding for it, because tier two informs the person at
the gate and never closes it.

The draft is made through ``create_draft``, the one registered way anything
leaves the agent, called directly rather than through a tool loop. A model
never chooses to call it; the graph calls it once, after the checks.
"""

from collections.abc import Sequence
from datetime import date

from blossom.drafts import Draft
from blossom.heuristic_relevance import CriticVerdict
from blossom.noticing import Noticing
from blossom.plan_checks import PlanVerification
from blossom.plans import DailyPlan
from blossom.stores.project_state import Assignment
from blossom.tools import create_draft


def spoken_date(value: date) -> str:
    """A date the way a person says it, with the year: ``Wednesday, August 19, 2026``."""
    return f"{value:%A, %B} {value.day}, {value.year}"


def short_date(value: date) -> str:
    """A date short enough for a parenthesis: ``Aug 21``."""
    return f"{value:%b} {value.day}"


def compose_draft(
    *,
    draft_id: str,
    plan: DailyPlan,
    assignments: Sequence[Assignment],
    verification: PlanVerification,
    verdict: CriticVerdict | None,
    settled: bool,
    noticings: Sequence[Noticing] = (),
) -> Draft:
    """The draft a parent reads: the evening, then what is uncertain, then the review.

    ``settled`` is whether the reviewer accepted the plan. When it did not, the
    heading says so, and the notes below show why, so the plan is presented as
    a proposal with a dissent attached rather than as a recommendation.

    ``draft_id`` is given rather than generated because the graph derives it
    from its thread: a node that runs twice must produce the same draft, and
    the drafts table keys on it.
    """
    by_id = {item.assignment_id: item for item in assignments}

    def named(assignment_id: str) -> str:
        item = by_id.get(assignment_id)
        if item is None:
            return assignment_id
        when = (
            "no due date on record" if item.due_date is None else f"due {short_date(item.due_date)}"
        )
        return f"{item.title} ({item.course}, {when})"

    lines = [f"Plan for {spoken_date(plan.plan_date)}"]
    if not settled:
        lines.append("The reviewer did not settle on this plan. Its notes are at the end.")
    lines.append("")

    for block in sorted(plan.blocks, key=lambda item: item.starts_at):
        lines.append(
            f"{block.starts_at:%H:%M} to {block.ends_at:%H:%M}  {named(block.assignment_id)}"
        )
        lines.append(f"    {block.rationale}")
    if not plan.blocks:
        lines.append("Nothing is scheduled tonight.")

    if plan.deferred:
        lines.extend(["", "Waiting for another day:"])
        lines.extend(f"- {named(item.assignment_id)}: {item.reason}" for item in plan.deferred)

    if verification.uncertain_due_dates:
        lines.extend(["", "Due dates worth checking with the school:"])
        lines.extend(
            f"- {named(assignment_id)}" for assignment_id in verification.uncertain_due_dates
        )

    if verification.undated:
        lines.extend(["", "No due date on record; worth asking:"])
        lines.extend(f"- {named(assignment_id)}" for assignment_id in verification.undated)

    contradicted = [item for item in noticings if item.contradicted]
    if contradicted:
        lines.extend(["", "The record and the school disagree; the record may need correcting:"])
        lines.extend(
            f"- {named(item.assignment_id)}, but the sources say {item.sources_say()}"
            for item in contradicted
        )

    if verdict is not None:
        lines.extend(["", "The reviewer's notes:"])
        if verdict.missing:
            skipped = ", ".join(verdict.missing)
            lines.append(f"- The reviewer did not consider: {skipped}.")
        lines.extend(
            f"- {finding.criterion} ({finding.judgment.value}): {finding.critique}"
            for finding in verdict.findings
        )

    return create_draft({"body": "\n".join(lines)}).model_copy(update={"draft_id": draft_id})
