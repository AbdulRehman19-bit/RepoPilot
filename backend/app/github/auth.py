import time
from dataclasses import dataclass

import httpx
import jwt

from app.config import settings


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str


def create_app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    """Short-lived JWT identifying the GitHub App itself (max 10 minutes)."""
    issued = (now if now is not None else int(time.time())) - 60
    payload = {"iat": issued, "exp": issued + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


def mint_installation_token(
    installation_id: int, client: httpx.Client | None = None
) -> InstallationToken:
    """Exchange the app JWT for a token scoped to one installation."""
    app_jwt = create_app_jwt(settings.github_app_id, settings.github_private_key())
    own_client = client is None
    client = client or httpx.Client(base_url=settings.github_api_url, timeout=15)
    try:
        response = client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        data = response.json()
        return InstallationToken(token=data["token"], expires_at=data["expires_at"])
    finally:
        if own_client:
            client.close()
