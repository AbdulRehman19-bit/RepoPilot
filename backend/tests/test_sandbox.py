import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.sandbox.runner import SandboxError, docker_command, run_in_sandbox


def _image_ready() -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", settings.sandbox_image],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not _image_ready(),
    reason="sandbox image not built (docker build -t repopilot-sandbox:py311 docker/sandbox)",
)


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    project = tmp_path / "project"
    project.mkdir()
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (project / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    return project


def py(code: str) -> list[str]:
    return ["python", "-c", code]


def leftover_containers() -> str:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=repopilot-sbx-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def test_command_contains_every_hardening_flag(tmp_path):
    cmd = docker_command(
        "n", tmp_path, ["true"], image="img", memory="256m", cpus=1.0, pids_limit=64
    )
    joined = " ".join(cmd)
    for flag in [
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 1000:1000",
        "--pids-limit 64",
        "--memory 256m",
        "--memory-swap 256m",
    ]:
        assert flag in joined
    assert "--privileged" not in joined
    assert "docker.sock" not in joined
    assert cmd[-2:] == ["img", "true"]


@requires_docker
def test_failing_tests_are_reported_not_hidden(checkout):
    result = run_in_sandbox(checkout, ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    assert result.exit_code == 1
    assert not result.ok
    assert "1 failed, 1 passed" in result.stdout
    assert not result.timed_out


@requires_docker
def test_passing_command_is_ok(checkout):
    result = run_in_sandbox(checkout, py("print('hello')"))
    assert result.ok
    assert result.stdout.strip() == "hello"


@requires_docker
def test_original_checkout_is_never_modified(checkout, tmp_path):
    kept = tmp_path / "kept"
    result = run_in_sandbox(
        checkout,
        py("open('created.txt', 'w').write('x'); open('calc.py', 'w').write('# wiped')"),
        keep_changes_in=kept,
    )
    assert result.ok, result.stderr
    assert not (checkout / "created.txt").exists()
    assert "wiped" not in (checkout / "calc.py").read_text()
    assert (kept / "created.txt").exists()  # changes are available when explicitly requested


@requires_docker
def test_no_network(checkout):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), 3)\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print('blocked')\n"
    )
    result = run_in_sandbox(checkout, py(code), timeout=30)
    assert result.stdout.strip() == "blocked"


@requires_docker
def test_root_filesystem_is_read_only(checkout):
    code = (
        "try:\n"
        "    open('/etc/pwned', 'w')\n"
        "    print('WRITTEN')\n"
        "except OSError:\n"
        "    print('read-only')\n"
    )
    assert run_in_sandbox(checkout, py(code)).stdout.strip() == "read-only"


@requires_docker
def test_runs_unprivileged_with_no_capabilities(checkout):
    result = run_in_sandbox(checkout, ["sh", "-c", "id -u; grep CapEff /proc/self/status"])
    uid, caps = result.stdout.strip().splitlines()
    assert uid == "1000"
    assert caps.split()[-1] == "0" * 16


@requires_docker
def test_host_environment_does_not_leak(checkout, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_FOR_TEST", "hunter2-super-secret")
    result = run_in_sandbox(checkout, ["env"])
    assert "hunter2" not in result.stdout
    assert "GITHUB_TOKEN_FOR_TEST" not in result.stdout


@requires_docker
def test_hanging_command_is_killed_and_container_removed(checkout):
    result = run_in_sandbox(checkout, ["sleep", "60"], timeout=3)
    assert result.timed_out
    assert result.exit_code == 124
    assert not result.ok
    assert result.duration_seconds < 25
    assert leftover_containers() == ""


@requires_docker
def test_fork_bomb_is_contained_by_pid_limit(checkout):
    code = (
        "import os, time\n"
        "children = 0\n"
        "try:\n"
        "    for _ in range(2000):\n"
        "        if os.fork() == 0:\n"
        "            time.sleep(30)\n"
        "            os._exit(0)\n"
        "        children += 1\n"
        "except OSError:\n"
        "    print('blocked at', children)\n"
        "    os._exit(0)\n"
    )
    result = run_in_sandbox(checkout, py(code), pids_limit=64, timeout=60)
    assert "blocked at" in result.stdout, result.stderr
    assert int(result.stdout.split()[-1]) < 64
    assert leftover_containers() == ""


@requires_docker
def test_memory_hog_is_killed(checkout):
    result = run_in_sandbox(checkout, py("x = 'x' * (1 << 30); print(len(x))"), memory="128m")
    assert result.killed
    assert result.exit_code == 137
    assert not result.ok


@requires_docker
def test_huge_output_is_truncated(checkout):
    code = "import sys\nfor _ in range(30000):\n    sys.stdout.write('x' * 1000 + '\\n')\n"
    result = run_in_sandbox(checkout, py(code), output_limit=10_000, timeout=60)
    assert result.truncated
    assert len(result.stdout) == 10_000
    assert result.exit_code == 0


@requires_docker
def test_missing_image_raises_sandbox_error(checkout):
    with pytest.raises(SandboxError):
        run_in_sandbox(checkout, ["true"], image="repopilot-image-that-does-not-exist:none")
