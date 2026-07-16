#!/usr/bin/env bash
set -euo pipefail

source /dr/scripts/common.sh
require_staging_identity

: "${SOURCE_PGHOST:?missing source host}"
: "${SOURCE_PGUSER:?missing source user}"
: "${SOURCE_PGPASSWORD:?missing source password}"
: "${SOURCE_PGDATABASE:?missing source database}"
: "${TARGET_PGHOST:?missing target host}"
: "${TARGET_PGUSER:?missing target user}"
: "${TARGET_PGPASSWORD:?missing target password}"
: "${TARGET_PGDATABASE:?missing target database}"

CONTRACT_FILE="${NAHLA_STG_DR_PARITY_CONTRACT:-/dr/contracts/canonical_parity.json}"
if [[ ! -f "${CONTRACT_FILE}" ]]; then
  echo "contract_missing=false" >&2
  exit 2
fi

contract_version="$(jq -r '.contract_version // empty' "${CONTRACT_FILE}")"
canonical_version="$(jq -r '.schema_fingerprint_version // empty' "${CONTRACT_FILE}")"
if [[ -z "${contract_version}" || -z "${canonical_version}" ]]; then
  echo "contract_invalid=false" >&2
  exit 2
fi
if [[ "${canonical_version}" != "nahla_public_tables_sha256_v1" ]]; then
  echo "schema_fingerprint_version_mismatch=false" >&2
  exit 2
fi

fingerprint() {
  local host="$1" user="$2" password="$3" database="$4"
  local canonical
  canonical="$(
    PGPASSWORD="${password}" psql \
      -h "${host}" -U "${user}" -d "${database}" -At \
      -c "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY relname" \
      | paste -sd,
  )"
  printf '%s' "${canonical}" | sha256sum | awk '{print $1}'
}

scalar() {
  local host="$1" user="$2" password="$3" database="$4" sql="$5"
  PGPASSWORD="${password}" psql -h "${host}" -U "${user}" -d "${database}" -At -c "${sql}"
}

src_fp="$(fingerprint "${SOURCE_PGHOST}" "${SOURCE_PGUSER}" "${SOURCE_PGPASSWORD}" "${SOURCE_PGDATABASE}")"
tgt_fp="$(fingerprint "${TARGET_PGHOST}" "${TARGET_PGUSER}" "${TARGET_PGPASSWORD}" "${TARGET_PGDATABASE}")"

src_revision="$(scalar "${SOURCE_PGHOST}" "${SOURCE_PGUSER}" "${SOURCE_PGPASSWORD}" "${SOURCE_PGDATABASE}" "SELECT version_num FROM alembic_version")"
tgt_revision="$(scalar "${TARGET_PGHOST}" "${TARGET_PGUSER}" "${TARGET_PGPASSWORD}" "${TARGET_PGDATABASE}" "SELECT version_num FROM alembic_version")"

table_sql="SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
src_tables="$(scalar "${SOURCE_PGHOST}" "${SOURCE_PGUSER}" "${SOURCE_PGPASSWORD}" "${SOURCE_PGDATABASE}" "${table_sql}")"
tgt_tables="$(scalar "${TARGET_PGHOST}" "${TARGET_PGUSER}" "${TARGET_PGPASSWORD}" "${TARGET_PGDATABASE}" "${table_sql}")"

declare -a aggregate_sql=(
  "SELECT count(*) FROM (SELECT whatsapp_business_account_id FROM whatsapp_connections WHERE whatsapp_business_account_id IS NOT NULL GROUP BY whatsapp_business_account_id HAVING count(*) > 1) dup"
  "SELECT count(*) FROM (SELECT tenant_id, external_id FROM orders WHERE external_id IS NOT NULL GROUP BY tenant_id, external_id HAVING count(*) > 1) dup"
  "SELECT count(*) FROM smart_automations WHERE automation_type IN ('cart_recovery', 'reorder_reminder', 'welcome_message')"
)

for sql in "${aggregate_sql[@]}"; do
  src_value="$(scalar "${SOURCE_PGHOST}" "${SOURCE_PGUSER}" "${SOURCE_PGPASSWORD}" "${SOURCE_PGDATABASE}" "${sql}")"
  tgt_value="$(scalar "${TARGET_PGHOST}" "${TARGET_PGUSER}" "${TARGET_PGPASSWORD}" "${TARGET_PGDATABASE}" "${sql}")"
  if [[ "${src_value}" != "0" || "${tgt_value}" != "0" || "${src_value}" != "${tgt_value}" ]]; then
    echo "destructive_aggregate_parity=false" >&2
    exit 2
  fi
done

if [[ "${src_fp}" != "${tgt_fp}" || "${src_revision}" != "${tgt_revision}" || "${src_tables}" != "${tgt_tables}" ]]; then
  echo "canonical_manifest_parity=false" >&2
  exit 2
fi

matched_profile="$(
  jq -r --arg rev "${src_revision}" --argjson tbl "${src_tables}" --arg fp "${src_fp}" '
    .source_eligibility_profiles[]
    | select(.alembic_revision == $rev and .public_table_count == $tbl)
    | select(.schema_fingerprint_sha256 == $fp)
    | .profile_id
  ' "${CONTRACT_FILE}" | head -n 1
)"

if [[ -z "${matched_profile}" ]]; then
  echo "source_contract_eligible=false" >&2
  exit 2
fi

echo "parity_contract_version=${contract_version}"
echo "schema_fingerprint_version=${canonical_version}"
echo "canonical_full_sha_parity=true"
echo "revision_parity=true"
echo "public_table_count_parity=true"
echo "destructive_aggregate_parity=true"
echo "source_contract_eligible=true"
echo "matched_source_profile_id=${matched_profile}"
echo "source_fingerprint_display=${src_fp:0:16}"
echo "restore_fingerprint_display=${tgt_fp:0:16}"
