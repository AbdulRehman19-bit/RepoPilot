"""Benchmark case format.

bench/cases/<id>/
  case.json   id, kind, description, issue {title, body}, expected_files, failing_tests
  fix.json    the reference fix, as search/replace edits (the same format the model uses)
  repo/       the buggy project snapshot
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.patching.apply import Edit

DEFAULT_CASES_DIR = Path(__file__).parent / "cases"


@dataclass(frozen=True)
class Case:
    id: str
    kind: str  # "identifier" | "traceback" | "prose" | "misdirected"
    description: str
    title: str
    body: str
    expected_files: tuple[str, ...]
    failing_tests: tuple[str, ...]
    fix_edits: tuple[Edit, ...]
    repo_dir: Path


def load_case(directory: Path) -> Case:
    spec = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    fix = json.loads((directory / "fix.json").read_text(encoding="utf-8"))
    return Case(
        id=spec["id"],
        kind=spec["kind"],
        description=spec["description"],
        title=spec["issue"]["title"],
        body=spec["issue"]["body"],
        expected_files=tuple(spec["expected_files"]),
        failing_tests=tuple(spec["failing_tests"]),
        fix_edits=tuple(Edit(**edit) for edit in fix),
        repo_dir=directory / "repo",
    )


def load_cases(root: Path = DEFAULT_CASES_DIR, only: list[str] | None = None) -> list[Case]:
    directories = sorted(p for p in root.iterdir() if (p / "case.json").is_file())
    cases = [load_case(d) for d in directories]
    if only:
        unknown = set(only) - {c.id for c in cases}
        if unknown:
            raise SystemExit(f"unknown case(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in only]
    return cases
