"""Store three: what the agent learns about itself, and never about her.

When a plan does not work, the agent records a short note to read the next time
it plans something similar. The same loop over the same evidence can produce
either "evening reminders for long-term projects do not lead to task starts" or
"she procrastinates on science projects". The first improves the planner; the
second is behavioral monitoring of a child. ``ReflectionsStore.write`` refuses
any subject other than ``SYSTEM`` so the boundary is structural rather than
left to the wording of a prompt.

Not done yet: a path for her to read, correct and delete reflections, and age
weighting at retrieval time (``observed_at`` is recorded but nothing reads it).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReflectionSubject(StrEnum):
    """Who a reflection is about. Only ``SYSTEM`` may ever be written."""

    SYSTEM = "SYSTEM"
    STUDENT = "STUDENT"
    PARENT = "PARENT"


@dataclass(frozen=True)
class Reflection:
    """A note the agent wrote about its own behavior, with the date it wrote it."""

    reflection_id: str
    subject: ReflectionSubject
    observation: str
    observed_at: datetime


class ReflectionsStore:
    """Holds the agent's self-observations, and only those."""

    name = "reflections"
    retention_policy = "Retain system self-observations for 90 days for behavior review."

    def __init__(self) -> None:
        self._reflections: list[Reflection] = []

    def write(self, reflection: Reflection) -> None:
        """Store a reflection, rejecting any subject other than ``SYSTEM``.

        It raises rather than filtering: a reflection about her that was
        quietly dropped would look like one never written, and whatever
        produced it would go uncorrected.
        """
        if reflection.subject is not ReflectionSubject.SYSTEM:
            msg = "reflections may only describe the system's own behavior"
            raise ValueError(msg)
        self._reflections.append(reflection)

    def list_all(self) -> list[Reflection]:
        """Return every stored reflection, as a copy."""
        return list(self._reflections)
