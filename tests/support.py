"""Shared test helpers.

Anything two test modules need lives here rather than in one of them, so the
modules do not import each other; cross-imports between test files make the
suite's collection order matter, which it should not. That covers the source
records, the scripted models, the two-assignment graph the plan graph tests
drive, the fixture-week plans and route override the application tests
drive, the browser on her page with its cards and forms, the small store
her reports and the family's checks are tested in, and the reading of a page
and its stylesheet that the layout and link color tests work out the cascade
with.

This is a plain module rather than `conftest.py`: importing from a conftest
makes the same file reachable under two module names, which mypy rejects.
"""

import dataclasses
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Any, Protocol
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from blossom.agent.compose import Composition, compose
from blossom.agent.graph import (
    Ask,
    CompiledPlanGraph,
    ModelAnswer,
    build_plan_graph,
    plan_graph_for,
)
from blossom.app import create_app
from blossom.assignment_status import AssignmentStatus, statuses_for
from blossom.candidates import candidate_readings, reader, readings_for, row_reader
from blossom.captures import (
    STUDENT,
    CaptureChanged,
    CaptureCreated,
    CaptureDetails,
    CapturePromoted,
    CaptureUnlinked,
    candidate_basis,
    new_capture_id,
)
from blossom.clock import Clock, FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, get_application_state
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.intake import PASTE_DAY, identity
from blossom.noticing import Noticing, Verdict
from blossom.plan_checks import check_plan
from blossom.plan_reading import anchor_for
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceChannel, SourceConfidence, SourceRecord
from blossom.routes.navigation import assignment_anchor, details_href
from blossom.routes.runs import PlanGraphs, plan_graphs
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    DEFAULT_EVENING_MINUTES,
    DEFAULT_TOO_MUCH_MINUTES,
    FIXTURE_PATH_VARIABLE,
    REPOSITORY_ROOT,
    TIMEZONE_VARIABLE,
    Settings,
)
from blossom.stores.drafts import DraftRecord, DraftsStore
from blossom.stores.project_state import (
    Assignment,
    AssignmentKind,
    ClaimReadings,
    ProjectStateStore,
    Saved,
    StatusReport,
    StudentStatus,
)
from blossom.stores.reflections import Reflection, ReflectionsStore, ReflectionSubject
from blossom.stores.support_rules import SupportRule, SupportRulesStore
from blossom.stores.workload_signals import WorkloadSignalsStore
from tests.state_guard import require_protection

FIXTURE_TIMEZONE = "America/New_York"
"""The zone the synthetic fixtures are written in. A fictional household's."""
FIXTURES = REPOSITORY_ROOT / "data" / "synthetic"
"""The synthetic set the suite runs against; the household's default is no fixture."""
ORIGIN = "http://testserver"
"""Where the test client's requests come from, as the app reads them: its own address."""
SAME_ORIGIN = {"Origin": ORIGIN}
"""The header every test client sends with every request, as a browser sends it with a
form or a script call: a request that would change something is refused without one
naming this server, so a client without it stands for a request made from elsewhere."""


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
    and keeps the value in one place if the fixtures ever move. The synthetic
    set is named the same way, since the household's default is no fixture.

    For a run the state guard stands behind. Anywhere else the paths these
    settings name are the defaults, the checkout's own record, so it refuses
    before it builds them.
    """
    require_protection("fixture_settings")
    return Settings.from_environment(
        {TIMEZONE_VARIABLE: FIXTURE_TIMEZONE, FIXTURE_PATH_VARIABLE: str(FIXTURES), **environ}
    )


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

    def deadline_records_by_assignment(
        self, assignment_ids: Iterable[str] | None = None
    ) -> dict[str, list[SourceRecord]]:
        """The same answers asked for together, so a subclass changes one method."""
        named = (
            [item.assignment_id for item in self.assignments()]
            if assignment_ids is None
            else assignment_ids
        )
        return {name: claims for name in named if (claims := self.deadline_records(name))}

    def read_claims(self, assignment_ids: Iterable[str] | None = None) -> ClaimReadings:
        """The same answers, with nothing that cannot be read."""
        return ClaimReadings(self.deadline_records_by_assignment(assignment_ids), frozenset())

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
    reports: Sequence[tuple[str, StudentStatus, str | None]] = (),
    on_record: ProjectStateStore | None = None,
    clock: Clock | None = None,
) -> CompiledPlanGraph:
    """The graph over in-memory stores. ``reports`` are what she has said about her part
    of each assignment named, status and note, saved before the run reads the week.
    ``on_record`` is a store the test made itself, from ``stores``, so it can change the
    record while a run is on its way."""
    project_state, support_rules, reflections = stores(assignments)
    if on_record is not None:
        project_state = on_record
    for assignment_id, status, note in reports:
        saved = project_state.report_status(
            assignment_id, status, note, expected_head=None, now=OBSERVED, today=PLAN_DATE
        )
        assert isinstance(saved, Saved)
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
        clock=clock or fixture_clock(),
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
    planner: Callable[[], list[DailyPlan]],
    critic: Callable[[], list[CriticVerdict]],
    *,
    planners: list[Scripted[DailyPlan]] | None = None,
    critics: list[Scripted[CriticVerdict]] | None = None,
) -> Callable[..., PlanGraphs]:
    """A replacement for the route's graphs dependency, over the app's own stores.

    Scripted models, and permission to start, so a run can be driven in an
    application that has no key; the models are never asked for one. Each
    planner built is added to ``planners`` when a test hands a list in, so
    it can read how often a run asked and what it was sent, and each critic
    to ``critics`` the same way.
    """

    def override(
        state: Annotated[ApplicationState, Depends(get_application_state)],
    ) -> PlanGraphs:
        def build() -> CompiledPlanGraph:
            asked = Scripted(*[ok(plan) for plan in planner()])
            if planners is not None:
                planners.append(asked)
            reviewer = Scripted(*[ok(verdict) for verdict in critic()])
            if critics is not None:
                critics.append(reviewer)
            return plan_graph_for(state, planner=asked, critic=reviewer)

        return PlanGraphs(build=build, may_start=True)

    return override


def work_listed(brief: Sequence[BaseMessage]) -> str:
    """The assignments a brief lists, whole: what the model was given to plan or review."""
    text = human_text(brief)
    return text[text.index("<assignments>") : text.index("</assignments>")]


def human_text(brief: Sequence[BaseMessage]) -> str:
    human = [message for message in brief if isinstance(message, HumanMessage)]
    assert len(human) == 1
    return str(human[0].content)


# ------------------------------------------------------------- her page, driven through the app

HER_PAGE = "/student/due-this-week"
ESSAY_ID = "assignment-canal-essay"
ESSAY_TITLE = "Canal Era comparison essay"
FIXTURE_WEEK = "2026-08-17"
"""The Monday of the fixture week her page shows on the pinned day."""
HERS = "the blue bicycle in the hallway"
THEIRS = "coffee before the school run"
PAGE_HEADERS = {"Accept": "text/html"}
MISSING_EMAIL = (
    "Assignments:\n08/19 World History - A: Homework: Canal Era comparison essay Grade: Missing\n"
)


class Answer(Protocol):
    """What these tests read of a response, whatever client library made it."""

    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


def browser(*, key: bool = False, **environ: str) -> TestClient:
    """Her page on the pinned day, with scripted models when ``key`` is set. For a run
    the state guard stands behind, like the settings it is built on."""
    require_protection("browser")
    with_key = {ANTHROPIC_API_KEY_VARIABLE: "not-a-key-and-never-sent"} if key else {}
    app = create_app(fixture_settings(BLOSSOM_TODAY=PLAN_DATE.isoformat(), **with_key, **environ))
    if key:
        app.dependency_overrides[plan_graphs] = scripted_graphs(
            lambda: [fixture_week_plan()], lambda: [accepting()]
        )
    return TestClient(app, follow_redirects=False, headers=SAME_ORIGIN)


PATHS = ("BLOSSOM_DATABASE_PATH", "BLOSSOM_CHECKPOINT_PATH", "BLOSSOM_TRACE_PATH")
"""The settings that place one household's three files."""


