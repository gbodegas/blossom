"""The tool boundary, both layers, and the approval gate.

Construction: a framework tool object exists only for a spec that passed the
capability allowlist, and it returns a draft.

Runtime: the backstop refuses any tool call whose name the registry does not
know, without invoking anything, and passes registered calls through.

Gate: the graph pauses with the draft, resumes with a decision, records it in
state, and re-runs nothing that came before the pause.
"""

from typing import Any, cast

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from blossom.agent.boundary import REGISTERED, boundary_middleware, is_permitted, tool_boundary
from blossom.agent.gates import ApprovalState, build_approval_graph, require_human_approval
from blossom.drafts import Draft, DraftStatus
from blossom.tools import TOOL_REGISTRY, ToolSpec, as_langchain_tool, create_draft, langchain_tools

# ---------------------------------------------------------------- construction


def test_a_registered_spec_becomes_a_framework_tool_that_returns_a_draft() -> None:
    tool = as_langchain_tool(TOOL_REGISTRY[0])

    result = tool.invoke({"body": "Could Maya have until Friday for the essay?"})

    draft = Draft.model_validate_json(result)
    assert draft.body == "Could Maya have until Friday for the essay?"
    assert draft.status is DraftStatus.DRAFT


def test_a_spec_with_an_unlisted_capability_cannot_become_a_framework_tool() -> None:
    invented = ToolSpec(
        name="submit_registration",
        description="A tool nobody registered.",
        capabilities=frozenset({"submit"}),
        call=create_draft,
        args_schema=TOOL_REGISTRY[0].args_schema,
    )

    with pytest.raises(ValueError, match="submit"):
        as_langchain_tool(invented)


def test_the_framework_validates_arguments_against_the_schema() -> None:
    tool = as_langchain_tool(TOOL_REGISTRY[0])

    with pytest.raises(Exception, match="body"):
        tool.invoke({"recipient": "teacher@school.example"})


def test_langchain_tools_mirrors_the_registry_exactly() -> None:
    assert [tool.name for tool in langchain_tools()] == [spec.name for spec in TOOL_REGISTRY]


# ------------------------------------------------------------------- runtime


def request_for(name: str, arguments: dict[str, Any] | None = None) -> ToolCallRequest:
    """A tool call as the framework would hand it to middleware."""
    call = {"name": name, "args": arguments or {}, "id": f"call-{name}", "type": "tool_call"}
    return ToolCallRequest(
        tool_call=cast(Any, call), tool=None, state={}, runtime=cast(Any, None)
    )


def test_a_registered_call_passes_through_to_the_handler() -> None:
    seen: list[str] = []

    def handler(request: ToolCallRequest) -> ToolMessage:
        seen.append(str(request.tool_call["name"]))
        return ToolMessage(content="ran", tool_call_id="call-create_manual_draft")

    request = request_for("create_manual_draft", {"body": "x"})
    result = tool_boundary.wrap_tool_call(request, handler)

    assert seen == ["create_manual_draft"]
    assert isinstance(result, ToolMessage)
    assert result.content == "ran"


@pytest.mark.parametrize("name", ["send_email", "submit_application", "register_for_test"])
def test_an_unregistered_call_is_refused_without_running_anything(name: str) -> None:
    def handler(request: ToolCallRequest) -> ToolMessage:
        raise AssertionError("the handler must never run for an unregistered tool")

    result = tool_boundary.wrap_tool_call(request_for(name), handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "blocked" in str(result.content)
    assert name in str(result.content)


def test_permission_is_by_registry_membership_not_by_name_shape() -> None:
    """A plausible name is not enough; only registration counts."""
    assert is_permitted("create_manual_draft") is True
    assert is_permitted("create_manual_draft_v2") is False
    assert set(REGISTERED) == {spec.name for spec in TOOL_REGISTRY}


def test_the_middleware_instance_is_what_a_graph_receives() -> None:
    assert boundary_middleware() is tool_boundary


# ---------------------------------------------------------------------- gate


def thread(name: str) -> RunnableConfig:
    return {"configurable": {"thread_id": name}}


def start(draft: Draft) -> ApprovalState:
    return ApprovalState(draft=draft)


def resume(**answer: object) -> Command[Any]:
    return Command[Any](resume=dict(answer))


def test_the_gate_pauses_with_the_draft_and_nothing_else() -> None:
    graph = build_approval_graph(InMemorySaver())
    draft = Draft(body="Requesting an extension to Friday.")

    paused = graph.invoke(start(draft), config=thread("t1"))

    interrupts = paused["__interrupt__"]
    assert len(interrupts) == 1
    assert interrupts[0].value == {"draft_id": draft.draft_id, "body": draft.body}
    assert "decision" not in paused


def test_approval_marks_the_draft_for_manual_send_and_records_the_decision() -> None:
    graph = build_approval_graph(InMemorySaver())
    graph.invoke(start(Draft(body="Requesting an extension.")), config=thread("t2"))

    final = graph.invoke(resume(approved=True, reason="ok"), config=thread("t2"))

    assert final["draft"].status is DraftStatus.APPROVED_FOR_MANUAL_SEND
    assert final["decision"] == "approved"
    assert final["reason"] == "ok"


def test_rejection_leaves_the_draft_unapproved_and_keeps_the_reason() -> None:
    graph = build_approval_graph(InMemorySaver())
    graph.invoke(start(Draft(body="Requesting an extension.")), config=thread("t3"))

    final = graph.invoke(resume(approved=False, reason="tone is too formal"), config=thread("t3"))

    assert final["draft"].status is DraftStatus.DRAFT
    assert final["decision"] == "rejected"
    assert final["reason"] == "tone is too formal"


def test_a_malformed_resume_is_treated_as_rejection() -> None:
    graph = build_approval_graph(InMemorySaver())
    graph.invoke(start(Draft(body="x")), config=thread("t4"))

    final = graph.invoke(Command[Any](resume="yes"), config=thread("t4"))

    assert final["decision"] == "rejected"
    assert final["draft"].status is DraftStatus.DRAFT


def test_work_before_the_gate_runs_exactly_once_across_the_pause() -> None:
    """Resume re-runs the interrupted node from its start, so side effects must
    live in an earlier node. This pins that the earlier node is not re-run."""
    runs: list[int] = []

    def produce(state: ApprovalState) -> dict[str, Any]:
        runs.append(1)
        return {"draft": Draft(body=f"draft number {len(runs)}")}

    builder: StateGraph[ApprovalState, Any, ApprovalState, ApprovalState] = StateGraph(
        ApprovalState
    )
    builder.add_node("produce", produce)
    builder.add_node("require_human_approval", require_human_approval)
    builder.add_edge(START, "produce")
    builder.add_edge("produce", "require_human_approval")
    builder.add_edge("require_human_approval", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    graph.invoke(start(Draft(body="placeholder")), config=thread("t5"))
    final = graph.invoke(resume(approved=True), config=thread("t5"))

    assert runs == [1]
    assert final["draft"].body == "draft number 1"
    assert final["draft"].status is DraftStatus.APPROVED_FOR_MANUAL_SEND
