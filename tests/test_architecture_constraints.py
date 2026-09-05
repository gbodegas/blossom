import ast
import pathlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.app import create_app
from blossom.reconciliation import Disagreement, Reconciler, SourceChannel, SourceRecord
from blossom.retrieval import (
    NothingRetrieved,
    RetrievalQuery,
    RetrievalResult,
    RetrievalRouter,
    SemanticRetriever,
)
from blossom.stores.reflections import Reflection, ReflectionsStore, ReflectionSubject
from blossom.tools import TOOL_REGISTRY
from blossom.views import ParentCheckpointView, StudentAssignmentView, VerifierClaimView
from tests.support import fixture_settings


class RecordingRetriever:
    store_name = "recording"

    def __init__(self, response: RetrievalResult | NothingRetrieved) -> None:
        self.calls: list[RetrievalQuery] = []
        self._response = response

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult | NothingRetrieved:
        self.calls.append(query)
        return self._response


def test_tool_registry_declares_no_transmitting_capability() -> None:
    banned = {"send", "email", "sms", "webhook", "transmit", "smtp"}
    for tool in TOOL_REGISTRY:
        assert tool.capabilities.isdisjoint(banned)


def test_package_contains_no_transmitting_imports_or_calls() -> None:
    package_root = pathlib.Path("blossom")
    banned_patterns = [
        re.compile(part)
        for part in [
            "smtp" + "lib",
            "send" + "mail",
            r"requests\.post\((?![^)]*(localhost|127\.0\.0\.1))",
            r"httpx\.post\((?![^)]*(localhost|127\.0\.0\.1))",
            r"urllib\.request\.urlopen",
            r"__import__\(",
            r"\bimportlib\b",
        ]
    ]
    for path in package_root.rglob("*.py"):
        content = path.read_text()
        for pattern in banned_patterns:
            assert not pattern.search(content), f"{path} matched {pattern.pattern}"


PACKAGE = pathlib.Path("blossom")

# Methods that hand tools to a model outside the agent factory, so outside the
# tool boundary's bind-time check. ``bind`` is the method ``bind_tools`` ends in.
MODEL_BINDING_METHODS = frozenset({"bind_tools", "bind"})
# Invocation methods whose keywords go straight into the request.
INVOKE_METHODS = frozenset({"invoke", "ainvoke", "stream", "astream", "batch", "abatch"})
# Request keys that would bind a tool, a server-side tool, or a beta at an invoke site.
FOREIGN_REQUEST_KEYS = frozenset({"tools", "mcp_servers", "betas", "model_kwargs"})
# Structured output binds a tool internally; only the graph module may use it.
STRUCTURED_OUTPUT_FILES = frozenset({"agent/graph.py"})
# Names that may appear only where the boundary is defined.
BOUNDARY_ONLY_NAMES = frozenset({"boundary_middleware", "tool_boundary"})


def callee(func: ast.expr) -> str | None:
    """The simple name a call is made through, if it has one."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def binding_violations(source: str, relative: str) -> list[str]:
    """Places where ``source`` hands tools to a model outside the agent factory."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        names: set[str] = set()
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Constant) and node.value == "bind_tools":
            names.add("bind_tools")
        for name in sorted(names & MODEL_BINDING_METHODS):
            found.append(f"line {line}: {name}")
        if "with_structured_output" in names and relative not in STRUCTURED_OUTPUT_FILES:
            found.append(f"line {line}: with_structured_output outside the graph module")
        if isinstance(node, ast.Call) and callee(node.func) in INVOKE_METHODS:
            found.extend(f"line {line}: {reason}" for reason in foreign_request_keywords(node))
    return found


