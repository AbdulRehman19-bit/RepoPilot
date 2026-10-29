import re
from typing import TypeVar

from pydantic import BaseModel

from app.llm.base import LLMError, LLMProvider

T = TypeVar("T", bound=BaseModel)
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> str:
    """Pull a JSON object out of a reply that may be wrapped in code fences or prose."""
    text = FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def generate_structured(
    provider: LLMProvider,
    prompt: str,
    model_cls: type[T],
    *,
    system: str | None = None,
    max_tokens: int = 700,
    attempts: int = 2,
) -> T:
    """Ask for JSON matching `model_cls`; on invalid output retry once with the error."""
    schema = model_cls.model_json_schema()
    current = prompt
    last_error: Exception | None = None
    for _ in range(attempts):
        raw = provider.generate(current, system=system, schema=schema, max_tokens=max_tokens)
        try:
            return model_cls.model_validate_json(extract_json(raw))
        except ValueError as exc:  # JSON syntax errors and pydantic validation errors
            last_error = exc
            current = (
                f"{prompt}\n\nYour previous reply was not valid ({str(exc)[:200]}). "
                "Reply with only a JSON object that matches the schema."
            )
    raise LLMError(f"model did not return valid JSON after {attempts} attempts: {last_error}")
