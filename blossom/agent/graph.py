"""The plan graph: gather the evening, propose, check, judge, revise within a bound, wait.

This is a workflow, not an agent. The planner and the critic are model calls
that each return one typed value and hold no tools, so there is no loop in
which a model decides what to call next. The graph decides, from the checks
and the verdict, and every route it can take is written in this file.

The loop runs in a fixed order. The planner proposes a ``DailyPlan``. Tier one
checks it, and a plan that fails goes back to the planner with the findings
before any critic sees it: a judgment about a plan that is already wrong is a
wasted call, and a check is cheaper than a model. A plan that passes goes to
the critic, whose verdict is tier two. A critic that finds fault sends the plan
back with its critique. One that cannot tell sends it forward, because that
answer is addressed to a person. After ``MAX_REVISIONS`` the loop stops
whatever the critic thinks and the plan goes forward with the critique
attached, since tier two informs the gate and never closes it. A plan that
still fails tier one at the bound goes nowhere: it is reported, not proposed.

The model can end the run on its own. A response cut off at the token limit, a
refusal, or a body the schema cannot parse each ends the graph with an outcome
naming which, and no draft, because guessing at a truncated plan would be
worse than having none.

Saved state holds the evening, the plan, what was found about it, and the
draft. The stores and the two model callables are closed over by the node
functions rather than carried in state, so nothing about the process is
written to disk and nothing in the state needs a class the serializer does not
list.

Two nodes write to the drafts table, and each performs that one side effect
in a form that running twice leaves unchanged. ``compose`` saves the draft as
waiting, keyed by an id derived from the thread, before the gate can pause on
it, so the parent's queue shows it. ``record_decision``, after the gate, saves
what the person decided. The table is the record across threads; saved state
is the record within one.
"""

import operator
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Final, Literal, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from blossom.agent.compose import compose_draft
from blossom.agent.gates import ApprovalState, require_human_approval
from blossom.agent.prompts import critic_brief, planner_brief
from blossom.anthropic_client import (
    MISSING_KEY,
    Effort,
    ModelUnavailable,
    chat_model,
    model_configured,
)
from blossom.dependencies import ApplicationState
from blossom.drafts import Decision, Draft
from blossom.heuristic_relevance import CriticVerdict
from blossom.noticing import Noticing, expect_due_date, in_week, notice_due_date
from blossom.plan_checks import DEFAULT_DAILY_MINUTES, PlanVerification, check_plan
from blossom.plans import DailyPlan
from blossom.reconciliation import Reconciler, SourceConfidence, classify_confidence
from blossom.settings import Settings
from blossom.sources import StateSource
from blossom.stores.drafts import DraftsStore
from blossom.stores.project_state import Assignment, ProjectStateStore
from blossom.stores.reflections import ReflectionsStore
from blossom.stores.support_rules import SupportRulesStore

MAX_REVISIONS: Final = 2
"""How many times the planner may be sent back. It runs at most one more time than this."""

NODES_PER_ROUND: Final = 3
"""Plan, verify, critique: the most nodes one round of the loop can run."""

NODES_OUTSIDE_THE_LOOP: Final = 4
"""Retrieve before it; compose, the gate, and the decision record after it."""

WORST_CASE_SUPERSTEPS: Final = NODES_OUTSIDE_THE_LOOP + (MAX_REVISIONS + 1) * NODES_PER_ROUND
"""The longest run this graph can take. It must fit under the recursion limit
with room to spare, or the limit would end a legitimate run before the bound
does."""

PLANNER_EFFORT: Final[Effort] = "high"
CRITIC_EFFORT: Final[Effort] = "medium"

Outcome = Literal[
    "accepted",
    "unsettled",
    "checks_failed",
    "model_truncated",
    "model_refused",
    "model_unparseable",
]
"""Why the run stopped. The first two reached the gate; the rest did not.

``accepted`` means the checks passed and the critic agreed. ``unsettled`` means
the checks passed and the critic did not agree, could not tell, or ran out of
rounds: the plan went to the gate with the critique attached. ``checks_failed``
means no plan within the bound passed tier one, so nothing was proposed. The
three ``model_`` outcomes name how the model ended the run itself."""

