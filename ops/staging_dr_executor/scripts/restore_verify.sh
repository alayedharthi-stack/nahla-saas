#!/usr/bin/env bash
set -euo pipefail
source /dr/scripts/common.sh
require_staging_identity

: "${NAHLA_STG_DR_ENCRYPT_KEY:?missing encryption key}"
: "${NAHLA_STG_DR_BUCKET:?missing bucket name}"
: "${NAHLA_STG_DR_S3_ENDPOINT:?missing s3 endpoint}"
: "${NAHLA_STG_DR_S3_REGION:?missing s3 region}"
: "${NAHLA_STG_DR_S3_ACCESS_KEY_ID:?missing s3 access key}"
: "${NAHLA_STG_DR_S3_SECRET_ACCESS_KEY:?missing s3 secret}"
: "${NAHLA_STG_DR_OBJECT_KEY:?missing object key}"
: "${TARGET_PGHOST:?missing target host}"
: "${TARGET_PGUSER:?missing target user}"
: "${TARGET_PGPASSWORD:?missing target password}"
: "${TARGET_PGDATABASE:?missing target database}"

export AWS_ACCESS_KEY_ID="${NAHLA_STG_DR_S3_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${NAHLA_STG_DR_S3_SECRET_ACCESS_KEY}"
export AWS_DEFAULT_REGION="${NAHLA_STG_DR_S3_REGION}"

echo "restore_identity_project=${RAILWAY_PROJECT_NAME}"
echo "restore_identity_environment=${RAILWAY_ENVIRONMENT_NAME}"
echo "restore_object_key=${NAHLA_STG_DR_OBJECT_KEY}"
echo "restore_key_fingerprint=$(key_fingerprint)"

aws s3 cp "s3://${NAHLA_STG_DR_BUCKET}/${NAHLA_STG_DR_OBJECT_KEY}" - \
  --endpoint-url "${NAHLA_STG_DR_S3_ENDPOINT}" \
  | openssl enc -d -aes-256-cbc -pbkdf2 -pass env:NAHLA_STG_DR_ENCRYPT_KEY \
  | PGPASSWORD="${TARGET_PGPASSWORD}" pg_restore \
      -h "${TARGET_PGHOST}" \
      -U "${TARGET_PGUSER}" \
      -d "${TARGET_PGDATABASE}" \
      --no-owner \
      --no-acl

PSQL=(psql -h "${TARGET_PGHOST}" -U "${TARGET_PGUSER}" -d "${TARGET_PGDATABASE}" -At)
export PGPASSWORD="${TARGET_PGPASSWORD}"

REV="$("${PSQL[@]}" -c "select version_num from alembic_version")"
TBL_COUNT="$("${PSQL[@]}" -c "select count(1) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'")"
TABLE_FP="$("${PSQL[@]}" -c "select md5(string_agg(relname, ',' order by relname)) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r'")"
LIVE_SUM="$("${PSQL[@]}" -c "select coalesce(sum(n_live_tup),0) from pg_stat_user_tables")"
EST_SUM="$("${PSQL[@]}" -c "select coalesce(sum(reltuples)::bigint,0) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r'")"

echo "restore_alembic_revision=${REV}"
echo "restore_public_table_count=${TBL_COUNT}"
echo "restore_table_set_fingerprint=${TABLE_FP}"
echo "restore_live_tuple_sum=${LIVE_SUM}"
echo "restore_estimated_row_sum=${EST_SUM}"
