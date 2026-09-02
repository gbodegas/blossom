"""Seam for model access.

Status: placeholder; nothing in this system calls a model.

``draft_plan`` raises ``NotImplementedError``. The class exists so generation
has one place to arrive, and the import allowlist in
``tests/test_capability_boundaries.py`` confines ``anthropic`` to this file, so
model access cannot reach a route without a reviewable edit to that allowlist.

Open question: this class assumes direct SDK calls, while the design notes call
for LangChain for generation and judging and LangGraph for control flow.
Whichever way that goes changes what this file becomes.
"""

from anthropic import Anthropic


class AnthropicGenerator:
    """Direct Anthropic SDK seam for future generation."""

    def __init__(self, client: Anthropic) -> None:
        self._client = client

    def draft_plan(self, prompt: str) -> str:
        """Not implemented; no model is called anywhere in this system."""
        raise NotImplementedError
