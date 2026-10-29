# First real run (pilot)

Everything so far was tested against fakes of GitHub. This runbook connects RepoPilot to a real
GitHub App and a real repository **you own**, so the parts that have never touched GitHub
(webhooks, installation tokens, sign-in, draft pull requests) get exercised once.

Use a throwaway public repository, not real work. Everything RepoPilot opens is a *draft* pull
request and nothing is ever merged, but it does create a branch and push commits to that repo.

## 1. Create the GitHub App

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.

| Field | Value |
|---|---|
| Name | anything unique, e.g. `repopilot-pilot-<yourname>` |
| Homepage URL | `http://localhost:5173` |
| Callback URL | `http://localhost:5173/api/auth/callback` |
| Expire user authorization tokens | leave on (RepoPilot uses the user token once and discards it) |
| Webhook → Active | yes |
| Webhook URL | your public tunnel URL + `/github/webhook` (step 2) |
| Webhook secret | a long random string; keep it, it goes in `.env` |
| Repository permissions | **Contents: Read & write**, **Pull requests: Read & write**, **Issues: Read-only**, Metadata: Read-only |
| Subscribe to events | **Issues** |
| Where can this app be installed | Only on this account |

After creating it:
- note the **App ID** and **Client ID**;
- **Generate a client secret**;
- **Generate a private key** (downloads a `.pem`). Put it at `backend/github-app.pem`
  (`*.pem` is git-ignored).

Then **Install App** on your account, choosing *Only select repositories* and picking the test
repository. Installing triggers the first webhook, which is how RepoPilot learns the repo exists.

## 2. Make the webhook reachable

GitHub cannot call `localhost`. Run a tunnel to port 8000 and use its public URL in step 1
(cloudflared, ngrok, or smee.io all work). Know what that means: **the tunnel provider sees the
webhook payloads**, which include the text of issues. For a public test repo that is fine; do not
do this with private repositories.

## 3. Configure

In `backend/.env`:

```
REPOPILOT_GITHUB_APP_ID=<App ID>
REPOPILOT_GITHUB_PRIVATE_KEY_PATH=./github-app.pem
REPOPILOT_GITHUB_WEBHOOK_SECRET=<the secret from step 1>
REPOPILOT_GITHUB_CLIENT_ID=<Client ID>
REPOPILOT_GITHUB_CLIENT_SECRET=<client secret>
REPOPILOT_FRONTEND_URL=http://localhost:5173
```

## 4. Start everything

Each in its own terminal:

```bash
docker compose up -d                                   # Postgres + Redis
cd backend && uv run alembic upgrade head
cd backend && uv run python -m app.worker              # indexing, tests, patches
cd backend && uv run uvicorn app.main:app --port 8000  # API (the tunnel points here)
cd frontend && npm run dev                             # page on http://localhost:5173
```

Then check the setup before opening any issue:

```bash
cd backend && uv run python -m app.doctor --online
```

Every `FAIL` line says what to change. `--online` asks GitHub (read-only) who the app is and
whether its permissions and events are enough. Nothing else should be attempted until it is clean.

## 5. The run

1. In GitHub, the app's *Advanced* tab lists **Recent deliveries**. After install there should be
   an `installation` delivery with a green 202. A 401 means the webhook secret differs between
   GitHub and `.env`.
2. Open `http://localhost:5173`, **Sign in with GitHub**. You should see the test repository
   with status *queued/indexing*, then *ready*.
3. In the test repository, open an issue about a real failing test. The issue must **name the
   failing test** (e.g. `tests/test_x.py::test_y`), because reproduction needs a named test.
4. In the page: pick the issue → **Explain with AI** → **Reproduce** → **Generate patch**.
5. If a patch verifies, review the diff and the before/after test output, then **Approve**.
   A draft pull request should appear in the repository.

A good first repository: a tiny Python project with a pure-Python test suite and no native
dependencies, with one deliberately planted bug. Dependencies that need compilers are a known gap.

## 6. What to write down

For each stage, whether it worked and how long it took. Specifically watch for:

- webhook deliveries that are not 202 (and the reason);
- the first real sign-in: a wrong callback URL or permission names are the likely surprises;
- whether the indexed file count and retrieved files for your issue look right to you;
- whether reproduction fails *for the reason in the issue* rather than for some other reason;
- if the draft PR opens: whether its diff is what the page showed.

Record the results in `docs/pilot-results.md`. Failures are the useful part.

## Cleaning up

Uninstall the app from the repository (GitHub → Settings → Applications), delete the branches
named `repopilot/...` it created, and delete the app if you are done with it. To end all
sign-in sessions: `delete from login_sessions;`.
