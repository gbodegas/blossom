"""Store two: operational support rules derived from her accommodations.

These are written as instructions -- break a long assignment into stages small
enough that starting is not the hardest part -- rather than as clinical
descriptions. That choice was made for privacy and turned out to serve
retrieval as well: an instruction is a self-contained unit of meaning, and a
clinical description is not.

Segmentation follows from that. One rule per chunk, no sliding window, because
the corpus is already discrete and splitting a rule produces fragments that
mislead rather than merely truncate. ``add_rule`` enforces it by rejecting any
rule containing a paragraph break.

The constraints these rules carry are absent from the assignment itself, so a
plan built only from the assignment is wrong in ways that are predictable and
avoidable.

Status: this store is not wired into anything. Nothing constructs it, no route
reads it, and no test covers it. Its ``retrieve`` is a substring scan rather
than the semantic retrieval the design calls for. Of the three stores the
design names, this is the one that exists only as a shape.
"""

from dataclasses import dataclass
from datetime import datetime

from blossom.retrieval import NothingRetrieved, RetrievalQuery, RetrievalResponse, RetrievalResult


@dataclass(frozen=True)
class SupportRule:
    """One self-contained instruction, with the date it was asserted."""

    rule_id: str
    instruction: str
    asserted_at: datetime


class SupportRulesStore:
    """Holds the support rules. See the module docstring: not yet wired in."""

    name = "support_rules"
    retention_policy = "Review accommodation-derived operational guidance each term."

    def __init__(self) -> None:
        self._rules: dict[str, SupportRule] = {}

    def add_rule(self, rule: SupportRule) -> None:
        """Store one rule, rejecting anything that looks like two.

        A paragraph break signals a rule that should have been split before it
        arrived. Storing it whole would make retrieval return two instructions
        under a single identifier.
        """
        if "\n\n" in rule.instruction:
            msg = "one support rule must be stored as one self-contained chunk"
            raise ValueError(msg)
        self._rules[rule.rule_id] = rule

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
        """Return the first rule whose instruction contains the query text.

        Placeholder. A substring scan is not similarity search, and the design
        calls for semantic retrieval of three to five rules with a score
        threshold below which nothing is returned at all.
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
