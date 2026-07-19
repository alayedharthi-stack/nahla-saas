#!/usr/bin/env bash
# Confined internal E2E runner entrypoint.
# Fails closed unless CAP_NET_ADMIN is present and firewall/probes succeed.
set -euo pipefail

export PYTHONPATH="/app:${PYTHONPATH:-}"
RUNNER_ROOT="/app/ops/internal_e2e_runner"
EVIDENCE_DIR="${NAHLA_INTERNAL_E2E_EVIDENCE_DIR:-/evidence}"
CONFIG_PATH="${NAHLA_INTERNAL_E2E_RUNNER_CONFIG:-/run/config/runner_config.json}"
DATABASE_URL_FILE="${NAHLA_INTERNAL_E2E_DATABASE_URL_FILE:-/run/secrets/database_url}"
STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

mkdir -p "${EVIDENCE_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "runner_config_missing" >&2
  exit 1
fi

python3 "${RUNNER_ROOT}/scripts/validate_config.py" \
  --config "${CONFIG_PATH}" \
  --database-url-file "${DATABASE_URL_FILE}" \
  --output "${EVIDENCE_DIR}/config_validation.json" >/dev/null

python3 - <<'PY' "${CONFIG_PATH}" "${DATABASE_URL_FILE}" "${EVIDENCE_DIR}/hosts_pinning.json"
import json
import sys
from pathlib import Path

from ops.internal_e2e_runner.lib.config import (
    database_url_fingerprint,
    load_runner_config,
    validate_runner_config_blockers,
)

config_path, database_url_file, hosts_out = sys.argv[1:4]
with open(config_path, encoding="utf-8") as handle:
    raw = json.load(handle)
database_url = Path(database_url_file).read_text(encoding="utf-8").strip() if Path(database_url_file).exists() else ""
blockers = validate_runner_config_blockers(raw, database_url=database_url or None)
if blockers:
    raise SystemExit("runner_config_blockers:" + ",".join(sorted(set(blockers))))
config = load_runner_config(raw)
hosts_pinning = {
    config.llm_endpoint.hostname: list(config.llm_endpoint.ips),
    config.db_proxy_endpoint.hostname: list(config.db_proxy_endpoint.ips),
    config.dns_resolver.hostname: list(config.dns_resolver.ips),
}
Path(hosts_out).write_text(json.dumps(hosts_pinning, indent=2, sort_keys=True) + "\n", encoding="utf-8")
Path("/tmp/database_url_fingerprint").write_text(
    database_url_fingerprint(database_url) if database_url else "",
    encoding="utf-8",
)
PY

# Pin resolved hosts before default-drop so TLS/HTTP clients avoid runtime DNS.
python3 - <<'PY' "${EVIDENCE_DIR}/hosts_pinning.json"
import json
import sys
from pathlib import Path

hosts = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
lines = []
seen = set()
for host, ips in hosts.items():
    for ip in ips:
        key = (ip, host)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{ip} {host}")
Path("/etc/hosts").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

RULES_DUMP="${EVIDENCE_DIR}/firewall_rules.sanitized"
BACKEND_FILE="${EVIDENCE_DIR}/firewall_backend"
bash "${RUNNER_ROOT}/scripts/apply_firewall.sh" "${CONFIG_PATH}" "${RULES_DUMP}" "${BACKEND_FILE}"

python3 - <<'PY' > "${EVIDENCE_DIR}/capability_proof.json"
import json
import os
from datetime import datetime, timezone

payload = {
    "cap_net_admin_required": True,
    "no_new_privileges_expected": True,
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "pid": os.getpid(),
}
print(json.dumps(payload, sort_keys=True))
PY

PROBE_OUT="${EVIDENCE_DIR}/probe_results.json"
python3 "${RUNNER_ROOT}/scripts/probe_connectivity.py" \
  --config "${CONFIG_PATH}" \
  --output "${PROBE_OUT}"

COMPLETED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
IMAGE_DIGEST_INPUT="${NAHLA_INTERNAL_E2E_IMAGE_DIGEST_INPUT:-unknown}"
DATABASE_FINGERPRINT="$(cat /tmp/database_url_fingerprint)"
FIREWALL_BACKEND="$(cat "${BACKEND_FILE}")"

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
  --runtime-verification-status "container_runtime_verified" \
  --output "${EVIDENCE_DIR}/network_evidence.json" \
  --operator-command "$@"

if [[ "$#" -eq 0 ]]; then
  set -- preflight
fi

OPERATOR_CMD=(python3 /app/scripts/operators/internal_conversational_e2e_session.py "$@")
if [[ "$1" == "preflight" || "$1" == "run" ]]; then
  TENANT_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "${CONFIG_PATH}")"
  if [[ "$1" == "preflight" ]]; then
    OPERATOR_CMD+=(--tenant-id "${TENANT_ID}")
  elif [[ "$1" == "run" ]]; then
    if [[ "$#" -lt 3 ]]; then
      echo "operator_run_requires_scenarios" >&2
      exit 1
    fi
    OPERATOR_CMD+=(--tenant-id "${TENANT_ID}" --scenarios "$3")
  fi
fi

exec "${OPERATOR_CMD[@]}"
