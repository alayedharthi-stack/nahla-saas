set -euo pipefail
umask 077
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
base=/recovery/shopify-logical-20261013
artifact="$base/nahla.dump"
expected_sha=5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c
test "$(sha256sum "$artifact" | awk '{print $1}')" = "$expected_sha"
test "$(stat -c %s "$artifact")" = 26077103
test "$(stat -c %a "$artifact")" = 400
target_psql() { env -u PGPASSWORD -u PGOPTIONS psql -X -q -A -t --no-password -h /var/run/postgresql -U postgres -d railway -v ON_ERROR_STOP=1 "$@"; }
test "$(target_psql -c 'SHOW server_version_num')" = 180006
perl -MJSON::PP -e '
 local $/; open my $in,"<",$ARGV[0] or die; my $sql=<$in>; close $in;
 my $lit=qr/\x27(?:\x27\x27|[^\x27])*\x27::character varying/;
 my @equivalence;
 my $count=($sql =~ s{\(ARRAY\[($lit(?:,\s*$lit)*)\]\)::text\[\]}{
    my $original=$&;my $body=$1;my @items=($body =~ /$lit/g);
    my $new="ARRAY[".join(", ",map{"($_)::text"}@items)."]";
    push @equivalence,"SELECT (($original) IS NOT DISTINCT FROM ($new));";
    $new;
 }ge);
 die "unexpected_rewrite_count" unless $count==33;
 open my $out,">",$ARGV[1] or die;print $out $sql;close $out;
 open my $checks,">",$ARGV[2] or die;print $checks "SET standard_conforming_strings=on;\n",join("\n",@equivalence),"\n";close $checks;
 print "NAHLA_RECOVERY ",encode_json({kind=>"schema_comparison_rule",rule=>"varchar_literal_array_to_text_element_cast",rewrites=>$count,archive_modified=>JSON::PP::false}),"\n";
' "$base/source.schema.canonical.sql" "$base/source.schema.pg18-normalized.sql" "$base/array_cast_equivalence.sql"
cmp -s "$base/source.schema.pg18-normalized.sql" "$base/target.schema.canonical.sql"
target_psql -f "$base/array_cast_equivalence.sql" > "$base/array_cast_equivalence.results.private" 2> "$base/array_cast_equivalence.errors.private"
test "$(wc -l < "$base/array_cast_equivalence.results.private" | tr -d ' ')" = 33
if grep -v '^t$' "$base/array_cast_equivalence.results.private" >/dev/null; then exit 51; fi
cmp -s "$base/source.rows.jsonl" "$base/target.rows.jsonl"
cmp -s "$base/source.heads.json" "$base/target.heads.json"
cmp -s "$base/source.locale.json" "$base/target.locale.json"
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
target_psql -f "$base/sequences.compare.sql" > "$base/sequences.results.private" 2> "$base/sequences.errors.private"
sequences="$(wc -l < "$base/sequences.toc.private" | tr -d ' ')"
test "$(wc -l < "$base/sequences.results.private" | tr -d ' ')" = "$sequences"
if grep -v '^t$' "$base/sequences.results.private" >/dev/null; then exit 52; fi
test "$(sha256sum "$artifact" | awk '{print $1}')" = "$expected_sha"
tables="$(wc -l < "$base/source.rows.jsonl" | tr -d ' ')"
rowsha="$(sha256sum "$base/source.rows.jsonl" | awk '{print $1}')"
schemasha="$(sha256sum "$base/source.schema.pg18-normalized.sql" | awk '{print $1}')"
verified="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"kind":"restore_rehearsal","status":"PASS","artifact_kind":"postgresql_custom_archive","sha256":"%s","actual_bytes":26077103,"snapshot_at_utc":"2026-10-13T08:18:50Z","completed_at_utc":"2026-10-13T08:18:55Z","restore_verified_at_utc":"%s","source_service_id":"b77b3d27-47b0-4a3a-83fd-44def66a3a84","source_volume_id":"009bd0d5-85ed-4de4-99fc-94ea963c9d65","storage_volume_id":"2f25c7cf-f0b1-467b-bb98-e203ec40e6d9","target_service_id":"6afcbd3e-15dd-46c1-9183-932fe02d075f","source_heads":["0111","0113","0120"],"restored_heads":["0111","0113","0120"],"tables_compared":%s,"row_manifest_sha256":"%s","schema_comparison":"byte_equal_after_33_verified_varchar_array_coercions","schema_sha256":"%s","array_cast_equivalence_checks":33,"archive_sequences_compared":%s,"sequence_parity":true,"locale_parity":true,"archive_unchanged":true,"archive_permission":"0400","source_connection_closed":true,"source_config_references_cleared":true,"source_ddl":false,"cross_database_oid_comparison":false,"original_647mb_card_restored":false,"migrations_run":false,"dump_contents_exported":false}\n' "$expected_sha" "$verified" "$tables" "$rowsha" "$schemasha" "$sequences" > "$base/verification.json"
printf 'NAHLA_RECOVERY '; cat "$base/verification.json"
