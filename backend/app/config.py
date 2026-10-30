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
    # Total bytes / files a run may hold in its writable directories (checkout copy, installs).
    sandbox_disk_limit_bytes: int = 1_000_000_000
    sandbox_max_files: int = 100_000
    # Dependency installs reach the internet only through an allowlist proxy on an isolated
    # network. Turning the proxy off gives the install container the open internet.
    install_egress_proxy: bool = True
    install_allowed_hosts: str = "pypi.org,files.pythonhosted.org"

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
    # More than one credential, each with a name that is recorded as the reviewer:
    # "alice:token-one,ci:token-two". The single api_token above counts as the name "admin".
    api_tokens: str = ""
    # When true, whoever asked for a patch cannot also approve it (a second pair of eyes).
    require_separate_reviewer: bool = False
    # Request size caps, in bytes. Webhooks carry whole issue bodies; API calls are small JSON.
    max_webhook_bytes: int = 5_000_000
    max_request_bytes: int = 1_000_000
    # Per caller per hour (0 disables): sandbox runs / patch generation, and model calls. Sign-in
    # endpoints are limited per client address per minute.
    rate_limit_jobs_per_hour: int = 30
    rate_limit_ai_per_hour: int = 60
    rate_limit_login_per_minute: int = 20

    github_api_url: str = "https://api.github.com"
    github_clone_base: str = "https://github.com"
    github_app_id: str = ""
    github_private_key_path: Path | None = None
    github_webhook_secret: str = ""

    # "Sign in with GitHub" (the GitHub App's user authorization). When the client id is empty,
    # login is unavailable and the shared API token (or open dev mode) is the only way in.
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_url: str = "https://github.com"
    frontend_url: str = "http://localhost:5173"
    session_ttl_hours: int = 4
    # Secure cookies need HTTPS. Chrome and Firefox accept them on http://localhost; some browsers
    # (notably Safari) do not, so set this to false for local development there.
    session_cookie_secure: bool = True

    def named_tokens(self) -> dict[str, str]:
        """Every accepted API credential as {name: token}."""
        tokens: dict[str, str] = {}
        for item in self.api_tokens.split(","):
            name, _, token = item.strip().partition(":")
            if name.strip() and token.strip():
                tokens[name.strip()] = token.strip()
        if self.api_token:
            tokens.setdefault("admin", self.api_token)
        return tokens

    def github_private_key(self) -> str:
        if not self.github_private_key_path:
            raise RuntimeError("REPOPILOT_GITHUB_PRIVATE_KEY_PATH is not set")
        return self.github_private_key_path.read_text()


settings = Settings()
