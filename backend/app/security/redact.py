"""Scrub secrets from text before it is stored, shown in the UI, or sent to a model.

Test output and error messages come from untrusted code and from tools that sometimes echo
credentials (a clone URL, an Authorization header, a key printed by a failing test). This is a
best-effort filter for well-known shapes plus our own configured secrets, not a guarantee: a
secret in an unrecognisable format passes through.
"""

import re

from app.config import settings

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 8  # shorter configured values would mangle ordinary text

_PATTERNS = [
    # PEM blocks, including ones cut off by the output cap (no END line).
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
    ),
    # GitHub tokens: classic, OAuth, user-to-server, server-to-server, refresh, fine-grained.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWTs
]
_AUTH_HEADER = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[^\s\"']+"
)
_URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)")


def _configured_secrets() -> list[str]:
    values = (*settings.named_tokens().values(), settings.github_webhook_secret)
    return [v for v in values if v and len(v) >= MIN_SECRET_LENGTH]


def redact(text: str) -> str:
    for secret in _configured_secrets():
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(rf"\1{REDACTED}", text)
    return _URL_CREDENTIALS.sub(rf"\1{REDACTED}\2", text)