def foreign_request_keywords(call: ast.Call) -> list[str]:
    """Keywords at an invoke site that could carry tools into the request.

    A ``**`` expansion is inspected when it is a literal dictionary with string
    keys and refused outright when it is anything else, because a dynamic
    mapping cannot be shown not to contain ``tools``.
    """
    reasons: list[str] = []
    for keyword in call.keywords:
        if keyword.arg in FOREIGN_REQUEST_KEYS:
            reasons.append(f"{keyword.arg} passed to {callee(call.func)}")
        elif keyword.arg is None:
            value = keyword.value
            literal_keys: set[str] = {
                key.value
                for key in (value.keys if isinstance(value, ast.Dict) else [])
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if isinstance(value, ast.Dict) and len(literal_keys) == len(value.keys):
                for name in sorted(literal_keys & FOREIGN_REQUEST_KEYS):
                    reasons.append(f"{name} expanded into {callee(call.func)}")
            else:
                reasons.append(f"dynamic ** expansion into {callee(call.func)}")
    return reasons


def wiring_violations(source: str, relative: str) -> list[str]:
    """Agent construction that does not go through ``middleware_stack`` and ``chat_model``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call) and callee(node.func) == "create_agent":
            if any(keyword.arg is None for keyword in node.keywords):
                found.append(f"line {line}: create_agent with a ** expansion")
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            middleware = keywords.get("middleware")
            if not (
                isinstance(middleware, ast.Call) and callee(middleware.func) == "middleware_stack"
            ):
                found.append(f"line {line}: create_agent without middleware_stack(...)")
            # The model must visibly come from the seam. A name, a string, or any
            # other factory is refused, since the scan cannot follow what it holds.
            model = keywords.get("model")
            if not (isinstance(model, ast.Call) and callee(model.func) == "chat_model"):
                found.append(f"line {line}: create_agent without model=chat_model(...)")
        if relative == "agent/boundary.py":
            continue
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name
        if name in BOUNDARY_ONLY_NAMES:
            found.append(f"line {line}: {name} outside agent/boundary.py")
    return found


def test_tools_are_bound_only_through_the_agent_factory() -> None:
    """No module may bind tools to a model directly.

    The tool boundary's bind-time check runs only inside the framework's agent
    factory. A graph node that binds tools to a model itself never passes
    through it, and an import scan cannot see a method call, so the syntax tree
    of every package file is checked here.
    """
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        assert binding_violations(path.read_text(encoding="utf-8"), relative) == [], relative


@pytest.mark.parametrize(
    "snippet",
    [
        "model.bind_tools([tool])",
        "model.bind_tools (tools)",
        "getattr(model, 'bind_tools')(tools)",
        "bind = model.bind_tools",
        "model.bind(tools=[server_tool])",
        "model.invoke(prompt, tools=[server_tool])",
        "model.invoke(prompt, mcp_servers=[server])",
        "model.invoke(prompt, **{'tools': foreign_tools})",
        "model.invoke(prompt, **request_options)",
        "model.stream(prompt, **dict(tools=foreign_tools))",
        "async def run():\n    return await model.ainvoke(prompt, tools=[])",
        "model.with_structured_output(Verdict)",
    ],
)
def test_the_binding_scan_flags_each_bypass_form(snippet: str) -> None:
    """Positive control: the scan sees every spelling, not only ``.bind_tools(``."""
    assert binding_violations(snippet, "routes/anything.py")


def test_the_binding_scan_allows_structured_output_in_the_graph_module() -> None:
    assert binding_violations("model.with_structured_output(Verdict)", "agent/graph.py") == []


def test_the_binding_scan_allows_a_literal_expansion_without_request_keys() -> None:
    assert binding_violations("model.invoke(prompt, **{'config': config})", "agent/graph.py") == []


def test_agents_are_wired_through_the_stack_and_the_seam() -> None:
    """Every agent carries ``middleware_stack(...)`` and a client, never a model name,
    and the boundary's instance names stay inside the boundary module."""
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        assert wiring_violations(path.read_text(encoding="utf-8"), relative) == [], relative


@pytest.mark.parametrize(
    "snippet",
    [
        "create_agent(model=m, tools=t, middleware=[boundary_middleware(), Other()])",
        "create_agent(model=m, tools=t, middleware=[*middleware_stack(), Other()])",
        "create_agent(model=m, tools=t)",
        "create_agent(model='anthropic:claude-opus-5', tools=t, middleware=middleware_stack())",
        "create_agent(model=MODEL, tools=t, middleware=middleware_stack())",
        "create_agent(model=some_factory(), tools=t, middleware=middleware_stack())",
        "create_agent(tools=t, middleware=middleware_stack())",
        "create_agent(model=chat_model(s, effort='low'), tools=t, **options)",
        "from blossom.agent.boundary import tool_boundary",
    ],
)
def test_the_wiring_scan_flags_each_bypass_form(snippet: str) -> None:
    assert wiring_violations(snippet, "agent/graph.py")


def names_bound_to(source_tree: ast.AST, function: str) -> set[str]:
    """Variables assigned the result of calling ``function`` anywhere in the tree."""
    bound: set[str] = set()
    for node in ast.walk(source_tree):
        value = getattr(node, "value", None)
        if not (isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(value, ast.Call)):
            continue
        if callee(value.func) != function:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        bound.update(target.id for target in targets if isinstance(target, ast.Name))
    return bound


def run_contract_violations(source: str) -> list[str]:
    """Runs built from the shared configuration that do not set ``DURABILITY``.

    The framework takes durability as an argument beside the configuration and
    defaults to ``async``, which lets a crash lose the step that recorded a
    decision, so the two travel together. The configuration counts whether it
    is built inline, held in a variable, or passed positionally, and the value
    must be the constant: ``None`` and ``"async"`` both ask for the default.
    """
    tree = ast.parse(source)
    from_run_config = names_bound_to(tree, "run_config")
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        config = keywords.get("config")
        if config is None and callee(node.func) in INVOKE_METHODS and len(node.args) >= 2:
            config = node.args[1]
        built_here = isinstance(config, ast.Call) and callee(config.func) == "run_config"
        held = isinstance(config, ast.Name) and config.id in from_run_config
        if not (built_here or held):
            continue
        durability = keywords.get("durability")
        if not (isinstance(durability, ast.Name) and durability.id == "DURABILITY"):
            line = getattr(node, "lineno", 0)
            found.append(f"line {line}: a run from run_config without durability=DURABILITY")
    return found


def test_every_run_that_uses_the_shared_config_sets_its_durability() -> None:
    """Covers the tests as well as the package, so the rule is live today."""
    for directory in (PACKAGE, pathlib.Path("tests")):
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert run_contract_violations(source) == [], str(path)


@pytest.mark.parametrize(
    "snippet",
    [
        "await graph.ainvoke(state, config=run_config(thread))",
        "graph.invoke(state, config=run_config(thread))",
        "cfg = run_config(thread)\ngraph.invoke(state, config=cfg)",
        "graph.invoke(state, run_config(thread))",
        "graph.invoke(state, config=run_config(thread), durability=None)",
        "graph.invoke(state, config=run_config(thread), durability='async')",
    ],
)
def test_the_run_contract_scan_flags_each_way_of_losing_the_durability(snippet: str) -> None:
    assert run_contract_violations(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "await graph.ainvoke(state, config=run_config(thread), durability=DURABILITY)",
        "cfg = run_config(thread)\ngraph.invoke(state, config=cfg, durability=DURABILITY)",
        "graph.invoke(state, config=other_config())",
    ],
)
def test_the_run_contract_scan_accepts_the_sanctioned_form(snippet: str) -> None:
    assert run_contract_violations(snippet) == []


def test_the_wiring_scan_accepts_the_sanctioned_form() -> None:
    sanctioned = (
        "create_agent(model=chat_model(s, effort='low'), tools=t, "
        "middleware=middleware_stack(Limit()))"
    )
    assert wiring_violations(sanctioned, "agent/graph.py") == []


def test_retrieval_router_never_sends_keyed_query_to_semantic_path() -> None:
    structured_result = RetrievalResult(
        store_name="project_state",
        record_id="abc",
        source_channel="fixture",
        asserted_at=datetime.now(UTC),
    )
    structured = RecordingRetriever(structured_result)
    semantic = RecordingRetriever(NothingRetrieved(reason="should not be called"))
    router = RetrievalRouter(structured=structured, semantic=semantic)

    result = router.retrieve(RetrievalQuery(text="due this week", lookup_key="assignment:abc"))

    assert result == structured_result
    assert len(structured.calls) == 1
    assert semantic.calls == []


def test_below_threshold_semantic_query_returns_nothing() -> None:
    class LowScoreCollection:
        def query(self, *, query_texts: list[str], n_results: int) -> dict[str, list[list[object]]]:
            return {
                "ids": [["nearest-but-weak"]],
                "distances": [[0.6]],
                "metadatas": [[{"asserted_at": "2026-08-19T09:00:00"}]],
            }

    router = RetrievalRouter(
        structured=RecordingRetriever(NothingRetrieved(reason="unused")),
        semantic=SemanticRetriever(
            LowScoreCollection(),
            store_name="support_rules",
            source_channel="synthetic",
        ),
    )

    result = router.retrieve(RetrievalQuery(text="vague", min_score=0.75))

    assert isinstance(result, NothingRetrieved)
    assert result.reason == "semantic score below threshold"


def test_reflections_reject_non_system_subjects() -> None:
    store = ReflectionsStore()
    with pytest.raises(ValueError, match="system's own behavior"):
        store.write(
            Reflection(
                reflection_id="r1",
                subject=ReflectionSubject.STUDENT,
                observation="student seemed tired",
                observed_at=datetime.now(UTC),
            )
        )


def test_reconciler_preserves_all_four_conflicting_records() -> None:
    records = [
        SourceRecord(
            channel=channel,
            asserted_value=value,
            observed_at=datetime.now(UTC),
            confidence=0.5,
        )
        for channel, value in [
            (SourceChannel.LMS, "Monday"),
            (SourceChannel.EMAIL, "Tuesday"),
            (SourceChannel.PARENT_ENTRY, "Wednesday"),
            (SourceChannel.STUDENT_REPORT, "Thursday"),
        ]
    ]

    result = Reconciler().reconcile(records)

    assert isinstance(result, Disagreement)
    assert result.conflicting_claims == records


def test_empty_workload_signal_post_succeeds() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/student/workload-signals")

    assert response.status_code == 200
    assert response.json()["principal"] == "STUDENT"
    assert response.json()["detail_attached"] is False


def test_three_principal_views_are_distinct_and_forbid_absent_fields() -> None:
    assert "role" not in StudentAssignmentView.model_fields
    assert "role" not in ParentCheckpointView.model_fields
    assert "role" not in VerifierClaimView.model_fields
    with pytest.raises(ValidationError):
        ParentCheckpointView.model_validate(
            {
                "checkpoint_at": datetime.now(UTC),
                "assignments": [],
                "workload_signal_count": 3,
            }
        )


def test_student_due_this_week_renders_disagreement() -> None:
    """The clock is pinned because the fixtures carry fixed August 2026 dates."""
    settings = fixture_settings(BLOSSOM_TODAY="2026-08-19")

    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week")

    assert response.status_code == 200
    assert "Source disagreement" in response.text
    assert "LMS: 2026-08-21" in response.text
    assert "PARENT_ENTRY: 2026-08-22" in response.text
