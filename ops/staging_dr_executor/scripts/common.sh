#!/usr/bin/env bash
set -euo pipefail

require_staging_identity() {
  if [[ "${RAILWAY_PROJECT_NAME:-}" != "desirable-growth" ]]; then
    echo "BLOCK: project identity mismatch" >&2
    exit 2
  fi
  if [[ "${RAILWAY_ENVIRONMENT_NAME:-}" != "staging" ]]; then
    echo "BLOCK: environment identity mismatch" >&2
    exit 2
  fi
}

key_fingerprint() {
  printf '%s' "${NAHLA_STG_DR_ENCRYPT_KEY}" | sha256sum | awk '{print $1}' | cut -c1-16
}

