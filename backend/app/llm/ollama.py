import httpx

from app.config import settings
from app.llm.base import LLMError, LLMUnavailable


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout: float | None = None,
        num_ctx: int | None = None,
        temperature: float | None = None,
        client: httpx.Client | None = None,
    ):
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.num_ctx = num_ctx or settings.llm_num_ctx
        self.temperature = settings.llm_temperature if temperature is None else temperature
        # CPU inference is slow, so the read timeout is long; connecting should still be fast.
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or settings.llm_timeout_seconds, connect=5),
        )

    def available(self) -> tuple[bool, str]:
        try:
            response = self.client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError:
            return False, f"cannot reach Ollama at {self.base_url} (is it running?)"
        names = {m.get("name", "") for m in response.json().get("models", [])}
        if self.model in names or f"{self.model}:latest" in names:
            return True, ""
        return False, f"model {self.model!r} is not installed; run: ollama pull {self.model}"

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        max_tokens: int = 700,
        temperature: float | None = None,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
            },
        }
        if schema:
            payload["format"] = schema  # Ollama constrains decoding to this JSON schema

        try:
            response = self.client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"cannot reach Ollama at {self.base_url}") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                "the model timed out; CPU inference is slow, so try a smaller model"
            ) from exc

        if response.status_code == 404:
            raise LLMError(f"model {self.model!r} not found; run: ollama pull {self.model}")
        if response.status_code >= 400:
            raise LLMError(f"Ollama returned {response.status_code}: {response.text[:300]}")
        try:
            return response.json()["message"]["content"]
        except (KeyError, ValueError) as exc:
            raise LLMError("unexpected response format from Ollama") from exc
