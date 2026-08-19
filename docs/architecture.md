# Blossom architecture scaffold

Blossom separates the student, parent, and verifier principals because each one is allowed to see a different projection of the same planning state. Separate route trees and Pydantic view models make absent fields impossible to serialize accidentally.

There is no sending path. Outbound work stops at a draft so a human can copy it manually, preventing the agent from contacting schools, teachers, or family members on its own.

Every agent step records an expectation before tool use. This preserves a checkable trace of what the system believed would happen and lets contradictions become data instead of silent surprises.

Retrieval routes on key presence. Exact keyed lookups stay in the structured SQLite path, while semantic search can explicitly return nothing when confidence is low instead of filling the UI with a weak nearest neighbor.

The three stores have different retention and write rules because assignment state, accommodation-derived support rules, and the agent's self-reflections have different risk profiles. Reflections are restricted to SYSTEM subjects so the store cannot become a diary about the student.

Reconciliation returns either agreement or all conflicting source records. It never chooses a winner silently because provenance is more important than a tidy single answer.

The workload signal is frictionless because the student's workload report is authoritative and must not require scales, labels, or text before it is recorded.

Verification is separate from generation and limited to factual and policy checks. Subjective relevance scoring lives in `blossom/heuristic_relevance.py` so checkable verification is not blurred with judgment.
