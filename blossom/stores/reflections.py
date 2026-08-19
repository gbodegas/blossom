from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReflectionSubject(StrEnum):
    SYSTEM = "SYSTEM"
    STUDENT = "STUDENT"
    PARENT = "PARENT"


@dataclass(frozen=True)
class Reflection:
    reflection_id: str
    subject: ReflectionSubject
    observation: str
    observed_at: datetime


class ReflectionsStore:
    name = "reflections"
    retention_policy = "Retain system self-observations for 90 days for behavior review."

    def __init__(self) -> None:
        self._reflections: list[Reflection] = []

    def write(self, reflection: Reflection) -> None:
        if reflection.subject is not ReflectionSubject.SYSTEM:
            msg = "reflections may only describe the system's own behavior"
            raise ValueError(msg)
        self._reflections.append(reflection)

    def list_all(self) -> list[Reflection]:
        return list(self._reflections)
