"""Open a draft pull request containing exactly the patch that was verified in the sandbox."""

import base64
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.config import settings
from app.github.auth import mint_installation_token
from app.patching.apply import Edit, PatchError, apply_to_texts, edit_paths

API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class StaleBase(GitHubError):
    """The base branch moved since the patch was verified."""


class PatchMismatch(GitHubError):
    """The files on GitHub differ from what the patch was verified against."""


@dataclass
class PullRequest:
    url: str
    number: int
    branch: str


class GitHubClient:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=settings.github_api_url, timeout=30)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"could not reach GitHub: {exc}") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise GitHubError(
                f"GitHub {response.status_code}: {message[:300]}", response.status_code
            )
        return response.json() if response.content else {}


def get_github_client(installation_id: int) -> GitHubClient:
    return GitHubClient(mint_installation_token(installation_id).token)


def build_pr_body(
    *,
    issue_number: int,
    issue_title: str,
    explanation: str,
    attempts: list[dict],
    before_failing: list[str],
    reviewer: str | None,
    note: str | None,
) -> str:
    """The pull request description: what changed, and the evidence it was verified."""
    verified = next((a for a in attempts if a.get("outcome") == "verified"), {})
    targeted, full = verified.get("targeted") or {}, verified.get("full") or {}
    lines = [
        f"Proposes a fix for #{issue_number}: {issue_title}",
        "",
        "## What changed",
        explanation or "_No explanation was provided._",
        "",
        "## Verification",
        "Run in an isolated sandbox (no network) before this pull request was opened:",
        "",
        f"- Before: reproduction failed ({', '.join(f'`{t}`' for t in before_failing) or 'n/a'})",
        f"- After: reproduction passes ({targeted.get('passed', '?')} passed)",
        f"- Full test suite: {full.get('passed', '?')} passed, no new failures",
        f"- Attempts needed: {len(attempts)}",
        "",
        "## Review",
        f"Approved by {reviewer or 'a maintainer'} in RepoPilot.",
    ]
    if note:
        lines += ["", f"> {note.strip()[:500]}"]
    lines += [
        "",
        "This is a draft written by an AI model. Please review the diff carefully before merging.",
        "",
        "_Opened by RepoPilot._",
    ]
    return "\n".join(lines)


def open_draft_pr(
    gh: GitHubClient,
    *,
    full_name: str,
    base_branch: str,
    base_sha: str,
    edits: list[dict],
    verified_diff: str,
    protected: set[str],
    issue_number: int,
    patch_id: int,
    title: str,
    body: str,
) -> PullRequest:
    repo = f"/repos/{full_name}"
    branch = f"repopilot/issue-{issue_number}-patch-{patch_id}"

    # 1. The patch is only known to work on the commit it was verified against.
    head = gh.request("GET", f"{repo}/git/ref/heads/{quote(base_branch, safe='/')}")
    if head["object"]["sha"] != base_sha:
        raise StaleBase(
            f"{base_branch} has moved since this patch was verified "
            f"({head['object']['sha'][:7]} != {base_sha[:7]}); re-index and generate a new patch"
        )

    # 2. Rebuild the change from GitHub's own copy of the files and require the identical diff.
    parsed = [Edit(**e) for e in edits]
    try:
        paths = edit_paths(parsed, protected)
    except PatchError as exc:
        raise PatchMismatch(f"stored edits are not allowed: {exc}") from exc

    originals: dict[str, str] = {}
    blob_shas: dict[str, str] = {}
    newlines: dict[str, str] = {}
    for path in paths:
        info = gh.request("GET", f"{repo}/contents/{quote(path)}", params={"ref": base_sha})
        if info.get("encoding") != "base64" or "content" not in info:
            raise PatchMismatch(f"could not read {path} from GitHub")
        raw = base64.b64decode(info["content"]).decode("utf-8")
        newlines[path] = "\r\n" if "\r\n" in raw else "\n"
        originals[path] = raw.replace("\r\n", "\n")
        blob_shas[path] = info["sha"]
    try:
        applied = apply_to_texts(originals, parsed, protected)
    except PatchError as exc:
        raise PatchMismatch(f"the patch no longer applies to the files on GitHub: {exc}") from exc
    if applied.diff != verified_diff:
        raise PatchMismatch("the files on GitHub differ from the version the patch was verified on")

    # 3. Branch, commit each changed file, open the draft.
    gh.request("POST", f"{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
    try:
        for path in applied.files:
            content = applied.updated[path].replace("\n", newlines[path]).encode("utf-8")
            gh.request(
                "PUT",
                f"{repo}/contents/{quote(path)}",
                json={
                    "message": f"Fix #{issue_number}: update {path}",
                    "content": base64.b64encode(content).decode(),
                    "sha": blob_shas[path],
                    "branch": branch,
                },
            )
        pr = gh.request(
            "POST",
            f"{repo}/pulls",
            json={"title": title, "head": branch, "base": base_branch, "body": body, "draft": True},
        )
    except GitHubError:
        # Do not leave a half-built branch behind.
        try:
            gh.request("DELETE", f"{repo}/git/refs/heads/{quote(branch, safe='/')}")
        except GitHubError:
            pass
        raise
    return PullRequest(url=pr["html_url"], number=pr["number"], branch=branch)
