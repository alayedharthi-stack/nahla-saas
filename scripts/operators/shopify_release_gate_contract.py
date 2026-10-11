"""Closed contract for the Shopify release gate (read-only, phase-aware, externally pinned).

This is the normal-deployment compatibility gate for the accepted Shopify
source on the existing platform target. It replaces the legacy inline catalog
guard **for the new source only** and inherits none of that guard's mutation
path: it never upgrades, stamps, merges or edits ``alembic_version``. Every
state it accepts is one exact, named state, and every deviation is a refusal.

Trust root
──────────
The guard is executed by a short rendered command (see
``shopify_release_gate_render.py``) that the owner pastes into the service's
pre-deploy command together with the source pin. That command carries, as
literals, the release commit, the SHA-256 of the gate module itself, the full
inventory digest and count, the phase, the platform identity and the database
target. The binding is not circular: the gate module never carries its own
digest; the command pins the module, and the module verifies an inventory
whose digest is given to it by the command.

Two inventories
───────────────
* **runtime inventory** — the legacy guard's rule (``requirements.txt``,
  ``database/alembic.ini``, the pilot migration job and its contract, every
  ``database/**/*.py``) plus the gate, this contract, the schema expectations,
  the bootstrap contract and the three Shopify flag modules. The approval
  record binds to this digest, so filling the record cannot change it.
* **full inventory** — the runtime inventory plus the approval record. The
  rendered command pins this digest, so the record cannot change without a
  re-rendered command.

Phases
──────
One phase per exact ``alembic_version`` state of the deployed topology. The
gate accepts the phase named by the command and nothing else: a database one
step ahead or behind, or with a partial Shopify footprint, is refused and the
deploy stops. There is no blanket permission and no "any of S0..S3".
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

GATE_VERSION = 1
LOG_PREFIX = "[shopify-release-gate]"

PASS, FAIL = "PASS", "FAIL"
UNKNOWN = "<unknown>"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# ── Source binding ────────────────────────────────────────────────────────────
CONTAINER_ROOT = "/app"
# The one platform-injected variable this gate trusts for the deployed commit.
# The looser list used by staging attestation (GIT_COMMIT_SHA, COMMIT_SHA, …)
# is deliberately not accepted here: those are settable by an operator.
RELEASE_SHA_ENV = "RAILWAY_GIT_COMMIT_SHA"
RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

GATE_RELATIVE_PATH = "scripts/operators/shopify_release_gate.py"
CONTRACT_RELATIVE_PATH = "scripts/operators/shopify_release_gate_contract.py"
EXPECTATIONS_RELATIVE_PATH = "scripts/operators/shopify_release_schema_expectations.json"
APPROVAL_RELATIVE_PATH = "scripts/operators/shopify_release_approval.json"

# Legacy guard rule, reproduced exactly: these four files plus every
# ``database/**/*.py``; sorted relative paths; ``path NUL bytes NUL`` per file.
# Reproduces the legacy digest over exactly 132 files at the deployed commit
# (verified in the slice's evidence), so the new rule is a superset of it.
LEGACY_FIXED_FILES: Tuple[str, ...] = (
    "requirements.txt",
    "database/alembic.ini",
    "scripts/operators/commerce_runtime_pilot_migration.py",
    "scripts/operators/commerce_runtime_pilot_migration_contract.py",
)
LEGACY_GLOB_ROOT = "database"
LEGACY_GLOB_SUFFIX = ".py"
RUNTIME_EXTRA_FILES: Tuple[str, ...] = (
    GATE_RELATIVE_PATH,
    CONTRACT_RELATIVE_PATH,
    EXPECTATIONS_RELATIVE_PATH,
    "scripts/operators/bootstrap_migration_contract.py",
    "backend/services/shopify_connection/config.py",
    "backend/services/shopify_catalog/config.py",
    "backend/services/shopify_privacy/config.py",
)
FULL_EXTRA_FILES: Tuple[str, ...] = (APPROVAL_RELATIVE_PATH,)

# ── Library pins (the legacy guard's; identical to requirements.txt) ─────────
LIBRARY_PINS: Mapping[str, str] = {
    "alembic": "1.13.1",
    "sqlalchemy": "2.0.30",
    "psycopg2-binary": "2.9.9",
}

# ── Migration graph of the accepted source ──────────────────────────────────
# Every edge the gate requires in the script directory it runs from. A source
# whose graph differs (a missing revision, a re-parented one, an extra head) is
# refused before the database is touched.
EXPECTED_DOWN_REVISIONS: Mapping[str, str] = {
    "0110": "0109",
    "0111": "0109",
    "0112": "0110",
    "0113": "0112",
    "0114": "0113",
    "0115": "0112",
    "0118": "0112",
    "0120": "0118",
    "0121": "0120",
    "0122": "0121",
    "0123": "0122",
}
EXPECTED_REPOSITORY_HEADS: FrozenSet[str] = frozenset({"0092", "0111", "0114", "0115", "0123"})
# The branch point the catalog/Shopify branch leaves the address branch from.
BRANCH_FORK_REVISION = "0112"
RETAINED_ROWS: FrozenSet[str] = frozenset({"0111", "0113"})

# ── Phases: one exact alembic_version state each ────────────────────────────
FOUNDATION_TABLES: Tuple[str, ...] = (
    "shopify_connections", "shopify_shop_leases", "shopify_oauth_states",
    "shopify_webhook_events", "shopify_connection_audit",
)
CATALOG_TABLES: Tuple[str, ...] = (
    "shopify_catalog_import_runs", "shopify_catalog_staged_records", "shopify_catalog_products",
    "shopify_catalog_inventory_items", "shopify_catalog_variants", "shopify_catalog_locations",
    "shopify_catalog_inventory_levels",
)
PRIVACY_TABLES: Tuple[str, ...] = (
    "shopify_privacy_requests", "shopify_privacy_deliveries", "shopify_privacy_request_events",
)
# Added to shopify_connections by 0122 (and required by 0123); absent at S0/S1.
CONNECTION_KEY_CONSTRAINT = "uq_shopify_connections_id_tenant"
SHOPIFY_NAME_PREFIX = "shopify_"

PHASES: Mapping[str, Dict[str, Any]] = {
    "S0": {"rows": frozenset({"0111", "0113", "0120"}), "shopify_row": "0120",
           "tables": (), "connection_key": False, "branch_delta": ("0118", "0120")},
    "S1": {"rows": frozenset({"0111", "0113", "0121"}), "shopify_row": "0121",
           "tables": FOUNDATION_TABLES, "connection_key": False,
           "branch_delta": ("0118", "0120", "0121")},
    "S2": {"rows": frozenset({"0111", "0113", "0122"}), "shopify_row": "0122",
           "tables": FOUNDATION_TABLES + CATALOG_TABLES, "connection_key": True,
           "branch_delta": ("0118", "0120", "0121", "0122")},
    "S3": {"rows": frozenset({"0111", "0113", "0123"}), "shopify_row": "0123",
           "tables": FOUNDATION_TABLES + CATALOG_TABLES + PRIVACY_TABLES, "connection_key": True,
           "branch_delta": ("0118", "0120", "0121", "0122", "0123")},
}
# Phases whose approval needs database recovery evidence beyond a backup
# observation: these are the states reached by a schema change.
SCHEMA_CHANGING_PHASES: FrozenSet[str] = frozenset({"S1", "S2", "S3"})
# Documentation-only label for the deployed legacy release: it is **not** a
# gate phase. The legacy inline guard governs that release; this gate's file
# does not exist in that source and the command refuses it.
LEGACY_RELEASE_LABEL = "LEGACY_462_S0"
LEGACY_RELEASE_SHA = "462ede1aff602cad84e9569be859cca0087b190d"

# ── Platform identity of the existing target (non-secret metadata) ───────────
# Injected by the platform into the service; compared with the command's
# expectation. Nothing here is a credential.
IDENTITY_ENVS: Mapping[str, str] = {
    "project": "RAILWAY_PROJECT_ID",
    "environment": "RAILWAY_ENVIRONMENT_ID",
    "service": "RAILWAY_SERVICE_ID",
}
EXISTING_PLATFORM_TARGET: Mapping[str, str] = {
    "project": "f0090862-0a40-4293-bd5d-e94df58762b5",
    "environment": "ede962ce-3042-4dae-94de-623837e83ed9",
    "service": "686b36c5-a926-4e58-912a-5e9d13fbc2e7",
    "db": "postgres-ancu.railway.internal:5432/railway",
}
EXPECTED_SCHEMA = "public"

# ── Bounded timeouts (seconds; the whole run is wall-clock bounded) ─────────
CONNECT_TIMEOUT_SEC = 5
STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 3_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 20_000
WALL_TIMEOUT_SEC = 120
APPLICATION_NAME = "nahla-shopify-release-gate"
# Session-level exclusive advisory lock (two int4 keys: 'NAHL', 'SRLG'). A
# second gate, a migration operator or any holder of the same key makes this
# run refuse rather than wait.
ADVISORY_LOCK_KEYS: Tuple[int, int] = (0x4E41484C, 0x53524C47)

# ── Shopify flags: every one must be unset or false during the transition ───
SHOPIFY_FLAG_ENVS: Tuple[str, ...] = (
    "NAHLA_SHOPIFY_CONNECTION_ENABLED",
    "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED",
)
TRUTHY = frozenset({"1", "true", "yes", "on"})

# ── Retained schema protections (definitions, never names only) ─────────────
# Full canonical definitions (columns, every constraint, every index) are
# pinned for the six tables the retained 0111/0113/0118/0120 branches own.
RETAINED_FULL_DEFINITION_TABLES: Tuple[str, ...] = (
    "commerce_runtime_handover_barrier",
    "commerce_runtime_handover_workers",
    "commerce_runtime_deferred_inbound",
    "commerce_runtime_navigation_snapshots",
    "catalog_channel_retirements",
    "meta_catalog_authorizations",
)
# The WhatsApp OAuth nonce relation (0103) is pinned by definition with names
# abstracted: columns exactly, constraint definitions exactly, index shapes
# exactly. Its index *names* may legitimately differ on a database that
# adopted the table under the 0103 contract.
NONCE_TABLE = "whatsapp_oauth_nonces"
# Identity-level pins for the oldest relations.
PK_TYPE_PINS: Mapping[str, str] = {"tenants": "integer", "users": "integer"}
UNIQUE_DEFINITION_PINS: Tuple[Tuple[str, str], ...] = (
    ("commerce_runtime_conversations", "UNIQUE (id, tenant_id, namespace)"),
)
EXPECTATIONS_VERSION = 1

# ── Approval record ─────────────────────────────────────────────────────────
APPROVAL_RECORD_VERSION = 1
APPROVAL_PHASE_FIELDS: Tuple[str, ...] = (
    "approved", "reviewer", "independent_review_ref", "approved_at_utc", "valid_until_utc",
)
ROLLBACK_SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "railway_deployment_id", "source_sha", "verified_redeployable_at_utc", "verified_by",
)
BACKUP_FIELDS: Tuple[str, ...] = (
    "service_id", "volume_id", "card_timestamp_utc", "size_mb", "observed_by", "observed_at_utc",
)

# Closed result tokens (the only reasons ever printed).
R_ROOT_MISSING = "root_missing"
R_RELEASE_SHA_MISSING = "release_sha_missing"
R_RELEASE_SHA_MISMATCH = "release_sha_mismatch"
R_INVENTORY_FILE_MISSING = "inventory_file_missing"
R_INVENTORY_COUNT_MISMATCH = "inventory_count_mismatch"
R_INVENTORY_DIGEST_MISMATCH = "inventory_digest_mismatch"
R_LIBRARY_PIN_MISMATCH = "library_pin_mismatch"
R_GRAPH_UNLOADABLE = "graph_unloadable"
R_GRAPH_EDGE_MISMATCH = "graph_edge_mismatch"
R_GRAPH_HEADS_MISMATCH = "graph_heads_mismatch"
R_GRAPH_BRANCH_DELTA_MISMATCH = "graph_branch_delta_mismatch"
R_IDENTITY_MISSING = "identity_missing"
R_IDENTITY_MISMATCH = "identity_mismatch"
R_DB_TARGET_MISMATCH = "database_target_mismatch"
R_APPROVAL_UNREADABLE = "approval_record_unreadable"
R_APPROVAL_VERSION = "approval_record_version"
R_APPROVAL_NOT_BOUND = "approval_not_bound_to_this_source"
R_APPROVAL_MISSING = "approval_missing_for_phase"
R_APPROVAL_EXPIRED = "approval_expired"
R_APPROVAL_TIMESTAMP_INVALID = "approval_timestamp_invalid"
R_ROLLBACK_SNAPSHOT_MISSING = "deployment_rollback_snapshot_missing"
R_BACKUP_EVIDENCE_MISSING = "backup_evidence_missing"
R_BACKUP_AFTER_APPROVAL = "backup_observed_after_approval"
R_RESTORE_NOT_VERIFIED = "restore_not_verified"
R_FLAG_ENABLED = "shopify_flag_enabled"
R_CONNECT_FAILED = "connect_failed"
R_TIMEOUT = "statement_or_lock_timeout"
R_WALL_TIMEOUT = "wall_timeout"
R_LOCK_DENIED = "lock_denied"
R_UNLOCK_FAILED = "unlock_failed"
R_SERVER_IN_RECOVERY = "server_in_recovery"
R_SERVER_READ_ONLY = "database_default_read_only"
R_SCHEMA_NOT_PUBLIC = "current_schema_not_public"
R_DATABASE_NAME_MISMATCH = "database_name_mismatch"
R_VERSION_TABLE_MISSING = "alembic_version_table_missing"
R_VERSION_ROWS_DUPLICATE = "alembic_version_duplicate_rows"
R_VERSION_ROWS_UNKNOWN = "alembic_version_unknown_rows"
R_VERSION_ROWS_MISMATCH = "alembic_version_rows_mismatch"
R_FOOTPRINT_MISSING = "shopify_footprint_missing"
R_FOOTPRINT_UNEXPECTED = "shopify_footprint_unexpected"
R_FOOTPRINT_KEY = "shopify_connection_key_mismatch"
R_RETAINED_TABLE_MISSING = "retained_table_missing"
R_RETAINED_DEFINITION_DRIFT = "retained_definition_drift"
R_RETAINED_IDENTITY_DRIFT = "retained_identity_drift"
R_EXPECTATIONS_UNREADABLE = "schema_expectations_unreadable"
R_EXPECTATIONS_STALE = "schema_expectations_not_for_this_source"
R_INTERNAL_ERROR = "internal_error"


def phase(name: str) -> Optional[Dict[str, Any]]:
    """The phase definition, or None for anything that is not a gate phase."""
    return PHASES.get(str(name or ""))


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def parse_utc(value: object) -> Optional[_dt.datetime]:
    """``YYYY-MM-DDTHH:MM:SSZ`` (optionally with fractional seconds) -> aware UTC datetime; else None."""
    raw = str(value or "").strip()
    if not _UTC_RE.match(raw):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw[:-1])
    except ValueError:
        return None
    return parsed.replace(tzinfo=_dt.timezone.utc)


def parse_db_target(raw: str) -> Optional[Dict[str, Any]]:
    """``host[:port]/database`` (host lower-cased, database exact) or None when malformed."""
    text = str(raw or "")
    if text.count("/") != 1:
        return None
    authority, database = text.split("/", 1)
    if not authority or not database:
        return None
    host, port = authority, 5432
    if ":" in authority:
        host, _, raw_port = authority.rpartition(":")
        if not raw_port.isdigit():
            return None
        port = int(raw_port)
    if not host or host != host.strip():
        return None
    return {"host": host.lower(), "port": port, "database": database}


def evaluate_rows(rows: Optional[List[str]], expected: FrozenSet[str], known: FrozenSet[str]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Exact-multiset comparison. Only known revision ids are ever reported; the rest is ``<unknown>``."""
    if rows is None:
        return FAIL, R_VERSION_TABLE_MISSING, {"rows": None}
    known_rows = sorted(r for r in rows if r in known)
    unknown_count = len(rows) - len(known_rows)
    detail = {"rows": known_rows + [UNKNOWN] * unknown_count, "row_count": len(rows), "unknown_row_count": unknown_count}
    if len(set(known_rows)) != len(known_rows):
        return FAIL, R_VERSION_ROWS_DUPLICATE, detail
    if unknown_count:
        return FAIL, R_VERSION_ROWS_UNKNOWN, detail
    if set(known_rows) != set(expected) or len(known_rows) != len(expected):
        return FAIL, R_VERSION_ROWS_MISMATCH, detail
    return PASS, None, detail


