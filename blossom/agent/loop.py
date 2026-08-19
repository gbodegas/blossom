from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


@dataclass(frozen=True, kw_only=True)
class AgentStep:
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
    contradiction = step.expectation.casefold() not in observation.casefold()
    return replace(step, observation=observation, contradiction=contradiction)
