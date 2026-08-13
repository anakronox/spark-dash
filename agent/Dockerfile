# Build context is the REPO ROOT, not this directory — the agent depends on
# the local `common/` package, which has to be inside the context.
#   docker build -f agent/Dockerfile -t spark-dash-agent .

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Two-stage install so third-party dependencies stay cached across source
# edits. This first layer needs the workspace manifests present (uv reads them
# to resolve) but deliberately installs no workspace code.
COPY pyproject.toml uv.lock ./
COPY common/pyproject.toml common/
COPY agent/pyproject.toml agent/
RUN mkdir -p common/src/spark_dash_common agent/src/spark_dash_agent \
    && touch common/src/spark_dash_common/__init__.py agent/src/spark_dash_agent/__init__.py \
    && uv sync --frozen --no-dev --no-install-workspace --package spark-dash-agent

# Now the real source, and install the workspace packages themselves.
COPY common/ common/
COPY agent/ agent/

# --no-editable matters: an editable install writes a .pth holding the BUILD
# path, which dangles once the venv is copied to a different path in the
# runtime stage. Installing properly puts real packages in site-packages, so
# the venv is relocatable and no source needs to ship.
RUN uv sync --frozen --no-dev --no-editable --package spark-dash-agent


FROM python:3.12-slim

# Where the HOST's /proc and /sys are mounted. A container's own
# /proc/pressure/memory and MemAvailable describe the container, not the
# machine being monitored.
ENV PROC_PATH=/host/proc \
    SYS_PATH=/host/sys \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# NVML comes from the host driver via the NVIDIA Container Toolkit, so nothing
# CUDA-related is installed here — this stays a small slim image.
WORKDIR /app

# The venv is self-contained (see --no-editable above); no source is copied.
COPY --from=builder /build/.venv /app/.venv

# Non-root: the agent only ever reads. Note this means /proc/<pid> entries for
# other users' processes may be unreadable, so process *names* can come back
# empty. Set `user: "0:0"` in compose if full process attribution matters more
# than least privilege on your setup.
RUN useradd --system --uid 10001 --no-create-home sparkdash
USER 10001

EXPOSE 9500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9500/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "spark_dash_agent"]
