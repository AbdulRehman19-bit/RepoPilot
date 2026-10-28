from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REPOPILOT_")

    app_name: str = "RepoPilot"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://repopilot:repopilot@localhost:5433/repopilot"
    redis_url: str = "redis://localhost:6380/0"


settings = Settings()
