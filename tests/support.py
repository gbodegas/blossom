"""Shared test helpers.

Anything two test modules need lives here rather than in one of them, so the
modules do not import each other; cross-imports between test files make the
suite's collection order matter, which it should not. That covers the source
records, the scripted models, the two-assignment graph the plan graph tests
drive, and the fixture-week plans and route override the application tests
drive.

This is a plain module rather than `conftest.py`: importing from a conftest
makes the same file reachable under two module names, which mypy rejects.
"""

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from blossom.agent.graph import (
    Ask,
    CompiledPlanGraph,
    ModelAnswer,
    build_plan_graph,
    plan_graph_for,
)
from blossom.clock import FrozenClock
from blossom.dependencies import ApplicationState, get_application_state
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.routes.runs import PlanGraphs
from blossom.settings import (
    DEFAULT_EVENING_MINUTES,
    DEFAULT_TOO_MUCH_MINUTES,
    TIMEZONE_VARIABLE,
    Settings,
)
from blossom.stores.drafts import DraftsStore
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.reflections import Reflection, ReflectionsStore, ReflectionSubject
from blossom.stores.support_rules import SupportRule, SupportRulesStore
from blossom.stores.workload_signals import WorkloadSignalsStore

FIXTURE_TIMEZONE = "America/New_York"
"""The zone the synthetic fixtures are written in. A fictional household's."""


class OffsetlessTimeZone(tzinfo):
    """A zone that names itself and returns no offset.

    Python reads a datetime carrying this as naive, because aware means having
    a ``tzinfo`` that answers with an offset. A guard that only tests
    ``tzinfo is not None`` lets it through, and ``astimezone`` then treats it
    as the running machine's local time.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """No offset, which is what makes a datetime carrying this naive."""
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """No daylight-saving information either."""
        return None

    def tzname(self, dt: datetime | None) -> str:
        """A name, so the value looks aware at a glance."""
        return "offsetless"


NAIVE_INSTANTS = [
    # Naive on purpose: this list exists to prove the guards refuse it.
    datetime(2026, 8, 19, 9, 0),  # noqa: DTZ001
    datetime(2026, 8, 19, 9, 0, tzinfo=OffsetlessTimeZone()),
]
"""The two shapes Python calls naive: no zone at all, and a zone with no offset."""

OBSERVED_AT = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


def fixture_clock(instant: datetime | None = None) -> FrozenClock:
    """A clock in the fixtures' zone, pinned to their week unless told otherwise."""
    return FrozenClock(instant or OBSERVED_AT, ZoneInfo(FIXTURE_TIMEZONE))


def fixture_settings(**environ: str) -> Settings:
    """Settings for a test that starts the app, in the fixtures' time zone.

    The application has no default zone, so a test that builds settings from an
    explicit mapping has to supply one. This keeps that from being repeated,
    and keeps the value in one place if the fixtures ever move.
    """
    return Settings.from_environment({TIMEZONE_VARIABLE: FIXTURE_TIMEZONE, **environ})


def record(channel: SourceChannel, value: str, *, confidence: float = 0.8) -> SourceRecord:
    """Build a source record with a fixed observation time."""
    return SourceRecord(
        channel=channel,
        asserted_value=value,
        observed_at=OBSERVED_AT,
        confidence=confidence,
    )


class Scripted[T: BaseModel]:
    """A model callable that answers from a list and keeps every brief it was sent.

    Stands in for the planner or the critic. It raises rather than inventing an
    answer when the script runs out, so a test that makes one call too many
    fails there and not on a later assertion.
    """

    def __init__(self, *answers: ModelAnswer[T]) -> None:
        self.answers = list(answers)
        self.briefs: list[list[BaseMessage]] = []

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[T]:
        self.briefs.append(list(messages))
        if not self.answers:
            msg = f"the script ran out after {len(self.briefs) - 1} calls"
            raise AssertionError(msg)
        return self.answers.pop(0)

    @property
    def calls(self) -> int:
        """How many times the graph asked."""
        return len(self.briefs)


