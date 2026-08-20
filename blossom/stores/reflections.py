"""Store three: what the agent learns about itself, and never about her.

When a plan does not work, the agent records a short note it can read the next
time it plans something similar. That is the adaptation mechanism, and it is
the most dangerous component in the system, because the natural way to build it
drifts straight into profiling a child. The same loop, over the same evidence,
produces either "evening reminders for long-term projects do not lead to task
starts" or "she procrastinates on science projects". The first improves the
planner. The second is behavioural monitoring arriving through a side door, as
a consequence of an architectural pattern rather than a feature anyone asked
for.

``ReflectionsStore.write`` refuses any subject other than ``SYSTEM``. That is
the boundary made structural rather than left to the wording of a prompt.

Two commitments this store does not yet honour. Reflections are meant to be
readable, correctable and deletable by her, as a visible part of the interface
rather than a setting; there is no read-for-her or delete path here. And
retrieval is meant to weigh a reflection's age, since a September note about
after-school timing may be wrong by winter. ``observed_at`` is recorded, but
nothing reads it.
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
    """A note the agent wrote about its own behaviour, with the date it wrote it."""

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

        Raising rather than silently filtering is deliberate. A reflection
        about her that was quietly dropped would be indistinguishable from one
        never written, and whatever produced it would go uncorrected.
        """
        if reflection.subject is not ReflectionSubject.SYSTEM:
            msg = "reflections may only describe the system's own behavior"
            raise ValueError(msg)
        self._reflections.append(reflection)

    def list_all(self) -> list[Reflection]:
        """Return every stored reflection, as a copy."""
        return list(self._reflections)
