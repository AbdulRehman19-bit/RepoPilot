"""A tiny in-memory stand-in for the parts of the GitHub REST API that RepoPilot writes to."""

import base64
import json
import re

import httpx

from app.github.pulls import GitHubClient


class FakeGitHub:
    def __init__(self, files: dict[str, bytes], head_sha: str = "a" * 40, branch: str = "main"):
        self.files = dict(files)
        self.head_sha = head_sha
        self.branch = branch
        self.branches: dict[str, dict[str, bytes]] = {}
        self.deleted: list[str] = []
        self.pulls: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.fail_put = False
        self.fail_everything = False

    def client(self) -> GitHubClient:
        http = httpx.Client(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.handle)
        )
        return GitHubClient("test-token", client=http)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if self.fail_everything:
            return httpx.Response(500, json={"message": "server on fire"})
        body = json.loads(request.content) if request.content else {}

        if method == "GET" and re.fullmatch(r"/repos/o/r/git/ref/heads/.+", path):
            return httpx.Response(200, json={"object": {"sha": self.head_sha}})

        if method == "GET" and path.startswith("/repos/o/r/contents/"):
            name = path.removeprefix("/repos/o/r/contents/")
            if name not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.encodebytes(self.files[name]).decode(),
                    "sha": f"blob-{name}",
                },
            )

        if method == "POST" and path == "/repos/o/r/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches[name] = dict(self.files)
            return httpx.Response(201, json={"ref": body["ref"]})

        if method == "PUT" and path.startswith("/repos/o/r/contents/"):
            if self.fail_put:
                return httpx.Response(409, json={"message": "conflict"})
            name = path.removeprefix("/repos/o/r/contents/")
            assert body["sha"] == f"blob-{name}", "must update the blob it read"
            self.branches[body["branch"]][name] = base64.b64decode(body["content"])
            return httpx.Response(200, json={"content": {"path": name}})

        if method == "POST" and path == "/repos/o/r/pulls":
            number = len(self.pulls) + 7
            self.pulls.append({**body, "number": number})
            return httpx.Response(
                201, json={"number": number, "html_url": f"https://github.test/o/r/pull/{number}"}
            )

        if method == "DELETE" and path.startswith("/repos/o/r/git/refs/heads/"):
            name = path.removeprefix("/repos/o/r/git/refs/heads/")
            self.deleted.append(name)
            self.branches.pop(name, None)
            return httpx.Response(204)

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})
