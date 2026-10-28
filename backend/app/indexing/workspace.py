import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path | None = None, token: str | None = None) -> str:
    cmd = ["git"]
    secrets: list[str] = []
    if token:
        # Pass the token per-invocation so it never lands in the URL or .git/config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        secrets += [token, basic]
        cmd += ["-c", f"http.extraheader=Authorization: Basic {basic}"]
    cmd += args
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        cmd, check=False, cwd=cwd, capture_output=True, text=True, timeout=300, env=env
    )
    if result.returncode != 0:
        stderr = result.stderr
        for secret in secrets:
            stderr = stderr.replace(secret, "***")
        raise GitError(f"git {args[0]} failed: {stderr.strip()}")
    return result.stdout.strip()


def clone_repository(clone_url: str, dest: Path, token: str | None = None) -> str:
    """Shallow-clone `clone_url` into `dest` and return the checked-out commit SHA."""
    if dest.exists() and any(dest.iterdir()):
        raise GitError(f"destination {dest} is not empty")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--depth", "1", "--no-tags", clone_url, str(dest)], token=token)
    return _git(["rev-parse", "HEAD"], cwd=dest)


def remove_tree(path: Path) -> None:
    """Delete a checkout; git pack files are read-only on Windows, so clear that bit and retry."""

    def make_writable(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onerror=make_writable)
