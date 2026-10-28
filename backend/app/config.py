from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REPOPILOT_")

    app_name: str = "RepoPilot"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://repopilot:repopilot@localhost:5433/repopilot"
    redis_url: str = "redis://localhost:6380/0"

    github_api_url: str = "https://api.github.com"
    github_app_id: str = ""
    github_private_key_path: Path | None = None
    github_webhook_secret: str = ""

    def github_private_key(self) -> str:
        if not self.github_private_key_path:
            raise RuntimeError("REPOPILOT_GITHUB_PRIVATE_KEY_PATH is not set")
        return self.github_private_key_path.read_text()


settings = Settings()
