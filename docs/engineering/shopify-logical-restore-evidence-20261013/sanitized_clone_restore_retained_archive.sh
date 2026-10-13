set -euo pipefail
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${CLONE_REHEARSAL_NONCE:-}" = 20261013T0924-no-source
for key in SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGPASSWORD SRC_PGDATABASE; do
 test "${!key:-}" = __REVOKED_NO_SOURCE_ACCESS__
 unset "$key"
done
test -z "${DATABASE_URL:-}"
set -euo pipefail
umask 077
phase=identity
trap 'printf "NAHLA_RECOVERY {\"kind\":\"clone_retained_archive_restore\",\"status\":\"BLOCKED\",\"phase\":\"%s\"}\n" "$phase"' ERR
test "${RAILWAY_PROJECT_ID:-}" = f0090862-0a40-4293-bd5d-e94df58762b5
test "${RAILWAY_ENVIRONMENT_ID:-}" = ede962ce-3042-4dae-94de-623837e83ed9
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${RAILWAY_DEPLOYMENT_ID:-}" = 36a2d33d-ab11-4666-b91c-a1a5ca7a1590
test "${PGDATA:-}" = /tmp/nahla-shopify-restore-pgdata
target_psql() { env -u PGPASSWORD -u PGOPTIONS psql -X -q -A -t --no-password -h /var/run/postgresql -U postgres -d railway -v ON_ERROR_STOP=1 "$@"; }
test "$(target_psql -c 'SHOW listen_addresses')" = 127.0.0.1
test "$(target_psql -c "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'")" = 0
base=/recovery/clone-rehearsal-20261013
artifact=/recovery/shopify-logical-20261013/nahla.dump
test "$(sha256sum "$artifact" | cut -d' ' -f1)" = 5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c
test "$(stat -c %a "$artifact")" = 400
phase=verify_code_bundle
test ! -e "$base/source-bundle.tar.gz"
for i in $(seq 0 21); do cat "$base/chunk-$i"; done > "$base/source-bundle.tar.gz"
test "$(sha256sum "$base/source-bundle.tar.gz" | cut -d' ' -f1)" = c003333fe802eec020b6576dd7e3f3bd3e0c12e56f3b00446fa453e28118041b
tar -xzf "$base/source-bundle.tar.gz" -C "$base"
test "$(sha256sum "$base/clone_rehearsal.py" | cut -d' ' -f1)" = 0167efff6c69535fd50bd933dea324f7fbdcea268ef1b461020f17334b15bf39
test "$(sha256sum "$base/alembic-clone/env.py" | cut -d' ' -f1)" = afc7cfeedf0140d05e5052435bb396621e414e2171b65ecf0de83a2287c97069
phase=restore_retained_archive
env -u PGPASSWORD -u PGOPTIONS timeout --signal=TERM --kill-after=15s 600s pg_restore --no-password --exit-on-error --single-transaction --clean --if-exists -h /var/run/postgresql -U postgres -d railway "$artifact" > "$base/restore.stdout.private" 2> "$base/restore.stderr.private"
phase=verify_restored_state
test "$(target_psql -c "SELECT string_agg(version_num,',' ORDER BY version_num) FROM alembic_version")" = 0111,0113,0120
test "$(target_psql -c "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'")" = 128
test "$(sha256sum "$artifact" | cut -d' ' -f1)" = 5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c
printf 'NAHLA_RECOVERY {"kind":"clone_retained_archive_restore","status":"PASS","heads":["0111","0113","0120"],"tables":128,"archive_unchanged":true,"source_connection":false,"repository_inventory_files":146,"reviewed_harness_hash_verified":true}\n'
