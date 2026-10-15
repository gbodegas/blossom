# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Running the plan graph for one evening, from either page.

She plans from her page; a parent may start an evening's plan for her from
theirs. Both doors lead here, so there is one way a run is admitted, one way it
is built, and one way it settles.

A run has ``RUN_DEADLINE_SECONDS`` in all, from handler entry, after the form body is
read. The record decides what became of it: a run is admitted as ``running``, one per
household, and only ``settle_run`` publishes its plan, while it is still running,
before its deadline and with the evening's plan the one it expected. Every other
ending is ``end_run``. The answer says what the request saw: the plan, why there
is none, that the plan couldn't be saved, or that its saving couldn't be
confirmed, with the run's id to check later. Every wait has a limit, and work the
request stops waiting for goes on detached without deciding anything.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Annotated, Any, ClassVar, Final, Literal
from uuid import uuid4

from fastapi import Depends, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import URL
from starlette.responses import Response

from blossom.agent.graph import CompiledPlanGraph, EndingNotSaved, PlanState, plan_graph_for
from blossom.agent.retention import clear_thread, finish_held_reviews
from blossom.agent.runs import (
    DURABILITY,
    EXPECT_AN_ANSWER,
    RunBudget,
    RunTimedOut,
    Unfinished,
    bounded,
    run_config,
    time_limit_step,
)
from blossom.agent.steps import (
    DATE_PROBLEM,
    EXPECT_A_SAVED_RESULT,
    PAST_DUE_WORK,
    RESULT_NOT_SAVED,
    SERVICE,
    SERVICE_FAILED,
    TIMED_OUT,
    TIMEOUT,
    PastDueWork,
    RunTiming,
    StepRecord,
    describe_failure,
    failure_category,
    past_due_work,
)
from blossom.agent.steps import NOTHING_TO_SCHEDULE as NOTHING_TO_SCHEDULE_OUTCOME
from blossom.anthropic_client import (
    MISSING_KEY,
    ModelUnavailable,
    ServiceFailed,
    model_configured,
)
from blossom.dependencies import ApplicationState, get_application_state
from blossom.noticing import read_week
from blossom.stores.drafts import (
    INTERRUPTED,
    OVERTAKEN,
    SETTLE_GRACE_SECONDS,
    STORE_WAIT_SECONDS,
    DraftRecord,
    RunEnded,
    RunState,
    StaleBasis,
    StoreBusy,
    UnknownBasis,
    WriterBusy,
)
from blossom.stores.help_requests import NotARequestId, request_id_from
from blossom.templating import ended
from blossom.views import (
    AlreadyPlanningView,
    PastDueView,
    PlanConflictView,
    PlanRunView,
    ProblemView,
    RunStatusView,
    UnconfirmedRunView,
)

logger = logging.getLogger(__name__)

State = Annotated[ApplicationState, Depends(get_application_state)]


PlanGraphBuilder = Callable[[], CompiledPlanGraph]


@dataclass(frozen=True)
class PlanGraphs:
    """What a route needs from the graph: a way to build it, and whether it may start one.

    A dependency is resolved before its handler runs, so this hands back a
    builder rather than a graph, and the handler calls it after consulting the
    table. ``may_start`` is whether a run can be started at all, which needs a
    model; resuming a paused thread does not. ``budget`` makes each run's time
    limit at handler entry, after the form body is read. A test substitutes the
    whole object, scripted models, permission, and clock together, over the real
    stores.
    """

    build: PlanGraphBuilder
    may_start: bool
    budget: Callable[[], RunBudget] = field(default=RunBudget)


def plan_graphs(state: State) -> PlanGraphs:
    """The application's graphs: built from the seam, allowed to start when there is a key,
    each run timed on the application's own monotonic clock."""
    return PlanGraphs(
        build=lambda: plan_graph_for(state),
        may_start=model_configured(state.settings),
        budget=lambda: RunBudget(clock=state.monotonic),
    )


def require_model(graphs: PlanGraphs) -> None:
    """Refuse to start a run without a model, before any thread is written."""
    if not graphs.may_start:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=MISSING_KEY)


NOTHING_TO_SCHEDULE: Final = "Nothing to schedule from the work in this planning window."
"""What every planning route answers, 409, for an evening whose window holds no work
still to do: nothing has been planned, no run has been written, and no model asked."""

COULD_NOT_START_SENTENCES: Final = (
    "Blossom couldn't start a plan this time.",
    "Try again in a moment.",
)
COULD_NOT_START: Final = " ".join(COULD_NOT_START_SENTENCES)
"""What a planning route answers, 503, when the week couldn't be read, the graph couldn't be
built, or the run couldn't be admitted in time. No model is asked. A failure before admission
records no run; an admission the route stopped waiting for can still insert its run, which then
refuses presses until its own deadline."""


class CouldNotStart(HTTPException):
    """The week couldn't be read, the graph couldn't be built, or the run couldn't be admitted
    within the wait. A failure before admission records nothing; an admission whose result
    isn't known yet can still insert its run."""

    def __init__(self) -> None:
        super().__init__(status.HTTP_503_SERVICE_UNAVAILABLE, detail=COULD_NOT_START)


async def require_work(state: ApplicationState, plan_date: date, budget: RunBudget) -> None:
    """Refuse to start a run for an evening with nothing left to plan, before any thread is
    written and before the model is asked for.

    The window is read as the graph reads it, on a worker thread, inside the run's time
    and at most ``STORE_WAIT_SECONDS``; a read that fails or doesn't finish is
    ``CouldNotStart``. The graph reads it again when it runs, since a report of hers can
    land in between, and ends the same way.
    """
    wait = min(budget.remaining(), STORE_WAIT_SECONDS)
    try:
        active = await bounded(
            lambda: read_week(state.project_state, state.project_state, plan_date).active(), wait
        )
    except Exception as error:
        if not isinstance(error, Unfinished):
            logger.exception("the week for %s could not be read", plan_date)
        raise CouldNotStart from error
    if not active:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=NOTHING_TO_SCHEDULE)


def graph_for_a_run(graphs: PlanGraphs) -> CompiledPlanGraph:
    """The graph a new run takes, built before the run is admitted; a build that fails is
    ``CouldNotStart``, since nothing has been recorded."""
    try:
        return graphs.build()
    except Exception as error:
        logger.exception("the plan graph could not be built")
        raise CouldNotStart from error


def refuse_an_empty_run(run: PlanRunView) -> None:
    """Refuse, 409, a run that ended at its first node with nothing left to schedule.

    The route asked whether there was work before the run, and a report of
    hers landed between that question and the run's reading. Such a run made
    no plan and asked no model, so it is answered as the guard would have
    answered, not as a plan made; the run's own record stays in the ledger.
    """
    if run.draft_id is None and run.outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=NOTHING_TO_SCHEDULE)


Sentences = tuple[str, ...]
"""Words as the sentences they are built from, in order, each whole, so the first is known
from how the words were made, whatever a typed value or a title inside it holds."""


TODAYS_PLAN_NOT_MADE: Final = "Blossom can't make today's plan."
"""How a date problem for today's evening opens, before the work it is about."""


def due_on(day: date) -> str:
    """``August 18``: a day as her page says it."""
    return f"{day:%B} {day.day}"


def past_due_views(kept: Iterable[PastDueWork]) -> list[PastDueView]:
    """The past-due work a run named, in its order, as an answer shows it."""
    return [PastDueView.model_validate(work.model_dump()) for work in kept]


def named_work(work: PastDueView) -> str:
    """``Map quiz (Geography, due August 18)``: past-due work by its title, course and due
    date."""
    return f"{work.title} ({work.course}, due {due_on(work.due_date)})"


def saved_sentence(*, parent: bool) -> str:
    """That her updates are kept, to her or to a parent reading her page."""
    return "Her homework updates are saved." if parent else "Your homework updates are saved."


def plan_unchanged(*, parent: bool) -> str:
    """That the plan she has is the one she had, to her or to a parent."""
    return "Her current plan hasn't changed." if parent else "Your current plan hasn't changed."


