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
| T3 | Oversized request exhausting memory/CPU | Webhook cap 5 MB (checked from `Content-Length` and again on the body); API cap 1 MB via `Content-Length` | tested |
| T4 | Malformed but correctly signed payload causing 500s | JSON / shape errors return 400 / 422 and roll back | tested |
| T5 | Unauthenticated API use | Bearer token, constant-time compare; approvals/PR creation disabled (503) without a token | tested |
| T6 | Repo code escaping the sandbox or reaching the network | No network, read-only root, `cap-drop ALL`, `no-new-privileges`, uid 1000, memory/CPU/pids/file-size/time limits, no host env, no Docker socket | tested with hostile cases (fork bomb, memory hog, hang, huge output, network) |
| T7 | Secrets leaking into the UI, database, or model prompts through test output or error text | `redact()` on test output and on stored worker errors: GitHub tokens, PEM keys (even truncated), AWS key ids, JWTs, `Authorization` headers, URL credentials, our own configured secrets | tested |
| T8 | Prompt injection in issue text or code | Issue/code are passed as delimited untrusted data, delimiters are neutralised, the model may only propose search/replace edits, never commands | tested |
| T9 | Model "fixes" a bug by editing the tests | Test files, `conftest.py`, `setup.py` are protected; edits there are rejected | tested |
| T10 | Model breaks something else | Regression check against the baseline; a patch is `verified` only if target tests pass and nothing that passed before fails | tested |
| T11 | PR contains something other than what was verified | PR diff is rebuilt from GitHub's own file list and must equal the verified diff; base-branch staleness check | tested |
| T12 | Clone token leaking | Passed via `http.extraheader`, never in the URL; clone errors are redacted | tested |

## Known gaps (not fixed)

- **Dependency install has network access.** The install phase must reach PyPI, so a malicious
  dependency name or sdist-free wheel runs with network (but no secrets, no project code).
  Mitigation to build: a package allowlist or a pull-through mirror.
- **Disk exhaustion.** Per-file size is capped (50 MB) but the number of files and the total
  written to the throwaway workspace are not. A hostile repo can fill the worker's disk.
- **Redaction is pattern-based.** A secret in an unrecognised format passes through. Secrets
  committed to a repo are also in the copy the sandbox sees.
- **Replay window is 24 h**, and the Redis claim fails open if Redis is down. GitHub sends no
  timestamp, so an older capture of a valid signed body can be replayed after the window.
- **One shared API token** and no per-user authorisation: anyone holding the token can see every
  connected repository's data and approve any patch. Real GitHub login is not built.
- **No rate limiting.** An authenticated caller can queue unlimited jobs. `Content-Length`-less
  (chunked) API bodies are not size-capped at the app layer; put a reverse proxy in front.
- **Source code goes to the model.** With the local Ollama provider it stays on the machine;
  any hosted provider would receive private code.
- **TLS and network exposure are not handled by the app.** Run it behind a TLS-terminating proxy.
- **The Docker daemon is trusted.** Anyone who can start the worker's containers effectively
  controls the host; the sandbox is only as strong as Docker's isolation (shared kernel).
