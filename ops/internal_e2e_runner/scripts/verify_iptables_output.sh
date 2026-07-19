#!/usr/bin/env bash
# Semantic verification helpers for the confined runner iptables policy.

verify_iptables_output_rules() {
  local proxy_ip="$1"
  local proxy_port="$2"
  local relay_ip="$3"
  local relay_port="$4"
  local accept_count

  if ! iptables -S OUTPUT | grep -q '\-P OUTPUT DROP'; then
    echo "firewall_verification_failed" >&2
    return 1
  fi
  if ! iptables -C OUTPUT -p tcp -d "${proxy_ip}" --dport "${proxy_port}" -j ACCEPT \
    || ! iptables -C OUTPUT -p tcp -d "${relay_ip}" --dport "${relay_port}" -j ACCEPT; then
    echo "firewall_verification_failed" >&2
    return 1
  fi
  accept_count="$(iptables -S OUTPUT | awk '$1=="-A" && $2=="OUTPUT" && $NF=="ACCEPT"{n++} END{print n+0}')"
  if [[ "${accept_count}" -ne 4 ]]; then
    echo "firewall_unexpected_accept_rule" >&2
    return 1
  fi
}
