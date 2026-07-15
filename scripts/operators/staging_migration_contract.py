"""Closed contract for staging legacy migration 0016 → 0024 operator gates."""
from __future__ import annotations

BASE_REVISION = "0016"
TARGET_REVISION = "0024"

CONFIRMATION_TOKEN = "RUN_STAGING_0016_TO_0024"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800

REQUIRED_TABLES = (
    "automation_executions",
    "webhook_guardian_log",
    "integrity_events",
    "webhook_events",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "smart_automations": ("ix_smart_automations_trigger_event",),
    "automation_executions": (
        "ix_automation_executions_event_automation",
        "ix_automation_executions_tenant_id",
    ),
    "webhook_guardian_log": (
        "ix_webhook_guardian_log_tenant_created",
        "ix_webhook_guardian_log_event",
    ),
    "integrity_events": ("ix_integrity_events_created_at",),
    "webhook_events": (
        "ix_webhook_events_status_retry",
        "ix_webhook_events_tenant_received",
        "uq_webhook_events_provider_event",
    ),
    "orders": ("uq_orders_tenant_external_id",),
    "whatsapp_connections": ("uq_wa_conn_waba_id",),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "smart_automations": ("trigger_event",),
    "whatsapp_connections": ("last_webhook_received_at",),
}
