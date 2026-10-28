from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REPOPILOT_")

    app_name: str = "RepoPilot"
    environment: str = "development"


settings = Settings()
