"""Preflight: is this machine ready to run RepoPilot against a real GitHub App?

    uv run python -m app.doctor            # local checks only
    uv run python -m app.doctor --online   # also asks GitHub what the app is allowed to do

Every line says what is wrong and what to do about it. Exit status is 1 if anything FAILed.
"""

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization

from app.config import settings

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "read"}
# GitHub always sends installation events to an app; only `issues` must be subscribed to.
REQUIRED_EVENTS = {"issues"}
LEVELS = {"read": 1, "write": 2, "admin": 3}


@dataclass(frozen=True)
class Result:
    status: str
    name: str
    detail: str


def check_database() -> Result:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from app.db import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
            current = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:  # noqa: BLE001 - any failure is the answer to "can we connect?"
        return Result(
            FAIL, "database", f"cannot connect ({type(exc).__name__}); run: docker compose up -d"
        )
    head = ScriptDirectory.from_config(
        Config(str(Path(__file__).parents[1] / "alembic.ini"))
    ).get_current_head()
    if current != head:
        return Result(
            FAIL,
            "database",
            f"schema is at {current}, expected {head}; run: uv run alembic upgrade head",
        )
    return Result(PASS, "database", f"connected, schema up to date ({head})")


def check_redis() -> Result:
    from redis import Redis

    try:
        Redis.from_url(settings.redis_url).ping()
    except Exception as exc:  # noqa: BLE001
        return Result(
            FAIL, "redis", f"cannot connect ({type(exc).__name__}); run: docker compose up -d"
        )
    return Result(PASS, "redis", "reachable")


def check_worker() -> Result:
    from redis import Redis
    from rq import Worker

    try:
        count = Worker.count(connection=Redis.from_url(settings.redis_url))
    except Exception:  # noqa: BLE001 - redis problems are reported by check_redis
        return Result(WARN, "worker", "could not check (see redis)")
    if count == 0:
        return Result(
            FAIL,
            "worker",
            "no worker is running, so nothing will be indexed; run: uv run python -m app.worker",
        )
    return Result(PASS, "worker", f"{count} running")


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=False
    )


def check_sandbox() -> Result:
    try:
        if _docker("info").returncode != 0:
            return Result(FAIL, "sandbox", "Docker is not running; start Docker Desktop")
        if _docker("image", "inspect", settings.sandbox_image).returncode != 0:
            return Result(
                FAIL,
                "sandbox",
                f"image {settings.sandbox_image!r} is missing; run: docker build -t {settings.sandbox_image} docker/sandbox",
            )
    except (OSError, subprocess.TimeoutExpired):
        return Result(FAIL, "sandbox", "cannot run docker; is it installed and on PATH?")
    return Result(PASS, "sandbox", f"Docker is up and image {settings.sandbox_image!r} exists")


def check_model() -> Result:
    from app.llm.factory import get_provider

    provider = get_provider()
    if provider is None:
        return Result(
            WARN, "model", "disabled (REPOPILOT_LLM_PROVIDER=none): no patches can be generated"
        )
    ok, reason = provider.available()
    return (
        Result(PASS, "model", f"{provider.model} is ready") if ok else Result(FAIL, "model", reason)
    )


def check_github_app() -> list[Result]:
    results: list[Result] = []
    if not settings.github_app_id:
        results.append(Result(FAIL, "github app", "REPOPILOT_GITHUB_APP_ID is not set"))
    key = settings.github_private_key_path
    if key is None or not key.is_file():
        results.append(
            Result(
                FAIL,
                "github key",
                f"private key file not found ({key}); set REPOPILOT_GITHUB_PRIVATE_KEY_PATH",
            )
        )
    else:
        try:
            serialization.load_pem_private_key(key.read_bytes(), password=None)
            results.append(Result(PASS, "github key", f"{key.name} is a valid private key"))
        except (ValueError, TypeError):
            results.append(
                Result(
                    FAIL,
                    "github key",
                    f"{key.name} is not a readable PEM private key (encrypted or the wrong file?)",
                )
            )
    if not settings.github_webhook_secret:
        results.append(
            Result(
                FAIL,
                "webhook secret",
                "REPOPILOT_GITHUB_WEBHOOK_SECRET is not set, so every webhook is rejected",
            )
        )
    else:
        results.append(Result(PASS, "webhook secret", "set"))
    return results


