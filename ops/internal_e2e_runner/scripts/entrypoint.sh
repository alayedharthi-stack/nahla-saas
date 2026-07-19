#!/usr/bin/env bash
# Confined internal E2E runner entrypoint.
# Fails closed unless CAP_NET_ADMIN is present and firewall/probes succeed.
set -euo pipefail

export PYTHONPATH="/app:${PYTHONPATH:-}"
RUNNER_ROOT="/app/ops/internal_e2e_runner"
EVIDENCE_DIR="${NAHLA_INTERNAL_E2E_EVIDENCE_DIR:-/evidence}"
CONFIG_PATH="${NAHLA_INTERNAL_E2E_RUNNER_CONFIG:-/run/config/runner_config.json}"
STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

mkdir -p "${EVIDENCE_DIR}" || {
  echo "evidence_dir_unavailable" >&2
  exit 1
}
export NAHLA_INTERNAL_E2E_SESSION_DIR="${EVIDENCE_DIR}/sessions"
mkdir -p "${NAHLA_INTERNAL_E2E_SESSION_DIR}" || {
  echo "session_evidence_dir_unavailable" >&2
  exit 1
}
[[ -d "${NAHLA_INTERNAL_E2E_SESSION_DIR}" && -w "${NAHLA_INTERNAL_E2E_SESSION_DIR}" ]] || {
  echo "session_evidence_dir_unavailable" >&2
  exit 1
}

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "runner_config_missing" >&2
  exit 1
fi

for secret in database_url evidence_hmac_key attestation_hmac_key attestation_json \
  attestation_signature network_confirm llm_api_key tenant_allowlist test_phone \
  phone_allowlist; do
  [[ -s "/run/secrets/${secret}" ]] || {
    echo "required_secret_file_missing:${secret}" >&2
    exit 1
  }
done

# Export only values required by PR #662. Never print or include values in evidence.
export NAHLA_INTERNAL_E2E_DATABASE_URL="$(< /run/secrets/database_url)"
export DATABASE_URL="${NAHLA_INTERNAL_E2E_DATABASE_URL}"
export NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY="$(< /run/secrets/evidence_hmac_key)"
export NAHLA_INTERNAL_E2E_ATTESTATION_HMAC_KEY="$(< /run/secrets/attestation_hmac_key)"
export NAHLA_INTERNAL_E2E_ATTESTATION_JSON="$(< /run/secrets/attestation_json)"
export NAHLA_INTERNAL_E2E_ATTESTATION_SIGNATURE="$(< /run/secrets/attestation_signature)"
export NAHLA_INTERNAL_E2E_NETWORK_FIREWALL_CONFIRM="$(< /run/secrets/network_confirm)"
export ANTHROPIC_API_KEY="$(< /run/secrets/llm_api_key)"
export NAHLA_INTERNAL_E2E_TENANT_ALLOWLIST="$(< /run/secrets/tenant_allowlist)"
export NAHLA_INTERNAL_E2E_TEST_PHONE="$(< /run/secrets/test_phone)"
export NAHLA_INTERNAL_E2E_PHONE_ALLOWLIST="$(< /run/secrets/phone_allowlist)"
export NAHLA_INTERNAL_E2E_ENABLED=true
export NAHLA_INTERNAL_E2E_CONFIRM=true
unset OPENAI_API_KEY CLAUDE_API_KEY GOOGLE_API_KEY GEMINI_API_KEY MISTRAL_API_KEY COHERE_API_KEY GROQ_API_KEY

CONFIG_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pinned_revision"])' "${CONFIG_PATH}")"
BAKED_SHA="$(< /nahla-revision)"
[[ "${CONFIG_SHA}" =~ ^[0-9a-f]{40}$ ]] || { echo "config_revision_invalid" >&2; exit 1; }
[[ "${NAHLA_INTERNAL_E2E_PINNED_REVISION:-}" == "${CONFIG_SHA}" ]] || { echo "checkout_revision_mismatch" >&2; exit 1; }
[[ "${NAHLA_IMAGE_LABEL_REVISION:-}" == "${CONFIG_SHA}" ]] || { echo "image_label_revision_mismatch" >&2; exit 1; }
[[ "${BAKED_SHA}" == "${CONFIG_SHA}" ]] || { echo "baked_revision_mismatch" >&2; exit 1; }

