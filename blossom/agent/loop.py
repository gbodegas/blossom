"""The reason-act-observe step, and the expectation that makes an observation mean something.

The discipline is that the agent states what it expects a tool call to return
before making it. An observation on its own is data. Compared against a stated
expectation it becomes either confirmation or contradiction, and the
contradiction is the more valuable of the two, because it says something the
system believed may be wrong.

``AgentStep`` enforces the first half: the expectation is a required keyword
argument and cannot be blank, so a step cannot be recorded without one.

The second half does not work yet, and the gap is larger than it looks.
``compare_expectation_to_observation`` tests whether the expectation is a
substring of the observation. That is not equivalence -- expecting "Friday" and
observing "8/21" reads as a contradiction, and the reverse is equally easy to
construct. Worse, in the one place this runs today
(``blossom/routes/student.py``) the expectation is a lookup key and the
observation is the record id the store echoed back, so the comparison is a
string against itself and ``contradiction`` can never be True. The result is
also discarded: only ``expectation`` is read from the returned step.

What this needs is a typed expectation, so that a claim about a value is
checked deterministically, and a three-way verdict, so that "cannot tell" stays
distinguishable from "these disagree". Defaulting an undecidable comparison to
contradiction would flood the one signal the system most needs to keep clean.

Status: placeholder. Nothing persists these steps either, so the checkable
trace the design calls for does not exist -- a step is built, compared, and
dropped inside a single function call.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


@dataclass(frozen=True, kw_only=True)
class AgentStep:
    """One tool call, with the belief it was made against.

    Frozen and keyword-only so a step cannot be assembled positionally or
    edited after the fact. ``expectation`` is validated as non-blank in
    ``__post_init__``: a step without one cannot be checked later, which
    defeats the point of recording it.
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

    Placeholder. The comparison is a substring test, which is not equivalence,
    and it currently cannot return True anywhere it is called. See the module
    docstring for why, and for what would replace it.
    """
    contradiction = step.expectation.casefold() not in observation.casefold()
    return replace(step, observation=observation, contradiction=contradiction)
