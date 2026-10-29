"""Benchmark command line.

uv run python -m bench validate                 # prove every case is a real, fixable bug
uv run python -m bench run --provider none      # retrieval + reproduction, no model needed
uv run python -m bench run --provider oracle    # also run the fix pipeline with a perfect proposer
uv run python -m bench run --provider ollama    # measure the configured local model
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider
from bench.cases import DEFAULT_CASES_DIR, Case, load_cases
from bench.oracle import OracleProvider
from bench.runner import (
    as_dicts,
    markdown_table,
    run_case,
    scratch_workspace,
    summarize,
    validate_case,
)

RESULTS_DIR = Path(__file__).parent / "results"


def provider_for(kind: str, case: Case) -> LLMProvider | None:
    if kind == "none":
        return None
    if kind == "oracle":
        return OracleProvider(case)
    return OllamaProvider()


def command_validate(args: argparse.Namespace) -> int:
    failed = 0
    for case in load_cases(args.cases, args.only):
        problems = validate_case(case)
        print(f"{'ok  ' if not problems else 'FAIL'} {case.id}")
        for problem in problems:
            print(f"       - {problem}")
        failed += bool(problems)
    return 1 if failed else 0


def command_run(args: argparse.Namespace) -> int:
    if args.provider == "ollama":
        ok, reason = OllamaProvider().available()
        if not ok:
            print(f"cannot run the ollama benchmark: {reason}", file=sys.stderr)
            return 2

    results = []
    started = time.monotonic()
    with scratch_workspace():
        for case in load_cases(args.cases, args.only):
            provider = provider_for(args.provider, case)
            print(f"running {case.id} ...", flush=True)
            results.append(run_case(case, provider, sandbox=not args.no_sandbox))

    summary = summarize(results)
    model = {"none": "none", "oracle": OracleProvider.model}.get(
        args.provider, settings.ollama_model
    )
    report = {
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": model,
        "sandbox": not args.no_sandbox,
        "total_seconds": round(time.monotonic() - started, 1),
        "summary": summary,
        "cases": as_dicts(results),
    }
    out = args.out or RESULTS_DIR / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{args.provider}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print()
    print(markdown_table(results))
    print()
    print(json.dumps(summary, indent=2))
    print(f"\nfull report: {out}")
    for r in results:
        if r.error:
            print(f"ERROR {r.case_id}: {r.error}", file=sys.stderr)
    return 1 if any(r.error for r in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m bench")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--cases", type=Path, default=DEFAULT_CASES_DIR)
        cmd.add_argument("--only", nargs="*", help="run only these case ids")
    run = sub.choices["run"]
    run.add_argument("--provider", choices=["none", "oracle", "ollama"], default="none")
    run.add_argument("--no-sandbox", action="store_true", help="retrieval only; no Docker needed")
    run.add_argument("--out", type=Path)

    args = parser.parse_args()
    return command_validate(args) if args.command == "validate" else command_run(args)


if __name__ == "__main__":
    sys.exit(main())
