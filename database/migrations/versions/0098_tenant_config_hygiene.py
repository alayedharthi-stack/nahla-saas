"""0098 — retire stale tenant config leftovers (platform-wide).

1. Drop retired ``ai_settings.persona_composer_allowlist_tenants``.
2. Align ``store_settings.platform_type`` with authoritative connection:
   enabled Integration.provider, else non-empty credentials, else ``custom``.

Idempotent. No tenant-id special cases. Does not touch Brain/LLM settings
such as ``persona_composer_enabled``, ``store_ai_mode``, or ``_kb_backup_v1``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import op
from sqlalchemy.orm import Session

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def _ensure_backend_path() -> None:
    repo = Path(__file__).resolve().parents[3]
    backend = repo / "backend"
    for path in (str(repo), str(backend)):
        if path not in sys.path:
            sys.path.insert(0, path)


def upgrade() -> None:
    _ensure_backend_path()
    from core.tenant_config_hygiene import normalize_all_tenant_settings  # noqa: PLC0415

    bind = op.get_bind()
    session = Session(bind=bind)
    try:
        summary = normalize_all_tenant_settings(session)
        session.commit()
        print(
            "[0098] tenant_config_hygiene scanned=%s changed=%s "
            "persona_allowlist_removed=%s platform_type_updated=%s"
            % (
                summary.get("scanned"),
                summary.get("changed"),
                summary.get("persona_allowlist_removed"),
                summary.get("platform_type_updated"),
            )
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def downgrade() -> None:
    # Retired keys and corrected labels are not reconstructable.
    return
