"""Seam for model access.

Status: unused seam. Nothing in this system calls a model.

``draft_plan`` raises ``NotImplementedError``: there is no generation in this
system today. The class exists so that when generation arrives it has one
place to arrive, and the import allowlist in
``tests/test_capability_boundaries.py`` confines ``anthropic`` to this file.
That confinement is the point. It means model access cannot spread into a route
without an explicit, reviewable edit to the allowlist.

Unresolved: the comment this module used to carry said that no agent framework
belongs here. Checkpoint 4.1 specifies LangChain for generation and judging and
LangGraph for control flow, which is the opposite position. That decision is
open, and whichever way it goes will change what this file becomes.
"""

from anthropic import Anthropic


class AnthropicGenerator:
    """Direct Anthropic SDK seam for future generation; no agent framework belongs here."""

    def __init__(self, client: Anthropic) -> None:
        self._client = client

    def draft_plan(self, prompt: str) -> str:
        """Not implemented. No model is called anywhere in this system yet."""
        raise NotImplementedError
