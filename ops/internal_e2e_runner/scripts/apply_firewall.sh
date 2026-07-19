#!/usr/bin/env bash
# Apply OUTPUT default-drop firewall rules for the confined internal E2E runner.
set -euo pipefail

CONFIG_JSON="${1:-}"
RULES_OUT="${2:-}"
BACKEND_OUT="${3:-}"

if [[ -z "${CONFIG_JSON}" || -z "${RULES_OUT}" || -z "${BACKEND_OUT}" ]]; then
  echo "apply_firewall_usage_error" >&2
  exit 1
fi

if ! python3 - <<'PY'
import ctypes
import sys

CAP_NET_ADMIN = 1 << 12

def has_cap_net_admin() -> bool:
    libc = ctypes.CDLL(None)
    capget = getattr(libc, "capget", None)
    if capget is None:
        return False
    class _cap_header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
    class _cap_data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
    header = _cap_header(0x20080522, 0)
    data = _cap_data()
    if capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        return False
    return bool(data.effective & CAP_NET_ADMIN)

if not has_cap_net_admin():
    print("cap_net_admin_missing", file=sys.stderr)
    raise SystemExit(1)
PY
then
  exit 1
fi

PROXY_IP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["connect_proxy_ip"])' "${CONFIG_JSON}")"
PROXY_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["connect_proxy_port"])' "${CONFIG_JSON}")"
RELAY_IP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_relay_ip"])' "${CONFIG_JSON}")"
RELAY_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_relay_port"])' "${CONFIG_JSON}")"

if command -v iptables >/dev/null 2>&1; then
  BACKEND="iptables"
  iptables -P OUTPUT DROP
  iptables -A OUTPUT -o lo -j ACCEPT
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A OUTPUT -p tcp -d "${PROXY_IP}" --dport "${PROXY_PORT}" -j ACCEPT
  iptables -A OUTPUT -p tcp -d "${RELAY_IP}" --dport "${RELAY_PORT}" -j ACCEPT
  iptables-save > "${RULES_OUT}"
  python3 -c 'import hashlib,sys; p=sys.argv[1]; print(hashlib.sha256(open(p,"rb").read()).hexdigest())' "${RULES_OUT}" > "${RULES_OUT}.hash"
elif command -v nft >/dev/null 2>&1; then
  BACKEND="nft"
  NFT_FILE="/tmp/confined_e2e.nft"
  cat > "${NFT_FILE}" <<EOF
flush ruleset
table inet confined_e2e {
  chain output {
    type filter hook output priority 0; policy drop;
    oif "lo" accept
    ct state established,related accept
EOF
  echo "    ip daddr ${PROXY_IP} tcp dport ${PROXY_PORT} accept" >> "${NFT_FILE}"
  echo "    ip daddr ${RELAY_IP} tcp dport ${RELAY_PORT} accept" >> "${NFT_FILE}"
  {
    echo "  }"
    echo "}"
  } >> "${NFT_FILE}"
  nft -f "${NFT_FILE}"
  nft list ruleset > "${RULES_OUT}"
  sha256sum "${RULES_OUT}" | awk '{print $1}' > "${RULES_OUT}.hash"
else
  echo "firewall_backend_unavailable" >&2
  exit 1
fi

printf '%s\n' "${BACKEND}" > "${BACKEND_OUT}"

# Verification: OUTPUT policy must be DROP and at least one LLM allow rule present.
if [[ "${BACKEND}" == "iptables" ]]; then
  if ! iptables -S OUTPUT | grep -q '\-P OUTPUT DROP'; then
    echo "firewall_verification_failed" >&2
    exit 1
  fi
  EXPECTED_RULES="$(iptables -S OUTPUT)"
  if [[ "${EXPECTED_RULES}" != *"-d ${PROXY_IP}/32 --dport ${PROXY_PORT} -j ACCEPT"* ]] \
    || [[ "${EXPECTED_RULES}" != *"-d ${RELAY_IP}/32 --dport ${RELAY_PORT} -j ACCEPT"* ]]; then
    echo "firewall_verification_failed" >&2
    exit 1
  fi
  ACCEPT_COUNT="$(iptables -S OUTPUT | awk '$1=="-A" && $2=="OUTPUT" && $NF=="ACCEPT"{n++} END{print n+0}')"
  if [[ "${ACCEPT_COUNT}" -ne 4 ]]; then
    echo "firewall_unexpected_accept_rule" >&2
    exit 1
  fi
else
  LIVE_NFT="$(nft list ruleset)"
  if [[ "${LIVE_NFT}" != *"policy drop"* ]] \
    || [[ "${LIVE_NFT}" != *"ip daddr ${PROXY_IP} tcp dport ${PROXY_PORT} accept"* ]] \
    || [[ "${LIVE_NFT}" != *"ip daddr ${RELAY_IP} tcp dport ${RELAY_PORT} accept"* ]]; then
    echo "firewall_verification_failed" >&2
    exit 1
  fi
  NFT_ACCEPT_COUNT="$(printf '%s\n' "${LIVE_NFT}" | awk '/ accept$/{n++} END{print n+0}')"
  if [[ "${NFT_ACCEPT_COUNT}" -ne 4 ]]; then
    echo "firewall_unexpected_accept_rule" >&2
    exit 1
  fi
fi
