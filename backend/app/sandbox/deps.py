"""Prepare a repository's third-party dependencies for sandboxed test runs.

Two phases, so untrusted test code never has network access:

1. Install (network on): only prebuilt wheels are fetched (`--only-binary=:all:`), so no
   repository or sdist build script executes while the network is reachable. The project
   itself is never pip-installed; its code is put on PYTHONPATH instead.
2. Test (network off): the installed packages are mounted read-only.
"""

import hashlib
import os
import shutil
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.indexing.workspace import remove_tree
from app.sandbox.runner import Mount, SandboxError, run_in_sandbox

REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements_dev.txt",
    "requirements_test.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)
TEST_EXTRAS = ("test", "tests", "testing", "dev")
INSTALL_TIMEOUT_SECONDS = 600
INSTALL_MEMORY = "1g"
COMPLETE_MARKER = ".complete"


class DependencyError(RuntimeError):
    pass


@dataclass
class Requirements:
    specs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, spec: str, source: str) -> None:
        spec = spec.split(" #")[0].strip()
        if not spec or spec.startswith("#"):
            return
        if spec.startswith("-"):
            self.warnings.append(f"{source}: skipped option line {spec!r}")
        elif "://" in spec or " @ " in spec or spec.startswith((".", "/")):
            self.warnings.append(f"{source}: skipped direct/local reference {spec!r}")
        elif spec not in self.specs:
            self.specs.append(spec)


def collect_requirements(checkout: Path) -> Requirements:
    """Gather installable requirement specifiers from the usual manifest files."""
    found = Requirements()
    for name in REQUIREMENT_FILES:
        path = checkout / name
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                found.add(line, name)

    pyproject = checkout / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            found.warnings.append(f"pyproject.toml: could not parse ({exc})")
            data = {}
        project = data.get("project", {})
        for spec in project.get("dependencies", []):
            found.add(spec, "pyproject.toml")
        extras = project.get("optional-dependencies", {})
        for extra in TEST_EXTRAS:
            for spec in extras.get(extra, []):
                found.add(spec, f"pyproject.toml[{extra}]")
        groups = data.get("dependency-groups", {})
        for group in TEST_EXTRAS:
            for item in groups.get(group, []):
                if isinstance(item, str):
                    found.add(item, f"dependency-group {group}")

    if not found.specs and not found.warnings:
        found.warnings.append(
            "no dependency manifest found; running with the standard library only"
        )
    return found


def requirements_hash(specs: list[str], image: str) -> str:
    digest = hashlib.sha256("\n".join([image, *sorted(specs)]).encode())
    return digest.hexdigest()[:20]


@dataclass
class DependencyEnv:
    """A prepared set of packages, or an empty environment when nothing needs installing."""

    site_packages: Path | None
    key: str
    cached: bool
    warnings: list[str]

    def mounts_and_env(self, checkout_target: str = "/workspace") -> tuple[list[Mount], dict]:
        paths = [checkout_target, f"{checkout_target}/src"]
        mounts: list[Mount] = []
        if self.site_packages is not None:
            mounts.append(Mount(self.site_packages, "/deps", writable=False))
            paths.insert(0, "/deps")
        return mounts, {"PYTHONPATH": ":".join(paths)}


def prepare_dependencies(checkout: Path) -> DependencyEnv:
    requirements = collect_requirements(checkout)
    if not requirements.specs:
        return DependencyEnv(None, "none", True, requirements.warnings)

    key = requirements_hash(requirements.specs, settings.sandbox_image)
    cache_dir = settings.workspace_dir / "deps" / key
    site_packages = cache_dir / "site-packages"
    if (cache_dir / COMPLETE_MARKER).exists():
        return DependencyEnv(site_packages, key, True, requirements.warnings)

    # Build in a private directory and move it into place so a crash never leaves a half-cache.
    building = settings.workspace_dir / "deps" / f"{key}.{uuid.uuid4().hex[:8]}.building"
    (building / "site-packages").mkdir(parents=True)
    os.chmod(building / "site-packages", 0o777)  # container user differs from the worker's
    manifest_dir = building / "manifest"
    manifest_dir.mkdir()
    (manifest_dir / "requirements.txt").write_text("\n".join(requirements.specs) + "\n")
    try:
        result = run_in_sandbox(
            manifest_dir,
            [
                "python", "-m", "pip", "install",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--target", "/deps",
                "-r", "/workspace/requirements.txt",
            ],
            network=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            memory=INSTALL_MEMORY,
            mounts=[Mount(building / "site-packages", "/deps", writable=True)],
        )  # fmt: skip
        if not result.ok:
            tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
            reason = "timed out" if result.timed_out else "\n".join(tail)
            raise DependencyError(f"dependency installation failed: {reason}")

        (building / COMPLETE_MARKER).write_text(key)
        shutil.rmtree(manifest_dir, ignore_errors=True)
        try:
            building.rename(cache_dir)
        except OSError:
            # Another worker finished the same key first; use theirs.
            if not (cache_dir / COMPLETE_MARKER).exists():
                raise
            remove_tree(building)
    except SandboxError as exc:
        raise DependencyError(str(exc)) from exc
    finally:
        if building.exists():
            remove_tree(building)
    return DependencyEnv(site_packages, key, False, requirements.warnings)
