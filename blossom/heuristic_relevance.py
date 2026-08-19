def score_internal_triage(text: str) -> float:
    """Subjective relevance scoring for internal triage, not verification."""
    return min(len(text.strip()) / 100.0, 1.0)
