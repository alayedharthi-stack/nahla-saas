set -euo pipefail
umask 077
test "${RAILWAY_PROJECT_ID:-}" = f0090862-0a40-4293-bd5d-e94df58762b5
test "${RAILWAY_ENVIRONMENT_ID:-}" = ede962ce-3042-4dae-94de-623837e83ed9
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${PGDATA:-}" = /tmp/nahla-shopify-restore-pgdata
base=/recovery/shopify-logical-20261013
sha=5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c
test "$(sha256sum "$base/nahla.dump" | awk '{print $1}')" = "$sha"
perl -MJSON::PP -e 'local $/;my $v=decode_json(<>);die unless $v->{status} eq "PASS" && $v->{archive_sequences_compared}==117 && $v->{tables_compared}==128 && $v->{archive_unchanged};' "$base/verification.json"
test ! -e "$base/initial-comparison-status.json"
cp "$base/status.json" "$base/initial-comparison-status.json"
cp "$base/verification.json" "$base/status.json"
chmod 400 "$base/nahla.dump" "$base/backup.manifest.json" "$base/verification.json"
manifest_sha="$(sha256sum "$base/backup.manifest.json" | awk '{print $1}')"
verification_sha="$(sha256sum "$base/verification.json" | awk '{print $1}')"
at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
clients="$(ps -eo comm | awk '$0=="psql" || $0=="pg_dump" || $0=="pg_restore" {n++} END {print n+0}')"
test "$clients" = 0
printf '{"kind":"retained_recovery_checkpoint","checkpoint_at_utc":"%s","archive_sha256":"%s","archive_bytes":26077103,"manifest_sha256":"%s","verification_sha256":"%s","storage_volume_id":"2f25c7cf-f0b1-467b-bb98-e203ec40e6d9","target_service_id":"6afcbd3e-15dd-46c1-9183-932fe02d075f","artifact_path":"/recovery/shopify-logical-20261013/nahla.dump","archive_outside_ephemeral_pgdata":true,"client_processes":0,"permanent_cleanup_authorized":false,"shutdown_requested":true}\n' "$at" "$sha" "$manifest_sha" "$verification_sha" > "$base/checkpoint.json"
sync -f "$base/nahla.dump"
sync -f "$base/checkpoint.json"
printf 'NAHLA_RECOVERY '; cat "$base/checkpoint.json"
unset SRC_PGPASSWORD SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGDATABASE
gosu postgres pg_ctl -D /tmp/nahla-shopify-restore-pgdata stop -m fast -w -t 15 > /recovery/operator/shutdown.private.log 2>&1