def ok[T: BaseModel](parsed: T) -> ModelAnswer[T]:
    """A complete, parsed answer, the shape a healthy model call produces."""
    return ModelAnswer(parsed=parsed, stop_reason="end_turn", parsing_error=None)


def hosted_tracer_attached() -> bool:
    """Ask the framework's real consumer whether it would attach a hosted tracer."""
    handlers = CallbackManager.configure().handlers
    return any(isinstance(handler, LangChainTracer) for handler in handlers)


# ------------------------------------------------ the two-assignment scripted graph


ZONE = ZoneInfo(FIXTURE_TIMEZONE)


PLAN_DATE = date(2026, 8, 19)


OBSERVED = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)


ESSAY = Assignment(
    assignment_id="assignment-canal-essay",
    course="World History",
    title="Canal Era comparison essay",
    due_date=date(2026, 8, 21),
    dependencies=[],
    reported_submission_status="in_progress",
)


PROBLEM_SET = Assignment(
    assignment_id="assignment-algebra-set",
    course="Algebra II",
    title="Quadratic modeling problem set",
    due_date=date(2026, 8, 24),
    dependencies=[],
    reported_submission_status="not_started",
)


def block(assignment: str, start: str, end: str) -> PlanBlock:
    return PlanBlock(
        assignment_id=assignment,
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
        rationale="the hardest thing first, while she is fresh",
    )


def good_plan() -> DailyPlan:
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[block("assignment-canal-essay", "16:30", "17:30")],
        deferred=[Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday")],
    )


def finding(
    judgment: Judgment, criterion: Criterion = Criterion.ORDER, critique: str = "reads well"
) -> CriterionFinding:
    return CriterionFinding(criterion=criterion, critique=critique, judgment=judgment)


def accepting() -> CriticVerdict:
    return CriticVerdict(findings=[finding(Judgment.PASSES, criterion) for criterion in Criterion])


class TwoChannelSource:
    """Deadline records for the two fixture assignments: one corroborated, one disputed."""

    def assignments(self) -> list[Assignment]:
        return [ESSAY, PROBLEM_SET]

    def deadline_records(self, assignment_id: str) -> list[SourceRecord]:
        if assignment_id == ESSAY.assignment_id:
            return [
                self.record(SourceChannel.LMS, "2026-08-21"),
                self.record(SourceChannel.PARENT_ENTRY, "2026-08-21"),
            ]
        if assignment_id == PROBLEM_SET.assignment_id:
            return [
                self.record(SourceChannel.LMS, "2026-08-24"),
                self.record(SourceChannel.PARENT_ENTRY, "2026-08-25"),
            ]
        return []

    def support_rules(self) -> list[SupportRule]:
        """The graph tests seed rules through the store, not the source."""
        return []

    def reflections(self) -> list[Reflection]:
        """The graph tests seed notes through the store, not the source."""
        return []

    @staticmethod
    def record(channel: SourceChannel, value: str) -> SourceRecord:
        return SourceRecord(
            channel=channel,
            asserted_value=value,
            observed_at=OBSERVED,
            confidence=0.8,
        )


def stores(
    assignments: Sequence[Assignment] = (ESSAY, PROBLEM_SET),
) -> tuple[ProjectStateStore, SupportRulesStore, ReflectionsStore]:
    project_state = ProjectStateStore(
        sqlite3.connect(":memory:", check_same_thread=False), fixture_clock()
    )
    project_state.upsert_assignments(list(assignments))
    return project_state, SupportRulesStore(), ReflectionsStore()


