#!/usr/bin/env bash
set -e
cd /app

HOST_BIND="${HOST:-0.0.0.0}"
PORT_BIND="${PORT:-8000}"
# auto | h11 | httptools — set NAHLA_UVICORN_HTTP=h11 to rule out httptools quirks
HTTP_IMPL="${NAHLA_UVICORN_HTTP:-auto}"

echo "[start.sh] HOST=${HOST_BIND} PORT=${PORT_BIND} HTTP=${HTTP_IMPL} NAHLA_MINIMAL_ASGI=${NAHLA_MINIMAL_ASGI:-}" >&2

# ── Phase 1A preflight ────────────────────────────────────────────────────────
# Validates JWT_SECRET / ADMIN_PASSWORD / WHATSAPP_VERIFY_TOKEN / ADMIN_EMAIL
# / DATABASE_URL in production. Refuses to bind a port when any of them are
# missing or set to a known placeholder. Skipped in non-production envs.
# Override with NAHLA_SKIP_PREFLIGHT=1 only for emergency boots.
if [ "${NAHLA_SKIP_PREFLIGHT:-}" != "1" ]; then
  echo "[start.sh] running preflight checks…" >&2
  python /app/scripts/preflight_check.py
fi

if [ "${NAHLA_MINIMAL_ASGI:-}" = "1" ] || [ "${NAHLA_MINIMAL_ASGI:-}" = "true" ]; then
  exec uvicorn backend.minimal_asgi:app \
    --host "${HOST_BIND}" \
    --port "${PORT_BIND}" \
    --http "${HTTP_IMPL}"
fi

exec uvicorn backend.main:app \
  --host "${HOST_BIND}" \
  --port "${PORT_BIND}" \
  --http "${HTTP_IMPL}"
