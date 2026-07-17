"""Closed contract for staging 0088 + 0089 attachment operator gates.

Repository note: migration ``0089`` (`0089_conversation_a1_subject_bindings.py`) is a
sibling branch from ``0087``, parallel to validated ``0088``. This operator attaches
``0089`` onto staging already at validated ``0088`` only — never ``head``, never
``0087``/``expand``, and never re-runs or reverts ``0088`` validation state.

Post-success ``alembic_version`` must contain **both** ``0088`` and ``0089``.
"""
from __future__ import annotations

from scripts.operators.staging_migration_0087_to_0088_contract import (
    CAPABILITY_KEY,
    CAPABILITY_STATE_VALIDATED,
    VALIDATION_REVISION as A1_VALIDATION_REVISION,
)

BASE_REVISION = "0088"
TARGET_REVISION = "0089"
EXPECTED_POST_SUCCESS_REVISIONS = frozenset({BASE_REVISION, TARGET_REVISION})

CONFIRMATION_TOKEN = "RUN_STAGING_0088_ATTACH_0089"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0088_ATTACH_0089_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800
MIN_MIGRATION_TIMEOUT_SEC = 300
MAX_MIGRATION_TIMEOUT_SEC = 3600

REJECTED_START_REVISIONS = frozenset({"0087", "0089"})
FORBIDDEN_PRE_ATTACH_TABLES = ("conversation_a1_subject_bindings",)

DR_RESTORE_PROFILE_REVISION = BASE_REVISION

CASB_TABLE = "conversation_a1_subject_bindings"
CONVERSATIONS_COMPOSITE_INDEX = "uq_conversations_tenant_id"
CASB_PARTIAL_UNIQUE_INDEX = "uq_casb_tenant_conversation_active"
CASB_STATE_INDEX = "ix_casb_tenant_conversation_state"
CASB_FOREIGN_KEYS = (
    "fk_casb_tenant_conversation",
    "fk_casb_tenant_internal_customer",
)
CASB_CHECK_CONSTRAINTS = (
    "chk_casb_binding_state",
    "chk_casb_subject_kind",
    "chk_casb_subject_xor",
    "chk_casb_state_revocation_timestamp",
)

MANIFEST_SCHEMA_VERSION = "staging_migration_0088_attach_0089_v1"
