"""Run untrusted code (a repository's tests, a proposed patch) inside a locked-down container.

Nothing from the host leaks in except a throwaway copy of the checkout: no network, no host
environment variables, no Docker socket, read-only root filesystem, non-root user, no
capabilities, and hard CPU/memory/process/time/output limits.
"""

import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.indexing.workspace import remove_tree

COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__")
KILL_GRACE_SECONDS = 15
TIMEOUT_EXIT_CODE = 124  # same convention as coreutils `timeout`
OOM_OR_KILLED_EXIT_CODE = 137


@dataclass(frozen=True)
class Mount:
    """An extra host directory made visible inside the container."""

    host: Path
    target: str
    writable: bool = False


class SandboxError(RuntimeError):
    """The sandbox itself failed (Docker unavailable, image missing), not the code inside it."""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    killed: bool  # SIGKILL from outside the program: usually the memory limit
    truncated: bool
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class _CappedReader(threading.Thread):
    """Drain a pipe, keeping only the first `limit` bytes so output can't exhaust host memory."""

    def __init__(self, stream, limit: int):
        super().__init__(daemon=True)
        self.stream, self.limit = stream, limit
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        while chunk := self.stream.read(65536):
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def docker_command(
    name: str,
    workdir: Path,
    command: list[str],
    *,
    image: str,
    memory: str,
    cpus: float,
    pids_limit: int,
    mounts: Sequence[Mount] = (),
    env: Mapping[str, str] | None = None,
    network: bool = False,
) -> list[str]:
    extra: list[str] = []
    for key, value in (env or {}).items():
        extra += ["-e", f"{key}={value}"]
    for mount in mounts:
        mode = "rw" if mount.writable else "ro"
        extra += ["-v", f"{mount.host.resolve().as_posix()}:{mount.target}:{mode}"]
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "bridge" if network else "none",
        "--memory", memory, "--memory-swap", memory,
        "--cpus", str(cpus),
        "--pids-limit", str(pids_limit),
        "--ulimit", "nofile=256:256",
        "--ulimit", "fsize=52428800",  # no single file larger than 50 MB
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        "-e", "HOME=/tmp",
        "-v", f"{workdir.resolve().as_posix()}:/workspace",
        *extra,
        "-w", "/workspace",
        image,
        *command,
    ]  # fmt: skip


def _make_world_writable(root: Path) -> None:
    """The container user (uid 1000) differs from the worker's user, so open up the copy."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(
            mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO if path.is_dir() else mode | 0o666
        )


def _copy_checkout(source: Path, scratch_root: Path) -> Path:
    destination = scratch_root / uuid.uuid4().hex
    # symlinks=True copies links as links; they are never followed on the host.
    shutil.copytree(source, destination, ignore=COPY_IGNORE, symlinks=True)
    _make_world_writable(destination)
    return destination


def run_in_sandbox(
    checkout: Path,
    command: list[str],
    *,
    timeout: int | None = None,
    image: str | None = None,
    memory: str | None = None,
    cpus: float | None = None,
    pids_limit: int | None = None,
    output_limit: int | None = None,
    keep_changes_in: Path | None = None,
    mounts: Sequence[Mount] = (),
    env: Mapping[str, str] | None = None,
    network: bool = False,
) -> SandboxResult:
    """Run `command` inside a container against a private copy of `checkout`.

    The original checkout is never modified. If `keep_changes_in` is given, the copy (including
    anything the command wrote) is moved there instead of being deleted. The network is off
    unless `network=True`, which is only for the dependency-install phase.
    """
    timeout = timeout or settings.sandbox_timeout_seconds
    output_limit = output_limit or settings.sandbox_output_limit_bytes
    scratch_root = settings.workspace_dir / "sandbox"
    scratch_root.mkdir(parents=True, exist_ok=True)
    workdir = _copy_checkout(checkout, scratch_root)
    name = f"repopilot-sbx-{uuid.uuid4().hex[:12]}"

    cmd = docker_command(
        name,
        workdir,
        command,
        image=image or settings.sandbox_image,
        memory=memory or settings.sandbox_memory,
        cpus=cpus or settings.sandbox_cpus,
        pids_limit=pids_limit or settings.sandbox_pids_limit,
        mounts=mounts,
        env=env,
        network=network,
    )
    started = time.monotonic()
    timed_out = False
    try:
        try:
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise SandboxError(f"could not start docker: {exc}") from exc

        out, err = (
            _CappedReader(process.stdout, output_limit),
            _CappedReader(process.stderr, output_limit),
        )
        out.start()
        err.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Killing the docker client does not stop the container; kill it by name.
            subprocess.run(["docker", "kill", name], capture_output=True, check=False, timeout=30)
            process.wait(timeout=KILL_GRACE_SECONDS)
        out.join(KILL_GRACE_SECONDS)
        err.join(KILL_GRACE_SECONDS)

        exit_code = TIMEOUT_EXIT_CODE if timed_out else process.returncode
        stderr = err.text()
        if exit_code == 125 and "Unable to find image" in stderr:
            raise SandboxError(f"sandbox image {cmd[-len(command) - 1]!r} is not built")
        return SandboxResult(
            exit_code=exit_code,
            stdout=out.text(),
            stderr=stderr,
            timed_out=timed_out,
            killed=exit_code == OOM_OR_KILLED_EXIT_CODE and not timed_out,
            truncated=out.truncated or err.truncated,
            duration_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30)
        if keep_changes_in is not None:
            remove_tree(keep_changes_in)
            shutil.move(str(workdir), str(keep_changes_in))
        else:
            remove_tree(workdir)
