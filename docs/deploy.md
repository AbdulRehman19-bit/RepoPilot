# Deployment

A single-machine deployment: one host runs Docker, the stack below, and the worker.

```
 browser ──HTTPS──> Caddy ──/api/*──> API (container, read-only, non-root)
                      │                  │
                      └─ static React    ├── Postgres (container, 127.0.0.1 only)
                         build           └── Redis    (container, password, 127.0.0.1 only)

 GitHub ──HTTPS──> Caddy ──> /api/github/webhook

 worker (ON THE HOST, not in a container) ──> Postgres/Redis on 127.0.0.1
        └──> Docker ──> sandbox containers (no network, read-only, quota)
                    └─> install containers (isolated network, PyPI-only proxy)
```

**Why the worker is not a container.** It starts sandbox containers, so it needs the Docker
socket, and a container with the Docker socket is root on the host. That would make the sandbox
pointless. Keep the worker a plain host process.

## What was verified

The compose stack was brought up end to end on a developer machine with throwaway secrets, behind
Caddy's local CA, and exercised with `curl`: the built frontend and its client-side routing, the
API under `/api`, security headers, `401` without credentials and `200` with a named token,
unsigned webhooks refused, HTTP redirected to HTTPS, the API container healthy while read-only
and non-root, migrations run by a one-shot service, and the host-side doctor passing against the
published Postgres and Redis.

**Not verified:** a real domain with Let's Encrypt, GitHub talking to it (see `docs/pilot.md`),
behaviour under load, anything on Linux (everything above ran on Windows with Docker Desktop),
and the worker running as a service.

## Steps

1. **Prerequisites:** Docker, Node 20+ (to build the frontend), `uv` and Python 3.11+ (for the
   worker), a DNS name pointing at the host with ports 80 and 443 open, and the sandbox image
   (`docker build -t repopilot-sandbox:py311 docker/sandbox`).
2. **Secrets:** `cp .env.prod.example .env.prod` and fill it in. Generate long random values for
   the two passwords and the webhook secret. Put the GitHub App's private key at
   `deploy/github-app.pem` (git-ignored) and set `GITHUB_APP_KEY_FILE` to it. `.env.prod` and
   `*.pem` are git-ignored; keep it that way.
3. **GitHub App:** follow `docs/pilot.md` section 1, but use `https://<your domain>` in place of
   the tunnel and `localhost`: webhook URL `https://<domain>/api/github/webhook`, callback URL
   `https://<domain>/api/auth/callback`.
4. **Frontend:** `cd frontend && npm ci && npm run build`.
5. **Stack:** `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`.
   Migrations run automatically before the API starts.
6. **Worker** (on the host, in `backend/`), with the same secrets, pointing at the published
   loopback ports:

   ```bash
   export REPOPILOT_DATABASE_URL='postgresql+psycopg://repopilot:<POSTGRES_PASSWORD>@127.0.0.1:5434/repopilot'
   export REPOPILOT_REDIS_URL='redis://:<REDIS_PASSWORD>@127.0.0.1:6381/0'
   export REPOPILOT_GITHUB_APP_ID=<id> REPOPILOT_GITHUB_PRIVATE_KEY_PATH=/path/to/github-app.pem
   export REPOPILOT_GITHUB_WEBHOOK_SECRET=<secret>
   uv run python -m app.doctor --online     # fix every FAIL first
   uv run python -m app.worker
   ```

   Run it under a supervisor so it restarts. On Linux a systemd unit with
   `Restart=on-failure`, `User=` a dedicated account in the `docker` group, and the variables
   above in an `EnvironmentFile=` is the usual shape; on Windows use a scheduled task or NSSM.
   (Neither has been tried here.)
7. **Check:** `https://<domain>` loads, **Sign in with GitHub** works, and
   `uv run python -m app.doctor --online` is clean.

## Operating it

- **Updating:** `git pull`, rebuild the frontend, `docker compose ... up -d --build`, restart the
  worker. Migrations apply on the way up.
- **Backups:** the only state worth keeping is Postgres: `docker compose ... exec postgres
  pg_dump -U repopilot repopilot > backup.sql`. Indexed code is rebuilt from GitHub by
  re-indexing; Redis holds only queues and counters. The restore path has not been rehearsed.
- **Ending sessions after a permission change:** `cd backend && uv run python -m app.auth.revoke
  <login>` (or `--all`), run against the same database URL.
- **Logs:** `docker compose ... logs api caddy`. The worker logs to its own terminal or journal.
- **Disk:** sandbox scratch space and the dependency cache live under `REPOPILOT_WORKSPACE_DIR`
  on the host. The dependency cache is never pruned automatically.

## Hardening notes

- Postgres and Redis publish only on `127.0.0.1`; Redis requires a password. If the host is
  shared, anyone with a shell on it can still reach them.
- The API container is read-only with every capability dropped and runs as a non-root user.
- Caddy sets HSTS, `nosniff`, `X-Frame-Options: DENY` and a same-origin referrer policy. There is
  no Content-Security-Policy yet.
- Per-address rate limits use the client address that Caddy forwards. The compose file trusts
  forwarded headers from any address on the compose network, which is fine because only Caddy
  can reach the API there; do not expose the API container's port directly.
- Everything in `docs/threat-model.md` still applies, in particular: permissions are a snapshot
  per session, the Docker daemon is trusted, and source code is sent to the model you configure.

## Not covered

Multiple workers or hosts, high availability, log shipping and alerting, a CSP, secret rotation
procedures, and automated backups.
