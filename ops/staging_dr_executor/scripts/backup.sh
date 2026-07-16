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
: "${SOURCE_PGHOST:?missing source host}"
: "${SOURCE_PGUSER:?missing source user}"
: "${SOURCE_PGPASSWORD:?missing source password}"
: "${SOURCE_PGDATABASE:?missing source database}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OBJECT_KEY="staging/postgres-staging/${TS}/nahla-staging-logical.enc"
export AWS_ACCESS_KEY_ID="${NAHLA_STG_DR_S3_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${NAHLA_STG_DR_S3_SECRET_ACCESS_KEY}"
export AWS_DEFAULT_REGION="${NAHLA_STG_DR_S3_REGION}"

echo "backup_identity_project=${RAILWAY_PROJECT_NAME}"
echo "backup_identity_environment=${RAILWAY_ENVIRONMENT_NAME}"
echo "backup_timestamp_utc=${TS}"
echo "backup_key_fingerprint=$(key_fingerprint)"
echo "backup_format=custom_pg_dump_openssl_aes256_pbkdf2"
echo "backup_storage_class=railway_bucket_private"
echo "backup_object_key=${OBJECT_KEY}"

PGPASSWORD="${SOURCE_PGPASSWORD}" pg_dump \
  -h "${SOURCE_PGHOST}" \
  -U "${SOURCE_PGUSER}" \
  -d "${SOURCE_PGDATABASE}" \
  --format=custom \
  --no-owner \
  --no-acl \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:NAHLA_STG_DR_ENCRYPT_KEY \
  | aws s3 cp - "s3://${NAHLA_STG_DR_BUCKET}/${OBJECT_KEY}" \
      --endpoint-url "${NAHLA_STG_DR_S3_ENDPOINT}"

META_JSON="$(aws s3api head-object \
  --bucket "${NAHLA_STG_DR_BUCKET}" \
  --key "${OBJECT_KEY}" \
  --endpoint-url "${NAHLA_STG_DR_S3_ENDPOINT}")"
ETAG="$(printf '%s' "${META_JSON}" | sed -n 's/.*"ETag": "\([^"]*\)".*/\1/p')"
SIZE="$(printf '%s' "${META_JSON}" | sed -n 's/.*"ContentLength": \([0-9]*\).*/\1/p')"
echo "backup_encrypted_etag=${ETAG}"
echo "backup_encrypted_size_bytes=${SIZE}"
echo "backup_retention_policy_days=7"
echo "NAHLA_STG_DR_LAST_OBJECT_KEY=${OBJECT_KEY}"
