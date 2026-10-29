from typing import Protocol


class LLMError(RuntimeError):
    """The model failed or returned something unusable."""


class LLMUnavailable(LLMError):
    """No model server could be reached."""


class LLMProvider(Protocol):
    name: str
    model: str

    def available(self) -> tuple[bool, str]:
        """Whether generation can work right now, and a human-readable reason if not."""

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        max_tokens: int = 700,
        temperature: float | None = None,
    ) -> str:
        """Return the model's reply. With `schema`, the reply should be JSON matching it."""
