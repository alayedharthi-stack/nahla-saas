#!/usr/bin/env python3
"""Sidecar-mediated probes; no LLM request, DB authentication, query, or write."""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import struct
from typing import Any

from ops.internal_e2e_runner.lib.config import RunnerConfig, load_runner_config


def tcp_probe(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
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


def connect_tls_probe(
    *, proxy_host: str, proxy_port: int, target_host: str, timeout: float = 5.0
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "https_connect_tls",
        "target": f"{target_host}:443",
        "connect_accepted": False,
        "tls_ok": False,
    }
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as sock:
            request = (
                f"CONNECT {target_host}:443 HTTP/1.1\r\n"
                f"Host: {target_host}:443\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = sock.recv(4096)
            result["connect_status"] = response.split(b"\r\n", 1)[0].decode(
                "ascii", "replace"
            )
            if not response.startswith(b"HTTP/1.1 200"):
                return result
            result["connect_accepted"] = True
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=target_host) as tls_sock:
                result["tls_ok"] = True
                result["tls_version"] = tls_sock.version()
                result["hostname_verified"] = True
    except (OSError, ssl.SSLError) as exc:
        result["error"] = exc.__class__.__name__
    return result


def connect_rejection_probe(
    *, proxy_host: str, proxy_port: int, target_host: str, timeout: float = 5.0
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "https_connect_acl_rejection",
        "target": f"{target_host}:443",
        "rejected_ok": False,
    }
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=timeout) as sock:
            sock.sendall(
                (
                    f"CONNECT {target_host}:443 HTTP/1.1\r\n"
                    f"Host: {target_host}:443\r\n\r\n"
                ).encode("ascii")
            )
            response = sock.recv(4096)
            result["status"] = response.split(b"\r\n", 1)[0].decode(
                "ascii", "replace"
            )
            result["rejected_ok"] = response.startswith(b"HTTP/1.1 403")
    except OSError as exc:
        result["error"] = exc.__class__.__name__
    return result


def postgres_ssl_probe(
    *, relay_host: str, relay_port: int, tls_hostname: str, timeout: float = 5.0
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "postgres_sslrequest_tls",
        "target_hostname": tls_hostname,
        "sslrequest_accepted": False,
        "tls_ok": False,
        "authentication_sent": False,
        "query_sent": False,
    }
    try:
        with socket.create_connection((relay_host, relay_port), timeout=timeout) as sock:
            sock.sendall(struct.pack("!II", 8, 80877103))
            if sock.recv(1) != b"S":
                result["error"] = "postgres_ssl_not_supported"
                return result
            result["sslrequest_accepted"] = True
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=tls_hostname) as tls_sock:
                result["tls_ok"] = True
                result["tls_version"] = tls_sock.version()
                result["hostname_verified"] = True
    except (OSError, ssl.SSLError) as exc:
        result["error"] = exc.__class__.__name__
    return result


def run_probes(
    config: RunnerConfig,
    *,
    proxy_host: str,
    proxy_port: int,
    relay_host: str,
    relay_port: int,
    direct_controls: list[str],
) -> dict[str, Any]:
    positive = [
        connect_tls_probe(
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            target_host=config.llm_endpoint.hostname,
        ),
        postgres_ssl_probe(
            relay_host=relay_host,
            relay_port=relay_port,
            tls_hostname=config.db_proxy_endpoint.hostname,
        ),
    ]
    proxy_negative = [
        connect_rejection_probe(
            proxy_host=proxy_host, proxy_port=proxy_port, target_host=host
        )
        for host in ("graph.facebook.com", "api.salla.dev")
    ]
    direct_negative = []
    for target in direct_controls:
        label, ip, port_text = target.split("|", 2)
        outcome = tcp_probe(ip, int(port_text))
        outcome["kind"] = "runner_direct_network_block"
        outcome["blocked_ok"] = outcome["tcp_ok"] is False
        outcome["control_id"] = f"{label}|{ip}|{port_text}"
        direct_negative.append(outcome)
    return {
        "positive": positive,
        "proxy_negative": proxy_negative,
        "direct_negative": direct_negative,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--proxy-host", required=True)
    parser.add_argument("--proxy-port", type=int, default=3128)
    parser.add_argument("--relay-host", required=True)
    parser.add_argument("--relay-port", type=int, default=5432)
    parser.add_argument("--direct-control", action="append", required=True)
    args = parser.parse_args()
    config = load_runner_config(args.config)
    results = run_probes(
        config,
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
        relay_host=args.relay_host,
        relay_port=args.relay_port,
        direct_controls=args.direct_control,
    )
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    blocked_ok = all(
        item.get("rejected_ok") for item in results["proxy_negative"]
    ) and all(item.get("blocked_ok") for item in results["direct_negative"])
    allowed_ok = all(item.get("tls_ok") for item in results["positive"])
    return 0 if blocked_ok and allowed_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