def files_in(folder: pathlib.Path) -> dict[str, str]:
    """The three files of one household under ``folder``, so a second app can open them
    on another day or with the sign-in on."""
    return {name: str(folder / f"{name.lower()}.sqlite3") for name in PATHS}


def signed_in_household(tmp_path: pathlib.Path) -> Settings:
    """The pinned day with the sign-in on, its files under ``tmp_path``. For a run the
    state guard stands behind: a folder handed in is the caller's word, not a check."""
    require_protection("signed_in_household")
    return fixture_settings(
        BLOSSOM_TODAY=PLAN_DATE.isoformat(),
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        BLOSSOM_STUDENT_PASSPHRASE=HERS,
        BLOSSOM_PARENT_PASSPHRASE=THEIRS,
    )


def state_of(client: TestClient) -> ApplicationState:
    state: ApplicationState = getattr(client.app.state, STATE_ATTRIBUTE)  # type: ignore[attr-defined]
    return state


def store_of(client: TestClient) -> ProjectStateStore:
    """The record's store the application on this client holds."""
    return state_of(client).project_state


def client_for(settings: Settings) -> TestClient:
    """The application on these settings as its own pages reach it: every form from this
    origin, and no redirect followed."""
    return TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN)


def signed_in(client: TestClient, passphrase: str) -> None:
    """Sign in on this client with a passphrase that must open the door."""
    came_in = client.post("/sign-in", data={"passphrase": passphrase})
    assert came_in.status_code == 303, came_in.text


