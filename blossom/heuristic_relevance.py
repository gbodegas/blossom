"""Subjective relevance scoring, kept out of ``blossom/verification.py``.

A hard check in the verifier is deterministic and passes or fails. A heuristic
score is an estimate: whether a notification is worth an interruption, whether
one candidate plan reads better than another. A score that shares a module with
a check eventually gets read as one, so scores live here. Whether a plan is
actually right for her cannot be automated at all.

Status: placeholder. It returns text length over one hundred, which is not a
relevance measure. Nothing calls it and no test covers it; it exists to hold
the module boundary open.
"""


def score_internal_triage(text: str) -> float:
    """Subjective relevance scoring for internal triage, not verification."""
    return min(len(text.strip()) / 100.0, 1.0)