def ended_without_a_plan(
    outcome: str,
    *,
    parent: bool,
    past_due: Sequence[PastDueView] = (),
    unchanged: bool = False,
    evening: date | None = None,
    where: bool = True,
) -> str:
    """``ended_sentences`` as one line reads them."""
    return " ".join(
        ended_sentences(
            outcome,
            parent=parent,
            past_due=past_due,
            unchanged=unchanged,
            evening=evening,
            where=where,
        )
    )


def ended_sentences(
    outcome: str,
    *,
    parent: bool,
    past_due: Sequence[PastDueView] = (),
    unchanged: bool = False,
    evening: date | None = None,
    where: bool = True,
) -> Sentences:
    """Why a run ended without a plan, as her page says it, a sentence each: what went wrong
    in plain words, a date problem as a short opening and then the work and why no plan can
    finish it, that her updates are kept, and, to a parent reading her page, where the
    run's record is. The run's own name for how it ended is never shown, and work from the record is
    named only for today's evening, since only there has its date passed. ``unchanged`` adds that
    her plan is the one she had; ``evening`` is the run's evening when it is not today's, which a
    date problem names. ``where`` false leaves out where the record is."""
    if outcome == NOTHING_TO_SCHEDULE_OUTCOME:
        return (NOTHING_TO_SCHEDULE,)
    category = failure_category(outcome)
    what: Sentences
    if category == DATE_PROBLEM and evening is not None:
        what = (
            f"Blossom can't make the plan for {evening_named(evening)}.",
            "Some work is due before that evening, so no plan can finish it on time.",
        )
    elif category == DATE_PROBLEM and past_due:
        named = [named_work(work) for work in past_due]
        listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"
        has, it = ("has a due date", "it") if len(named) == 1 else ("have due dates", "them")
        what = (
            TODAYS_PLAN_NOT_MADE,
            f"{listed} {has} that already passed, so no plan can finish {it} on time.",
        )
    elif category == DATE_PROBLEM:
        what = (
            TODAYS_PLAN_NOT_MADE,
            "Some work has a due date that already passed, so no plan can finish it on time.",
        )
    elif category == TIMEOUT:
        what = ("Planning took too long, so Blossom stopped.",)
    elif category == SERVICE:
        what = ("Blossom couldn't get a plan from the planning service this time.",)
    else:
        what = ("Blossom couldn't finish a reliable plan this time.",)
    then = "Family review shows what happened." if parent and where else ""
    kept = plan_unchanged(parent=parent) if unchanged else ""
    return tuple(part for part in (*what, kept, saved_sentence(parent=parent), then) if part)


Graphs = Annotated[PlanGraphs, Depends(plan_graphs)]


def evening_prefix(plan_date: date) -> str:
    """How every thread for one evening begins."""
    return f"plan:{plan_date.isoformat()}:"


def thread_for(plan_date: date) -> str:
    """A new thread for one evening, which is also its run's id. The date is for a person
    reading the table."""
    return f"{evening_prefix(plan_date)}{uuid4().hex[:8]}"


RUN_ID: Final = "run_id"
EVENING: Final = "evening"
ISSUED_AT: Final = "issued_at"
NEWEST_PLAN: Final = "newest_plan"
PLAN_FORM_FIELDS: Final = frozenset({RUN_ID, EVENING, ISSUED_AT, NEWEST_PLAN})
"""What every plan form carries besides its choices: the id its run is recorded under, the
evening it was made for, the real instant it was issued, and the newest published plan its
page knew, empty for none."""
FORM_LIFETIME: Final = timedelta(days=7)
"""How long a plan form may be pressed, in real time from when it was issued."""
ISSUED_AT_FORM: Final = "%Y%m%dT%H%M%SZ"
"""How a page writes a form's issue time: ISO 8601's basic form in UTC, to the second, with
no colon to be read as a time of day."""
ISSUED_AHEAD: Final = timedelta(minutes=5)
"""How far ahead of the real clock a form's issue time may be, for clocks a little apart."""


@dataclass(frozen=True)
class PlanForm:
    """A plan form's own fields, as a page writes them."""

    run_id: str
    evening: date
    issued_at: datetime
    newest_plan: str

    def fields(self) -> dict[str, str]:
        """The hidden fields a page writes for this form."""
        return {
            RUN_ID: self.run_id,
            EVENING: self.evening.isoformat(),
            ISSUED_AT: self.issued_at.astimezone(UTC).strftime(ISSUED_AT_FORM),
            NEWEST_PLAN: self.newest_plan,
        }

    def expired(self, now: datetime) -> bool:
        """Whether the form was issued ``FORM_LIFETIME`` or more before ``now``."""
        return now - self.issued_at >= FORM_LIFETIME


def fresh_plan_form(state: ApplicationState, evening: date, newest_plan: str) -> PlanForm:
    """A new plan form for ``evening``, issued now by the real clock. Writes nothing."""
    return PlanForm(uuid4().hex, evening, state.real_clock.now(), newest_plan)


def issue_time_from(value: str | None, now: datetime) -> datetime | None:
    """A form's issue time, held to ISO 8601 with an offset and no further ahead of ``now``
    than ``ISSUED_AHEAD``; ``None`` when it is missing or another shape."""
    try:
        issued_at = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    if issued_at.tzinfo is None or issued_at - now > ISSUED_AHEAD:
        return None
    return issued_at


def plan_form_from(fields: Mapping[str, str | None], now: datetime) -> PlanForm | None:
    """The plan form a press sent, held to the shapes a page writes: ``None`` when a field is
    missing or another shape, or its issue time is further ahead of ``now`` than allowed."""
    try:
        run_id = request_id_from(fields.get(RUN_ID) or "")
        evening = date.fromisoformat(fields.get(EVENING) or "")
    except (NotARequestId, ValueError):
        return None
    issued_at = issue_time_from(fields.get(ISSUED_AT), now)
    newest_plan = fields.get(NEWEST_PLAN)
    if newest_plan is None or issued_at is None:
        return None
    return PlanForm(run_id, evening, issued_at, newest_plan)


class SameRun(Exception):
    """The press's id already names a run: nothing is started, and the run is answered as
    it stands."""

    def __init__(self, run: RunState) -> None:
        super().__init__(f"run {run.run_id!r} is {run.status}")
        self.run = run


def run_replaced(state: ApplicationState, run: RunState) -> bool:
    """Whether a run's evening has a plan newer than any it made: it was overtaken, or the
    plan it published was since replaced."""
    if run.status == "ended":
        return run.reason == OVERTAKEN
    if run.status != "published" or run.draft is None:
        return False
    latest = state.drafts.latest_for(run.plan_date)
    return latest is not None and latest.draft_id != run.draft.draft_id


async def run_of_the_form(state: ApplicationState, run_id: str) -> RunState | None:
    """The run a pressed form's id names, read without ending anything, on a worker thread
    for at most the store's wait. ``None`` when none is, or the read fails or runs late:
    admission then decides again in its own transaction."""
    try:
        return await bounded(
            partial(state.drafts.run_status, run_id, STORE_WAIT_SECONDS, reconcile=False),
            STORE_WAIT_SECONDS,
        )
    except Exception as error:
        logger.warning("run %s could not be read for a press: %s", run_id, type(error).__name__)
        return None


def evening_named(day: date) -> str:
    """``Tuesday, October 6``: an evening as a sentence names it."""
    return f"{day:%A}, {day:%B} {day.day}"


