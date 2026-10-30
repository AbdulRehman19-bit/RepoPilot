# Threat model

What RepoPilot trusts, what an attacker can reach, what is defended, and what is **not**. Written
against the code as of step 17. A control listed here has a test unless marked otherwise.

## Assets

| Asset | Why it matters |
|---|---|
| GitHub App private key | Can mint tokens for every installation. |
| Installation tokens | Read code, and write branches/PRs on connected repositories. |
| API token, webhook secret | Gate the API and the webhook. |
| Customer source code (private repos) | Cloned to disk, indexed in Postgres, sent to the model. |
| The host (worker machine) | Runs Docker and holds all of the above. |

## Untrusted inputs

1. **Webhook bodies**: anyone can POST to the URL.
2. **Issue text**: written by anyone who can open an issue on a connected repo.
3. **Repository contents**: code, tests, config, `conftest.py`, dependency lists. Executed in the sandbox.
4. **Test output**: produced by (3).
5. **Model output**: a proposed patch, shaped by (2) and (3).
6. **API callers**: anyone who can reach the API port.

## Threats and controls

| # | Threat | Control | Status |
|---|---|---|---|
| T1 | Forged webhook | HMAC-SHA256 over the raw body, constant-time compare; fails closed if no secret is set | tested |
| T2 | Replayed webhook (e.g. old `issues` event overwrites newer issue text) | Body-hash claim in Redis for 24 h; released if processing fails so GitHub's redelivery works | tested |
| T3 | Oversized request exhausting memory/CPU | Webhook cap 5 MB, API cap 1 MB, enforced from `Content-Length` and by counting bytes as they stream in, so chunked uploads without a length are cut off too (reading stops at the cap and the answer is 413) | tested |
| T4 | Malformed but correctly signed payload causing 500s | JSON / shape errors return 400 / 422 and roll back | tested |
| T5 | Unauthenticated API use | Sign in with GitHub (random session cookie, HttpOnly, SameSite=Lax, only its hash stored) or the shared bearer token (constant-time compare); approvals/PR creation disabled (503) when neither is configured | tested |
| T13 | A signed-in user reaches another team's repositories | Access map taken from GitHub at sign-in (repositories reachable by both the user and the app); every route checks it; unreachable repositories return 404, read-only ones 403 on anything that runs code or writes | tested |
| T14 | Cross-site request forgery on cookie sessions | SameSite=Lax, plus state-changing requests must carry an `Origin` that matches the host; OAuth `state` is checked against a short-lived cookie | tested |
| T15 | A reviewer is recorded under someone else's name | The recorded reviewer is the signed-in GitHub login; the request body is ignored for signed-in users | tested |
| T6 | Repo code escaping the sandbox or reaching the network | No network, read-only root, `cap-drop ALL`, `no-new-privileges`, uid 1000, memory/CPU/pids/file-size/time limits, no host env, no Docker socket | tested with hostile cases (fork bomb, memory hog, hang, huge output, network) |
| T7 | Secrets leaking into the UI, database, or model prompts through test output or error text | `redact()` on test output and on stored worker errors: GitHub tokens, PEM keys (even truncated), AWS key ids, JWTs, `Authorization` headers, URL credentials, our own configured secrets | tested |
| T8 | Prompt injection in issue text or code | Issue/code are passed as delimited untrusted data, delimiters are neutralised, the model may only propose search/replace edits, never commands | tested |
| T9 | Model "fixes" a bug by editing the tests | Test files, `conftest.py`, `setup.py` are protected; edits there are rejected | tested |
| T10 | Model breaks something else | Regression check against the baseline; a patch is `verified` only if target tests pass and nothing that passed before fails | tested |
| T11 | PR contains something other than what was verified | PR diff is rebuilt from GitHub's own file list and must equal the verified diff; base-branch staleness check | tested |
| T12 | Clone token leaking | Passed via `http.extraheader`, never in the URL; clone errors are redacted | tested |