def as_a_browser_sends(form: Mapping[str, str]) -> dict[str, str]:
    """A form as a browser submits it: every line break in a name or a value, a line feed or
    a carriage return alone, sent as a carriage return and a line feed."""

    def crlf(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

    return {crlf(name): crlf(value) for name, value in form.items()}


class PathAsServed:
    """The path as a real server hands it to the application: the raw path, its escapes
    undone once.

    The test client undoes them twice: the path it takes from its request is
    already decoded, and it decodes that again. Nothing shows for any id but
    one that holds an escaped percent sign, where the second pass turns
    ``unit%2F3`` into ``unit/3``, which is another assignment. Uvicorn
    decodes once. A test about such an id puts this in front of the
    application, so what reaches the routes is what reaches them at home.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "path": unquote(scope["raw_path"].decode("ascii"))}
        await self.app(scope, receive, send)


def as_served(client: TestClient) -> TestClient:
    """The same client over the same application, with paths decoded as a server decodes
    them. Called before the client is entered, while the application can still be wrapped."""
    client.app.add_middleware(PathAsServed)  # type: ignore[attr-defined]
    return client


def with_clock(client: TestClient, clock: Clock) -> None:
    """Give a running application another clock, as a day turning over does."""
    state = state_of(client)
    setattr(client.app.state, STATE_ATTRIBUTE, dataclasses.replace(state, clock=clock))  # type: ignore[attr-defined]


class SetClock:
    """A clock whose household day is whatever the test last set."""

    def __init__(self, day: date, at: datetime) -> None:
        self.day = day
        self.at = at
        self._zone = ZoneInfo(FIXTURE_TIMEZONE)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    def now(self) -> datetime:
        return self.at

    def today(self) -> date:
        return self.day


class ReportsWhileAsked[T: BaseModel]:
    """A model callable whose first answer comes only after her Done has landed, as a save
    does that arrives while the call is pending. It takes the decision lock for the save,
    as her page's route does, so a run that held the lock through the call would never
    answer."""

    def __init__(self, state: ApplicationState, *answers: T) -> None:
        self.state = state
        self.answers = list(answers)
        self.briefs: list[list[BaseMessage]] = []
        self.lock_was_free = False
        self.saved: object = None

    async def __call__(self, messages: Sequence[BaseMessage]) -> ModelAnswer[T]:
        self.briefs.append(list(messages))
        if self.saved is None:
            self.lock_was_free = not self.state.decision_lock.locked()
            async with self.state.decision_lock:
                self.saved = self.state.project_state.report_status(
                    ESSAY_ID,
                    "done",
                    None,
                    expected_head=None,
                    now=self.state.clock.now(),
                    today=self.state.clock.today(),
                )
        return ok(self.answers.pop(0))


def card_for(page: str, assignment_id: str) -> str:
    """One card or list entry, whole: from its id to the next card's, or the page's end."""
    start = page.index(f'id="{assignment_anchor(assignment_id)}"')
    following = page.find('id="assignment-', start + 1)
    return page[start:] if following < 0 else page[start:following]


def hidden(html: str, name: str) -> str:
    """The value of the hidden field ``name`` in a piece of a page."""
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match is not None, name
    return match.group(1)


def main_of(page: str) -> str:
    """The page's main part, where every fact and control of the page is."""
    return page.split('<main id="main">', 1)[1].split("</main>", 1)[0]


def words(html: str) -> str:
    """A piece of a page as the words it reads, with tags dropped and entities undone."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split()).replace(" .", ".")


def lands_on(page: str, address: str) -> str:
    """The opening tag of the element a browser lands on for an address: the element whose
    id is the fragment as written, or failing that the fragment with its escapes undone,
    which is the order a browser tries them in. Empty when the fragment names nothing."""
    fragment = urlsplit(address).fragment
    tags = {
        unescape(found.group(1)): found.group(0)
        for found in re.finditer(r'<\w+\b[^>]*?\sid="([^"]*)"[^>]*>', page)
    }
    return tags.get(fragment) or tags.get(unquote(fragment)) or ""


class _FormReader(HTMLParser):
    """Every form of a page with what each would send: inputs of every kind but the ones a
    browser leaves out, text areas, and lists, their values as a browser reads them. A
    list sends the option marked as selected, or its first option when none is."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, dict[str, str]]] = []
        self._fields: dict[str, str] | None = None
        self._area: str | None = None
        self._list: str | None = None
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag == "form":
            self._fields = {}
            self.forms.append((given.get("action") or "", self._fields))
        elif self._fields is None or "disabled" in given:
            return
        elif tag == "option":
            if self._list is not None and ("selected" in given or self._list not in self._fields):
                self._fields[self._list] = given.get("value") or ""
        elif not given.get("name"):
            return
        elif tag == "select":
            self._list = str(given["name"])
        elif tag == "input" and (
            given.get("type") not in ("radio", "checkbox", "submit") or "checked" in given
        ):
            self._fields[str(given["name"])] = given.get("value") or ""
        elif tag == "textarea":
            self._area = str(given["name"])
            self._fields[self._area] = ""

    def handle_data(self, data: str) -> None:
        if self._area is not None and self._fields is not None:
            self._fields[self._area] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea":
            self._area = None
        elif tag == "select":
            self._list = None
        elif tag == "form":
            self._fields = None


class _Names(HTMLParser):
    """Each button, link and summary of a page, or each element of ``named``: the words it
    shows, and the name a screen reader or voice control uses, which is its label when it
    has one and else its words, visually hidden ones included. Visually hidden words are
    laid out apart from the words beside them, so a browser's name sets them off with a
    space: hidden words that start with punctuation read with a space before it."""

    def __init__(self, page: str, named: tuple[str, ...] = ("button", "a", "summary")) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[tuple[str, str]] = []
        self._named = named
        self._open: list[tuple[str, str | None, list[str], list[str]]] = []
        self._hidden = 0
        self._spans: list[bool] = []
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag in self._named:
            self._open.append((tag, given.get("aria-label"), [], []))
        elif tag == "span":
            hides = "visually-hidden" in (given.get("class") or "").split()
            self._spans.append(hides)
            self._hidden += hides
            if hides:
                self._set_off()

    def handle_data(self, data: str) -> None:
        for _, _, shown, heard in self._open:
            heard.append(data)
            if not self._hidden:
                shown.append(data)

    def _set_off(self) -> None:
        for _, _, _, heard in self._open:
            heard.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._spans:
            hid = self._spans.pop()
            self._hidden -= hid
            if hid:
                self._set_off()
        elif self._open and self._open[-1][0] == tag:
            _, label, shown, heard = self._open.pop()
            words = " ".join("".join(shown).split())
            name = " ".join((label if label is not None else "".join(heard)).split())
            if words:
                self.controls.append((words, name))


def control_names(page: str) -> list[tuple[str, str]]:
    """Each button, link and summary of a page as (the words it shows, its name)."""
    return _Names(page).controls


def field_names(page: str) -> list[tuple[str, str]]:
    """Each legend and label of a page as (the words it shows, the name it gives its group
    or field)."""
    return _Names(page, ("legend", "label")).controls


def names_without_their_words(page: str) -> list[tuple[str, str]]:
    """The controls whose name leaves out the words they show, as (shown, name). WCAG 2.5.3
    asks that a control's name contain its visible label."""
    return [
        (words, name)
        for words, name in _Names(page).controls
        if words.casefold() not in name.casefold()
    ]


def names_not_led_by_their_words(page: str) -> list[tuple[str, str]]:
    """The controls whose name doesn't start with the words they show: Blossom's convention,
    which is stricter than WCAG, so a name reads the way the control looks."""
    return [
        (words, name)
        for words, name in _Names(page).controls
        if not name.casefold().startswith(words.casefold())
    ]


def whole_form(html: str, action: str) -> dict[str, str]:
    """Everything the one form with this action would send with no button pressed, hidden
    and visible alike, read the way a browser reads the page."""
    found = [fields for where, fields in _FormReader(html).forms if where == action]
    assert len(found) == 1, (action, len(found))
    return dict(found[0])


def form_fields(html: str, action: str) -> dict[str, str]:
    """The hidden fields of the form with this action, as a browser would send them back:
    each value read out of its attribute, so what the page escaped arrives as it was."""
    start = html.index(f'action="{action}"')
    form = html[start : html.index("</form>", start)]
    found = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
    return {name: unescape(value) for name, value in found}


def report(client: TestClient, assignment_id: str, status: str, note: str = "", **more: str) -> str:
    """Send her update from the card as it stands on the week ``week`` names, the fixture
    week unless it names another, and return the address it goes back to."""
    week = more.pop("week", FIXTURE_WEEK)
    page = client.get(
        HER_PAGE, params={"week": week, "change": assignment_id}, headers=PAGE_HEADERS
    ).text
    head = hidden(card_for(page, assignment_id), "expected_report_id")
    answer = client.post(
        f"/student/actions/assignments/{assignment_id}/report",
        data={"status": status, "note": note, "expected_report_id": head, "week": week, **more},
    )
    assert answer.status_code == 303, (answer.status_code, answer.text[:400])
    return answer.headers["location"]


def school_said(status: str, channel: SourceChannel, day: date) -> StatusReport:
    """One report of the school's, dated by the day it was pasted."""
    return StatusReport(
        status=status,
        channel=channel,
        reported_on=day,
        dated_by=PASTE_DAY,
        observed_at=datetime(2026, 8, 19, 22, 30, tzinfo=UTC),
    )


# ------------------------------------------------------------- her week's cards, press by press

NOW = datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
"""The pinned day's evening, when the updates and choices these tests keep are made."""
READING_LOG_ID = "assignment-reading-log"
"""The fixture's reading log, given out this week and due later: a row under the cards."""
QUIZ_ID = "assignment-vocabulary-quiz"
SYLLABUS_ID = "assignment-signed-syllabus"
ESCAPED = "set/2 it's #1? é"
"""An assignment id that holds a slash, an apostrophe, a hash, a question mark, and a letter a
browser escapes, so every address and place on a page has to escape it too."""
LATER_WEEK = "2026-08-24"
"""The Monday of the week after the fixture week."""
DETAILS = f"/student/assignments/{ESSAY_ID}"
REPORT = f"/student/actions/assignments/{ESSAY_ID}/report"
UNDO = f"/student/actions/assignments/{ESSAY_ID}/undo-report"
"""The essay's details, and the routes her update on it and its Undo go through."""
NONE_APPLIES = '<p class="source">No instruction from the school applies now.</p>'
KEY = "not-a-key-and-never-sent"
"""A key that lets the plan button show for scripted graphs; nothing is ever sent with it."""


@contextmanager
def reading(
    reader: str, tmp_path: pathlib.Path, *, graphs: Callable[..., PlanGraphs] | None = None
) -> Iterator[TestClient]:
    """The pinned day as one reader has it: her device or a parent's, signed in, or the
    household with the sign-in off. ``graphs`` gives the household a key and scripted
    graphs, so the plan button is offered and answers without a model."""
    if reader == "sign-in off":
        settings = fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(), **({"ANTHROPIC_API_KEY": KEY} if graphs else {})
        )
    else:
        settings = signed_in_household(tmp_path)
        if graphs:
            settings = dataclasses.replace(settings, anthropic_api_key=KEY)
    app = create_app(settings)
    if graphs:
        app.dependency_overrides[plan_graphs] = graphs
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        if reader != "sign-in off":
            signed_in(client, HERS if reader == "her" else THEIRS)
        yield client


def reported(store: ProjectStateStore, status: str, assignment_id: str = ESSAY_ID) -> None:
    """Her update, saved over whatever stands, as a card showing the latest saves it."""
    events = store.student_reports(assignment_id)
    store.report_status(
        assignment_id,
        status,  # type: ignore[arg-type]
        None,
        expected_head=events[-1].report_id if events else None,
        now=NOW,
        today=PLAN_DATE,
    )


def due(name: str, title: str, on: date, *, assigned: date | None = None) -> Assignment:
    """Homework in Geometry due on ``on``, given out on ``assigned`` when it says."""
    return Assignment(
        assignment_id=name,
        course="Geometry",
        title=title,
        due_date=on,
        dependencies=[],
        reported_submission_status="not_started",
        assigned_on=assigned,
        kind=AssignmentKind.HOMEWORK,
    )


def page_of(client: TestClient, **params: str) -> str:
    """Her week, as the address with these values shows it."""
    return client.get(HER_PAGE, params=params, headers=PAGE_HEADERS).text


