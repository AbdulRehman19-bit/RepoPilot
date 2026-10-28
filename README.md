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
