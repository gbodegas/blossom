"""Shared test helpers.

Anything two test modules need lives here rather than in one of them, so the
modules do not import each other; cross-imports between test files make the
suite's collection order matter, which it should not. That covers the source
records, the scripted models, the two-assignment graph the plan graph tests
drive, the fixture-week plans and route override the application tests
drive, the browser on her page with its cards and forms, and the small store
her reports and the family's checks are tested in.

This is a plain module rather than `conftest.py`: importing from a conftest
makes the same file reachable under two module names, which mypy rejects.
"""

import dataclasses
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Any, Protocol
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

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
from blossom.candidates import candidate_readings, reader
from blossom.captures import (
    STUDENT,
    CaptureChanged,
    CaptureCreated,
    CaptureDetails,
    CapturePromoted,
    candidate_basis,
    new_capture_id,
)
from blossom.clock import Clock, FrozenClock
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState, get_application_state
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.intake import PASTE_DAY
from blossom.noticing import Noticing, Verdict
from blossom.plan_checks import check_plan
from blossom.plan_reading import anchor_for
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceChannel, SourceConfidence, SourceRecord
from blossom.routes.navigation import assignment_anchor
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
    """Send her update from the card as it stands and return the address it goes back to."""
    page = client.get(
        HER_PAGE,
        params={"week": more.pop("week", FIXTURE_WEEK), "change": assignment_id},
        headers=PAGE_HEADERS,
    ).text
    head = hidden(card_for(page, assignment_id), "expected_report_id")
    answer = client.post(
        f"/student/actions/assignments/{assignment_id}/report",
        data={
            "status": status,
            "note": note,
            "expected_report_id": head,
            "week": FIXTURE_WEEK,
            **more,
        },
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
