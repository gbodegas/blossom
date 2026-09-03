"""Human approval gates: where the graph stops and waits for a person.

Anything that would leave the family takes two human steps. A person reviews
the draft and decides, and a person transmits it by hand, because no tool can.
This module is the first step. The gate is a graph node that pauses with the
draft and resumes with the decision; the graph cannot continue past it on its
own, and the decision is recorded in checkpointed state.

One rule shapes the node: nothing happens before ``interrupt``. When a paused
graph resumes, the framework re-runs the interrupted node from its start, so
any side effect placed ahead of the pause would run twice. The node reads
state, pauses, and writes the outcome. Everything that produces the draft lives
in earlier nodes, which run exactly once.
"""

from typing import Any, Literal, NotRequired, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from blossom.drafts import Draft, DraftStatus

Decision = Literal["approved", "rejected"]


class ApprovalState(TypedDict):
    """State carried through the gate: the draft, then the decision about it."""

    draft: Draft
    decision: NotRequired[Decision]
    reason: NotRequired[str | None]


def require_human_approval(state: ApprovalState) -> dict[str, Any]:
    """Pause with the draft; resume with ``{"approved": bool, "reason": str}``.

    Approval marks the draft ``APPROVED_FOR_MANUAL_SEND``, which is the whole
    of what approval does. The draft still leaves the system only when a person
    copies it out.
    """
    draft = state["draft"]
    answer = interrupt({"draft_id": draft.draft_id, "body": draft.body})
    # Only the boolean True approves. A string such as "false" or "no", a
    # number, a missing key, or a payload that is not a dictionary all reject,
    # because a gate that guesses at a person's meaning fails open.
    approved = isinstance(answer, dict) and answer.get("approved") is True
    reason = answer.get("reason") if isinstance(answer, dict) else None
    if approved:
        draft = draft.model_copy(update={"status": DraftStatus.APPROVED_FOR_MANUAL_SEND})
    return {
        "draft": draft,
        "decision": "approved" if approved else "rejected",
        "reason": reason if isinstance(reason, str) else None,
    }


def build_approval_graph(
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[ApprovalState, Any, ApprovalState, ApprovalState]:
    """A graph that is only the gate. Larger graphs add the node the same way.

    A checkpointer is required, not optional: an interrupt with nowhere to
    save its state cannot be resumed.
    """
    graph: StateGraph[ApprovalState, Any, ApprovalState, ApprovalState] = StateGraph(ApprovalState)
    graph.add_node("require_human_approval", require_human_approval)
    graph.add_edge(START, "require_human_approval")
    graph.add_edge("require_human_approval", END)
    return graph.compile(checkpointer=checkpointer)
