"""Fail-closed configuration validation for the confined internal E2E runner."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


RUNNER_CONFIG_SCHEMA_VERSION = "internal_e2e_confined_runner_config_v1"
EVIDENCE_SCHEMA_VERSION = "internal_e2e_network_evidence_v1"

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_IMAGE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,127}$", re.IGNORECASE)
_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3})$"
)

_REJECTED_DB_HOST_SUBSTRINGS = (
    ".railway.internal",
    "postgres-staging",
    "postgres.railway",
    "canonical",
    "shared-staging",
)
_REJECTED_PROVIDER_HOSTS = frozenset(
    {
        "graph.facebook.com",
        "graph.instagram.com",
        "api.whatsapp.com",
        "api.salla.dev",
        "api.salla.sa",
        "api.zid.sa",
        "webhook.site",
    }
)
_PRIVATE_OR_RESERVED = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)


@dataclass(frozen=True)
class ResolvedEndpoint:
    hostname: str
    port: int
    ips: tuple[str, ...]


@dataclass(frozen=True)
class RunnerConfig:
    schema_version: str
    pinned_revision: str
    image_label: str
    llm_endpoint: ResolvedEndpoint
    db_proxy_endpoint: ResolvedEndpoint
    negative_probe_targets: tuple[ResolvedEndpoint, ...]
    tenant_id: int
    provider: str
    connect_proxy_ip: str
    connect_proxy_port: int
    db_relay_ip: str
    db_relay_port: int

    def to_public_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pinned_revision": self.pinned_revision,
            "image_label": self.image_label,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "connect_proxy_ip": self.connect_proxy_ip,
            "connect_proxy_port": self.connect_proxy_port,
            "db_relay_ip": self.db_relay_ip,
            "db_relay_port": self.db_relay_port,
            "llm_host": self.llm_endpoint.hostname,
            "llm_port": self.llm_endpoint.port,
            "llm_ips": list(self.llm_endpoint.ips),
            "db_proxy_host": self.db_proxy_endpoint.hostname,
            "db_proxy_port": self.db_proxy_endpoint.port,
            "db_proxy_ips": list(self.db_proxy_endpoint.ips),
            "negative_probe_targets": [
                {
                    "host": target.hostname,
                    "port": target.port,
                    "ips": list(target.ips),
                }
                for target in self.negative_probe_targets
            ],
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_ips(raw: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        tokens = [token.strip() for token in raw.split(",") if token.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        tokens = [str(token).strip() for token in raw if str(token).strip()]
    else:
        raise ValueError(f"{field}_invalid")
    if not tokens:
        raise ValueError(f"{field}_required")
    ips: list[str] = []
    for token in tokens:
        if "/" in token or "*" in token or token == "0.0.0.0":
            raise ValueError(f"{field}_wildcard_or_cidr_rejected")
        if not _IPV4_RE.fullmatch(token):
            raise ValueError(f"{field}_invalid_ipv4")
        addr = ipaddress.ip_address(token)
        if any(addr in network for network in _PRIVATE_OR_RESERVED):
            raise ValueError(f"{field}_private_or_reserved_rejected")
        ips.append(str(addr))
    return tuple(sorted(set(ips)))


def _validate_hostname(hostname: str, *, field: str, allow_provider_host: bool = False) -> str:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host or not _HOST_RE.fullmatch(host):
        raise ValueError(f"{field}_invalid")
    if any(token in host for token in _REJECTED_DB_HOST_SUBSTRINGS):
        raise ValueError(f"{field}_canonical_or_private_rejected")
    if not allow_provider_host and host in _REJECTED_PROVIDER_HOSTS:
        raise ValueError(f"{field}_provider_host_rejected")
    try:
        ipaddress.ip_address(host)
        raise ValueError(f"{field}_literal_ip_rejected")
    except ValueError as exc:
        if "does not appear to be an IPv4 or IPv6 address" not in str(exc):
            raise
    return host


def _endpoint_from_mapping(
    raw: Mapping[str, Any],
    *,
    field: str,
    default_port: int,
    allow_provider_host: bool = False,
) -> ResolvedEndpoint:
    host = _validate_hostname(
        str(raw.get("host") or raw.get("hostname") or ""),
        field=field,
        allow_provider_host=allow_provider_host,
    )
    port = int(raw.get("port") or default_port)
    if port <= 0 or port > 65535:
        raise ValueError(f"{field}_port_invalid")
    ips = _parse_ips(raw.get("ips") or raw.get("ip_addresses"), field=f"{field}_ips")
    return ResolvedEndpoint(hostname=host, port=port, ips=ips)


def validate_database_url_requirements(database_url: str) -> list[str]:
    """Validate disposable DB URL requirements without returning credentials."""
    blockers: list[str] = []
    raw = str(database_url or "").strip()
    if not raw:
        return ["database_url_missing"]
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ["database_url_invalid"]
    if parsed.scheme not in {"postgresql", "postgres"}:
        blockers.append("database_url_scheme_invalid")
    host = str(parsed.hostname or "").strip().lower()
    try:
        _validate_hostname(host, field="database_url_host")
    except ValueError as exc:
        blockers.append(str(exc))
    query = parse_qs(parsed.query, keep_blank_values=True)
    sslmode = str((query.get("sslmode") or [""])[0]).strip().lower()
    if sslmode != "require":
        blockers.append("database_url_sslmode_require_missing")
    return blockers


def database_url_fingerprint(database_url: str) -> str:
    """Return a non-reversible host/port/sslmode fingerprint for evidence."""
    import hashlib

    parsed = urlparse(str(database_url or "").strip())
    query = parse_qs(parsed.query, keep_blank_values=True)
    safe = {
        "host": str(parsed.hostname or "").lower(),
        "port": str(parsed.port or "5432"),
        "scheme": str(parsed.scheme or ""),
        "sslmode": str((query.get("sslmode") or [""])[0]).lower(),
    }
    digest = hashlib.sha256(_canonical(safe).encode()).hexdigest()
    return f"sha256:{digest}"


def parse_runner_config(raw: Mapping[str, Any]) -> RunnerConfig:
    schema = str(raw.get("schema_version") or "")
    if schema != RUNNER_CONFIG_SCHEMA_VERSION:
        raise ValueError("runner_config_schema_invalid")

    revision = str(raw.get("pinned_revision") or "").strip().lower()
    if not _REVISION_RE.fullmatch(revision):
        raise ValueError("pinned_revision_invalid")

    image_label = str(raw.get("image_label") or "").strip()
    if not _IMAGE_LABEL_RE.fullmatch(image_label):
        raise ValueError("image_label_invalid")

    tenant_raw = raw.get("tenant_id")
    if type(tenant_raw) is not int or tenant_raw <= 0 or tenant_raw == 1:
        raise ValueError("tenant_id_invalid")

    provider = str(raw.get("provider") or "").strip().lower()
    if provider != "anthropic":
        raise ValueError("exactly_one_supported_provider_required")

    connect_proxy_ip = str(raw.get("connect_proxy_ip") or "")
    db_relay_ip = str(raw.get("db_relay_ip") or "")
    try:
        proxy_addr = ipaddress.ip_address(connect_proxy_ip)
        relay_addr = ipaddress.ip_address(db_relay_ip)
    except ValueError as exc:
        raise ValueError("sidecar_ip_invalid") from exc
    if not proxy_addr.is_private or not relay_addr.is_private or proxy_addr == relay_addr:
        raise ValueError("sidecar_ips_must_be_distinct_private_addresses")
    connect_proxy_port = int(raw.get("connect_proxy_port") or 3128)
    db_relay_port = int(raw.get("db_relay_port") or 5432)
    if connect_proxy_port != 3128 or db_relay_port != 5432:
        raise ValueError("sidecar_port_invalid")

    llm = _endpoint_from_mapping(
        {
            "host": raw.get("llm_host"),
            "port": raw.get("llm_port") or 443,
            "ips": raw.get("llm_host_ips") or raw.get("llm_ips"),
        },
        field="llm_host",
        default_port=443,
    )
    if llm.port != 443:
        raise ValueError("llm_port_must_be_443")

    db_proxy = _endpoint_from_mapping(
        {
            "host": raw.get("db_proxy_host"),
            "port": raw.get("db_proxy_port") or 5432,
            "ips": raw.get("db_proxy_ips"),
        },
        field="db_proxy_host",
        default_port=5432,
    )

    negative_raw = raw.get("negative_probe_targets") or []
    if not isinstance(negative_raw, Sequence) or isinstance(negative_raw, (str, bytes)):
        raise ValueError("negative_probe_targets_invalid")
    if len(negative_raw) < 2:
        raise ValueError("negative_probe_targets_minimum_two")
    negative_targets: list[ResolvedEndpoint] = []
    for index, item in enumerate(negative_raw):
        if not isinstance(item, Mapping):
            raise ValueError("negative_probe_target_invalid")
        negative_targets.append(
            _endpoint_from_mapping(
                item,
                field=f"negative_probe_{index}",
                default_port=443,
                allow_provider_host=True,
            )
        )

    allowed_hosts = {llm.hostname, db_proxy.hostname}
    for target in negative_targets:
        if target.hostname in allowed_hosts:
            raise ValueError("negative_probe_target_overlaps_allowlist")

    return RunnerConfig(
        schema_version=schema,
        pinned_revision=revision,
        image_label=image_label,
        llm_endpoint=llm,
        db_proxy_endpoint=db_proxy,
        negative_probe_targets=tuple(negative_targets),
        tenant_id=tenant_raw,
        provider=provider,
        connect_proxy_ip=connect_proxy_ip,
        connect_proxy_port=connect_proxy_port,
        db_relay_ip=db_relay_ip,
        db_relay_port=db_relay_port,
    )


def load_runner_config(path: str) -> RunnerConfig:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ValueError("runner_config_invalid")
    return parse_runner_config(raw)


def validate_runner_config_blockers(
    raw: Mapping[str, Any],
    *,
    database_url: str | None = None,
) -> list[str]:
    blockers: list[str] = []
    try:
        config = parse_runner_config(raw)
    except ValueError as exc:
        return [str(exc)]
    if config.llm_endpoint.hostname == config.db_proxy_endpoint.hostname:
        blockers.append("llm_and_db_proxy_host_must_differ")
    if database_url is not None:
        blockers.extend(validate_database_url_requirements(database_url))
        try:
            parsed = urlparse(database_url)
            if str(parsed.hostname or "").lower() != config.db_proxy_endpoint.hostname:
                blockers.append("database_url_host_mismatch")
        except ValueError:
            blockers.append("database_url_invalid")
    return blockers


def default_operator_command() -> list[str]:
    return ["preflight"]


def normalize_operator_command(argv: Sequence[str] | None) -> list[str]:
    if not argv:
        return default_operator_command()
    command = [str(token) for token in argv]
    if command == ["preflight"]:
        return command
    if (
        len(command) == 3
        and command[0] == "run"
        and command[1] == "--scenarios"
        and command[2]
    ):
        return command
    raise ValueError("operator_command_invalid")


SECRET_REDACTION_PATTERNS = (
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)postgresql://[^@\s]+@[^\s]+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\b\d{10,15}\b"),
)


def redact_secrets(text: str) -> str:
    redacted = str(text or "")
    for pattern in SECRET_REDACTION_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
