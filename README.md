# RepoPilot

AI bug investigator for GitHub. Connect a repository, give it a bug report, and it locates the relevant files, reproduces the problem in a sandbox, proposes a patch, and runs the tests before asking a developer to approve a draft pull request.

The differentiator is **reproducibility and verification**, not just code generation.

## MVP scope

- Language: Python
- Test framework: pytest
- Model: local, via Ollama (CPU only); the system stays useful without it (static analysis + deterministic test runs)
- Stack: FastAPI, PostgreSQL, Redis/RQ, Docker, LangGraph, React

## Status

Work in progress. See `docs/plan.md`.

## Sandbox image

Untrusted code (a repository's tests, proposed patches) only ever runs inside a locked-down container. Build the image once:

```bash
docker build -t repopilot-sandbox:py311 docker/sandbox
```

Containers run with no network, a read-only root filesystem, a non-root user, no capabilities, and CPU/memory/process/time/output limits. The sandbox tests (`backend/tests/test_sandbox.py`) are skipped automatically if the image isn't built.
