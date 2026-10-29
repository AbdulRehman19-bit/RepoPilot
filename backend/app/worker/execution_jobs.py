from dataclasses import asdict

from app.config import settings
from app.db import SessionLocal
from app.models import Repository, TestExecution
from app.sandbox.deps import DependencyError, prepare_dependencies
from app.sandbox.pytest_runner import InvalidSelector, run_pytest
from app.sandbox.runner import SandboxError
from app.security.redact import redact

MAX_STORED_OUTPUT_CHARS = 100_000


def run_execution_job(execution_id: int) -> None:
    """Prepare dependencies, then run the requested tests in the sandbox and store the result."""
    with SessionLocal() as session:
        execution = session.get(TestExecution, execution_id)
        if execution is None:
            return
        repo = session.get(Repository, execution.repository_id)
        execution.status = "running"
        execution.error = None
        session.commit()

        try:
            checkout = settings.workspace_dir / str(repo.id)
            if not checkout.is_dir():
                raise SandboxError("repository checkout is missing; re-index the repository")
            deps = prepare_dependencies(checkout)
            outcome = run_pytest(checkout, deps, execution.selectors)

            execution.commit_sha = repo.indexed_commit
            execution.exit_code = outcome.exit_code
            execution.passed = outcome.passed
            execution.failed = outcome.failed
            execution.errors = outcome.errors
            execution.skipped = outcome.skipped
            execution.timed_out = outcome.timed_out
            execution.duration_seconds = outcome.duration_seconds
            execution.failures = [asdict(f) for f in outcome.failures]
            execution.warnings = deps.warnings
            execution.output = outcome.output[:MAX_STORED_OUTPUT_CHARS]
            execution.status = "completed"
        except (DependencyError, SandboxError, InvalidSelector) as exc:
            execution.status = "error"
            execution.error = redact(str(exc))[:2000]
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded on the execution
            execution.status = "error"
            execution.error = redact(f"unexpected error: {exc}")[:2000]
        session.commit()
