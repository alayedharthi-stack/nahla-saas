"""Closed contract for the Shopify release gate (read-only, phase-aware, externally pinned).

This is the normal-deployment compatibility gate for the accepted Shopify
source on the existing platform target. It replaces the legacy inline catalog
guard **for the new source only** and inherits none of that guard's mutation
path: it never upgrades, stamps, merges or edits ``alembic_version``. Every
state it accepts is one exact, named state, and every deviation is a refusal.

Trust order
───────────
The owner's pre-deploy command is a **standard-library-only bootstrap**
rendered by ``shopify_release_gate_render.py``. Before any repository module is
imported it verifies, from literals it carries: the container root, the
deployed commit (``RAILWAY_GIT_COMMIT_SHA``), the platform identity, the
absence of inherited libpq target variables, the complete byte inventory
(count and digest, computed by the bootstrap itself), the absence of package
initializers that must not exist, the gate module's own digest and the
SHA-256 of the external approval payload. Only then does it ``exec`` the gate,
which re-verifies everything fail-fast and performs the database checks.

Bindings (none circular)
────────────────────────
* The rendered command pins the inventory digest; the inventory includes the
  gate and this contract, and neither stores its own digest.
* The approval record is **external** to the source (an owner-held payload
  delivered through one environment variable and pinned by SHA-256 in the
  command). It binds to the exact deployed commit, the inventory digest and
  count, the platform target, the backup identity and the phase, so a commit
  never has to name itself.

Phases
──────
Normal deployment is allowed in exactly two states: S0 (the deployed
topology before any Shopify revision) and S3 (the foundation, catalog and
privacy revisions through 0123 applied). Source-only install-first revision
0124 is not an authorized database state. S1 and S2 are intermediate states of
the separately approved manual migration window; they are named here so tests can prove they are refused,
and they are never a deployment phase.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

GATE_VERSION = 2
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
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
TOKEN_RE = re.compile(r"^[a-z0-9_]{1,64}$")

GATE_RELATIVE_PATH = "scripts/operators/shopify_release_gate.py"
CONTRACT_RELATIVE_PATH = "scripts/operators/shopify_release_gate_contract.py"
EXPECTATIONS_RELATIVE_PATH = "scripts/operators/shopify_release_schema_expectations.json"
APPROVAL_TEMPLATE_RELATIVE_PATH = "scripts/operators/shopify_release_approval.template.json"

# Legacy guard rule, reproduced exactly: these four files plus every
# ``database/**/*.py``; sorted relative paths; ``path NUL bytes NUL`` per file.
# It reproduces the legacy digest over exactly 132 files at the deployed
# commit, so the gate's rule is a strict superset of the legacy one.
LEGACY_FIXED_FILES: Tuple[str, ...] = (
    "requirements.txt",
    "database/alembic.ini",
    "scripts/operators/commerce_runtime_pilot_migration.py",
    "scripts/operators/commerce_runtime_pilot_migration_contract.py",
)
LEGACY_GLOB_ROOT = "database"
LEGACY_GLOB_SUFFIX = ".py"
# Everything the gate itself imports or reads from the repository, plus the
# modules whose flag names and defaults it relies on. Package initializers on
# the gate's import path are pinned (``scripts/operators/__init__.py``; the
# ``database`` ones are covered by the glob).
RUNTIME_EXTRA_FILES: Tuple[str, ...] = (
    GATE_RELATIVE_PATH,
    CONTRACT_RELATIVE_PATH,
    EXPECTATIONS_RELATIVE_PATH,
    "scripts/operators/__init__.py",
    "scripts/operators/bootstrap_migration_contract.py",
    # Graph discovery imports dormant revision 0115 even when it is not applied.
    # Pin its exact package/model closure; no other backend imports are allowed.
    "backend/__init__.py",
    "backend/payments/__init__.py",
    "backend/payments/models.py",
    "backend/services/shopify_connection/config.py",
    "backend/services/shopify_connection/install_config.py",
    "backend/services/shopify_connection/install_admission.py",
    "backend/services/shopify_connection/shop_domain.py",
    "backend/services/shopify_connection/install.py",
    "backend/services/shopify_connection/install_models.py",
    "backend/services/shopify_connection/install_cleanup.py",
    "backend/routers/shopify_connection.py",
    "backend/routers/shopify_install.py",
    "scripts/operators/shopify_install_schema_expectations.json",
    "scripts/operators/shopify_install_rollback.draft.json",
    "scripts/operators/salla_comm_0125_schema_expectations.json",
    "scripts/operators/salla_comm_0125_rollback.draft.json",
    "backend/services/shopify_catalog/config.py",
    "backend/services/shopify_privacy/config.py",
)
# Initializers that do not exist in the accepted source and must not appear:
# ``scripts`` is a namespace package, and a planted ``scripts/__init__.py``
# would execute on the gate's first import.
MUST_BE_ABSENT: Tuple[str, ...] = ("scripts/__init__.py",)

# ── Library pins (the legacy guard's; identical to requirements.txt) ─────────
LIBRARY_PINS: Mapping[str, str] = {
    "alembic": "1.13.1",
    "sqlalchemy": "2.0.30",
    "psycopg2-binary": "2.9.9",
}

# ── Migration graph of the accepted source ──────────────────────────────────
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
    "0124": "0123",
    "0125": "0124",
}
EXPECTED_REPOSITORY_HEADS: FrozenSet[str] = frozenset({"0092", "0111", "0114", "0115", "0125"})
BRANCH_FORK_REVISION = "0112"
RETAINED_ROWS: FrozenSet[str] = frozenset({"0111", "0113"})

# ── States ──────────────────────────────────────────────────────────────────
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
SHOPIFY_TABLES: Tuple[str, ...] = FOUNDATION_TABLES + CATALOG_TABLES + PRIVACY_TABLES
CONNECTION_KEY_CONSTRAINT = "uq_shopify_connections_id_tenant"
SHOPIFY_NAME_PREFIX = "shopify_"
SYSTEM_SCHEMAS: Tuple[str, ...] = ("pg_catalog", "information_schema")

# Deployment phases: exactly two. One exact ``alembic_version`` state each.
DEPLOY_PHASES: Mapping[str, Dict[str, Any]] = {
    "S0": {"rows": frozenset({"0111", "0113", "0120"}), "shopify_row": "0120",
           "tables": (), "branch_delta": ("0118", "0120")},
    "S3": {"rows": frozenset({"0111", "0113", "0123"}), "shopify_row": "0123",
           "tables": SHOPIFY_TABLES, "branch_delta": ("0118", "0120", "0121", "0122", "0123")},
}
# Explicit post-0124 contracts. S0/S3 and their legacy footprint stay closed.
# The dormant phase is also the application rollback: retain all 0124 evidence
# and turn flags OFF, never downgrade the ownership/flow tables to 0123.
INSTALL_TABLES: Tuple[str, ...] = SHOPIFY_TABLES + ("shopify_owner_identities", "shopify_install_flows")
POST_INSTALL_PHASES: Mapping[str, Dict[str, Any]] = {
    name: {"rows": frozenset({"0111", "0113", "0124"}), "shopify_row": "0124",
           "tables": INSTALL_TABLES, "branch_delta": ("0118", "0120", "0121", "0122", "0123", "0124"),
           "expectations_key": "shopify_s4"}
    for name in ("S4_DORMANT", "S4_PILOT")
}
POST_INSTALL_PHASES = dict(POST_INSTALL_PHASES, S4_COMM_DORMANT={
    "rows": frozenset({"0111", "0113", "0125"}), "shopify_row": "0125",
    "tables": INSTALL_TABLES,
    "branch_delta": ("0118", "0120", "0121", "0122", "0123", "0124", "0125"),
    "expectations_key": "shopify_s4", "communication_schema_required": True,
})
INSTALL_EXPECTATIONS_RELATIVE_PATH = "scripts/operators/shopify_install_schema_expectations.json"
INSTALL_ROLLBACK_RELATIVE_PATH = "scripts/operators/shopify_install_rollback.draft.json"
# The 0334 rollback deployment carried these exact source and expectations bytes.
# A later release can add migration 0125 without rewriting this historical pin.
PRIOR_INSTALL_DATABASE_PY_DIGEST = "14b14130c43f0a590589ac02ed4e5533f97eeb29b7d3e7915d393c2b017dbb09"
PRIOR_INSTALL_EXPECTATIONS_SHA256 = "68082c651efe1f0a86de05512f701d867545b655122938967c5ddbbf8f9e8ebf"
COMM_EXPECTATIONS_RELATIVE_PATH = "scripts/operators/salla_comm_0125_schema_expectations.json"
COMM_ROLLBACK_RELATIVE_PATH = "scripts/operators/salla_comm_0125_rollback.draft.json"
ROLLBACK_MAX_AGE_HOURS = 24
PILOT_SHOP = "nahlah-dev.myshopify.com"
# Intermediate states of the manual migration window. Never a deployment
# phase: the gate refuses them as an argument and refuses a database in them
# under either deployment phase.
INTERMEDIATE_STATES: Mapping[str, FrozenSet[str]] = {
    "S1": frozenset({"0111", "0113", "0121"}),
    "S2": frozenset({"0111", "0113", "0122"}),
}
SCHEMA_CHANGING_PHASES: FrozenSet[str] = frozenset({"S3", "S4_DORMANT", "S4_PILOT", "S4_COMM_DORMANT"})
LEGACY_RELEASE_LABEL = "LEGACY_462_S0"
LEGACY_RELEASE_SHA = "462ede1aff602cad84e9569be859cca0087b190d"

# ── Platform identity of the existing target (non-secret metadata) ───────────
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
    "backup_service": "b77b3d27-47b0-4a3a-83fd-44def66a3a84",
    "backup_volume": "009bd0d5-85ed-4de4-99fc-94ea963c9d65",
}
EXPECTED_SCHEMA = "public"
# The legacy guard's pinned session search path, restored deliberately and
# verified on the connection.
SEARCH_PATH = "public,pg_catalog"
# libpq variables that move a connection's target; refused when present (the
# pilot job's own list, duplicated here so the stdlib bootstrap can refuse
# them before any import).
LIBPQ_TARGET_ENV_VARS: Tuple[str, ...] = (
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE",
    "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGPASSFILE", "PGCONNECT_TIMEOUT",
)

# ── Bounded timeouts (seconds / milliseconds; the whole run is wall-clock bounded) ──
CONNECT_TIMEOUT_SEC = 5
STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 3_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 20_000
WALL_TIMEOUT_SEC = 120
APPLICATION_NAME = "nahla-shopify-release-gate"
# The legacy guard's exclusive advisory key, preserved exactly so this gate
# contends with it and with any operator that serializes on it:
# ``sha256(b"nahla:primary:catalog-schema:0120")[:8]`` read as a big-endian
# signed 64-bit integer (``struct.unpack(">q", ...)``).
ADVISORY_LOCK_KEY_SEED = b"nahla:primary:catalog-schema:0120"
ADVISORY_LOCK_KEY = 8182734029736770447

# ── Shopify flags: every one must be unset or false during the transition ───
# The two VITE_ names are the dashboard's build-time flags. They are refused
# here if they appear in the checked (API service) environment; the API gate
# cannot observe another service's compiled Vite configuration, which needs
# separate owner evidence.
SHOPIFY_FLAG_ENVS: Tuple[str, ...] = (
    "NAHLA_SHOPIFY_CONNECTION_ENABLED",
    "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED",
    "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED",
    "NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED",
    "VITE_SHOPIFY_CONNECTION_UI",
    "VITE_SHOPIFY_INSTALL_FIRST_ENABLED",
    "VITE_SHOPIFY_CATALOG_IMPORT_UI",
)
COMM_SEND_FLAG_ENV = "SALLA_COMM_WHATSAPP_ENABLED"
TRUTHY = frozenset({"1", "true", "yes", "on"})
# S4_PILOT may additionally enable the read-only catalog import, and only
# with the runtime shop bound (backend/services/shopify_catalog/config.py,
# ``IMPORT_SHOPS_ENV``) naming exactly PILOT_SHOP with a deadline at most
# seven days ahead. Every other flag keeps its S4_PILOT rule.
CATALOG_IMPORT_FLAG = "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED"
CATALOG_IMPORT_SHOPS_ENV = "NAHLA_SHOPIFY_CATALOG_IMPORT_SHOPS"
CATALOG_IMPORT_MAX_DAYS = 7

# ── Retained schema protections (definitions, never names only) ─────────────
RETAINED_FULL_DEFINITION_TABLES: Tuple[str, ...] = (
    "commerce_runtime_handover_barrier",
    "commerce_runtime_handover_workers",
    "commerce_runtime_deferred_inbound",
    "commerce_runtime_navigation_snapshots",
    "catalog_channel_retirements",
    "meta_catalog_authorizations",
)
# The WhatsApp OAuth nonce relation (0103): full definition — every column
# with type, nullability, default, identity and generation; every constraint
# with its definition and deferrable/deferred/validated flags; every index
# with its shape and validity flags — with only constraint and index *names*
# abstracted, because a database that adopted the table under the 0103
# contract may legitimately carry different names.
NONCE_TABLE = "whatsapp_oauth_nonces"
PK_TYPE_PINS: Mapping[str, str] = {"tenants": "integer", "users": "integer"}
UNIQUE_DEFINITION_PINS: Tuple[Tuple[str, str], ...] = (
    ("commerce_runtime_conversations", "UNIQUE (id, tenant_id, namespace)"),
)
IDENTITY_TABLES: Tuple[str, ...] = RETAINED_FULL_DEFINITION_TABLES + (NONCE_TABLE, "tenants", "users", "commerce_runtime_conversations")
EXPECTATIONS_VERSION = 3

# ── Approval record (external payload) ──────────────────────────────────────
APPROVAL_RECORD_VERSION = 2
APPROVAL_PAYLOAD_ENV = "NAHLA_SHOPIFY_RELEASE_APPROVAL_B64"
# A backup observation older than this at approval time or at run time is
# refused: the owner must take a fresh backup before a later deploy.
BACKUP_MAX_AGE_HOURS = 72
BACKUP_MAX_SIZE_MB = 10_000_000
APPROVAL_FIELDS: Tuple[str, ...] = ("approved", "reviewer", "independent_review_ref", "approved_at_utc", "valid_until_utc")
ROLLBACK_SNAPSHOT_FIELDS: Tuple[str, ...] = ("railway_deployment_id", "source_sha", "verified_redeployable_at_utc", "verified_by")
BACKUP_FIELDS: Tuple[str, ...] = ("service_id", "volume_id", "card_timestamp_utc", "size_mb", "completed", "restore_available",
                                  "observed_by", "observed_at_utc")

# Closed result tokens (the only reasons ever printed).
R_ROOT_MISSING = "root_missing"
R_PHASE_NOT_DEPLOYABLE = "phase_not_deployable"
R_RELEASE_SHA_MISSING = "release_sha_missing"
R_RELEASE_SHA_MISMATCH = "release_sha_mismatch"
R_INVENTORY_FILE_MISSING = "inventory_file_missing"
R_INVENTORY_COUNT_MISMATCH = "inventory_count_mismatch"
R_INVENTORY_DIGEST_MISMATCH = "inventory_digest_mismatch"
R_INITIALIZER_UNEXPECTED = "initializer_unexpected"
R_GATE_DIGEST_MISMATCH = "gate_digest_mismatch"
R_LIBRARY_PIN_MISMATCH = "library_pin_mismatch"
R_GRAPH_UNLOADABLE = "graph_unloadable"
R_GRAPH_EDGE_MISMATCH = "graph_edge_mismatch"
R_GRAPH_HEADS_MISMATCH = "graph_heads_mismatch"
R_GRAPH_BRANCH_DELTA_MISMATCH = "graph_branch_delta_mismatch"
R_IDENTITY_MISSING = "identity_missing"
R_IDENTITY_MISMATCH = "identity_mismatch"
R_LIBPQ_OVERRIDE = "libpq_environment_override"
R_DB_TARGET_MISMATCH = "database_target_mismatch"
R_APPROVAL_PAYLOAD_MISSING = "approval_payload_missing"
R_APPROVAL_PAYLOAD_MALFORMED = "approval_payload_malformed"
R_APPROVAL_PAYLOAD_PIN_MISMATCH = "approval_payload_pin_mismatch"
R_APPROVAL_UNREADABLE = "approval_record_unreadable"
R_APPROVAL_VERSION = "approval_record_version"
R_APPROVAL_RELEASE_MISMATCH = "approval_release_mismatch"
R_APPROVAL_NOT_BOUND = "approval_not_bound_to_this_source"
R_APPROVAL_TARGET_MISMATCH = "approval_target_mismatch"
R_APPROVAL_PHASE_MISMATCH = "approval_phase_mismatch"
R_APPROVAL_MISSING = "approval_missing"
R_APPROVAL_EXPIRED = "approval_expired"
R_APPROVAL_TIMESTAMP_INVALID = "approval_timestamp_invalid"
R_APPROVAL_TIMESTAMP_ORDER = "approval_timestamp_order"
R_ROLLBACK_SNAPSHOT_MISSING = "deployment_rollback_snapshot_missing"
R_SCHEMA_ROLLBACK_INVALID = "schema_rollback_not_bound"
R_SCHEMA_ROLLBACK_STALE = "schema_rollback_evidence_stale"
R_BACKUP_EVIDENCE_MISSING = "backup_evidence_missing"
R_BACKUP_IDENTITY_MISMATCH = "backup_identity_mismatch"
R_BACKUP_SIZE_INVALID = "backup_size_invalid"
R_BACKUP_NOT_COMPLETED = "backup_not_completed"
R_BACKUP_NOT_RESTORABLE = "backup_not_restorable"
R_BACKUP_TOO_OLD = "backup_too_old"
R_RESTORE_NOT_VERIFIED = "restore_not_verified"
R_FLAG_ENABLED = "shopify_flag_enabled"
R_COMM_FLAG_ENABLED = "communication_send_flag_enabled"
R_COMM_SCHEMA_MISMATCH = "communication_schema_mismatch"
R_PILOT_ADMISSION_INVALID = "pilot_admission_invalid"
R_CATALOG_IMPORT_BOUND_INVALID = "catalog_import_bound_invalid"
R_CONNECT_FAILED = "connect_failed"
R_TIMEOUT = "statement_or_lock_timeout"
R_WALL_TIMEOUT = "wall_timeout"
R_LOCK_DENIED = "lock_denied"
R_UNLOCK_FAILED = "unlock_failed"
R_SERVER_IN_RECOVERY = "server_in_recovery"
R_SERVER_READ_ONLY = "database_default_read_only"
R_SCHEMA_NOT_PUBLIC = "current_schema_not_public"
R_SEARCH_PATH_MISMATCH = "search_path_mismatch"
R_DATABASE_NAME_MISMATCH = "database_name_mismatch"
R_VERSION_TABLE_MISSING = "alembic_version_table_missing"
R_VERSION_ROWS_DUPLICATE = "alembic_version_duplicate_rows"
R_VERSION_ROWS_UNKNOWN = "alembic_version_unknown_rows"
R_VERSION_ROWS_MISMATCH = "alembic_version_rows_mismatch"
R_FOOTPRINT_MISSING = "shopify_footprint_missing"
R_FOOTPRINT_UNEXPECTED = "shopify_footprint_unexpected"
R_SHOPIFY_DEFINITION_DRIFT = "shopify_definition_drift"
R_RETAINED_TABLE_MISSING = "retained_table_missing"
R_RETAINED_DEFINITION_DRIFT = "retained_definition_drift"
R_RETAINED_IDENTITY_DRIFT = "retained_identity_drift"
R_RELATION_REPLACED = "relation_replaced_during_run"
R_EXPECTATIONS_UNREADABLE = "schema_expectations_unreadable"
R_EXPECTATIONS_STALE = "schema_expectations_not_for_this_source"
R_INTERNAL_ERROR = "internal_error"


def phase(name: str) -> Optional[Dict[str, Any]]:
    """The deployment phase definition, or None for anything that is not one (S1/S2 included)."""
    return DEPLOY_PHASES.get(str(name or "")) or POST_INSTALL_PHASES.get(str(name or ""))


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def parse_utc(value: object) -> Optional[_dt.datetime]:
    """``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` -> aware UTC datetime; anything else -> None."""
    raw = str(value or "").strip()
    if not UTC_RE.match(raw):
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


def target_label(target: Mapping[str, Any]) -> str:
    return f"{target['host']}:{target['port']}/{target['database']}"


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


def normalize(value: Any) -> Any:
    """JSON round-trip shape: tuples become lists so file and live forms compare equal."""
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    return value


def evaluate_footprint(observed: Mapping[str, Any], phase_def: Mapping[str, Any],
                       shopify_expectations: Mapping[str, Any]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Exact Shopify footprint for a deployment phase, by canonical definition.

    ``observed``: ``tables`` (every ``shopify_*`` table in any user schema, as
    ``schema.name``), ``definitions`` (canonical definition per expected
    public table that exists), ``sequences`` (every sequence in any user
    schema that is Shopify-named **or** owned by an expected table, as
    ``[schema.sequence, owner_table_or_null, owner_column_or_null]``) and
    ``stray_count`` (every other ``shopify_*`` relation, type or function in
    any user schema, and every ``*shopify*`` constraint outside the expected
    tables). At S0 no sequence may exist; at S3 the sequence set must equal
    the pinned one exactly (name, owner table, owner column). Only expected
    names are ever reported; unexpected objects are counted.
    """
    expected = [f"public.{t}" for t in phase_def["tables"]]
    tables = set(observed.get("tables") or ())
    missing = sorted(t for t in expected if t not in tables)
    unexpected_count = len(tables - set(expected))
    stray_count = int(observed.get("stray_count") or 0)
    pinned_sequences = {tuple(s) for s in (shopify_expectations.get("sequences") or [])} if phase_def["tables"] else set()
    observed_sequences = {tuple(s) for s in (observed.get("sequences") or [])}
    pinned_names = {s[0] for s in pinned_sequences}
    extra_sequences = sum(1 for s in observed_sequences - pinned_sequences if s[0] not in pinned_names)
    sequence_drift = sorted(s[0] for s in (pinned_sequences ^ observed_sequences) if s[0] in pinned_names)
    missing_sequences = sorted(name for name in pinned_names if name not in {s[0] for s in observed_sequences})
    drift: List[str] = []
    for table in phase_def["tables"]:
        definition = (observed.get("definitions") or {}).get(table)
        pinned = (shopify_expectations.get("tables") or {}).get(table)
        if definition is not None and (pinned is None or normalize(definition) != normalize(pinned)):
            drift.append(table)
    detail = {"missing": [m.split(".", 1)[1] for m in missing] + missing_sequences, "unexpected_count": unexpected_count + extra_sequences,
              "stray_count": stray_count, "drift": sorted(set(drift) | set(sequence_drift))}
    if missing or missing_sequences:
        return FAIL, R_FOOTPRINT_MISSING, detail
    if unexpected_count or stray_count or extra_sequences:
        return FAIL, R_FOOTPRINT_UNEXPECTED, detail
    if drift or sequence_drift:
        return FAIL, R_SHOPIFY_DEFINITION_DRIFT, detail
    return PASS, None, detail


def evaluate_retained(observed: Mapping[str, Any], expectations: Mapping[str, Any]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Canonical definitions of the retained relations against the pinned expectations. Table names only."""
    drift: List[str] = []
    missing: List[str] = []
    expected_tables = expectations.get("tables") or {}
    observed_tables = observed.get("tables") or {}
    for table, definition in expected_tables.items():
        actual = observed_tables.get(table)
        if actual is None:
            missing.append(table)
        elif normalize(actual) != normalize(definition):
            drift.append(table)
    nonce_actual = observed.get("nonce")
    if nonce_actual is None:
        missing.append(NONCE_TABLE)
    elif normalize(nonce_actual) != normalize(expectations.get("nonce") or {}):
        drift.append(NONCE_TABLE)
    identity: List[str] = []
    for table, pk_type in (expectations.get("pk_types") or {}).items():
        if (observed.get("pk_types") or {}).get(table) != pk_type:
            identity.append(table)
    expected_uniques = {tuple(x) for x in (expectations.get("unique_definitions") or [])}
    observed_uniques = {tuple(x) for x in (observed.get("unique_definitions") or [])}
    for table, _definition in sorted(expected_uniques - observed_uniques):
        identity.append(table)
    if {tuple(x) for x in (expectations.get("sequences") or [])} != {tuple(x) for x in (observed.get("sequences") or [])}:
        identity.append("sequence_ownership")
    detail = {"missing": sorted(set(missing)), "drift": sorted(set(drift)), "identity": sorted(set(identity))}
    if missing:
        return FAIL, R_RETAINED_TABLE_MISSING, detail
    if drift:
        return FAIL, R_RETAINED_DEFINITION_DRIFT, detail
    if identity:
        return FAIL, R_RETAINED_IDENTITY_DRIFT, detail
    return PASS, None, detail


def evaluate_relation_identity(before: Mapping[str, Any], after: Mapping[str, Any]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """The relations checked must be the same objects at the end of the run as at its start."""
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    if changed:
        return FAIL, R_RELATION_REPLACED, {"relations": [n for n in changed if n in IDENTITY_TABLES or n in SHOPIFY_TABLES]}
    return PASS, None, {"relations": len(before)}


def _catalog_import_bound_valid(raw: object) -> bool:
    """The import's shop bound names exactly PILOT_SHOP, deadline within seven days."""
    try:
        if not isinstance(raw, str) or len(raw) > 4096:
            return False

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        entries = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(entries, dict) or set(entries) != {PILOT_SHOP}:
            return False
        entry = entries[PILOT_SHOP]
        if not isinstance(entry, dict) or set(entry) != {"expires_at"}:
            return False
        expires = entry["expires_at"]
        if not isinstance(expires, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expires):
            return False
        deadline = parse_utc(expires)
        now = _dt.datetime.now(_dt.timezone.utc)
        return deadline is not None and now < deadline <= now + _dt.timedelta(days=CATALOG_IMPORT_MAX_DAYS)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return False


def evaluate_flags(environ: Mapping[str, str], phase_name: str = "S3") -> Tuple[str, Optional[str], Dict[str, Any]]:
    enabled = sorted(name for name in SHOPIFY_FLAG_ENVS if truthy(environ.get(name)))
    if phase_name == "S4_COMM_DORMANT" and truthy(environ.get(COMM_SEND_FLAG_ENV)):
        return FAIL, R_COMM_FLAG_ENABLED, {"enabled": [COMM_SEND_FLAG_ENV]}
    if phase_name == "S4_PILOT":
        required = {"NAHLA_SHOPIFY_CONNECTION_ENABLED", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED"}
        if not required <= set(enabled) <= required | {CATALOG_IMPORT_FLAG}:
            return FAIL, R_FLAG_ENABLED, {"enabled": enabled}
        # The live server still requires a valid private code and verifies the
        # Shopify online owner. This source-bound contract restricts configured
        # admission to the one development shop; malformed peers also refuse.
        try:
            raw = str(environ.get("SHOPIFY_INSTALL_PILOT_ADMISSION") or "")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate")
                    result[key] = value
                return result
            entries = json.loads(raw, object_pairs_hook=unique) if len(raw) <= 4096 else None
            if not isinstance(entries, dict) or set(entries) != {PILOT_SHOP}:
                raise ValueError("shops")
            admission = entries[PILOT_SHOP]
            if not isinstance(admission, dict) or set(admission) != {"sha256", "expires_at"}:
                raise ValueError("fields")
            if not isinstance(admission["sha256"], str) or not DIGEST_RE.fullmatch(admission["sha256"]):
                raise ValueError("hash")
            expires = admission["expires_at"]
            if not isinstance(expires, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expires):
                raise ValueError("deadline_format")
            deadline = parse_utc(expires)
            now = _dt.datetime.now(_dt.timezone.utc)
            if deadline is None or not now < deadline <= now + _dt.timedelta(days=7):
                raise ValueError("deadline")
        except (ValueError, TypeError, RecursionError, OverflowError):
            return FAIL, R_PILOT_ADMISSION_INVALID, {"enabled": enabled}
        if CATALOG_IMPORT_FLAG in enabled:
            if not _catalog_import_bound_valid(environ.get(CATALOG_IMPORT_SHOPS_ENV)):
                return FAIL, R_CATALOG_IMPORT_BOUND_INVALID, {"enabled": enabled}
            return PASS, None, {"enabled": enabled, "pilot_shops": 1, "catalog_import_shops": 1}
        return PASS, None, {"enabled": enabled, "pilot_shops": 1}
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


def _present(mapping: Any, fields: Tuple[str, ...]) -> bool:
    if not isinstance(mapping, dict):
        return False
    for name in fields:
        value = mapping.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


DORMANT_PRIOR_FIELDS: Tuple[str, ...] = ("rollback_mode", "source_sha", "runtime_inventory_digest",
                                          "runtime_inventory_count", "schema_expectations_sha256")


def _pinned_dormant_prior(artifact: Mapping[str, Any], bindings: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The one earlier source the reviewed manifest names as a dormant rollback, or None.

    Exact fields only: a full commit SHA, that source's runtime inventory, and
    its S4 schema-expectations hash, which must equal this source's (the same
    canonical seventeen-table definitions, so the earlier release accepts the
    0124 state this release leaves)."""
    prior = artifact.get("dormant_prior_rollback")
    if (not isinstance(prior, dict) or set(prior) != set(DORMANT_PRIOR_FIELDS)
            or prior.get("rollback_mode") != "pinned_prior_flags_off"
            or not isinstance(prior.get("source_sha"), str) or not RELEASE_SHA_RE.fullmatch(prior["source_sha"])
            or not isinstance(prior.get("runtime_inventory_digest"), str)
            or not DIGEST_RE.fullmatch(prior["runtime_inventory_digest"])
            or type(prior.get("runtime_inventory_count")) is not int or prior["runtime_inventory_count"] <= 0
            or not isinstance(prior.get("schema_expectations_sha256"), str)
            or not DIGEST_RE.fullmatch(prior["schema_expectations_sha256"])
            or prior.get("schema_expectations_sha256") != bindings.get("prior_schema_expectations_sha256")):
        return None
    return prior


def _evaluate_comm_schema_rollback(evidence: Any, snapshot: Mapping[str, Any], bindings: Mapping[str, Any], *,
                                   release_sha: str, target: Mapping[str, str], approved_at: _dt.datetime,
                                   now: _dt.datetime) -> Optional[str]:
    """Require a separately pinned 0125-compatible, flags-OFF rollback source."""
    artifact = bindings.get("artifact")
    if not isinstance(artifact, dict):
        return R_SCHEMA_ROLLBACK_INVALID
    expected_flags = {name: "false" for name in SHOPIFY_FLAG_ENVS + (COMM_SEND_FLAG_ENV,)}
    prior = artifact.get("compatible_rollback")
    if (artifact.get("record_version") != 2 or artifact.get("status") != "READY"
            or artifact.get("rollback_mode") != "compatible_source_flags_off"
            or artifact.get("phase") != "S4_COMM_DORMANT"
            or artifact.get("database_heads") != ["0111", "0113", "0125"]
            or artifact.get("shopify_table_count") != 17
            or artifact.get("bootstrap_target") != "0093"
            or artifact.get("schema_action") != "retain_0125_no_downgrade"
            or artifact.get("expectations_path") != COMM_EXPECTATIONS_RELATIVE_PATH
            or artifact.get("shopify_expectations_path") != INSTALL_EXPECTATIONS_RELATIVE_PATH
            or artifact.get("flags") != expected_flags
            or not isinstance(prior, dict)
            or set(prior) != {"source_sha", "runtime_inventory_digest", "runtime_inventory_count",
                              "shopify_expectations_sha256", "communication_expectations_sha256",
                              "independent_review_ref"}
            or not isinstance(prior.get("source_sha"), str)
            or not RELEASE_SHA_RE.fullmatch(prior["source_sha"])
            or prior["source_sha"] == release_sha
            or not isinstance(prior.get("runtime_inventory_digest"), str)
            or not DIGEST_RE.fullmatch(prior["runtime_inventory_digest"])
            or type(prior.get("runtime_inventory_count")) is not int
            or prior["runtime_inventory_count"] <= 0
            or not isinstance(prior.get("shopify_expectations_sha256"), str)
            or not DIGEST_RE.fullmatch(prior["shopify_expectations_sha256"])
            or prior.get("communication_expectations_sha256") != bindings.get("communication_schema_sha256")
            or not _present(prior, ("independent_review_ref",))):
        return R_SCHEMA_ROLLBACK_INVALID
    if (not isinstance(evidence, dict) or evidence.get("record_version") != 2
            or evidence.get("phase") != "S4_COMM_DORMANT"
            or evidence.get("source_sha") != prior["source_sha"]
            or evidence.get("railway_deployment_id") != snapshot.get("railway_deployment_id")
            or snapshot.get("source_sha") != prior["source_sha"]
            or evidence.get("target") != dict(target)
            or evidence.get("flags") != expected_flags
            or evidence.get("database_heads") != ["0111", "0113", "0125"]
            or evidence.get("table_names") != sorted(INSTALL_TABLES + ("salla_communication_function_nonces",))
            or evidence.get("runtime_inventory_digest") != prior["runtime_inventory_digest"]
            or evidence.get("runtime_inventory_count") != prior["runtime_inventory_count"]
            or evidence.get("schema_expectations_sha256") != prior["shopify_expectations_sha256"]
            or evidence.get("communication_schema_sha256") != bindings.get("communication_schema_sha256")
            or evidence.get("artifact_sha256") != bindings.get("artifact_sha256")
            or evidence.get("independent_review_ref") != prior["independent_review_ref"]):
        return R_SCHEMA_ROLLBACK_INVALID
    verified = parse_utc(evidence.get("verified_at_utc"))
    expires = parse_utc(evidence.get("valid_until_utc"))
    if verified is None or expires is None or verified != parse_utc(snapshot.get("verified_redeployable_at_utc")):
        return R_SCHEMA_ROLLBACK_INVALID
    max_age = _dt.timedelta(hours=ROLLBACK_MAX_AGE_HOURS)
    if not verified <= approved_at <= now < expires or now - verified > max_age or expires > verified + max_age:
        return R_SCHEMA_ROLLBACK_STALE
    return None


def evaluate_schema_rollback(evidence: Any, snapshot: Mapping[str, Any], bindings: Optional[Mapping[str, Any]], *,
                             phase_name: str, release_sha: str, runtime_digest: str, runtime_count: int,
                             target: Mapping[str, str], approved_at: _dt.datetime, now: _dt.datetime) -> Optional[str]:
    """S4 rollback is a flags-OFF S4_DORMANT deployment of a 0124-compatible source.

    Bind the external evidence to the source-pinned manifest and full canonical
    seventeen-table definitions. A generic S3 redeployable UUID cannot satisfy
    this contract. This is validation only; it never creates an approval.

    * S4_PILOT: only the exact reviewed source of this release, with flags OFF.
    * S4_DORMANT: that, or — so a schema-preserving release can be deployed
      before any deployment of it exists — the single earlier source this
      release's manifest pins (``dormant_prior_rollback``): the evidence must
      name that full SHA (the deployment snapshot too), its pinned inventory,
      this release's identical S4 definitions and every flag ``"false"``. An
      older source is never accepted merely because it is redeployable.
    """
    if not isinstance(evidence, dict) or not isinstance(bindings, dict):
        return R_SCHEMA_ROLLBACK_INVALID
    if phase_name == "S4_COMM_DORMANT":
        return _evaluate_comm_schema_rollback(evidence, snapshot, bindings,
                                              release_sha=release_sha, target=target,
                                              approved_at=approved_at, now=now)
    artifact = bindings.get("artifact")
    if (not isinstance(artifact, dict) or artifact.get("record_version") != 1
            or artifact.get("rollback_mode") != "same_source_flags_off"
            or artifact.get("phase") != "S4_DORMANT" or artifact.get("database_heads") != ["0111", "0113", "0124"]
            or artifact.get("shopify_table_count") != 17 or artifact.get("bootstrap_target") != "0093"
            or artifact.get("schema_action") != "retain_0124_no_downgrade"
            or artifact.get("expectations_path") != INSTALL_EXPECTATIONS_RELATIVE_PATH
            or not isinstance(artifact.get("flags"), dict) or set(artifact["flags"]) != set(SHOPIFY_FLAG_ENVS)
            or any(value != "false" for value in artifact["flags"].values())):
        return R_SCHEMA_ROLLBACK_INVALID
    rollback_source = evidence.get("source_sha")
    if rollback_source == release_sha:
        inventory_digest, inventory_count = runtime_digest, runtime_count
    else:
        prior = _pinned_dormant_prior(artifact, bindings) if phase_name == "S4_DORMANT" else None
        if (prior is None or rollback_source != prior["source_sha"]
                or evidence.get("flags") != {name: "false" for name in SHOPIFY_FLAG_ENVS}):
            return R_SCHEMA_ROLLBACK_INVALID
        inventory_digest, inventory_count = prior["runtime_inventory_digest"], prior["runtime_inventory_count"]
    if (evidence.get("record_version") != 1 or evidence.get("phase") != "S4_DORMANT"
            or snapshot.get("source_sha") != rollback_source
            or evidence.get("railway_deployment_id") != snapshot.get("railway_deployment_id")
            or evidence.get("target") != dict(target)
            or evidence.get("database_heads") != ["0111", "0113", "0124"]
            or evidence.get("table_names") != sorted(INSTALL_TABLES)
            or evidence.get("runtime_inventory_digest") != inventory_digest
            or type(evidence.get("runtime_inventory_count")) is not int
            or evidence.get("runtime_inventory_count") != inventory_count
            or not _present(evidence, ("independent_review_ref",))):
        return R_SCHEMA_ROLLBACK_INVALID
    for name in ("artifact_sha256", "schema_expectations_sha256"):
        expected = bindings.get(name)
        if not isinstance(expected, str) or not DIGEST_RE.fullmatch(expected) or evidence.get(name) != expected:
            return R_SCHEMA_ROLLBACK_INVALID
    verified = parse_utc(evidence.get("verified_at_utc"))
    expires = parse_utc(evidence.get("valid_until_utc"))
    if verified is None or expires is None or verified != parse_utc(snapshot.get("verified_redeployable_at_utc")):
        return R_SCHEMA_ROLLBACK_INVALID
    max_age = _dt.timedelta(hours=ROLLBACK_MAX_AGE_HOURS)
    if not verified <= approved_at <= now < expires or now - verified > max_age or expires > verified + max_age:
        return R_SCHEMA_ROLLBACK_STALE
    return None


def evaluate_approval(record: Any, *, phase_name: str, release_sha: str, runtime_digest: str, runtime_count: int,
                      target: Mapping[str, str], backup_service: str, backup_volume: str,
                     now: _dt.datetime, rollback_bindings: Optional[Mapping[str, Any]] = None) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """The owner's external approval for exactly this release, source, target, phase and backup.

    Fail-closed on: an unreadable or wrong-version record; a different release
    commit, inventory, target or phase; an unapproved or incomplete approval;
    timestamps that are not UTC or out of order (card <= observed <= approved
    <= now <= expiry); a deployment rollback snapshot without a UUID
    deployment id, a full source sha, a verifier and a verification time
    ``<= approved <= now``; backup evidence
    for another service or volume, with a non-positive or absurd size, not
    completed, not restorable, or older than ``BACKUP_MAX_AGE_HOURS`` at
    approval or at run time; and, for a schema-changing phase, a restore that
    is not verified with a reference and a timestamp between the backup and
    the approval.
    """
    detail: Dict[str, Any] = {"phase": phase_name}
    if not isinstance(record, dict):
        return FAIL, R_APPROVAL_UNREADABLE, detail
    if record.get("record_version") != APPROVAL_RECORD_VERSION:
        return FAIL, R_APPROVAL_VERSION, detail
    if record.get("release_sha") != release_sha:
        return FAIL, R_APPROVAL_RELEASE_MISMATCH, detail
    if record.get("runtime_inventory_digest") != runtime_digest or record.get("runtime_inventory_count") != runtime_count:
        return FAIL, R_APPROVAL_NOT_BOUND, detail
    bound_target = record.get("target")
    if not isinstance(bound_target, dict) or any(bound_target.get(key) != target.get(key) for key in ("project", "environment", "service", "db")):
        return FAIL, R_APPROVAL_TARGET_MISMATCH, detail
    if record.get("phase") != phase_name:
        return FAIL, R_APPROVAL_PHASE_MISMATCH, detail
    approval = record.get("approval")
    if not _present(approval, APPROVAL_FIELDS) or approval.get("approved") is not True:
        return FAIL, R_APPROVAL_MISSING, detail
    approved_at = parse_utc(approval.get("approved_at_utc"))
    valid_until = parse_utc(approval.get("valid_until_utc"))
    if approved_at is None or valid_until is None:
        return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
    if valid_until <= approved_at:
        return FAIL, R_APPROVAL_TIMESTAMP_ORDER, detail
    if now < approved_at or now > valid_until:
        return FAIL, R_APPROVAL_EXPIRED, detail
    snapshot = record.get("deployment_rollback_snapshot")
    if not _present(snapshot, ROLLBACK_SNAPSHOT_FIELDS):
        return FAIL, R_ROLLBACK_SNAPSHOT_MISSING, detail
    if not UUID_RE.match(str(snapshot.get("railway_deployment_id"))) or not RELEASE_SHA_RE.match(str(snapshot.get("source_sha"))):
        return FAIL, R_ROLLBACK_SNAPSHOT_MISSING, detail
    verified_at = parse_utc(snapshot.get("verified_redeployable_at_utc"))
    if verified_at is None:
        return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
    if not (verified_at <= approved_at <= now):
        return FAIL, R_APPROVAL_TIMESTAMP_ORDER, detail
    if phase_name in POST_INSTALL_PHASES:
        reason = evaluate_schema_rollback(record.get("schema_rollback"), snapshot, rollback_bindings,
                                          phase_name=phase_name, release_sha=release_sha, runtime_digest=runtime_digest,
                                          runtime_count=runtime_count, target=target, approved_at=approved_at, now=now)
        if reason:
            return FAIL, reason, detail
    recovery = record.get("db_recovery_evidence")
    backup = recovery.get("backup") if isinstance(recovery, dict) else None
    if not _present(backup, BACKUP_FIELDS):
        return FAIL, R_BACKUP_EVIDENCE_MISSING, detail
    if backup.get("service_id") != backup_service or backup.get("volume_id") != backup_volume:
        return FAIL, R_BACKUP_IDENTITY_MISMATCH, detail
    size = backup.get("size_mb")
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not (0 < size <= BACKUP_MAX_SIZE_MB):
        return FAIL, R_BACKUP_SIZE_INVALID, detail
    if backup.get("completed") is not True:
        return FAIL, R_BACKUP_NOT_COMPLETED, detail
    if backup.get("restore_available") is not True:
        return FAIL, R_BACKUP_NOT_RESTORABLE, detail
    card_time = parse_utc(backup.get("card_timestamp_utc"))
    observed_at = parse_utc(backup.get("observed_at_utc"))
    if card_time is None or observed_at is None:
        return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
    if not (card_time <= observed_at <= approved_at):
        return FAIL, R_APPROVAL_TIMESTAMP_ORDER, detail
    max_age = _dt.timedelta(hours=BACKUP_MAX_AGE_HOURS)
    if approved_at - card_time > max_age or now - card_time > max_age:
        return FAIL, R_BACKUP_TOO_OLD, detail
    if phase_name in SCHEMA_CHANGING_PHASES:
        if recovery.get("restore_verified") is not True or not _present(recovery, ("restore_verification_ref", "restore_verified_at_utc")):
            return FAIL, R_RESTORE_NOT_VERIFIED, detail
        restored_at = parse_utc(recovery.get("restore_verified_at_utc"))
        if restored_at is None:
            return FAIL, R_APPROVAL_TIMESTAMP_INVALID, detail
        if not (card_time <= restored_at <= approved_at):
            return FAIL, R_APPROVAL_TIMESTAMP_ORDER, detail
    return PASS, None, detail


__all__ = [name for name, value in list(globals().items())
           if not name.startswith("_") and name not in {"re", "Any", "Dict", "FrozenSet", "List", "Mapping", "Optional", "Tuple"}]