def evaluate_footprint(observed: Mapping[str, Any], phase_def: Mapping[str, Any]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Exact Shopify footprint for a phase.

    ``observed`` carries: ``tables`` (relations named shopify_* with kind r/p),
    ``other_relations`` (shopify_* relations of any other kind except the
    sequences owned by expected tables and their indexes), ``types`` (shopify_*
    types that are not the row/array types of expected tables), ``functions``
    (shopify_* functions), ``foreign_constraints`` (constraints named *shopify*
    on tables outside the expected set) and ``connection_key`` (bool).
    """
    expected_tables = set(phase_def["tables"])
    tables = set(observed.get("tables") or ())
    missing = sorted(expected_tables - tables)
    unexpected = sorted(tables - expected_tables)
    stray = sorted(set(observed.get("other_relations") or ()) | set(observed.get("types") or ())
                   | set(observed.get("functions") or ()) | set(observed.get("foreign_constraints") or ()))
    detail = {"missing": missing, "unexpected": unexpected, "stray": stray,
              "connection_key": bool(observed.get("connection_key"))}
    if missing:
        return FAIL, R_FOOTPRINT_MISSING, detail
    if unexpected or stray:
        return FAIL, R_FOOTPRINT_UNEXPECTED, detail
    if bool(observed.get("connection_key")) != bool(phase_def["connection_key"]):
        return FAIL, R_FOOTPRINT_KEY, detail
    return PASS, None, detail


def evaluate_retained(observed: Mapping[str, Any], expectations: Mapping[str, Any]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Canonical definitions of the retained relations against the pinned expectations.

    Reports table names only, never definitions.
    """
    drift: List[str] = []
    missing: List[str] = []
    expected_tables = expectations.get("tables") or {}
    observed_tables = observed.get("tables") or {}
    for table, definition in expected_tables.items():
        actual = observed_tables.get(table)
        if actual is None:
            missing.append(table)
        elif _normalize(actual) != _normalize(definition):
            drift.append(table)
    nonce_expected = expectations.get("nonce") or {}
    nonce_actual = observed.get("nonce")
    if nonce_actual is None:
        missing.append(NONCE_TABLE)
    elif _normalize(nonce_actual) != _normalize(nonce_expected):
        drift.append(NONCE_TABLE)
    identity: List[str] = []
    for table, pk_type in (expectations.get("pk_types") or {}).items():
        if (observed.get("pk_types") or {}).get(table) != pk_type:
            identity.append(table)
    expected_uniques = {tuple(x) for x in (expectations.get("unique_definitions") or [])}
    observed_uniques = {tuple(x) for x in (observed.get("unique_definitions") or [])}
    for table, _definition in sorted(expected_uniques - observed_uniques):
        identity.append(table)
    expected_sequences = {tuple(x) for x in (expectations.get("sequences") or [])}
    observed_sequences = {tuple(x) for x in (observed.get("sequences") or [])}
    if expected_sequences != observed_sequences:
        identity.append("sequence_ownership")
    detail = {"missing": sorted(set(missing)), "drift": sorted(set(drift)), "identity": sorted(set(identity))}
    if missing:
        return FAIL, R_RETAINED_TABLE_MISSING, detail
    if drift:
        return FAIL, R_RETAINED_DEFINITION_DRIFT, detail
    if identity:
        return FAIL, R_RETAINED_IDENTITY_DRIFT, detail
    return PASS, None, detail


def _normalize(value: Any) -> Any:
    """JSON round-trip shape: tuples become lists so file and live forms compare equal."""
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    return value


def evaluate_flags(environ: Mapping[str, str]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    enabled = sorted(name for name in SHOPIFY_FLAG_ENVS if truthy(environ.get(name)))
    if enabled:
        return FAIL, R_FLAG_ENABLED, {"enabled": enabled}
    return PASS, None, {"enabled": []}


def evaluate_identity(environ: Mapping[str, str], expected: Mapping[str, str]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Platform identity: every expected id must be present and equal. Actual values are never printed."""
    missing = sorted(k for k, env_name in IDENTITY_ENVS.items() if not str(environ.get(env_name) or "").strip())
    if missing:
        return FAIL, R_IDENTITY_MISSING, {"missing": missing}
    mismatched = sorted(k for k, env_name in IDENTITY_ENVS.items()
                        if str(environ.get(env_name) or "").strip() != str(expected.get(k) or ""))
    if mismatched:
        return FAIL, R_IDENTITY_MISMATCH, {"mismatched": mismatched}
    return PASS, None, {"expected": dict(expected)}


def _all_present(mapping: Any, fields: Tuple[str, ...]) -> bool:
    if not isinstance(mapping, dict):
        return False
    for name in fields:
        value = mapping.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def evaluate_approval(record: Any, phase_name: str, *, runtime_digest: str, runtime_count: int,
                      now: _dt.datetime) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """The owner's per-phase operational approval, bound to the runtime inventory it was given for.

    Fail-closed: an unfilled record, a record for another source, an expired or
    unapproved phase, a missing deployment rollback snapshot, missing backup
    evidence, backup evidence observed after the approval, or (for a
    schema-changing phase) an unverified restore each refuse the deploy.
    """
    detail: Dict[str, Any] = {"phase": phase_name}
    if not isinstance(record, dict):
        return FAIL, R_APPROVAL_UNREADABLE, detail
    if record.get("record_version") != APPROVAL_RECORD_VERSION:
        return FAIL, R_APPROVAL_VERSION, detail
    if record.get("bound_runtime_inventory_digest") != runtime_digest or record.get("bound_runtime_inventory_count") != runtime_count:
        return FAIL, R_APPROVAL_NOT_BOUND, detail
    entry = (record.get("phase_approvals") or {}).get(phase_name)
    if not _all_present(entry, APPROVAL_PHASE_FIELDS) or entry.get("approved") is not True:
        return FAIL, R_APPROVAL_MISSING, detail
    approved_at = parse_utc(entry.get("approved_at_utc"))
    valid_until = parse_utc(entry.get("valid_until_utc"))
    if approved_at is None or valid_until is None or valid_until <= approved_at:
        return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
    if now < approved_at or now > valid_until:
        return FAIL, R_APPROVAL_EXPIRED, detail
    if not _all_present(record.get("deployment_rollback_snapshot"), ROLLBACK_SNAPSHOT_FIELDS):
        return FAIL, R_ROLLBACK_SNAPSHOT_MISSING, detail
    recovery = record.get("db_recovery_evidence") or {}
    backup = recovery.get("backup") if isinstance(recovery, dict) else None
    if not _all_present(backup, BACKUP_FIELDS):
        return FAIL, R_BACKUP_EVIDENCE_MISSING, detail
    card_time = parse_utc(backup.get("card_timestamp_utc"))
    if card_time is None:
        return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
    if card_time > approved_at:
        return FAIL, R_BACKUP_AFTER_APPROVAL, detail
    if phase_name in SCHEMA_CHANGING_PHASES:
        if recovery.get("restore_verified") is not True or not _all_present(recovery, ("restore_verification_ref", "restore_verified_at_utc")):
            return FAIL, R_RESTORE_NOT_VERIFIED, detail
    return PASS, None, detail


__all__ = [name for name in globals() if not name.startswith("_")]
