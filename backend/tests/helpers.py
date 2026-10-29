import subprocess

import pytest

from app.config import settings


def image_ready() -> bool:
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
    not image_ready(),
    reason="sandbox image not built (docker build -t repopilot-sandbox:py311 docker/sandbox)",
)
