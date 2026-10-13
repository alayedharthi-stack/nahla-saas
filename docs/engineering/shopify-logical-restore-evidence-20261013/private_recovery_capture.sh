# Run only inside the approved PG18 target. Raw archives/errors never leave its private volume.
set -euo pipefail
umask 077
test "${RAILWAY_PROJECT_ID:-}" = f0090862-0a40-4293-bd5d-e94df58762b5
test "${RAILWAY_ENVIRONMENT_ID:-}" = ede962ce-3042-4dae-94de-623837e83ed9
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${PGDATA:-}" = /tmp/nahla-shopify-restore-pgdata
test "${SRC_PGHOST:-}" = postgres-ancu.railway.internal
test "${SRC_PGPORT:-}" = 5432
test "${SRC_PGDATABASE:-}" = railway
test "${SRC_PGUSER:-}" = postgres
test -n "${SRC_PGPASSWORD:-}"
test "$(id -u)" = 0
grep -q ' /recovery ' /proc/self/mountinfo
base=/recovery/shopify-logical-20261013
mkdir "$base"
chmod 700 "$base"
artifact="$base/nahla.dump"
status="$base/status.json"
phase=source_snapshot
finished=0
holding=0
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
source_psql() {
 env PGHOST="$SRC_PGHOST" PGPORT="$SRC_PGPORT" PGUSER="$SRC_PGUSER" PGPASSWORD="$SRC_PGPASSWORD" PGDATABASE="$SRC_PGDATABASE" PGSSLMODE=require PGCONNECT_TIMEOUT=5 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=1200000' psql -X -q -A -t --no-password -v ON_ERROR_STOP=1 "$@"
}
source_dump() {
 env PGHOST="$SRC_PGHOST" PGPORT="$SRC_PGPORT" PGUSER="$SRC_PGUSER" PGPASSWORD="$SRC_PGPASSWORD" PGDATABASE="$SRC_PGDATABASE" PGSSLMODE=require PGCONNECT_TIMEOUT=5 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=5000' timeout --signal=TERM --kill-after=15s 900s pg_dump --no-password --lock-wait-timeout=5000 --snapshot="$snapshot" "$@"
}
target_psql() { env -u PGPASSWORD -u PGOPTIONS psql -X -q -A -t --no-password -h /var/run/postgresql -U postgres -d railway -v ON_ERROR_STOP=1 "$@"; }
onexit() {
 rc=$?
 set +e
 if [ "$holding" = 1 ]; then printf 'ROLLBACK;\n\\q\n' >&"$infd"; fi
 unset SRC_PGPASSWORD SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGDATABASE
 if [ "$finished" != 1 ]; then
   printf '{"kind":"restore_rehearsal","status":"BLOCKED","phase":"%s","exit_code":%s,"original_archive_retained_if_completed":true,"source_ddl":false,"dump_contents_exported":false}\n' "$phase" "$rc" > "$status"
 fi
}
trap onexit EXIT
local_version="$(target_psql -c 'SHOW server_version_num')"
case "$local_version" in 18????) ;; *) exit 31;; esac
test "$(target_psql -c 'SHOW listen_addresses')" = 127.0.0.1
test "$(target_psql -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','m','S')")" = 0
coproc SNAP { source_psql 2>"$base/source.errors.private"; }
infd=${SNAP[1]}
outfd=${SNAP[0]}
holding=1
printf 'BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n' >&"$infd"
IFS= read -r snapshot <&"$outfd"
case "$snapshot" in *[!0-9A-Fa-f-]*|'') exit 32;; esac
printf "SELECT CASE WHEN current_database()='railway' AND current_setting('transaction_read_only')='on' AND current_setting('server_version_num')::int BETWEEN 180000 AND 189999 AND NOT pg_is_in_recovery() THEN 'SOURCE_IDENTITY_PASS' ELSE 'SOURCE_IDENTITY_FAIL' END;\n" >&"$infd"
IFS= read -r identity <&"$outfd"
test "$identity" = SOURCE_IDENTITY_PASS
printf "SELECT COALESCE(json_agg(version_num ORDER BY version_num),'[]'::json)::text FROM public.alembic_version;\n" >&"$infd"
IFS= read -r source_heads <&"$outfd"
test "${source_heads// /}" = '["0111","0113","0120"]'
printf "SELECT count(*) FROM pg_roles WHERE rolname <> current_user AND rolname NOT LIKE 'pg_%%';\n" >&"$infd"
IFS= read -r extra_roles <&"$outfd"
test "$extra_roles" = 0
printf "SELECT to_char(transaction_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');\n" >&"$infd"
IFS= read -r snapshot_at <&"$outfd"
printf '%s\n' "$source_heads" > "$base/source.heads.json"
printf "\\\\o %s/source.locale.json\\nSELECT json_build_object('encoding',pg_encoding_to_char(d.encoding),'collate',d.datcollate,'ctype',d.datctype,'locale_provider',d.datlocprovider,'locale',to_jsonb(d)->>'datlocale')::text FROM pg_database d WHERE datname=current_database();\\n\\\\o\\nSELECT 'SOURCE_LOCALE_DONE';\\n" "$base" >&"$infd"
IFS= read -r marker <&"$outfd"
test "$marker" = SOURCE_LOCALE_DONE
target_psql -c "SELECT json_build_object('encoding',pg_encoding_to_char(d.encoding),'collate',d.datcollate,'ctype',d.datctype,'locale_provider',d.datlocprovider,'locale',to_jsonb(d)->>'datlocale')::text FROM pg_database d WHERE datname=current_database()" > "$base/target.locale.json"
cmp -s "$base/source.locale.json" "$base/target.locale.json"
phase=capture_rows
cat > "$base/rows.sql" <<'ROWS'
SELECT format('SELECT json_build_object(''schema'',%L,''table'',%L,''rows'',count(*))::text FROM %I.%I;',n.nspname,c.relname,n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','m')
ORDER BY n.nspname,c.relname
\gexec
ROWS
printf '\\o %s/source.rows.jsonl\n\\i %s/rows.sql\n\\o\nSELECT '\''SOURCE_ROWS_DONE'\'';\n' "$base" "$base" >&"$infd"
IFS= read -r marker <&"$outfd"
test "$marker" = SOURCE_ROWS_DONE
phase=logical_backup
source_dump --format=custom --file="$artifact" 2> "$base/dump.errors.private"
completed="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
bytes="$(stat -c %s "$artifact")"
test "$bytes" -gt 0
test "$bytes" -lt 800000000
sha="$(sha256sum "$artifact" | awk '{print $1}')"
chmod 400 "$artifact"
printf '{"kind":"postgresql_custom_archive","sha256":"%s","actual_bytes":%s,"capture_started_at_utc":"%s","snapshot_at_utc":"%s","completed_at_utc":"%s","source_service_id":"b77b3d27-47b0-4a3a-83fd-44def66a3a84","source_volume_id":"009bd0d5-85ed-4de4-99fc-94ea963c9d65","storage_volume_id":"2f25c7cf-f0b1-467b-bb98-e203ec40e6d9","storage_service_id":"6afcbd3e-15dd-46c1-9183-932fe02d075f","artifact_path":"/recovery/shopify-logical-20261013/nahla.dump","original_647mb_card_restored":false}\n' "$sha" "$bytes" "$started" "$snapshot_at" "$completed" > "$base/backup.manifest.json"
phase=source_schema
source_dump --schema-only --no-owner --no-acl --no-comments --file="$base/source.schema.sql" 2> "$base/schema.errors.private"
printf 'ROLLBACK;\n\\q\n' >&"$infd"
holding=0
unset SRC_PGPASSWORD SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGDATABASE
phase=restore
pg_restore --list "$artifact" > "$base/archive.toc.private"
env -u PGPASSWORD -u PGOPTIONS timeout --signal=TERM --kill-after=15s 600s pg_restore --no-password --exit-on-error --single-transaction --clean --if-exists -h /var/run/postgresql -U postgres -d railway "$artifact" > "$base/restore.stdout.private" 2> "$base/restore.errors.private"
phase=compare_rows
target_psql -f "$base/rows.sql" > "$base/target.rows.jsonl"
cmp -s "$base/source.rows.jsonl" "$base/target.rows.jsonl"
target_psql -c "SELECT COALESCE(json_agg(version_num ORDER BY version_num),'[]'::json)::text FROM public.alembic_version" > "$base/target.heads.json"
cmp -s "$base/source.heads.json" "$base/target.heads.json"
phase=compare_schema
env -u PGPASSWORD -u PGOPTIONS pg_dump --no-password -h /var/run/postgresql -U postgres -d railway --schema-only --no-owner --no-acl --no-comments --file="$base/target.schema.sql"
sed -E '/^\\(un)?restrict /d; /^-- Dumped (from|by)/d' "$base/source.schema.sql" > "$base/source.schema.canonical.sql"
sed -E '/^\\(un)?restrict /d; /^-- Dumped (from|by)/d' "$base/target.schema.sql" > "$base/target.schema.canonical.sql"
cmp -s "$base/source.schema.canonical.sql" "$base/target.schema.canonical.sql"
phase=compare_archive_sequences
awk '/ SEQUENCE SET /' "$base/archive.toc.private" > "$base/sequences.toc.private"
pg_restore --use-list="$base/sequences.toc.private" --file="$base/sequences.archive.sql" "$artifact"
cat > "$base/sequences.compare.sql" <<'SEQ'
CREATE FUNCTION pg_temp.nahla_compare_sequence(seqname text, expected_value bigint, expected_called boolean) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE actual_value bigint; actual_called boolean;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=seqname::regclass AND relkind='S') THEN RAISE EXCEPTION 'archive_sequence_missing'; END IF;
 EXECUTE format('SELECT last_value,is_called FROM %s',seqname::regclass) INTO actual_value,actual_called;
 RETURN actual_value=expected_value AND actual_called=expected_called;