## Closed in step 24

| # | Threat | Control | Status |
|---|---|---|---|
| T16 | A hostile repo fills the worker's disk | A watchdog polls every writable directory of a run (the checkout copy and any install target) and kills the container past `sandbox_disk_limit_bytes` (1 GB) or `sandbox_max_files` (100,000); an oversized checkout is refused before any container starts; the copy is deleted either way | tested with endless-file and endless-byte attacks |
| T17 | One caller queues unlimited work | Per-caller hourly limits on sandbox/patch jobs (30) and model calls (60), shared across API processes via Redis; sign-in endpoints are limited per client address per minute (20); `429` with `Retry-After` | tested |
| T18 | An API credential cannot be traced to a person | `REPOPILOT_API_TOKENS="alice:...,ci:..."` gives each credential a name that is recorded as the reviewer and as the requester of a patch and cannot be overridden by the request body | tested |
| T19 | The person who asked for a patch also approves it | `REPOPILOT_REQUIRE_SEPARATE_REVIEWER=true` refuses approval by the recorded requester (off by default, since a solo user would be locked out) | tested |
| T20 | Revoked access keeps working until the session expires | Sessions default to 4 hours; `python -m app.auth.revoke <login>` or `--all` ends them immediately | tested |
| T21 | Cookie requests refused behind a proxy that rewrites `Host` | The configured `frontend_url` origin is accepted alongside the request's own host | tested |
| T22 | The dependency-install container reaches arbitrary hosts | It runs on a Docker `--internal` network (no route out) with a locked-down proxy container (read-only, no capabilities, uid 1000, 128 MB) that tunnels HTTPS only to `pypi.org` and `files.pythonhosted.org`, port 443, by exact hostname (no IP literals, no other ports); the network and proxy are removed afterwards, also on failure | tested: direct connections fail, other hosts get 403, a pip index on another host fails, an end-to-end install works |

## Known gaps (not fixed)

- **Dependency install can still reach PyPI**, and a repository can make RepoPilot install any
  package name it chooses from there (typosquats included). What limits the damage: only
  prebuilt wheels are installed (no build scripts run), direct URLs, VCS references, local paths
  and option lines are skipped, the container holds no secrets and no project code, and the
  disk quota applies. The installed code then only ever runs with the network off. A mirror that
  serves an allowlist of packages would close the rest. Setting `REPOPILOT_INSTALL_EGRESS_PROXY=false`
  removes all of this and gives the installer the open internet; do not.
- **The egress proxy trusts DNS on the worker's machine** to resolve the two allowed names.
- **Redaction is pattern-based.** A secret in an unrecognised format passes through. Secrets
  committed to a repo are also in the copy the sandbox sees.
- **Replay window is 24 h**, and the Redis claim fails open if Redis is down. GitHub sends no
  timestamp, so an older capture of a valid signed body can be replayed after the window.
- **Permissions are a snapshot** for the session (4 hours by default). Removing someone's access
  on GitHub takes effect at the next sign-in, or immediately with the revoke command.
- **API tokens reach every repository.** Naming them makes them traceable, not narrower. Leave
  them unset if no scripts need the API.
- **Sign-in has not been exercised against real GitHub.** It is tested against a faithful fake of
  the OAuth and REST endpoints; the first real login may surface differences (callback URL,
  permission names).
- **Rate limits fail open if Redis is down**, and the per-address login limit sees the proxy's
  address, not the user's, when the API sits behind a reverse proxy that does not forward it.
- **Source code goes to the model.** With the local Ollama provider it stays on the machine;
  any hosted provider would receive private code.
- **TLS is the proxy's job.** `docs/deploy.md` ships a Caddy configuration (automatic certificates, HSTS, a strict Content-Security-Policy and other headers) that was exercised with a local CA only, not with a real domain.
- **The Docker daemon is trusted.** Anyone who can start the worker's containers effectively
  controls the host; the sandbox is only as strong as Docker's isolation (shared kernel).