def week_card(client: TestClient, assignment_id: str = ESSAY_ID, **params: str) -> str:
    """One card of her week with its update form open, as Change opens it."""
    return card_for(page_of(client, change=assignment_id, **params), assignment_id)


def save(
    client: TestClient,
    card: str,
    status: str | None,
    note: str = "",
    assignment_id: str = ESSAY_ID,
    **over: str,
) -> Answer:
    """Her update sent from a card as the card shows it, with fields in ``over`` written over
    the ones the form carries."""
    action = f"/student/actions/assignments/{assignment_id}/report"
    fields = form_fields(card, action)
    chosen = {} if status is None else {"status": status}
    return client.post(
        action, data={**fields, **chosen, "note": note, **over}, headers=PAGE_HEADERS
    )


def after(client: TestClient, answer: Answer) -> Answer:
    """The page a redirect sends her to."""
    assert answer.status_code == 303, answer.text[:300]
    return client.get(answer.headers["location"], headers=PAGE_HEADERS)


def refuse_writes(client: TestClient) -> None:
    """Make the file refuse every new update of hers, as a failed write does."""
    store = store_of(client)
    store._connection.execute(
        "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    store._connection.commit()


# Her presses on the essay's card, each answered on her week: a refusal, a save, or an Undo.


def conflict(client: TestClient) -> Answer:
    card = week_card(client)
    reported(store_of(client), "not_yet")
    return save(client, card, "done", "Mine.")


def stale_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(page_of(client), ESSAY_ID)
    reported(store_of(client), "not_yet")
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


def already_undone(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(page_of(client), ESSAY_ID)
    assert client.post(UNDO, data=form_fields(card, UNDO)).status_code == 303
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


def malformed(client: TestClient) -> Answer:
    fields = form_fields(week_card(client), REPORT)
    return client.post(
        REPORT, data={**fields, "status": "done", "note": ["one", "two"]}, headers=PAGE_HEADERS
    )


def bad_return(client: TestClient) -> Answer:
    return save(client, week_card(client), "done", week="not a day")


def not_this_cards(client: TestClient) -> Answer:
    return save(client, week_card(client), "done", expected_report_id="x" * 201)


def failed_write(client: TestClient) -> Answer:
    card = week_card(client)
    refuse_writes(client)
    return save(client, card, "not_yet", "kept")


def failed_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(page_of(client), ESSAY_ID)
    refuse_writes(client)
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


def no_choice(client: TestClient) -> Answer:
    return save(client, week_card(client), None, "kept")


def note_too_long(client: TestClient) -> Answer:
    return save(client, week_card(client), "done", "x" * 501)


def saved(client: TestClient) -> Answer:
    return save(client, week_card(client), "done")


def saved_again(client: TestClient) -> Answer:
    card = week_card(client)
    assert save(client, card, "done").status_code == 303
    return save(client, card, "done")


def undone(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(page_of(client), ESSAY_ID)
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


# ------------------------------------------------------------- her reports, in a store alone

SAID_AT = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
SAID_ON = date(2026, 9, 16)
"""Half past four in the afternoon in the fixtures' zone, on the day the report is made."""
PRACTICE = "assignment-practice"
PRACTICE_LOG = "assignment-log"


def a_row(assignment_id: str, title: str) -> Assignment:
    return Assignment(
        assignment_id=assignment_id,
        course="Math",
        title=title,
        due_date=date(2026, 9, 18),
        dependencies=[],
        reported_submission_status="not_started",
        kind=AssignmentKind.HOMEWORK,
    )


def practice_store(path: pathlib.Path) -> ProjectStateStore:
    """A store of two assignments, the weekly practice and the reading log, and nothing
    said. It opens and writes the file it is given, so it is for a run the state guard
    stands behind."""
    require_protection("practice_store")
    store = ProjectStateStore.open(path, fixture_clock())
    store.put_on_record(
        [a_row(PRACTICE, "Weekly practice"), a_row(PRACTICE_LOG, "Reading log")], {}
    )
    return store


def school_missing(day: date) -> StatusReport:
    """The school's email saying missing, as of ``day``."""
    return StatusReport(
        status="missing",
        channel=SourceChannel.EMAIL,
        reported_on=day,
        dated_by="the day it was pasted",
        observed_at=SAID_AT,
    )


def status_of(store: ProjectStateStore, assignment_id: str) -> AssignmentStatus:
    return statuses_for(store, [assignment_id])[assignment_id]


# ------------------------------------------------------------- a composition with every shape

SYLLABUS = Assignment(
    assignment_id="assignment-signed-syllabus",
    course="Geometry",
    title="Syllabus, signed",
    due_date=None,
    dependencies=[],
    reported_submission_status="not_started",
)
NAMESAKE_ESSAY = Assignment(
    assignment_id="assignment-canal-essay-english",
    course="English",
    title="Canal Era comparison essay",
    due_date=date(2026, 8, 21),
    dependencies=[],
    reported_submission_status="not_started",
)


def plan_block(
    assignment_id: str, start: str, end: str, why: str = "while it is fresh"
) -> PlanBlock:
    return PlanBlock(
        assignment_id=assignment_id,
        starts_at=time.fromisoformat(start),
        ends_at=time.fromisoformat(end),
        rationale=why,
    )


def two_sittings() -> DailyPlan:
    """The essay in two sittings, its namesake in one, the problem set put off, and the
    undated syllabus put off too."""
    return DailyPlan(
        plan_date=PLAN_DATE,
        blocks=[
            plan_block(ESSAY.assignment_id, "18:00", "18:30", "the second half\nafter a break"),
            plan_block(ESSAY.assignment_id, "16:30", "17:00", "the outline first"),
            plan_block(NAMESAKE_ESSAY.assignment_id, "17:15", "17:45"),
        ],
        deferred=[
            Deferral(assignment_id=PROBLEM_SET.assignment_id, reason="not due until Monday"),
            Deferral(assignment_id=SYLLABUS.assignment_id, reason="ask what the date is"),
        ],
    )


SITTINGS_WINDOW = [ESSAY, NAMESAKE_ESSAY, PROBLEM_SET, SYLLABUS]


def contested() -> list[Noticing]:
    """The portal gives the problem set another date than the record's."""
    return [
        Noticing(
            assignment_id=PROBLEM_SET.assignment_id,
            expected=PROBLEM_SET.due_date,
            observed=("LMS says 2026-08-26",),
            spoken=("the school portal says August 26",),
            observed_dates=(date(2026, 8, 26),),
            verdict=Verdict.CONTRADICTED,
        )
    ]


def dissent() -> CriticVerdict:
    """One criterion judged and failed, the rest not considered."""
    return CriticVerdict(
        findings=[finding(Judgment.FAILS, Criterion.ORDER, "the hard one\tcomes  late")]
    )


def composed_plan(plan: DailyPlan | None = None, **over: object) -> Composition:
    plan = plan or two_sittings()
    given: dict[str, object] = {
        "draft_id": "draft:plan:2026-08-19:abc12345",
        "plan": plan,
        "assignments": SITTINGS_WINDOW,
        "verification": check_plan(
            plan, due_in_window=SITTINGS_WINDOW, zone=ZONE, requested_evening=PLAN_DATE
        ),
        "verdict": dissent(),
        "settled": False,
        "noticings": contested(),
        "confidence": {ESSAY.assignment_id: SourceConfidence.SOURCES_DISAGREE},
        "too_much": True,
        "budget_minutes": 45,
    }
    given.update(over)
    return compose(**given)  # type: ignore[arg-type]


# ------------------------------------------------------------- a plan to follow through the pages

NAMESAKE_COURSE = "English"


def walkthrough_plan(namesake_id: str) -> DailyPlan:
    """The fixture week's plan with the essay in two sittings and its namesake, another
    course's assignment of the same title, put off: two rows for one id, a row for a
    neighbor with the same words, and work put off beside them."""
    whole = fixture_week_plan()
    return whole.model_copy(
        update={
            "blocks": [
                PlanBlock(
                    assignment_id=ESSAY_ID,
                    starts_at=time(16, 30),
                    ends_at=time(17, 0),
                    rationale="the outline first, while she is fresh",
                ),
                PlanBlock(
                    assignment_id="assignment-science-fair-proposal",
                    starts_at=time(17, 0),
                    ends_at=time(17, 30),
                    rationale="nobody has confirmed this date, so it gets done tonight",
                ),
                PlanBlock(
                    assignment_id=ESSAY_ID,
                    starts_at=time(18, 0),
                    ends_at=time(18, 30),
                    rationale="the first two paragraphs after a break",
                ),
            ],
            "deferred": [
                *whole.deferred,
                Deferral(assignment_id=namesake_id, reason="the other essay comes first"),
            ],
        }
    )


def walkthrough(client: TestClient) -> str:
    """Put the namesake on record, script the walkthrough plan for the next run, and return
    the namesake's id. Nothing is planned yet, and the shared fixtures are left as they are."""
    entered = client.post(
        "/parent/inbox/keep",
        data={"course": NAMESAKE_COURSE, "title": ESSAY_TITLE, "due_date": "2026-08-21"},
    )
    assert entered.status_code == 303, entered.text[:300]
    namesake = next(
        item.assignment_id
        for item in state_of(client).project_state.all_assignments()
        if item.title == ESSAY_TITLE and item.course == NAMESAKE_COURSE
    )
    client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
        lambda: [walkthrough_plan(namesake)], lambda: [accepting()]
    )
    return namesake


def planned(client: TestClient) -> DraftRecord:
    """Make today's plan from her page and return its record."""
    made = client.post("/student/actions/plan")
    assert made.status_code == 303, made.text[:300]
    record = state_of(client).drafts.latest_for(PLAN_DATE)
    assert record is not None
    return record


def to_details(assignment_id: str) -> str:
    """The address a row of today's plan links to for one assignment."""
    return details_href(assignment_id, return_to="today")


def plan_on(page: str, record: DraftRecord) -> str:
    """One plan's container on a page, whole: from its anchor to the end of its original
    text fold, or to the end of its text reading."""
    start = page.index(f'id="{anchor_for(record.draft_id)}"')
    ends = [
        found
        for found in (page.find(mark, start) for mark in ("</pre>", "</section>", "</article>"))
        if found >= 0
    ]
    return page[start : min(ends)] if ends else page[start:]


NOTE_AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
NOTE_DAY = date(2026, 9, 14)


def homework_from_a_note(
    store: ProjectStateStore,
    *,
    by: str = STUDENT,
    channel: SourceChannel = SourceChannel.STUDENT_REPORT,
    course: str = "Geometry",
    title: str = "Questions 4-8",
    due_date: date | None = None,
    note: str | None = None,
    choice: str = "new",
) -> str:
    """A homework note promoted to new homework under this course and title, with her due
    date and note when given, as a student or a parent does it; the new assignment's id.
    ``separate`` keeps it apart from homework of the same class and title on record."""
    name = new_capture_id()
    made = store.create_capture(
        name,
        "Geometry questions 4-8, heard from a classmate",
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(made, CaptureCreated)
    given = CaptureDetails(
        course=course, title=title, kind="HOMEWORK", due_date=due_date, note=note
    )
    done = store.promote_capture(
        name,
        given,
        expected_revision=1,
        basis=candidate_basis(candidate_readings(store, given)),
        candidates=reader(store),
        choice=choice,  # type: ignore[arg-type]
        authored_by=by,  # type: ignore[arg-type]
        channel=channel,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(done, CapturePromoted)
    return done.assignment_id


def waiting_note(
    store: ProjectStateStore,
    *,
    course: str,
    title: str,
    text: str,
    due_date: date | None = None,
) -> str:
    """A homework note of hers that waits, given this class, title, and day; the note's
    id."""
    name = new_capture_id()
    made = store.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(made, CaptureCreated)
    given = CaptureDetails(course=course, title=title, kind="HOMEWORK", due_date=due_date)
    clarified = store.clarify_capture(
        name,
        given,
        expected_revision=1,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(clarified, CaptureChanged)
    return name


NOTE_DETAILS = CaptureDetails(course="Geometry", title="Questions 4-8", kind="HOMEWORK")
"""A class and a title for a note that is to become homework of its own."""


def promote_note(store: ProjectStateStore, name: str, revision: int = 1) -> str:
    """Add a waiting note to homework as new homework, as she does it; the new assignment's
    id."""
    done = store.promote_capture(
        name,
        NOTE_DETAILS,
        expected_revision=revision,
        basis=candidate_basis(candidate_readings(store, NOTE_DETAILS)),
        candidates=reader(store),
        choice="new",
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(done, CapturePromoted), done
    return done.assignment_id


def link_note(store: ProjectStateStore, name: str, revision: int = 1) -> Assignment:
    """Join a waiting note to homework the school lists, put on record for it; that
    homework."""
    target = Assignment(
        assignment_id=identity("Humanities", "Summer reading log", "2026-09-25"),
        course="Humanities",
        title="Summer reading log",
        due_date=date(2026, 9, 25),
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
    )
    store.put_on_record([target], {})
    done = store.link_capture(
        name,
        target=target.assignment_id,
        expected_revision=revision,
        basis=candidate_basis(readings_for(store, [target])),
        shown=row_reader(store),
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(done, CapturePromoted), done
    return target


def unlink_note(store: ProjectStateStore, name: str, revision: int = 1) -> None:
    """Join a waiting note to homework, then take it off again, so it waits once more."""
    target = link_note(store, name, revision)
    done = store.unlink_capture(
        name,
        expected_revision=revision + 1,
        leaving=target.assignment_id,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=NOTE_AT,
        today=NOTE_DAY,
    )
    assert isinstance(done, CaptureUnlinked), done


# ------------------------------------------------------------- a record that fails to read


def quiet_client(settings: Settings) -> TestClient:
    """The application on these settings as its pages reach it, with a failure no page
    catches answered as a server answers it, a bare 500, rather than raised into the test."""
    return TestClient(
        create_app(settings),
        follow_redirects=False,
        headers=SAME_ORIGIN,
        raise_server_exceptions=False,
    )


def refusing(
    kind: type[Exception] = sqlite3.OperationalError, marks: list[str] | None = None
) -> Callable[..., None]:
    """A store call that fails the way a file the store can't read fails, ``kind`` raised with
    the message a busy file gives; the moment it fails is marked in ``marks`` when given."""

    def refused(*_: object, **__: object) -> None:
        if marks is not None:
            marks.append("FAILED HERE")
        msg = "database is locked"
        raise kind(msg)

    return refused


def connections_of(state: ApplicationState) -> list[sqlite3.Connection]:
    """The four connections a page reads the household's file through: the record, the
    drafts, her signals, and her requests for help."""
    return [
        state.project_state._connection,
        state.drafts._connection,
        state.workload_signals._connection,
        state.help_requests._connection,
    ]


class Statements:
    """Every statement the four connections run, in the order they run, with the moment a
    store call was made to fail marked among them."""

    def __init__(self, state: ApplicationState) -> None:
        self.seen: list[str] = []
        self.connections = connections_of(state)

    def __enter__(self) -> list[str]:
        for connection in self.connections:
            connection.set_trace_callback(self.seen.append)
        return self.seen

    def __exit__(self, *_: object) -> None:
        for connection in self.connections:
            connection.set_trace_callback(None)


def after_the_failure(seen: list[str]) -> list[str]:
    """What ran after the marked failure, leaving out the rollback that ends the failed
    read itself."""
    at = seen.index("FAILED HERE")
    return [line for line in seen[at + 1 :] if line.strip().upper() != "ROLLBACK"]


def every_row(path: pathlib.Path) -> list[str]:
    """Every table of the household's file and every row in it, as the statements that would
    make them again, read through a connection of its own, so a press can be shown to have
    written nothing."""
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


class HeldByAnother:
    """The household's file held by another program: a second connection that has begun an
    exclusive transaction, so every connection of the application waits for it and then
    fails. With ``reading`` it holds a read open instead, which lets reads through and
    refuses a commit."""

    def __init__(self, path: pathlib.Path, *, reading: bool = False) -> None:
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.reading = reading

    def __enter__(self) -> None:
        if self.reading:
            self.connection.execute("BEGIN")
            self.connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        else:
            self.connection.execute("BEGIN EXCLUSIVE")

    def __exit__(self, *_: object) -> None:
        self.connection.execute("ROLLBACK")
        self.connection.close()


def the_alert(page: str) -> str:
    """The words of the one focused explanation on a page that reads no store, links and
    all, or empty when there is none."""
    found = re.search(
        r'<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>(.*?)</p>',
        page,
        re.S,
    )
    return "" if found is None else words(found.group(1))


def database_of(client: TestClient) -> pathlib.Path:
    """The household's file the application on this client keeps its record in."""
    return pathlib.Path(state_of(client).settings.database_path)


def household_client(reader: str, tmp_path: pathlib.Path) -> TestClient:
    """The fixture week for ``reader``, its files under ``tmp_path``: the sign-in on for her
    and a parent, who sign in with ``sign_in_as``, and off for ``open``. Entered by the
    caller. For a run the state guard stands behind."""
    require_protection("household_client")
    if reader != "open":
        return client_for(signed_in_household(tmp_path))
    return client_for(
        fixture_settings(
            BLOSSOM_TODAY=PLAN_DATE.isoformat(),
            BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
            BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
            BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
        )
    )


def sign_in_as(client: TestClient, reader: str) -> None:
    """Sign her or a parent in on this client; the household with the sign-in off needs
    nothing."""
    if reader == "her":
        signed_in(client, HERS)
    elif reader == "parent":
        signed_in(client, THEIRS)


def ways_back_of(main: str) -> list[tuple[str, str]]:
    """The ways back a page offers, in order, as href and words."""
    found = re.findall(r'<p class="return"><a href="([^"]*)">([^<]*)</a></p>', main)
    return [(href, label) for href, label in found]


STORE_FREE_NEVER = (
    "Internal Server Error",
    "is saved",
    "Already saved",
    "is undone",
    "shows what stands",
    "as it stands now",
    "Here it is",
    "Review it",
    "Choose and save again",
    "choose and save from here",
    "Archive it instead",
    "Go to the",
    "Press again from the list",
    "Check the details and add it again",
    "choose again",
)
"""What no page that reads no store says: a save, or a pointer to a page it does not show."""


def store_free_page(
    answer: Answer, *, status: int, heading: str, alert: str, alert_id: str = "problem-summary"
) -> str:
    """What every page that reads no store holds and never holds: its status, its heading,
    one focused explanation with these words, no control, and no pointer to a page it does
    not show. Its main part."""
    assert answer.status_code == status, (answer.status_code, answer.text[:600])
    main = main_of(answer.text)
    assert f"<h1>{heading}</h1>" in main, main[:600]
    found = re.search(
        rf'<p class="problem" role="alert" id="{alert_id}" tabindex="-1" autofocus>(.*?)</p>',
        main,
        re.S,
    )
    assert found is not None, main[:600]
    assert words(found.group(1)) == alert, words(found.group(1))
    assert answer.text.count("autofocus") == 1
    assert 'tabindex="' not in main.replace('tabindex="-1"', "")
    assert "<form" not in main
    assert "<button" not in main
    for never in STORE_FREE_NEVER:
        assert never not in main, never
    return main


STORES = ("project_state", "drafts", "workload_signals", "help_requests")
"""The four stores a page reads the household's file through."""


def spy_on_stores(
    monkeypatch: pytest.MonkeyPatch, state: ApplicationState, calls: list[str]
) -> None:
    """Mark in ``calls`` every public method of the four stores as it is called, by store and
    method, so a page can be shown to call none of them after a marked failure."""
    for store_name in STORES:
        store = getattr(state, store_name)
        for name in dir(type(store)):
            if name.startswith("_") or not callable(getattr(store, name)):
                continue
            real = getattr(store, name)

            def counted(
                *given: object,
                real: Callable[..., object] = real,
                called: str = f"{store_name}.{name}",
                **named: object,
            ) -> object:
                calls.append(called)
                return real(*given, **named)

            monkeypatch.setattr(store, name, counted)


def rules_named(selector: str) -> list[str]:
    """The declarations of every rule of the stylesheet whose selector list holds
    ``selector`` itself."""
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        inside
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if selector in (part.strip() for part in head.split(","))
    ]


def went_to(answer: Answer) -> str:
    """Where a press sent the browser on to: the answer is a 303, and this is its address."""
    assert answer.status_code == 303, (answer.status_code, answer.text[:400])
    return answer.headers["location"]


def help_row(page: str, request_id: str) -> str:
    """One request's row in Help, whole."""
    start = page.index(f'<li class="help-request" id="help-{request_id}" tabindex="-1">')
    return page[start : page.index("</li>", start)]


# ------------------------------------------------------------- the family page's rows, and a check


def row_for(page: str, assignment_id: str) -> str:
    """One row of the assignment updates, whole: from its id to the next row's, or the
    section's end."""
    start = page.index(f'id="update-{assignment_id}"')
    ends = [
        found
        for found in (page.find('id="update-', start + 1), page.find("</section>", start))
        if found >= 0
    ]
    return page[start : min(ends)] if ends else page[start:]


def family_page(client: TestClient) -> str:
    return client.get("/parent", headers=PAGE_HEADERS).text


def a_discrepancy(client: TestClient) -> None:
    """The school's email says the essay is missing; she reports it done."""
    told = client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL})
    assert told.status_code == 303
    report(client, ESSAY_ID, "done", "Handed in Tuesday.")


def action_of(row: str, ending: str) -> str:
    """The address one of a row's two check forms is sent to, as the page wrote it."""
    found = re.search(rf'<form method="post" action="([^"]*/{ending})"', row)
    assert found is not None, ending
    return unescape(found.group(1))


def mark(client: TestClient, assignment_id: str, note: str = "") -> Answer:
    """Mark the row checked from the family page as it stands, with the fields it carries."""
    row = row_for(family_page(client), assignment_id)
    return client.post(
        action_of(row, "mark"),
        data={
            "basis": hidden(row, "basis"),
            "expected_check_id": hidden(row, "expected_check_id"),
            "note": note,
        },
        headers=PAGE_HEADERS,
    )


# ------------------------------------------------------------- a row the file holds damaged

NOT_UTF8 = "text bytes that are not UTF-8"
"""What ``spoil`` writes as a word followed by two bytes no UTF-8 decoder takes, which no
bound value can hold: the word, ZEBRA, is there to be looked for in a log."""


def spoil(
    store: ProjectStateStore,
    table: str,
    column: str,
    value: str,
    assignment_id: str = ESSAY_ID,
) -> list[tuple[object, ...]]:
    """Write ``value`` into one column of every row an assignment has in ``table`` with
    plain SQL, as a damaged file holds it, and hand back the table as it is stored."""
    if value == NOT_UTF8:
        store._connection.execute(
            f"UPDATE {table} SET {column} = CAST(X'5A45425241FF80' AS TEXT) "  # noqa: S608
            "WHERE assignment_id = ?",
            (assignment_id,),
        )
    else:
        store._connection.execute(
            f"UPDATE {table} SET {column} = ? WHERE assignment_id = ?",  # noqa: S608
            (value, assignment_id),
        )
    store._connection.commit()
    return as_stored(store, table)


def as_stored(store: ProjectStateStore, table: str) -> list[tuple[object, ...]]:
    """Every row of ``table`` in the file's order, each text column as its stored bytes,
    so a row that is not UTF-8 can be read back and compared too."""
    connection = store._connection
    kept = connection.text_factory
    connection.text_factory = bytes
    try:
        return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
    finally:
        connection.text_factory = kept


# ------------------------------------------------------------- a page and its stylesheet


class UnreadCss(AssertionError):
    """A part of a stylesheet the resolver below cannot evaluate. Raised, never guessed at,
    since a guess either way could pass a page that some screen draws differently."""


@dataclasses.dataclass(frozen=True)
class View:
    """One reader's screen: its width, the browser's text size (what a media query's `rem`
    is measured in), the root element's text size (what a property's `rem` is measured in),
    and whether it asks for less motion."""

    width: int
    browser_text: int = 16
    root_text: int = 16
    reduced_motion: bool = False


@dataclasses.dataclass(eq=False)
class Element:
    """An element of a page, the same only as itself."""

    tag: str
    attributes: dict[str, str]
    parent: "Element | None"
    text: str = ""
    position: int = 0
    """Its place among its parent's elements, from 1; 0 where it is not known."""

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attributes.get("class", "").split())

    def ancestors(self) -> list["Element"]:
        found, above = [], self.parent
        while above is not None:
            found.append(above)
            above = above.parent
        return found

    def within(self, test: "Element") -> bool:
        return self is test or any(above is test for above in self.ancestors())


class Elements(HTMLParser):
    """Every element of a page, each with the chain of elements above it."""

    VOID = frozenset({"input", "br", "img", "meta", "link", "hr", "source"})

    def __init__(self) -> None:
        super().__init__()
        self.open: list[Element] = []
        self.found: list[Element] = []
        self.children: dict[int, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.open[-1] if self.open else None
        place = self.children[id(parent)] = self.children.get(id(parent), 0) + 1
        element = Element(tag, {name: value or "" for name, value in attrs}, parent, "", place)
        self.found.append(element)
        if tag not in self.VOID:
            self.open.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index].tag == tag:
                del self.open[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.open:
            self.open[-1].text += data


def elements_of(page: str) -> list[Element]:
    parser = Elements()
    parser.feed(page)
    return parser.found


NEVER_ON = frozenset({"hover", "focus", "focus-visible", "focus-within", "active"})
"""States a page at rest is never in."""
LINK_STATES = frozenset({"link", "visited"})


@dataclasses.dataclass(frozen=True)
class Compound:
    tag: str | None = None
    ids: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str | None], ...] = ()
    negated: tuple["Compound", ...] = ()
    never: bool = False
    states: tuple[str, ...] = ()
    """Each `:root`, `:link`, `:visited` and `:first-child` it asks for, as written."""
    places: tuple[int, ...] = ()
    """Each place among its parent's elements that a `:first-child` or `:nth-child(n)` asks
    for."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        inner = [item.specificity for item in self.negated]
        return (
            len(self.ids) + sum(i[0] for i in inner),
            len(self.classes)
            + len(self.attributes)
            + len(self.states)
            + len(self.places)
            + sum(i[1] for i in inner),
            (1 if self.tag else 0) + sum(i[2] for i in inner),
        )

    def matches(self, element: Element, state: str = "link") -> bool:
        """Whether ``element`` is matched, a link being in ``state``. `:link` and `:visited`
        match only a link, an `a` or `area` with an `href`."""
        if self.never or ("root" in self.states and element.tag != "html"):
            return False
        if self.tag is not None and self.tag != element.tag:
            return False
        if any(element.attributes.get("id") != one for one in self.ids):
            return False
        if any(element.position != one for one in self.places):
            return False
        if not set(self.classes) <= element.classes:
            return False
        for name, wanted in self.attributes:
            if name not in element.attributes:
                return False
            if wanted is not None and element.attributes[name] != wanted:
                return False
        asked = {one for one in self.states if one in LINK_STATES}
        a_link = element.tag in ("a", "area") and "href" in element.attributes
        if asked and (not a_link or asked != {state}):
            return False
        return not any(item.matches(element, state) for item in self.negated)


PIECE = re.compile(
    r"""(?P<not>:not\((?P<inner>[^()]*)\))
      | (?P<id>\#[\w-]+)
      | (?P<class>\.[\w-]+)
      | (?P<attribute>\[(?P<name>[\w-]+)(?:=["']?(?P<value>[^"'\]]*)["']?)?\])
      | (?P<pseudo>::?[\w-]+(?:\([^()]*\))?)
      | (?P<tag>[a-zA-Z][\w-]*|\*)""",
    re.VERBOSE,
)


def compound(text: str) -> Compound:
    """One compound selector. A pseudo-element, or a state a page at rest is never in, never
    matches; a pseudo-class other than `:root`, `:link`, `:visited`, `:first-child`,
    `:nth-child()` of a number, and `:not()` is refused."""
    tag: str | None = None
    ids: list[str] = []
    classes: list[str] = []
    attributes: list[tuple[str, str | None]] = []
    negated: list[Compound] = []
    never = False
    states: list[str] = []
    places: list[int] = []
    position = 0
    while position < len(text):
        piece = PIECE.match(text, position)
        if piece is None:
            raise UnreadCss(text)
        position = piece.end()
        if piece["not"]:
            negated.append(compound(piece["inner"].strip()))
        elif piece["id"]:
            ids.append(piece["id"][1:])
        elif piece["class"]:
            classes.append(piece["class"][1:])
        elif piece["attribute"]:
            attributes.append((piece["name"].lower(), piece["value"]))
        elif piece["pseudo"]:
            name = piece["pseudo"].lstrip(":").lower()
            if piece["pseudo"].startswith("::") or name in NEVER_ON:
                never = True
            elif name in LINK_STATES | {"root"}:
                states.append(name)
            elif name == "first-child" or re.fullmatch(r"nth-child\(\s*\d+\s*\)", name):
                places.append(int(re.sub(r"\D", "", name) or 1))
            else:
                raise UnreadCss(text)
        elif piece["tag"] and piece["tag"] != "*":
            tag = piece["tag"].lower()
    return Compound(
        tag,
        tuple(ids),
        tuple(classes),
        tuple(attributes),
        tuple(negated),
        never,
        tuple(states),
        tuple(places),
    )


@dataclasses.dataclass(frozen=True)
class Selector:
    parts: tuple[tuple[str, Compound], ...]
    """Each compound with the combinator that joins it to the one before: a space or ``>``."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        each = [item.specificity for _, item in self.parts]
        return (sum(i[0] for i in each), sum(i[1] for i in each), sum(i[2] for i in each))

    def matches(self, element: Element, state: str = "link") -> bool:
        """Whether ``element`` is matched, itself a link in ``state`` and any link above it
        unvisited."""

        def climb(index: int, at: Element) -> bool:
            joiner, item = self.parts[index]
            if not item.matches(at, state if index == len(self.parts) - 1 else "link"):
                return False
            if index == 0:
                return True
            above = at.ancestors()
            reach = above[:1] if joiner == ">" else above
            return any(climb(index - 1, candidate) for candidate in reach)

        return climb(len(self.parts) - 1, element)


def selector(text: str) -> Selector:
    """One complex selector, joined by spaces and ``>``; a sibling combinator is refused, and
    so is what a browser drops: an empty selector, or a ``>`` with no compound on a side."""
    parts: list[tuple[str, Compound]] = []
    joiner = " "
    for piece in re.findall(r"[>+~]|[^\s>+~]+", unshielded(text).strip()):
        if piece in ("+", "~"):
            raise UnreadCss(text)
        if piece == ">":
            if joiner == ">" or not parts:
                raise UnreadCss(text)
            joiner = ">"
            continue
        parts.append((joiner, compound(piece)))
        joiner = " "
    if joiner == ">" or not parts:
        raise UnreadCss(text)
    return Selector(tuple(parts))


SHIELD = {";": "", "{": "", "}": "", ",": "", ":": ""}
"""Characters that end or split a part of a stylesheet, as they stand inside a string or an
unquoted ``url()``, where they are only text."""


def shielded(css: str) -> str:
    """``css`` with each comment a space, as a browser splits words at one, and each
    character of ``SHIELD`` inside a quoted string or an unquoted ``url()`` stood in for, so
    a ``/*`` or ``;`` there is read as text."""
    kept: list[str] = []
    index = 0
    while index < len(css):
        if css.startswith("/*", index):
            end = css.find("*/", index + 2)
            index = len(css) if end < 0 else end + 2
            kept.append(" ")
            continue
        quote = css[index] if css[index] in "\"'" else None
        url = (
            quote is None
            and css[index : index + 4].lower() == "url("
            and css[index + 4 :].lstrip()[:1] not in ("'", '"')
        )
        if quote is None and not url:
            kept.append(css[index])
            index += 1
            continue
        end, closing = (index + 4, ")") if url else (index + 1, quote)
        while end < len(css) and css[end] != closing:
            end += 2 if css[end] == "\\" else 1
        end = min(end + 1, len(css))
        kept.append("".join(SHIELD.get(character, character) for character in css[index:end]))
        index = end
    return "".join(kept)


def unshielded(text: str) -> str:
    for character, stand_in in SHIELD.items():
        text = text.replace(stand_in, character)
    return text


def blocks(css: str) -> list[tuple[str, str]]:
    """Each outermost block of ``css`` as what comes before its brace and what is inside."""
    found: list[tuple[str, str]] = []
    depth, after, opened = 0, 0, 0
    for index, character in enumerate(css):
        if character == "{":
            if depth == 0:
                opened = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                found.append((css[after:opened].strip(), css[opened + 1 : index]))
                after = index + 1
    return found


IMPORTANT = re.compile(r"\s*!\s*important$", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class StyleRule:
    """One style rule: its selector list as written, its declarations in source order (each
    a name, lowercase unless it is a custom property's, the value as written, and whether it
    is important), its place in the sheet, and the media condition it sits under, ``None``
    for one that holds everywhere."""

    selectors: str
    declarations: tuple[tuple[str, str, bool], ...]
    order: int
    media: str | None


def style_rules(css: str) -> list[StyleRule]:
    """Every style rule of ``css``, in source order. Font faces and keyframes hold no rules
    for elements and are passed over; a media query inside another, any other at-rule, and
    a rule nested inside another are refused."""
    found: list[StyleRule] = []

    def read(part: str, media: str | None) -> None:
        for before, inside in blocks(part):
            at = before.lower()
            if at.startswith(("@font-face", "@keyframes")):
                continue
            if at.startswith("@media"):
                if media is not None:
                    msg = f"a media query inside {media}"
                    raise UnreadCss(msg)
                read(inside, before[len("@media") :].strip())
                continue
            if before.startswith("@") or "{" in inside:
                raise UnreadCss(before)
            declared = []
            for line in inside.split(";"):
                name, _, value = line.partition(":")
                value, important = IMPORTANT.subn("", value.strip())
                name = name.strip()
                if value:
                    named = name if name.startswith("--") else name.lower()
                    declared.append((named, unshielded(value), important > 0))
            found.append(StyleRule(before, tuple(declared), len(found) + 1, media))

    read(shielded(css), None)
    return found


KEYWORDS = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})
"""The values any property takes, which name another value rather than give one."""


@dataclasses.dataclass(frozen=True)
class Rule:
    """One declaration as the cascade weighs it: the selector that carries it, its value,
    its place in the sheet, the media condition it sits under, and whether it is
    important."""

    chosen: Selector
    value: str
    order: int
    media: str | None
    important: bool = False


def winner(rules: Iterable[Rule], view: View) -> Rule | None:
    """Among rules that reach one element, the one the cascade settles on for ``view``: an
    important one over a normal one, then the more specific, then the later."""
    found: Rule | None = None
    for rule in rules:
        if holds(rule.media, view) and (
            found is None
            or (rule.important, rule.chosen.specificity, rule.order)
            > (found.important, found.chosen.specificity, found.order)
        ):
            found = rule
    return found


def media_length(value: str, view: View) -> float:
    """A length in a media query, in pixels: its `rem` and `em` are the browser's text size."""
    found = re.fullmatch(r"(\d*\.?\d+)(px|rem|em)", value.strip())
    if found is None:
        raise UnreadCss(value)
    return float(found.group(1)) * (1 if found.group(2) == "px" else view.browser_text)


def holds(condition: str | None, view: View) -> bool:
    """Whether a media condition holds on ``view``. A rule outside any media query holds
    everywhere; a list holds when any of its queries does."""
    if condition is None:
        return True
    return any(one_query_holds(query.strip(), view) for query in condition.split(","))


def one_query_holds(query: str, view: View) -> bool:
    result = True
    for word in re.sub(r"\([^)]*\)", " ", query).split():
        if word == "print":
            result = False
        elif word not in ("and", "only", "screen", "all"):
            raise UnreadCss(query)
    features = re.findall(r"\(\s*([\w-]+)\s*:\s*([^)]+?)\s*\)", query)
    if len(features) != query.count("("):
        raise UnreadCss(query)
    for name, value in features:
        if name == "min-width":
            result = result and view.width >= media_length(value, view)
        elif name == "max-width":
            result = result and view.width <= media_length(value, view)
        elif name == "prefers-reduced-motion":
            if value not in ("reduce", "no-preference"):
                raise UnreadCss(query)
            result = result and (value == "reduce") == view.reduced_motion
        else:
            raise UnreadCss(query)
    return result