REACHED_THE_GATE: Final[frozenset[str]] = frozenset({"accepted", "unsettled"})


@dataclass(frozen=True)
class ModelAnswer[T: BaseModel]:
    """What one structured model call came back with, in the terms the graph reads.

    ``stop_reason`` is the provider's word for how the response ended. It is
    read before ``parsed`` because a truncated response can still parse: a plan
    cut off after two of three blocks is valid JSON and a wrong plan.
    """

    parsed: T | None
    stop_reason: str | None
    parsing_error: str | None

    @classmethod
    def from_structured(cls, answer: Mapping[str, Any]) -> "ModelAnswer[T]":
        """Read the ``include_raw`` shape: ``raw``, ``parsed``, ``parsing_error``."""
        raw = answer.get("raw")
        stop_reason = (
            raw.response_metadata.get("stop_reason") if isinstance(raw, AIMessage) else None
        )
        error = answer.get("parsing_error")
        return cls(
            parsed=answer.get("parsed"),
            stop_reason=str(stop_reason) if stop_reason is not None else None,
            parsing_error=str(error) if error is not None else None,
        )

    def failure(self) -> Outcome | None:
        """The outcome this answer forces, or ``None`` when it can be used."""
        if self.stop_reason == "max_tokens":
            return "model_truncated"
        if self.stop_reason == "refusal":
            return "model_refused"
        if self.parsed is None:
            return "model_unparseable"
        return None


type Ask[T: BaseModel] = Callable[[Sequence[BaseMessage]], Awaitable[ModelAnswer[T]]]
"""One structured model call: messages in, a typed answer out. The graph holds
two, and a test substitutes both."""


class PlanState(TypedDict):
    """Everything a run of the plan graph saves.

    ``rounds`` counts how many times the planner has run and is the only key
    with a reducer. The gate's three keys are named exactly as in
    ``ApprovalState`` so the same node serves both graphs.
    """

    plan_date: date
    rounds: Annotated[int, operator.add]
    assignments: NotRequired[list[Assignment]]
    confidence: NotRequired[dict[str, SourceConfidence]]
    noticings: NotRequired[list[Noticing]]
    support_rules: NotRequired[list[str]]
    reflections: NotRequired[list[str]]
    feedback: NotRequired[list[str]]
    plan: NotRequired[DailyPlan]
    verification: NotRequired[PlanVerification]
    verdict: NotRequired[CriticVerdict]
    outcome: NotRequired[Outcome]
    draft: NotRequired[Draft]
    decision: NotRequired[Decision]
    reason: NotRequired[str | None]


type CompiledPlanGraph = CompiledStateGraph[PlanState, Any, PlanState, PlanState]


