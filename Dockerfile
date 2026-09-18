# ── Stage 1: download Siegfried binary ────────────────────────────────────────
FROM debian:bookworm-slim AS siegfried-dl

ARG SIEGFRIED_VERSION=1.11.4

# Releases use dashes in the version string and ship as .zip (e.g. siegfried_1-11-4_linux64.zip)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && SF_VER_DASHES="$(echo ${SIEGFRIED_VERSION} | tr '.' '-')" \
    && curl -fsSL \
       "https://github.com/richardlehane/siegfried/releases/download/v${SIEGFRIED_VERSION}/siegfried_${SF_VER_DASHES}_linux64.zip" \
       -o /tmp/sf.zip \
    && unzip /tmp/sf.zip sf -d /usr/local/bin \
    && chmod +x /usr/local/bin/sf \
    && rm /tmp/sf.zip

# ── Stage 2: Python application ───────────────────────────────────────────────
FROM python:3.11-slim AS app

# Copy Siegfried binary
COPY --from=siegfried-dl /usr/local/bin/sf /usr/local/bin/sf

# Create non-root user early so Siegfried signatures are downloaded into their home dir.
# Siegfried uses XDG paths (~/.local/share/siegfried/) so HOME must be set correctly.
RUN useradd -m ffis
ENV HOME=/home/ffis
RUN sf -update || true

# Install Python deps (runs as root; output owned by root, fixed below)
WORKDIR /app
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir ".[dev]" \
    && chown -R ffis:ffis /app

# Cache volume mount point. It must exist in the image and be owned by ffis:
# Docker creates a missing mount point as root:root, and the non-root process
# then cannot create the SQLite cache file in it.
RUN mkdir -p /data && chown ffis:ffis /data

USER ffis

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", "ffis.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2"]
