# ---- dashboard build -------------------------------------------------------
FROM node:22-bookworm-slim AS dashboard
WORKDIR /dash
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/dashboard/ ./
RUN NEXT_TELEMETRY_DISABLED=1 npx next build

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin noesis
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Code is owned by root and read-only to the runtime user: bots cannot modify
# their own code, configuration or policies.
COPY workforce/ workforce/
COPY integrations/ integrations/
COPY services/ services/
COPY database/ database/
COPY scripts/ scripts/
COPY config/ config/
COPY policies/ policies/
COPY prompts/ prompts/
COPY assets/ assets/
COPY pyproject.toml ./
COPY --from=dashboard /dash/out apps/dashboard/out
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOESIS_DB_PATH=/data/noesis.db \
    NOESIS_DATA_DIR=/data \
    NOESIS_CONFIG_DIR=/app/config \
    NOESIS_POLICIES_DIR=/app/policies \
    NOESIS_PROMPTS_DIR=/app/prompts
RUN mkdir -p /data /vault && chown noesis:noesis /data /vault
USER noesis
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "uvicorn", "services.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
