"""The words a step record uses, held to what a person would read on the page."""

from blossom.agent.steps import (
    count,
    describe_failure,
    describe_outcome,
    describe_verdict,
    expect_plan,
    tokens_note,
)
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment


def finding(
    criterion: Criterion, judgment: Judgment, critique: str = "reads well"
) -> CriterionFinding:
    return CriterionFinding(criterion=criterion, critique=critique, judgment=judgment)


def test_counts_read_as_english() -> None:
    assert count(1, "block") == "1 block"
    assert count(0, "deferral") == "0 deferrals"
    assert count(2, "rule") == "2 rules"


def test_the_cost_is_shown_only_when_the_answer_carried_it() -> None:
    assert tokens_note(1777, 863) == " (1777 tokens in, 863 out)"
    assert tokens_note(None, 863) == ""
    assert tokens_note(1777, None) == ""


def test_the_first_round_asks_for_a_plan_and_later_rounds_for_a_revision() -> None:
    assert expect_plan(1, 0, 150) == "a plan that accounts for every assignment inside 150 minutes"
    assert expect_plan(2, 1, 150) == "a revised plan that answers 1 finding"
    assert expect_plan(3, 4, 150) == "a revised plan that answers 4 findings"


def test_a_failure_names_what_was_missing_and_why() -> None:
    assert describe_failure("model_refused", "verdict", "") == "no verdict: the model declined"
    assert describe_failure("model_unparseable", "plan", "") == "no plan: the answer did not parse"
    assert describe_failure("something_new", "plan", "") == "no plan: something_new"


def test_a_verdict_is_described_by_where_it_did_not_pass() -> None:
    everything = CriticVerdict(
        findings=[finding(criterion, Judgment.PASSES) for criterion in Criterion]
    )
    mixed = CriticVerdict(
        findings=[
            finding(Criterion.ORDER, Judgment.PASSES),
            finding(Criterion.SIZING, Judgment.FAILS, "an hour is short"),
            finding(Criterion.DEFERRALS, Judgment.CANNOT_TELL, "no dates to weigh"),
        ]
    )

    assert describe_verdict(everything, " (10 tokens in, 5 out)") == (
        "accepted on every criterion (10 tokens in, 5 out)"
    )
    assert describe_verdict(mixed, "") == (
        "faulted sizing: an hour is short; could not tell on deferrals; "
        "did not consider support rules, rationale"
    )


def test_an_outcome_reads_as_one_sentence_for_the_page() -> None:
    assert describe_outcome("checks_failed") == "The plan failed its checks after every revision."
    assert describe_outcome("model_truncated") == "The model's answer was cut off."
    assert describe_outcome("something_new") == "The run ended with something_new."
