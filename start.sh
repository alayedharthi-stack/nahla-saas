#!/usr/bin/env bash
set -e
cd /app

HOST_BIND="${HOST:-0.0.0.0}"
PORT_BIND="${PORT:-8000}"
# auto | h11 | httptools — set NAHLA_UVICORN_HTTP=h11 to rule out httptools quirks
HTTP_IMPL="${NAHLA_UVICORN_HTTP:-auto}"

echo "[start.sh] HOST=${HOST_BIND} PORT=${PORT_BIND} HTTP=${HTTP_IMPL} NAHLA_MINIMAL_ASGI=${NAHLA_MINIMAL_ASGI:-}" >&2

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
