from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REPOPILOT_")

    app_name: str = "RepoPilot"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://repopilot:repopilot@localhost:5433/repopilot"
    redis_url: str = "redis://localhost:6380/0"
    queue_name: str = "repopilot"
    workspace_dir: Path = Path(".workspaces")

    sandbox_image: str = "repopilot-sandbox:py311"
    sandbox_timeout_seconds: int = 120
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 128
    sandbox_output_limit_bytes: int = 200_000

    # "ollama" talks to a local Ollama server; "none" disables the model entirely.
    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:3b"
    llm_timeout_seconds: int = 180
    llm_num_ctx: int = 6144
    llm_temperature: float = 0.1
    patch_max_attempts: int = 3

    # Shared secret for the API. When empty the API is open (local development only), and the
    # approval endpoints, which write to GitHub, refuse to work.
    api_token: str = ""
    # Request size caps, in bytes. Webhooks carry whole issue bodies; API calls are small JSON.
    max_webhook_bytes: int = 5_000_000
    max_request_bytes: int = 1_000_000

    github_api_url: str = "https://api.github.com"
    github_clone_base: str = "https://github.com"
    github_app_id: str = ""
    github_private_key_path: Path | None = None
    github_webhook_secret: str = ""

    def github_private_key(self) -> str:
        if not self.github_private_key_path:
            raise RuntimeError("REPOPILOT_GITHUB_PRIVATE_KEY_PATH is not set")
        return self.github_private_key_path.read_text()


settings = Settings()
