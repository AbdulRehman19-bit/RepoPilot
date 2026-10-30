# The API only. The worker is deliberately NOT containerised: it starts sandbox containers, and
# handing a container the Docker socket is root on the host. Run the worker on the host (see
# docs/deploy.md).
#
#   docker build -f docker/api/Dockerfile -t repopilot-api .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.8.17

WORKDIR /app
# Dependencies first, so editing application code does not reinstall them.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/alembic.ini ./
COPY backend/app ./app
COPY backend/migrations ./migrations

# Not root, and nothing in the image is writable by it except where it must write.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin repopilot \
 && mkdir /workspaces && chown repopilot /workspaces
USER repopilot
ENV REPOPILOT_WORKSPACE_DIR=/workspaces

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
