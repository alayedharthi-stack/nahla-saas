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

mapfile -t LLM_IPS < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("\n".join(c["llm_host_ips"]))' "${CONFIG_JSON}")
mapfile -t DB_IPS < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("\n".join(c["db_proxy_ips"]))' "${CONFIG_JSON}")
mapfile -t DNS_IPS < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("\n".join(c["dns_resolver_ips"]))' "${CONFIG_JSON}")
DB_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["db_proxy_port"])' "${CONFIG_JSON}")"

if command -v iptables >/dev/null 2>&1; then
  BACKEND="iptables"
  iptables -P OUTPUT DROP
  iptables -A OUTPUT -o lo -j ACCEPT
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  for ip in "${DNS_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    iptables -A OUTPUT -p udp -d "${ip}" --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp -d "${ip}" --dport 53 -j ACCEPT
  done
  for ip in "${LLM_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    iptables -A OUTPUT -p tcp -d "${ip}" --dport 443 -j ACCEPT
  done
  for ip in "${DB_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    iptables -A OUTPUT -p tcp -d "${ip}" --dport "${DB_PORT}" -j ACCEPT
  done
  iptables-save > "${RULES_OUT}"
  # Remove DNS egress after initial resolution and /etc/hosts pinning.
  for ip in "${DNS_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    iptables -D OUTPUT -p udp -d "${ip}" --dport 53 -j ACCEPT || true
    iptables -D OUTPUT -p tcp -d "${ip}" --dport 53 -j ACCEPT || true
  done
  iptables-save | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())' > "${RULES_OUT}.hash"
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
  for ip in "${DNS_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    echo "    ip daddr ${ip} udp dport 53 accept" >> "${NFT_FILE}"
    echo "    ip daddr ${ip} tcp dport 53 accept" >> "${NFT_FILE}"
  done
  for ip in "${LLM_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    echo "    ip daddr ${ip} tcp dport 443 accept" >> "${NFT_FILE}"
  done
  for ip in "${DB_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    echo "    ip daddr ${ip} tcp dport ${DB_PORT} accept" >> "${NFT_FILE}"
  done
  {
    echo "  }"
    echo "}"
  } >> "${NFT_FILE}"
  nft -f "${NFT_FILE}"
  # DNS egress removal for nft is represented by rebuilding without DNS rules.
  NFT_POST="/tmp/confined_e2e_post_dns.nft"
  cat > "${NFT_POST}" <<EOF
flush ruleset
table inet confined_e2e {
  chain output {
    type filter hook output priority 0; policy drop;
    oif "lo" accept
    ct state established,related accept
EOF
  for ip in "${LLM_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    echo "    ip daddr ${ip} tcp dport 443 accept" >> "${NFT_POST}"
  done
  for ip in "${DB_IPS[@]}"; do
    [[ -n "${ip}" ]] || continue
    echo "    ip daddr ${ip} tcp dport ${DB_PORT} accept" >> "${NFT_POST}"
  done
  {
    echo "  }"
    echo "}"
  } >> "${NFT_POST}"
  nft -f "${NFT_POST}"
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
  if ! iptables -S OUTPUT | grep -q -- '--dport 443'; then
    echo "firewall_verification_failed" >&2
    exit 1
  fi
else
  if ! nft list ruleset | grep -q 'policy drop'; then
    echo "firewall_verification_failed" >&2
    exit 1
  fi
fi