def graph_with(
    planner: Ask[DailyPlan],
    critic: Ask[CriticVerdict],
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    drafts: DraftsStore | None = None,
    assignments: Sequence[Assignment] = (ESSAY, PROBLEM_SET),
    rules: Sequence[str] = (),
    notes: Sequence[str] = (),
    source: TwoChannelSource | None = None,
    signals: WorkloadSignalsStore | None = None,
    evening_minutes: int = DEFAULT_EVENING_MINUTES,
    too_much_minutes: int = DEFAULT_TOO_MUCH_MINUTES,
) -> CompiledPlanGraph:
    project_state, support_rules, reflections = stores(assignments)
    for index, rule in enumerate(rules):
        support_rules.add_rule(
            SupportRule(rule_id=f"rule-{index}", instruction=rule, asserted_at=OBSERVED)
        )
    for index, note in enumerate(notes):
        reflections.write(
            Reflection(
                reflection_id=f"note-{index}",
                subject=ReflectionSubject.SYSTEM,
                observation=note,
                observed_at=OBSERVED,
            )
        )
    return build_plan_graph(
        project_state=project_state,
        support_rules=support_rules,
        reflections=reflections,
        drafts=drafts or drafts_in_memory(),
        signals=signals or signals_in_memory(),
        clock=fixture_clock(),
        planner=planner,
        critic=critic,
        checkpointer=checkpointer or InMemorySaver(),
        source=source or TwoChannelSource(),
        evening_minutes=evening_minutes,
        too_much_minutes=too_much_minutes,
    )


def drafts_in_memory() -> DraftsStore:
    return DraftsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())


def signals_in_memory() -> WorkloadSignalsStore:
    return WorkloadSignalsStore(
        sqlite3.connect(":memory:", check_same_thread=False), fixture_clock()
    )


# ------------------------------------------------- the fixture week through the app


def fixture_week_plan() -> DailyPlan:
    """A plan that passes every check against the fixture week."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first, while she is fresh",
            ),
            PlanBlock(
                assignment_id="assignment-science-fair-proposal",
                starts_at=time(18, 0),
                ends_at=time(18, 30),
                rationale="nobody has confirmed this date, so it gets done tonight",
            ),
        ],
        deferred=[
            Deferral(assignment_id="assignment-algebra-set", reason="not due until Monday"),
            Deferral(
                assignment_id="assignment-textbook-cover", reason="five minutes on the weekend"
            ),
            Deferral(assignment_id="assignment-reading-log", reason="a page a night is on track"),
            Deferral(assignment_id="assignment-signed-syllabus", reason="ask what the date is"),
            Deferral(assignment_id="assignment-vocabulary-quiz", reason="the portal says Friday"),
        ],
    )


def light_fixture_plan() -> DailyPlan:
    """The fixture week's plan cut to fit a reduced evening: the essay, and the rest put off."""
    whole = fixture_week_plan()
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=whole.blocks[:1],
        deferred=[
            *whole.deferred,
            Deferral(
                assignment_id="assignment-science-fair-proposal", reason="tonight is too much"
            ),
        ],
    )


def forgetful_fixture_plan() -> DailyPlan:
    """Leaves two assignments unmentioned, so tier one fails every round."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            PlanBlock(
                assignment_id="assignment-canal-essay",
                starts_at=time(16, 30),
                ends_at=time(17, 30),
                rationale="the essay first",
            )
        ],
    )


def scripted_graphs(
    planner: Callable[[], list[DailyPlan]], critic: Callable[[], list[CriticVerdict]]
) -> Callable[..., PlanGraphs]:
    """A replacement for the route's graphs dependency, over the app's own stores.

    Scripted models, and permission to start, so a run can be driven in an
    application that has no key; the models are never asked for one.
    """

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        return PlanGraphs(
            build=lambda: plan_graph_for(
                state,
                planner=Scripted(*[ok(plan) for plan in planner()]),
                critic=Scripted(*[ok(verdict) for verdict in critic()]),
            ),
            may_start=True,
        )

    return override


def human_text(brief: Sequence[BaseMessage]) -> str:
    human = [message for message in brief if isinstance(message, HumanMessage)]
    assert len(human) == 1
    return str(human[0].content)
