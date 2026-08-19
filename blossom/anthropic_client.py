from anthropic import Anthropic


class AnthropicGenerator:
    """Direct Anthropic SDK seam for future generation; no agent framework belongs here."""

    def __init__(self, client: Anthropic) -> None:
        self._client = client

    def draft_plan(self, prompt: str) -> str:
        raise NotImplementedError
