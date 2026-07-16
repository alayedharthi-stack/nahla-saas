#!/usr/bin/env bash
set -euo pipefail
source /dr/scripts/common.sh
require_staging_identity

: "${TARGET_PGHOST:?missing target host}"
: "${TARGET_PGUSER:?missing target user}"
: "${TARGET_PGPASSWORD:?missing target password}"
: "${TARGET_PGDATABASE:?missing target database}"

export PGPASSWORD="${TARGET_PGPASSWORD}"
PSQL=(psql -h "${TARGET_PGHOST}" -U "${TARGET_PGUSER}" -d "${TARGET_PGDATABASE}" -At)

PUBLIC_TABLE_COUNT="$("${PSQL[@]}" -c "select count(1) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'")"
ALEMBIC_PRESENT="$("${PSQL[@]}" -c "select count(1) from information_schema.tables where table_schema='public' and table_name='alembic_version'")"

if [[ "${PUBLIC_TABLE_COUNT}" != "0" || "${ALEMBIC_PRESENT}" != "0" ]]; then
  echo "target_empty=false" >&2
  echo "public_table_count=${PUBLIC_TABLE_COUNT}" >&2
  echo "alembic_version_present=$([[ ${ALEMBIC_PRESENT} -gt 0 ]] && echo true || echo false)" >&2
  exit 2
fi

echo "attestation_project=${RAILWAY_PROJECT_NAME}"
echo "attestation_environment=${RAILWAY_ENVIRONMENT_NAME}"
echo "attestation_runner=${RAILWAY_SERVICE_NAME}"
echo "target_empty=true"
echo "public_table_count=0"
echo "alembic_version_present=false"