END;
$$;
SEQ
awk '/^SELECT pg_catalog.setval\(/ {sub(/^SELECT pg_catalog.setval\(/,"SELECT pg_temp.nahla_compare_sequence("); print}' "$base/sequences.archive.sql" >> "$base/sequences.compare.sql"
target_psql -f "$base/sequences.compare.sql" > "$base/sequences.results.private"
sequence_count="$(wc -l < "$base/sequences.toc.private" | tr -d ' ')"
test "$(wc -l < "$base/sequences.results.private" | tr -d ' ')" = "$sequence_count"
if grep -v '^t$' "$base/sequences.results.private" >/dev/null; then exit 41; fi
phase=final_archive_integrity
test "$(sha256sum "$artifact" | awk '{print $1}')" = "$sha"
test "$(stat -c %a "$artifact")" = 400
rows_sha="$(sha256sum "$base/source.rows.jsonl" | awk '{print $1}')"
schema_sha="$(sha256sum "$base/source.schema.canonical.sql" | awk '{print $1}')"
table_count="$(wc -l < "$base/source.rows.jsonl" | tr -d ' ')"
verified="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"kind":"restore_rehearsal","status":"PASS","artifact_kind":"postgresql_custom_archive","sha256":"%s","actual_bytes":%s,"snapshot_at_utc":"%s","completed_at_utc":"%s","restore_verified_at_utc":"%s","source_service_id":"b77b3d27-47b0-4a3a-83fd-44def66a3a84","source_volume_id":"009bd0d5-85ed-4de4-99fc-94ea963c9d65","storage_volume_id":"2f25c7cf-f0b1-467b-bb98-e203ec40e6d9","target_service_id":"6afcbd3e-15dd-46c1-9183-932fe02d075f","source_heads":["0111","0113","0120"],"restored_heads":["0111","0113","0120"],"tables_compared":%s,"row_manifest_sha256":"%s","schema_sha256":"%s","archive_sequences_compared":%s,"sequence_parity":true,"archive_unchanged":true,"archive_permission":"0400","source_connection_closed":true,"source_ddl":false,"cross_database_oid_comparison":false,"original_647mb_card_restored":false,"migrations_run":false,"dump_contents_exported":false}\n' "$sha" "$bytes" "$snapshot_at" "$completed" "$verified" "$table_count" "$rows_sha" "$schema_sha" "$sequence_count" > "$status"
finished=1
