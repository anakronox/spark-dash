# Build context is the REPO ROOT, not this directory — the agent depends on
# the local `common/` package, which has to be inside the context.
#   docker build -f agent/Dockerfile -t spark-dash-agent .

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Manifests first so the dependency layer survives source edits.
COPY pyproject.toml uv.lock ./
COPY common/pyproject.toml common/
COPY agent/pyproject.toml agent/

COPY common/ common/
COPY agent/ agent/

RUN uv sync --frozen --no-dev --package spark-dash-agent


FROM python:3.12-slim

# Read-only mounts of the host's /proc and /sys. The agent needs the *host's*
# view, not the container's namespaced one — PSI and MemAvailable inside a
# container describe the container, which is not what we're monitoring.
ENV PROC_PATH=/host/proc \
    SYS_PATH=/host/sys \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# NVML comes from the host driver via the NVIDIA Container Toolkit, so nothing
# CUDA-related is installed here. This stays a small slim image.
WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/common /app/common
COPY --from=builder /build/agent /app/agent

# Non-root: the agent only ever reads. Note this means /proc/<pid> entries for
# other users' processes may be unreadable — process *names* can come back
# empty. Run with user "0:0" in compose if full process attribution matters
# more than least privilege on your setup.
RUN useradd --system --uid 10001 --no-create-home sparkdash
USER 10001

EXPOSE 9500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9500/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "spark_dash_agent"]
