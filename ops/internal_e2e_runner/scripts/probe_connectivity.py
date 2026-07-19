#!/usr/bin/env python3
"""Sidecar-mediated probes; no LLM request, DB authentication, query, or write."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import socket
import ssl
import struct
from datetime import datetime, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

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
    *,
    relay_host: str,
    relay_port: int,
    tls_hostname: str,
    expected_spki_sha256: str,
    timeout: float = 5.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "postgres_sslrequest_tls",
        "target_hostname": tls_hostname,
        "sslrequest_accepted": False,
        "tls_ok": False,
        "certificate_pin_verified": False,
        "certificate_validity_verified": False,
        "identity_verification_mode": "spki_sha256",
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
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with context.wrap_socket(sock, server_hostname=tls_hostname) as tls_sock:
                result["tls_ok"] = True
                result["tls_version"] = tls_sock.version()
                peer_der = tls_sock.getpeercert(binary_form=True)
                if not peer_der:
                    result["error"] = "certificate_missing"
                    return result
                try:
                    certificate = x509.load_der_x509_certificate(peer_der)
                except ValueError:
                    result["error"] = "certificate_malformed"
                    return result

                not_before = certificate.not_valid_before_utc
                not_after = certificate.not_valid_after_utc
                observed_at = now or datetime.now(timezone.utc)
                if observed_at < not_before:
                    result["error"] = "certificate_not_yet_valid"
                    return result
                if observed_at > not_after:
                    result["error"] = "certificate_expired"
                    return result
                result["certificate_validity_verified"] = True

                spki_der = certificate.public_key().public_bytes(
                    Encoding.DER,
                    PublicFormat.SubjectPublicKeyInfo,
                )
                observed_pin = f"sha256:{hashlib.sha256(spki_der).hexdigest()}"
                result["certificate_spki_sha256"] = observed_pin
                if not hmac.compare_digest(observed_pin, expected_spki_sha256):
                    result["error"] = "certificate_spki_mismatch"
                    return result
                result["certificate_pin_verified"] = True
    except (OSError, ssl.SSLError) as exc:
        result["error"] = exc.__class__.__name__
    return result


def positive_probes_ready(probes: list[dict[str, Any]]) -> bool:
    by_kind = {str(probe.get("kind") or ""): probe for probe in probes}
    if set(by_kind) != {"https_connect_tls", "postgres_sslrequest_tls"}:
        return False
    llm = by_kind["https_connect_tls"]
    database = by_kind["postgres_sslrequest_tls"]
    return bool(
        llm.get("tls_ok")
        and llm.get("hostname_verified")
        and database.get("sslrequest_accepted")
        and database.get("tls_ok")
        and database.get("identity_verification_mode") == "spki_sha256"
        and database.get("certificate_pin_verified")
        and database.get("certificate_validity_verified")
        and database.get("authentication_sent") is False
        and database.get("query_sent") is False
    )


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
            expected_spki_sha256=config.db_tls_spki_sha256,
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
    allowed_ok = positive_probes_ready(results["positive"])
    return 0 if blocked_ok and allowed_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
