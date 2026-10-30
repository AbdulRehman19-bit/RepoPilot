import os
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.config import settings
from app.sandbox.deps import prepare_dependencies
from app.sandbox.egress import PROXY_SCRIPT, allowed_hosts, restricted_egress
from app.sandbox.runner import SandboxError, run_in_sandbox
from tests.helpers import requires_docker


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def internet_available() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=4).close()
        return True
    except OSError:
        return False


requires_internet = pytest.mark.skipif(not internet_available(), reason="needs internet access")


# --- The proxy itself, run locally against an echo server ----------------------------------------


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(data)


@pytest.fixture(scope="module")
def echo_port():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def proxy(tmp_path_factory, echo_port):
    """The real proxy script, allowing only `localhost` and only the echo server's port."""
    script = tmp_path_factory.mktemp("proxy") / "proxy.py"
    script.write_text(PROXY_SCRIPT, encoding="utf-8")
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-I", str(script)],
        env={
            **os.environ,
            "ALLOWED_HOSTS": "localhost",
            "ALLOWED_PORT": str(echo_port),
            "LISTEN_PORT": str(port),
        },
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "ready"
    yield port
    process.kill()
    process.wait()


def talk(port: int, payload: bytes, *, read: int = 200) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        try:
            return sock.recv(read)
        except OSError:
            return b""


def status(reply: bytes) -> str:
    return reply.split(b"\r\n")[0].decode()


def connect(host: str, port: int) -> bytes:
    return f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()


def test_hosts_off_the_allowlist_are_refused(proxy, echo_port):
    assert "403" in status(talk(proxy, connect("example.com", echo_port)))
    assert "403" in status(talk(proxy, connect("pypi.org", echo_port)))


def test_ip_literals_cannot_be_used_to_dodge_the_hostname_allowlist(proxy, echo_port):
    # 127.0.0.1 is what "localhost" resolves to, but only the name is allowed.
    assert "403" in status(talk(proxy, connect("127.0.0.1", echo_port)))
    assert "403" in status(talk(proxy, connect("[::1]", echo_port)))


def test_allowed_host_on_another_port_is_refused(proxy, echo_port):
    assert "403" in status(talk(proxy, connect("localhost", echo_port + 1)))
    assert "403" in status(talk(proxy, connect("localhost", 22)))


def test_only_connect_is_supported(proxy):
    assert "405" in status(talk(proxy, b"GET http://localhost/ HTTP/1.1\r\n\r\n"))
    assert "405" in status(talk(proxy, b"POST / HTTP/1.1\r\n\r\n"))


def test_an_allowed_connection_tunnels_data_both_ways(proxy, echo_port):
    with socket.create_connection(("127.0.0.1", proxy), timeout=5) as sock:
        sock.sendall(connect("localhost", echo_port))
        assert b"200" in sock.recv(200)
        sock.sendall(b"hello through the tunnel")
        assert sock.recv(200) == b"hello through the tunnel"


def test_allowlist_matching_is_case_insensitive_and_exact(proxy, echo_port):
    assert "200" in status(talk(proxy, connect("LocalHost", echo_port)))
    assert "403" in status(talk(proxy, connect("evil-localhost", echo_port)))
    assert "403" in status(talk(proxy, connect("localhost.evil.com", echo_port)))


def test_header_floods_and_garbage_do_not_break_the_proxy(proxy, echo_port):
    flood = b"CONNECT localhost:1 HTTP/1.1\r\n" + b"X: y\r\n" * 500 + b"\r\n"
    assert "431" in status(talk(proxy, flood))
    assert talk(proxy, b"\x00\xff\xfe garbage\r\n\r\n") != b"HTTP/1.1 200"
    assert talk(proxy, b"") == b""
    assert "200" in status(talk(proxy, connect("localhost", echo_port)))  # still serving


def test_allowed_hosts_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "install_allowed_hosts", " pypi.org , ,files.pythonhosted.org,")
    assert allowed_hosts() == "pypi.org,files.pythonhosted.org"


# --- Through Docker ------------------------------------------------------------------------------


def leftovers() -> str:
    found = subprocess.run(
        "docker ps -aq --filter name=repopilot-sbx-; docker network ls -q --filter name=repopilot-net-",
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip()


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    project = tmp_path / "project"
    project.mkdir()
    (project / "placeholder.py").write_text("", encoding="utf-8")
    return project


@requires_docker
def test_network_and_proxy_exist_only_inside_the_context(work):
    with restricted_egress() as egress:
        assert (
            egress.network
            in subprocess.run(
                ["docker", "network", "ls", "--format", "{{.Name}}"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        )
    assert leftovers() == ""


@requires_docker
def test_cleanup_happens_even_when_the_install_fails(work):
    with pytest.raises(RuntimeError, match="boom"), restricted_egress():
        raise RuntimeError("boom")
    assert leftovers() == ""


@requires_docker
def test_a_container_there_cannot_reach_the_internet_by_ignoring_the_proxy(work):
    probe = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5); print('REACHED')"
    with restricted_egress() as egress:
        result = run_in_sandbox(work, ["python", "-c", probe], network=egress.network, timeout=60)
    assert "REACHED" not in result.stdout and not result.ok


@requires_docker
def test_the_proxy_container_refuses_hosts_that_are_not_allowed(work):
    probe = (
        "import os, socket\n"
        "host, port = os.environ['HTTPS_PROXY'][len('http://'):].split(':')\n"
        "s = socket.create_connection((host, int(port)), timeout=5)\n"
        "s.sendall(b'CONNECT example.com:443 HTTP/1.1\\r\\n\\r\\n')\n"
        "print(s.recv(100).split(b'\\r\\n')[0].decode())\n"
    )
    with restricted_egress() as egress:
        result = run_in_sandbox(
            work, ["python", "-c", probe], network=egress.network, env=egress.env, timeout=60
        )
    assert "403" in result.stdout, result.stderr


@requires_docker
def test_the_proxy_container_runs_locked_down(work):
    with restricted_egress() as egress:
        name = egress.proxy_url.split("//")[1].split(":")[0]
        inspect = subprocess.run(
            ["docker", "inspect", name, "--format",
             "{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.Config.User}} {{.HostConfig.Privileged}}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()  # fmt: skip
    assert inspect == "true [ALL] 1000:1000 false"


@requires_docker
def test_pip_cannot_be_pointed_at_another_index(work):
    cmd = [
        "python", "-m", "pip", "install", "--only-binary=:all:", "--no-cache-dir",
        "--disable-pip-version-check", "--retries", "0", "--timeout", "8",
        "--target", "/tmp/t", "--index-url", "https://example.com/simple", "six",
    ]  # fmt: skip
    with restricted_egress() as egress:
        result = run_in_sandbox(work, cmd, network=egress.network, env=egress.env, timeout=90)
    assert not result.ok
    # The refusal itself (a 403 from the proxy) is asserted above; pip reports it as "no matching
    # distribution" because the index was unreachable, and nothing was installed.
    assert "No matching distribution" in result.stderr


@requires_docker
@requires_internet
def test_dependencies_install_through_the_proxy_end_to_end(work):
    (work / "requirements.txt").write_text("six==1.16.0\n", encoding="utf-8")
    env = prepare_dependencies(work)
    assert env.site_packages is not None and (env.site_packages / "six.py").is_file()
    assert leftovers() == ""


@requires_docker
def test_a_broken_proxy_setup_is_a_sandbox_error_not_a_hang(work, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_image", "repopilot-image-that-does-not-exist:none")
    with pytest.raises(SandboxError), restricted_egress():
        pass
    assert leftovers() == ""
