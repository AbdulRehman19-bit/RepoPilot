"""Run a repository's pytest suite in the sandbox and parse the outcome."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.sandbox.deps import DependencyEnv
from app.sandbox.runner import SandboxResult, run_in_sandbox

# Pytest node ids such as tests/test_x.py::TestA::test_b[param]
NODE_ID = re.compile(r"[\w./-]+\.py(?:::[\w\[\].,\-]+)+")
SAFE_SELECTOR = re.compile(r"^[\w][\w./\-]*(?:::[\w\[\].,\-]+)*$")
MAX_SELECTORS = 25
FAILURE_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", re.MULTILINE)
COUNT = re.compile(r"(\d+) (failed|passed|skipped|errors?|xfailed|xpassed|deselected)")


class InvalidSelector(ValueError):
    pass


@dataclass
class Failure:
    kind: str  # "FAILED" | "ERROR"
    node_id: str
    message: str


@dataclass
class PytestOutcome:
    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failures: list[Failure] = field(default_factory=list)
    timed_out: bool = False
    duration_seconds: float = 0.0
    output: str = ""
    truncated: bool = False

    @property
    def all_passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def no_tests_ran(self) -> bool:
        return self.exit_code == 5  # pytest: "no tests collected"


def extract_node_ids(text: str) -> list[str]:
    """Pytest node ids mentioned in an issue or log, in order of first appearance."""
    # Strip sentence punctuation that the pattern also allows inside parametrize ids.
    found = (match.rstrip(".,") for match in NODE_ID.findall(text))
    return list(dict.fromkeys(found))[:MAX_SELECTORS]


def validate_selectors(selectors: list[str]) -> list[str]:
    """Selectors become pytest arguments, so reject anything that could be read as an option."""
    if len(selectors) > MAX_SELECTORS:
        raise InvalidSelector(f"at most {MAX_SELECTORS} selectors are allowed")
    for selector in selectors:
        if not SAFE_SELECTOR.match(selector) or ".." in selector.split("/"):
            raise InvalidSelector(f"not a valid test selector: {selector!r}")
    return selectors


def parse_pytest_output(stdout: str) -> dict:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    # The final summary line looks like "1 failed, 2 passed, 1 skipped in 0.12s".
    for line in reversed(stdout.strip().splitlines()):
        if " in " in line and COUNT.search(line):
            for number, label in COUNT.findall(line):
                key = "errors" if label.startswith("error") else label
                if key in counts:
                    counts[key] = int(number)
            break
    failures = [
        Failure(kind, node_id, (message or "").strip())
        for kind, node_id, message in FAILURE_LINE.findall(stdout)
    ]
    return {**counts, "failures": failures}


def run_pytest(
    checkout: Path,
    deps: DependencyEnv,
    selectors: list[str] | None = None,
    timeout: int | None = None,
) -> PytestOutcome:
    selectors = validate_selectors(selectors or [])
    mounts, env = deps.mounts_and_env()
    result: SandboxResult = run_in_sandbox(
        checkout,
        [
            "python",
            "-m",
            "pytest",
            "-q",
            "-rfE",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            *selectors,
        ],
        timeout=timeout,
        mounts=mounts,
        env=env,
    )
    parsed = parse_pytest_output(result.stdout)
    return PytestOutcome(
        exit_code=result.exit_code,
        passed=parsed["passed"],
        failed=parsed["failed"],
        errors=parsed["errors"],
        skipped=parsed["skipped"],
        failures=parsed["failures"],
        timed_out=result.timed_out,
        duration_seconds=result.duration_seconds,
        output=(result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")),
        truncated=result.truncated,
    )
