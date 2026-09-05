"""Store two: operational support rules derived from her accommodations.

Rules are written as instructions (for example, break a long assignment into
stages small enough that starting is not the hardest part), not as clinical
descriptions. The instruction form protects privacy and suits retrieval: an
instruction is a self-contained unit of meaning, and a clinical description is
not. One rule per chunk, no sliding window: the corpus is already discrete, and
a split rule is a fragment that misleads rather than merely truncates.
``add_rule`` enforces this by rejecting any rule containing a paragraph break.
The constraints these rules carry are absent from the assignment itself, so a
plan built only from the assignment misses them.

The plan graph reads every rule, in order, rather than searching for the
relevant ones. The corpus is a few sentences about one student, which fits in
a prompt whole, and an index over it would add a way to miss a rule for no
saving. ``retrieve`` remains for the retrieval router and is a substring scan.

The fixture set seeds it at startup from ``support_rules.json``.
"""

from dataclasses import dataclass
from datetime import datetime

from blossom.clock import require_aware
from blossom.retrieval import NothingRetrieved, RetrievalQuery, RetrievalResponse, RetrievalResult


@dataclass(frozen=True)
class SupportRule:
    """One self-contained instruction, with the date it was asserted."""

    rule_id: str
    instruction: str
    asserted_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.asserted_at, "asserted_at")


class SupportRulesStore:
    """Holds the support rules, in the order they were added."""

    name = "support_rules"
    retention_policy = "Review accommodation-derived operational guidance each term."

    def __init__(self) -> None:
        self._rules: dict[str, SupportRule] = {}

    def add_rule(self, rule: SupportRule) -> None:
        """Store one rule. A paragraph break means two instructions under one
        identifier, so such a rule is rejected.
        """
        if "\n\n" in rule.instruction:
            msg = "one support rule must be stored as one self-contained chunk"
            raise ValueError(msg)
        self._rules[rule.rule_id] = rule

    def list_all(self) -> list[SupportRule]:
        """Every rule, in insertion order, for a reader that wants the whole corpus."""
        return list(self._rules.values())

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Return the first rule whose instruction contains the query text.

        Placeholder: the design notes call for semantic retrieval of three to
        five rules with a score threshold below which nothing is returned.
        """
        for rule in self._rules.values():
            if query.text.casefold() in rule.instruction.casefold():
                return RetrievalResult(
                    store_name=self.name,
                    record_id=rule.rule_id,
                    source_channel="synthetic_rule",
                    asserted_at=rule.asserted_at,
                    score=1.0,
                    payload={"instruction": rule.instruction},
                )
        return NothingRetrieved(reason="no support rule matched")
