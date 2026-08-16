# Build context is the REPO ROOT — the backend depends on the local `common/`
# package and on the built frontend assets.
#   docker build -f backend/Dockerfile -t spark-dash-backend .

# --- frontend ---------------------------------------------------------------
# Built here rather than committed, so the served assets always match the
# source. The backend serves them itself: same origin as the API, so there's no
# CORS config and the WebSocket needs no separate host.
FROM node:22-slim AS frontend

WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# build:fast skips svelte-check — type checking belongs in CI, not in a
# deployment path where it only adds a way for the image build to fail.
RUN npm run build:fast


# --- backend ----------------------------------------------------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Dependency layer from manifests alone, so editing source doesn't reinstall
# every third-party package.
COPY pyproject.toml uv.lock ./
COPY common/pyproject.toml common/
COPY backend/pyproject.toml backend/
RUN mkdir -p common/src/spark_dash_common backend/src/spark_dash_backend \
    && touch common/src/spark_dash_common/__init__.py backend/src/spark_dash_backend/__init__.py \
    && uv sync --frozen --no-dev --no-install-workspace --package spark-dash-backend

COPY common/ common/
COPY backend/ backend/

# --no-editable so the venv holds real packages rather than a .pth pointing at
# this build path — otherwise it breaks when copied to /app below.
RUN uv sync --frozen --no-dev --no-editable --package spark-dash-backend


FROM python:3.12-slim

# Set by publish-images.sh from the git sha. The agent has always recorded this
# and the backend did not, so `/health` could name every agent's build but not
# its own — and the only record of what backend was running was the image tag
# in a .env file on the host.
ARG BUILD_VERSION=unknown

ENV BACKEND_VERSION=${BUILD_VERSION} \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /build/.venv /app/.venv

# Built Svelte assets. If this were absent the app would still start and serve
# the API, logging that it's in API-only mode.
COPY --from=frontend /web/dist /app/static

RUN useradd --system --uid 10002 --no-create-home sparkdash

# Seed the targets directory with the right ownership. Docker copies an image
# directory's ownership onto a freshly created named volume, which is what lets
# the non-root user render scrape targets into it.
RUN mkdir -p /etc/prometheus/targets && chown -R 10002:10002 /etc/prometheus/targets

USER 10002

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "spark_dash_backend"]
