"""Tier two: subjective scoring, deliberately kept out of ``blossom/verification.py``.

The design separates three kinds of confidence. A hard check is deterministic
and produces a pass or a fail. A heuristic score is an estimate -- whether a
notification is worth an interruption, whether one candidate plan reads better
than another. Whether a plan is actually right for her is neither, and cannot
be automated at all.

This module exists so the second kind has somewhere to live that is not the
verifier. A score that shares a module with a check eventually gets read as
one.

Status: the implementation below is a placeholder. It returns text length over
one hundred, which is not a relevance measure by any account. Nothing calls it
and no test covers it. It is kept because the boundary it marks is real and
worth holding open, but it should not be mistaken for a working critic.
"""

def score_internal_triage(text: str) -> float:
    """Subjective relevance scoring for internal triage, not verification."""
    return min(len(text.strip()) / 100.0, 1.0)
