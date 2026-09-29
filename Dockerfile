# WITALCO Guest Concierge (F33): one process, non-root, read-only root file system.
# The interpreter matches the tested one (Python 3.13); the lock file pins every
# dependency with hashes. Build: docker build -t witalco/concierge .
FROM python:3.13-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
COPY docs/BUILD_PROMPT.md docs/BUILD_PROMPT.md
RUN /opt/venv/bin/python -m pip install --require-hashes -r requirements.lock \
 && /opt/venv/bin/python -m pip install --no-deps --no-build-isolation .

FROM python:3.13-slim
ENV PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH" \
    GC_DATABASE_URL="postgresql+asyncpg://gc:gc@db:5432/gc" \
    GC_TZ_DEFAULT="Europe/Berlin"
RUN groupadd --system gc && useradd --system --gid gc --home /var/lib/gc --create-home gc \
 && mkdir -p /var/lib/gc/backups && chown -R gc:gc /var/lib/gc
COPY --from=build --chown=gc:gc /opt/venv /opt/venv
USER gc
WORKDIR /var/lib/gc
VOLUME ["/var/lib/gc"]
EXPOSE 8000
# uvicorn runs inside `concierge serve` with proxy headers on (Caddy terminates TLS)
CMD ["concierge", "serve", "--host", "0.0.0.0", "--port", "8000"]
