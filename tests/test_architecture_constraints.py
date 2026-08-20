import pathlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blossom.agent.loop import AgentStep, compare_expectation_to_observation
from blossom.app import create_app
from blossom.principals import Principal
from blossom.reconciliation import Disagreement, Reconciler, SourceChannel, SourceRecord
from blossom.retrieval import (
    NothingRetrieved,
    RetrievalQuery,
    RetrievalResult,
    RetrievalRouter,
    SemanticRetriever,
)
from blossom.settings import Settings
from blossom.stores.reflections import Reflection, ReflectionsStore, ReflectionSubject
from blossom.tools import TOOL_REGISTRY
from blossom.verification import ORDERED_TIERS, VerificationTier, Verifier
from blossom.views import ParentCheckpointView, StudentAssignmentView, VerifierClaimView


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
        re.compile(part) for part in [
            "smtp" + "lib",
            "send" + "mail",
            r"requests\.post\((?![^)]*(localhost|127\.0\.0\.1))",
            r"httpx\.post\((?![^)]*(localhost|127\.0\.0\.1))",
            r"urllib\.request\.urlopen",
        ]
    ]
    for path in package_root.rglob("*.py"):
        content = path.read_text()
        for pattern in banned_patterns:
            assert not pattern.search(content), f"{path} matched {pattern.pattern}"


def test_agent_step_requires_expectation_constructor_argument() -> None:
    with pytest.raises(TypeError):
        AgentStep(tool_name="tool", tool_input={}, timestamp=datetime.now(UTC))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        AgentStep(expectation=" ", tool_name="tool", tool_input={}, timestamp=datetime.now(UTC))


def test_expectation_comparison_sets_contradiction() -> None:
    step = AgentStep(
        expectation="deadline is Friday",
        tool_name="lookup",
        tool_input={},
        timestamp=datetime.now(UTC),
    )
    checked = compare_expectation_to_observation(step, "deadline is Thursday")
    assert checked.contradiction is True


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
    with pytest.raises(ValueError):
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


def test_verification_tiers_are_ordered_and_only_student_workload_overrides() -> None:
    assert ORDERED_TIERS == (
        VerificationTier.SOURCE_PRESENT,
        VerificationTier.FACTUAL_CONSISTENCY,
        VerificationTier.POLICY_CONFORMANCE,
    )
    verifier = Verifier()
    failed = verifier.verify_fact("", 0)

    parent_result = verifier.apply_workload_override(
        failed,
        principal=Principal.PARENT,
        has_workload_signal=True,
    )
    student_result = verifier.apply_workload_override(
        failed,
        principal=Principal.STUDENT,
        has_workload_signal=True,
    )

    assert parent_result.passed is False
    assert student_result.passed is True
    assert student_result.workload_override is True


def test_student_due_this_week_renders_disagreement() -> None:
    """The clock is pinned because the fixtures carry fixed August 2026 dates.

    Before the clock became injectable this test read as if it were
    time-independent, but it only passed because the store hardcoded
    2026-08-19. Stating the date here makes the dependency visible instead of
    hiding it inside the store.
    """
    settings = Settings.from_environment({"BLOSSOM_TODAY": "2026-08-19"})

    with TestClient(create_app(settings)) as client:
        response = client.get("/student/due-this-week")

    assert response.status_code == 200
    assert "Source disagreement" in response.text
    assert "LMS: 2026-08-21" in response.text
    assert "PARENT_ENTRY: 2026-08-22" in response.text
