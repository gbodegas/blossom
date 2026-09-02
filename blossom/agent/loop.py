"""Reason-act-observe step records, each carrying the expectation the tool call was made against.

The agent states what it expects a tool call to return before making it. An
observation compared against that expectation becomes confirmation or
contradiction, and a contradiction signals that something the system believed
may be wrong.

``AgentStep`` requires a non-blank expectation, so a step cannot be recorded
without one. ``compare_expectation_to_observation`` tests whether the
expectation is a substring of the observation, which is not equivalence:
expecting "Friday" and observing "8/21" reads as a contradiction. The intended
replacement is a typed expectation checked deterministically and a three-way
verdict (confirmed, contradicted, undecidable); an undecidable comparison must
not default to contradiction.

Status: placeholder. Steps are not persisted and the verdict is not acted on by
any caller; a step is built, compared, and dropped inside one function call.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


@dataclass(frozen=True, kw_only=True)
class AgentStep:
    """One tool call, with the belief it was made against.

    Frozen and keyword-only. ``expectation`` must be non-blank because a step
    without one cannot be checked.
    """

    expectation: str
    tool_name: str
    tool_input: dict[str, Any]
    observation: str | None = None
    contradiction: bool = False
    timestamp: datetime

    def __post_init__(self) -> None:
        if not self.expectation.strip():
            msg = "expectation is required before an action step is recorded"
            raise ValueError(msg)


def compare_expectation_to_observation(step: AgentStep, observation: str) -> AgentStep:
    """Attach an observation to a step and flag whether it contradicts the expectation.

    Placeholder: the comparison is a substring test, not equivalence.
    """
    contradiction = step.expectation.casefold() not in observation.casefold()
    return replace(step, observation=observation, contradiction=contradiction)
