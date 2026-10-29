from app.config import settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider


def get_provider() -> LLMProvider | None:
    """The configured provider, or None when the model is disabled."""
    match settings.llm_provider:
        case "none":
            return None
        case "ollama":
            return OllamaProvider()
        case other:
            raise ValueError(f"unknown REPOPILOT_LLM_PROVIDER {other!r} (use 'ollama' or 'none')")