def build_plan_graph(
    *,
    project_state: ProjectStateStore,
    source: StateSource,
    support_rules: SupportRulesStore,
    reflections: ReflectionsStore,
    drafts: DraftsStore,
    zone: ZoneInfo,
    planner: Ask[DailyPlan],
    critic: Ask[CriticVerdict],
    checkpointer: BaseCheckpointSaver[Any],
    daily_minutes: int = DEFAULT_DAILY_MINUTES,
) -> CompiledPlanGraph:
    """Wire the graph around one household's stores and two model callables.

    Everything a node needs beyond the state is bound here, so the state stays
    data. A checkpointer is required because the gate cannot pause without one.
    """
    reconciler = Reconciler()

    def evening(state: PlanState) -> dict[str, Any]:
        """The arguments both briefs share, read from state."""
        return {
            "plan_date": state["plan_date"],
            "zone": zone.key,
            "budget_minutes": daily_minutes,
            "assignments": state.get("assignments", []),
            "confidence": state.get("confidence", {}),
            "noticings": state.get("noticings", []),
            "support_rules": state.get("support_rules", []),
            "reflections": state.get("reflections", []),
        }

    def retrieve(state: PlanState) -> dict[str, Any]:
        """Read the week from the stores. Whole corpora, no index: they are small.

        Every assignment on record has its due date stated before its sources
        are read, then set against them. The week is selected after that, so
        a date a source gives can put an item in it that the record alone
        would leave out. What the sources say also decides how far the family
        can trust each date.
        """
        everything = project_state.all_assignments()
        expectations = [expect_due_date(item) for item in everything]
        records = {
            item.assignment_id: source.deadline_records(item.assignment_id) for item in everything
        }
        noticed = {
            expectation.assignment_id: notice_due_date(
                expectation, records[expectation.assignment_id]
            )
            for expectation in expectations
        }
        assignments = [
            item
            for item in everything
            if in_week(item, noticed[item.assignment_id], state["plan_date"])
        ]
        confidence = {
            item.assignment_id: classify_confidence(
                reconciler.reconcile(records[item.assignment_id])
            )
            for item in assignments
        }
        noticings = [noticed[item.assignment_id] for item in assignments]
        return {
            "assignments": assignments,
            "confidence": confidence,
            "noticings": noticings,
            "support_rules": [rule.instruction for rule in support_rules.list_all()],
            "reflections": [note.observation for note in reflections.list_all()],
        }

    async def plan(state: PlanState) -> dict[str, Any]:
        """Ask the planner. Counts the round whether or not a plan comes back."""
        messages = planner_brief(
            **evening(state),
            feedback=state.get("feedback", []),
            round_number=state["rounds"] + 1,
        )
        answer = await planner(messages)
        failure = answer.failure()
        if failure is not None:
            return {"rounds": 1, "outcome": failure}
        return {"rounds": 1, "plan": answer.parsed}

    def verify(state: PlanState) -> dict[str, Any]:
        """Tier one. A failing plan becomes feedback, or the end when rounds are spent."""
        verification = check_plan(
            state["plan"],
            due_in_window=state.get("assignments", []),
            zone=zone,
            confidence=state.get("confidence", {}),
            noticings=state.get("noticings", []),
            daily_minutes=daily_minutes,
        )
        if verification.passed:
            return {"verification": verification, "feedback": []}
        update: dict[str, Any] = {
            "verification": verification,
            "feedback": list(verification.as_findings()),
        }
        if state["rounds"] > MAX_REVISIONS:
            update["outcome"] = "checks_failed"
        return update

    async def critique(state: PlanState) -> dict[str, Any]:
        """Tier two. Fault sends the plan back; doubt or spent rounds send it forward."""
        messages = critic_brief(
            **evening(state), plan=state["plan"], verification=state["verification"]
        )
        answer = await critic(messages)
        failure = answer.failure()
        verdict = answer.parsed
        if failure is not None or verdict is None:
            return {"outcome": failure or "model_unparseable"}
        if verdict.accepted:
            return {"verdict": verdict, "outcome": "accepted"}
        if verdict.failed and state["rounds"] <= MAX_REVISIONS:
            feedback = [f"{item.criterion}: {item.critique}" for item in verdict.failed]
            return {"verdict": verdict, "feedback": feedback}
        return {"verdict": verdict, "outcome": "unsettled"}

    def compose(state: PlanState, config: RunnableConfig) -> dict[str, Any]:
        """Render the plan as the text a person reads at the gate, and save it as waiting.

        The draft id comes from the thread, so this node run twice yields the
        same draft and the same row. Saving happens here, before the gate,
        because the gate must do nothing before it pauses and the queue must
        show the draft while it waits.
        """
        thread_id = str(config["configurable"]["thread_id"])
        outcome = state["outcome"]
        draft = compose_draft(
            draft_id=f"draft:{thread_id}",
            plan=state["plan"],
            assignments=state.get("assignments", []),
            verification=state["verification"],
            verdict=state.get("verdict"),
            settled=outcome == "accepted",
            noticings=state.get("noticings", []),
        )
        if outcome not in REACHED_THE_GATE:
            msg = f"compose reached with outcome {outcome!r}, which produces no draft"
            raise RuntimeError(msg)
        drafts.record_waiting(
            draft,
            thread_id=thread_id,
            plan_date=state["plan_date"],
            outcome=cast(Literal["accepted", "unsettled"], outcome),
        )
        return {"draft": draft}

    def gate(state: PlanState) -> dict[str, Any]:
        """The approval gate, unchanged; the state's gate keys match its own."""
        return require_human_approval(cast(ApprovalState, state))

    def record_decision(state: PlanState) -> dict[str, Any]:
        """Save the decision to the table. Writes nothing to state; the gate already did."""
        draft = state["draft"]
        drafts.record_decision(
            draft.draft_id,
            status=draft.status,
            decision=state["decision"],
            reason=state.get("reason"),
        )
        return {}

    def after_plan(state: PlanState) -> str:
        return END if "outcome" in state else "verify"

    def after_verify(state: PlanState) -> str:
        if state["verification"].passed:
            return "critique"
        return END if "outcome" in state else "plan"

    def after_critique(state: PlanState) -> str:
        outcome = state.get("outcome")
        if outcome is None:
            return "plan"
        return "compose" if outcome in REACHED_THE_GATE else END

    graph: StateGraph[PlanState, Any, PlanState, PlanState] = StateGraph(PlanState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("plan", plan)
    graph.add_node("verify", verify)
    graph.add_node("critique", critique)
    graph.add_node("compose", compose)
    graph.add_node("require_human_approval", gate)
    graph.add_node("record_decision", record_decision)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "plan")
    graph.add_conditional_edges("plan", after_plan, {"verify": "verify", END: END})
    graph.add_conditional_edges(
        "verify", after_verify, {"critique": "critique", "plan": "plan", END: END}
    )
    graph.add_conditional_edges(
        "critique", after_critique, {"compose": "compose", "plan": "plan", END: END}
    )
    graph.add_edge("compose", "require_human_approval")
    graph.add_edge("require_human_approval", "record_decision")
    graph.add_edge("record_decision", END)
    return graph.compile(checkpointer=checkpointer)