def seconds_to_wait(run: RunState) -> int:
    """Whole seconds until a running run's deadline and its settle grace have passed."""
    return max(1, int(-(-(run.seconds_left + SETTLE_GRACE_SECONDS) // 1)))


def planning_sentences(run: RunState, *, parent: bool) -> Sentences:
    """What a press answers while the household's one run is still running: whose evening
    it is for, how long to wait, and that her updates are kept."""
    return (
        f"A plan for {evening_named(run.plan_date)} is being made.",
        f"Try again in about {seconds_to_wait(run)} seconds.",
        saved_sentence(parent=parent),
    )


def already_planning(run: RunState, *, parent: bool) -> str:
    """``planning_sentences`` as one line reads them."""
    return " ".join(planning_sentences(run, parent=parent))


class AlreadyPlanning(HTTPException):
    """The household has a run still running, so another is not started: 409, naming it,
    in her words or, with ``parent``, in a parent's."""

    def __init__(self, run: RunState, *, parent: bool = False) -> None:
        refusal = AlreadyPlanningView(
            message=already_planning(run, parent=parent),
            run_id=run.run_id,
            plan_date=run.plan_date,
            seconds_left=seconds_to_wait(run),
        )
        super().__init__(status.HTTP_409_CONFLICT, detail=refusal.model_dump(mode="json"))
        self.run = run


def not_saved_sentences(*, parent: bool, kept: bool = False) -> Sentences:
    """What a request answers when the store refused the plan's publication before it began,
    or was found not to have published it, to her or to a parent reading her page. ``kept``
    adds that her plan hasn't changed, when the evening is known to have one."""
    made = "Blossom made a plan but couldn't save it"
    if kept:
        plan = "her current plan" if parent else "your current plan"
        made = f"{made}, so {plan} hasn't changed"
    return f"{made}.", "Try again in a moment.", saved_sentence(parent=parent)


def not_saved(*, parent: bool, kept: bool = False) -> str:
    """``not_saved_sentences`` as one line reads them."""
    return " ".join(not_saved_sentences(parent=parent, kept=kept))


class NotSaved(HTTPException):
    """The run's plan was confirmed not published, and the run is being ended: 503. ``kept``
    when the evening's plan is known to be the one it had."""

    def __init__(self, *, kept: bool = False) -> None:
        super().__init__(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=not_saved(parent=False, kept=kept)
        )
        self.kept = kept


UNCONFIRMED: Final = "Blossom couldn't confirm that the new plan was saved."
FINISHING: Final = "Blossom is finishing the last plan request."
"""What a page says about a run still running past its deadline: a settle it authorized in
time may still commit, so nothing is said to have timed out until the record says so."""


CHECK_ON_THAT_REQUEST: Final = "Check on that request."
CHECK_ON_IT: Final = "Check on it."
CHECK_AGAIN: Final = "Check again."
"""The words of the link that asks a page where a run stands, each a sentence of its own
after the line it ends: on a refusal naming the run, on a run being made or finished, and on
an outcome that couldn't be confirmed."""


@dataclass(frozen=True)
class RunCheck:
    """The link that asks a page again where a run stands, and its words."""

    href: str
    label: str


def run_check(page: str, run_id: str, label: str) -> RunCheck:
    """The link to ``page`` naming ``run_id``, for the page to say where that run stands."""
    return RunCheck(str(URL(page).include_query_params(run=run_id)), label)


@dataclass(frozen=True)
class RunNotice:
    """What a page says about a planning run, its link to check again, if any, and the run
    a page's ``?run=`` named when it ended without a plan."""

    said: str
    check: RunCheck | None = None
    running: bool = False
    run: RunState | None = None
    past_due: tuple[PastDueView, ...] = ()
    """The past-due work a run of today's evening kept, while the plan it was admitted
    against is still the evening's newest, which her week links after the notice."""


STORE_FAILURES: Final = (Unfinished, StoreBusy, WriterBusy, sqlite3.Error)
"""Every way a read of the store fails: busy past its wait, or the file refusing it."""

PlanRow = Literal[
    "her-not-whole",
    "her-another-evening",
    "her-expired",
    "her-running",
    "her-newer-plan",
    "her-plan-made",
    "her-ended",
    "her-unconfirmed",
    "her-before",
    "family-not-whole",
    "family-expired",
    "family-another-evening",
    "family-unreadable-date",
    "family-running",
    "family-newer-plan",
    "family-newer-plan-unread",
    "family-plan-made",
    "family-ended",
    "family-unconfirmed",
    "family-not-a-date",
    "family-passed",
    "family-beyond",
    "family-already-planning",
    "family-not-saved",
    "family-could-not-start",
    "family-refused",
    "family-interrupted",
]
"""Each answer a plan press gives on a page, besides landing on the page it planned for."""
PlanLanding = Literal[
    "her-made",
    "her-plan-latest",
    "family-made",
    "family-plan-latest",
    "family-ended-first",
]
"""Each press that lands on the page it planned for, with no answer of its own: a plan made,
a used form whose plan is still the newest, or, on the family page, a first press whose run
ended without a plan, which the page's ended fold explains."""


def landed[Landing: Response](row: PlanLanding, response: Landing) -> Landing:
    """``response``, a press landing on the page it planned for, named by its row."""
    del row
    return response


Needs = Literal["nothing", "plan", "button", "place", "shown"]
"""What the page sent must show for a clause to be said: nothing, today's plan (her page),
the plan button or form, the plan the answer is about, or that plan in the waiting list or
under today's heading."""
NOT_USED: Final = ("Blossom couldn't use that plan request.", "No plan was started.")
OPENED_A_WEEK_AGO: Final = ("That page was opened a week or more ago.", "No new plan was started.")
"""The two refusals of a plan form on either page: one that isn't whole or names no plan, and
one issued ``FORM_LIFETIME`` or more before it was sent."""


@dataclass(frozen=True)
class Clause:
    """One sentence of an answer's line, whole, said when the page shows what it ``needs``,
    and ``otherwise``, one sentence or none, said in its place when it doesn't."""

    words: str
    needs: Needs = "nothing"
    otherwise: str = ""


def opening_and_rest(sentences: Sentences) -> tuple[str, str]:
    """Sentences as the first and the rest after it, which is empty for one sentence."""
    first, *rest = sentences
    return first, " ".join(rest)


@dataclass(frozen=True)
class PlanAnswer:
    """What a plan press answers on a page: its row, status, line, the sentences its stand-in
    says, what the page keeps, whether it offers a plan form, and the plan it is about."""

    row: PlanRow
    status: int
    line: tuple[Clause, ...]
    elsewhere: Sentences
    keeps: date | None = None
    offers_form: bool = True
    open_plan: str | None = None
    check: RunCheck | None = None

    def sentences(self, shows: Callable[[Needs], bool]) -> Sentences:
        """The line's sentences as the page says them, each clause kept only when ``shows``
        its need."""
        return tuple(
            part
            for clause in self.line
            if (part := clause.words if shows(clause.needs) else clause.otherwise)
        )

    def said(self, shows: Callable[[Needs], bool]) -> str:
        """The line as the page says it."""
        return " ".join(self.sentences(shows))

    def opening(self, shows: Callable[[Needs], bool]) -> tuple[str, str]:
        """The line as the page says it, as its first sentence, which takes the focus, and
        the rest."""
        return opening_and_rest(self.sentences(shows))

    def stand_in(self, line: str) -> tuple[str, str]:
        """The stand-in's line, its own sentences, the last ending one, and then ``line``
        when there is one, as its first sentence and the rest."""
        *said, last = self.elsewhere
        return opening_and_rest((*said, ended(last), line) if line else self.elsewhere)


def fact(*words: str) -> tuple[Clause, ...]:
    """Sentences an answer says whatever the page shows, a clause each."""
    return tuple(Clause(said) for said in words)


def being_made(run: RunState, page: str) -> RunNotice:
    """A running run as a page says it, before its deadline and after it, either way with a
    link to check on it. Neither says what is behind the row, which may be nothing."""
    said = (
        f"The plan request for {evening_named(run.plan_date)} is still being finished."
        if run.seconds_left > 0
        else FINISHING
    )
    return RunNotice(said, run_check(page, run.run_id, CHECK_ON_IT), running=True)


def ended_notice(run: RunState, *, parent: bool, today: date) -> RunNotice | None:
    """An ended run as a page says it: why, and, when the evening has a plan, that it is the
    one she had, with the past-due work a run of today's evening kept while its plan is still
    the newest. A run overtaken by a newer plan adds nothing, since that plan is shown."""
    if run.reason == OVERTAKEN:
        return None
    kept = run.past_due if run.plan_date == today and run.plan_unchanged else None
    return RunNotice(
        ended_without_a_plan(
            run.reason,
            parent=parent,
            unchanged=run.plan_unchanged and run.has_plan,
            evening=None if run.plan_date == today else run.plan_date,
        ),
        past_due=tuple(past_due_views(kept or ())),
    )


def asked_run(state: ApplicationState, run_id: str, page: str, *, parent: bool) -> RunNotice | None:
    """Where the run a page's ``?run=`` names stands, after ending any run past its deadline,
    for a page read on a worker thread. A published run, or one never made, adds nothing; a
    record that can't be read in time is said as unconfirmed, with a link to check again."""
    try:
        run = state.drafts.run_status(run_id, STORE_WAIT_SECONDS)
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        logger.warning("run %s could not be read for a page: %s", run_id, type(error).__name__)
        return RunNotice(
            f"{UNCONFIRMED} {saved_sentence(parent=parent)}",
            run_check(page, run_id, CHECK_AGAIN),
        )
    if run is None or run.status == "published":
        return None
    if run.status == "running":
        return being_made(run, page)
    notice = ended_notice(run, parent=parent, today=state.clock.today())
    return None if notice is None else replace(notice, run=run)


def run_notice(
    state: ApplicationState, run_id: str | None, page: str, *, parent: bool, today: date | None
) -> RunNotice | None:
    """What a page says about a planning run: the one ``?run=`` names or, when it names none
    or that run adds nothing, what the page says on load, so a newer run still shows."""
    asked = None if run_id is None else asked_run(state, run_id, page, parent=parent)
    if asked is not None:
        return asked
    return latest_run_notice(state, page, parent=parent, today=today)


def latest_run_notice(
    state: ApplicationState, page: str, *, parent: bool, today: date | None
) -> RunNotice | None:
    """What a page says on load about the household's newest run, read without ending
    anything: a run still running, with a link to check on it. With ``today``, a run for
    that evening that
    ended with no newer plan published is said too. A record that can't be read adds
    nothing to the page."""
    try:
        run = state.drafts.latest_run()
    except (StoreBusy, WriterBusy, sqlite3.Error) as error:
        logger.warning("the latest run could not be read for a page: %s", type(error).__name__)
        return None
    if run is None:
        return None
    if run.status == "running":
        return being_made(run, page)
    if (
        run.status == "ended"
        and run.plan_date == today
        and run.plan_unchanged
        and run.reason != NOTHING_TO_SCHEDULE_OUTCOME
    ):
        return ended_notice(run, parent=parent, today=today)
    return None


class Unconfirmed(Exception):
    """Whether the run's plan was published couldn't be confirmed in time. Its run id is
    where to look later."""

    def __init__(self, run_id: str, plan_date: date) -> None:
        super().__init__(UNCONFIRMED)
        self.run_id = run_id
        self.plan_date = plan_date

    def view(self) -> UnconfirmedRunView:
        """The 202 answer: the run to check, its evening, and that it is unconfirmed."""
        return UnconfirmedRunView(run_id=self.run_id, plan_date=self.plan_date)


def run_view(thread_id: str, plan_date: date, result: dict[str, Any]) -> PlanRunView:
    """What a finished or paused run looks like to the parent."""
    draft = result.get("draft")
    return PlanRunView(
        thread_id=thread_id,
        plan_date=plan_date,
        outcome=result["outcome"],
        draft_id=None if draft is None else draft.draft_id,
        waiting="__interrupt__" in result,
        steps=list(result.get("steps", [])),
        past_due=past_due_views(
            past_due_work(result.get("assignments", []), result.get("past_due", {}))
        ),
    )


@dataclass(frozen=True)
class SeenEnding:
    """What a press has seen of its run's ended record when it holds no ``RunState``: the
    graph's ``committed`` result, as its status, its reason and the past-due work it kept."""

    status: str
    reason: str
    past_due: tuple[PastDueWork, ...] | None = None


@dataclass(frozen=True)
class PlanMade:
    """How a planning request ended: the run as the parent reads it, and the record
    ``settle_run`` returned when its plan was published."""

    view: PlanRunView
    record: DraftRecord | None = None


def hold(state: ApplicationState, work: "asyncio.Future[Any]") -> None:
    """Keep a reference to ``work`` while it runs, and drop it when it ends."""
    state.detached.add(work)
    work.add_done_callback(state.detached.discard)


def detach(state: ApplicationState, work: "asyncio.Future[Any]", what: str) -> None:
    """Go on without waiting for ``work``: it is held until it ends, and how it ended is
    read and logged as ``what``. A run that had already ended is its timeout, not an error."""
    hold(state, work)

    def ended(done: "asyncio.Future[Any]") -> None:
        # Work stopped by a cancel has no outcome to read.
        error: BaseException | None = None
        with contextlib.suppress(BaseException):
            error = done.exception()
        if isinstance(error, RunEnded):
            logger.info("%s: the run had already ended (%s)", what, error)
        elif error is not None:
            logger.error("%s failed", what, exc_info=error)

    work.add_done_callback(ended)


def in_the_background(state: ApplicationState, call: Callable[[], Any], what: str) -> None:
    """Start ``call`` on a worker thread and go on without waiting for it."""
    detach(state, asyncio.get_running_loop().run_in_executor(None, call), what)


async def tidy_thread(thread_id: str, state: ApplicationState) -> None:
    """Clear a thread nothing will resume, or log why not and leave it to the sweep.

    Tidying is never what a caller hears about: a run that paused or ended has
    its outcome, and the sweep clears every thread no waiting draft or running run
    refers to within the hour.
    """
    try:
        await clear_thread(state.checkpointer, thread_id)
    except Exception:
        logger.exception("saved state of thread %s not cleared; the sweep clears it", thread_id)


def tidy_later(thread_id: str, state: ApplicationState) -> None:
    """Clear a thread without waiting for it."""
    detach(state, asyncio.ensure_future(tidy_thread(thread_id, state)), f"tidying {thread_id}")


async def hold_in_time(lock: asyncio.Lock, budget: RunBudget) -> bool:
    """True once ``lock`` is held with time left in the run; False, holding nothing, otherwise.

    The decision lock is held across saved-state reads by reviews and the sweep, so a
    run waiting for it is still spending its time, and one that gets it too late goes
    no further.
    """
    try:
        async with asyncio.timeout(budget.remaining()):
            await lock.acquire()
    except TimeoutError:
        return False
    if budget.remaining() <= 0:
        lock.release()
        return False
    return True


async def read_in_time[T](reading: Awaitable[T], budget: RunBudget) -> T:
    """What ``reading`` returns, with time left in the run, or ``RunTimedOut``.

    Only the run's own limit is a timeout here: a ``TimeoutError`` the reading raises
    itself is the failure it is.
    """
    limit = asyncio.timeout(budget.remaining())
    try:
        async with limit:
            read = await reading
    except TimeoutError as error:
        if limit.expired():
            raise RunTimedOut from error
        raise
    if budget.remaining() <= 0:
        raise RunTimedOut
    return read


WAITED_FOR: Final = {"plan": "plan", "critique": "verdict"}

ADMISSION_ALLOWANCE_SECONDS: Final = 0.25
"""How far past its store wait an admission is waited for: room for the statement the
file's busy handler is in when the wait ends."""


async def admit(
    state: ApplicationState,
    run_id: str,
    plan_date: date,
    budget: RunBudget,
    basis: str | None = None,
) -> None:
    """Record the run as the household's one running run, or refuse the press.

    The deadline is the end of the request's budget, on the store's clock. A run
    still running is ``AlreadyPlanning``; an admission that doesn't finish in time,
    or fails, is ``CouldNotStart``. One the route stopped waiting for can still insert
    its row, since the file's busy wait can overrun its cap on Windows, and that row
    refuses presses until its own deadline. A run already recorded under ``run_id`` is
    ``SameRun``; with ``basis``, a page that didn't know the evening's newest plan is
    ``StaleBasis``, and one naming a plan never published is ``UnknownBasis``.
    """
    wait = min(budget.remaining(), STORE_WAIT_SECONDS)
    admission = partial(
        state.drafts.admit_run,
        run_id,
        plan_date=plan_date,
        deadline_mono=state.monotonic() + budget.remaining(),
        wait=wait,
        basis=basis,
    )
    try:
        blocking = await bounded(admission, wait + ADMISSION_ALLOWANCE_SECONDS)
    except (StaleBasis, UnknownBasis):
        raise
    except Exception as error:
        if not isinstance(error, Unfinished | StoreBusy | WriterBusy):
            logger.exception("run %s could not be admitted", run_id)
        raise CouldNotStart from error
    except BaseException:
        # A cancel. The insert may still land after it; ending it now leaves at most a
        # row with no graph behind it, which ends at its own deadline.
        in_the_background(
            state,
            partial(state.drafts.end_run, run_id, reason=INTERRUPTED),
            f"ending the canceled run {run_id}",
        )
        raise
    if blocking is not None:
        if blocking.run_id == run_id:
            raise SameRun(blocking)
        raise AlreadyPlanning(blocking)


@dataclass
class Planning:
    """One admitted run, from its graph to its answer."""

    state: ApplicationState
    run_id: str
    plan_date: date
    budget: RunBudget
    generation_seconds: float | None = None
    settle_seconds: float | None = None

    def timing(self, outcome: str | None, *, unconfirmed: bool = False) -> RunTiming:
        """The run's time as it stands, for the terminal write or the final one."""
        return self.budget.timing(
            None if outcome is None else failure_category(outcome),
            generation_seconds=self.generation_seconds,
            settle_seconds=self.settle_seconds,
            response_seconds=round(self.budget.elapsed(), 3),
            unconfirmed=unconfirmed,
        )

    def keep_timing(self, outcome: str | None, *, unconfirmed: bool = False) -> None:
        """Complete the run's time with its answer's, without waiting. Never changes status."""
        in_the_background(
            self.state,
            partial(
                self.state.drafts.record_timing,
                self.run_id,
                self.timing(outcome, unconfirmed=unconfirmed),
            ),
            f"keeping the time of run {self.run_id}",
        )

    def terminal_step(self, outcome: str) -> StepRecord:
        """The step a run cut off by its time or its service ends with."""
        node, round_number = self.budget.waiting_on or ("time_limit", 0)
        self.budget.lap(node, round_number)
        if node in WAITED_FOR:
            return self.step(node, round_number, describe_failure(outcome, WAITED_FOR[node]))
        return self.time_limit_step()

    def step(self, node: str, round_number: int, found: str) -> StepRecord:
        """A step expecting an answer inside the run's time, recorded now. Reads only the
        clock, so building it can't fail in the middle of an ending."""
        return StepRecord(
            node=node,
            round=round_number,
            expected=EXPECT_AN_ANSWER,
            found=found,
            recorded_at=self.state.clock.now(),
        )

    def time_limit_step(self) -> StepRecord:
        """The step an ending recorded out of time takes when no model request was still
        awaited past the deadline: the run's time limit, with no lap."""
        return time_limit_step(self.state.clock.now())

    def result_not_saved_step(self) -> StepRecord:
        """The step an ending the route records after the graph couldn't save how the run
        ended takes. Reads only the clock, so building it can't fail in an ending."""
        return StepRecord(
            node="stopped",
            round=0,
            expected=EXPECT_A_SAVED_RESULT,
            found=RESULT_NOT_SAVED,
            recorded_at=self.state.clock.now(),
        )

    def canceled_step(self) -> StepRecord:
        """The step a canceled run's ending takes if it is recorded out of time: the model
        request still awaited when the deadline had passed, or else the time limit."""
        in_flight = self.budget.in_flight
        if in_flight is None or in_flight[0] not in WAITED_FOR or self.budget.remaining() > 0:
            return self.time_limit_step()
        node, round_number = in_flight
        return self.step(node, round_number, describe_failure(TIMED_OUT, WAITED_FOR[node]))

    async def end(self, reason: str, terminal: StepRecord | None = None) -> PlanMade | None:
        """End the run without a plan. Before its deadline the ending is waited for, so an
        immediate press is admitted; at or after it, it goes on without a wait.

        Past the deadline the store records the run ``timed_out``, with the time-limit
        step in place of ``terminal``. When the run is seen ended ``timed_out`` in place
        of ``reason``, by this ending or one before it, the answer follows the record: the
        out-of-time answer with the steps the record keeps, which is returned. Otherwise
        ``None``, and the caller answers for ``reason``.
        """
        out_of_time = self.time_limit_step()
        ending = partial(
            self.state.drafts.end_run,
            self.run_id,
            reason=reason,
            steps=list(self.budget.steps),
            terminal=terminal,
            timing=self.timing(reason),
            out_of_time=out_of_time,
        )
        left = self.budget.remaining()
        if left <= 0:
            # Nobody waits for this ending, so its steps are never read.
            in_the_background(self.state, ending, f"ending run {self.run_id}")
            return None
        # One allowance for the ending and, when it is seen out of time, the read of its steps.
        loop = asyncio.get_running_loop()
        allowance_ends = loop.time() + self.ending_wait()
        try:
            ended = await bounded(ending, allowance_ends - loop.time())
        except Unfinished as unfinished:
            detach(self.state, unfinished.work, f"ending run {self.run_id}")
            logger.warning("run %s not seen ended in time; its deadline ends it", self.run_id)
            return None
        except Exception:
            logger.exception("run %s could not be ended; its deadline ends it", self.run_id)
            return None
        return await self.as_recorded(ended, asked=reason, wait=allowance_ends - loop.time())

    def ending_wait(self) -> float:
        """How long a press waits on the store as its run ends: what is left of the run's
        time and the settle grace, at most the store's own wait."""
        return min(STORE_WAIT_SECONDS, self.budget.remaining() + SETTLE_GRACE_SECONDS)

    async def as_recorded(
        self, run: RunState | SeenEnding | None, *, asked: str, wait: float
    ) -> PlanMade | None:
        """The answer when the press has seen its run's ended record and it says
        ``timed_out``: that reason, the steps it keeps, read for at most ``wait`` seconds,
        and the past-due work it kept. A run ended ``timed_out`` may be another writer's
        account, so it is followed whatever ``asked`` was.

        ``None``, and the caller answers for ``asked``, when the press saw no ended record,
        or its own ending as asked under a reason only it records, or another in-time
        reason, which is answered as asked."""
        if run is None or run.status != "ended":
            return None
        if run.reason == asked and asked != TIMED_OUT:
            return None
        # An in-time reason other than the one asked, as when the graph committed its own
        # ending and the route then ended the run interrupted, is answered as asked.
        if run.reason not in (TIMED_OUT, asked):
            return None
        return self.ended_view(
            run.reason, await self.stored_steps(wait), past_due_views(run.past_due or ())
        )

    def ended_view(
        self, reason: str, steps: Sequence[StepRecord], past_due: Sequence[PastDueView] = ()
    ) -> PlanMade:
        """The answer for a run that ended without a plan, under ``reason``."""
        self.keep_timing(reason)
        return PlanMade(
            PlanRunView(
                thread_id=self.run_id,
                plan_date=self.plan_date,
                outcome=reason,
                draft_id=None,
                waiting=False,
                steps=list(steps),
                past_due=list(past_due),
            )
        )

    async def cut_off(self, outcome: str) -> PlanMade:
        """End a run its time or its service cut off, with the step it ended on."""
        terminal = self.terminal_step(outcome)
        steps = [*self.budget.steps, terminal]
        made = await self.end(outcome, terminal)
        tidy_later(self.run_id, self.state)
        return self.ended_view(outcome, steps) if made is None else made

    def timed_out(self) -> PlanMade:
        """Answer at once for a run whose time ran out before it settled; the ending goes on
        without a wait, since no settlement was asked for and none can publish now."""
        terminal = self.terminal_step(TIMED_OUT)
        steps = [*self.budget.steps, terminal]
        in_the_background(
            self.state,
            partial(
                self.state.drafts.end_run,
                self.run_id,
                reason=TIMED_OUT,
                steps=list(self.budget.steps),
                terminal=terminal,
                timing=self.timing(TIMED_OUT),
            ),
            f"ending run {self.run_id}",
        )
        tidy_later(self.run_id, self.state)
        return self.ended_view(TIMED_OUT, steps)

    def not_saved(self, *, kept: bool = False) -> NotSaved:
        """End a run whose plan was confirmed not published, without a wait, and say so;
        ``kept`` when the evening's plan is known to be the one it had."""
        in_the_background(
            self.state,
            partial(
                self.state.drafts.end_run,
                self.run_id,
                reason=INTERRUPTED,
                timing=self.timing(INTERRUPTED),
                out_of_time=self.time_limit_step(),
            ),
            f"ending run {self.run_id}",
        )
        tidy_later(self.run_id, self.state)
        self.keep_timing(INTERRUPTED)
        return NotSaved(kept=kept)

    def unconfirmed(self) -> Unconfirmed:
        """Say the plan's saving couldn't be confirmed, and note it with the run's time."""
        self.keep_timing(None, unconfirmed=True)
        return Unconfirmed(self.run_id, self.plan_date)

    def as_settled(self, run: RunState, steps: Sequence[StepRecord]) -> PlanMade:
        """The answer for a run the record shows settled: its plan, or why there is none."""
        if run.status == "published" and run.draft is not None:
            self.keep_timing(None)
            return PlanMade(
                PlanRunView(
                    thread_id=self.run_id,
                    plan_date=self.plan_date,
                    outcome=run.draft.outcome,
                    draft_id=run.draft.draft_id,
                    waiting=True,
                    steps=list(steps),
                ),
                run.draft,
            )
        tidy_later(self.run_id, self.state)
        return self.ended_view(run.reason, steps)

    async def generate(self, graph: CompiledPlanGraph) -> dict[str, Any] | None:
        """The graph's result, or ``None`` when the run's time ran out first.

        The graph runs as a task of its own, waited on with what is left. At the
        deadline, or when the request is canceled, the task is canceled and left to
        unwind on its own: its saved-state writes are not waited for.
        """
        started = self.budget.elapsed()
        task = asyncio.ensure_future(
            graph.ainvoke(
                PlanState(plan_date=self.plan_date, rounds=0),
                config=run_config(self.run_id, callbacks=[self.state.tracer]),
                durability=DURABILITY,
                context=self.budget,
            )
        )
        hold(self.state, task)
        done: set[asyncio.Future[Any]]
        try:
            done, _ = await asyncio.wait({task}, timeout=self.budget.remaining())
        except BaseException:
            # The request was canceled: so is the graph, left to unwind on its own.
            task.cancel()
            detach(self.state, task, f"the plan graph of run {self.run_id}")
            raise
        finally:
            self.generation_seconds = round(self.budget.elapsed() - started, 3)
        if task not in done:
            task.cancel()
            detach(self.state, task, f"the plan graph of run {self.run_id}")
            return None
        return dict(task.result())

    async def publish(self, view: PlanRunView) -> PlanMade:
        """Settle a run that paused with its draft: held reviews first, then ``settle_run``,
        under the decision lock, each inside the run's time, the commit inside its grace."""
        started = self.budget.elapsed()
        if not await hold_in_time(self.state.decision_lock, self.budget):
            return self.timed_out()
        try:
            try:
                finished = await read_in_time(
                    finish_held_reviews(
                        self.state.checkpointer,
                        self.state.drafts,
                        plan_date=self.plan_date,
                        to_the_end_of_each=False,
                    ),
                    self.budget,
                )
            except BaseException:
                self.state.decision_lock.release()
                raise
        except RunTimedOut:
            return self.timed_out()
        except Exception:
            # A held review that can't be read is a failure like any other: the run ends
            # before the answer, so the next press is admitted. The decision lock is
            # already free, so no decision waits on that ending.
            made = await self.end(INTERRUPTED)
            tidy_later(self.run_id, self.state)
            if made is not None:
                logger.exception("the plan for %s failed on the way", self.plan_date)
                return made
            self.keep_timing(INTERRUPTED)
            raise
        try:
            for _, thread in finished:
                tidy_later(thread, self.state)
            left = self.budget.remaining()
            if left <= 0:
                return self.timed_out()
            settling = partial(
                self.state.drafts.settle_run,
                self.run_id,
                timing=self.timing(None),
                wait=min(left, STORE_WAIT_SECONDS),
            )
            try:
                settled = await bounded(settling, left + SETTLE_GRACE_SECONDS)
            except (StoreBusy, WriterBusy):
                # Refused before anything began. When the wait was all the time left,
                # the time ran out waiting for the store.
                if left <= STORE_WAIT_SECONDS:
                    return self.timed_out()
                raise self.not_saved() from None
            except Exception as error:
                if isinstance(error, Unfinished):
                    detach(self.state, error.work, f"settling run {self.run_id}")
                else:
                    logger.warning("settling run %s ended in %s", self.run_id, type(error).__name__)
                return await self.read_back(view)
            finally:
                self.settle_seconds = round(self.budget.elapsed() - started, 3)
        finally:
            self.state.decision_lock.release()
        for displaced in settled.displaced:
            tidy_later(displaced.thread_id, self.state)
        return self.as_settled(settled.run, view.steps)

    async def read_back(self, view: PlanRunView) -> PlanMade:
        """What became of a settle that ended in an error: read before the deadline only.

        Past the deadline nothing is looked up, and the answer is unconfirmed.
        """
        left = self.budget.remaining()
        if left <= 0:
            raise self.unconfirmed()
        reading = partial(self.state.drafts.run_status, self.run_id, left, reconcile=False)
        try:
            found = await bounded(reading, left)
        except Exception as error:
            if isinstance(error, Unfinished):
                detach(self.state, error.work, f"reading back run {self.run_id}")
            raise self.unconfirmed() from error
        if found is None or found.status == "running":
            kept = found is not None and found.plan_unchanged and found.has_plan
            raise self.not_saved(kept=kept)
        return self.as_settled(found, view.steps)

    async def stored_steps(self, wait: float) -> list[StepRecord]:
        """The steps the run's record keeps, read on a worker thread for at most ``wait``
        seconds; none when the read fails or doesn't finish, and a read still going is left
        to end on its own. Never taken for the ending's own failure."""
        try:
            return await bounded(partial(self.state.drafts.steps_for, self.run_id), wait)
        except Exception as error:
            if isinstance(error, Unfinished):
                detach(self.state, error.work, f"reading back run {self.run_id}")
            logger.warning(
                "run %s could not be read for a press: %s", self.run_id, type(error).__name__
            )
            return []

    async def run(self, graph: CompiledPlanGraph) -> PlanMade:
        """From the graph to the answer, for a run already admitted."""
        try:
            result = await self.generate(graph)
        except RunEnded as error:
            if error.run is None:
                raise
            tidy_later(self.run_id, self.state)
            made = await self.as_recorded(
                error.run, asked=error.run.reason, wait=self.ending_wait()
            )
            if made is not None:
                return made
            return self.ended_view(
                error.run.reason, self.budget.steps, past_due_views(error.run.past_due or ())
            )
        except ModelUnavailable as error:
            made = await self.end(INTERRUPTED)
            tidy_later(self.run_id, self.state)
            if made is not None:
                return made
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
        except RunTimedOut:
            return await self.cut_off(TIMED_OUT)
        except ServiceFailed:
            return await self.cut_off(SERVICE_FAILED)
        except Exception as error:
            # A store's or a request's own TimeoutError is a failure like any other. When
            # the graph couldn't save how the run ended, the route's ending asks to say so,
            # which the record keeps only when that ending is the one recorded in time.
            unsaved = isinstance(error, EndingNotSaved)
            made = await self.end(INTERRUPTED, self.result_not_saved_step() if unsaved else None)
            tidy_later(self.run_id, self.state)
            if made is not None:
                logger.exception("the plan for %s failed on the way", self.plan_date)
                return made
            self.keep_timing(INTERRUPTED)
            raise
        if result is None:
            return self.timed_out()
        committed = result.get("committed")
        if committed is not None:
            # The record's own reason and past-due work, as it committed them, never ones
            # worked out here; and, when it ended out of time, the steps it keeps.
            tidy_later(self.run_id, self.state)
            kept = committed.get("past_due")
            seen = SeenEnding(
                status=str(committed["status"]),
                reason=str(committed["reason"]),
                past_due=None if kept is None else PAST_DUE_WORK.validate_json(str(kept)),
            )
            made = await self.as_recorded(
                seen, asked=str(result.get("outcome")), wait=self.ending_wait()
            )
            if made is not None:
                return made
            ended = run_view(self.run_id, self.plan_date, {**result, "outcome": ""})
            return self.ended_view(seen.reason, ended.steps, past_due_views(seen.past_due or ()))
        view = run_view(self.run_id, self.plan_date, result)
        if not view.waiting:
            made = await self.end(INTERRUPTED)
            tidy_later(self.run_id, self.state)
            return self.ended_view(INTERRUPTED, view.steps) if made is None else made
        made = await self.publish(view)
        if made.record is not None:
            return PlanMade(made.view.model_copy(update={"past_due": view.past_due}), made.record)
        return made


async def make_plan(
    graph: CompiledPlanGraph,
    plan_date: date,
    state: ApplicationState,
    *,
    budget: RunBudget | None = None,
    run_id: str | None = None,
    basis: str | None = None,
) -> PlanMade:
    """Admit a run for one evening, run the graph, and settle it, inside ``budget``.

    ``budget`` starts at handler entry, after the form body is read; without one, the run's
    time starts now. ``run_id`` names the run, a new thread for the evening unless given,
    and ``basis`` is the newest plan the pressing page knew, checked at admission.
    The household has one running
    run at a time: a press while one runs is ``AlreadyPlanning``, naming it, and
    an admission that can't be made in time is ``CouldNotStart``. The graph runs
    as its own task; a run that stops before the gate is ended by its last node,
    and the answer gives the reason the record committed. A run that pauses with
    its draft is settled by ``settle_run`` alone, which publishes only while the
    run is running, before its deadline and with the evening's plan unchanged.

    When the press has seen its run's record, the answer follows it; an ending it
    goes on without is answered as asked. A settle refused before it began is
    ``NotSaved``; any other failure is read back by the run's id before the
    deadline, and past it, or when the reading doesn't finish, the answer is
    ``Unconfirmed``. A canceled request cancels its graph without waiting for it to
    unwind, ends the run ``interrupted`` within the settle grace, and goes on
    canceling: whichever of that ending and a settlement already asked for
    commits first is what the record keeps.
    """
    budget = RunBudget(clock=state.monotonic) if budget is None else budget
    run_id = thread_for(plan_date) if run_id is None else run_id
    await admit(state, run_id, plan_date, budget, basis)
    planning = Planning(state, run_id, plan_date, budget)
    try:
        return await planning.run(graph)
    except BaseException as error:
        if isinstance(error, Exception):
            raise
        ending = partial(
            state.drafts.end_run,
            run_id,
            reason=INTERRUPTED,
            timing=planning.timing(INTERRUPTED),
            wait=SETTLE_GRACE_SECONDS,
            out_of_time=planning.canceled_step(),
        )
        try:
            ended = await bounded(ending, SETTLE_GRACE_SECONDS)
        except Unfinished as unfinished:
            detach(state, unfinished.work, f"ending the canceled run {run_id}")
        except Exception:
            logger.exception("the canceled run %s could not be ended", run_id)
        else:
            # A plan its settle published keeps the thread a review resumes.
            if ended is not None and ended.status == "ended":
                tidy_later(run_id, state)
        raise


RUN_STATUS_ANSWERS: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_404_NOT_FOUND: {"model": ProblemView},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ProblemView},
}
"""The bodies a run's status answers with besides the run, as both status routes declare
them: an unknown run, and a record that couldn't be read."""


async def run_status_view(state: ApplicationState, run_id: str) -> RunStatusView:
    """Where one run stands, after ending any run past its deadline: 404 for an unknown
    run, and 503 when the record can't be read in time or the read fails."""
    try:
        found = await bounded(
            partial(state.drafts.run_status, run_id, STORE_WAIT_SECONDS), STORE_WAIT_SECONDS
        )
    except (Unfinished, StoreBusy, WriterBusy, sqlite3.Error) as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNCONFIRMED) from error
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no run {run_id!r}")
    return RunStatusView(
        run_id=found.run_id,
        plan_date=found.plan_date,
        status=found.status,
        reason=None if found.status == "running" else found.reason,
        draft_id=None if found.draft is None else found.draft.draft_id,
    )


