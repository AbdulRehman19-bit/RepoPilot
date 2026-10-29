from collections.abc import Callable

from app.llm.base import LLMError


class FakeProvider:
    """Scripted provider for tests: replies come from a list, or from a function of the prompt."""

    name = "fake"

    def __init__(
        self,
        replies: list[str] | Callable[[str], str] | None = None,
        model: str = "fake-model",
        available: tuple[bool, str] = (True, ""),
    ):
        self.model = model
        self._replies = replies if replies is not None else []
        self._available = available
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.schemas: list[dict | None] = []

    def available(self) -> tuple[bool, str]:
        return self._available

    def generate(self, prompt, *, system=None, schema=None, max_tokens=700, temperature=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        self.schemas.append(schema)
        if callable(self._replies):
            return self._replies(prompt)
        if not self._replies:
            raise LLMError("fake provider has no more replies")
        return self._replies.pop(0)
