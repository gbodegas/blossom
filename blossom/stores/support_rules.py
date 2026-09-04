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

Status: placeholder. Nothing constructs this store, no route reads it, and no
test covers it. ``retrieve`` is a substring scan, not the semantic retrieval
the design notes call for.
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
    """Holds the support rules. Placeholder; nothing constructs it yet."""

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