if [[ "$#" -eq 0 ]]; then set -- preflight; fi
case "$1:$#" in
  preflight:1) NORMALIZED=(preflight) ;;
  run:3)
    [[ "$2" == "--scenarios" && -r "$3" ]] || { echo "operator_command_invalid" >&2; exit 1; }
    NORMALIZED=(run --scenarios "$3")
    ;;
  *) echo "operator_command_invalid" >&2; exit 1 ;;
esac

python3 "${RUNNER_ROOT}/scripts/validate_config.py" \
  --config "${CONFIG_PATH}" \
  --database-url-file /run/secrets/database_url \
  --output "${EVIDENCE_DIR}/config_validation.json" >/dev/null
python3 "${RUNNER_ROOT}/scripts/verify_docker_topology.py" \
  --inspect "${EVIDENCE_DIR}/docker-inspect.json" \
  --egress-baseline "${EVIDENCE_DIR}/egress-control-baseline.json" \
  --expected-revision "${CONFIG_SHA}" \
  --output "${EVIDENCE_DIR}/docker-topology-verified.json"

LLM_HOST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["llm_host"])' "${CONFIG_PATH}")"
DB_HOST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_proxy_host"])' "${CONFIG_PATH}")"
PROXY_IP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["connect_proxy_ip"])' "${CONFIG_PATH}")"
RELAY_IP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_relay_ip"])' "${CONFIG_PATH}")"
RELAY_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_relay_port"])' "${CONFIG_PATH}")"
export HTTPS_PROXY="http://${PROXY_IP}:3128"
export https_proxy="${HTTPS_PROXY}"
export NO_PROXY="localhost,127.0.0.1,${DB_HOST}"
export NAHLA_INTERNAL_E2E_LLM_ENABLED=true
export NAHLA_INTERNAL_E2E_LLM_HOST_ALLOWLIST="${LLM_HOST}"
python3 - <<'PY' "${EVIDENCE_DIR}/hosts_pinning.json" "${LLM_HOST}" "${PROXY_IP}" "${DB_HOST}" "${RELAY_IP}" "${RELAY_PORT}"
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({
        "llm_transport": {"hostname": sys.argv[2], "via_connect_proxy_ip": sys.argv[3]},
        "db_tls_hostname_mapping": {
            "hostname": sys.argv[4],
            "relay_ip": sys.argv[5],
            "port": int(sys.argv[6]),
        },
        "base_etc_hosts_preserved": True,
    }, handle, sort_keys=True)
    handle.write("\n")
PY

RULES_DUMP="${EVIDENCE_DIR}/firewall_rules.sanitized"
BACKEND_FILE="${EVIDENCE_DIR}/firewall_backend"
bash "${RUNNER_ROOT}/scripts/apply_firewall.sh" "${CONFIG_PATH}" "${RULES_DUMP}" "${BACKEND_FILE}"

python3 - <<'PY' > "${EVIDENCE_DIR}/capability_proof.json"
import json
import os
from datetime import datetime, timezone

status = {}
with open("/proc/self/status", encoding="utf-8") as handle:
    for line in handle:
        if line.startswith(("CapEff:", "NoNewPrivs:")):
            key, value = line.split(":", 1)
            status[key] = value.strip()
payload = {
    "cap_net_admin_required": True,
    "cap_net_admin_effective": bool(int(status.get("CapEff", "0"), 16) & (1 << 12)),
    "no_new_privileges": status.get("NoNewPrivs") == "1",
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "pid": os.getpid(),
}
print(json.dumps(payload, sort_keys=True))
PY