def structured[T: BaseModel](settings: Settings, schema: type[T], *, effort: Effort) -> Ask[T]:
    """One role's model call: the seam's client, asked for ``schema`` and nothing else.

    ``json_schema`` makes the provider constrain the output to the schema
    rather than routing it through a tool call, so a plan never arrives as a
    tool invocation and the tool boundary has nothing to inspect here.
    ``include_raw`` keeps the message the answer came in, which is where the
    stop reason lives.
    """
    chain = chat_model(settings, effort=effort).with_structured_output(
        schema, method="json_schema", include_raw=True
    )

    async def ask(messages: Sequence[BaseMessage]) -> ModelAnswer[T]:
        answer = await chain.ainvoke(list(messages))
        return ModelAnswer.from_structured(cast(Mapping[str, Any], answer))

    return ask


def unavailable[T: BaseModel]() -> Ask[T]:
    """A model callable for a graph that may be resumed but not started.

    Without a key the graph is still built, so a thread paused at the gate can
    be resumed and its decision recorded on a machine that has no key: nothing
    past the gate asks a model. Asking raises the error the seam would have
    raised, so a start without a key fails with the reason.
    """

    async def ask(messages: Sequence[BaseMessage]) -> ModelAnswer[T]:
        raise ModelUnavailable(MISSING_KEY)

    return ask


def plan_graph_for(
    state: ApplicationState,
    *,
    planner: Ask[DailyPlan] | None = None,
    critic: Ask[CriticVerdict] | None = None,
) -> CompiledPlanGraph:
    """The graph for the running application, with the seam's models unless given others.

    Always builds. With no key and no substitute, the two model callables
    raise ``ModelUnavailable`` when asked, which a start does at its first node
    and a resume never does. Callers that start a run check the key first, so
    the refusal arrives before any thread is written.
    """
    configured = model_configured(state.settings)
    if planner is None:
        planner = (
            structured(state.settings, DailyPlan, effort=PLANNER_EFFORT)
            if configured
            else unavailable()
        )
    if critic is None:
        critic = (
            structured(state.settings, CriticVerdict, effort=CRITIC_EFFORT)
            if configured
            else unavailable()
        )
    return build_plan_graph(
        project_state=state.project_state,
        source=state.source,
        support_rules=state.support_rules,
        reflections=state.reflections,
        drafts=state.drafts,
        zone=state.clock.zone,
        planner=planner,
        critic=critic,
        checkpointer=state.checkpointer,
    )