# ------------------------------------------------------------------ the JSON plan requests


def repeats_a_field(body: bytes) -> bool:
    """Whether a JSON body names a field twice in one object, which a reader would take as its
    last. A body that isn't JSON repeats nothing here; the framework refuses it."""
    repeated = False

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal repeated
        repeated = repeated or len({name for name, _ in items}) != len(items)
        return dict(items)

    try:
        json.loads(body, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        return False
    return repeated


RUN_ID_FIELD: Final = Field(
    pattern=r"^[0-9a-f]{32}$",
    description=(
        "A new id of 32 lowercase hexadecimal characters, made by the caller for each request, "
        "for example uuid.uuid4().hex. A retry sends the same id."
    ),
)
ISSUED_AT_FIELD: Final = Field(
    description=(
        "When the request was made, in ISO 8601 with its offset, made once for each request "
        "and sent unchanged on a retry. A new request made a week or more ago starts nothing."
    )
)


class HerPlanRequest(BaseModel):
    """A request for today's plan sent as JSON: the id and issue time the caller made for it,
    the evening it is for, and the newest plan the caller knows for that evening."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = RUN_ID_FIELD
    evening: str = Field(description="The household's today, as YYYY-MM-DD.")
    issued_at: str = ISSUED_AT_FIELD
    newest_plan: str = Field(
        description=(
            "The draft_id of today's newest plan, from GET /student/plans/today, or an empty "
            "string when that answers 404."
        )
    )


class FamilyPlanRequest(BaseModel):
    """A request for an evening's plan sent as JSON: the id and issue time the caller made for
    it, the evening to plan, and the newest plan the caller knows for that evening."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = RUN_ID_FIELD
    plan_date: str = Field(description="The evening to plan, today or later, as YYYY-MM-DD.")
    issued_at: str = ISSUED_AT_FIELD
    newest_plan: str = Field(
        description=(
            "The draft_id listed for that evening in newest_plans from GET /parent/approvals, "
            "or an empty string when the evening isn't listed."
        )
    )


HerPlanCode = Literal[
    "not_valid", "another_evening", "expired", "used_for_another_evening", "newer_plan"
]
FamilyPlanCode = Literal[
    "not_valid", "expired", "used_for_another_evening", "newer_plan", "passed", "beyond_calendar"
]


class PlanRefusalView(BaseModel):
    """Why a JSON plan request started nothing: a code, a sentence, the household's today where
    the refusal turns on it, and, for an id used for another evening, where that id's run
    stands. Each route declares its own codes."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    today: date | None = None
    run: RunStatusView | None = None


class HerPlanRefusalView(PlanRefusalView):
    """Why her JSON plan request started nothing, by one of her route's codes."""

    code: HerPlanCode


class FamilyPlanRefusalView(PlanRefusalView):
    """Why the family's JSON plan request started nothing, by one of its route's codes."""

    code: FamilyPlanCode


class HerPlanRefusal(BaseModel):
    """Her JSON plan request refused, as its body carries it."""

    model_config = ConfigDict(extra="forbid")

    detail: HerPlanRefusalView


class FamilyPlanRefusal(BaseModel):
    """The family's JSON plan request refused, as its body carries it."""

    model_config = ConfigDict(extra="forbid")

    detail: FamilyPlanRefusalView


REQUEST_FIELDS: Final = (
    "issued_at, the time the request was made, in ISO 8601 with its offset and no more than "
    "five minutes ahead of the server's clock; and newest_plan, the draft_id of the newest plan "
    "known for that evening, or an empty string for none."
)
NOT_HER_PLAN: Final = HerPlanRefusalView(
    code="not_valid",
    message=(
        "Blossom couldn't use that plan request. A plan request needs run_id, a new id of 32 "
        "lowercase hexadecimal characters for each request; evening, the household's today as "
        f"YYYY-MM-DD; {REQUEST_FIELDS} No plan was started."
    ),
)
NOT_A_FAMILY_PLAN: Final = FamilyPlanRefusalView(
    code="not_valid",
    message=(
        "Blossom couldn't use that plan request. A plan request needs run_id, a new id of 32 "
        "lowercase hexadecimal characters for each request; plan_date, the evening to plan as "
        f"YYYY-MM-DD; {REQUEST_FIELDS} No plan was started."
    ),
)
A_WEEK_OLD: Final = "That request was made a week or more ago. No new plan was started."
"""The JSON refusal of a new request made ``FORM_LIFETIME`` or more before it was handled."""


def a_week_old[Refusal: PlanRefusalView](kind: type[Refusal]) -> Refusal:
    """A new JSON request made a week or more ago, refused in ``kind``, its route's own."""
    return kind(code="expired", message=A_WEEK_OLD)


def not_her_evening(evening: date, today: date) -> HerPlanRefusalView:
    """Her JSON request for an evening that isn't today, naming today."""
    return HerPlanRefusalView(
        code="another_evening",
        message=(
            f"That request was for {evening.isoformat()}, not today, {today.isoformat()}. "
            "No new plan was started."
        ),
        today=today,
    )


def evening_refused(
    code: Literal["passed", "beyond_calendar"], said: Sentences, today: date
) -> FamilyPlanRefusalView:
    """The family's JSON request for an evening that has passed or is past the calendar's edge."""
    return FamilyPlanRefusalView(
        code=code, message=" ".join((*said, "No plan was started.")), today=today
    )


def json_newer_plan[Refusal: PlanRefusalView](evening: date, kind: type[Refusal]) -> Refusal:
    """A JSON request whose newest_plan isn't the evening's newest, refused at admission."""
    return kind(
        code="newer_plan",
        message=(
            f"A newer plan for {evening.isoformat()} was made than the one named in newest_plan. "
            "No plan was started."
        ),
    )


def plan_refusal(refused: PlanRefusalView) -> JSONResponse:
    """A JSON plan request that started nothing: 422 for a body that isn't one, 409 otherwise."""
    code = (
        status.HTTP_422_UNPROCESSABLE_CONTENT
        if refused.code == "not_valid"
        else status.HTTP_409_CONFLICT
    )
    return JSONResponse({"detail": refused.model_dump(mode="json")}, status_code=code)


class PlanRoute(APIRoute):
    """A JSON plan route: a body that isn't one of its requests, a field missing, of another
    shape, extra or sent twice, is answered with its one refusal, 422, never the framework's
    list of errors. Who may press is the household gate's to say, before this reads anything."""

    not_a_request: ClassVar[PlanRefusalView]

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """The framework's handler, with a body that isn't a request refused as one."""
        handle = super().get_route_handler()
        refused = self.not_a_request

        async def coded(request: Request) -> Response:
            if repeats_a_field(await request.body()):
                return plan_refusal(refused)
            try:
                return await handle(request)
            except RequestValidationError:
                return plan_refusal(refused)

        return coded


class HerPlanRoute(PlanRoute):
    """``POST /student/plans``."""

    not_a_request = NOT_HER_PLAN


class FamilyPlanRoute(PlanRoute):
    """``POST /parent/plans``."""

    not_a_request = NOT_A_FAMILY_PLAN


class RunUnread(Exception):
    """The record couldn't be read in time to say whether a JSON request's run_id names a
    run."""


async def run_of_the_request(state: ApplicationState, run_id: str) -> RunState | None:
    """The run a JSON request's id names, read without ending anything, on a worker thread for
    at most the store's wait; ``None`` when no run has that id. ``RunUnread`` when the read
    fails or runs late, so the route never takes a used id for an unused one."""
    try:
        return await bounded(
            partial(state.drafts.run_status, run_id, STORE_WAIT_SECONDS, reconcile=False),
            STORE_WAIT_SECONDS,
        )
    except Exception as error:
        logger.warning("run %s could not be read for a request: %s", run_id, type(error).__name__)
        raise RunUnread(run_id) from error


def unread_outcome() -> HTTPException:
    """What a JSON request answers when its run_id couldn't be looked up and it would otherwise
    be refused as a new request: 503, unconfirmed, since the id may name a run."""
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNCONFIRMED)


