"""Restricted internet access for the dependency-install phase.

The install container needs PyPI and nothing else. Instead of giving it the open internet, it is
attached to a Docker `--internal` network, which has no route out at all, together with a small
proxy container that is also on a normal network. The proxy tunnels HTTPS (CONNECT) to an
allowlist of hosts and refuses everything else, so even a container that ignores its proxy
settings has nowhere to connect.

    install container --(internal network)--> proxy container --(bridge)--> pypi.org only
"""

import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.sandbox.runner import SandboxError

PROXY_PORT = 3128
READY_TIMEOUT_SECONDS = 20

# Runs inside the proxy container, using only the standard library. Kept as a string so it can be
# mounted into any Python image and exercised directly by the tests.
PROXY_SCRIPT = """\
import asyncio
import os

ALLOWED = {h.strip().lower() for h in os.environ["ALLOWED_HOSTS"].split(",") if h.strip()}
ALLOWED_PORT = os.environ.get("ALLOWED_PORT", "443")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "3128"))
MAX_CONNECTIONS = 32
MAX_HEADER_LINES = 100
HANDSHAKE_SECONDS = 10
CONNECT_SECONDS = 15
IDLE_SECONDS = 120
slots = asyncio.Semaphore(MAX_CONNECTIONS)


async def pipe(source, sink):
    try:
        while data := await asyncio.wait_for(source.read(65536), IDLE_SECONDS):
            sink.write(data)
            await sink.drain()
    except Exception:
        pass
    finally:
        sink.close()


async def refuse(writer, status):
    writer.write(f"HTTP/1.1 {status}\\r\\nConnection: close\\r\\n\\r\\n".encode())
    await writer.drain()


async def handle(reader, writer):
    upstream = None
    try:
        request = (await asyncio.wait_for(reader.readline(), HANDSHAKE_SECONDS)).decode("latin-1")
        for _ in range(MAX_HEADER_LINES):
            line = await asyncio.wait_for(reader.readline(), HANDSHAKE_SECONDS)
            if line in (b"\\r\\n", b"\\n", b""):
                break
        else:
            return await refuse(writer, "431 Request Header Fields Too Large")
        parts = request.split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            return await refuse(writer, "405 Method Not Allowed")
        host, _, port = parts[1].rpartition(":")
        if host.lower() not in ALLOWED or port != ALLOWED_PORT:
            print("refused", parts[1], flush=True)
            return await refuse(writer, "403 Forbidden")
        async with slots:
            upstream = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), CONNECT_SECONDS
            )
            writer.write(b"HTTP/1.1 200 Connection established\\r\\n\\r\\n")
            await writer.drain()
            await asyncio.gather(pipe(reader, upstream[1]), pipe(upstream[0], writer))
    except Exception:
        pass
    finally:
        for stream in (writer, upstream[1] if upstream else None):
            if stream is not None:
                stream.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    print("ready", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
"""


@dataclass(frozen=True)
class Egress:
    network: str  # the internal Docker network to attach the install container to
    proxy_url: str

    @property
    def env(self) -> dict[str, str]:
        return {
            "HTTPS_PROXY": self.proxy_url,
            "https_proxy": self.proxy_url,
            "HTTP_PROXY": self.proxy_url,
            "http_proxy": self.proxy_url,
        }


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"docker failed: {exc}") from exc


def allowed_hosts() -> str:
    return ",".join(h.strip() for h in settings.install_allowed_hosts.split(",") if h.strip())


@contextmanager
def restricted_egress() -> Iterator[Egress]:
    """An isolated network whose only way out is the allowlist proxy; removed on exit."""
    tag = uuid.uuid4().hex[:10]
    network = f"repopilot-net-{tag}"
    proxy = f"repopilot-sbx-proxy-{tag}"  # the sandbox prefix, so leak checks cover it
    script_dir = Path(tempfile.mkdtemp(prefix="repopilot-proxy-", dir=_scratch()))
    (script_dir / "proxy.py").write_text(PROXY_SCRIPT, encoding="utf-8")
    script_dir.chmod(0o755)
    (script_dir / "proxy.py").chmod(0o644)

    try:
        created = _docker("network", "create", "--internal", network)
        if created.returncode != 0:
            raise SandboxError(f"could not create the install network: {created.stderr.strip()}")
        started = _docker(
            "run", "-d", "--rm", "--name", proxy,
            "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "1000:1000",
            "-e", f"ALLOWED_HOSTS={allowed_hosts()}",
            "-v", f"{script_dir.resolve().as_posix()}:/proxy:ro",
            settings.sandbox_image, "python", "/proxy/proxy.py",
        )  # fmt: skip
        if started.returncode != 0:
            raise SandboxError(f"could not start the install proxy: {started.stderr.strip()}")
        attached = _docker("network", "connect", network, proxy)
        if attached.returncode != 0:
            raise SandboxError(f"could not attach the install proxy: {attached.stderr.strip()}")
        _wait_until_ready(proxy)
        yield Egress(network=network, proxy_url=f"http://{proxy}:{PROXY_PORT}")
    finally:
        _docker("rm", "-f", proxy)
        _docker("network", "rm", network)
        for leftover in script_dir.glob("*"):
            leftover.unlink(missing_ok=True)
        script_dir.rmdir()


def _scratch() -> Path:
    path = settings.workspace_dir / "egress"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _wait_until_ready(proxy: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        logs = _docker("logs", proxy)
        if "ready" in logs.stdout:
            return
        if _docker("inspect", "-f", "{{.State.Running}}", proxy).stdout.strip() != "true":
            raise SandboxError(f"the install proxy exited: {logs.stderr.strip()[-300:]}")
        time.sleep(0.3)
    raise SandboxError("the install proxy did not become ready")
