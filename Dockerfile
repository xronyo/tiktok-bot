# ── Stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed for some Python packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

# ffmpeg from apt — native, full-featured, ARM64 + x86_64 covered automatically
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code (no .venv, no data, no binaries)
COPY config.py database.py downloader.py processor.py stats.py main.py ./

# ── runtime directories ───────────────────────────────────────────────────────
# /data  → mount a Railway volume here for SQLite persistence
# /tmp/* → ephemeral work dirs, fine for downloads/processed
RUN mkdir -p /data /tmp/downloads /tmp/processed

# ── environment defaults ──────────────────────────────────────────────────────
ENV DATA_DIR=/data \
    DOWNLOAD_DIR=/tmp/downloads \
    PROCESSED_DIR=/tmp/processed \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Non-root user for security
RUN useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /data /tmp/downloads /tmp/processed /app
USER botuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os; os.path.exists('${DATA_DIR}/tiktok_bot.db') or exit(0)" || exit 1

CMD ["python", "-u", "main.py"]
