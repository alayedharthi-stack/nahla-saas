# ── Nahla SaaS — Python services image ────────────────────────────────────────
# Shared by: backend, whatsapp-service, ai-engine, catalog-service,
#            order-service, coupon-service, campaign-service, widget-service,
#            conversation-service, automation-service, analytics-service,
#            billing-service, location-service, marketplace-service,
#            integrations/salla, integrations/zid
#
# Each service overrides CMD in docker-compose.yml.
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System deps needed by psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire repo into the image
# (all services reference each other via sys.path / relative imports)
COPY . .

# Default: backend on port 8000 (Railway injects $PORT automatically)
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