PROBE_OUT="${EVIDENCE_DIR}/probe_results.json"
mapfile -t DIRECT_CONTROLS < <(
  python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))
for target in c["negative_probe_targets"]:
    for ip in sorted(set(target["ips"])):
        print("{}|{}|{}".format(target["host"], ip, target["port"]))
' "${CONFIG_PATH}"
)
[[ "${#DIRECT_CONTROLS[@]}" -ge 2 ]] || {
  echo "negative_probe_controls_incomplete" >&2
  exit 1
}
DIRECT_CONTROL_ARGS=()
for control in "${DIRECT_CONTROLS[@]}"; do
  DIRECT_CONTROL_ARGS+=(--direct-control "${control}")
done
python3 "${RUNNER_ROOT}/scripts/probe_connectivity.py" \
  --config "${CONFIG_PATH}" \
  --proxy-host "${PROXY_IP}" \
  --relay-host "${RELAY_IP}" \
  --relay-port "${RELAY_PORT}" \
  "${DIRECT_CONTROL_ARGS[@]}" \
  --output "${PROBE_OUT}"

COMPLETED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
IMAGE_DIGEST_INPUT="${NAHLA_INTERNAL_E2E_IMAGE_DIGEST_INPUT:?image digest required}"
DATABASE_FINGERPRINT="$(python3 -c 'from pathlib import Path; from ops.internal_e2e_runner.lib.config import database_url_fingerprint; print(database_url_fingerprint(Path("/run/secrets/database_url").read_text().strip()))')"
FIREWALL_BACKEND="$(cat "${BACKEND_FILE}")"
OPERATOR_COMMAND_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${NORMALIZED[@]}")"

python3 "${RUNNER_ROOT}/scripts/assemble_evidence.py" \
  --config "${CONFIG_PATH}" \
  --started-at "${STARTED_AT}" \
  --completed-at "${COMPLETED_AT}" \
  --capability-proof "${EVIDENCE_DIR}/capability_proof.json" \
  --firewall-backend "${FIREWALL_BACKEND}" \
  --rules-dump "${RULES_DUMP}" \
  --hosts-pinning "${EVIDENCE_DIR}/hosts_pinning.json" \
  --probe-results "${PROBE_OUT}" \
  --image-digest-input "${IMAGE_DIGEST_INPUT}" \
  --database-url-fingerprint "${DATABASE_FINGERPRINT}" \
  --runtime-verification-status "network_proofs_passed_operator_pending" \
  --docker-inspect "${EVIDENCE_DIR}/docker-topology-verified.json" \
  --output "${EVIDENCE_DIR}/network_evidence.json" \
  --operator-command-json "${OPERATOR_COMMAND_JSON}"

TENANT_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "${CONFIG_PATH}")"
if [[ "${NORMALIZED[0]}" == "preflight" ]]; then
  OPERATOR_ARGS=(preflight --tenant-id "${TENANT_ID}")
else
  OPERATOR_ARGS=(run --tenant-id "${TENANT_ID}" --scenarios "${NORMALIZED[2]}")
fi

set +e
python3 /app/scripts/operators/internal_conversational_e2e_session.py "${OPERATOR_ARGS[@]}"
OPERATOR_EXIT=$?
set -e
python3 - <<'PY' "${EVIDENCE_DIR}/operator_status.json" "${EVIDENCE_DIR}/network_evidence.json" "${OPERATOR_EXIT}"
import hashlib, json, sys
from datetime import datetime, timezone
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({
        "operator_exit_status": int(sys.argv[3]),
        "network_evidence_preserved": True,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }, handle, sort_keys=True)
    handle.write("\n")
with open(sys.argv[2], encoding="utf-8") as handle:
    evidence = json.load(handle)
evidence["operator_exit_status"] = int(sys.argv[3])
evidence["operator_outcome"] = "passed" if int(sys.argv[3]) == 0 else "failed"
evidence.pop("evidence_hash_sha256", None)
canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
evidence["evidence_hash_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(evidence, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY
exit "${OPERATOR_EXIT}"
