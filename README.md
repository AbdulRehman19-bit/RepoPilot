# RepoPilot

AI bug investigator for GitHub. Connect a repository, give it a bug report, and it locates the relevant files, reproduces the problem in a sandbox, proposes a patch, and runs the tests before asking a developer to approve a draft pull request.

The differentiator is **reproducibility and verification**, not just code generation.

## MVP scope

- Language: Python
- Test framework: pytest
- Model: local, via Ollama (CPU only); the system stays useful without it (static analysis + deterministic test runs)
- Stack: FastAPI, PostgreSQL, Redis/RQ, Docker, LangGraph, React

## Status

Work in progress. See `docs/plan.md`. To connect a real GitHub App and repository, follow `docs/pilot.md`; `cd backend && uv run python -m app.doctor --online` checks the setup. Measured results and their limits are in `docs/benchmark.md`; the security model is in `docs/threat-model.md`.

## Sandbox image

Untrusted code (a repository's tests, proposed patches) only ever runs inside a locked-down container. Build the image once:

```bash
docker build -t repopilot-sandbox:py311 docker/sandbox
```

Containers run with no network, a read-only root filesystem, a non-root user, no capabilities, and CPU/memory/process/time/output limits. The sandbox tests (`backend/tests/test_sandbox.py`) are skipped automatically if the image isn't built.

## Review and pull requests

RepoPilot never merges anything. A patch that passed sandbox verification waits for a human to approve it; approving creates a branch and a **draft** pull request from the GitHub App.

- **Sign in with GitHub.** In the GitHub App's settings, set the *Callback URL* to `<where the API is reachable>/auth/callback` (in development: `http://localhost:5173/api/auth/callback`), then put the app's Client ID and a generated client secret in `backend/.env` as `REPOPILOT_GITHUB_CLIENT_ID` and `REPOPILOT_GITHUB_CLIENT_SECRET`. Set `REPOPILOT_FRONTEND_URL` to the page's address. Users then see only the repositories GitHub says they can reach through the app, and need **write** access to a repository to run tests, generate patches, or approve/reject. Their GitHub token is used once at sign-in and not stored. Sessions last `REPOPILOT_SESSION_TTL_HOURS` (4) and are checked against GitHub again only when the user signs in again, so a revoked permission can linger until then (see *Ending sessions* below).
- **API tokens (optional).** For scripts, `REPOPILOT_API_TOKENS="alice:token-one,ci:token-two"` gives each credential a name (`Authorization: Bearer <token>`). The name is what gets recorded as the reviewer, and a typed-in name cannot override it. A token reaches every repository, so treat it like a root password. The older single `REPOPILOT_API_TOKEN` still works but is anonymous.
- **Separate approver (optional).** `REPOPILOT_REQUIRE_SEPARATE_REVIEWER=true` stops whoever asked for a patch from approving it.
- **Ending sessions.** Permissions are read at sign-in and kept for the session (`REPOPILOT_SESSION_TTL_HOURS`, default 4). To apply a change sooner: `cd backend && uv run python -m app.auth.revoke <login>` (or `--all`).
- **Limits.** Per caller per hour: `REPOPILOT_RATE_LIMIT_JOBS_PER_HOUR` (30, for test runs and patch generation) and `REPOPILOT_RATE_LIMIT_AI_PER_HOUR` (60); `REPOPILOT_RATE_LIMIT_LOGIN_PER_MINUTE` (20) per client address. `0` disables a limit. Sandbox runs are also capped at `REPOPILOT_SANDBOX_DISK_LIMIT_BYTES` (1 GB) and `REPOPILOT_SANDBOX_MAX_FILES` (100,000).
- When **neither** sign-in nor a token is configured (local development), the API is open and the approval endpoints are disabled entirely.
- The GitHub App needs these repository permissions: Contents (read & write), Pull requests (read & write), Issues (read), Metadata (read).
- Before opening the PR, RepoPilot checks that the base branch has not moved since verification, re-applies the edits to GitHub's own copy of the files, and refuses unless the result is identical to the verified diff.
