# Nod — DEPLOYMENT.md §3.
#
# Multi-stage: resolve and build wheels in stage one, copy a wheel-only runtime
# into a slim stage two. Non-root, read-only root filesystem, `/data` the only
# writable mount.
#
# **No Next.js build stage**, and that is a decision rather than an omission:
# ADR-038 made the one demo screen a static HTML file served from the API
# container, so there is nothing to compile. §3's "build the Next.js export in
# stage one" predates it.
#
# The `bench` extra is deliberately absent from the runtime. `librosa` and
# `soundfile` are bench-only (CLAUDE.md §3) and pull a numeric stack that is
# most of the 400 MB budget on its own.

# TODO: pin by digest before deploying. DEPLOYMENT.md §3 requires
# `python:3.12-slim@sha256:...`, and the digest cannot be resolved on a machine
# with no container runtime — see DEPLOYMENT.md §3's note. Deploying from a tag
# means the image is not reproducible.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
# Base dependencies only: no `[bench]`, per the note above.
RUN python -m pip install --upgrade pip build \
    && python -m pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOD_TRACE_DIR=/data/traces \
    NOD_DB_PATH=/data/nod.db

# Non-root. The uid is fixed so a volume's ownership can be set to match it
# without inspecting the image.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nod \
    && mkdir -p /data && chown -R 10001:10001 /data

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels nod \
    && rm -rf /wheels

USER 10001
WORKDIR /srv
VOLUME ["/data"]
EXPOSE 8000

# `/healthz` never touches upstream (DEPLOYMENT.md §4), so an AssemblyAI outage
# cannot make the container restart itself.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# `--factory`: importing `nod_server.app` must never construct an application
# (see its module docstring).
CMD ["python", "-m", "uvicorn", "--factory", "nod_server.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
