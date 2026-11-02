"""Create the GitHub App with one click, using GitHub's app-manifest flow.

    uv run python -m app.github.register

Opens a page on 127.0.0.1 that sends GitHub a ready-made app definition (name, permissions,
sign-in callback). You review it on github.com and click "Create GitHub App"; GitHub then sends
your browser back here with a one-time code, which this script exchanges for the app's id,
private key and secrets and writes straight to `backend/.env` and `backend/github-app.pem`.
The secrets are never printed.

The webhook is registered inactive: a local pilot reads GitHub with `python -m app.github.sync`
instead of needing a public URL. Turn it on in the app's settings when you deploy.
"""

import argparse
import html
import json
import os
import secrets
import sys
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}
EVENTS = ["issues"]
TIMEOUT_SECONDS = 15 * 60


def build_manifest(name: str, frontend_url: str, redirect_url: str) -> dict:
    return {
        "name": name,
        "url": frontend_url,
        "redirect_url": redirect_url,
        "callback_urls": [f"{frontend_url.rstrip('/')}/api/auth/callback"],
        # Inactive: the pilot uses `app.github.sync`. GitHub still wants a URL here.
        "hook_attributes": {"url": "https://example.invalid/github/webhook", "active": False},
        "public": False,
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
    }


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines, replacing existing ones and leaving every other line untouched."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out += [f"{key}={value}" for key, value in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


def write_private(path: Path, content: str) -> None:
    """Write a secret file readable only by the current user where the OS supports it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def convert_manifest_code(code: str) -> dict:
    """Trade the one-time code for the new app's credentials (an unauthenticated call)."""
    response = httpx.post(
        f"{settings.github_api_url}/app-manifests/{code}/conversions",
        headers={"Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


class Registration:
    """State for one registration attempt, kept apart from the HTTP plumbing for testing."""

    def __init__(
        self,
        *,
        manifest: dict,
        org: str | None,
        env_file: Path,
        key_file: Path,
        frontend_url: str,
        convert: Callable[[str], dict] = convert_manifest_code,
    ):
        self.manifest, self.org = manifest, org
        self.env_file, self.key_file = env_file, key_file
        self.frontend_url = frontend_url
        self.convert = convert
        self.state = secrets.token_urlsafe(24)
        self.done = threading.Event()
        self.result: dict | None = None

    @property
    def github_form_url(self) -> str:
        base = "https://github.com"
        path = f"/organizations/{self.org}/settings/apps/new" if self.org else "/settings/apps/new"
        return f"{base}{path}?state={self.state}"

    def start_page(self) -> str:
        manifest = html.escape(json.dumps(self.manifest), quote=True)
        return (
            "<!doctype html><meta charset=utf-8><title>Register RepoPilot</title>"
            "<body style='font:16px system-ui;max-width:42rem;margin:3rem auto'>"
            "<h1>Create the RepoPilot GitHub App</h1>"
            "<p>You will be taken to github.com to review the app and confirm. Nothing is created "
            "until you click <b>Create GitHub App</b> there.</p>"
            f"<form id=f method=post action='{html.escape(self.github_form_url, quote=True)}'>"
            f"<input type=hidden name=manifest value='{manifest}'>"
            "<button>Continue to GitHub</button></form>"
            "<script>setTimeout(()=>document.getElementById('f').submit(),400)</script></body>"
        )

    def finish(self, code: str | None, state: str | None) -> tuple[int, str]:
        """Handle GitHub's redirect back. Returns (http status, page)."""
        if self.done.is_set():
            return 400, "This registration was already completed."
        if not (code and state and secrets.compare_digest(state, self.state)):
            return 400, "Invalid or expired registration attempt. Start again from the terminal."
        try:
            app = self.convert(code)
        except Exception as exc:  # noqa: BLE001 - shown to the person at the terminal
            print(
                f"GitHub did not accept the registration code: {type(exc).__name__}",
                file=sys.stderr,
            )
            return (
                502,
                "GitHub did not accept the registration. Check the terminal, then try again.",
            )

        write_private(self.key_file, app["pem"])
        update_env_file(
            self.env_file,
            {
                "REPOPILOT_GITHUB_APP_ID": str(app["id"]),
                "REPOPILOT_GITHUB_PRIVATE_KEY_PATH": str(self.key_file),
                # Webhooks are off for the pilot, but the setting must not be empty.
                "REPOPILOT_GITHUB_WEBHOOK_SECRET": app.get("webhook_secret")
                or secrets.token_urlsafe(32),
                "REPOPILOT_GITHUB_CLIENT_ID": app["client_id"],
                "REPOPILOT_GITHUB_CLIENT_SECRET": app["client_secret"],
                "REPOPILOT_FRONTEND_URL": self.frontend_url,
            },
        )
        self.result = {"id": app["id"], "slug": app["slug"], "html_url": app["html_url"]}
        self.done.set()
        install = f"https://github.com/apps/{app['slug']}/installations/new"
        return 200, (
            "<!doctype html><meta charset=utf-8><title>Done</title>"
            "<body style='font:16px system-ui;max-width:42rem;margin:3rem auto'>"
            f"<h1>Created {html.escape(app['name'])}</h1>"
            "<p>The credentials were saved on this computer. They are not shown here.</p>"
            f"<p><b>Next:</b> <a href='{html.escape(install, quote=True)}'>install the app on one repository</a>. "
            "You can close this tab afterwards.</p></body>"
        )


def make_server(registration: Registration, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                self._send(200, registration.start_page())
            elif url.path == "/callback":
                code, state = query.get("code", [None])[0], query.get("state", [None])[0]
                self._send(*registration.finish(code, state))
            else:
                self._send(404, "Not found")

        def log_message(self, *args):  # the request line of /callback contains the one-time code
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the RepoPilot GitHub App.")
    parser.add_argument("--name", default=f"repopilot-pilot-{secrets.token_hex(3)}")
    parser.add_argument("--org", help="create it under an organisation instead of your account")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--frontend-url", default="http://localhost:5173")
    parser.add_argument("--env-file", type=Path, default=BACKEND_DIR / ".env")
    parser.add_argument("--key-file", type=Path, default=BACKEND_DIR / "github-app.pem")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if args.key_file.exists():
        print(
            f"{args.key_file} already exists; move it aside first so a key is never overwritten.",
            file=sys.stderr,
        )
        return 2

    redirect = f"http://localhost:{args.port}/callback"
    registration = Registration(
        manifest=build_manifest(args.name, args.frontend_url, redirect),
        org=args.org,
        env_file=args.env_file,
        key_file=args.key_file.resolve(),
        frontend_url=args.frontend_url,
    )
    server = make_server(registration, args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = f"http://localhost:{args.port}/"
    print(f"Open {start} in your browser (opening it now) and click 'Create GitHub App' on GitHub.")
    if not args.no_browser:
        webbrowser.open(start)

    finished = registration.done.wait(TIMEOUT_SECONDS)
    server.shutdown()
    if not finished:
        print("Timed out waiting for GitHub. Run the command again.", file=sys.stderr)
        return 1
    app = registration.result
    print(
        f"Created app '{app['slug']}' (id {app['id']}). Credentials saved to {args.env_file} and {args.key_file}."
    )
    print(f"Install it on a repository: https://github.com/apps/{app['slug']}/installations/new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
