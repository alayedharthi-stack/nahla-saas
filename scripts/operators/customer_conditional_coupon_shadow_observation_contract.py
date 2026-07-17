"""Closed contract for staging conditional-coupon shadow observation probes."""
from __future__ import annotations

REPORT_SCHEMA_VERSION = "coupon_shadow_observation_probe_v1"

# Container layout baked by the shared Dockerfile (WORKDIR /app, COPY . .).
DEPLOYMENT_APP_ROOT = "/app"
APP_CONTAINER_SYS_PATH = (
    f"{DEPLOYMENT_APP_ROOT}",
    f"{DEPLOYMENT_APP_ROOT}/backend",
    f"{DEPLOYMENT_APP_ROOT}/database",
)

# Deployed ``/app`` exposes SessionLocal only via ``database.session`` (``database/__init__.py`` is empty).
SESSION_LOCAL_MODULE = "database.session"
VALID_SESSION_LOCAL_IMPORT = "from database.session import SessionLocal"
INVALID_LEGACY_SESSION_LOCAL_IMPORT = "from database import SessionLocal"

OBSERVATION_PROBE_MESSAGE = "conditional coupon after min orders for loyalty offer"

CODE_SHADOW_FLAG_NOT_ENABLED = "shadow_flag_not_enabled"
CODE_COMPOSE_FLAG_ENABLED = "compose_flag_enabled"
CODE_FIXTURE_CONVERSATION_MISSING = "fixture_conversation_missing"
CODE_FIXTURE_CONVERSATION_NOT_FOUND = "fixture_conversation_not_found"
CODE_SESSION_LOCAL_IMPORT_INVALID = "session_local_import_invalid"

__all__ = [
    "APP_CONTAINER_SYS_PATH",
    "CODE_COMPOSE_FLAG_ENABLED",
    "CODE_FIXTURE_CONVERSATION_MISSING",
    "CODE_FIXTURE_CONVERSATION_NOT_FOUND",
    "CODE_SESSION_LOCAL_IMPORT_INVALID",
    "CODE_SHADOW_FLAG_NOT_ENABLED",
    "DEPLOYMENT_APP_ROOT",
    "INVALID_LEGACY_SESSION_LOCAL_IMPORT",
    "OBSERVATION_PROBE_MESSAGE",
    "REPORT_SCHEMA_VERSION",
    "SESSION_LOCAL_MODULE",
    "VALID_SESSION_LOCAL_IMPORT",
]
