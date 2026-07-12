# ── Stage 1: dependency installer ────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Cache-friendly: install deps before copying source
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra web

# Now install the project itself
COPY mmhue/ ./mmhue/
RUN uv sync --frozen --no-dev --extra web

# ── Stage 2: minimal runtime ──────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy only what's needed to run
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/mmhue /app/mmhue

# Non-root user
RUN useradd -m -u 1000 mmhue && chown -R mmhue /app
USER mmhue

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "-m", "mmhue.interfaces.telegram"]