def check_signin() -> list[Result]:
    results: list[Result] = []
    has_id, has_secret = bool(settings.github_client_id), bool(settings.github_client_secret)
    if has_id != has_secret:
        results.append(
            Result(
                FAIL,
                "sign-in",
                "set both REPOPILOT_GITHUB_CLIENT_ID and REPOPILOT_GITHUB_CLIENT_SECRET, or neither",
            )
        )
    elif has_id:
        results.append(Result(PASS, "sign-in", "GitHub sign-in is configured"))
    elif settings.api_token:
        results.append(
            Result(
                WARN,
                "sign-in",
                "only the shared API token protects the API; everyone with it is an admin",
            )
        )
    else:
        results.append(
            Result(
                WARN,
                "sign-in",
                "nothing is configured: the API is open and approvals are disabled (fine for development only)",
            )
        )

    frontend = urlsplit(settings.frontend_url)
    local = frontend.hostname in ("localhost", "127.0.0.1")
    if has_id and frontend.scheme != "https" and not local and settings.session_cookie_secure:
        results.append(
            Result(
                FAIL,
                "cookies",
                "the page is served over http but session cookies are Secure, so browsers will drop them; use https",
            )
        )
    if has_id and settings.environment != "development" and not settings.session_cookie_secure:
        results.append(
            Result(WARN, "cookies", "REPOPILOT_SESSION_COOKIE_SECURE is off outside development")
        )
    return results


def check_github_online(client: httpx.Client | None = None) -> list[Result]:
    """Ask GitHub who the app is and what it may do. Read-only."""
    from app.github.auth import create_app_jwt

    try:
        token = create_app_jwt(settings.github_app_id, settings.github_private_key())
    except Exception as exc:  # noqa: BLE001
        return [Result(FAIL, "github online", f"cannot sign a token: {type(exc).__name__}: {exc}")]
    own = client is None
    client = client or httpx.Client(base_url=settings.github_api_url, timeout=15)
    try:
        response = client.get(
            "/app",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError as exc:
        return [Result(FAIL, "github online", f"cannot reach GitHub ({type(exc).__name__})")]
    finally:
        if own:
            client.close()
    if response.status_code == 401:
        return [
            Result(
                FAIL,
                "github online",
                "GitHub rejected the app: wrong App ID, or the key belongs to another app",
            )
        ]
    if response.status_code != 200:
        return [Result(FAIL, "github online", f"GitHub answered {response.status_code}")]

    app = response.json()
    results = [Result(PASS, "github online", f"authenticated as app {app.get('slug')!r}")]
    granted = app.get("permissions", {})
    short = [
        f"{name}:{need}"
        for name, need in REQUIRED_PERMISSIONS.items()
        if LEVELS.get(granted.get(name, ""), 0) < LEVELS[need]
    ]
    results.append(
        Result(
            FAIL, "permissions", f"the app lacks: {', '.join(short)} (app settings > Permissions)"
        )
        if short
        else Result(PASS, "permissions", "contents, pull requests and issues are sufficient")
    )
    missing = sorted(REQUIRED_EVENTS - set(app.get("events", [])))
    results.append(
        Result(
            FAIL,
            "events",
            f"the app does not subscribe to: {', '.join(missing)} (app settings > Subscribe to events)",
        )
        if missing
        else Result(PASS, "events", "subscribed to issues events")
    )
    return results


def run(
    online: bool = False, checks: list[Callable[[], Result | list[Result]]] | None = None
) -> list[Result]:
    checks = checks or [
        check_database, check_redis, check_worker, check_sandbox, check_model, check_github_app, check_signin,
    ]  # fmt: skip
    if online:
        checks = [*checks, check_github_online]
    results: list[Result] = []
    for check in checks:
        found = check()
        results += found if isinstance(found, list) else [found]
    return results


def main(argv: list[str] | None = None) -> int:
    results = run(online="--online" in (argv if argv is not None else sys.argv[1:]))
    for r in results:
        print(f"{r.status:<4}  {r.name:<15} {r.detail}")
    failed = sum(r.status == FAIL for r in results)
    warned = sum(r.status == WARN for r in results)
    print(f"\n{len(results)} checks: {failed} failed, {warned} warnings")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
