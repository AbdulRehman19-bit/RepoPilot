FROM python:3.11-slim

# Non-root user that sandboxed code runs as (matches --user 1000:1000 in the runner).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin sandbox \
    && pip install --no-cache-dir pytest==8.3.4 \
    && mkdir /workspace && chown sandbox:sandbox /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace
USER sandbox