async def answered_by_its_run(
    state: ApplicationState, run: RunState, evening: date, kind: type[PlanRefusalView]
) -> JSONResponse:
    """A JSON request whose id names a run: that run's current status, read again from the
    record, 200, when the request is for the run's evening, at any age; the id-conflict
    refusal, in ``kind``, naming the run's evening and where it stands, when it is for
    another."""
    stands = await run_status_view(state, run.run_id)
    if run.plan_date != evening:
        return plan_refusal(
            kind(
                code="used_for_another_evening",
                message=(
                    f"This run_id was used for {run.plan_date.isoformat()}. No plan was started "
                    f"for {evening.isoformat()}."
                ),
                run=stands,
            )
        )
    return JSONResponse(stands.model_dump(mode="json"), status_code=status.HTTP_200_OK)


def expired_request(issued_at: datetime, now: datetime) -> bool:
    """Whether a JSON request was made ``FORM_LIFETIME`` or more before ``now``."""
    return now - issued_at >= FORM_LIFETIME


UNDECODABLE: Final = (
    "A body the framework can't decode at all, in its own answer: one not in a JSON text "
    "encoding, or nested too deeply. A body that decodes but isn't a request is not_valid, 422."
)
"""The 400 every JSON route here declares, the signal's included."""


def plan_request_answers(
    refusal: type[BaseModel], codes: str, forbidden: dict[str, Any]
) -> dict[int | str, dict[str, Any]]:
    """Every body one JSON plan route answers with besides a new plan: ``refusal`` is its
    refusal body, ``codes`` names its 409 codes, and ``forbidden`` is its 403."""
    return {
        status.HTTP_200_OK: {
            "model": RunStatusView,
            "description": (
                "A retry: the run_id names a run for this evening, and this is where it stands "
                "now. Nothing was started."
            ),
        },
        status.HTTP_202_ACCEPTED: {"model": UnconfirmedRunView},
        status.HTTP_400_BAD_REQUEST: {
            "model": ProblemView,
            "description": f"{UNDECODABLE} No plan was started.",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": ProblemView,
            "description": "No sign-in, with the household sign-in on. Nothing was read.",
        },
        status.HTTP_403_FORBIDDEN: forbidden,
        status.HTTP_409_CONFLICT: {
            "model": PlanConflictView | refusal,
            "description": (
                f"A refusal with a code, made before anything started: {codes}. Or, as a "
                "sentence, why a run made no plan, or the run still running."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": refusal,
            "description": "not_valid: a body that isn't a plan request. No plan was started.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "The server's own error, in plain text, as for any route.",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ProblemView,
            "description": (
                "In a sentence: no model key, a run that couldn't start, or a plan made but not "
                "saved. Or the unconfirmed sentence when the record couldn't be read in time to "
                "say whether the run_id names a run and a new request's refusal would otherwise "
                "follow; its outcome is unknown, so retry with the same run_id and issued_at."
            ),
        },
    }


ANOTHER_ORIGIN: Final = {
    "description": "A request from another origin, in plain text.",
    "content": {"text/plain": {"schema": {"type": "string"}}},
}
HER_PLAN_ANSWERS: Final = plan_request_answers(
    HerPlanRefusal,
    "another_evening, expired, used_for_another_evening or newer_plan",
    ANOTHER_ORIGIN,
)
FAMILY_PLAN_ANSWERS: Final = plan_request_answers(
    FamilyPlanRefusal,
    "expired, used_for_another_evening, newer_plan, passed or beyond_calendar",
    {
        "model": ProblemView,
        "description": (
            "A student's sign-in, before the body is read; or, in plain text, a request from "
            "another origin."
        ),
        "content": {"text/plain": {"schema": {"type": "string"}}},
    },
)
"""Every body each JSON plan route answers with besides a new plan."""
