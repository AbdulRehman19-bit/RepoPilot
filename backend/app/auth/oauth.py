"""The "Sign in with GitHub" exchange. The user's GitHub token lives only inside `complete_login`:
it is used to read who they are and which repositories they can reach through the app, then dropped.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import settings

TIMEOUT = 15.0
PAGE_SIZE = 100
MAX_PAGES = 10  # bounds the work a user with a huge number of repositories can cause
WRITE_PERMISSIONS = ("admin", "maintain", "push")


class OAuthError(RuntimeError):
    """GitHub refused the login, or answered something unexpected."""


@dataclass(frozen=True)
class Profile:
    github_id: int
    login: str
    access: dict[int, str]  # GitHub repository id -> "read" | "write"


def is_configured() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


def authorize_url(state: str) -> str:
    query = urlencode({"client_id": settings.github_client_id, "state": state})
    return f"{settings.github_oauth_url}/login/oauth/authorize?{query}"


def make_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _exchange_code(client: httpx.Client, code: str) -> str:
    response = client.post(
        f"{settings.github_oauth_url}/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "code": code,
        },
    )
    body = response.json() if response.status_code == 200 else {}
    token = body.get("access_token")
    if not token:
        # `error_description` is GitHub's own text; never echo the code or secret.
        raise OAuthError(body.get("error_description") or "GitHub did not return a token")
    return token


def _get(client: httpx.Client, token: str, path: str, **params) -> dict:
    response = client.get(
        f"{settings.github_api_url}{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if response.status_code != 200:
        raise OAuthError(f"GitHub returned {response.status_code} for {path}")
    return response.json()


def _paged(client: httpx.Client, token: str, path: str, key: str) -> list[dict]:
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _get(client, token, path, per_page=PAGE_SIZE, page=page).get(key, [])
        items += batch
        if len(batch) < PAGE_SIZE:
            break
    return items


def _level(repo: dict) -> str:
    permissions = repo.get("permissions") or {}
    return "write" if any(permissions.get(p) for p in WRITE_PERMISSIONS) else "read"


def complete_login(code: str, client: httpx.Client | None = None) -> Profile:
    """Turn an authorization code into who the user is and what they may reach."""
    own = client is None
    client = client or make_client()
    try:
        token = _exchange_code(client, code)
        user = _get(client, token, "/user")
        access: dict[int, str] = {}
        # Only repositories that both the user and RepoPilot's app can reach count.
        for installation in _paged(client, token, "/user/installations", "installations"):
            path = f"/user/installations/{installation['id']}/repositories"
            for repo in _paged(client, token, path, "repositories"):
                level = _level(repo)
                if access.get(repo["id"]) != "write":
                    access[repo["id"]] = level
        return Profile(github_id=user["id"], login=user["login"], access=access)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise OAuthError(f"login failed: {type(exc).__name__}") from exc
    finally:
        if own:
            client.close()
