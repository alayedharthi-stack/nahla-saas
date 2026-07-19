#!/usr/bin/env python3
"""TCP/TLS reachability probes that never send LLM or DB application payloads."""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
from typing import Any

from ops.internal_e2e_runner.lib.config import RunnerConfig, load_runner_config


def _tcp_probe(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "tcp_ok": False,
        "error": None,
    }
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["tcp_ok"] = True
    except OSError as exc:
        result["error"] = exc.__class__.__name__
    return result


def _tls_probe(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    result = _tcp_probe(host, port, timeout=timeout)
    if not result["tcp_ok"]:
        return result
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                cert = tls_sock.getpeercert()
                result["tls_ok"] = True
                result["tls_version"] = tls_sock.version()
                result["peer_subject"] = cert.get("subject")
    except OSError as exc:
        result["tls_ok"] = False
        result["error"] = exc.__class__.__name__
    return result


def run_probes(config: RunnerConfig) -> dict[str, Any]:
    positive: list[dict[str, Any]] = []
    for label, endpoint, probe in (
        ("llm", config.llm_endpoint, _tls_probe),
        ("db_proxy", config.db_proxy_endpoint, _tcp_probe),
    ):
        for ip in endpoint.ips:
            outcome = probe(ip, endpoint.port)
            outcome["label"] = label
            outcome["probe_kind"] = probe.__name__.strip("_")
            positive.append(outcome)

    negative: list[dict[str, Any]] = []
    for endpoint in config.negative_probe_targets:
        for ip in endpoint.ips:
            outcome = _tcp_probe(ip, endpoint.port)
            outcome["label"] = endpoint.hostname
            outcome["expected_blocked"] = True
            outcome["blocked_ok"] = outcome["tcp_ok"] is False
            negative.append(outcome)

    return {"positive": positive, "negative": negative}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = load_runner_config(args.config)
    results = run_probes(config)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    blocked_ok = all(item.get("blocked_ok") for item in results["negative"])
    allowed_ok = all(item.get("tcp_ok") for item in results["positive"])
    return 0 if blocked_ok and allowed_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
