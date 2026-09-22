# ── Stage 1: build the SPA ───────────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so app edits don't bust the layer cache.
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

COPY data/ ./data/
COPY --from=web /web/dist ./static

# Run unprivileged. No .env is ever copied — secrets arrive as env vars at
# runtime, so they never land in an image layer.
RUN useradd --create-home --uid 10001 app && chown -R app:app /srv
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
