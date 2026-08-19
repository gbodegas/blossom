from dataclasses import dataclass
from datetime import datetime

from blossom.retrieval import NothingRetrieved, RetrievalQuery, RetrievalResponse, RetrievalResult


@dataclass(frozen=True)
class SupportRule:
    rule_id: str
    instruction: str
    asserted_at: datetime


class SupportRulesStore:
    name = "support_rules"
    retention_policy = "Review accommodation-derived operational guidance each term."

    def __init__(self) -> None:
        self._rules: dict[str, SupportRule] = {}

    def add_rule(self, rule: SupportRule) -> None:
        if "\n\n" in rule.instruction:
            msg = "one support rule must be stored as one self-contained chunk"
            raise ValueError(msg)
        self._rules[rule.rule_id] = rule

    def retrieve(self, query: RetrievalQuery) -> RetrievalResponse:
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
